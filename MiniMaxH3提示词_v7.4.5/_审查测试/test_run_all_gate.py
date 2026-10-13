"""Task 9 guard：诚实的当前总门禁聚合器（`_审查测试/run_all.py`）。

覆盖五层：
1. 首行诚实声明：`HISTORICAL_FULL_SUITE=UNAVAILABLE`——不得把新套件冒充历史 316 项；
2. 子套件退出非零 ⇒ 总门禁非零；
3. 用例数从子套件实际输出动态求和（解析 `Ran N tests`），改输出即改总数，不得硬编码；
4. 无计数输出的子套件 fail-closed（数不出来就拒绝通过）；
5. 辅助检查组成：`reference_graph.py`、`validate_source_manifest.py`、`compileall`、
   上下文审计器必须全部在编。

测试全部使用注入的 fake runner / 临时目录，绝不递归调用真实总门禁。

运行（目录名不是合法 Python 标识符，禁止 `-m unittest` 模块路径）：

    python "minimax-h3-craft/_审查测试/test_run_all_gate.py" -v
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
RUN_ALL_PATH = SKILL_ROOT / "_审查测试" / "run_all.py"

_spec = importlib.util.spec_from_file_location("h3_run_all", str(RUN_ALL_PATH))
run_all = importlib.util.module_from_spec(_spec)
if RUN_ALL_PATH.exists():
    _spec.loader.exec_module(run_all)


def ok_output(count: int) -> str:
    return f"Ran {count} tests in 0.001s\n\nOK"


def fake_runner(results: dict):
    """按套件文件名返回 (rc, output) 的假执行器。"""

    def _run(cmd, cwd=None):
        name = Path(cmd[-1]).name if isinstance(cmd, list) else str(cmd)
        if name in results:
            return results[name]
        return 0, ok_output(0)

    return _run


class DiscoveryTest(unittest.TestCase):
    def test_discovers_only_test_files_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("test_b.py", "test_a.py", "run_all.py", "helper.py", "notes.md"):
                (root / name).write_text("pass\n", encoding="utf-8")
            discovered = run_all.discover_suites(root)
            self.assertEqual([p.name for p in discovered], ["test_a.py", "test_b.py"])


class AggregationTest(unittest.TestCase):
    def test_first_line_declares_historical_suite_unavailable(self):
        code, report = run_all.run_gate(
            suites=[("fake_ok.py", (0, ok_output(3)))],
            auxiliary=[],
        )
        self.assertEqual(report.splitlines()[0], run_all.HISTORICAL_FULL_SUITE_LINE)
        self.assertEqual(code, 0)

    def test_subsuite_failure_forces_gate_nonzero(self):
        code, report = run_all.run_gate(
            suites=[
                ("ok.py", (0, ok_output(3))),
                ("broken.py", (1, "Ran 4 tests\nFAILED (failures=1)")),
            ],
            auxiliary=[],
        )
        self.assertNotEqual(code, 0)
        self.assertIn("broken.py", report)

    def test_counts_are_summed_dynamically(self):
        _, report = run_all.run_gate(
            suites=[
                ("a.py", (0, ok_output(3))),
                ("b.py", (0, ok_output(4))),
            ],
            auxiliary=[],
        )
        self.assertIn("total=7", report)
        _, report2 = run_all.run_gate(
            suites=[
                ("a.py", (0, ok_output(5))),
                ("b.py", (0, ok_output(4))),
            ],
            auxiliary=[],
        )
        self.assertIn("total=9", report2)

    def test_missing_count_is_fail_closed(self):
        code, report = run_all.run_gate(
            suites=[("silent.py", (0, "seems fine but no count line"))],
            auxiliary=[],
        )
        self.assertNotEqual(code, 0)
        self.assertIn("silent.py", report)

    def test_auxiliary_failure_forces_gate_nonzero(self):
        code, report = run_all.run_gate(
            suites=[("a.py", (0, ok_output(2)))],
            auxiliary=[("reference_graph", (1, "FAIL: broken links"))],
        )
        self.assertNotEqual(code, 0)
        self.assertIn("reference_graph", report)


class CompositionTest(unittest.TestCase):
    def test_default_auxiliary_checks_are_all_enlisted(self):
        names = [name for name, _ in run_all.build_auxiliary_checks(SKILL_ROOT)]
        for required in (
            "reference_graph",
            "validate_source_manifest",
            "verification_protocol",
            "skill_context_audit",
            "compileall",
        ):
            self.assertIn(required, names, f"总门禁缺少辅助检查：{required}")

    def test_default_suite_discovery_covers_every_current_test_file(self):
        expected = {
            "test_skill_context_budget.py",
            "test_duration_policy.py",
            "test_script_duration.py",
            "test_duration_planner.py",
            "test_episode_contract.py",
            "test_render_episode_pack.py",
            "test_duration_docs_consistency.py",
            "test_reference_loading_routes.py",
        }
        discovered = {p.name for p in run_all.discover_suites(SKILL_ROOT / "_审查测试")}
        self.assertTrue(expected <= discovered, f"发现缺失：{expected - discovered}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
