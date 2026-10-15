# -*- coding: utf-8 -*-
"""工单 1 红测：文档段落分类 + 证据保留的确定性源事实层。

覆盖：段落可区分（对白/场景头/动作/制作说明/后期文字）；制作说明只给约束不产正式资产；
后期文字归 post_element；事实保留精确字符范围与摘句且 fact_id 稳定；规则与模型事实并集合并、
冲突并存待裁决，任一方发现的候选不因另一方缺失而消失。
"""
from engine import document_sections as ds
from engine import source_facts as sf


# ---------- 段落分类 ----------

def test_classify_sections_distinguishes_kinds():
    text = (
        "第十二场 临河茶楼\n"
        "沈姑娘：茶还热着。\n"
        "檐下掌柜拨着算盘，雨落进四方天井。\n"
        "【制作说明：本剧整体偏冷色调。】\n"
        "【字幕：永昌三年，秋】\n"
    )
    spans = ds.classify_sections(text)
    kinds = {s.kind for s in spans}
    assert "scene_heading" in kinds
    assert "dialogue" in kinds
    assert "action" in kinds
    assert "production_note" in kinds
    assert "post_text" in kinds


def test_classify_sections_spans_cover_exact_ranges():
    text = "第一场\n这是动作描述的一行。\n"
    spans = ds.classify_sections(text)
    for s in spans:
        assert text[s.start:s.end].strip() == s.text.strip()
    assert spans[0].start == 0


# ---------- 确定性扫描 ----------

def test_scan_facts_preserves_quote_and_range():
    text = "檐下掌柜拨着算盘，雨落进四方天井。\n"
    facts = sf.scan_deterministic_facts(text, source_file="a.txt", segment_id="seg-01")
    assert facts, "至少应产出一条确定性事实"
    for f in facts:
        assert text[f["start"]:f["end"]] == f["quote"], "摘句必须与字符范围逐字一致"
        assert f["segment_id"] == "seg-01"
        assert f["source_file"] == "a.txt"
        assert f["extractor"] == "rule"


def test_fact_id_is_deterministic():
    text = "檐下掌柜拨着算盘，雨落进四方天井。\n"
    a = sf.scan_deterministic_facts(text, source_file="a.txt", segment_id="s1")
    b = sf.scan_deterministic_facts(text, source_file="a.txt", segment_id="s1")
    assert [x["fact_id"] for x in a] == [x["fact_id"] for x in b]
    c = sf.scan_deterministic_facts(text, source_file="a.txt", segment_id="s2")
    assert {x["fact_id"] for x in c} != {x["fact_id"] for x in a}, "不同段应有不同 fact_id"


def test_production_note_yields_constraint_not_formal_asset():
    text = "【制作说明：本剧整体偏冷色调，主角服装忌用亮黄。】\n"
    facts = sf.scan_deterministic_facts(text, source_file="a.txt", segment_id="s1")
    # 制作说明应作为约束事实存在，但不得生成人物/场景/道具/服装的正式资产候选
    asset_candidates = [f for f in facts if f["candidate_type"] in {"person", "scene", "prop", "costume"}]
    assert not asset_candidates, "制作说明不得直接生成正式资产候选"
    assert any(f["section_kind"] == "production_note" for f in facts)


def test_late_text_classified_as_post_element():
    text = "【字幕：永昌三年，秋】\n【片头预告：下一章。】\n"
    facts = sf.scan_deterministic_facts(text, source_file="a.txt", segment_id="s1")
    post = [f for f in facts if f["candidate_type"] == "post_element"]
    assert len(post) >= 2, "字幕/预告应归为后期元素"
    assert all(f["section_kind"] == "post_text" for f in post)


# ---------- 语义合并 ----------

def test_merge_union_keeps_both_sides_when_one_misses():
    rule = [sf.make_fact("檐下掌柜", "person", "掌柜在拨算盘", source_file="a.txt", segment_id="s1")]
    model = [sf.make_fact("青瓷壶", "prop", "青瓷壶凉了的茶", source_file="a.txt", segment_id="s1")]
    merged = sf.merge_semantic_facts(rule, model)
    names = {f["candidate_name"] for f in merged}
    assert "檐下掌柜" in names and "青瓷壶" in names, "任一方发现的候选都不得因另一方缺失而消失"


def test_merge_dedupes_identical_evidence():
    f1 = sf.make_fact("算盘", "prop", "拨着算盘", source_file="a.txt", segment_id="s1")
    f2 = sf.make_fact("算盘", "prop", "拨着算盘", source_file="a.txt", segment_id="s1")
    merged = sf.merge_semantic_facts([f1], [f2])
    same = [f for f in merged if f["candidate_name"] == "算盘"]
    assert len(same) == 1, "相同证据应合并为一条"


def test_merge_conflict_is_pending_not_silently_resolved():
    # 同名不同类型 → 冲突并存、标待裁决，不得静默择一
    rule = [sf.make_fact("阿锦", "person", "撑伞姑娘唤作阿锦", source_file="a.txt", segment_id="s1")]
    model = [sf.make_fact("阿锦", "scene", "阿锦渡口", source_file="a.txt", segment_id="s1")]
    merged = sf.merge_semantic_facts(rule, model)
    ajin = [f for f in merged if f["candidate_name"] == "阿锦"]
    assert len(ajin) >= 2, "冲突不得被静默合并到单一类型"
    assert all(f["confidence"] < 1.0 or f.get("pending_adjudication") for f in ajin), "冲突须标待裁决"
