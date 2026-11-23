#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""包内自检门：一条命令在当前树跑完，开发树与发布包同一把尺。

与其它产品的门同源，但本项目特有的三件事必须自己盯：
1. 版本有三个权威点（VERSION、SKILL.md frontmatter、pyproject.toml）——发布清单曾写 5.4.5
   而冻结树处处 5.4.4，签错版号就是签错包；
2. 启动器契约（`$PSScriptRoot`、解释器可覆盖、退出码透传、无个人路径回退）——
   这是 T03 的全部内容，回归时要看得见；
3. 合成 key 字面量：公开扫描对含 fake 的整行会抑制，包级读数于是报「凭据命中 0」。
   本门**不做占位抑制**：key 形状命中必须逐条出现在 `DECLARED_TEST_SECRETS.json` 里，
   漏一条判红，全部申报则记 DECLARED 并计数，绝不写成 0 命中。

约定：末行 `Result: N/M checks passed`；exit 0 只在无未过项时。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOV = ROOT / "scripts" / "package_governance"
SHIPPED_TESTS = ["tests/test_release_package.py", "tests/test_start_web_launcher.py",
                 "tests/test_launcher_lifecycle.py"]
PRIVATE_PATHS = [re.compile(r"[A-Z]:[\\/](?:Users|迁移|Janson)", re.I),
                 re.compile(r"[A-Z]:[\\/].*自制skill", re.I),
                 re.compile(r"/home/[a-z0-9_]+/", re.I)]
KEY_SHAPE = re.compile(r"\b(?:sk|ghp|github_pat|xox[bap])[-_][A-Za-z0-9\-]{16,}")
LIVE_CONFIG = re.compile(r"(^|/)(\.env|llm_nodes\.json)$")
RESIDUE = re.compile(r"__pycache__|\.pytest_cache|/test-results/|^web/test-results|\.playwright|"
                     r"egg-info|^\.github/")
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".yaml", ".yml", ".ps1", ".example"}
CABINET_REF = re.compile(r"schemas/[\w.\-]+")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def row(name: str, passed: bool, detail: str, status: str = "") -> dict:
    return {"name": name, "passed": bool(passed), "status": status or ("PASS" if passed else "FAIL"),
            "detail": detail[:1200]}


def front_version(text: str) -> str | None:
    m = re.search(r"\n\s*version:\s*\"?([\w.\-]+)\"?", text)
    return m.group(1) if m else None


def check_identity(root: Path) -> dict:
    trio = {}
    for label, getter in (
            ("VERSION", lambda: (root / "VERSION").read_text(encoding="utf-8").strip()),
            ("SKILL.md", lambda: front_version((root / "SKILL.md").read_text(encoding="utf-8",
                                                                             errors="replace"))),
            ("pyproject.toml", lambda: (re.search(r'^version\s*=\s*"([^"]+)"',
                                                  (root / "pyproject.toml").read_text(
                                                      encoding="utf-8"), re.M) or
                                        {"group": lambda _x: None}).group(1))):
        try:
            trio[label] = getter()
        except OSError:
            trio[label] = None
    if any(v is None for v in trio.values()):
        return row("version_single_source", False, f"取不到完整版本三元组：{trio}")
    same = len(set(trio.values())) == 1
    return row("version_single_source", same,
               f"{trio}" + ("" if same else " —— 三处不一致，发布清单会签出错版号"))


def check_license(root: Path) -> dict:
    lic = root / "LICENSE"
    if not lic.is_file():
        return row("license_text", False, "缺 LICENSE：frontmatter 的许可声明需要可核验文本")
    text = lic.read_text(encoding="utf-8")
    ok = "Proprietary" in text and "柠檬才不萌" in text
    return row("license_text", ok,
               f"LICENSE 存在（{len(text.splitlines())} 行），自有件 Proprietary、"
               f"孪生内核与合成件另有说明段" if ok else "LICENSE 缺少许可人或许可类型声明")


def check_launcher(root: Path) -> dict:
    ps = root / "start_web.ps1"
    if not ps.is_file():
        return row("launcher_contract", False, "缺 start_web.ps1")
    text = ps.read_text(encoding="utf-8", errors="replace")
    missing = []
    if "$PSScriptRoot" not in text:
        missing.append("未用 $PSScriptRoot（依赖调用方 cwd）")
    if "exit $LASTEXITCODE" not in text:
        missing.append("未透传退出码（启动失败会被读成成功）")
    if "$env:KNOWLEDGE_INDEX_PYTHON" not in text:
        missing.append("解释器不可由环境/参数覆盖")
    if re.search(r"[A-Za-z]:[\\/](?:Users|迁移)", text):
        missing.append("残留个人绝对路径回退")
    return row("launcher_contract", not missing, "；".join(missing) or "四项契约齐备")


