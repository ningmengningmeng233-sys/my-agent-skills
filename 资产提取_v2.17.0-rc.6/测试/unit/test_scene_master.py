#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 7：场景母版空间模型、必要视图与轻量站位连续性。

覆盖：
- 空间槽位齐备且未知项一律「未说明」，不虚构；
- 禁改项由固定锚点与出入口派生；
- 必要视图只确立空间，缺关键方向才增补，永不生成侧视图；
- 场景审美由美术总则 + 场景功能 + 叙事氛围编译，无证据的空泛堆砌词被剔除；
- 漂移检查比较结构锚点集合，而不是关键词搜索；
- 叙事站位必须可追溯到剧本/资产索引/分镜提示词之一，否则待审。
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.scene_master import (  # noqa: E402
    BASE_SCENE_VIEW,
    BLOCKING_OK,
    BLOCKING_PENDING,
    BLOCKING_SOURCES,
    IMMUTABLE_SLOT,
    REVERSE_SCENE_VIEW,
    SPACE_SLOTS,
    UNKNOWN,
    blocking_status,
    compare_anchors,
    compile_scene_aesthetic,
    evidence_text,
    immutable_items,
    immutable_text,
    required_scene_views,
    scene_master_rows,
    space_slot_values,
    unsupported_vague_words,
)


def _fact(value, chapter="第1章", segment="段01.md", line="00010"):
    return {"value": value, "chapter": chapter, "segment": segment, "line": line}


def _scene(**space):
    return {
        "asset_id": "SC_茶楼",
        "name": "茶楼",
        "type": "场景",
        "story_function": "剧本驱动变体载体",
        "evidence": [{"quote": "林姑娘走进茶楼，抬头看见二楼栏杆"}],
        "space_facts": space,
    }


class _Constitution:
    scene_aesthetic = "空灵飘渺"
    palette = "玉清色调"
    lighting = "体积光"


# --- 空间槽位：齐备且未知写「未说明」 ------------------------------------------

def test_every_space_slot_is_present_and_unknown_is_explicit():
    slots = space_slot_values(_scene(anchor=[_fact("柜台")]))
    assert set(slots) == {label for label, _ in SPACE_SLOTS}
    assert slots["固定锚点"].startswith("柜台")
    for label in ("主要物件", "朝向", "时间"):
        assert slots[label] == UNKNOWN, label


def test_space_slots_cover_the_seven_required_dimensions():
    labels = {label for label, _ in SPACE_SLOTS}
    assert {"空间结构", "固定锚点", "入口出口", "主要物件", "朝向", "时间"} <= labels
    assert {"光源", "材质"} <= labels


# --- 禁改项由锚点与出入口派生 --------------------------------------------------

def test_immutable_items_derive_from_anchors_and_entrances():
    scene = _scene(anchor=[_fact("柜台、梁柱")], entry_exit=[_fact("西侧门")])
    assert immutable_items(scene) == ("柜台", "梁柱", "西侧门")
    assert "柜台" in immutable_text(scene)


def test_immutable_text_is_unknown_without_anchors():
    assert immutable_text(_scene()) == UNKNOWN


# --- 必要视图：只确立空间，缺方向才增补，永不侧视 -------------------------------

def test_base_scene_needs_only_the_master_view():
    assert required_scene_views(_scene()) == (BASE_SCENE_VIEW,)


def test_reverse_view_added_only_when_evidence_points_backwards():
    scene = _scene(anchor=[_fact("柜台")])
    scene["evidence"] = [{"quote": "二人自门口折返，回望二楼栏杆"}]
    assert required_scene_views(scene) == (BASE_SCENE_VIEW, REVERSE_SCENE_VIEW)


def test_required_views_never_include_side_views():
    scene = _scene(entry_exit=[_fact("西侧门")])
    scene["evidence"] = [{"quote": "侧面墙壁上挂着字画，他折返出门"}]
    for view in required_scene_views(scene):
        assert "侧面" not in view and "侧视" not in view


def test_base_scene_views_do_not_depend_on_storyboard():
    """基础资产视图只由场景自身的空间证据决定，签名里没有任何分镜输入。"""
    import inspect

    params = inspect.signature(required_scene_views).parameters
    assert list(params) == ["scene_asset"]


# --- 场景审美：总则 + 功能 + 氛围，无证据的堆砌词剔除 ---------------------------

def test_aesthetic_compiles_from_constitution_function_and_evidence():
    text, removed = compile_scene_aesthetic(_Constitution(), _scene(anchor=[_fact("柜台")]))
    assert "空灵飘渺" in text and "剧本驱动变体载体" in text
    assert removed == ()


def test_vague_words_without_evidence_are_removed_and_reported():
    scene = _scene(anchor=[_fact("柜台")])
    scene["story_function"] = "宏大华丽的史诗空间"
    text, removed = compile_scene_aesthetic(_Constitution(), scene)
    assert set(("宏大", "华丽", "史诗")) <= set(removed)
    for word in ("宏大", "华丽", "史诗"):
        assert word not in text


