#!/usr/bin/env python3
"""Unified local quality gate: lint + unit tests + offline e2e checks."""
import os
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL = Path(__file__).resolve().parent.parent

# 规格 3.1/9：核心反推与资料签发不得依赖任何生成式 API 配置。
# 显式清空密钥环境跑核心门，防止"开发者本机恰好有密钥"掩盖隐性依赖。
NO_SECRET_ENV = {"GEMINI_API_KEY": "", "OPENAI_API_KEY": "", "GOOGLE_API_KEY": ""}

CHECKS = [
    ("flake8", [sys.executable, "-m", "flake8", "--count", str(SKILL / "scripts"), str(SKILL / "测试")]),
    # 工单 8.3：编译门（语法/导入期错误在打包前拦下）
    ("compileall", [sys.executable, "-m", "compileall", "-q", str(SKILL / "scripts"),
                    str(SKILL / "测试")]),
    # 工单 8.3：仓库级密钥扫描（复用发布级正则，只计数不回显）
    ("secret scan", [sys.executable, str(SKILL / "scripts" / "secret_scan.py"), "--quiet"]),
    ("unit tests", [sys.executable, "-m", "pytest", str(SKILL / "测试" / "unit"), "-q"]),
    ("core without visual",
     [sys.executable, "-m", "pytest", str(SKILL / "测试" / "unit" / "test_core_without_visual.py"), "-q"],
     NO_SECRET_ENV),
    ("style regression", [sys.executable, str(SKILL / "测试" / "run_style_test.py")]),
    ("verdict regression", [sys.executable, str(SKILL / "测试" / "run_verdict_test.py")]),
    # 规格 2.2/4：TXT、DOCX、EPUB 三种正式输入各建 fresh 项目跑到真实签发
    # 规格 11：跨类型金标准契约（老罗/长发/甲胄/宽袍/场景/道具/失败反例）
    ("visual gold contract",
     [sys.executable, "-m", "pytest", str(SKILL / "测试" / "unit" / "test_visual_gold_contract.py"),
      "-q"], NO_SECRET_ENV),
    # 规格 8/9：离线视觉端到端（计划→导出→导入→QC→拼版→签发 + 失败/恢复矩阵）
    ("visual offline e2e", [sys.executable, str(SKILL / "测试" / "run_visual_offline.py")]),
    ("source formats smoke", [sys.executable, str(SKILL / "测试" / "run_source_formats.py")]),
    ("e2e checks", [sys.executable, str(SKILL / "测试" / "run_e2e_check.py")]),
    ("fresh v2.13.1 regression", [sys.executable, str(SKILL / "测试" / "run_fresh_v2131.py")]),
    ("release migration regression", [sys.executable, str(SKILL / "测试" / "run_release_migration.py")]),
    # 工单 12：旧库迁移 / 回滚 / 影子运行（幂等 + scan 阶段哈希不变）
    ("asset ledger migration", [sys.executable, str(SKILL / "测试" / "run_asset_ledger_migration.py")]),
    # 工单 11/14：质量不退化的性能基准（默认 mock，无真实密钥）。新链路已在工单 14 接线，
    # 因此这里**撤掉** `--allow-not-run`：存在未测项就要拦下，否则性能门迟早变成空门。
    ("asset compiler benchmark",
     [sys.executable, str(SKILL / "测试" / "run_asset_compiler_benchmark.py"),
      "--fixture", str(SKILL / "测试" / "fixtures" / "asset_compiler_regression"),
      "--out", str(SKILL / ".process" / "benchmark.json")]),
    # 工单 S5 / Task D2：十项人工画质表 + 「机器签发不等于内容正确」发布闸门。
    # 十项未全由真人签署、或真实两图冒烟仍 BLOCKED-CRED 时，版本号不得去掉 -rc、
    # 验收文档不得正面宣称「正式版已完成」；口径自洽即 rc=0（当前即为 rc + 全表待人工）。
    ("manual qc gate", [sys.executable, str(SKILL / "scripts" / "check_manual_qc.py")]),
]


def git_diff_check() -> bool:
    """`git diff --check`：空白错误与冲突标记。非 git 环境（发布包解压副本）自动跳过并说明。"""
    print("\n🔍 git diff --check")
    if not (SKILL / ".git").exists():
        print("  ⏭️ 非 git 工作区（解压副本/发布包），跳过该项")
        return True
    result = subprocess.run(["git", "diff", "--check"], cwd=SKILL, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode == 0:
        print("  ✅ git diff --check passed")
        return True
    print("  ❌ git diff --check failed")
    return False


def run(name: str, cmd: list[str], env_overrides: dict[str, str] | None = None) -> bool:
    print(f"\n🔍 {name}")
    env = {**os.environ, **env_overrides} if env_overrides else None
    result = subprocess.run(cmd, cwd=SKILL, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=env)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    if result.returncode == 0:
        print(f"  ✅ {name} passed")
        return True
    print(f"  ❌ {name} failed")
    return False


def main() -> int:
    print("=" * 50)
    print("Local quality gate")
    print("=" * 50)
    results = [run(*check) for check in CHECKS]
    results.append(git_diff_check())
    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 50)
    print(f"Result: {passed}/{total} checks passed")
    print("=" * 50)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
