# -*- coding: utf-8 -*-
"""T5 五张规范卡（工单 T5；纯文档票，机检结构）。

卡头固定三行 = 原件路径 + 行号区 + 摘录方式；条款「只删不改」（保留原文序号）；
卡尾附完成标准清单；违禁词表/抽象心理词表**只引代码常量名，不复制第二份表**。
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARDS_DIR = ROOT / "skills_runtime" / "dramacraft_patch" / "spec_cards"
CARDS = sorted(CARDS_DIR.glob("*.md"))

DIRECTOR = CARDS_DIR / "导演规划规范卡.md"
SHOTS = CARDS_DIR / "镜头组规范卡.md"
SUP_FILM = CARDS_DIR / "监督规范卡·成片层.md"
SUP_OUTLINE = CARDS_DIR / "监督规范卡·大纲层.md"
MEETING = CARDS_DIR / "会议机制卡.md"


def _read(path):
    return path.read_text(encoding="utf-8")


def test_five_cards_with_provenance_header():
    assert len(CARDS) == 5, [c.name for c in CARDS]
    for card in CARDS:
        head = _read(card).splitlines()
        assert head[0].startswith("源文件："), card.name
        assert "行号：" in head[1], card.name
        assert any(ch.isdigit() for ch in head[1]), card.name


def test_every_card_ends_with_completion_checklist():
    for card in CARDS:
        text = _read(card)
        assert "完成标准清单" in text, card.name
        tail = text.split("完成标准清单")[-1]
        assert "- [ ]" in tail, card.name


def test_director_card_keeps_transition_exception():
    t = _read(DIRECTOR)
    assert "场间过渡" in t and "唯一" in t and t.count("光影") >= 1
    # 违禁词表只引常量名，不复制第二份表
    assert "FORBIDDEN_LEXICON" in t
    assert "FORBIDDEN_LEXICON = (" not in t


def test_shot_card_refers_to_psy_lexicon_constant():
    t = _read(SHOTS)
    assert "ABSTRACT_PSY_LEXICON" in t
    assert "ABSTRACT_PSY_LEXICON = (" not in t
    for gate in ("SG1", "SG2", "SG3", "SG4", "SG5", "SG6", "SG7"):
        assert gate in t, gate
    assert "00:00-00:25" in t, "绝对时间段禁令要照原文带上"


def test_supervision_cards_split_film_vs_outline():
    film, outline = _read(SUP_FILM), _read(SUP_OUTLINE)
    assert "只提出问题和建议" in film or "只提问不决策" in film
    for red in ("R1", "R2", "R3", "R4"):
        assert red in film, red
    assert "不用于逐集终审" in outline
    assert "骨架质量红线" in outline and "改编策略质量红线" in outline


def test_meeting_card_marks_creative_merge_and_minutes_fields():
    t = _read(MEETING)
    assert "已归并" in t
    for field in ("核心结论", "关键决策", "被放弃的方向", "待人类确认的问题", "下一步"):
        assert field in t, field
    assert "五个字段" in t, "纪要字段数以原件为准（五个），不许写成别的数"
    assert "心理契约" in t and "超时行为" in t


def test_cards_are_wired_from_skill_and_workflow_docs():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    workflow = (ROOT / "references" / "WORKFLOW.md").read_text(encoding="utf-8")
    assert "spec_cards" in skill and "规范卡" in skill
    assert "第十五节" in workflow or "十五、产线工作流" in workflow
    assert "规范卡" in workflow
