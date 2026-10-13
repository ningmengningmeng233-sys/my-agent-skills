"""发布包自检门：一条命令在**当前树**里跑完，包内包外同一把尺。

为什么重写（三起真实事故）：
① 上一版把 flake8 的「输出行数」当结论，于是 flake8 缺失/超时/崩溃时输出很少，几乎等于放行；
② 它把本仓 927 条既有风格债当成本次红——本仓没有 `.flake8`、`pyproject` 无 lint 段、
   CI 只跑 pytest，那把 100 列尺是我从资产仓搬来的，属越界 invent；
③ 报告不写验收对象，结果我把 cp 进解包树的新脚本读数当成 cand3 原包读数。

现在的三档口径：
- `lint_hard`：F*/E9*/W605（W605 在未来 Python 会升级成语法错误，是真隐患），
  按 `(相对路径, 规则码, 行, 列)` 逐项棘轮 —— **新增即红，即使总数下降**；
  修掉的项从基线移除并记在 `fixed`。基线写死在下面，可评审。
- `lint_style_declared`：其余按规则码计数并申报，不拦（拦它会逼出全仓格式化，不在本次范围）。
- 每个检查项记实际退出码与状态；工具缺失/超时/不可解析分别记 `NOT_RUN`/`TIMEOUT`/`UNPARSEABLE`
  并计入未过，绝不因「没输出」而通过。
- 报告 `target` 段绑定验收对象（树路径、门脚本自身 SHA、可选 ZIP SHA、源码提交）。

判绿口径沿用资产侧约定：末行 `Result: N/M checks passed`；exit 0 只在无未过项时。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HARD_RE = re.compile(r"^(F\d+|E9\d*|W605)$")
LINT_LINE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?P<col>\d+): (?P<rule>[A-Z]\d+) (?P<msg>.*)$")
# 扫描面跟着被改动的包走：本轮修的是 `v4/enhance/reference_audit.py`，
# 尺不覆盖它，就等于改完没人量。
LINT_ARGV = (sys.executable, "-m", "flake8", "--select=F,E9,W605",
             "v4/core", "v4/enhance", "scripts")
# 基线曾有 3 条 W605（旧模块文档串里的作者机路径非法转义），后改为原始文档串消掉；
# run03 收尾又把该默认路径收件人化（skill 内置索引库），文档串文本随之变更；
# 基线保持为空：此后任何硬伤都是新增。
HARD_BASELINE: list[tuple] = []
# flake8 没装 / 没跑起来：这类输出不是「0 条问题」，是没读数。
LINT_NOT_RUN_RE = re.compile(
    r"No module named ['\"]?flake8|flake8: (?:command not found|not found)"
    r"|can't open file [^\n]*flake8", re.I)
TOP_LEVEL_COMMANDS = [
    "init", "ingest", "confirm-boundary", "decide", "pause", "resume", "recover",
    "resolve-events", "align-events", "status", "rewrite", "check", "repair-coverage",
    "versions", "approve", "rollback", "blind-review", "platform-review", "publish-split",
    "merge", "blockers", "refresh", "consistency", "assets", "card", "lib",
]
CREDENTIAL_SHAPES = [
    ("SECRET_VALUE", re.compile(r"\b(?:sk|ghp|github_pat|xox[bap])[-_][A-Za-z0-9\-]{16,}")),
    ("KEY_ASSIGNED", re.compile(r"\b(?:api[_-]?key|apikey|access[_-]?key|secret[_-]?key|"
                                r"auth[_-]?token|password|secret)\b"
                                r"[\"']?\s*[:=]\s*[\"']?([0-9a-fA-F]{24,}|[A-Za-z0-9+/_\-]{32,})",
                                re.I)),
]
PRIVATE_PATHS = [re.compile(r"[A-Z]:[\\/](?:Users|迁移|Janson)", re.I),
                 re.compile(r"/home/[a-z0-9_]+/", re.I)]
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".yaml", ".yml", ".ps1"}
MANUSCRIPT_DIR = Path("references") / "样本库"
MANUSCRIPT_KEEP = {"SOURCE-NOTES.md"}
# 随包分发的业务验收面：真实入口 + 拒绝类断言，缺一不可
ACCEPTANCE_FILES = [
    "v4/tests/test_package_business.py",
    "v4/tests/test_merge_state_guard.py",
    "v4/tests/test_check_version_binding.py",
    "v4/tests/test_state_delta.py",
    "v4/tests/test_content_coverage.py",
]

# 排除规则（追加要求 2 复核后收紧）：
# - 只有「缓存目录」里的东西按目录受控豁免；不再按文件名（manifest.json / SHA256SUMS）自动免检，
#   清单和校验表本身就是被验对象，过期或被人改过必须判红；
# - 报告/日志只豁免本次命令行明确声明过的输出路径（--report / --json），
#   别人塞进来一个 gate_report_xxx.json 仍然算未申报新增；
# - 树里的清单/校验表先验证再谈豁免：它声明的成员若与实测不符，就是内容漂移。
CACHE_DIRS = {"__pycache__", ".pytest_cache"}
SCAN_SKIP_DIRS = CACHE_DIRS | {".git"}
CHECKSUM_NAME_RE = re.compile(r"(manifest\.json$|SHA256SUMS$|\.sha256$|\.sums$)", re.I)
LINT_SUMS_LINE = re.compile(r"^([0-9a-f]{64})[* ]?\s+(.+)$")
MAX_CLAIM_FILE_BYTES = 2_000_000


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_manifest_index(manifest_path: Path) -> dict:
    """清单 -> {相对路径: 校验和}。绑定要看内容，光有路径与提交证明不了没被换。"""
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    return {f["path"]: f["sha256"] for f in data.get("files", []) if "path" in f}


def is_cache_artifact(rel: str) -> bool:
    parts = rel.split("/")
    return any(part in CACHE_DIRS for part in parts[:-1])


def _skip_dir(rel: str) -> bool:
    parts = rel.split("/")
    return any(part in SCAN_SKIP_DIRS for part in parts[:-1])


def parse_lint_output(text: str) -> tuple[list[dict], bool]:
    """flake8 输出 -> 定位记录。读不懂（含空输出）一律 unparseable=True。

    「没有输出」在 flake8 里既可能是真干净，也可能是崩溃/缺失，
    所以这里不给结论，只如实报不可解析；干净与否由 collect_lint 结合退出码判。
    """
    records: list[dict] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = LINT_LINE.match(line)
        if not m:
            return [], True
        g = m.groupdict()
        records.append({"path": g["path"].replace("\\", "/"), "rule": g["rule"],
                        "line": int(g["line"]), "col": int(g["col"]),
                        "message": g["msg"]})
    if not records:
        return [], True
    return records, False


def split_buckets(records: list[dict]) -> tuple[list[dict], list[dict]]:
    hard = [r for r in records if HARD_RE.match(r["rule"])]
    style = [r for r in records if not HARD_RE.match(r["rule"])]
    return hard, style


def _key(rec) -> tuple:
    return (rec["path"], rec["rule"], int(rec["line"]), int(rec["col"]))


def lint_hard_verdict(records: list[dict], baseline) -> dict:
    """逐项棘轮：认的是具体位置，不是计数——总数下降也照样拦新增。"""
    current = {_key(r) for r in records}
    base = {(b[0], b[1], int(b[2]), int(b[3])) for b in baseline}
    new = sorted(current - base)
    fixed = sorted(base - current)
    return {"ok": not new, "new": [list(k) for k in new],
            "fixed": [list(k) for k in fixed], "count": len(current)}


def collect_lint(call, baseline=None) -> dict:
    """跑 flake8 并给出可评审的分档结论；异常口径全部判未过。"""
    baseline = HARD_BASELINE if baseline is None else baseline
    rc, out = call(list(LINT_ARGV))
    text = out or ""
    if rc == -1 or text.strip().upper().startswith("TIMEOUT"):
        status, records, reason = "TIMEOUT", [], "flake8 超时：没有读数就是没读数"
    elif LINT_NOT_RUN_RE.search(text):
        status, records, reason = "NOT_RUN", [], "flake8 未安装或不可调用"
    elif not text.strip():
        if rc == 0:
            status, records, reason = "OK", [], ""
        else:
            status, records, reason = "NOT_RUN", [], f"flake8 退出码 {rc} 且无输出"
    else:
        parsed, unparseable = parse_lint_output(text)
        if unparseable:
            status, records = "UNPARSEABLE", []
            reason = f"输出不可解析（rc={rc}）: {text[:120]!r}"
        else:
            status, records, reason = "OK", parsed, ""
    hard, style = split_buckets(records)
    verdict = lint_hard_verdict(hard, baseline)
    return {"status": status, "exit_code": rc, "hard": hard, "style": style,
            "verdict": verdict, "hard_ok": status == "OK" and verdict["ok"],
            "reason": reason}


def find_manuscript_bodies(root: Path) -> list[str]:
    """样本库递归扫描：正文一律不随包分发，嵌套子目录也要看到。"""
    base = Path(root) / MANUSCRIPT_DIR
    if not base.is_dir():
        return []
    found = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name in MANUSCRIPT_KEEP:
            continue
        found.append(p.relative_to(root).as_posix())
    return found


def _claims_in_text(rel: str, text: str, name_is_listsheet: bool) -> list[tuple]:
    """从一份清单/校验表里提取它作出的声明：(声明文件, 被声明路径, 声明摘要)。"""
    claims: list[tuple] = []
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        entries = data.get("files")
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict) and "path" in item and "sha256" in item:
                    claims.append((rel, str(item["path"]).replace("\\", "/"),
                                   str(item["sha256"]).strip().lower()))
            return claims
        return claims
    if name_is_listsheet:
        for line in text.splitlines():
            m = LINT_SUMS_LINE.match(line.strip())
            if m:
                claims.append((rel, m.group(2).strip().lstrip("*").replace("\\", "/"),
                               m.group(1)))
    return claims


def embedded_claims(root: Path) -> list[tuple]:
    root = Path(root)
    claims: list[tuple] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if _skip_dir(rel):
            continue
        name_is_listsheet = bool(CHECKSUM_NAME_RE.search(p.name))
        if not name_is_listsheet and p.suffix.lower() != ".json":
            continue
        try:
            if p.stat().st_size > MAX_CLAIM_FILE_BYTES:
                continue
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        claims.extend(_claims_in_text(rel, text, name_is_listsheet))
    return claims


def check_tree_drift(root: Path, index: dict, declared_outputs=()) -> dict:
    """逐成员比校验和；豁免只有两类，且都写进报告。

    清单/校验表若在树里，它就是被验对象：过期或被改一律判 changed，绝不按名字放行。
    """
    root = Path(root)
    declared = {str(x).replace("\\", "/") for x in declared_outputs}
    changed: set[str] = set()
    missing, ignored_caches, ignored_declared, stale = [], [], [], []
    for rel, digest in sorted(index.items()):
        target = root / rel
        if not target.is_file():
            missing.append(rel)
        elif sha256_file(target) != digest:
            changed.add(rel)
    present = []
    for q in sorted(root.rglob("*")):
        if not q.is_file():
            continue
        rel = q.relative_to(root).as_posix()
        if rel in index:
            continue
        if rel in declared:
            ignored_declared.append(rel)
        elif is_cache_artifact(rel):
            ignored_caches.append(rel)
        else:
            present.append(rel)
    unlisted = sorted(set(present) - set(ignored_caches) - set(ignored_declared))
    for src, claimed_rel, digest in embedded_claims(root):
        target = root / claimed_rel
        if not target.is_file():
            continue
        if sha256_file(target) != digest:
            stale.append(f"{src} 声明 {claimed_rel}={digest[:12]}… 与实测不符")
            changed.add(claimed_rel)
    return {"changed": sorted(changed), "missing": missing, "unlisted": unlisted,
            "stale_claims": sorted(set(stale)),
            "ignored_caches": sorted(set(ignored_caches)),
            "ignored_declared": sorted(set(ignored_declared)),
            "ignored": sorted(set(ignored_caches) | set(ignored_declared)),
            "checked": len(index),
            "clean": not changed and not missing and not unlisted}


def help_is_meaningful(text: str, required: list[str]) -> bool:
    """只看退出码会放过「入口守卫丢失 → 静默空操作」，所以必须查内容。"""
    if "usage:" not in text:
        return False
    return all(cmd in text for cmd in required)


def build_target_binding(root: Path, gate_script_sha256: str,
                         zip_path: Path | None = None, source_commit: str = "") -> dict:
    return {"kind": "zip-bound-tree" if zip_path else "working-tree",
            "root": str(Path(root).resolve()),
            "zip": str(zip_path) if zip_path else None,
            "zip_sha256": sha256_file(zip_path) if zip_path else None,
            "gate_script_sha256": gate_script_sha256,
            "source_commit": source_commit,
            "hard_lint_baseline": [list(b) for b in HARD_BASELINE]}


def _git_commit(root: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(root),
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return "NOT_A_GIT_TREE"
        return out.stdout.strip() or "UNKNOWN"
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"


def check(name: str, passed: bool, detail: str, command: str = "",
          exit_code: int | None = None, status: str = "") -> dict:
    return {"name": name, "passed": bool(passed),
            "status": status or ("PASS" if passed else "FAIL"),
            "command": command, "exit_code": exit_code, "detail": detail[:4000]}


def default_runner(root: Path):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               PYTHONDONTWRITEBYTECODE="1")

    def call(argv: list[str]) -> tuple[int, str]:
        try:
            proc = subprocess.run(argv, cwd=str(root), env=env, capture_output=True, timeout=900)
            return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            return -1, "TIMEOUT"
        except FileNotFoundError as exc:
            return 127, f"MISSING_EXECUTABLE: {exc}"
    return call


def _count_argv_surface(root: Path) -> int:
    sys.path.insert(0, str(root))
    import importlib
    cli = importlib.import_module("v4.core.cli")

    def walk(action, prefix=""):
        found = {}
        for name, sub in action.choices.items():
            path = f"{prefix} {name}".strip()
            found[path] = None
            for act in sub._actions:
                choices = getattr(act, "choices", None)
                if isinstance(choices, dict) and choices and \
                        all(hasattr(v, "_actions") for v in choices.values()):
                    found.update(walk(act, path))
        return found

    sub = next(a for a in cli.build_parser()._actions if getattr(a, "choices", None))
    return len(walk(sub))


def _scan_text_tree(root: Path, declared_text_skips=()) -> tuple[int, list, list]:
    hits_priv, hits_cred, scanned = [], [], 0
    skips = {str(x).replace("\\", "/") for x in declared_text_skips}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        rel = p.relative_to(root).as_posix()
        if _skip_dir(rel) or rel in skips:
            continue
        scanned += 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rule in PRIVATE_PATHS:
            m = rule.search(text)
            if m:
                hits_priv.append(f"{rel}:{text[:m.start()].count(chr(10)) + 1}")
                break
        for rname, rule in CREDENTIAL_SHAPES:
            if rule.search(text):
                hits_cred.append(f"{rname} {rel}")
    return scanned, hits_priv, hits_cred


def run_gate(root: Path, call=None, *, manifest_index: dict | None = None,
             drift_before: dict | None = None, declared_outputs=()) -> list[dict]:
    root = Path(root)
    rows: list[dict] = []
    py = sys.executable
    call = call or default_runner(root)

    if not root.is_dir():
        return [check("root_exists", False, f"树不存在: {root}")]

    rc, out = call([py, "-m", "compileall", "-q", "v4", "scripts"])
    rows.append(check("compileall", rc == 0, out, "python -m compileall -q v4 scripts", rc))

    lint = collect_lint(call)
    verdict = lint["verdict"]
    rows.append(check("lint_hard", lint["hard_ok"],
                      f"状态 {lint['status']}；硬伤 {len(lint['hard'])} 条，"
                      f"新增 {len(verdict['new'])} 条，修掉 {len(verdict['fixed'])} 条；"
                      f"{lint['reason'] or ('新增项: ' + str(verdict['new'][:3]))}",
                      "python -m flake8 --select=F,E9,W605 v4/core v4/enhance scripts",
                      lint["exit_code"], lint["status"]))
    by_rule: dict[str, int] = {}
    for rec in lint["style"]:
        by_rule[rec["rule"]] = by_rule.get(rec["rule"], 0) + 1
    rows.append(check("lint_style_declared", True,
                      f"风格类合计 {len(lint['style'])} 条，按规则码申报：{by_rule}；"
                      f"既有债不冒充本次回归（本仓无 lint 配置、CI 不跑 lint）",
                      "python -m flake8 v4/core v4/enhance scripts", lint["exit_code"], "DECLARED"))

    # 兼容尺一项；业务验收一项（把随包分发的验收文件一次跑完，缺哪份就在明细里说明）
    compat_rel = "v4/tests/test_cli_compat.py"
    rc = None
    if (root / compat_rel).is_file():
        rc, out = call([py, "-m", "pytest", compat_rel, "-q", "-p", "no:cacheprovider"])
    if rc is None:
        rows.append(check("compat_ruler", False, "NOT_RUN: 兼容尺未随包分发（裁剪过度）",
                          "pytest v4/tests/test_cli_compat.py", None, "NOT_RUN"))
    else:
        rows.append(check("compat_ruler", rc == 0, out, f"pytest {compat_rel}", rc))

    present = [rel for rel in ACCEPTANCE_FILES if (root / rel).is_file()]
    absent = [rel for rel in ACCEPTANCE_FILES if not (root / rel).is_file()]
    if present:
        rc, out = call([py, "-m", "pytest", *present, "-q", "-p", "no:cacheprovider"])
        rows.append(check("business_acceptance", rc == 0 and not absent,
                          out + chr(10) + (f"缺验收文件: {absent}" if absent else ""),
                          "pytest " + " ".join(present), rc))
    else:
        rows.append(check("business_acceptance", False,
                          "NOT_RUN: 包内业务验收文件一份都没随包分发",
                          "pytest " + " ".join(ACCEPTANCE_FILES), None, "NOT_RUN"))

    rc, out = call([py, "-m", "v4.core.cli", "--help"])
    missing_cmds = [c for c in TOP_LEVEL_COMMANDS if c not in out]
    rows.append(check("cli_help_lists_commands",
                      rc == 0 and help_is_meaningful(out, TOP_LEVEL_COMMANDS),
                      f"rc={rc}，usage={'有' if 'usage:' in out else '无'}，"
                      f"缺命令 {missing_cmds[:6]}",
                      "python -m v4.core.cli --help", rc))

    try:
        count = _count_argv_surface(root)
        rows.append(check("argv_surface_39", count == 39,
                          f"实测 argv 面 {count} 条（工单分母 39＝顶层 26+card 8+lib 5）"))
    except Exception as exc:  # noqa: BLE001 - 记红不静默
        rows.append(check("argv_surface_39", False, f"{type(exc).__name__}: {exc}"))

    scanned, hits_priv, hits_cred = _scan_text_tree(root, declared_outputs)
    rows.append(check("no_private_paths", not hits_priv,
                      f"扫描 {scanned} 份文本，命中 {len(hits_priv)} 处: {hits_priv[:4]}"))
    rows.append(check("no_credential_shapes", not hits_cred,
                      f"命中 {len(hits_cred)} 处: {hits_cred[:4]}；只记规则与路径，不记明文"))

    bodies = find_manuscript_bodies(root)
    rows.append(check("no_manuscript_bodies", not bodies,
                      f"样本库正文 {len(bodies)} 份（含嵌套目录）: {bodies[:3]}；"
                      f"产品规定正文不随包分发"))

    if manifest_index:
        drift = check_tree_drift(root, manifest_index, declared_outputs)
        bad = drift["changed"] + drift["missing"] + drift["unlisted"]
        rows.append(check("tree_drift", drift["clean"],
                          f"逐成员校验和 {drift['checked']} 项：改 {len(drift['changed'])}、"
                          f"缺 {len(drift['missing'])}、未申报新增 {len(drift['unlisted'])}、"
                          f"豁免（缓存/本次声明输出）{len(drift['ignored'])}、"
                          f"清单过期声明 {len(drift['stale_claims'])}；"
                          f"前 3 处问题: {bad[:3]}；豁免明细: "
                          f"caches={drift['ignored_caches'][:3]} "
                          f"declared={drift['ignored_declared'][:3]}",
                          "compare tree against manifest.json"))
        if drift_before is not None:
            stable = (sorted(drift["changed"]) == sorted(drift_before["changed"])
                      and sorted(drift["missing"]) == sorted(drift_before["missing"])
                      and sorted(drift["unlisted"]) == sorted(drift_before["unlisted"]))
            rows.append(check("tree_stable_during_acceptance", stable,
                              "验收前后树内容漂移集合必须一致（中途 cp 新脚本会被这条拦住）",
                              "compare drift before/after"))
    else:
        rows.append(check("tree_drift", False,
                          "NOT_RUN: 未给 --manifest，无法按内容绑定验收对象",
                          "compare tree against manifest.json", None, "NOT_RUN"))
    return rows


def _declared_path(root: Path, p: Path | None) -> str | None:
    """只豁免「本次明确声明的输出路径」；落在树外就不必进清单。"""
    if p is None:
        return None
    try:
        return Path(p).resolve().relative_to(root).as_posix()
    except ValueError:
        return None


MANIFEST_NAMES = ("manifest.json", "release-manifest.json")


def locate_delivery_manifest(root: Path) -> tuple[Path | None, list[str]]:
    """只按明确交付清单名定位：包根，以及包根的同级（解包目录与 sidecar 并列的交付目录）。

    不做递归搜索、不按后缀猜——之前 `tree_drift` 因为清单在 ZIP 外而永远 NOT_RUN，
    收件人跑不出全绿自证。这里自动定位必须把用到的路径申报出来，不静默顶替。
    """
    tried = []
    for base in (root, root.parent):
        for name in MANIFEST_NAMES:
            cand = base / name
            tried.append(str(cand))
            if cand.is_file() and _looks_like_delivery_manifest(cand):
                return cand, tried
    return None, tried


def _looks_like_delivery_manifest(path: Path) -> bool:
    """内容形状必须像交付清单（有 files 数组且每项带 path+sha256），否则不算命中。"""
    import json as _json
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not files:
        return False
    return all(isinstance(f, dict) and f.get("path") and f.get("sha256") for f in files[:5])



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT), help="要验收的树（解包目录或仓库根）")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--zip", type=Path, help="验收对象 ZIP，写进报告的 target 绑定")
    ap.add_argument("--manifest", type=Path, help="该 ZIP 的 manifest.json，用于内容绑定")
    ap.add_argument("--report", type=Path, help="本次声明的输出路径（唯一可豁免的报告）")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    index = None
    manifest_source = ""
    if args.manifest and args.manifest.is_file():
        index = load_manifest_index(args.manifest)
        manifest_source = "命令行显式指定: " + str(args.manifest)
    else:
        found, tried = locate_delivery_manifest(root)
        if found:
            index = load_manifest_index(found)
            manifest_source = "自动定位（只认明确交付清单名）: " + str(found)
        else:
            manifest_source = ("未找到交付清单，tree_drift 保持 NOT_RUN（不跳过、不判绿）；"
                               "试过: " + "; ".join(tried))
    declared = [x for x in (_declared_path(root, args.report), _declared_path(root, args.json))
                if x]
    before = check_tree_drift(root, index, declared) if index else None
    rows = run_gate(root, manifest_index=index, drift_before=before,
                    declared_outputs=declared)
    failed = [r["name"] for r in rows if not r["passed"]]
    zip_arg = args.zip if args.zip and args.zip.is_file() else None
    report = {
        "target": build_target_binding(root, sha256_file(Path(__file__).resolve()), zip_arg,
                                       _git_commit(root)),
        "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "total": len(rows), "passed": len(rows) - len(failed), "failed": failed,
        "manifest_members": len(index or {}),
        "manifest_source": manifest_source,
        "declared_outputs": declared,
        "drift_before": before,
        "drift_after": check_tree_drift(root, index, declared) if index else None,
        "rows": rows,
    }
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8", newline="\n")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                               encoding="utf-8", newline="\n")
    print(f"Result: {report['passed']}/{report['total']} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
