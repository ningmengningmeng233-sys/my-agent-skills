#!/usr/bin/env python3
"""Unit tests for engine/style.py parsing and style locking (no LLM calls)."""
import re
import unittest

from style import (
    build_render_block,
    content_lock_sentence,
    render_lock_sentences,
    build_style_block,
    load_style_map,
    parse_style_card,
    re_split,
    resolve_style,
)


class TestReSplit(unittest.TestCase):
    def test_basic_split(self):
        self.assertEqual(re_split("古风/现代"), ["古风", "现代"])

    def test_preserve_slash_in_parentheses(self):
        self.assertEqual(re_split("权谋（宫斗/宅斗）,仙侠"), ["权谋（宫斗/宅斗）", "仙侠"])

    def test_empty(self):
        self.assertEqual(re_split(None), [])
        self.assertEqual(re_split(""), [])


class TestLoadStyleMap(unittest.TestCase):
    def test_loads_known_mappings(self):
        smap = load_style_map()
        self.assertIn("古风", smap)
        self.assertTrue(smap["古风"])


class TestParseStyleCard(unittest.TestCase):
    def test_a1_card_exists(self):
        card = parse_style_card("A1")
        self.assertIsNotNone(card)
        self.assertEqual(card["id"], "A1")
        self.assertTrue(card["name"])
        self.assertTrue(card["zh"])

    def test_missing_card_defaults_to_a1(self):
        card = parse_style_card("ZZ99")
        self.assertIsNotNone(card)
        self.assertEqual(card["id"], "A1")


class TestBuildStyleBlock(unittest.TestCase):
    def test_free_description(self):
        block = build_style_block(None, "水墨风")
        self.assertIn("水墨风", block)
        self.assertIn("风格锁定句", block)
        self.assertNotIn("Style lock", block)
        self.assertIsNone(re.search(r"[A-Za-z]{4,}", block))

    def test_card_block_includes_zh_and_is_pure_chinese(self):
        card = parse_style_card("A1")
        block = build_style_block(card)
        self.assertIn(str(card["zh"]).splitlines()[0], block)
        self.assertNotIn("Style lock", block)
        self.assertIsNone(re.search(r"[A-Za-z]{4,}", block))


class TestBuildRenderBlock(unittest.TestCase):
    def test_keeps_model_and_render_layers(self):
        card = parse_style_card("A1")
        block = build_render_block(card)
        self.assertIn("建模层", block)
        self.assertIn("渲染层", block)
        self.assertNotIn("Render style lock", block)

    def test_none_card_returns_empty(self):
        self.assertEqual(build_render_block(None), "")

    def test_render_block_falls_back_to_qualitative_without_layers(self):
        # ADR-0076 后 A2 已带标准分层；无分层回退口径用合成卡验证
        card = {"id": "X1", "name": "测试无分层卡", "zh": "氛围一句话定性", "layers": []}
        block = build_render_block(card)
        self.assertIn("渲染基调", block)
        self.assertIn("氛围一句话定性", block)
        self.assertNotIn("Render style lock", block)

    def test_render_block_accepts_2d_layer_names(self):
        card = parse_style_card("H9")
        block = build_render_block(card)
        self.assertIn("渲染基调", block)
        self.assertIn("介质层", block)


