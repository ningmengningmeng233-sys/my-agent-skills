# -*- coding: utf-8 -*-
"""W05 H3 分镜输入直读与对账单测。"""
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
import sys
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from storyboard_reader import (
    parse_shot_table_markdown,
    reconcile_shot_table,
)


SAMPLE_TABLE_MD = """| 镜头号 | 时长 | 景别·角度 | 画面内容 | 对白/台词 | 声音 |
|---|---|---|---|---|---|
| E01-01-f1 (SC001-SH001) | 3s | 平视·中景 | medium shot of a young woman standing at the edge of the pier | （无） | （无） |
| E01-01-f2 (SC001-SH002) | 3s | 平视·大远景 | extreme wide shot of a fog-drowned pier | 老周："上船喽——" | 水声、橹声 |
"""


def test_parse_shot_table_markdown():
    """测试直接读取六列分镜表并提取各字段。"""
    shots = parse_shot_table_markdown(SAMPLE_TABLE_MD)
    assert len(shots) == 2
    s1, s2 = shots[0], shots[1]

    assert "SC001-SH001" in s1["shot_id"]
    assert s1["seconds"] == 3.0
    assert "中景" in s1["angle"]
    assert s1["dialogue"] == "（无）"

    assert "SC001-SH002" in s2["shot_id"]
    assert s2["seconds"] == 3.0
    assert "大远景" in s2["angle"]
    assert '老周："上船喽——"' in s2["dialogue"]
    assert "水声" in s2["soundscape"]


def test_reconcile_shot_table_matches_prompt():
    """测试分镜表与 H3 提示词对白/秒数的确定性对账。"""
    shots = parse_shot_table_markdown(SAMPLE_TABLE_MD)
    h3_prompt = """
[Shot 1]
medium shot of a young woman standing at the edge of the pier

[Shot 2] At 00:03.000,
extreme wide shot of a fog-drowned pier
<d>[Chinese] 老周："上船喽——" </d>

overall_soundscape: 水声、橹声
"""
    result = reconcile_shot_table(shots, h3_prompt)
    assert result["ok"] is True
    assert result["matched_cuts"] == 2
    assert len(result["mismatches"]) == 0


def test_reconcile_shot_table_detects_dialogue_mismatch():
    """反例：提示词台词被篡改或漏词时被拦下。"""
    shots = parse_shot_table_markdown(SAMPLE_TABLE_MD)
    bad_prompt = """
[Shot 1]
medium shot...

[Shot 2] At 00:03.000,
extreme wide shot...
<d>[Chinese] 老周："走了走了" </d>
"""
    result = reconcile_shot_table(shots, bad_prompt)
    assert result["ok"] is False
    assert any("台词不匹配" in m or "dialogue" in m for m in result["mismatches"])
