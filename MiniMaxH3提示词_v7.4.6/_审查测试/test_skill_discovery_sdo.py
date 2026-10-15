"""SDO 发现性微测（writing-skills 第二轮迭代 · RED→GREEN→REFACTOR）。

对 SKILL.md frontmatter `description` 断言四条，判据全部来自
superpowers:writing-skills「Skill Discovery Optimization (SDO)」段：

a) 以触发条件开头（「当用户…」式），不第一人称、不抽象口号；
b) 长度 <= 500 字符（中文按字符计，spec 原文 "Keep under 500 characters if possible"）；
c) 至少 5 个具体触发词/触发症状——只数可枚举实体：「『…』」内用户原话、
   slash 命令名、错误消息式全大写 token（如 NOT_RUN），抽象名词不算；
d) 不复述工作流：不得出现「流程为/步骤为/依次执行」句式；且不得出现
   以顿号「、」串联的 >=4 段连续阶段执行词枚举。SDO 真实案例：描述里写
   「每个任务派一个子代理并做一次 code review」，代理就只做一次 review，
   而正文要求两次——描述写流程会让代理照描述干活而跳过正文。

失败消息一律打印实测值。做成 unittest 形态以同时兼容 run_all.py 总门禁
（其契约要求子套件打印 `Ran N tests`）与 pytest 收集。运行：

    python "minimax-h3-craft/_审查测试/test_skill_discovery_sdo.py" -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]

DESC_MAX_CHARS = 500
TRIGGER_MIN = 5
CHAIN_RUN_MIN = 4  # 「、」串联的连续含阶段执行词段数达到即判复述工作流

FORBIDDEN_WORKFLOW_PHRASES = ("流程为", "步骤为", "依次执行", "按序执行", "随后执行")
STAGE_TOKENS = (
    "路由", "阻断", "审计", "凭证", "拆解", "推进", "终验", "工单",
    "红绿", "架构", "脚手架", "需求对齐", "六段式", "对齐拷问",
)


def read_description() -> str:
    text = (SKILL_ROOT / "SKILL.md").read_text("utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise AssertionError("SKILL.md 缺少 YAML frontmatter")
    for line in parts[1].splitlines():
        stripped = line.strip()
        if stripped.startswith("description:"):
            value = stripped[len("description:"):].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            return value
    raise AssertionError("frontmatter 缺少 description 字段")


def quoted_items(desc: str) -> list:
    return re.findall(r"[「『]([^」』]+)[」』]", desc)


def slash_commands_outside_quotes(desc: str) -> list:
    quoted = " ".join(quoted_items(desc))
    found = re.findall(r"(?<![\w/\-])/([A-Za-z一-鿿][\w一-鿿\-]*)", desc)
    return [s for s in found if s not in quoted]


def error_tokens_outside_quotes(desc: str) -> list:
    quoted = " ".join(quoted_items(desc))
    found = re.findall(r"\b[A-Z][A-Z][A-Z_]{2,}\b", desc)
    return [t for t in found if t not in quoted]


def longest_stage_chain(desc: str) -> list:
    """返回最长的「、」串联连续段（每段至少含一个阶段执行词），不足 CHAIN_RUN_MIN 段返回 []。"""
    best: list = []
    current: list = []
    for seg in desc.split("、"):
        if any(tok in seg for tok in STAGE_TOKENS):
            current.append(seg)
            if len(current) > len(best):
                best = list(current)
        else:
            current = []
    return best if len(best) >= CHAIN_RUN_MIN else []


class SkillDiscoverySDOTest(unittest.TestCase):
    def setUp(self):
        self.desc = read_description()

    def test_a_description_starts_with_trigger_condition(self):
        head = self.desc[:80]
        self.assertTrue(
            re.match(r"^当(用户|需要|你|要为|有)", self.desc.lstrip()),
            f"判据a 失败：description 未以「当…」触发条件开头。实测开头 80 字符：{head!r}",
        )
        self.assertFalse(
            re.search(r"^(我|我会|本技能可以帮你)", self.desc.lstrip()),
            f"判据a 失败：description 疑似第一人称。实测开头 40 字符：{self.desc[:40]!r}",
        )

    def test_b_description_within_500_chars(self):
        self.assertLessEqual(
            len(self.desc), DESC_MAX_CHARS,
            f"判据b 失败：description 实测 {len(self.desc)} 字符，超过 {DESC_MAX_CHARS} 上限。",
        )

    def test_c_description_has_specific_triggers(self):
        quoted = quoted_items(self.desc)
        slash = slash_commands_outside_quotes(self.desc)
        errors = error_tokens_outside_quotes(self.desc)
        total = len(quoted) + len(slash) + len(errors)
        self.assertGreaterEqual(
            total, TRIGGER_MIN,
            (
                f"判据c 失败：具体触发词实测 {total} 个 < {TRIGGER_MIN}。"
                f"「」内原话={quoted}；引号外 slash 命令={slash}；错误消息式 token={errors}"
            ),
        )

    def test_d_description_does_not_restate_workflow(self):
        hit = [p for p in FORBIDDEN_WORKFLOW_PHRASES if p in self.desc]
        self.assertFalse(
            hit,
            f"判据d 失败：description 出现流程句式 {hit}。实测全文：{self.desc!r}",
        )
        chain = longest_stage_chain(self.desc)
        self.assertFalse(
            chain,
            (
                f"判据d 失败：description 以「、」串联 {len(chain)} 段阶段执行词"
                f"（复述工作流，代理会照描述干活而跳过正文）。命中段：{chain}"
            ),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
