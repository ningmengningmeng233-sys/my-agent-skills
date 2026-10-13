# -*- coding: utf-8 -*-
import json
import os
import pytest

from dramacraft.premise_lint import lint_premise
from dramacraft.card_ledger import CardLedger


def _create_sample_premise():
    return {
        "ep": 1,
        "title": "战神外卖员的逆袭",
        "cards_used": ["card_001", "card_002"],
        "four": {
            "desire": {
                "summary": "主角要在10分钟内拿到续命银针",
                "irreversible_consequence": "若拿不到，救命恩人将脑死亡且无法追回家族遗产",
                "decided_by": "user_confirmed"
            },
            "obstacle": {
                "summary": "反派富二代将银针踩在脚下逼主角下跪认输",
                "decided_by": "user_confirmed"
            },
            "twist": {
                "summary": "主角反手用普通铁丝封住恩人穴位，现场拆穿假药",
                "decided_by": "user_confirmed"
            },
            "cost": {
                "summary": "主角暴露了封针绝技，引来昔日仇敌定位",
                "decided_by": "user_confirmed"
            }
        },
        "cast": [
            {"id": "C01", "name": "陆凡", "role": "主角", "source": "card_001"},
            {"id": "C02", "name": "赵天成", "role": "反派", "source": "card_002"}
        ],
        "hook": "镜头一亮，一把手术刀擦着主角耳边钉入墙面，火花四溅。",
        "cliff": "主角一把掀翻寿礼盒，里面滚出一颗血淋淋的机械义眼，黑屏出字幕。",
        "target_seconds": 90,
        "tone": "快节奏打脸",
        "ending_type": "反转悬念",
        "custom_anchors": [],
        "verdict": "pass"
    }


def test_premise_lint_perfect_sample(tmp_path):
    ledger = CardLedger(str(tmp_path / "cards.jsonl"))
    ledger.add_card("陆凡设定", card_type="character")
    ledger.add_card("赵天成设定", card_type="character")

    data = _create_sample_premise()
    violations = lint_premise(data, card_ledger=ledger)
    assert len(violations) == 0, f"Expected clean, got violations: {violations}"


def test_p1_four_missing(tmp_path):
    data = _create_sample_premise()
    del data["four"]["desire"]
    violations = lint_premise(data)
    assert any("[P1]" in v for v in violations)


def test_p2_irreversible_consequence_missing(tmp_path):
    data = _create_sample_premise()
    data["four"]["desire"]["irreversible_consequence"] = ""
    violations = lint_premise(data)
    assert any("[P2]" in v for v in violations)


def test_p3_cast_exceeds_limit(tmp_path):
    data = _create_sample_premise()
    data["cast"].extend([
        {"id": "C03", "name": "路人甲", "role": "配角"},
        {"id": "C04", "name": "路人乙", "role": "配角"},
        {"id": "C05", "name": "路人丙", "role": "配角"}
    ])
    violations = lint_premise(data)
    assert any("[P3]" in v and "4" in v for v in violations)


def test_p4_hook_literature_sickness(tmp_path):
    data = _create_sample_premise()
    data["hook"] = "主角若有所思地望着对方，神色复杂。"
    violations = lint_premise(data)
    assert any("[P4]" in v for v in violations)


def test_p5_target_seconds_invalid(tmp_path):
    data = _create_sample_premise()
    data["target_seconds"] = 120  # 非标准档
    violations = lint_premise(data)
    assert any("[P5]" in v for v in violations)


def test_p8_custom_anchors_missing_when_user_custom(tmp_path):
    data = _create_sample_premise()
    data["four"]["twist"]["decided_by"] = "user_custom"
    data["custom_anchors"] = []
    violations = lint_premise(data)
    assert any("[P8]" in v for v in violations)


def test_p9_verdict_not_pass(tmp_path):
    data = _create_sample_premise()
    data["verdict"] = "pending"
    violations = lint_premise(data)
    assert any("[P9]" in v for v in violations)
