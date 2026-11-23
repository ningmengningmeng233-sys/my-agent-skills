# -*- coding: utf-8 -*-
"""包内发布门：别人解压后会不会当场撞墙，这里逐项自证。

只在**解压出来的树里**跑（`--as-package` 带 `--manifest` 对账清单）。
它不是开发柜的尺：柜根 `schemas/contract_check.py` 按柜根相对路径解析，不随包分发。

    python scripts/check_package.py --as-package --manifest manifest.json
    末行期望 `Result: N/N checks passed`；任何一项 FAIL 都别对外说这包能用。
"""
from __future__ import annotations

import argparse
import ast
import compileall
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_SHAPE = re.compile(r"[A-Za-z]:[/\\](?:Users|home|迁移|Janson)")
CABINET_ONLY = re.compile(r"(?<![A-Za-z0-9_./\\])schemas/[A-Za-z0-9_.-]+")
KEY_SHAPE = re.compile(r"[\'\"](sk-[A-Za-z0-9._-]{8,}|sk-ant-[A-Za-z0-9._-]{8,})[\'\"]")
PLACEHOLDER = re.compile(r"(REPLACE|CHANGE|EXAMPLE|changeme|your[-_]|占位|示例|假)", re.I)
TEXT_SUFFIX = {".md", ".py", ".mjs", ".json", ".txt", ".yaml", ".yml", ".html", ".css", ".js"}
SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}
RESIDUE_NAMES = {"workspace", ".gates.jsonl", "test-results", "__pycache__", ".pytest_cache"}


def _rows(name, ok, detail, declared=False):
    status = "DECLARED" if declared else ("PASS" if ok else "FAIL")
    return {"name": name, "passed": bool(ok or declared), "status": status, "detail": detail}


BARE_TEXT_NAMES = {"LICENSE", "NOTICE", "VERSION", "README", "CHANGELOG", "SHA256SUMS"}


def _is_text(f: Path) -> bool:
    """按扩展名筛文本，但无扩展名的分发件（LICENSE/NOTICE/VERSION）也必须进尺。"""
    if f.suffix.lower() in TEXT_SUFFIX:
        return True
    return f.suffix == "" and f.name.upper() in BARE_TEXT_NAMES


def _texts(root: Path):
    for f in sorted(root.rglob("*")):
        if not f.is_file() or any(p in SKIP_PARTS for p in f.parts):
            continue
        if not _is_text(f):
            continue
        yield f