def check_cabinet_refs(root: Path, members: list[str]) -> dict:
    reg = GOV / "CABINET_ONLY_REFS.json"
    if not reg.is_file():
        return row("cabinet_only_refs_declared", False, "缺柜根引用登记册", "NOT_RUN")
    declared = {i["reference"] for i in
                json.loads(reg.read_text(encoding="utf-8"))["references"]}
    found = set()
    for rel in members:
        p = root / rel
        if p.suffix.lower() != ".md" or not p.is_file():
            continue
        found |= set(CABINET_REF.findall(p.read_text(encoding="utf-8", errors="replace")))
    live = {r for r in found if (root / r).is_file()}
    missing = sorted(found - live - declared)
    stale = sorted(declared - found)
    return row("cabinet_only_refs_declared", not missing and not stale,
               f"柜根引用 {len(found)} 项，包内实有 {len(live)}；未登记 {missing[:3]}，"
               f"登记失效 {stale[:3]}")


def check_declared_secrets(root: Path, members: list[str]) -> dict:
    """key 形状命中必须逐条申报；本检查刻意不用占位词表做抑制。"""
    hits = []
    for rel in members:
        p = root / rel
        if p.suffix.lower() not in {".py", ".md", ".ps1", ".json"} or not p.is_file():
            continue
        for num, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if KEY_SHAPE.search(line):
                hits.append((rel, num))
    reg = GOV / "DECLARED_TEST_SECRETS.json"
    declared = set()
    if reg.is_file():
        doc = json.loads(reg.read_text(encoding="utf-8"))
        declared = {(i["path"], i["line"]) for i in doc["entries"]}
        bad_verdict = [i for i in doc["entries"] if i.get("verdict") != "TEST_PLACEHOLDER"
                       or not i.get("covered_by")]
    else:
        bad_verdict = ["登记册不存在"] if hits else []
    undeclared = sorted(set(hits) - declared)
    stale = sorted(declared - set(hits))
    ok = not undeclared and not stale and not bad_verdict
    return row("declared_test_secrets", ok,
               f"key 形状命中 {len(hits)} 处（无占位抑制）：未申报 {undeclared[:3]}，"
               f"登记失效 {stale[:3]}，申报不合格 {str(bad_verdict)[:120]}。"
               f"申报≠放行：包级公开扫描仍会以 SUPPRESSED_SHAPE 露出并要求人工确认",
               "DECLARED" if ok and hits else ("PASS" if ok else "FAIL"))


def check_shipped_tests(root: Path) -> dict:
    present = [p for p in SHIPPED_TESTS if (root / p).is_file()]
    if not present:
        return row("shipped_acceptance", False, "NOT_RUN: 包内没有随包验收测试", "NOT_RUN")
    proc = subprocess.run([sys.executable, "-m", "pytest", *present, "-q",
                          "-p", "no:cacheprovider"],
                          cwd=str(root), env=_test_subprocess_env(root),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=600)
    out = proc.stdout + proc.stderr
    if "no tests ran" in out:
        return row("shipped_acceptance", False, "NOT_RUN: pytest 不可用或没收到用例 " + out[-160:],
                   "NOT_RUN")
    return row("shipped_acceptance", proc.returncode == 0, out[-260:])


def check_hygiene(root: Path, members: list[str]) -> list[dict]:
    live = [m for m in members if LIVE_CONFIG.search(m)]
    residue = [m for m in members if RESIDUE.search(m)]
    priv, scanned = [], 0
    for rel in members:
        p = root / rel
        if p.suffix.lower() not in TEXT_SUFFIXES and p.name not in {"LICENSE", "VERSION"}:
            continue
        if not p.is_file():
            continue
        scanned += 1
        text = p.read_text(encoding="utf-8", errors="replace")
        if any(r.search(text) for r in PRIVATE_PATHS):
            m = next(r.search(text) for r in PRIVATE_PATHS if r.search(text))
            priv.append(f"{rel}:{text[:m.start()].count(chr(10)) + 1}")
    return [row("no_live_config", not live,
                f"实配/私钥类文件 {len(live)}：{live[:4]}" if live else f"{len(members)} 成员无 .env、无 llm_nodes.json"),
            row("no_build_or_test_residue", not residue,
                f"构建/测试残留 {len(residue)}：{residue[:4]}" if residue else "无缓存、无 test-results、无 CI 目录"),
            row("no_private_paths", not priv, f"扫描 {scanned} 份文本，私人路径命中 {len(priv)}：{priv[:4]}")]


