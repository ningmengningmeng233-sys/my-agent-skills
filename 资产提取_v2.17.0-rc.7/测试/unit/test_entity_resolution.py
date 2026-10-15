# -*- coding: utf-8 -*-
"""工单 3 红测：实体消歧与状态/部件/别名关系（scripts/engine/entity_resolution.py）。

验证：同义别名可高置信合并（ALIAS_OF）；名字包含但非同一对象判为独立（INDEPENDENT）；
人物状态→STATE_OF 且指向母资产不造自环；道具部件→PART_OF；同名多实体→AMBIGUOUS 且绝不吞并、
不重复分配同一 stable_key。自动合并只允许高置信规则。
"""
import pytest

from engine import entity_resolution as er


def _existing(asset_id, name, type_, aliases=()):
    return {"asset_id": asset_id, "name": name, "type": type_, "aliases": list(aliases)}


def test_synonym_alias_merges_with_high_confidence():
    existing = [_existing("CH_掌柜", "檐下掌柜", "人物", aliases=["茶楼掌柜", "老周"])]
    r = er.resolve_entity({"name": "老周", "type": "人物", "fact_ids": ["f1"]}, existing)
    assert r["decision"] == er.ALIAS_OF
    assert r["target_id"] == "CH_掌柜"
    assert r["auto_merge"] is True
    assert r["confidence"] >= er.AUTO_MERGE_THRESHOLD
    assert r["evidence_fact_ids"] == ["f1"]


def test_substring_but_different_object_is_independent():
    existing = [_existing("SC_林府", "林府", "场景"), _existing("SC_竹林", "竹林", "场景")]
    # 「林」出现在多个场景名里，但不是同一对象：不得合并
    r = er.resolve_entity({"name": "小林", "type": "场景", "fact_ids": ["f9"]}, existing)
    assert r["decision"] == er.INDEPENDENT
    assert r["target_id"] == ""
    assert r["auto_merge"] is False


def test_character_state_resolves_to_mother_not_self():
    existing = [_existing("CH_公子", "负剑公子", "人物")]
    cand = {"name": "断臂公子", "type": "人物", "state_of": "负剑公子", "fact_ids": ["s1", "s2"]}
    r = er.resolve_entity(cand, existing)
    assert r["decision"] == er.STATE_OF
    assert r["target_id"] == "CH_公子"
    assert r["target_id"] != ""  # 指向母资产，绝不指向自身/造自环
    assert r["auto_merge"] is False  # 状态是变体，不吞并成新母资产


def test_prop_part_resolves_to_parent():
    existing = [_existing("PR_长剑", "长剑", "道具")]
    cand = {"name": "剑鞘", "type": "道具", "part_of": "长剑", "fact_ids": ["p1"]}
    r = er.resolve_entity(cand, existing)
    assert r["decision"] == er.PART_OF
    assert r["target_id"] == "PR_长剑"
    assert r["auto_merge"] is False


def test_same_name_multiple_entities_is_ambiguous():
    existing = [_existing("CH_阿锦", "阿锦", "人物", aliases=["琴师"]),
                _existing("CH_阿锦2", "阿锦", "人物", aliases=["撑船的"])]
    r = er.resolve_entity({"name": "阿锦", "type": "人物", "fact_ids": ["a"]}, existing)
    assert r["decision"] == er.AMBIGUOUS
    assert r["target_id"] == ""
    assert r["auto_merge"] is False


def test_unknown_type_collision_is_ambiguous():
    existing = [_existing("SC_渡口", "渡口", "场景"), _existing("CH_船夫", "渡口船夫", "人物", aliases=["渡口"])]
    # 名字「渡口」既是场景又被人物的别名指向：跨类型冲突→待裁决
    r = er.resolve_entity({"name": "渡口", "type": "人物", "fact_ids": ["x"]}, existing)
    assert r["decision"] == er.AMBIGUOUS
    assert r["auto_merge"] is False


def test_fresh_entity_is_independent():
    r = er.resolve_entity({"name": "卖药少年", "type": "人物", "fact_ids": ["n1"]}, [])
    assert r["decision"] == er.INDEPENDENT
    assert r["target_id"] == ""
    assert r["auto_merge"] is False


def test_declared_state_without_mother_is_ambiguous():
    existing = [_existing("CH_公子", "负剑公子", "人物")]
    cand = {"name": "蒙面人", "type": "人物", "state_of": "不存在的人", "fact_ids": ["q"]}
    r = er.resolve_entity(cand, existing)
    assert r["decision"] == er.AMBIGUOUS


def test_assign_stable_keys_never_reuses_for_ambiguous():
    existing = [_existing("CH_甲", "甲", "人物"), _existing("CH_乙", "甲", "人物")]
    amb = er.resolve_entity({"name": "甲", "type": "人物", "fact_ids": ["1"]}, existing)
    # 同名两实体：歧义不得被吞并到某一个目标
    assert amb["decision"] == er.AMBIGUOUS
    assert amb["target_id"] == ""
    assert amb["auto_merge"] is False
    # 未命中既有名的新实体判独立（不按包含关系误合并）
    fresh = er.resolve_entity({"name": "乙", "type": "人物", "fact_ids": ["2"]}, existing)
    assert fresh["decision"] == er.INDEPENDENT
    keys = er.assign_stable_keys([{"name": "甲"}, {"name": "乙"}], "CHR")
    assert len(set(keys)) == len(keys)  # 不重复分配同一 stable_key


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
