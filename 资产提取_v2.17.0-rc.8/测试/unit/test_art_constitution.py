#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 5：项目美术总则 + 确定性规则优先级 + 风格冲突检测。

覆盖：
- 规则优先级顺序被钉死（用户明确要求 > Omni > 原文事实 > 美术总则 > 主风格包 > 类型模板 > 必要补全）；
- 美术总则九要素齐备、内容指纹稳定且随字段变化；
- 冲突检测逐轴命中相斥词、给出冲突双方与规则来源；
- 人物/服装基础资产固定纯白背景，风格包的灰底/摄影棚底不得覆盖。
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.art_constitution import (  # noqa: E402
    CONFLICT_AXES,
    RULE_PRECEDENCE,
    ArtConflictError,
    ArtConstitution,
    check_prompt_conflicts,
    detect_conflicts,
    detect_conflicts_by_source,
    enforce_pure_white_background,
    requires_pure_white,
)


# --- 规则优先级 ---------------------------------------------------------------

def test_rule_precedence_is_frozen_and_ordered():
    assert RULE_PRECEDENCE == (
        "用户明确要求", "Omni标准", "原文事实", "项目美术总则",
        "唯一主风格包", "类型模板", "必要补全",
    )


def test_precedence_index_orders_constitution_above_stylepack():
    from scripts.engine.art_constitution import precedence_ranks

    ranks = precedence_ranks()
    assert ranks["项目美术总则"] < ranks["唯一主风格包"] < ranks["必要补全"]
    assert ranks["用户明确要求"] == 0


# --- 美术总则内容与指纹 -------------------------------------------------------

def _constitution():
    return ArtConstitution(
        medium="写实三维渲染",
        realism="国漫三维",
        proportions="写实头身比",
        materials="物理渲染材质",
        palette="玉清色调",
        lighting="体积光",
        era="仙侠古代",
        scene_aesthetic="空灵飘渺",
        costume_aesthetic="完整合体整洁",
        forbidden=("无现代元素", "无科技感"),
    )


def test_constitution_carries_nine_aesthetic_fields():
    c = _constitution()
    for field in ("medium", "realism", "proportions", "materials", "palette",
                  "lighting", "era", "scene_aesthetic", "costume_aesthetic"):
        assert getattr(c, field), field
    assert c.forbidden


def test_constitution_fingerprint_is_stable_and_field_sensitive():
    c = _constitution()
    assert c.content_fingerprint() == _constitution().content_fingerprint()
    changed = ArtConstitution(**{**c.to_dict(), "palette": "暗黑色调"})
    assert changed.content_fingerprint() != c.content_fingerprint()


# --- 冲突检测 -----------------------------------------------------------------

def test_conflict_axes_cover_required_pairs():
    axes = {name for name, _, _ in CONFLICT_AXES}
    assert {"写实/卡通", "高饱和/低饱和", "华丽/朴素", "干净/破旧",
            "低模/高精度", "纯白背景/环境背景"} <= axes


def test_realistic_and_cartoon_in_one_request_is_a_conflict():
    conflicts = detect_conflicts("写实三维渲染，物理渲染材质，另加卡通二次元贴纸")
    axes = {c.axis for c in conflicts}
    assert "写实/卡通" in axes
    hit = next(c for c in conflicts if c.axis == "写实/卡通")
    assert hit.a_term in ("写实",) and hit.b_term in ("卡通", "二次元")


@pytest.mark.parametrize("text,axis", [
    ("高精度模型配合低模面片", "低模/高精度"),
    ("整洁崭新的衣袍带着破旧补丁", "干净/破旧"),
    ("高饱和浓艳与低饱和灰暗并存", "高饱和/低饱和"),
    ("华丽繁复的极简朴素大厅", "华丽/朴素"),
])
def test_each_axis_fires_on_its_own_opposites(text, axis):
    assert axis in {c.axis for c in detect_conflicts(text)}


def test_clean_prompt_has_no_conflict():
    assert detect_conflicts("写实三维渲染，物理渲染材质，玉清色调，体积光") == []


def test_conflict_reports_rule_source_for_both_sides():
    segments = {
        "唯一主风格包": "写实三维渲染，物理渲染材质",
        "类型模板": "加入卡通化夸张表情",
    }
    conflicts = detect_conflicts_by_source(segments)
    assert conflicts
    c = next(x for x in conflicts if x.axis == "写实/卡通")
    assert {c.a_source, c.b_source} == {"唯一主风格包", "类型模板"}