def _test_subprocess_env(root: Path) -> dict:
    """跑包内验收测试时，字节码与 pytest 缓存都不许落进被验收目录。

    只挪 compileall 的产物不够：包内验收那一步（pytest 导入 scripts/）同样会写 __pycache__，
    在普通环境下（调用方没设 PYTHONDONTWRITEBYTECODE）门会再次自己判红。
    所以门自己启动的子进程统一带上出树口径，而不是要求调用方先配好环境。
    """
    prefix = Path(tempfile.mkdtemp(prefix="gate-test-pyc-", dir=str(_gate_tmp_root())))
    return dict(os.environ, PYTHONPYCACHEPREFIX=str(prefix), PYTHONDONTWRITEBYTECODE="1")


def _gate_tmp_root() -> Path:
    """编译产物落盘位置：显式 GATE_TMP_ROOT 优先，否则走系统临时目录；绝不让字节码落在被验收的树里。"""
    env = os.environ.get("GATE_TMP_ROOT", "").strip()
    cand = Path(env) if env else Path(tempfile.gettempdir()) / "gate-pyc"
    try:
        cand.mkdir(parents=True, exist_ok=True)
        return cand
    except OSError:
        return Path(tempfile.gettempdir())


def compile_scripts_outside_tree(scripts_dir: Path) -> tuple[bool, str]:
    """用子进程 + PYTHONPYCACHEPREFIX 把 .pyc 引到树外临时目录。

    上一版直接 `compileall.compile_dir(force=True)` 把字节码写进 `scripts/__pycache__`，
    同一把门的卫生项又禁止 `__pycache__` —— 门自己把自己判红，收件人照 INSTALL.md 跑必见 8/9。
    这里既不放宽卫生检查，也不跳过缓存检查，只把产物挪出被验收目录。
    """
    prefix = Path(tempfile.mkdtemp(prefix="gate-pyc-", dir=str(_gate_tmp_root())))
    env = dict(os.environ, PYTHONPYCACHEPREFIX=str(prefix), PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run([sys.executable, "-m", "compileall", "-q", "-f", str(scripts_dir)],
                              capture_output=True, text=True, errors="replace",
                              env=env, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"编译子进程异常：{exc}"
    polluted = sorted(str(p.relative_to(scripts_dir.parent).as_posix())
                      for p in scripts_dir.rglob("__pycache__"))
    produced = sum(1 for p in prefix.rglob("*.pyc"))
    out = (proc.stdout or "") + (proc.stderr or "")
    if polluted:
        return False, f"字节码仍落在被验收目录内：{polluted[:3]}"
    detail = (f"编译产物引到树外（前缀 {prefix.name}，生成 {produced} 个 .pyc）；"
              f"树内 __pycache__ 目录 0；退出码 {proc.returncode}"
              + ("" if proc.returncode == 0 else "；输出尾 " + out[-160:]))
    return proc.returncode == 0, detail



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--as-package", action="store_true")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    root = Path(args.root).resolve()

    members = None
    comp_ok, comp_detail = compile_scripts_outside_tree(root / "scripts")
    rows = [row("compileall", comp_ok, comp_detail),
            check_identity(root), check_license(root), check_launcher(root)]
    if args.manifest and args.manifest.is_file():
        doc = json.loads(args.manifest.read_text(encoding="utf-8"))
        members = [f["path"] for f in doc["files"]]
        absent = [m for m in members if not (root / m).is_file()]
        mismatched = [f["path"] for f in doc["files"]
                      if (root / f["path"]).is_file()
                      and sha256_file(root / f["path"]) != f["sha256"]]
        rows.append(row("manifest_binding", not absent and not mismatched,
                        f"清单 {len(members)} 项：树内缺 {absent[:3]}，摘要不符 {mismatched[:3]}"))
    if args.as_package and members is None:
        members = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    if members is not None:
        rows += check_hygiene(root, members)
        rows.append(check_cabinet_refs(root, members))
        rows.append(check_declared_secrets(root, members))
    else:
        rows.append(row("package_hygiene", True,
                        "DECLARED: 开发树本来含 .env 与 llm_nodes.json 实配；解压包内或用 --manifest 复跑才判这类",
                        "DECLARED"))
        rows.append(check_cabinet_refs(root, [p.relative_to(root).as_posix()
                                              for p in root.rglob("*.md") if p.is_file()]))
        rows.append(check_declared_secrets(root, [p.relative_to(root).as_posix()
                                                  for p in (root / "scripts").rglob("*.py")]))
    rows.append(check_shipped_tests(root))

    failed = [r["name"] for r in rows if not r["passed"]]
    report = {"root": str(root), "mode": "package" if members is not None else "dev-tree",
              "total": len(rows), "passed": len(rows) - len(failed), "failed": failed,
              "rows": rows}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8", newline="\n")
    for r in rows:
        print(f"[{r['status']:>8}] {r['name']}: {r['detail'][:110]}")
    print(f"Result: {report['passed']}/{report['total']} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
