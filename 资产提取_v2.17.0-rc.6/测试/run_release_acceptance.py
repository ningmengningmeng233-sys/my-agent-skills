#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选包异目录验收（阶段 8 工单 8.4）。

只做一件事：在**解压出来的技能副本**里，用三个临时项目验证三种视觉模式的无密钥错误边界，
并把每条命令的退出码与关键输出落成一张表。**零网络、零密钥**——全程不设任何 API key。

口径（规格三 / 工单 8.4）：

- `data_only`：`visual-plan` 必须拒绝建计划；`run` 必须拒绝自动生成（原因码 `VISUAL_MODE_DATA_ONLY`）。
- `prompt_only`：`run` 必须拒绝自动生成（`VISUAL_MODE_PROMPT_ONLY`）；`export` 必须照常可用（零网络）。
- `direct_generate`：无密钥时 `run`/`run-ready` 必须**明确失败**并给出 prompt-only / 手工出路，
  绝不允许「假通过」。

用法：
    python 测试/run_release_acceptance.py --lib-root <解压后的技能根> --work-root <外置独占工作根>
    python 测试/run_release_acceptance.py --lib-root <技能根> --json 报告.json --skip-heavy

**为什么必须有 `--work-root`**（工单 Z01/Z02）：heavy 段的四条包内自检会在**被审树本身**里
重建 e2e 快照、跑 pytest/compileall/benchmark，于是随包声明成员
`测试/e2e/资产库/交付清单.json` 被改写，`.pytest_cache/`、`.process/`、`.lemon/run-ledger.jsonl`、
`__pycache__/`、e2e 状态库等新文件留在树内——门跑完后「包树≠清单」，逐成员对账失去意义。
现在 heavy 段改在 `--work-root/stages/<轮次>/skill` 的**只读暂存副本**里跑，账本/缓存/字节码/临时
全部钉在工作根下，被审树跑完必须与开跑前逐文件一致；不一致直接判红，不靠文档登记豁免。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):  # 作为脚本直跑时才能按路径导入同目录助手
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    # 助手是本入口**自己**要 import 的模块：解释器在启动时就定了 sys.pycache_prefix，
    # 之后改环境变量无效，所以必须在 import 之前关掉本进程的字节码写入，
    # 否则 `测试/__pycache__/acceptance_workroot.*.pyc` 会落在被审树里（污染逐成员对账）。
    # 这只影响父进程自身的 import；heavy 段的 `compileall` 在子进程里跑，
    # 由 PYTHONPYCACHEPREFIX 引到工作根，编译门仍然是真执行（有测试钉住不许设 DONTWRITEBYTECODE）。
    sys.dont_write_bytecode = True
import acceptance_workroot as WR  # noqa: E402

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@contextlib.contextmanager
def _scoped_env(extra: dict):
    """在本入口进程内临时挂上外置落点，退出即还原。

    这样 heavy 段与三模式段的每条子进程都自然继承同一套 TEMP / PYTHONPYCACHEPREFIX /
    UNIFIED_LEDGER_PATH / PYTEST_ADDOPTS，不必逐个调用点传参（漏传一个就把产物写回包里）。
    """
    saved = {k: os.environ.get(k) for k in extra}
    os.environ.update(extra)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _env() -> dict:
    """验收环境：**清空全部密钥**，并固定并发与 UTF-8（外置落点由 _scoped_env 提供）。"""
    env = {**os.environ, "ASSET_WORKERS": "4", "PYTHONUTF8": "1"}
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        env.pop(key, None)
    return env


