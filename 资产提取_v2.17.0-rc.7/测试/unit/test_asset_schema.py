#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：资产规范化模式验证（ADR-0084 / 工单 2 先行失败用例）"""

import unittest
from pathlib import Path
import sys

# 将目标仓库加入模块查找路径
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from scripts.engine.asset_schema import (
        ASSET_SUBTYPES,
        VISUAL_NEED_KINDS,
        AssetSchemaValidator,
        StableKeyAllocator,
        build_visual_need,
        normalize_subtype,
        subtype_with_raw,
        validate_visual_needs,
    )
except ImportError:
    AssetSchemaValidator = None


class TestAssetSchema(unittest.TestCase):
    def setUp(self):
        if AssetSchemaValidator is None:
            self.fail("无法导入 scripts.engine.asset_schema 模块")
        self.validator = AssetSchemaValidator()

    def test_valid_asset_index(self):
        index_data = {
            "schema_version": 1,
            "source_fingerprint": "sha256:11223344",
            "assets": [
                {
                    "asset_id": "CH_萧炎",
                    "stable_key": "CHR:00001:1",
                    "type": "人物",
                    "subtype": "主角",
                    "name": "萧炎",
                    "story_function": "故事核心主角",
                    "priority": "P0",
                    "fact_level": "剧本事实",
                    "evidence": [{"segment": "第01段", "line": "00001", "quote": "萧炎看着手中的戒指"}],
                    "dependencies": [],
                    "relations": [],
                    "status": "CONFIRMED",
                },
                {
                    "asset_id": "CO_萧炎_黑袍",
                    "stable_key": "CST:00001:1",
                    "type": "服装",
                    "subtype": "常服",
                    "name": "萧炎_黑袍",
                    "story_function": "初始常服",
                    "priority": "P1",
                    "fact_level": "剧本事实",
                    "evidence": [{"segment": "第01段", "line": "00002", "quote": "一袭普通黑袍"}],
                    "dependencies": ["CH_萧炎"],
                    "relations": [{"kind": "WEARS", "target": "CH_萧炎"}],
                    "status": "CONFIRMED",
                },
            ],
        }
        res = self.validator.validate_index(index_data)
        self.assertTrue(res.is_valid, f"验证应通过，但报错: {res.errors}")

    def test_invalid_fact_level(self):
        index_data = {
            "schema_version": 1,
            "source_fingerprint": "sha256:11223344",
            "assets": [
                {
                    "asset_id": "CH_萧炎",
                    "stable_key": "CHR:00001:1",
                    "type": "人物",
                    "name": "萧炎",
                    "story_function": "主角",
                    "priority": "P1",
                    "fact_level": "未知等级",  # 非法
                    "evidence": [],
                    "dependencies": [],
                    "relations": [],
                    "status": "CONFIRMED",
                }
            ],
        }
        res = self.validator.validate_index(index_data)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("fact_level" in e for e in res.errors))

    def test_fact_level_script_fact_requires_evidence(self):
        index_data = {
            "schema_version": 1,
            "source_fingerprint": "sha256:11223344",
            "assets": [
                {
                    "asset_id": "CH_萧炎",
                    "stable_key": "CHR:00001:1",
                    "type": "人物",
                    "name": "萧炎",
                    "story_function": "主角",
                    "priority": "P1",
                    "fact_level": "剧本事实",
                    "evidence": [],  # 剧本事实必须有 evidence 摘句
                    "dependencies": [],
                    "relations": [],
                    "status": "CONFIRMED",
                }
            ],
        }
        res = self.validator.validate_index(index_data)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("evidence" in e for e in res.errors))

    def test_missing_relation_target(self):
        index_data = {
            "schema_version": 1,
            "source_fingerprint": "sha256:11223344",
            "assets": [
                {
                    "asset_id": "CH_萧炎",
                    "stable_key": "CHR:00001:1",
                    "type": "人物",
                    "name": "萧炎",
                    "story_function": "主角",
                    "priority": "P1",
                    "fact_level": "强推断",
                    "evidence": [],
                    "dependencies": [],
                    "relations": [{"kind": "WEARS", "target": "CO_不存在的衣服"}],
                    "status": "CONFIRMED",
                }
            ],
        }
        res = self.validator.validate_index(index_data)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("CO_不存在的衣服" in e for e in res.errors))

    def test_connects_to_consistency(self):
        # 场景 CONNECTS_TO 必须对端也存在
        index_data = {
            "schema_version": 1,
            "source_fingerprint": "sha256:11223344",
            "assets": [
                {
                    "asset_id": "SC_走廊",
                    "stable_key": "SCN:00001:1",
                    "type": "场景",
                    "name": "走廊",
                    "story_function": "通道",
                    "priority": "P1",
                    "fact_level": "强推断",
                    "evidence": [],
                    "dependencies": [],
                    "relations": [{"kind": "CONNECTS_TO", "target": "SC_大厅"}],
                    "status": "CONFIRMED",
                }
            ],
        }
        res = self.validator.validate_index(index_data)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("SC_大厅" in e for e in res.errors))

    def test_stable_key_allocation_and_rename_continuity(self):
        allocator = StableKeyAllocator()
        key1 = allocator.allocate("人物", "萧炎", line_no=10)
        self.assertEqual(key1, "CHR:00010:1")
        # 再次调用同名必须幂等复用
        key1_again = allocator.allocate("人物", "萧炎", line_no=999)
        self.assertEqual(key1_again, key1)
        # 新角色即便在更前行号也不得改变已有映射
        mapping = allocator.get_mapping()
        allocator2 = StableKeyAllocator(mapping)
        key2 = allocator2.allocate("人物", "药老", line_no=5)
        self.assertEqual(key2, "CHR:00005:1")
        self.assertEqual(allocator2.allocate("人物", "萧炎", line_no=10), key1)

    def test_homonym_gets_distinct_keys(self):
        # 同名异物必须保留不同 key（以不同 asset_id 区分）
        allocator = StableKeyAllocator()
        k1 = allocator.allocate("人物", "CH_阿强", {"line": "00001"}, ["阿强"])
        k2 = allocator.allocate("人物", "CH_阿强_2", {"line": "00002"}, ["阿强店员"])
        self.assertNotEqual(k1, k2)

    def test_alias_collision_raises(self):
        from scripts.engine.asset_schema import AliasConflictError
        allocator = StableKeyAllocator()
        allocator.allocate("人物", "CH_甲", {"line": "00001"}, ["阿强"])
        allocator.allocate("人物", "CH_乙", {"line": "00002"}, ["阿强二号"] if False else ["阿强B"])
        # 新实体别名同时命中两个已有实体 → 抛冲突
        store = allocator.get_mapping()
        store["keys"]["CHR:00003:9"] = {"asset_id": "CH_丙", "type": "人物",
                                        "aliases": ["阿强", "阿强B"], "first_evidence": None}
        allocator2 = StableKeyAllocator(store)
        with self.assertRaises(AliasConflictError):
            allocator2.allocate("人物", "CH_丁", {"line": "00003"}, ["阿强", "阿强B"])

    def test_in_build_alias_no_silent_merge(self):
        # R1/R4a：同一构建内两个新实体共享别名，不得静默复用对方 stable_key
        allocator = StableKeyAllocator()
        k1 = allocator.allocate("场景", "SC_东街茶楼", {"line": "00003"}, ["茶楼"])
        k2 = allocator.allocate("场景", "SC_西街茶楼", {"line": "00006"}, ["茶楼"])
        self.assertNotEqual(k1, k2)
        # 旧库复用（外部 mapping）语义不变：唯一别名命中仍可复用
        store = allocator.to_dict()
        allocator2 = StableKeyAllocator(store)
        self.assertEqual(allocator2.allocate("场景", "SC_东街茶楼", {"line": "00003"}, ["茶楼"]), k1)

    def test_relation_direction_enforced(self):
        # R4c：validator 与 RELATION_MATRIX 同契约——WEARS 必须 服装→人物
        index_data = {
            "schema_version": 1,
            "source_fingerprint": "sha256:11223344",
            "assets": [
                {"asset_id": "CH_萧炎", "stable_key": "CHR:00001:1", "type": "人物", "name": "萧炎",
                 "story_function": "主角", "priority": "P1", "fact_level": "强推断", "evidence": [],
                 "dependencies": [], "relations": [{"kind": "WEARS", "target": "CO_黑袍"}], "status": "CONFIRMED"},
                {"asset_id": "CO_黑袍", "stable_key": "CST:00001:1", "type": "服装", "name": "黑袍",
                 "story_function": "常服", "priority": "P1", "fact_level": "强推断", "evidence": [],
                 "dependencies": [], "relations": [], "status": "CONFIRMED"},
            ],
        }
        res = self.validator.validate_index(index_data)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("WEARS" in e and "方向" in e for e in res.errors),
                        f"应报方向错误: {res.errors}")

    def test_state_of_self_loop_rejected(self):
        # R4b：无独立变体节点时 STATE_OF 自环必须被 validator 拒绝
        index_data = {
            "schema_version": 1,
            "source_fingerprint": "sha256:11223344",
            "assets": [
                {"asset_id": "SC_茶楼", "stable_key": "SCN:00001:1", "type": "场景", "name": "茶楼",
                 "story_function": "主场景", "priority": "P1", "fact_level": "强推断", "evidence": [],
                 "dependencies": [], "relations": [{"kind": "STATE_OF", "target": "SC_茶楼"}],
                 "status": "CONFIRMED"},
            ],
        }
        res = self.validator.validate_index(index_data)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("自环" in e for e in res.errors), f"应报自环错误: {res.errors}")


