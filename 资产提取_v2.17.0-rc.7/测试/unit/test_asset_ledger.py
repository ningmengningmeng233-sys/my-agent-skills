# -*- coding: utf-8 -*-
"""工单 2 红测：唯一类型化资产总账 + 数量守恒覆盖门。

验证：五类记录齐备且后期元素不进生图；正式人物/场景/道具必带 base_prompt 必需产物；
旧四表可由总账渲染且保留旧 ID；删除任一提示词映射时覆盖门给出具体资产 ID 并非零退出。
"""
import pytest

from engine import asset_ledger as al
from scripts import check_asset_coverage as cac


def _index():
    return {
        "schema_version": 1,
        "source_fingerprint": "fp",
        "assets": [
            {"asset_id": "CH_甲", "stable_key": "CHR:00001:1", "type": "人物", "name": "甲",
             "priority": "P0", "fact_level": "剧本事实", "status": "CONFIRMED",
             "evidence": [{"quote": "甲来了", "line": "00001"}], "relations": []},
            {"asset_id": "SC_楼", "stable_key": "SCN:00002:1", "type": "场景", "name": "楼",
             "priority": "P2", "fact_level": "剧本事实", "status": "CONFIRMED",
             "evidence": [{"quote": "在楼里", "line": "00002"}], "relations": []},
            {"asset_id": "PR_壶", "stable_key": "PRP:00003:1", "type": "道具", "name": "壶",
             "priority": "P2", "fact_level": "剧本事实", "status": "CONFIRMED",
             "evidence": [{"quote": "一只壶", "line": "00003"}], "relations": []},
            {"asset_id": "CO_袍", "stable_key": "CST:00004:1", "type": "服装", "name": "袍",
             "priority": "P2", "fact_level": "剧本事实", "status": "CONFIRMED",
             "evidence": [{"quote": "穿袍", "line": "00004"}],
             "relations": [{"kind": "WEARS", "target": "CH_甲"}]},
        ],
        "unresolved_relations": [],
    }


_POST = [{"name": "字幕：三年后", "quote": "【字幕：三年后】", "kind": "字幕"}]


def test_ledger_covers_five_record_types():
    ledger = al.build_ledger(_index(), post_elements=_POST)
    types = {e["asset_type"] for e in ledger}
    assert types >= {"person", "scene", "prop", "costume", "post_element"}


def test_formal_person_scene_prop_require_base_prompt():
    ledger = {e["asset_id"]: e for e in al.build_ledger(_index(), post_elements=_POST)}
    for aid in ("CH_甲", "SC_楼", "PR_壶"):
        assert "base_prompt" in ledger[aid]["required_outputs"], f"{aid} 必须要求基础提示词"
        assert ledger[aid]["ledger_status"] == "formal"


def test_post_element_never_enters_image_plan():
    ledger = al.build_ledger(_index(), post_elements=_POST)
    post = [e for e in ledger if e["asset_type"] == "post_element"]
    assert post, "应有后期元素记录"
    for e in post:
        assert e["ledger_status"] != "formal"
        assert "image_task" not in e["required_outputs"], "后期元素不得进入生图计划"
        assert e["production_mode"] == "post_only"


def test_legacy_ids_and_stable_keys_preserved():
    ledger = al.build_ledger(_index(), post_elements=_POST)
    ids = [e["asset_id"] for e in ledger if e["asset_type"] != "post_element"]
    assert "CH_甲" in ids and "PR_壶" in ids
    keyed = {e["asset_id"]: e["stable_key"] for e in ledger}
    assert keyed["CH_甲"] == "CHR:00001:1", "stable_key 不得重编号"


def test_render_legacy_tables_emits_four_headers():
    ledger = al.build_ledger(_index(), post_elements=_POST)
    md = al.render_legacy_tables(ledger)
    for title in ("人物资产提取表", "场景资产提取表", "道具资产提取表", "服装妆造资产提取表"):
        assert title in md


def _prompts(produce_all=True):
    """基础提示词分节名集合（模拟 generate_prompts 产物）。"""
    prompted = {"甲", "楼"}
    if produce_all:
        prompted.add("壶")
    return prompted


def test_coverage_full_when_every_formal_prompted():
    ledger = al.build_ledger(_index(), post_elements=_POST)
    report = cac.evaluate(ledger, prompted_names=_prompts(True), skipped={})
    assert report["violations"] == []
    assert report["coverage"] == 1.0


def test_coverage_flags_specific_asset_when_prompt_missing():
    ledger = al.build_ledger(_index(), post_elements=_POST)
    report = cac.evaluate(ledger, prompted_names=_prompts(False), skipped={})
    ids = {v["asset_id"] for v in report["violations"]}
    assert "PR_壶" in ids, "缺提示词且无豁免理由的正式道具必须被点名"
    assert report["exit_code"] != 0


def test_blocked_asset_with_reason_is_not_a_violation():
    ledger = al.build_ledger(_index(), post_elements=_POST)
    report = cac.evaluate(ledger, prompted_names=_prompts(False),
                          skipped={"PR_壶": "待审：出处未验证"})
    assert "PR_壶" not in {v["asset_id"] for v in report["violations"]}


def test_conservation_identity_holds():
    """守恒式：正式资产 = 已产基础提示词 + 明确待审 + 明确阻断（无第三类静默态）。"""
    ledger = al.build_ledger(_index(), post_elements=_POST)
    report = cac.evaluate(ledger, prompted_names=_prompts(False), skipped={"PR_壶": "待审"})
    assert report["counts"]["formal"] == (
        report["counts"]["prompted"] + report["counts"]["pending_or_blocked"]
    ), "存在既无产物又无原因的资产即违反守恒"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
