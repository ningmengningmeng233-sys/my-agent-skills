#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选包异目录验收实跑器（E6）：核对 SHA-256 → 全新解压 → 三步离线门 → 退出码落表。

为什么需要它：候选包按白名单不含 `测试/e2e` 静态快照，且质量门要求 flake8/pytest，
直接跑 `run_all_checks.py` 会因缺快照/缺依赖失败。本脚本把「先重建快照 → 再跑总门 →
再 compileall」的顺序、以及「每个解释器各自全新解压目录」的隔离固定下来。

为什么是 Python 而不是 shell：包根目录名是中文（资产提取/）。shell 在非 UTF-8 区域
（中文 Windows 的 GBK、`LANG=C` 的 Linux）下把该路径经命令行往返会损坏字节，Python 以
str 路径交给子进程则无损；同时 stdlib 即可完成 sha256 与解压，不额外依赖 sha256sum/unzip。

判定语义（严格口径，宁可非零也不假通过）：
- 只有**每个请求的解释器**的 snapshot/gate/compileall **全部 rc=0**，整体才退 0；
- 解释器不存在/缺 pytest+flake8 记 SKIP，某步非零记 FAIL，两者都在结果表里单列 status 列；
- 任一 SKIP 或 FAIL 一律非零退出，且 SKIP 与 FAIL 用不同退出码区分。

运行隔离：`--out` 是父目录，每次运行在其中新建**唯一** `run-<UTC时间戳>-<pid>[-n]/`
（含 results.tsv / environment.txt / logs/ / unpack-*/），因此本轮结果表天然不含旧行；
若必须写固定目录，用 `--run-dir`（已存在且非空时拒绝，除非 `--reuse-run-dir`——
此时旧表**归档**为 `results.prev-<戳>.tsv` 而非删除）。

用法：
  单解释器（当前解释器）：
    python 测试/run_linux_acceptance.py --zip <候选包 zip> --expected-sha256 <hash> --out <目录>
  覆盖多解释器（Ubuntu 验收要求三版本全绿）：
    python3 测试/run_linux_acceptance.py --zip <zip> --expected-sha256 <hash> \
        --out "$HOME/asset_accept_linux" --python-list "python3.10 python3.11 python3.12"

退出码：0=全 PASS；1=存在 FAIL；3=无 FAIL 但存在 SKIP（没有证据，不算通过）；
       2=参数/完整性错误（含缺 `--expected-sha256`、哈希不匹配、证据目录冲突）。