class TestResolveStyle(unittest.TestCase):
    def test_genre_fallback(self):
        card, mode = resolve_style("古风")
        self.assertEqual(mode, "card")
        self.assertIsNotNone(card)

    def test_unknown_genre_uses_default(self):
        card, mode = resolve_style("不存在的题材XYZ")
        self.assertEqual(mode, "card")
        self.assertEqual(card["id"], "A1")

    def test_free_style_arg(self):
        card, mode = resolve_style("古风", "自由描述水墨")
        self.assertIsNone(card)
        self.assertEqual(mode, "free")

    def test_chinese_shorthand_matches_card_prefix(self):
        card, mode = resolve_style("玄幻", "仙侠奇幻")
        self.assertEqual(mode, "card")
        self.assertEqual(card["id"], "A2")

    def test_exact_style_name_wins_over_prefix(self):
        card, mode = resolve_style("玄幻", "仙侠奇幻风")
        self.assertEqual(mode, "card")
        self.assertEqual(card["id"], "A2")

    def test_style_id_resolves_to_card(self):
        card, mode = resolve_style("玄幻", "H7")
        self.assertEqual(mode, "card")
        self.assertEqual(card["id"], "H7")
        card, mode = resolve_style("古风", "A2")
        self.assertEqual(mode, "card")
        self.assertEqual(card["id"], "A2")

    def test_content_lock_sentence_returns_first_qualitative_line(self):
        card = parse_style_card("H7")
        sentence = content_lock_sentence(card)
        self.assertTrue(sentence)
        self.assertTrue(sentence.startswith("高精度电影级三维国风渲染"))
        self.assertIn("干燥哑光至柔缎", sentence)
        self.assertEqual(content_lock_sentence(None), "")

    def test_render_lock_sentences_extracts_four_layers(self):
        card = parse_style_card("H7")
        sentences = render_lock_sentences(card)
        self.assertEqual(len(sentences), 4)
        self.assertTrue(any("建模层" in x for x in sentences))
        self.assertTrue(any("渲染层" in x for x in sentences))
        self.assertTrue(any("场景/背景" in x for x in sentences))
        self.assertTrue(any("镜头" in x for x in sentences))

    def test_xuanhuan_default_is_xianxia_cinematic_cg(self):
        """玄幻默认画风 = 仙侠国风电影CG（母版对齐后的静态外壳，v2.15.0）。"""
        card, mode = resolve_style("玄幻")
        self.assertEqual(mode, "card")
        self.assertEqual(card["id"], "H7")
        self.assertEqual(card["name"], "仙侠国风电影CG")
        zh = str(card["zh"])
        for phrase in ("高精度电影级三维国风渲染", "干燥哑光至柔缎", "中间调", "成束", "珍珠", "水墨", "蓝灰"):
            self.assertIn(phrase, zh)
        # 旧外壳与母版冲突的表述不得回流
        for conflict in ("斗罗", "玄机", "瓷感", "高饱和", "三格", "每4秒", "运镜", "裁头版"):
            self.assertNotIn(conflict, zh)
        self.assertIn("高精度电影级三维国风渲染", build_style_block(card))
        negative = str(card.get("negative", ""))
        self.assertNotIn("NPR", negative)
        self.assertIn("无动画赛璐璐", negative)
        self.assertIn("无蜡质皮肤", negative)
        self.assertIn("无镜面地板", negative)
        self.assertNotIn("云雾缭绕", zh)
        self.assertNotIn("法阵符文", zh)


class TestStyleRegistryIntegration(unittest.TestCase):
    """工单02-Task1：中文正式名/别名优先经登记表解析，兼容层兜底。"""

    def test_semantic_name_resolves_to_card(self):
        card, mode = resolve_style(None, "仙侠国风电影CG")
        self.assertEqual(mode, "card")
        self.assertEqual(card["id"], "H7")
        self.assertEqual(card["name"], "仙侠国风电影CG")

    def test_internal_id_still_resolves(self):
        card, mode = resolve_style(None, "H7")
        self.assertEqual(mode, "card")
        self.assertEqual(card["id"], "H7")
        # 显示名必须是中文正式名，不是内部编号
        self.assertNotEqual(card["name"], "H7")

    def test_omni_aliases_resolve(self):
        for value in ("仙侠CG", "luminous-xianxia"):
            card, mode = resolve_style(None, value)
            self.assertEqual(mode, "card", value)
            self.assertEqual(card["id"], "H7", value)

    def test_free_description_still_falls_back(self):
        card, mode = resolve_style(None, "我自创的画风")
        self.assertIsNone(card)
        self.assertEqual(mode, "free")

    def test_legacy_card_keeps_working(self):
        card, mode = resolve_style(None, "仙侠奇幻风")
        self.assertEqual(mode, "card")
        self.assertEqual(card["id"], "A2")

    def test_alias_conflict_surfaces_instead_of_first_hit(self):
        """登记表别名冲突必须显式报错，禁止静默第一个命中。"""
        import tempfile
        from pathlib import Path as _Path
        from engine import style_registry as reg_lib

        conflicting = "\n".join([
            "schema_version = 1",
            "",
            "[[styles]]",
            'id = "H7"',
            'name = "仙侠国风电影CG"',
            'aliases = ["同名风格"]',
            'status = "active"',
            'validation_level = "omni_verified"',
            'kind = "style"',
            "shareable = false",
            "",
            "[[styles]]",
            'id = "A2"',
            'name = "仙侠奇幻风"',
            'aliases = ["同名风格"]',
            'status = "active"',
            'validation_level = "legacy_unverified"',
            'kind = "style"',
            "shareable = true",
            "",
        ])
        with tempfile.TemporaryDirectory() as td:
            path = _Path(td) / "registry.toml"
            path.write_text(conflicting, encoding="utf-8")
            original = reg_lib.REGISTRY_PATH
            reg_lib.REGISTRY_PATH = path
            try:
                with self.assertRaises(reg_lib.StyleAliasConflict) as ctx:
                    resolve_style(None, "同名风格")
                self.assertEqual(ctx.exception.code, "STYLE_ALIAS_CONFLICT")
            finally:
                reg_lib.REGISTRY_PATH = original

    def test_style_equivalent_unchanged_by_registry(self):
        """签发门用的等价判定保持既有语义（A2 ≡ 仙侠奇幻风，A2 ≢ H12）。"""
        from style import style_equivalent
        self.assertTrue(style_equivalent("A2", "仙侠奇幻风"))
        self.assertFalse(style_equivalent("A2", "H12"))


if __name__ == "__main__":
    unittest.main()