def test_check_prompt_conflicts_raises_with_both_sides():
    prompt = "【风格】\n写实三维渲染\n\n【构图】\n卡通二次元贴纸\n"
    with pytest.raises(ArtConflictError) as ctx:
        check_prompt_conflicts(prompt)
    msg = str(ctx.value)
    assert "写实" in msg and "卡通" in msg


# --- 纯白背景固定（人物/服装基础资产不可被覆盖） --------------------------------

def test_base_identity_kinds_require_pure_white():
    assert requires_pure_white("正脸")
    assert requires_pure_white("服装独立正面")
    assert requires_pure_white("人物全身正面")


def test_scene_and_prop_do_not_require_pure_white():
    assert not requires_pure_white("场景母版")
    assert not requires_pure_white("道具")


def test_pure_white_overrides_gray_and_studio_background():
    style_block = "环境光照：素灰背景 #B8B8B8\n布景：摄影棚背景，柔光箱\n介质：写实三维渲染"
    out = enforce_pure_white_background(style_block, "正脸")
    assert "纯白" in out and "#FFFFFF" in out
    assert "摄影棚背景" not in out
    assert "#B8B8B8" not in out
    # 非背景指令必须原样保留，不得顺手删掉
    assert "写实三维渲染" in out


def test_enforce_is_noop_for_non_base_kind():
    block = "素灰背景 #B8B8B8，环境氛围"
    assert enforce_pure_white_background(block, "场景母版") == block


# --- 按秩覆盖：同槽位上游独占，下游只填未设定槽位 -------------------------------

def test_resolve_by_precedence_upstream_value_blocks_downstream():
    from scripts.engine.art_constitution import resolve_by_precedence

    slots = [("项目美术总则", {"背景": "空灵飘渺实景"}),
             ("Omni标准", {"背景": "纯白 #FFFFFF"})]
    resolved, rulings = resolve_by_precedence(slots)
    assert resolved["背景"] == "纯白 #FFFFFF"
    assert any("背景" in r and "项目美术总则" in r for r in rulings), rulings


def test_resolve_by_precedence_fills_unset_slots_from_lower_layers():
    from scripts.engine.art_constitution import resolve_by_precedence

    slots = [("Omni标准", {"背景": "纯白 #FFFFFF"}),
             ("项目美术总则", {"光照": "体积光"})]
    resolved, rulings = resolve_by_precedence(slots)
    assert resolved == {"背景": "纯白 #FFFFFF", "光照": "体积光"}
    assert rulings == []


def test_resolve_by_precedence_ignores_blank_values():
    from scripts.engine.art_constitution import resolve_by_precedence

    slots = [("Omni标准", {"背景": "   "}), ("项目美术总则", {"背景": "空灵飘渺"})]
    resolved, _ = resolve_by_precedence(slots)
    assert resolved["背景"] == "空灵飘渺"


# --- 美术总则载体：落盘与载入（工单 5「有规则无载体」收口） ---------------------

def test_load_constitution_returns_default_when_file_absent(tmp_path):
    from scripts.engine.art_constitution import load_constitution

    c = load_constitution(tmp_path)
    assert c == ArtConstitution()
    assert c.content_fingerprint() == ArtConstitution().content_fingerprint()


def test_save_then_load_round_trips_the_constitution(tmp_path):
    from scripts.engine.art_constitution import load_constitution, save_constitution

    path = save_constitution(tmp_path, _constitution())
    assert Path(path) == tmp_path / ".process" / "项目美术总则.json"
    assert load_constitution(tmp_path) == _constitution()


def test_load_constitution_rejects_corrupted_file(tmp_path):
    from scripts.engine.art_constitution import ArtConstitutionInvalid, load_constitution

    proc = tmp_path / ".process"
    proc.mkdir()
    (proc / "项目美术总则.json").write_text("{不是合法 JSON", encoding="utf-8")
    with pytest.raises(ArtConstitutionInvalid):
        load_constitution(tmp_path)


def test_load_constitution_rejects_fingerprint_mismatch(tmp_path):
    from scripts.engine.art_constitution import (
        ArtConstitutionInvalid,
        load_constitution,
        save_constitution,
    )

    save_constitution(tmp_path, _constitution())
    path = tmp_path / ".process" / "项目美术总则.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["palette"] = "被手改过的色调"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ArtConstitutionInvalid):
        load_constitution(tmp_path)
