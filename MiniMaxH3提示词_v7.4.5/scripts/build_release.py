#!/usr/bin/env python3
"""受版本控制的正规发布构建器（P93 拍板选项②落地）。

这是 minimax-h3-craft 发布面唯一的诚实产出路径。全流程 fail-closed，
判定层完全交给 `closure_gate_contract`（本文件自己不发明任何「通过」口径）：

  1. 预检：tracked 面必须干净（根 `release-manifest.json` 每次重建，天然豁免），
     且发布链路的关键文件（本构建器、批次驱动、e2e 冻结语料、守卫测试）
     必须已在 HEAD 里——证据只能绑定已提交的代码；
  2. 跑冻结的 30 例本地批次（`scripts/run_local_batch.py` + 原生度量器）；
  3. 跑 `scripts/collect_closure_evidence.py --local-run-dir <批次>` 铸造
     一个全新证据 run（静态/CLI 段套件当场执行并留日志，本地段逐字节重算）；
  4. 用 `verify_index` 重算证据索引每一条的 sha256/bytes，再用
     `release_allowed("local-stable", ...)` 问判定层能不能签发——判定层
     说不，就拒绝签发（exit 1），绝不绕过；
  5. 把全部 git tracked 文件 + 新证据 run 复制进仓库之外的 dist 目录，
     并当场生成一份全新 `release-manifest.json`（新哈希、新 git_commit、
     新 evidence run 绑定）。REAL_AGENT / MODEL_RENDER / VIDEO_REVERSE
     在没有在线通道的环境里如实 NOT_RUN 并写明原因——永不伪造 PASS。

注意：A08 的 local-stable 档位只要求三个本地可证门 PASS，外部三门以
NOT_RUN 随包发布是契约允许的诚实状态；若要把它们抬成 PASS，只有按
`closure_gate_contract` 的证据口径真跑出来一条路。

运行：

    python scripts/build_release.py [--dist <目录>]

退出码：0 已签发；1 拒绝签发（打印原因）；2 崩溃。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from closure_gate_contract import GATE_ORDER, LOCAL_GATES, release_allowed, verify_index  # noqa: E402
from versioning import (VersionValidationError, check_single_source_version,  # noqa: E402
                        get_version)

UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

# 根清单每轮重建，是唯一允许在构建时保持 dirty 的 tracked 文件。
DIRTY_EXEMPT = ("release-manifest.json",)

# 柜内过程文档（交接/工单/设计/验收）不是交付物：它们写的是开发柜的工作树路径，
# 收件人既不需要也读不懂，还把作者机器形状带进分发面（现场见
# _审查测试/test_release_no_private_path_surface.py 的 docstring）。
INTERNAL_DOC_PREFIX = "文档/柠檬"
_PRIVACY_TEXT_SUFFIX = (".md", ".txt", ".json", ".py", ".yml", ".yaml", ".html", ".cfg", ".toml")
_PRIVACY_PATTERNS = (
    re.compile(r"[A-Za-z]:[\\/]{1,2}(?:Users|home|迁移)", re.I),
    re.compile(r"[A-Za-z]:[\\/]?.{0,80}Janson", re.I),
)
_privacy_cache: dict[str, bool] = {}


def carries_author_machine_path(rel: str) -> bool:
    """这份文本件里是否有作者机绝对路径形状（命中即禁发，结果按路径缓存）。"""
    rel = str(rel).replace("\\", "/")
    if rel in _privacy_cache:
        return _privacy_cache[rel]
    path = os.path.join(REPO_ROOT, rel)
    hit = False
    if rel.lower().endswith(_PRIVACY_TEXT_SUFFIX) and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                hit = any(p.search(line) for p in _PRIVACY_PATTERNS for line in handle)
        except OSError:
            hit = False
    _privacy_cache[rel] = hit
    return hit

# 这些 tracked 文件必须已进 HEAD，否则证据绑定的就不是要发布的内容。
CRITICAL_TRACKED = (
    "scripts/build_release.py",
    "scripts/run_local_batch.py",
    "scripts/collect_closure_evidence.py",
    "scripts/closure_gate_contract.py",
    "scripts/generate_output.py",
    "scripts/measure_e2e_metrics.py",
    "scripts/storyboard_reader.py",
    "_审查测试/test_build_release.py",
    "_审查测试/test_storyboard_reader.py",
)

EXPECTED_CORPUS_COUNT = 30  # 与采集器的 30 例冻结口径一致

# 治理排除层把运行期日志（*.log）列为禁发（`gitguard.republish` 文件头同款口径，
# pre-commit 密钥闸门对 banned-path 直接拒绝入库）。发布面在构建器里就自己遵守：
# 门执行日志不随技能分发，其 sha256 完整封存于随包的 evidence-index.json /
# closure-report.json，可在构建环境逐字节重算——这是裁剪，不是伪造。
WITHHELD_SUFFIXES = (".log",)

WITHHELD_NOTE = (
    "运行期日志（*.log）按治理排除层（banned runtime files）不随发布面分发；"
    "每份日志的 sha256 与字节数已封存于随包发布的 evidence-index.json 与 "
    "closure-report.json，重跑采集器即可逐字节复算，未分发≠未验证。"
)

# 历史证据 run 默认不随包：多轮 closure-report.json 的 run_dir 写的是构建机
# 绝对路径（作者桌面/F 盘/维护目录），收件人不需要也读不懂。唯一例外是
# 随包凭证校验器点名的 3 个文件（解密见 references/protocol/verification-protocol.json
# 的 STATIC/CLI 两档 artifact_paths + release_surface 的 withheld_covered 回退）：
# 2 份索引 JSON 必须在场，2 份 .log 缺席但被 EVIDENCE_KEEP 的 evidence-index.json
# 以文件名封存 sha256。3 个文件均已验不含构建机绝对路径形状，且仍过 _blocked 隐私闸。
EVIDENCE_KEEP = (
    "evidence/20260923T010501Z-fd5712fd/evidence-index.json",
    "evidence/20260923T010501Z-fd5712fd/logs/static-index.json",
    "evidence/20260923T010501Z-fd5712fd/logs/cli-index.json",
)

# 新证据 run 的 closure-report 进发布面前必须脱敏：run_dir 是构建机绝对路径。
REDACTED_RUN_DIR = "REDACTED-BUILD-PATH"
_STRICT_PATH = re.compile(r"(?<![A-Za-z/:])[A-Za-z]:[\\/]|/(?:home|c/Users)/", re.I)


def is_withheld(rel: str) -> bool:
    """发布面禁发判定：与治理排除层同口径，只按文件名后缀。"""
    return rel.lower().endswith(WITHHELD_SUFFIXES)

PROVENANCE_NOTE = (
    "30 例本地批次语料（e2e/cases + e2e/expected）于 2026-09-25 依据路由/审计/"
    "约束契约文档重建：历史语料已随 283d750（7.3.1 工具链）一并丢失、不可恢复；"
    "批次驱动 scripts/run_local_batch.py 与产物均受版本控制，任何人可在 HEAD "
    "重算。REAL_AGENT / MODEL_RENDER / VIDEO_REVERSE 三门需要在线通道与授权"
    "素材，本机离线构建中如实 NOT_RUN。"
)


def sha_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def run_git(args: list[str]) -> str:
    proc = subprocess.run(["git", "-c", "core.quotepath=off"] + args,
                          cwd=REPO_ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="strict", check=True,
                          env=UTF8_ENV)
    return proc.stdout


def head_commit() -> str:
    return run_git(["rev-parse", "HEAD"]).strip()


def current_branch() -> str:
    try:
        return run_git(["symbolic-ref", "--short", "-q", "HEAD"]).strip()
    except subprocess.CalledProcessError:
        return "DETACHED_HEAD"


def _git_toplevel() -> str | None:
    """Monorepo 顶层目录；取不到时返回 None（调用方按单包处理）。"""
    try:
        top = run_git(["rev-parse", "--show-toplevel"]).strip()
    except subprocess.CalledProcessError:
        return None
    return top or None


def _package_prefix() -> str:
    """当前技能包相对 git 顶层的目录前缀（末尾带 /；单包仓库为空串）。

    MM-06：`git status --porcelain` 永远返回顶层相对路径（含包外改动），
    而预检的豁免/关键文件/套件口径全是包内相对路径。不去前缀、不 scope，
    包外一次改动就会误杀本包发布，而包内未入库套件反而因前缀对不上被漏掉。
    """
    top = _git_toplevel()
    if not top:
        return ""
    try:
        rel = os.path.relpath(REPO_ROOT, top).replace("\\", "/")
    except ValueError:
        return ""
    if rel in (".", ""):
        return ""
    return rel.rstrip("/") + "/"


def _status_path_inside_package(raw_path: str, prefix: str) -> str | None:
    """status 行路径归一为包内相对路径；包外返回 None。"""
    path = raw_path.strip().replace("\\", "/")
    if " -> " in path:  # rename/copy：只看新路径
        path = path.split(" -> ", 1)[1].strip().strip('"')
    if prefix:
        if not path.startswith(prefix):
            return None
        return path[len(prefix):]
    return path


def ls_tracked() -> list[str]:
    raw = run_git(["ls-files", "-z"])
    names = [p for p in raw.split("\0") if p]
    prefix = _package_prefix()
    if prefix and any(p.startswith(prefix) for p in names):
        # 顶层相对形态：只留本包并去前缀。
        return sorted(p[len(prefix):] for p in names if p.startswith(prefix))
    return sorted(names)


def dirty_tracked_paths() -> list[str]:
    """非豁免的 tracked 改动（不含 untracked 的 ?? 行；只看本包）。"""
    prefix = _package_prefix()
    out = []
    for line in run_git(["status", "--porcelain"]).splitlines():
        if not line.strip() or line.startswith("??"):
            continue
        path = _status_path_inside_package(line[3:], prefix)
        if path is None:
            continue
        if path not in DIRTY_EXEMPT:
            out.append(path)
    return out


def untracked_test_suites() -> list[str]:
    """`_审查测试` 里未入库的 test_*.py：静态段会真实执行它们（它们只是
    额外加测、不会替任何门造假），故记入 manifest 作披露而不阻断构建。"""
    prefix = _package_prefix()
    out = []
    for line in run_git(["status", "--porcelain"]).splitlines():
        if not line.startswith("?? "):
            continue
        path = _status_path_inside_package(line[3:], prefix)
        if path is None:
            continue
        if (path.startswith("_审查测试/")
                and os.path.basename(path).startswith("test_")
                and path.endswith(".py")):
            out.append(path)
    return sorted(set(out))


def preflight() -> dict:
    problems: list[str] = []
    try:
        check_single_source_version(REPO_ROOT)
    except VersionValidationError as exc:
        problems.append(f"单一真源版本号漂移（MM-01）：{exc}")
    tracked = ls_tracked()
    tracked_set = set(tracked)
    dirty = dirty_tracked_paths()
    if dirty:
        problems.append(f"tracked 面不干净（发布内容与 HEAD 不一致）: {dirty}")
    missing = [p for p in CRITICAL_TRACKED if p not in tracked_set]
    if missing:
        problems.append(f"发布链路文件未入库: {missing}")
    cases = sorted(p for p in tracked if p.startswith("e2e/cases/") and p.endswith(".json"))
    expected = sorted(p for p in tracked if p.startswith("e2e/expected/") and p.endswith(".json"))
    if len(cases) != EXPECTED_CORPUS_COUNT or len(expected) != EXPECTED_CORPUS_COUNT:
        problems.append(f"冻结语料未完整入库: cases={len(cases)} expected={len(expected)}"
                        f"（要求各 {EXPECTED_CORPUS_COUNT}）")
    warnings = untracked_test_suites()
    if warnings:
        print(f"[warn] 静态段将额外执行未入库套件（已在 manifest 中披露）: {warnings}")
    if problems:
        for p in problems:
            print(f"[preflight] {p}")
        raise SystemExit(1)
    return {"head": head_commit(), "tracked": tracked,
            "untracked_suites_executed": warnings}


def run_local_batch() -> dict:
    """跑冻结批次 + 原生度量器；度量门不过就拒绝（exit 1）。"""
    import run_local_batch as batch_driver
    batch = batch_driver.run_batch(
        os.path.join(REPO_ROOT, "e2e", "cases"),
        os.path.join(REPO_ROOT, "e2e", "expected"),
        os.path.join(REPO_ROOT, "e2e", "results"))
    rc = batch_driver.run_measure(batch["run_dir"])
    failed = [r["case_id"] for r in batch["results"] if int(r.get("exit_code", 2)) != 0]
    if batch["crashes"] or failed or rc != 0:
        print(f"[batch] 本地批次未全绿: crashes={batch['crashes']} "
              f"failed={failed} measure_rc={rc}")
        raise SystemExit(1)
    print(f"[batch] 30 例本地批次全绿 run_id={batch['run_id']}")
    return batch


def collect_evidence(local_run_dir: str) -> tuple[str, dict, list]:
    """跑采集器铸造新证据 run，返回 (out_dir, report, index)。"""
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "collect_closure_evidence.py"),
         "--local-run-dir", local_run_dir],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=UTF8_ENV)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        print(f"[collector] 采集器退出码 {proc.returncode}，拒绝签发")
        raise SystemExit(1)
    m = re.search(r"closure-report written:\s*(.+)\s*$", proc.stdout, re.MULTILINE)
    if not m:
        print("[collector] 找不到 'closure-report written' 行，拒绝签发")
        raise SystemExit(1)
    out_dir = m.group(1).strip()
    with open(os.path.join(out_dir, "closure-report.json"), encoding="utf-8") as f:
        report = json.load(f)
    with open(os.path.join(out_dir, "evidence-index.json"), encoding="utf-8") as f:
        index = json.load(f)
    return out_dir, report, index


def gate_statuses(report: dict) -> dict:
    """报告六门的纯状态映射（键序即契约 GATE_ORDER）。"""
    return {name: report["gates"][name]["status"] for name in GATE_ORDER}


def gate_reasons(report: dict) -> dict:
    """非 PASS 门的如实原因（缺省给统一说明，绝不留空伪装）。"""
    reasons = {}
    for name in GATE_ORDER:
        g = report["gates"][name]
        if g["status"] != "PASS":
            reasons[name] = g.get("reason") or "no PASS-level evidence at this commit"
    return reasons


def decide_release(report: dict, index: list, head: str) -> bool:
    """唯一判定入口：先逐字节重验证据索引，再问 A08 local-stable。"""
    if not verify_index(report["_out_dir"], index, fallback_root=REPO_ROOT):
        print("[refuse] 证据索引逐字节重验未通过（verify_index），拒绝签发")
        return False
    return release_allowed("local-stable", gate_statuses(report),
                           report["commit"], head, index, report["run_id"])


def build_files_map(dist_root: str) -> dict:
    """对 dist 里除清单自身外的每个字节做哈希（清单永不自我封存）。"""
    files = {}
    for root, _, names in os.walk(dist_root):
        for name in names:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, dist_root).replace("\\", "/")
            if rel == "release-manifest.json" or is_withheld(rel):
                continue
            files[rel] = {"sha256": sha_file(full), "size_bytes": os.path.getsize(full)}
    return dict(sorted(files.items()))


def build_manifest(*, version: str, head: str, branch: str, timestamp: str,
                   report: dict, batch_run_id: str,
                   files_map: dict, reasons: dict, extra: dict) -> dict:
    """纯装配：字段名沿用 7.3.1 清单形状，语义全部换成本轮真凭据。"""
    statuses = gate_statuses(report)
    files_map = dict(sorted(files_map.items()))
    return {
        "release_version": version,
        "release_name": f"v{version}-local-stable",
        "source_branch": branch,
        "build_timestamp": timestamp,
        "git_commit": head,
        "release_tier": "local-stable",
        "withheld_patterns": list(WITHHELD_SUFFIXES),
        "withheld_note": WITHHELD_NOTE,
        "closure_gates_source": "explicit_closure_report",
        "closure_report": f"evidence/{report['run_id']}/closure-report.json",
        "closure_report_sha256": report["_closure_report_sha256"],
        "evidence_run_id": report["run_id"],
        "evidence_index_sha256": report["evidence_index_sha256"],
        "local_batch_run_id": batch_run_id,
        "gate_statuses": statuses,
        "gate_reasons": reasons,
        "builder": "scripts/build_release.py",
        "provenance": {
            "note": PROVENANCE_NOTE,
            **extra,
        },
        "file_count": len(files_map),
        "files": files_map,
    }


def _is_old_evidence(rel: str, new_run_id: str) -> bool:
    """历史证据 run（非本轮新铸造的那个）一律不随包，只留 EVIDENCE_KEEP。"""
    norm = str(rel).replace("\\", "/")
    if not norm.startswith("evidence/"):
        return False
    if norm in EVIDENCE_KEEP:
        return False
    return not norm.startswith(f"evidence/{new_run_id}/")


def sanitize_dist_closure_report(dist_closure: str) -> tuple[int, str]:
    """脱敏发布面 closure-report：把构建机绝对路径 run_dir 逐个改写为占位。

    返回 (改写字段数, 脱敏前原文 sha256)。改写后仍命中绝对路径形状则拒绝签发。
    原文 run 留在构建树（私有证据），清单 provenance 登记原文摘要供审计追溯。
    """
    with open(dist_closure, "rb") as f:
        original_sha = hashlib.sha256(f.read()).hexdigest()
    with open(dist_closure, encoding="utf-8") as f:
        doc = json.load(f)
    redacted = 0
    gates = doc.get("gates") if isinstance(doc, dict) else None
    if isinstance(gates, dict):
        for gate in gates.values():
            if isinstance(gate, dict) and isinstance(gate.get("run_dir"), str):
                gate["run_dir"] = REDACTED_RUN_DIR
                redacted += 1
    blob = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if _STRICT_PATH.search(blob):
        raise SystemExit(f"脱敏后仍命中绝对路径形状，拒绝签发: {dist_closure}")
    with open(dist_closure, "w", encoding="utf-8", newline="\n") as f:
        f.write(blob)
    return redacted, original_sha


def stage_dist(tracked: list[str], evidence_dir: str, dist_root: str,
               new_run_id: str) -> tuple[list[str], list[str]]:
    """复制 tracked 面 + 新证据 run 到 dist。

    返回 (被禁发的运行期文件清单, 被剔除的历史证据文件清单)。
    历史证据 run 只留 EVIDENCE_KEEP（随包校验器点名必需且已验干净），其余
    留在构建树私有证据区；新 run 的 closure-report 在 dist 内脱敏后再 sealing。
    """
    if os.path.exists(dist_root):
        if not os.path.basename(dist_root).startswith("_minimax_dist"):
            raise SystemExit(f"拒绝清理非构建目录: {dist_root}")
        shutil.rmtree(dist_root)
    os.makedirs(dist_root)
    withheld: list[str] = []
    dropped_internal: list[str] = []
    dropped_private: list[str] = []
    dropped_old_evidence: list[str] = []

    def _blocked(rel: str) -> bool:
        """柜内过程文档与带作者机绝对路径的文本件都不许进发布面。"""
        if rel.replace("\\", "/").startswith(INTERNAL_DOC_PREFIX):
            dropped_internal.append(rel)
            return True
        if carries_author_machine_path(rel):
            dropped_private.append(rel)
            return True
        return False

    for rel in tracked:
        if rel == "release-manifest.json":
            continue  # 清单当场重写，不从工作区复制
        if _is_old_evidence(rel, new_run_id):
            dropped_old_evidence.append(rel)
            continue
        if is_withheld(rel) or _blocked(rel):
            withheld.append(rel)
            continue
        src = os.path.join(REPO_ROOT, rel)
        dst = os.path.join(dist_root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    for root, _, names in os.walk(evidence_dir):
        for name in names:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, REPO_ROOT).replace("\\", "/")
            if is_withheld(rel) or _blocked(rel):
                withheld.append(rel)
                continue
            dst = os.path.join(dist_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(full, dst)
    withheld.sort()
    dropped_old_evidence.sort()
    print(f"[dist] {dist_root} 已就绪（tracked {len(tracked) - 1} 件 + 证据 run "
          f"{os.path.basename(evidence_dir)}；禁发运行期件 {len(withheld)}，"
          f"柜内过程文档 {len(dropped_internal)}，含作者机路径 {len(dropped_private)}，"
          f"历史证据留私有 {len(dropped_old_evidence)}）")
    if dropped_private:
        print(f"[隐私闸] 因绝对路径被挡下：{sorted(dropped_private)[:5]}")
    return withheld, dropped_old_evidence


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a signed-by-evidence H3 release package")
    ap.add_argument("--dist", default=None,
                    help="发布包输出目录（仓库之外），默认 <仓库同级>/_minimax_dist_<版本>")
    args = ap.parse_args()

    try:
        version = check_single_source_version(REPO_ROOT)
    except VersionValidationError as exc:
        raise SystemExit(f"单一真源版本号漂移（MM-01），拒绝构建：{exc}")
    dist_root = os.path.abspath(args.dist) if args.dist else os.path.join(
        os.path.dirname(REPO_ROOT), f"_minimax_dist_{version}")
    if dist_root == REPO_ROOT or dist_root.startswith(REPO_ROOT + os.sep):
        raise SystemExit(f"dist 必须在源仓库之外，拒绝: {dist_root}")

    stage = preflight()
    head = stage["head"]
    batch = run_local_batch()
    if head_commit() != head:
        print("[guard] HEAD 在构建途中移动，拒绝签发")
        return 1
    evidence_dir, report, index = collect_evidence(batch["run_dir"])
    if head_commit() != head:
        print("[guard] HEAD 在采集途中移动，拒绝签发")
        return 1
    dirty = dirty_tracked_paths()
    if dirty:
        print(f"[guard] 构建期间 tracked 面被改动，拒绝签发: {dirty}")
        return 1

    report["_out_dir"] = evidence_dir
    if not decide_release(report, index, head):
        statuses = gate_statuses(report)
        print(f"[refuse] release_allowed('local-stable') 判否，拒绝签发。本轮门状态: "
              f"{json.dumps(statuses, ensure_ascii=False)}")
        print("[refuse] 修法只有一个：按 closure_gate_contract 的证据口径把缺的门跑绿，"
              "不许改判定层、不许伪造。")
        return 1

    stage_dist_result, dropped_old_evidence = stage_dist(
        stage["tracked"], evidence_dir, dist_root, report["run_id"])
    # 发布面 closure-report 脱敏后再 sealing：清单绑定的就是收件人拿到的字节，
    # 原文 run 留在构建树私有证据区，原文摘要登记 provenance 供审计追溯。
    dist_closure_rel = f"evidence/{report['run_id']}/closure-report.json"
    dist_closure_abs = os.path.join(dist_root, dist_closure_rel)
    if not os.path.isfile(dist_closure_abs):
        raise SystemExit(f"新 run 的 closure-report 未进发布面（疑似被隐私闸挡下），"
                         f"拒绝签发: {dist_closure_rel}")
    for keep in EVIDENCE_KEEP:
        if not os.path.isfile(os.path.join(dist_root, keep)):
            raise SystemExit(f"随包校验必需的历史证据件缺席，拒绝签发: {keep}")
    redacted_fields, original_closure_sha = sanitize_dist_closure_report(dist_closure_abs)
    print(f"[隐私闸] closure-report 脱敏 run_dir 字段 {redacted_fields} 个")
    files_map = build_files_map(dist_root)
    report["_closure_report_sha256"] = files_map[dist_closure_rel]["sha256"]
    manifest = build_manifest(
        version=version, head=head, branch=current_branch(),
        timestamp=datetime.now(timezone.utc).isoformat(),
        report=report, batch_run_id=batch["run_id"],
        files_map=files_map, reasons=gate_reasons(report),
        extra={
            "untracked_suites_executed": stage["untracked_suites_executed"],
            "local_gates_required": list(LOCAL_GATES),
            "closure_report_original_sha256": original_closure_sha,
            "closure_report_redacted_fields": redacted_fields,
            "evidence_keep": list(EVIDENCE_KEEP),
            "dropped_old_evidence_count": len(dropped_old_evidence),
            "withheld_runtime_files": {
                "note": WITHHELD_NOTE,
                "patterns": list(WITHHELD_SUFFIXES),
                "count": len(stage_dist_result),
                "paths": stage_dist_result,
            },
        })
    blob = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    for target in (os.path.join(dist_root, "release-manifest.json"),
                   os.path.join(REPO_ROOT, "release-manifest.json")):
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(blob)
    print(json.dumps({
        "status": "RELEASED",
        "release_version": version,
        "git_commit": head,
        "dist": dist_root.replace("\\", "/"),
        "evidence_run_id": report["run_id"],
        "local_batch_run_id": batch["run_id"],
        "gate_statuses": manifest["gate_statuses"],
        "files": manifest["file_count"],
        "release_manifest_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
