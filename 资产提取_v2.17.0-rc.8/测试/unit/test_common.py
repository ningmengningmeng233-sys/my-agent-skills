#!/usr/bin/env python3
"""Unit tests for engine/common.py utilities."""
import tempfile
import unittest
from pathlib import Path

from common import (
    number_lines,
    parse_frontmatter,
    range_label,
    read_text_auto,
    safe_name,
    split_template,
)

SKILL_DIR = Path(__file__).resolve().parent.parent.parent


class TestParseFrontmatter(unittest.TestCase):
    def test_parses_frontmatter(self):
        text = "---\n小说名称: 测试\n作者: 某人\n---\n正文第一行\n"
        meta, body = parse_frontmatter(text)
        self.assertEqual(meta["小说名称"], "测试")
        self.assertEqual(meta["作者"], "某人")
        self.assertEqual(body.strip(), "正文第一行")

    def test_no_frontmatter(self):
        text = "只有正文\n"
        meta, body = parse_frontmatter(text)
        self.assertEqual(meta, {})
        self.assertEqual(body, text)


class TestNumberLines(unittest.TestCase):
    def test_prefixes_lines(self):
        text = "a\nb\nc"
        self.assertEqual(number_lines(text), "00001: a\n00002: b\n00003: c")


class TestRangeLabel(unittest.TestCase):
    def test_chapter_range(self):
        self.assertEqual(range_label("全流程测试-流测-第1章~第5章-001.md"), "第1章~第5章")

    def test_single_chapter(self):
        self.assertEqual(range_label("某书-第10章.md"), "第10章")

    def test_fallback_to_name(self):
        self.assertEqual(range_label("unknown.md"), "unknown.md")


class TestSafeName(unittest.TestCase):
    def test_removes_illegal_chars(self):
        self.assertEqual(safe_name('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_empty_defaults(self):
        self.assertEqual(safe_name(None), "未命名")
        self.assertEqual(safe_name(""), "未命名")


class TestReadTextAuto(unittest.TestCase):
    def test_utf8(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.md"
            p.write_text("utf-8 文本", encoding="utf-8")
            self.assertEqual(read_text_auto(p), "utf-8 文本")

    def test_gbk_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "b.md"
            p.write_bytes("gbk 文本".encode("gbk"))
            self.assertEqual(read_text_auto(p), "gbk 文本")


class TestSplitTemplate(unittest.TestCase):
    def test_splits_real_template(self):
        system, user = split_template(SKILL_DIR / "templates" / "描述补全模板.md")
        self.assertTrue(system)
        self.assertTrue(user)

    def test_invalid_template_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.md"
            p.write_text("no sections", encoding="utf-8")
            with self.assertRaises(ValueError):
                split_template(p)


if __name__ == "__main__":
    unittest.main()