def test_vague_words_backed_by_source_are_kept():
    scene = _scene(anchor=[_fact("柜台")])
    scene["evidence"] = [{"quote": "账房宏大，梁柱华丽"}]
    scene["story_function"] = "宏大华丽的账房"
    text, removed = compile_scene_aesthetic(_Constitution(), scene)
    assert removed == ()
    assert "宏大" in text


def test_aesthetic_without_constitution_does_not_invent():
    text, removed = compile_scene_aesthetic(None, _scene())
    assert removed == ()
    assert text != UNKNOWN and "空灵飘渺" not in text


def test_unsupported_vague_words_ignores_evidenced_words():
    assert unsupported_vague_words("宏大", "宏大") == ()
    assert unsupported_vague_words("宏大", "") == ("宏大",)


# --- 漂移检查：比较结构锚点集合，而不是关键词搜索 -------------------------------

def test_anchor_drift_detects_added_and_removed_tokens():
    old = {"固定锚点": "柜台、梁柱", "入口出口": "西侧门", "主要物件": UNKNOWN}
    new = {"固定锚点": "柜台、屏风", "入口出口": "西侧门", "主要物件": UNKNOWN}
    drifts = compare_anchors(old, new)
    assert len(drifts) == 1
    assert "梁柱" in drifts[0] and "屏风" in drifts[0]


def test_anchor_drift_ignores_unknown_slots_and_identical_sets():
    same = {"固定锚点": "柜台", "入口出口": UNKNOWN, "主要物件": UNKNOWN}
    assert compare_anchors(same, dict(same)) == []
    other = {"固定锚点": UNKNOWN, "入口出口": UNKNOWN, "主要物件": "屏风"}
    assert compare_anchors(same, other) == []


def test_anchor_drift_is_set_based_not_keyword_search():
    """同一批锚点换顺序不算漂移——关键词搜索会误报，集合比较不会。"""
    old = {"固定锚点": "柜台、梁柱", "入口出口": UNKNOWN, "主要物件": UNKNOWN}
    new = {"固定锚点": "梁柱、柜台", "入口出口": UNKNOWN, "主要物件": UNKNOWN}
    assert compare_anchors(old, new) == []


# --- 站位可追溯性 --------------------------------------------------------------

def test_blocking_traceable_only_from_the_three_allowed_sources():
    for source in BLOCKING_SOURCES:
        assert blocking_status(source) == BLOCKING_OK, source


def test_blocking_without_source_awaits_review():
    assert blocking_status("导演推导") == BLOCKING_PENDING
    assert blocking_status("") == BLOCKING_PENDING


# --- 母版内容行：供 continuity 直接拼装 ----------------------------------------

def test_scene_master_rows_include_slots_immutable_views_and_aesthetic():
    rows = dict(scene_master_rows(_scene(anchor=[_fact("柜台")]), _Constitution()))
    assert rows["固定锚点"].startswith("柜台")
    assert IMMUTABLE_SLOT in rows and "柜台" in rows[IMMUTABLE_SLOT]
    assert rows["必要视图"] == BASE_SCENE_VIEW
    assert "空灵飘渺" in rows["场景审美"]


# --- 证据去重与「不倒原文进审美」 ----------------------------------------------

def test_evidence_text_dedups_repeated_quotes():
    scene = _scene()
    scene["evidence"] = [{"quote": "同一句"}, {"quote": "同一句"}, {"quote": "同一句"}]
    text = evidence_text(scene)
    assert text.count("同一句") == 1


def test_aesthetic_does_not_dump_raw_evidence():
    scene = _scene(anchor=[_fact("柜台")])
    scene["evidence"] = [{"quote": "林姑娘走进茶楼，抬头看见二楼栏杆"}]
    text, _ = compile_scene_aesthetic(_Constitution(), scene)
    assert "林姑娘走进茶楼" not in text
    assert "空灵飘渺" in text


def test_aesthetic_takes_atmosphere_from_confirmed_facts_only():
    scene = _scene(time=[_fact("黄昏")], lighting=[_fact("烛光")])
    text, removed = compile_scene_aesthetic(None, scene)
    assert "黄昏" in text and "烛光" in text
    assert removed == ()


# --- 与 continuity 的集成：母版文件必须带上新字段 -------------------------------

def test_build_scene_master_emits_scene_master_rows():
    from scripts.engine.continuity import build_scene_master

    index = {
        "assets": [{
            "asset_id": "SC_茶楼", "stable_key": "SCN:00001:1", "type": "场景", "name": "茶楼",
            "story_function": "剧本驱动变体载体", "fact_level": "剧本事实", "relations": [],
            "evidence": [{"chapter": "第1章", "segment": "段01.md", "line": "00010",
                          "quote": "林姑娘走进茶楼"}],
            "space_facts": {"anchor": [{"value": "柜台", "chapter": "第1章",
                                        "segment": "段01.md", "line": "00010"}]},
        }],
    }
    master = build_scene_master(index, [], _Constitution())
    for label in ("主要物件", "朝向", "时间", IMMUTABLE_SLOT, "必要视图", "场景审美"):
        assert f"| {label} |" in master, label
    assert "| 禁改项 | 柜台 |" in master
    assert "空灵飘渺" in master
