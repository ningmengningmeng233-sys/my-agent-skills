#!/usr/bin/env python3
"""AE-02: 候选表合并失败必须如实保留 b 表条目，杜绝资产静默丢失（mock LLM，无外呼）。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

from scan_registry import merge_round

TABLE_A = (
    "| 候选主名 | 别名 | 首现章 | 一句定性 | 类别 |\n"
    "|---|---|---|---|---|\n"
    "| 陈阿甲 | | 第1章 | 甲表独有资产 | 人物 |\n"
)
TABLE_B = (
    "| 候选主名 | 别名 | 首现章 | 一句定性 | 类别 |\n"
    "|---|---|---|---|---|\n"
    "| 陈阿乙 | | 第2章 | 乙表独有资产 | 人物 |\n"
)


class TestMergeFailurePreservesBothTables(unittest.TestCase):
    def test_failed_pair_keeps_b_entries(self):
        """合并抛错时：输出集合必须同时包含 a/b 两表条目，且 carry 计数如实记录。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            a, b = d / "a-候选表.md", d / "b-候选表.md"
            a.write_text(TABLE_A, encoding="utf-8")
            b.write_text(TABLE_B, encoding="utf-8")
            merge_dir = d / "合并"
            merge_dir.mkdir()
            with mock.patch("scan_registry.call_llm",
                            side_effect=RuntimeError("mock LLM 熔断")):
                files, carries = merge_round(1, [a, b], merge_dir, False, workers=1)
            self.assertEqual(carries, 1)
            self.assertTrue(files)
            combined = "\n".join(p.read_text(encoding="utf-8") for p in files)
            self.assertIn("陈阿甲", combined)
            self.assertIn("陈阿乙", combined)

    def test_failed_pair_writes_isolated_carry_file(self):
        """回退产物必须落盘为合并目录下的独立文件，而非直接复用 a 表路径。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            a, b = d / "a-候选表.md", d / "b-候选表.md"
            a.write_text(TABLE_A, encoding="utf-8")
            b.write_text(TABLE_B, encoding="utf-8")
            merge_dir = d / "合并"
            merge_dir.mkdir()
            with mock.patch("scan_registry.call_llm",
                            side_effect=RuntimeError("mock LLM 熔断")):
                files, _carries = merge_round(1, [a, b], merge_dir, False, workers=1)
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].parent, merge_dir)
            self.assertNotEqual(files[0].resolve(), a.resolve())


if __name__ == "__main__":
    unittest.main()