"""
import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from datetime import datetime, timezone
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NO_EVIDENCE = 3

STEPS = ("snapshot", "gate", "compileall")
STEP_LABEL = {
    "snapshot": "e2e 快照重建（e2e_flow.py --snapshot，离线 mock）",
    "gate": "离线总门（run_all_checks.py）",
    "compileall": "字节码编译（compileall -q scripts 测试）",
}
STEP_ARGV = {
    "snapshot": lambda py, pkg: [py, str(pkg / "测试" / "e2e_flow.py"), "--snapshot"],
    "gate": lambda py, pkg: [py, str(pkg / "测试" / "run_all_checks.py")],
    "compileall": lambda py, pkg: [py, "-m", "compileall", "-q", "scripts", "测试"],
}
TSV_HEADER = "interpreter\tstep\tstatus\trc\tlog\n"


class UsageError(Exception):
    """参数/证据目录冲突：调用方需修正输入，不是被测包的问题。"""


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _child_env() -> dict:
    """F1.1 口径：子进程不得依赖编码保护伞——本 runner 显式剥离后仍须全绿。"""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["ASSET_WORKERS"] = env.get("ASSET_WORKERS", "8")
    return env


def _new_run_dir(out: Path, run_dir: str = "", reuse: bool = False) -> Path:
    """本轮证据目录：默认唯一新建；显式指定时非空则拒绝（除非显式复用并归档旧表）。"""
    if run_dir:
        rd = Path(run_dir).resolve()
        if rd.exists() and any(rd.iterdir()):
            if not reuse:
                raise UsageError(
                    f"证据目录已存在且非空: {rd}——换一个目录，或显式加 --reuse-run-dir "
                    "把旧 results.tsv/environment.txt 归档为 *.prev-<戳>.* 后重跑")
            stamp = _stamp()
            for name in ("results.tsv", "environment.txt"):
                p = rd / name
                if p.is_file():
                    p.rename(p.with_name(f"{p.stem}.prev-{stamp}{p.suffix}"))
        rd.mkdir(parents=True, exist_ok=True)
        return rd
    base = out / f"run-{_stamp()}-{os.getpid()}"
    rd, n = base, 2
    while rd.exists():
        rd = base.with_name(f"{base.name}-{n}")
        n += 1
    rd.mkdir(parents=True)
    return rd


def _run_step(pkg: Path, argv: list[str], log_path: Path) -> int:
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write(f"# cwd: {pkg}\n# argv: {argv}\n\n")
        log.flush()
        r = subprocess.run(argv, cwd=str(pkg), stdout=log, stderr=subprocess.STDOUT, env=_child_env())
    return r.returncode


def _interpreter_ok(python: str) -> tuple[bool, str]:
    """确认解释器可用且带 pytest/flake8；不满足时返回原因（记为 SKIP，不记 PASS）。"""
    try:
        r = subprocess.run([python, "-c", "import sys, pytest, flake8; print(sys.version.split()[0])"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=_child_env())
    except OSError as e:
        return False, f"无法执行（{e}）"
    if r.returncode != 0:
        detail = (r.stdout + r.stderr).strip().splitlines()
        hint = "缺 pytest/flake8，先 `%s -m pip install -e '.[dev]'`" % python
        return False, f"{hint}（{detail[-1] if detail else 'rc!=0'}）"
    return True, r.stdout.strip()


def _extract_fresh(zip_path: Path, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    roots = [p for p in dest.iterdir() if p.is_dir()]
    if not roots:
        raise SystemExit(f"❌ 解压后找不到包根目录: {dest}")
    return roots[0]


def _record(run_dir: Path, interpreter: str, step: str, status: str, rc: str, log_rel: str) -> None:
    with (run_dir / "results.tsv").open("a", encoding="utf-8") as f:
        f.write(f"{interpreter}\t{step}\t{status}\t{rc}\t{log_rel}\n")


def run_one(zip_path: Path, run_dir: Path, python: str, label: str) -> str:
    """跑一个解释器的三步；返回该解释器的总体状态 PASS / FAIL / SKIP。"""
    (run_dir / "logs").mkdir(exist_ok=True)
    ok, detail = _interpreter_ok(python)
    if not ok:
        print(f"SKIP  {label}：{detail}")
        _record(run_dir, label, "ALL", "SKIP", "-", "-")
        return "SKIP"
    pkg = _extract_fresh(zip_path, run_dir / f"unpack-{label}")
    print(f"=== {label}（{python} {detail}）→ {pkg}")
    failed = False
    for step in STEPS:
        log_rel = f"logs/{label}-{step}.log"
        rc = _run_step(pkg, STEP_ARGV[step](python, pkg), run_dir / log_rel)
        status = "PASS" if rc == 0 else "FAIL"
        failed = failed or rc != 0
        _record(run_dir, label, step, status, str(rc), log_rel)
        print(f"  {step:<10} {status} rc={rc}  ({STEP_LABEL[step]})")
    return "FAIL" if failed else "PASS"


def _verify_integrity(zip_path: Path, expected: str) -> str:
    """完整性验收：期望 SHA-256 必填且必须匹配（缺失/不符都是参数级错误）。"""
    if not (expected or "").strip():
        raise UsageError("缺 --expected-sha256：不做完整性比对的验收无效（取自包外 SHA256SUMS 末行）")
    actual = _sha256(zip_path)
    if actual.lower() != expected.strip().lower():
        raise UsageError(f"zip SHA-256 不匹配\n   期望: {expected.strip()}\n   实际: {actual}")
    return actual


def main() -> int:
    ap = argparse.ArgumentParser(description="候选包异目录验收实跑器（离线，不需要 API）")
    ap.add_argument("--zip", required=True, help="候选包 zip 路径")
    ap.add_argument("--expected-sha256", default="",
                    help="期望的 zip SHA-256（必填；取自包外 SHA256SUMS 末行）")
    ap.add_argument("--out", required=True, help="证据父目录（每次运行在其中新建唯一 run-* 目录）")
    ap.add_argument("--run-dir", default="", help="显式指定本次证据目录（非空时拒绝，除非 --reuse-run-dir）")
    ap.add_argument("--reuse-run-dir", action="store_true",
                    help="允许复用非空 --run-dir：旧 results.tsv/environment.txt 归档为 *.prev-<戳>.*")
    ap.add_argument("--python", default=sys.executable, help="单个解释器路径（默认当前解释器）")
    ap.add_argument("--label", default="", help="该解释器的标签（默认取解释器名）")
    ap.add_argument("--python-list", default="",
                    help="多个解释器（空格分隔）：逐个跑满三步，全 PASS 才整体退 0")
    args = ap.parse_args()

    try:
        zip_path = Path(args.zip).resolve()
        if not zip_path.is_file():
            raise UsageError(f"候选包不存在: {zip_path}")
        actual = _verify_integrity(zip_path, args.expected_sha256)
        run_dir = _new_run_dir(Path(args.out).resolve(), args.run_dir, args.reuse_run_dir)
    except UsageError as e:
        print(f"❌ {e}")
        return EXIT_USAGE

    env_lines = [
        "=== 候选包异目录验收 ===",
        f"时间: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"包: {zip_path}",
        f"包 SHA-256: {actual}",
        f"包字节数: {zip_path.stat().st_size}",
        f"平台: {platform.platform()} / {platform.machine()}",
        f"python: {sys.version.split()[0]} ({sysconfig.get_platform()})",
        f"文件系统编码: {sys.getfilesystemencoding()}",
        f"证据目录: {run_dir}",
        "完整性: 与期望一致",
        "",
    ]
    (run_dir / "environment.txt").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    (run_dir / "results.tsv").write_text(TSV_HEADER, encoding="utf-8")
    print(env_lines[0])
    for line in env_lines[1:5]:
        print("  " + line)
    print(f"  ✅ zip SHA-256 与期望一致；证据目录: {run_dir}")

    if not args.python_list:
        status = run_one(zip_path, run_dir, args.python, args.label or Path(args.python).name)
        codemap = {"PASS": EXIT_OK, "FAIL": EXIT_FAIL, "SKIP": EXIT_NO_EVIDENCE}
        _print_summary(run_dir, [], codemap[status])
        return codemap[status]

    interpreters = args.python_list.split()
    print(f"\n🔍 覆盖解释器: {' '.join(interpreters)}（各自全新解压目录）")
    statuses = {name: run_one(zip_path, run_dir, name, name) for name in interpreters}
    failed = [n for n, s in statuses.items() if s == "FAIL"]
    skipped = [n for n, s in statuses.items() if s == "SKIP"]
    passed = [n for n, s in statuses.items() if s == "PASS"]
    rc = EXIT_FAIL if failed else (EXIT_NO_EVIDENCE if skipped else EXIT_OK)
    _print_summary(run_dir, interpreters, rc)
    print(f"PASS: {len(passed)}{'（' + '、'.join(passed) + '）' if passed else ''}"
          f"｜FAIL: {len(failed)}{'（' + '、'.join(failed) + '）' if failed else ''}"
          f"｜SKIP: {len(skipped)}{'（' + '、'.join(skipped) + '）' if skipped else ''}")
    if failed:
        print("❌ 有组合失败——按「失败转回归」规则先保存最小复现再修")
    if skipped:
        print("❌ 有解释器未产生证据（SKIP）——三版本全绿才算完成，SKIP 不计通过")
    return rc


def _print_summary(run_dir: Path, _interpreters: list[str], _rc: int) -> None:
    print(f"\n================ 汇总（详见 {run_dir / 'results.tsv'}）================")
    print((run_dir / "results.tsv").read_text(encoding="utf-8").rstrip())
    print(f"环境记录: {run_dir / 'environment.txt'}；日志目录: {run_dir / 'logs'}")


if __name__ == "__main__":
    sys.exit(main())
