"""Task 8 guard：收紧真实上下文加载路径——任务类型 → 初始最小参考集的确定性路由。

覆盖四层：
1. 工单 Step 1 冻结路由表逐字保留（六类任务的初始参考集必须精确匹配）；
2. 收紧不变量：普通任务初始参考 ≤3 份；`CHANGELOG.md`、`references/legacy`、
   `references/examples`、`文档/柠檬` 不得进入普通路由；路由目标文件必须真实存在；
3. 长期规则驻留主入口：一次不加载整个 references、长命令写日志、阶段结束更新执行摘要、
   上下文 70%—80% 建立检查点、子代理只收最小上下文；
4. `templates/execution-summary.md` 固定字段齐全（目标/规格/工单/提交/已完成阶段/
   修改文件/验证结果/冻结决定/当前问题/下一条精确命令/兼容项）。

路由表来源：`SKILL.md` §3 的 markdown 表（任务类型行以 ASCII 标识开头，
初始参考集以「、」分隔）。

运行（目录名不是合法 Python 标识符，禁止 `-m unittest` 模块路径）：

    python "minimax-h3-craft/_审查测试/test_reference_loading_routes.py" -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {
    "single-native-drama": {"references/native/drama-series-h3.md"},
    "multi-segment-drama": {"references/native/drama-series-h3.md", "references/duration-ledger.md"},
    "continuation": {"references/continuation-bridging.md", "references/blocking-memory.md"},
    "dialogue": {"references/dialogue-audio.md"},
    "high-risk-action": {"references/action-director.md", "references/high-risk-shots.md"},
    "review": {"references/review-checklist.md", "references/failure-guards.md"},
}

FORBIDDEN_IN_ROUTES = (
    "CHANGELOG.md",
    "references/legacy",
    "references/examples",
    "文档/柠檬",
)

EXECUTION_SUMMARY_FIELDS = (
    "目标",
    "规格",
    "工单",
    "提交",
    "已完成阶段",
    "修改文件",
    "验证结果",
    "冻结决定",
    "当前问题",
    "下一条精确命令",
    "兼容项",
)


def read_skill() -> str:
    return (SKILL_ROOT / "SKILL.md").read_text("utf-8")


def parse_route_table() -> dict:
    """从 SKILL.md §3 解析路由表：ASCII 任务标识 → 初始参考集。"""
    routes: dict = {}
    section = re.split(r"^## 3\. ", read_skill(), maxsplit=1, flags=re.MULTILINE)[1]
    section = section.split("\n## ")[0]
    for line in section.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= {"-", " ", ":"}:
            continue
        match = re.match(r"([a-z0-9-]+)", cells[0])
        if not match:
            continue
        task_type = match.group(1)
        refs = {ref.strip().strip("`") for ref in cells[1].split("、") if ref.strip()}
        routes[task_type] = refs
    return routes


class WorkOrderRouteTableTest(unittest.TestCase):
    """工单 Task 8 Step 1 的冻结路由表。"""

    def test_expected_routes_are_present_and_exact(self):
        routes = parse_route_table()
        for task_type, expected_refs in EXPECTED.items():
            self.assertIn(task_type, routes, f"路由表缺少任务类型 {task_type}")
            self.assertEqual(
                routes[task_type], expected_refs,
                f"{task_type} 的初始参考集与冻结契约不符",
            )

    def test_normal_tasks_load_at_most_three_initial_references(self):
        routes = parse_route_table()
        for task_type, refs in routes.items():
            self.assertLessEqual(
                len(refs), 3,
                f"{task_type} 初始参考 {len(refs)} 份，超过普通任务上限 3",
            )


class TighteningInvariantsTest(unittest.TestCase):
    """收紧不变量：禁入清单与路由目标存在性。"""

    def test_forbidden_paths_are_not_in_the_route_table(self):
        routes = parse_route_table()
        table_text = repr(routes)
        for forbidden in FORBIDDEN_IN_ROUTES:
            self.assertNotIn(forbidden, table_text, f"{forbidden} 不得进入普通路由")

    def test_every_route_target_exists_on_disk(self):
        for refs in parse_route_table().values():
            for ref in refs:
                self.assertTrue(
                    (SKILL_ROOT / ref).is_file(), f"路由目标不存在：{ref}"
                )

    def test_long_term_rules_live_in_the_main_entry(self):
        skill = read_skill()
        self.assertIn("不加载整个", skill)
        self.assertIn(".logs", skill)
        self.assertIn("当前执行摘要", skill)
        self.assertIn("70%", skill)
        self.assertIn("最小上下文", skill)
        self.assertIn("references/craft/", skill)


class ExecutionSummaryTemplateTest(unittest.TestCase):
    """templates/execution-summary.md 固定字段。"""

    def test_template_exists_with_every_fixed_field(self):
        path = SKILL_ROOT / "templates" / "execution-summary.md"
        self.assertTrue(path.is_file(), "templates/execution-summary.md 必须存在")
        template = path.read_text("utf-8")
        for field in EXECUTION_SUMMARY_FIELDS:
            self.assertIn(field, template, f"执行摘要模板缺少固定字段：{field}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