class TestAssetSubtypes(unittest.TestCase):
    """工单01-Task5：四类顶层资产不变，通过中文子类型与视觉需求扩展（规格 2.1/3.1）。"""

    def test_top_level_types_stay_four(self):
        from scripts.engine.asset_schema import AssetType
        self.assertEqual(set(AssetType.valid_values()), {"人物", "场景", "道具", "服装"})
        self.assertEqual(set(ASSET_SUBTYPES), {"人物", "场景", "道具", "服装"})

    def test_subtype_enums_are_chinese(self):
        for asset_type, values in ASSET_SUBTYPES.items():
            self.assertTrue(values, f"{asset_type} 子类型枚举不得为空")
            for v in values:
                self.assertTrue(all(not ch.isascii() for ch in v) or v == "其他",
                                f"{asset_type} 子类型「{v}」必须为中文枚举")

    def test_monster_maps_to_nonhuman_character(self):
        self.assertEqual(normalize_subtype("人物", "怪物"), "非人角色")
        self.assertEqual(normalize_subtype("人物", "妖兽"), "非人角色")
        self.assertEqual(normalize_subtype("人物", "神兽"), "非人角色")

    def test_carriage_maps_to_vehicle_prop(self):
        self.assertEqual(normalize_subtype("道具", "马车"), "载具")
        self.assertEqual(normalize_subtype("道具", "舟船"), "载具")

    def test_palace_maps_to_architecture_scene(self):
        self.assertEqual(normalize_subtype("场景", "宫殿"), "建筑")
        self.assertEqual(normalize_subtype("场景", "楼阁"), "建筑")

    def test_armor_maps_to_costume_armor(self):
        self.assertEqual(normalize_subtype("服装", "盔甲"), "甲胄")
        self.assertEqual(normalize_subtype("服装", "铠甲"), "甲胄")

    def test_known_canonical_value_passes_through(self):
        self.assertEqual(normalize_subtype("服装", "甲胄"), "甲胄")
        self.assertEqual(normalize_subtype("人物", "主角"), "主角")

    def test_unknown_subtype_falls_back_to_other_and_keeps_raw(self):
        self.assertEqual(normalize_subtype("道具", "蒸汽机"), "其他")
        rec = subtype_with_raw("道具", "蒸汽机")
        self.assertEqual(rec["subtype"], "其他")
        self.assertEqual(rec["subtype_raw"], "蒸汽机")

    def test_known_subtype_has_no_raw_note(self):
        rec = subtype_with_raw("道具", "马车")
        self.assertEqual(rec["subtype"], "载具")
        self.assertEqual(rec["subtype_raw"], "")

    def test_empty_subtype_is_undivided(self):
        self.assertEqual(normalize_subtype("人物", ""), "未细分")
        self.assertEqual(normalize_subtype("人物", "   "), "未细分")

    def test_unknown_asset_type_falls_back_to_other(self):
        self.assertEqual(normalize_subtype("法术", "雷法"), "其他")


