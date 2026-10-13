# -*- coding: utf-8 -*-
import json
import pytest

from dramacraft.premise_to_outline import convert_premises_to_outline
from dramacraft.outline_lint import lint_outline


def _create_sample_premises():
    p1 = {
        "ep": 1,
        "title": "银针断生死",
        "cards_used": ["card_001", "card_002"],
        "four": {
            "desire": {"summary": "救活首富拿到救命钱", "irreversible_consequence": "恩人死亡", "decided_by": "user_confirmed"},
            "obstacle": {"summary": "富二代阻拦搜身", "decided_by": "user_confirmed"},
            "twist": {"summary": "用铁丝施展绝技破局", "decided_by": "user_confirmed"},
            "cost": {"summary": "暴露行踪引来仇敌", "decided_by": "user_confirmed"}
        },
        "cast": [
            {"id": "C01", "name": "陆凡", "role": "主角", "voiceStyle": "冷静低沉", "desire": "救人", "obstacle": "身份卑微"},
            {"id": "C02", "name": "赵天成", "role": "反派", "voiceStyle": "嚣张狂妄", "desire": "夺权", "obstacle": "主角插手"}
        ],
        "hook": "一把飞刀擦着主角耳廓没入石柱，火花飞溅。",
        "cliff": "主角推开寿礼红木箱，里面赫然是滴血的战神令，黑屏卡点。",
        "target_seconds": 90,
        "tone": "快节奏打脸",
        "ending_type": "反转悬念",
        "custom_anchors": [],
        "verdict": "pass"
    }

    p2 = {
        "ep": 2,
        "title": "假面名媛",
        "cards_used": ["card_003"],
        "four": {
            "desire": {"summary": "在拍卖会夺回母亲遗物", "irreversible_consequence": "遗物被毁家产不保", "decided_by": "user_confirmed"},
            "obstacle": {"summary": "假千金恶意抬价十倍", "decided_by": "user_confirmed"},
            "twist": {"summary": "主角拿出底价合同证明古董是赝品", "decided_by": "user_confirmed"},
            "cost": {"summary": "背负巨额违约金", "decided_by": "user_confirmed"}
        },
        "cast": [
            {"id": "C03", "name": "苏晴", "role": "主角", "voiceStyle": "优雅清冷", "desire": "寻仇", "obstacle": "假名媛欺压"},
            {"id": "C04", "name": "林薇薇", "role": "反派", "voiceStyle": "尖酸刻薄", "desire": "抢夺古董", "obstacle": "资金不足"}
        ],
        "hook": "聚光灯突然爆裂，玻璃碎屑如雨下，女主角稳稳托住水晶杯。",
        "cliff": "展台底座暗格弹开，露出空空如也的防盗盒，现场警报大作。",
        "target_seconds": 90,
        "tone": "悬疑紧绷",
        "ending_type": "绝境悬空",
        "custom_anchors": [],
        "verdict": "pass"
    }
    return [p1, p2]


def test_convert_premises_to_outline_passes_o1_to_o6():
    premises = _create_sample_premises()
    outline = convert_premises_to_outline(premises, title="都市奇案录")

    assert outline["title"] == "都市奇案录"
    assert outline["mode"] == "anthology"
    assert len(outline["episodes"]) == 2

    # 验证人物表已聚合且具备 name, desire, obstacle, voiceStyle 四字段
    assert len(outline["characters"]) >= 4
    for c in outline["characters"]:
        for f in ("id", "name", "desire", "obstacle", "voiceStyle"):
            assert str(c.get(f, "")).strip() != "", f"角色 {c} 缺失字段 {f}"

    # 验证生成的 outline 100% 自动通过 O1~O6 大纲预审门
    violations = lint_outline(outline)
    assert len(violations) == 0, f"转换后的大纲未能通过 O1~O6 预审门:\n" + "\n".join(violations)