def _run(skill: Path, args: list[str], *, no_pyc: bool = True) -> subprocess.CompletedProcess:
    """跑一条产品命令。

    默认给子进程加 `-B`：三模式段是**在被审树里**跑的，不加就会把
    `scripts/engine/__pycache__/*.pyc` 写进包根，门跑完「包树≠清单」。
    heavy 段在只读暂存副本里跑，必须保留写字节码的真实行为（`compileall` 不能被阉成空操作），
    所以那里显式传 `no_pyc=False`。
    """
    argv = [sys.executable] + (["-B"] if no_pyc else []) + [str(a) for a in args]
    env = _env()
    if no_pyc:
        # -B 只管这一层子进程；产品脚本自己再起的孙进程要靠这个可继承的环境变量，
        # 否则 scripts/engine/__pycache__ 仍会落进被审树（实测：加 -B 后仍漏 6 件）
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(argv, cwd=str(skill),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def _make_project(root: Path, name: str) -> Path:
    lib = root / name / "资产库"
    (lib / ".process").mkdir(parents=True)
    (lib / "原文段" / "测试-作者").mkdir(parents=True)
    (lib / "原文段" / "测试-作者" / "段01.md").write_text(
        "第一场 茶楼\n老罗把缺角粗陶碗放在桌上。\n", encoding="utf-8")
    index = {
        "schema_version": 1, "source_fingerprint": "sha256:acceptance",
        "assets": [{
            "asset_id": "CH_老罗", "stable_key": "CHR:00001:1", "type": "人物",
            "name": "老罗", "priority": "P1", "fact_level": "剧本事实",
            "evidence": [{"segment": "段01.md", "chapter": "第一场", "line": "00001",
                          "quote": "老罗把缺角粗陶碗放在桌上"}],
            "dependencies": [], "relations": [], "status": "CONFIRMED",
        }],
    }
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return lib


def _configure(skill: Path, lib: Path, mode: str) -> subprocess.CompletedProcess:
    return _run(skill, [skill / "scripts" / "maintenance.py", "configure-visual",
                        "--lib", lib, "--mode", mode])


def _record(rows: list[dict], label: str, proc: subprocess.CompletedProcess,
            *, expect_zero: bool, expect_tokens: tuple[str, ...] = (),
            argv: list = ()) -> None:
    text = proc.stdout + proc.stderr
    ok = (proc.returncode == 0) if expect_zero else (proc.returncode != 0)
    missing = [token for token in expect_tokens if token not in text]
    # 失败项要留名：只说「16 项里挂了一项」对复核毫无用处。
    failed_items = [line.strip() for line in text.splitlines() if "❌" in line][:8]
    rows.append({
        "case": label,
        "command": " ".join(str(a) for a in argv),
        "returncode": proc.returncode,
        "expected": "rc=0" if expect_zero else "rc≠0",
        "tokens": list(expect_tokens),
        "tokens_missing": missing,
        "passed": bool(ok and not missing),
        "failed_items": failed_items,
        "output_tail": "\n".join(text.strip().splitlines()[-3:]),
    })


def verdict(rows: list, passed: int, audit: dict) -> bool:
    """放行判定：零收集一律不算绿；被审树被改过也不算绿；每项都必须真过。"""
    return bool(rows) and passed == len(rows) and bool(audit.get("ok"))


def run_acceptance(skill: Path, *, heavy: bool = True, work_root: Path | None = None,
                   protected: tuple = (), rnd: str = "round1") -> dict:
    skill = Path(skill).resolve()
    assert (skill / "SKILL.md").is_file(), f"不是技能根目录：{skill}"
    rows: list[dict] = []
    audit_before = WR.tree_digest(skill)
    wroot = stage = None
    work_env: dict = {}
    if heavy:
        if work_root is None:
            raise WR.Refused(
                "HEAVY_NEEDS_WORK_ROOT",
                "heavy 段会在被审树内重建 e2e 快照并留下缓存/账本/字节码，必须给外置 --work-root")
        wroot = WR.validate_work_root(pkg_root=skill, work_root=work_root, protected=protected)
        sp = WR.stage_paths(work_root=wroot, rnd=rnd)
        stage = WR.stage_copy(pkg_root=skill, dest=sp["skill"])
        work_env = WR.gate_env_for_work(work_root=wroot, base_env={})

    with _scoped_env(work_env), tempfile.TemporaryDirectory(prefix="asset_accept_") as tmp:
        root = Path(tmp)

        if heavy:
            # --- 包内自检（工单 14 步骤 5）：快照 / 全门 / 迁移 / compileall。
            # 这四条**必须在只读暂存副本里跑**（工单 Z02）：它们会改写随包声明成员
            # `测试/e2e/资产库/交付清单.json` 并在树内留 `.pytest_cache`、`.process`、
            # `.lemon/run-ledger.jsonl`、`__pycache__`，在被审树里跑一次就等于毁掉一次逐成员对账。
            # 顺序不能随意：`测试/e2e/` 被 .gitignore 排除，**不在包里**，而全门里的
            # 「e2e checks」要求快照已存在。因此必须先 `--snapshot` 重建，再跑全门。
            # 注意 `_run` 已负责前置解释器，这里只给参数，别再传 sys.executable。
            for case, argv in (
                ("包内端到端快照重建 e2e_flow.py --snapshot",
                 ["测试/e2e_flow.py", "--snapshot"]),
                ("包内全门 run_all_checks.py", ["测试/run_all_checks.py"]),
                ("包内迁移回归（总账 scan/apply/rollback/shadow）",
                 ["测试/run_asset_ledger_migration.py"]),
                ("包内 compileall", ["-m", "compileall", "-q", "scripts", "测试"]),
            ):
                _record(rows, case, _run(stage["dest"], argv, no_pyc=False), expect_zero=True,
                        argv=list(argv))

        # --- data_only ---
        lib = _make_project(root, "data_only")
        conf = _configure(skill, lib, "data-only")
        _record(rows, "data_only 设置成功", conf, expect_zero=True,
                argv=["configure-visual", "--mode", "data-only"])
        plan = _run(skill, [skill / "scripts" / "maintenance.py", "visual-plan",
                            "--lib", lib, "--style", "仙侠国风电影CG"])
        _record(rows, "data_only 拒绝建视觉计划", plan, expect_zero=False,
                expect_tokens=("VISUAL_MODE_DATA_ONLY",),
                argv=["maintenance.py", "visual-plan"])
        run_one = _run(skill, [skill / "scripts" / "visual_production.py", "run",
                               "--lib", lib, "--task", "CH_老罗-正脸-v01"])
        _record(rows, "data_only 拒绝自动生成", run_one, expect_zero=False,
                expect_tokens=("VISUAL_MODE_DATA_ONLY",),
                argv=["visual_production.py", "run"])
        rows[-1]["plan_created"] = (lib / "视觉资产" / "计划.toml").exists()

        # --- prompt_only ---
        lib = _make_project(root, "prompt_only")
        conf = _configure(skill, lib, "prompt-only")
        _record(rows, "prompt_only 设置成功", conf, expect_zero=True,
                argv=["configure-visual", "--mode", "prompt-only"])
        plan = _run(skill, [skill / "scripts" / "maintenance.py", "visual-plan",
                            "--lib", lib, "--style", "仙侠国风电影CG"])
        _record(rows, "prompt_only 建计划成功（零网络）", plan, expect_zero=True,
                argv=["maintenance.py", "visual-plan"])
        run_one = _run(skill, [skill / "scripts" / "visual_production.py", "run",
                               "--lib", lib, "--task", "CH_老罗-正脸-v01"])
        _record(rows, "prompt_only 拒绝自动生成", run_one, expect_zero=False,
                expect_tokens=("VISUAL_MODE_PROMPT_ONLY",),
                argv=["visual_production.py", "run"])
        export = _run(skill, [skill / "scripts" / "visual_production.py", "export-all",
                              "--lib", lib])
        _record(rows, "prompt_only 导出任务包可用（零网络）", export, expect_zero=True,
                argv=["visual_production.py", "export-all"])

        # --- direct_generate 无密钥 ---
        lib = _make_project(root, "direct_generate")
        conf = _configure(skill, lib, "direct-generate")
        _record(rows, "direct_generate 设置成功", conf, expect_zero=True,
                argv=["configure-visual", "--mode", "direct-generate"])
        plan = _run(skill, [skill / "scripts" / "maintenance.py", "visual-plan",
                            "--lib", lib, "--style", "仙侠国风电影CG"])
        _record(rows, "direct_generate 建计划成功", plan, expect_zero=True,
                argv=["maintenance.py", "visual-plan"])
        run_one = _run(skill, [skill / "scripts" / "visual_production.py", "run",
                               "--lib", lib, "--task", "CH_老罗-正脸-v01"])
        _record(rows, "direct_generate 无密钥拒绝自动生成", run_one, expect_zero=False,
                expect_tokens=("GEMINI_API_KEY", "export"),
                argv=["visual_production.py", "run"])
        run_ready = _run(skill, [skill / "scripts" / "visual_production.py", "run-ready",
                                 "--lib", lib])
        _record(rows, "direct_generate 无密钥拒绝批量生成", run_ready,
                expect_zero=False, expect_tokens=("GEMINI_API_KEY", "export-all"),
                argv=["visual_production.py", "run-ready"])
        status = _run(skill, [skill / "scripts" / "visual_production.py", "status",
                              "--lib", lib, "--json"])
        _record(rows, "direct_generate 状态可读（零密钥）", status, expect_zero=True,
                argv=["visual_production.py", "status", "--json"])
        if status.returncode == 0:
            payload = json.loads(status.stdout)
            rows[-1]["mode"] = payload.get("mode")
            rows[-1]["ready"] = len(payload.get("ready", []))

    passed = sum(1 for row in rows if row["passed"])
    audit = WR.audit_tree(skill, audit_before)
    return {"skill_root": str(skill), "rows": rows, "passed": passed, "total": len(rows),
            "ok": verdict(rows, passed, audit),
            "work_root": str(wroot) if wroot else None,
            "heavy_stage": (stage["dest"], stage["files"]) if stage else None,
            "package_audit": audit}


def main() -> int:
    parser = argparse.ArgumentParser(description="候选包异目录验收（三模式无密钥边界 + 包内自检）")
    parser.add_argument("--lib-root", required=True, help="解压后的技能根目录")
    parser.add_argument("--work-root", help="外置独占工作根（跑 heavy 段必填；不得在被审树内/受保护目录内/撞名）")
    parser.add_argument("--protected", action="append", default=[],
                        help="受保护根（真实库/现役安装/其它候选），可重复；工作根落在其内即拒绝")
    parser.add_argument("--round", default="round1", help="本轮暂存目录名，两轮各用独立输入")
    parser.add_argument("--json", help="把结果写成 JSON 报告")
    parser.add_argument("--skip-heavy", action="store_true",
                        help="跳过包内四项自检（快照/全门/迁移/compileall），只跑三模式边界")
    args = parser.parse_args()
    skill = Path(args.lib_root)
    if args.json:
        WR.validate_report_path(pkg_root=skill, report_path=args.json)
    report = run_acceptance(
        skill, heavy=not args.skip_heavy,
        work_root=Path(args.work_root) if args.work_root else None,
        protected=tuple(args.protected), rnd=args.round)
    print("=" * 50)
    print("候选包异目录验收（三模式 · 无密钥 · 零网络）")
    print("=" * 50)
    for row in report["rows"]:
        mark = "✅" if row["passed"] else "❌"
        print(f"{mark} {row['case']}｜{row['expected']}｜实际 rc={row['returncode']}")
        if not row["passed"]:
            print(f"   缺少关键词：{row['tokens_missing']}")
            print("   " + row["output_tail"].replace("\n", "\n   "))
    print("-" * 50)
    print(f"Result: {report['passed']}/{report['total']} cases passed")
    aud = report["package_audit"]
    print(f"被审树卫生: {'OK 逐成员不变' if aud['ok'] else 'RED 包树已被改动'}"
          f"｜改写 {len(aud['modified'])} 新增 {len(aud['added'])} 删除 {len(aud['removed'])}"
          f"｜工作根 {report['work_root']}｜暂存 {report['heavy_stage']}")
    for k in (aud["modified"] + aud["added"] + aud["removed"])[:12]:
        print("   ⚠️", k)
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8", newline="\n")
        print(f"报告：{args.json}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
