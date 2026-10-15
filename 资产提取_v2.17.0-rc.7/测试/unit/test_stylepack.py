# -*- coding: utf-8 -*-
"""ADR-0076 风格包 / 材质路由 / 名称归一化 / 负面并集 单元测试。"""
import unittest

from engine.style import (
    build_material_block,
    build_negative_block,
    build_render_lock_text,
    find_card_by_name,
    load_stylepack,
    normalize_style_name,
    parse_style_card,
    resolve_style,
    route_materials,
)


class TestNormalizeMatching(unittest.TestCase):
    def test_3d_normalizes_to_sanwei(self):
        self.assertEqual(normalize_style_name("高精度3D国漫设定集风"), normalize_style_name("高精度三维国漫设定集风"))

    def test_document_alias_hits_h7(self):
        """映射表/文档口径「3D」写法必须命中 H7，不得静默落入自由描述（ADR-0076）。"""
        card, mode = resolve_style("玄幻", "高精度3D国漫设定集风")
        self.assertEqual(mode, "card")
        self.assertEqual(card["id"], "H7")

    def test_version_suffix_hits_pack_cards(self):
        card, mode = resolve_style("玄幻", "3D渲染v8")
        self.assertEqual(mode, "card")
        self.assertEqual(card["id"], "H18")

    def test_free_mode_still_works(self):
        card, mode = resolve_style("玄幻", "自由描述 测试")
        self.assertIsNone(card)
        self.assertEqual(mode, "free")

    def test_alias_matches_old_names(self):
        """旧名/俗称经「别名」行仍可命中（ADR-0076 直白命名兼容层）。"""
        for alias, expected in (("3D渲染v8", "H18"), ("儿童插画", "H12"), ("水墨", "B1"),
                                ("西幻史诗", "H6"), ("中式国漫三维基础风", "A1"), ("CGI", "E2")):
            card = find_card_by_name(alias)
            self.assertIsNotNone(card, alias)
            self.assertEqual(card["id"], expected, alias)

    def test_alias_parsed_from_card(self):
        card = parse_style_card("H12")
        self.assertIn("儿童插画", card["aliases"])
        self.assertIn("绘本", card["aliases"])


class TestStylePack(unittest.TestCase):
    def test_h7_pack_full(self):
        pack = load_stylepack("H7")
        self.assertIsNotNone(pack)
        self.assertEqual(len(pack["views"]), 4)
        self.assertEqual(len(pack["materials"]), 16)
        self.assertTrue(pack["invariants"])
        self.assertTrue(pack["negative"])

    def test_minimal_pack(self):
        pack = load_stylepack("H9")
        self.assertIsNotNone(pack)
        self.assertTrue(pack["negative"])
        self.assertEqual(pack["materials"], {})

    def test_missing_pack_returns_none(self):
        self.assertIsNone(load_stylepack("ZZ"))
        self.assertIsNone(load_stylepack(""))
        self.assertIsNone(load_stylepack(None))

    def test_card_pack_pointer_parsed(self):
        card = parse_style_card("H8")
        self.assertEqual(card["pack"], "H8")
        card7 = parse_style_card("H7")
        self.assertEqual(card7["pack"], "H7")


class TestMaterialRouting(unittest.TestCase):
    def test_character_defaults_and_keywords(self):
        pack = load_stylepack("H7")
        names = route_materials(pack, "人物", "华丽仙甲鎏金护腕，丝绸长裙，精致妆容，泪痣")
        for expected in ("皮肤", "头发", "布料", "金属", "妆面"):
            self.assertIn(expected, names)
        self.assertNotIn("植被", names)

    def test_scene_routes_terrain(self):
        pack = load_stylepack("H7")
        names = route_materials(pack, "场景", "石桥横跨河面，两岸竹林")
        for expected in ("石材", "木材", "植被", "液体"):
            self.assertIn(expected, names)
        self.assertNotIn("皮肤", names)

    def test_cap_limits_materials(self):
        pack = load_stylepack("H7")
        names = route_materials(pack, "场景", "金属石木陶瓷玻璃塑料纸水火橡胶植被妆面皮革")
        self.assertLessEqual(len(names), 8)

    def test_block_contains_only_routed(self):
        pack = load_stylepack("H7")
        block = build_material_block(pack, "道具", "青铜古镜")
        self.assertIn("金属", block)
        self.assertNotIn("植被", block)
        self.assertTrue(block.startswith("【材质定性"))

    def test_no_pack_returns_empty(self):
        self.assertEqual(build_material_block(None, "人物", "丝绸"), "")
        self.assertEqual(route_materials(None, "人物", "丝绸"), [])


class TestNegativeBlock(unittest.TestCase):
    def test_union_and_type_filter(self):
        card = parse_style_card("H7")
        pack = load_stylepack("H7")
        person = build_negative_block(card, pack, "人物")
        # 卡片负面（母版对齐措辞）与风格包负面都必须进并集
        self.assertIn("无真人感光元件写实", person)
        self.assertIn("无蜡质皮肤", person)
        scene = build_negative_block(card, pack, "场景")
        self.assertIn("无真人感光元件写实", scene)
        # 人物面容词不进场景负面（蜡质皮肤属渲染质量词，全类型保留）
        self.assertIn("无蜡质皮肤", person)
        self.assertNotIn("无网红脸", scene)
        self.assertIn("无网红脸", person)

    def test_empty_when_no_sources(self):
        self.assertEqual(build_negative_block(None, None, "人物"), "")


