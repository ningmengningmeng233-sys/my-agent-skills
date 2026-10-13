# -*- coding: utf-8 -*-
"""工单 4 红测：叙事重要度与生产方式分离，取消 P3 丢提示词。

产品硬要求：每一个正式 人物/场景/道具 都必须产出基础提示词（P3/单次出现不得再吞掉提示词）；
待定冲突产出「带阻断标识的草案」而非无声跳过；跳过清单只接受「非资产 / 用户明确取消」。
「核心人物」与「需要几张图」是两个独立字段：narrative_importance 表达剧情重要度，
production_mode 表达图片规格；只出现一次但推动剧情的道具可以是高叙事重要度。
"""
import pytest

import aggregate as agg
from engine import asset_ledger as al
from generate_prompts import should_generate_base


# ---------- 生产优先级：不再因「单次出现」推断 P3 ----------

def test_single_appearance_prop_is_not_demoted_for_count():
    # 等级缺省（普通 B）+ 只出现一次 + 无衍生需求：旧逻辑给 P3（丢提示词），新逻辑给 P2
    assert agg._infer_production_priority("道具", "B", 1, False) == "P2"


def test_explicit_background_still_p3_by_narrative_level():
    # 「一次性/背景」是显式叙事信号，仍可 P3，但 P3 不再等于无提示词
    assert agg._infer_production_priority("场景", "C", 1, False) == "P3"


def test_core_character_is_p0():
    assert agg._infer_production_priority("人物", "S", 5, True) == "P0"


# ---------- 两个独立字段：narrative_importance ≠ production_mode ----------

def test_narrative_importance_and_mode_are_separate_fields():
    imp = agg.infer_narrative_importance("S")
    mode = agg.infer_production_mode("道具", bound=False, formal=True)
    assert imp == "main"
    assert mode == "image"


def test_single_but_plot_driving_prop_can_be_high_importance():
    # 只出现一次的关键道具：叙事重要度可高，但生产方式仍是出图；二者解耦
    imp = agg.infer_narrative_importance("关键道具")
    mode = agg.infer_production_mode("道具", bound=False, formal=True)
    assert imp in ("main", "supporting")
    assert mode == "image"


def test_bound_costume_mode_is_bound():
    assert agg.infer_production_mode("服装", bound=True, formal=True) == "bound"


# ---------- 基础提示词：正式资产永不因优先级缺失 ----------

def test_p3_prop_still_gets_base_prompt():
    ok, reason = should_generate_base({"priority": "P3", "fact_level": "剧本事实", "type": "道具"})
    assert ok is True, "P3 不再吞掉基础提示词"
    assert reason == ""


def test_pending_conflict_produces_blocked_draft_not_silent_skip():
    ok, note = should_generate_base({"priority": "P2", "fact_level": "待定冲突", "type": "人物"})
    assert ok is True
    assert "阻断" in note  # 带阻断标识的草案


def test_only_non_asset_or_user_cancel_excludes():
    ok, reason = should_generate_base({"exclude_reason": "非资产：天气", "type": "道具"})
    assert ok is False
    assert "非资产" in reason
    ok2, _ = should_generate_base({"exclude_reason": "用户取消", "type": "场景"})
    assert ok2 is False


def test_formal_always_required():
    ok, _ = should_generate_base({"priority": "P0", "fact_level": "剧本事实", "type": "场景"})
    assert ok is True


# ---------- 后期元素：只进清单，绝不进生图/道具提示词 ----------

def test_post_elements_render_into_manifest():
    md = agg.render_post_elements_md([{"name": "片头", "quote": "片头字幕"}, {"name": "字幕", "quote": "台词字幕"}])
    assert "后期元素清单" in md
    assert "片头" in md and "字幕" in md
    assert "不是道具" in md


def test_post_element_never_requires_base_prompt():
    ledger = al.build_ledger({"assets": []}, post_elements=[{"name": "片尾", "quote": "片尾"}])
    assert len(ledger) == 1
    entry = ledger[0]
    assert entry["asset_type"] == "post_element"
    assert entry["production_mode"] == "post_only"
    assert "base_prompt" not in entry["required_outputs"]
    assert "image_task" not in entry["required_outputs"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
