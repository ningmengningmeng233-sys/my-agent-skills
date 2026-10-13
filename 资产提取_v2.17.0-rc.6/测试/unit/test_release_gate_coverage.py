#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 8 工单 8.3：发布级自动门的覆盖映射（机械守卫）。

规格要求的每一项都必须**真的有门在跑**，而不是写在文档里：本文件把「能力 → 门 → 测试文件」
钉死，任何一项被删/改名/不再被门覆盖都会红。
"""
import re
import sys
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

GATE = REPO_ROOT / "测试" / "run_all_checks.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "quality.yml"

# 规格 8.3 要求的门名（在 run_all_checks.py 的 CHECKS 里必须出现）
REQUIRED_GATE_NAMES = (
    "flake8",
    "compileall",
    "secret scan",
    "unit tests",
    "core without visual",
    "visual gold contract",
    "visual offline e2e",
    "source formats smoke",
    "e2e checks",
    "fresh v2.13.1 regression",
    "release migration regression",
)

# 能力 → 必须有测试的落点文件（相对仓库根）
CAPABILITY_FILES = {
    "三模式与 prompt-only 零网络": "测试/unit/test_visual_modes.py",
    "导演构图卡": "测试/unit/test_director_composition.py",
    "场景站位与连续性": "测试/unit/test_scene_blocking.py",
    "并发竞态与崩溃恢复": "测试/unit/test_visual_scheduler.py",
    "传输重试与自适应并发": "测试/unit/test_retry_policy.py",
    "审核六项绑定": "测试/unit/test_review_contract.py",
    "发布绕过负例": "测试/unit/test_visual_delivery_gate_rc2.py",
    "发布器与正式版本": "测试/unit/test_visual_publisher.py",
    "旧库迁移": "测试/unit/test_visual_migration_v216.py",
    "迁移回归": "测试/run_release_migration.py",
    "硬门与判定集合": "测试/unit/test_image_qc.py",
    "主入口路由与体量": "测试/unit/test_skill_router_size.py",
    "中文长路径": "测试/unit/test_long_cjk_paths.py",
}


def _gate_text() -> str:
    return GATE.read_text(encoding="utf-8")


def test_gate_declares_every_required_check():
    text = _gate_text()
    missing = [name for name in REQUIRED_GATE_NAMES if f'("{name}"' not in text]
    assert not missing, f"总门缺少这些检查项：{missing}"


def test_gate_has_exactly_one_entry_per_check_and_counts_all():
    text = _gate_text()
    assert "results.append(git_diff_check())" in text, "总门必须包含 git diff --check"
    assert re.search(r"passed = sum\(results\)", text), "总门必须汇总全部检查项"


def test_each_required_capability_has_a_real_test_file():
    missing = [rel for rel in CAPABILITY_FILES.values() if not (REPO_ROOT / rel).is_file()]
    assert not missing, f"这些能力没有落点测试文件：{missing}"


def test_capability_test_files_actually_contain_tests():
    """落点文件必须真的有内容：pytest 文件要有测试函数，脚本式门要有可执行入口。"""
    empty = []
    for capability, rel in CAPABILITY_FILES.items():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        has_tests = re.search(r"^def test_|^\s+def test_", text, re.M) is not None
        has_entry = 'if __name__ == "__main__"' in text
        if not (has_tests or has_entry):
            empty.append(f"{capability}（{rel}）")
    assert not empty, f"这些落点文件既没有测试也没有执行入口：{empty}"


def test_unit_gate_runs_the_unit_directory_that_holds_capability_tests():
    text = _gate_text()
    assert '"unit tests"' in text and "测试\" / \"unit\"" in text, (
        "能力落点测试都放在 测试/unit 下，总门的单测项必须跑整个目录")
    unit_dir = REPO_ROOT / "测试" / "unit"
    for rel in CAPABILITY_FILES.values():
        if rel.startswith("测试/unit/"):
            assert (unit_dir / Path(rel).name).is_file()


def test_workflow_runs_the_same_entry_on_both_platforms():
    """CI 工作流是**源码仓库**关注点：发布包不含 `.github`，副本里显式跳过；仓库里必须存在。"""
    if not WORKFLOW.is_file():
        in_repo = (REPO_ROOT / ".git").exists()
        assert not in_repo, f"源码仓库里缺少 CI 工作流：{WORKFLOW}（只有发布包副本才允许没有）"
        pytest.skip("发布包副本不含 CI 工作流（.github 不入包），跳过")
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "windows-latest" in text and "ubuntu-latest" in text
    for version in ("3.10", "3.11", "3.12"):
        assert version in text, f"工作流缺少 Python {version}"
    assert "run_all_checks.py" in text, "工作流必须跑同一个总门入口"


def test_gate_skips_git_check_outside_a_git_worktree():
    """发布包解压副本没有 .git：git diff --check 必须优雅跳过而不是报失败。"""
    text = _gate_text()
    assert 'not (SKILL / ".git").exists()' in text
    assert "跳过该项" in text


def test_release_cli_refuses_dirty_tree_and_failed_gate():
    """发布命令行的前置：脏工作树与总门失败都必须被拦下，库函数不受影响。"""

    text = (REPO_ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8")
    assert "RELEASE_TREE_DIRTY" in text and "RELEASE_GATE_FAILED" in text
    assert "RELEASE_GATE_MISSING" in text
    assert "--skip-gate" in text, "试打包需要显式跳过开关（不能默默跳过）"
    assert "raise ReleasePreflightError" in text
    help_proc = run_py(
            [str(REPO_ROOT / "scripts" / "build_release.py"), "--help"],
            cwd=str(REPO_ROOT),
    )
    assert help_proc.returncode == 0
    assert "--skip-gate" in help_proc.stdout