class TestRenderLockText(unittest.TestCase):
    def test_layered_card_returns_layers_verbatim(self):
        card = parse_style_card("H7")
        text = build_render_lock_text(card)
        self.assertTrue(text.startswith("1. 建模层："))
        for layer in card["layers"]:
            self.assertIn(layer, text)

    def test_layerless_card_falls_back_to_qualitative(self):
        card = {"id": "X1", "name": "测试", "zh": "氛围一句话定性", "layers": []}
        self.assertEqual(build_render_lock_text(card), "氛围一句话定性")


class TestViewBlock(unittest.TestCase):
    """ADR-0077 视图定性路由。"""

    def test_character_routes_face_and_body(self):
        from engine.style import build_view_block
        block = build_view_block(load_stylepack("H7"), "人物")
        self.assertIn("人物视图定性", block)
        self.assertIn("面部解剖", block)  # 视图-脸部 正文
        self.assertIn("成人比例", block)  # 视图-全身 正文

    def test_scene_routes_environment(self):
        from engine.style import build_view_block
        block = build_view_block(load_stylepack("H7"), "场景")
        self.assertIn("环境视图定性", block)
        self.assertIn("剪影", block)

    def test_prop_routes_object(self):
        from engine.style import build_view_block
        block = build_view_block(load_stylepack("H7"), "道具")
        self.assertIn("物件视图定性", block)

    def test_no_pack_returns_empty(self):
        from engine.style import build_view_block
        self.assertEqual(build_view_block(None, "场景"), "")
        self.assertEqual(build_view_block(load_stylepack("H9"), "场景"), "")  # 精简包无视图


class TestSceneDuplicates(unittest.TestCase):
    """ADR-0077 场景层次雷同拦截。"""

    @staticmethod
    def _make_lib(td, scene_doc: str):
        from pathlib import Path
        lib = Path(td)
        (lib / "提示词" / "基础").mkdir(parents=True)
        (lib / "提示词" / "衍生").mkdir(parents=True)
        (lib / "提示词" / "基础" / "场景提示词.md").write_text(scene_doc, encoding="utf-8")
        return lib

    def test_duplicate_layers_reported(self):
        import tempfile
        from check_schedule import check_scene_duplicates
        doc = (
            "# 场景基础提示词\n\n## 001. 茶楼\n\n前景青石板庭院，中景木质楼阁，后景远山。单画面。\n\n---\n\n"
            "## 002. 码头\n\n前景青石板庭院，中景木质楼阁，后景远山。单画面。\n"
        )
        with tempfile.TemporaryDirectory() as td:
            lib = self._make_lib(td, doc)
            issues = check_scene_duplicates(lib / "提示词" / "基础", lib / "提示词" / "衍生")
        self.assertTrue(issues)
        self.assertIn("前景", issues[0])

    def test_distinct_layers_pass(self):
        import tempfile
        from check_schedule import check_scene_duplicates
        doc = (
            "# 场景基础提示词\n\n## 001. 茶楼\n\n前景青石板台阶与幌子旗，中景木构楼阁飞檐，后景街市屋脊。单画面。\n\n---\n\n"
            "## 002. 码头\n\n前景系船石桩与缆绳，中景木栈桥与货箱，后景江面帆影。单画面。\n"
        )
        with tempfile.TemporaryDirectory() as td:
            lib = self._make_lib(td, doc)
            issues = check_scene_duplicates(lib / "提示词" / "基础", lib / "提示词" / "衍生")
        self.assertEqual(issues, [])


class TestSectionDuplicates(unittest.TestCase):
    """ADR-0078 内容整节雷同拦截（去掉资产名后归一化比对）。"""

    @staticmethod
    def _make_lib(td, prop_doc: str):
        from pathlib import Path
        lib = Path(td)
        (lib / "提示词" / "基础").mkdir(parents=True)
        (lib / "提示词" / "衍生").mkdir(parents=True)
        (lib / "提示词" / "基础" / "道具提示词.md").write_text(prop_doc, encoding="utf-8")
        return lib

    def test_identical_bodies_reported(self):
        import tempfile
        from check_schedule import check_section_duplicates
        body = "国漫三维道具动态视图规划设定图。{name}，正面图、侧面图、背面图三视图保底，素灰背景#B8B8B8，均匀柔光，物理渲染材质，写实三维渲染，材质纹理清晰，做旧痕迹自然。图中不要有任何文字。负面提示词：无文字、无人物肢体、无复杂场景背景。"
        doc = "# 道具基础提示词\n\n## 001. 拨算盘\n\n" + body.format(name="拨算盘") + "\n\n---\n\n## 002. 琴\n\n" + body.format(name="琴") + "\n"
        with tempfile.TemporaryDirectory() as td:
            lib = self._make_lib(td, doc)
            issues = check_section_duplicates([(lib / "提示词" / "基础", "道具"), (lib / "提示词" / "衍生", "道具")])
        self.assertEqual(len(issues), 1)
        self.assertIn("拨算盘", issues[0])
        self.assertIn("琴", issues[0])

    def test_distinct_bodies_pass(self):
        import tempfile
        from check_schedule import check_section_duplicates
        doc = (
            "# 道具基础提示词\n\n## 001. 拨算盘\n\n"
            "国漫三维道具动态视图规划设定图。拨算盘，正面图、侧面图、背面图三视图保底，紫檀木框体配黄铜包角，算珠油亮包浆只出现在指触部位。图中不要有任何文字。负面提示词：无文字。\n\n---\n\n"
            "## 002. 琴\n\n"
            "国漫三维道具动态视图规划设定图。琴，正面图、侧面图、背面图三视图保底，桐木面板配鹿角琴徽，弦路磨痕集中在按弦区。图中不要有任何文字。负面提示词：无文字。\n"
        )
        with tempfile.TemporaryDirectory() as td:
            lib = self._make_lib(td, doc)
            issues = check_section_duplicates([(lib / "提示词" / "基础", "道具"), (lib / "提示词" / "衍生", "道具")])
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
