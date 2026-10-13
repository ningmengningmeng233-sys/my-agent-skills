"""Task 7 guard：修复规范冲突并建立文档一致性测试。

覆盖两层：
1. 工单 Step 1 四个冻结样例逐字保留：运行时文档不得声称"每集固定六组"/"固定六个生成"，
   不得有"10—15s 黄金"式活跃规则、不得有"时长不足时按比例压缩"，`SKILL.md` 必须含
   "片段默认4—10秒"、"多片段"与 `references/duration-ledger.md` 直达路由；
2. 文档统一到配置口径：全部运行时文档不得再有死指针"SKILL.md §9b"；`duration-ledger.md`
   必须让位 `config/duration-policy.json` 单一事实源并区分"模型最大能力 15 秒"与
   "本技能稳定默认上限 10 秒"；`review-checklist.md`/`dialogue-audio.md`/`camera-editing.md`
   的时长口径跟随 4—10 秒；`SKILL.md` 明确六段式是单条提示词骨架、组数动态、
   无对白保留声音签名段、普通任务不加载全量 references。

运行时文档语料：`SKILL.md` + `AGENTS.md` + `references/*.md` 顶层文件
（`references/` 子目录是 craft 兼容卡 / legacy / examples 归档，不进入普通路由，也不进语料）。

运行（目录名不是合法 Python 标识符，禁止 `-m unittest` 模块路径）：

    python "minimax-h3-craft/_审查测试/test_duration_docs_consistency.py" -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]

RUNTIME_DOC_NAMES = (
    ["SKILL.md", "AGENTS.md"]
    + sorted(f"references/{p.name}" for p in (SKILL_ROOT / "references").glob("*.md"))
)


def read(name: str) -> str:
    return (SKILL_ROOT / name).read_text("utf-8")


def read_runtime_docs() -> str:
    return "\n".join(f"===== {name} =====\n{read(name)}" for name in RUNTIME_DOC_NAMES)


class WorkOrderSpecTest(unittest.TestCase):
    """工单 Task 7 Step 1 的四个冻结样例。"""

    def test_no_fixed_six_group_claim(self):
        corpus = read_runtime_docs()
        self.assertNotIn("每集固定六组", corpus)
        self.assertNotIn("固定六个生成", corpus)

    def test_runtime_docs_use_policy_range(self):
        corpus = read_runtime_docs()
        self.assertIsNone(
            re.search(r"10(?:\.0)?s\s*[~—-]\s*15(?:\.0)?s.*黄金", corpus),
            "运行时文档不得保留 10—15s 黄金区间式活跃规则",
        )
        self.assertNotIn("时长不足时按比例压缩", corpus)
        self.assertIn("片段默认4—10秒", read("SKILL.md"))

    def test_storyboard_has_no_dead_skill_section(self):
        self.assertNotIn("SKILL.md §9b", read("references/storyboard-input-template.md"))

    def test_duration_reference_is_directly_routed(self):
        skill = read("SKILL.md")
        self.assertIn("多片段", skill)
        self.assertIn("references/duration-ledger.md", skill)


class CorpusConsistencyTest(unittest.TestCase):
    """全部运行时文档统一到配置口径（冻结样例之外的强化层）。"""

    def test_no_dead_skill_section_pointer_anywhere(self):
        corpus = read_runtime_docs()
        for name in RUNTIME_DOC_NAMES:
            self.assertNotIn("SKILL.md §9b", read(name), f"{name} 仍指向已删除的 SKILL.md §9b")

    def test_duration_ledger_defers_to_policy_config(self):
        ledger = read("references/duration-ledger.md")
        self.assertIn("config/duration-policy.json", ledger)
        self.assertIn("4—10", ledger)
        self.assertIn("15", ledger, "模型最大能力 15 秒作为能力事实应保留")
        self.assertIn("默认上限10秒", ledger.replace(" ", ""))

    def test_review_checklist_uses_policy_range(self):
        checklist = read("references/review-checklist.md")
        self.assertIn("4—10", checklist)
        self.assertNotIn("总时长 ≤15s 上限", checklist)

    def test_dialogue_budget_and_camera_budget_follow_policy(self):
        dialogue = read("references/dialogue-audio.md")
        self.assertIn("4—10", dialogue)
        self.assertIn("能力上限", dialogue)
        camera = read("references/camera-editing.md")
        self.assertIn("4—10", camera)

    def test_skill_states_dynamic_groups_and_single_prompt_scope(self):
        skill = read("SKILL.md")
        self.assertIn("组数", skill)
        self.assertIn("单条提示词", skill)
        self.assertIn("本段无对白", skill)
        self.assertIn("不加载全量", skill.replace("普通任务不加载全量 references", "不加载全量"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