def check_identity(root: Path):
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"^\s*version:\s*\"?([0-9.]+)\"?", skill, re.M)
    top = re.search(r"^## v([0-9.]+)", (root / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
    same = bool(m and top and m.group(1) == top.group(1))
    lic = root / "LICENSE"
    lic_ok = lic.is_file() and "Proprietary" in lic.read_text(encoding="utf-8")
    mit_ok = lic.is_file() and "MIT" in lic.read_text(encoding="utf-8")
    return [_rows("version_single_source", same,
                  f"SKILL.md={m.group(1) if m else '?'} CHANGELOG={top.group(1) if top else '?'}"),
            _rows("license_text", lic_ok,
                  f"LICENSE 存在（{len(lic.read_text(encoding='utf-8').splitlines()) if lic.is_file() else 0} 行），"
                  f"自有件 Proprietary＋派生骨架与 MIT 署名件另述"),
            _rows("third_party_notice", mit_ok, "LICENSE 保留了 h3 派生副本与 MIT 署名件的来源声明")]


def check_node_entries(root: Path):
    entries = sorted((root / "skills_runtime").glob("*/scripts/selftest.mjs"))
    validate = (root / "skills_runtime" / "dramacraft_patch" / "validate.mjs").is_file()
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    return [_rows("node_selftest_entries", len(entries) == 5 and validate and "selftest.mjs" in skill,
                  f"自测入口 {len(entries)}/5、validate.mjs {'在' if validate else '缺'}、"
                  f"SKILL.md {'已记录' if 'selftest.mjs' in skill else '未记录'}")]


def check_hygiene(root: Path, members=None):
    """实配文件、构建/运行残留、私人路径——三样都按清单成员算，不按整棵树算。"""
    names = members if members is not None else [f.relative_to(root).as_posix()
                                                 for f in root.rglob("*") if f.is_file()]
    live, residue, priv = [], [], []
    for rel in names:
        rel = str(rel).replace("\\", "/")
        parts = rel.split("/")
        if Path(rel).name in {"llm_nodes.json", ".env"}:
            live.append(rel)
        if Path(rel).name in RESIDUE_NAMES:
            residue.append(rel)
        f = root / rel
        if f.is_file() and _is_text(f):
            for num, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if PRIVATE_SHAPE.search(line):
                    priv.append(f"{rel}:{num}")
    return [_rows("no_live_config", not live,
                  f"{len(names)} 成员{'含实配 ' + str(live[:3]) if live else '：无 llm_nodes.json、无 .env（只带 example）'}"),
            _rows("no_build_or_test_residue", not residue,
                  f"运行/构建残留 {len(residue)}：{residue[:4]}"),
            _rows("no_private_paths", not priv, f"私人路径命中 {len(priv)}：{priv[:4]}")]


def check_cabinet_refs(root: Path):
    registry = root / "scripts" / "package_governance" / "CABINET_ONLY_REFS.json"
    if not registry.is_file():
        return [_rows("cabinet_only_refs_declared", False, "缺申报件 scripts/package_governance/CABINET_ONLY_REFS.json")]
    declared = {str(r.get("reference", "")).strip()
                for r in json.loads(registry.read_text(encoding="utf-8")).get("references", [])}
    used, weak = set(), []
    ruler = root / "tests" / "test_release_package.py"
    for f in _texts(root):
        if f == registry or f == ruler:
            continue   # 尺子与申报件里的字面量是要检测的形状，不是对收件人的指令
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            for m in CABINET_ONLY.finditer(line):
                used.add(m.group(0).strip(" `").replace("\\", "/"))
    for r in json.loads(registry.read_text(encoding="utf-8")).get("references", []):
        if not str(r.get("recipient_action") or "").strip():
            weak.append(str(r.get("reference")))
    unreg = sorted(x for x in used if x not in declared)
    stale = sorted(x for x in declared if x not in used)
    return [_rows("cabinet_only_refs_declared", not unreg and not stale and not weak,
                  f"柜根引用 {len(used)} 项，包内实有 {len([x for x in used if (root / x).exists()])}；"
                  f"未登记 {unreg[:3]}，登记失效 {stale[:3]}，缺收件动作 {weak[:3]}")]


def check_key_shapes(root: Path, members=None):
    registry = root / "scripts" / "package_governance" / "DECLARED_TEST_SECRETS.json"
    declared = {}
    if registry.is_file():
        doc = json.loads(registry.read_text(encoding="utf-8"))
        declared = {(str(r.get("path")), str(r.get("line"))) for r in doc.get("secrets", [])}
    hits, unexplained = [], []
    scope = [root / m for m in members] if members else [f for f in _texts(root)]
    for f in scope:
        if not f.is_file() or not _is_text(f):
            continue
        rel = f.relative_to(root).as_posix()
        for num, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for m in KEY_SHAPE.finditer(line):
                hits.append((rel, num))
                if (rel, str(num)) not in declared and not PLACEHOLDER.search(line):
                    unexplained.append(f"{rel}:{num}")
    stale = sorted(f"{p}:{l}" for p, l in declared if (p, int(l)) not in hits)
    ok = not unexplained and not stale
    detail = (f"key 形状命中 {len(hits)} 处；无法解释 {unexplained[:4]}，登记失效 {stale[:4]}")
    return [_rows("declared_test_secrets", ok, detail, declared=bool(hits) and ok)]


def check_manifest(root: Path, manifest: Path):
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    files = doc["files"] if isinstance(doc, dict) else doc
    missing, bad = [], []
    for entry in files:
        rel = entry if isinstance(entry, str) else str(entry.get("path"))
        digest = "" if isinstance(entry, str) else str(entry.get("sha256") or "")
        p = root / rel
        if not p.is_file():
            missing.append(rel)
            continue
        if digest and hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            bad.append(rel)
    return [_rows("manifest_binding", not missing and not bad,
                  f"清单 {len(files)} 项：树内缺 {missing[:3]}，摘要不符 {bad[:3]}")]


def check_media_pins(root: Path, members=None):
    """示例图逐张钉摘要：图被换掉、或包内冒出没登记的第二张媒体，都在这里判红。

    登记件 `MEDIA_PIN.json` 由柜侧执行体 `scripts/media_pin.py` 生成，不是手抄。
    human_review 仍是 pending 时状态记 DECLARED（不谎称已复核），并在详情里点名。
    """
    suffixes = {".webp", ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mov"}
    pin = root / "scripts" / "package_governance" / "MEDIA_PIN.json"
    if not pin.is_file():
        return [_rows("media_pin_matches", False, "缺 scripts/package_governance/MEDIA_PIN.json")]
    doc = json.loads(pin.read_text(encoding="utf-8"))
    media = doc.get("media") or {}
    names = members if members is not None else [f.relative_to(root).as_posix()
                                                 for f in root.rglob("*") if f.is_file()]
    actual = {str(n).replace("\\", "/") for n in names
              if Path(str(n)).suffix.lower() in suffixes and (root / str(n)).is_file()}
    bad, missing = [], []
    for rel, info in media.items():
        f = root / rel
        if not f.is_file():
            missing.append(rel)
        elif hashlib.sha256(f.read_bytes()).hexdigest() != str(info.get("sha256")):
            bad.append(rel)
    unregistered = sorted(actual - set(media))
    review = str(doc.get("human_review") or "pending")
    ok = not bad and not missing and not unregistered
    detail = (f"钉住 {len(media)} 件，包内实有 {len(actual)} 件；摘要不符 {bad[:3]}，"
              f"登记件缺失 {missing[:3]}，未登记 {unregistered[:3]}，"
              f"人工内容复核状态={review}（pending＝发布前外部门）")
    return [_rows("media_pin_matches", ok, detail, declared=ok and review != "done")]


def run_shipped_acceptance(root: Path):
    cp = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_release_package.py"],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=600)
    tail = (cp.stdout or "").strip().splitlines()
    return [_rows("shipped_acceptance", cp.returncode == 0,
                  "\n".join(tail[-2:]) if tail else (cp.stderr or "")[-200:])]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--as-package", action="store_true")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    root = Path(args.root).resolve()

    members = None
    rows = [_rows("compileall", bool(compileall.compile_dir(str(root / "dramacraft"), quiet=1,
                                                          force=True)), "dramacraft/ 编译")]
    rows += check_identity(root) + check_node_entries(root)
    if args.manifest and args.manifest.is_file():
        doc = json.loads(args.manifest.read_text(encoding="utf-8"))
        members = [f["path"] for f in doc["files"]]
        rows.append(check_manifest(root, args.manifest)[0])
    rows += check_hygiene(root, members) + check_cabinet_refs(root)
    rows += check_key_shapes(root, members) + check_media_pins(root, members)
    rows += run_shipped_acceptance(root)

    passed = sum(1 for r in rows if r["passed"])
    print(f"target: {root}")
    for r in rows:
        print(f"[{r['status']}] {r['name']} — {r['detail']}")
    print(f"Result: {passed}/{len(rows)} checks passed")
    if args.json:
        args.json.write_text(json.dumps({"root": str(root), "mode": "package" if members else "tree",
                                         "total": len(rows), "passed": passed, "rows": rows},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