class TestFifthAssetTypeRejected(unittest.TestCase):
    """第五类顶层资产必须被索引校验器拒绝（规格 2.1）。"""

    def test_fifth_asset_type_rejected_by_index_validator(self):
        validator = AssetSchemaValidator()
        index_data = {
            "schema_version": 1,
            "source_fingerprint": "sha256:11223344",
            "assets": [
                {"asset_id": "FX_雷电", "stable_key": "AST:00001:1", "type": "特效", "name": "雷电",
                 "story_function": "法术表现", "priority": "P2", "fact_level": "强推断", "evidence": [],
                 "dependencies": [], "relations": [], "status": "CONFIRMED"},
            ],
        }
        res = validator.validate_index(index_data)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("type" in e for e in res.errors), res.errors)


class TestVisualNeeds(unittest.TestCase):
    """视觉需求必须绑定资产或场景并带原文证据，绝不生成第五类资产 ID（规格 2.1）。"""

    def _need(self, asset_id="SC_茶楼", kind="天候", value="雨", fact="剧本事实",
              quote="雨点打在茶楼的瓦上", line="00012"):
        return build_visual_need(asset_id, kind, value, quote=quote, line=line, fact_level=fact)

    def test_need_shape_is_bound_to_asset(self):
        need = self._need()
        self.assertEqual(need["asset_id"], "SC_茶楼")
        self.assertEqual(need["kind"], "天候")
        self.assertEqual(need["value"], "雨")
        self.assertEqual(need["fact_level"], "剧本事实")
        self.assertEqual(need["evidence"]["quote"], "雨点打在茶楼的瓦上")
        self.assertEqual(need["evidence"]["line"], "00012")
        # 视觉需求不是资产：不得带资产 ID 前缀字段冒充第五类
        self.assertNotIn("asset_id_prefix", need)
        self.assertFalse(need["need_id"].startswith(("CH_", "SC_", "PR_", "CO_")))

    def test_spell_and_weather_are_visual_needs_not_assets(self):
        needs = [
            self._need(asset_id="CH_甲", kind="法术", value="掌心雷"),
            self._need(asset_id="SC_长街", kind="天候", value="雷雨"),
        ]
        errors = validate_visual_needs(needs, {"CH_甲", "SC_长街"})
        self.assertEqual(errors, [])
        for need in needs:
            self.assertIn(need["kind"], VISUAL_NEED_KINDS)

    def test_need_with_unknown_asset_is_rejected(self):
        needs = [self._need(asset_id="FX_雷电")]
        errors = validate_visual_needs(needs, {"CH_甲", "SC_长街"})
        self.assertTrue(any("FX_雷电" in e for e in errors), errors)

    def test_need_without_evidence_is_rejected_for_script_fact(self):
        needs = [self._need(quote="", fact="剧本事实")]
        errors = validate_visual_needs(needs, {"SC_茶楼"})
        self.assertTrue(any("摘句" in e or "quote" in e for e in errors), errors)

    def test_need_with_unknown_kind_is_rejected(self):
        needs = [self._need(kind="第五类资产")]
        errors = validate_visual_needs(needs, {"SC_茶楼"})
        self.assertTrue(any("kind" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
