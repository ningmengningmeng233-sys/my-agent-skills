#!/usr/bin/env python3
"""Unit tests for split_segments chapter detection (ADR-0010)."""
import unittest

from split_segments import cn2ar, find_chapter_lines, range_label
import tempfile
from pathlib import Path
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

SKILL = Path(__file__).resolve().parent.parent.parent


class TestStaleSegmentGate(unittest.TestCase):
    """ADR-0081 冒烟回归：源文件章节范围变化时，旧段文件必须被拦截/清理，不许静默残留。"""

    SRC_V1 = "第一章 试炼\n林姑娘走进茶楼。\n第二章 琴声\n琴声穿过天井。\n第三章 码头\n船夫在码头。\n"
    SRC_V2 = "第一章 试炼\n林姑娘走进茶楼喝茶。\n第二章 琴声\n琴声穿过天井。\n"

    def _split(self, src_path: str, lib: Path, extra: list[str]) -> int:
        r = run_py(
                [str(SKILL / "scripts" / "split_segments.py"), src_path, "--novel", "冒烟回归", "--author", "测", "--lib", str(lib)] + extra,
        )
        return r.returncode

    def _write_src(self, td: str, content: str) -> str:
        src_path = str(Path(td) / "源文件.txt")
        Path(src_path).write_text(content, encoding="utf-8")
        return src_path

    def test_range_change_blocks_then_force_prunes(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "资产库"
            src = self._write_src(td, self.SRC_V1)
            self.assertEqual(self._split(src, lib, []), 0)
            seg_dir = lib / "原文段" / "冒烟回归-测"
            self.assertTrue((seg_dir / "冒烟回归-测-第1章~第3章-001.md").exists())
            # 章节范围变化（3章→2章）：不带 --force 必须拦截（rc=1）
            self._write_src(td, self.SRC_V2)
            self.assertEqual(self._split(src, lib, []), 1)
            self.assertTrue((seg_dir / "冒烟回归-测-第1章~第3章-001.md").exists())
            # --force 重跑：自动清理过期段，只留新方案
            self.assertEqual(self._split(src, lib, ["--force"]), 0)
            names = [p.name for p in seg_dir.glob("*.md")]
            self.assertEqual(names, ["冒烟回归-测-第1章~第2章-001.md"])

    def test_idempotent_rerun_passes(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "资产库"
            src = self._write_src(td, self.SRC_V1)
            self.assertEqual(self._split(src, lib, []), 0)
            self.assertEqual(self._split(src, lib, []), 0)  # 幂等重跑无过期段


class TestScreenplaySceneMarkers(unittest.TestCase):
    """剧本为正式输入（规格 2.2）：场次标记必须可识别，且不得误吞散文句。"""

    def test_scene_marker_recognized(self):
        lines = ["第一场 茶楼", "正文内容", "第二场 长街", "正文内容", "第十场 决战"]
        got = find_chapter_lines(lines)
        self.assertEqual([c[2] for c in got], ["第1场", "第2场", "第10场"])

    def test_scene_marker_needs_delimiter(self):
        # 「第一场雪」是散文而非场次标题：场后必须跟空白/标点/行尾才算
        lines = ["第一场雪落了下来", "正文内容", "第五场戏开始了", "正文内容"]
        self.assertEqual(find_chapter_lines(lines), [])


class TestNormalizedSourceIntegration(unittest.TestCase):
    """工单01-Task4：非 TXT/MD 或多文件先规范化，段 frontmatter 同时写原始来源定位。"""

    DOCX = SKILL / "测试" / "fixtures" / "sources" / "最小剧本.docx"
    EPUB = SKILL / "测试" / "fixtures" / "sources" / "最小小说.epub"

    def _split(self, sources: list[Path], lib: Path, extra: list[str] | None = None):
        return run_py(
                [str(SKILL / "scripts" / "split_segments.py"), *[str(s) for s in sources], "--novel", "规范化", "--author", "测", "--lib", str(lib)] + (extra or []),
        )

    def test_docx_source_normalized_then_split_by_scene(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "资产库"
            r = self._split([self.DOCX], lib)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            norm = lib / ".process" / "normalized_sources"
            self.assertTrue((norm / "规范化原文.md").is_file())
            self.assertTrue((norm / "来源清单.json").is_file())
            segs = sorted((lib / "原文段" / "规范化-测").glob("*.md"))
            self.assertEqual(len(segs), 1)
            body = segs[0].read_text(encoding="utf-8")
            self.assertIn("第一场 茶楼", body)
            self.assertIn("第二场 长街", body)
            # 按场切分（剧本的"章"就是场），而不是退化成按字数切分
            self.assertIn("第1场", segs[0].name)
            self.assertIn("原始来源定位", body)
            self.assertIn("来源清单", body)
            self.assertIn("第一场 茶楼/标题", body)

    def test_epub_source_normalized_then_split(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "资产库"
            r = self._split([self.EPUB], lib)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((lib / ".process" / "normalized_sources" / "规范化原文.md").is_file())
            segs = sorted((lib / "原文段" / "规范化-测").glob("*.md"))
            self.assertTrue(segs)
            body = segs[0].read_text(encoding="utf-8")
            self.assertIn("第一章 茶楼", body)
            self.assertIn("chapter_", body)

    def test_multi_source_uses_explicit_order_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, text in (("卷1.txt", "第一章 甲\n甲出现。\n"), ("卷2.txt", "第二章 乙\n乙出现。\n")):
                (root / name).write_text(text, encoding="utf-8")
            order = root / "顺序清单.txt"
            order.write_text("卷2.txt\n卷1.txt\n", encoding="utf-8")
            lib = root / "资产库"
            r = self._split([root / "卷1.txt", root / "卷2.txt"], lib, ["--order-file", str(order)])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = (lib / ".process" / "normalized_sources" / "规范化原文.md").read_text(encoding="utf-8")
            self.assertLess(text.index("乙出现。"), text.index("甲出现。"))
            segs = sorted((lib / "原文段" / "规范化-测").glob("*.md"))
            self.assertTrue(segs)
            self.assertIn("原始来源定位", segs[0].read_text(encoding="utf-8"))

    def test_pdf_source_rejected_with_reason_code(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdf = root / "剧本.pdf"
            pdf.write_bytes(b"%PDF-1.7\nfake")
            lib = root / "资产库"
            r = self._split([pdf], lib)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("UNSUPPORTED_SOURCE_FORMAT", r.stdout + r.stderr)
            seg_dir = lib / "原文段" / "规范化-测"
            self.assertEqual(list(seg_dir.glob("*.md")) if seg_dir.is_dir() else [], [])

    def test_single_txt_keeps_legacy_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "源文件.txt"
            src.write_text("第一章 开局\n正文内容\n", encoding="utf-8")
            lib = root / "资产库"
            r = self._split([src], lib)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            # 单 TXT 走既有路径：不产生规范化中间产物
            self.assertFalse((lib / ".process" / "normalized_sources").exists())
            segs = sorted((lib / "原文段" / "规范化-测").glob("*.md"))
            self.assertEqual(len(segs), 1)
            body = segs[0].read_text(encoding="utf-8")
            self.assertIn("源文件: 源文件.txt", body)
            self.assertNotIn("原始来源定位", body)


class TestCn2Ar(unittest.TestCase):
    def test_digit(self):
        self.assertEqual(cn2ar("0"), 0)
        self.assertEqual(cn2ar("123"), 123)

    def test_single(self):
        self.assertEqual(cn2ar("一"), 1)
        self.assertEqual(cn2ar("十"), 10)

    def test_two_digits(self):
        self.assertEqual(cn2ar("十二"), 12)
        self.assertEqual(cn2ar("三十五"), 35)
        self.assertEqual(cn2ar("二十"), 20)

    def test_hundred(self):
        self.assertEqual(cn2ar("一百零三"), 103)
        self.assertEqual(cn2ar("一百"), 100)
        self.assertEqual(cn2ar("一千二百三十"), 1230)

    def test_ten_thousand(self):
        self.assertEqual(cn2ar("一万零五"), 10005)
        self.assertEqual(cn2ar("十万"), 100000)
        self.assertEqual(cn2ar("三万二千"), 32000)

    def test_year_style(self):
        self.assertEqual(cn2ar("二〇二四"), 2024)
        self.assertEqual(cn2ar("二三"), 23)

    def test_mixed(self):
        self.assertEqual(cn2ar("一千零821"), 1821)


class TestFindChapterLines(unittest.TestCase):
    def test_standard_chapters(self):
        lines = [
            "第一章 开局",
            "正文内容",
            "第十二章 试炼",
            "正文内容",
            "第1000章 大战",
            "正文内容",
            "第一万章 巅峰",
        ]
        got = find_chapter_lines(lines)
        self.assertEqual([c[2] for c in got], ["第1章", "第12章", "第1000章", "第10000章"])

    def test_various_markers(self):
        lines = [
            "第一回 风起",
            "第二卷 云涌",
            "第三话 相遇",
            "第四集 离别",
        ]
        got = find_chapter_lines(lines)
        self.assertEqual([c[2] for c in got], ["第1回", "第2卷", "第3话", "第4集"])

    def test_english_chapters(self):
        lines = [
            "Chapter 1 Begin",
            "正文",
            "Chapter 12 End",
        ]
        got = find_chapter_lines(lines)
        self.assertEqual([c[2] for c in got], ["第1章", "第12章"])

    def test_special_chapters(self):
        lines = [
            "序章",
            "楔子：开端",
            "番外 后记",
            "终章",
            "尾声",
            "后记",
        ]
        got = find_chapter_lines(lines)
        self.assertEqual(len(got), 6)
        self.assertEqual([c[2] for c in got], ["第序章", "第楔子", "第番外", "第终章", "第尾声", "第后记"])

    def test_ignore_plain_numbered_lists(self):
        lines = [
            "1. 他说今天天气很好",
            "2、她开始行动",
            "3． 这是列表项",
            "正文",
            "第一章 真正的章节",
        ]
        got = find_chapter_lines(lines)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][2], "第1章")

    def test_numbered_list_with_chapter_marker(self):
        lines = [
            "1. 第一章 开局",
            "正文",
        ]
        got = find_chapter_lines(lines)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][2], "第1章")


class TestRangeLabel(unittest.TestCase):
    def test_ascending(self):
        self.assertEqual(range_label("第1章", "第5章"), "第1章~第5章")

    def test_descending(self):
        self.assertEqual(range_label("第5章", "第1章"), "第1章~第5章")

    def test_same(self):
        self.assertEqual(range_label("第3章", "第3章"), "第3章")

    def test_non_numeric(self):
        self.assertEqual(range_label("序章", "第1章"), "序章~第1章")


if __name__ == "__main__":
    unittest.main()
