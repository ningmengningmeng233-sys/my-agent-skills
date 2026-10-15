#!/usr/bin/env python3
"""Boundary tests for the costume design mechanical checker (no LLM)."""
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SKILL_DIR / "服饰设计" / "scripts"))

from check_costumes import (  # noqa: E402
    check_role,
    load_standard_lib,
    main_color_hex,
    parse_hex,
    split_roles,
)


class TestParseHex(unittest.TestCase):
    def test_extracts_uppercase_hex_only(self):
        self.assertEqual(parse_hex("#1f2937 #F5F0E6 #GGG"), {"1F2937", "F5F0E6"})


class TestLoadStandardLib(unittest.TestCase):
    def test_blacklist_from_colon_lines_only(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "lib.md"
            p.write_text(
                "## 配色板\n`#1F2937`、`#F5F0E6`\n"
                "黑名单（红线）\n"
                "黑名单并集（民国 ∪ 悬疑）：机甲、外骨骼\n"
                "用户额外禁忌：露肩装\n",
                encoding="utf-8",
            )
            palette, blacklist = load_standard_lib(p)
            self.assertEqual(palette, {"1F2937", "F5F0E6"})
            self.assertIn("机甲", blacklist)
            self.assertIn("外骨骼", blacklist)
            self.assertIn("露肩装", blacklist)
            self.assertNotIn("红线", blacklist)


class TestSplitRoles(unittest.TestCase):
    def test_fallback_when_no_role_heading(self):
        self.assertEqual(split_roles("无角色标题正文")[0][0], "整文")

    def test_splits_multiple_roles(self):
        text = "## 角色 1/2：甲\n内容甲\n## 角色 2/2：乙\n内容乙"
        roles = split_roles(text)
        self.assertEqual([n for n, _ in roles], ["角色 1/2：甲", "角色 2/2：乙"])


class TestMainColorHex(unittest.TestCase):
    def test_primary_color_detected(self):
        self.assertEqual(main_color_hex("主玄青 `#1F2937`、次月白 `#F5F0E6`"), "1F2937")

    def test_secondary_color_not_mistaken_for_primary(self):
        self.assertIsNone(main_color_hex("主色未定，次色 `#F5F0E6`"))


class TestCheckRole(unittest.TestCase):
    def test_blacklist_hit_is_blocker(self):
        findings = check_role("甲", "身穿机甲", set(), {"机甲"})
        self.assertTrue(any(sev == "阻断" and "机甲" in desc for sev, desc in findings))

    def test_negative_declaration_is_not_a_hit(self):
        findings = check_role(
            "甲",
            "6.5 头身，正面、侧面、背面三视图齐全，严禁机甲、禁止西装",
            set(),
            {"机甲", "西装"},
        )
        self.assertEqual(findings, [])

    def test_out_of_palette_color_warns(self):
        findings = check_role("甲", "主色 `#123456`", {"123456"}, set())
        self.assertFalse(any(sev == "警告" and "配色出界" in desc for sev, desc in findings))

    def test_missing_three_views_warns(self):
        findings = check_role("甲", "6.5 头身，主色 `#123456`", {"123456"}, set())
        self.assertTrue(any(sev == "警告" and "正面/侧面/背面" in desc for sev, desc in findings))


if __name__ == "__main__":
    unittest.main()
