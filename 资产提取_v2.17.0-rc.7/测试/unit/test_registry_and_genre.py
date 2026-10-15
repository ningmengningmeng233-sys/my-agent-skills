#!/usr/bin/env python3
"""Unit tests for registry checkpoint hashing and drama-type resolution."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

from prepare_costume_input import load_genre_map, resolve_genres
from scan_registry import _merge_pair_hash, merge_pair


class TestMergePairCheckpoint(unittest.TestCase):
    def test_pair_hash_stable_and_input_sensitive(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            a, b, c = d / "a.md", d / "b.md", d / "c.md"
            a.write_text("A", encoding="utf-8")
            b.write_text("B", encoding="utf-8")
            c.write_text("C", encoding="utf-8")
            h1 = _merge_pair_hash(a, b)
            h2 = _merge_pair_hash(a, b)
            h3 = _merge_pair_hash(a, c)
            self.assertEqual(h1, h2)
            self.assertNotEqual(h1, h3)

    def test_merge_skips_when_inputs_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            a, b = d / "a.md", d / "b.md"
            a.write_text("A", encoding="utf-8")
            b.write_text("B", encoding="utf-8")
            out = d / "merge.md"
            out.write_text("x" * 80, encoding="utf-8")
            (d / "merge.md.hash").write_text(_merge_pair_hash(a, b), encoding="utf-8")
            with mock.patch("scan_registry.call_llm") as m:
                result, st = merge_pair(a, b, out, force=False)
            self.assertEqual(st, "skipped")
            self.assertTrue(result)
            m.assert_not_called()

    def test_merge_reruns_when_input_hash_changed(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            a, b = d / "a.md", d / "b.md"
            a.write_text("A", encoding="utf-8")
            b.write_text("B", encoding="utf-8")
            out = d / "merge.md"
            out.write_text("x" * 80, encoding="utf-8")
            (d / "merge.md.hash").write_text("stale-hash", encoding="utf-8")
            content = "## 候选主名表\n| 候选主名 | 别名 | 首现章 | 一句定性 | 类别 |\n|---|---|---|---|---|\n| 测试 | | 第1章 | 定性 | 人物 |"
            with mock.patch("scan_registry.call_llm", return_value=(content, {})) as m:
                result, st = merge_pair(a, b, out, force=False)
            self.assertEqual(st, "done")
            self.assertTrue(result)
            m.assert_called_once()
            self.assertEqual((d / "merge.md.hash").read_text(encoding="utf-8"), _merge_pair_hash(a, b))


class TestResolveGenres(unittest.TestCase):
    def test_mixed_genre_primary_and_secondary(self):
        gmap = load_genre_map()
        gname, approx, secondary = resolve_genres("都市玄幻", gmap)
        self.assertEqual(gname, "都市现代")
        self.assertIn("玄幻仙侠", secondary)

    def test_single_genre_no_secondary(self):
        gmap = load_genre_map()
        gname, approx, secondary = resolve_genres("玄幻", gmap)
        self.assertEqual(gname, "玄幻仙侠")
        self.assertEqual(secondary, [])

    def test_unknown_genre(self):
        gmap = load_genre_map()
        gname, approx, secondary = resolve_genres("不存在剧种XYZ", gmap)
        self.assertIsNone(gname)
        self.assertEqual(secondary, [])

    def test_segment_tag_parsing_in_notes(self):
        from scripts.extract_segments import parse_metadata_tags_from_remark
        note = "手持黑色长剑 [子类:室内空间] [功能:通往密室] [状态:断裂] [关系:CONNECTS_TO=SC_302房间]"
        tags = parse_metadata_tags_from_remark(note)
        self.assertEqual(tags.get("subtype"), "室内空间")
        self.assertEqual(tags.get("story_function"), "通往密室")
        self.assertEqual(tags.get("state"), "断裂")
        self.assertEqual(tags.get("relations"), [{"kind": "CONNECTS_TO", "target": "SC_302房间"}])

    def test_space_tags_only_explicit_content(self):
        from scripts.extract_segments import parse_metadata_tags_from_remark
        note = "[空间结构:走廊尽头右侧为木门] [固定锚点:门内有台灯] [连接点:SC_302房间]"
        tags = parse_metadata_tags_from_remark(note)
        self.assertEqual(tags.get("space_structure"), "走廊尽头右侧为木门")
        self.assertEqual(tags.get("anchor"), "门内有台灯")
        self.assertEqual(tags.get("connects"), ["SC_302房间"])
        # 未写标签即无键，不推断
        self.assertNotIn("entry_exit", tags)
        self.assertNotIn("lighting", tags)


class TestSubtypeTagNormalization(unittest.TestCase):
    """工单01-Task5：段级备注的子类标签必须归一到中文枚举（规格 2.1）。"""

    def test_known_alias_normalized_when_type_known(self):
        from scripts.extract_segments import parse_metadata_tags_from_remark
        tags = parse_metadata_tags_from_remark("手持长剑 [子类:马车]", "道具")
        self.assertEqual(tags["subtype"], "载具")
        self.assertNotIn("subtype_raw", tags)

    def test_unknown_subtype_keeps_raw(self):
        from scripts.extract_segments import parse_metadata_tags_from_remark
        tags = parse_metadata_tags_from_remark("[子类:蒸汽朋克装置]", "道具")
        self.assertEqual(tags["subtype"], "其他")
        self.assertEqual(tags["subtype_raw"], "蒸汽朋克装置")

    def test_without_type_raw_value_preserved(self):
        # 调用方不知道类型时保持旧行为：原样返回，不猜类型
        from scripts.extract_segments import parse_metadata_tags_from_remark
        tags = parse_metadata_tags_from_remark("[子类:载具]")
        self.assertEqual(tags["subtype"], "载具")
        self.assertNotIn("subtype_raw", tags)

    def test_visual_need_tag_parsed(self):
        from scripts.extract_segments import parse_metadata_tags_from_remark
        tags = parse_metadata_tags_from_remark("[视觉需求:天候=雨] [视觉需求:法术=掌心雷]")
        self.assertEqual(tags["visual_needs"], [{"kind": "天候", "value": "雨"},
                                                {"kind": "法术", "value": "掌心雷"}])

    def test_no_visual_need_tag_no_key(self):
        from scripts.extract_segments import parse_metadata_tags_from_remark
        self.assertNotIn("visual_needs", parse_metadata_tags_from_remark("[子类:载具]"))


class TestSegmentTemplateContract(unittest.TestCase):
    """模板与代码契约不得漂移：枚举列表必须与 asset_schema 完全一致。"""

    TEMPLATE = REPO_ROOT / "templates" / "段级提取模板.md"

    def _text(self) -> str:
        return self.TEMPLATE.read_text(encoding="utf-8")

    def _enum_lists(self) -> dict:
        import re
        out = {}
        for line in self._text().splitlines():
            m = re.match(r"^\s*- (人物|场景|道具|服装)：(.*)$", line)
            if m:
                out[m.group(1)] = [v.strip() for v in m.group(2).split("、") if v.strip()]
        return out

    def test_template_exists(self):
        self.assertTrue(self.TEMPLATE.is_file())

    def test_subtype_enums_match_code(self):
        from scripts.engine.asset_schema import ASSET_SUBTYPES
        listed = self._enum_lists()
        self.assertEqual(set(listed), set(ASSET_SUBTYPES), f"模板缺少类型枚举: {listed}")
        for asset_type, values in listed.items():
            # 模板行括注了「（怪物/… → 非人角色）」说明，只比较枚举词本身
            clean = [v.split("（")[0].strip() for v in values]
            self.assertEqual(set(clean), set(ASSET_SUBTYPES[asset_type]),
                             f"{asset_type} 枚举与代码不一致：模板 {clean}")

    def test_visual_need_kinds_match_code(self):
        import re
        from scripts.engine.asset_schema import VISUAL_NEED_KINDS
        m = re.search(r"种类只允许 ([^\n]+)", self._text())
        self.assertIsNotNone(m, "模板未声明视觉需求种类枚举")
        listed = {v.strip() for v in re.split(r"[、,，/]", m.group(1)) if v.strip()}
        self.assertEqual(listed, set(VISUAL_NEED_KINDS))

    def test_five_class_ban_is_stated(self):
        text = self._text()
        self.assertIn("顶层类型只有四类", text)
        self.assertIn("不得作为顶层类型", text)
        self.assertIn("视觉需求不是资产", text)


if __name__ == "__main__":
    unittest.main()
