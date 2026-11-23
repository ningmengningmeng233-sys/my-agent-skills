#!/usr/bin/env python3
"""Unit tests for strict CLI guard and description pure-Chinese check."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate_prompts
from check_schedule import scan_desc_pure_chinese


class TestYesRequiresStyle(unittest.TestCase):
    def test_yes_without_genre_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            args = ["generate_prompts.py", "--deriv", "--lib", td, "--yes", "--style", "仙侠奇幻风"]
            with mock.patch("sys.argv", args):
                self.assertEqual(generate_prompts.main(), 1)

    def test_multiple_actions_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            args = ["generate_prompts.py", "--base", "missing.md", "--deriv", "--genre", "古风", "--lib", td]
            with mock.patch("sys.argv", args):
                self.assertEqual(generate_prompts.main(), 1)

    def test_crop_without_full_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            args = ["generate_prompts.py", "--base", "missing.md", "--genre", "古风", "--lib", td, "--view-mode", "crop"]
            with mock.patch("sys.argv", args):
                self.assertEqual(generate_prompts.main(), 1)

    def test_append_requires_init_schedule(self):
        with tempfile.TemporaryDirectory() as td:
            args = ["generate_prompts.py", "--base", "missing.md", "--genre", "古风", "--lib", td, "--append"]
            with mock.patch("sys.argv", args):
                self.assertEqual(generate_prompts.main(), 1)

    def test_init_schedule_append_only_adds_new_rows(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            final = lib / "反推_最终.md"
            final.write_text("""# 测试
## 服装妆造资产提取表
| 资产ID | 造型名称 | 绑定人物 | 出现位置 | 重要等级 | 备注 |
| c1 | 淡蓝长裙 | 林姑娘 | 第1章 | 重要 | |
| c2 | 蓑衣 | 船夫 | 第2章 | 普通 | |
""", encoding="utf-8")
            args = ["generate_prompts.py", "--init-schedule", str(final), "--genre", "玄幻", "--lib", str(lib)]
            with mock.patch("sys.argv", args):
                self.assertEqual(generate_prompts.main(), 0)
            sched = lib / "提示词" / "调度清单.md"
            text1 = sched.read_text(encoding="utf-8")
            self.assertIn("林姑娘", text1)
            self.assertIn("船夫", text1)
            # 手工删掉船夫行后 append：应把船夫行加回，但林姑娘行保留原样
            sched.write_text(text1.replace("| 人物 | 船夫 | 蓑衣", "| 人物 | 林姑娘 | 淡蓝长裙"), encoding="utf-8")
            args = ["generate_prompts.py", "--init-schedule", str(final), "--genre", "玄幻", "--lib", str(lib), "--append"]
            with mock.patch("sys.argv", args):
                self.assertEqual(generate_prompts.main(), 0)
            text2 = sched.read_text(encoding="utf-8")
            self.assertEqual(text2.count("林姑娘"), 2)  # 原手工行保留 + 追加行？这里实际是保留原行，新行追加
            self.assertIn("船夫", text2)

    def test_yes_without_style_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            args = ["generate_prompts.py", "--deriv", "--genre", "古风", "--lib", td, "--yes"]
            with mock.patch("sys.argv", args):
                self.assertEqual(generate_prompts.main(), 1)


class TestDescPureChinese(unittest.TestCase):
    def test_latin_section_reported(self):
        with tempfile.TemporaryDirectory() as td:
            desc = Path(td) / "人物描述.md"
            desc.write_text(
                "# 人物描述（按序号）\n\n"
                "## 001. 林姑娘\n\n面部骨相鹅蛋脸，肤色白皙肤质细腻。负面限制：无网红脸。\n\n---\n\n"
                "## 002. 船夫\n\n面部骨相方正，肤色偏深。负面限制：无随机换脸。包含 PBR 材质的英文描述。\n\n---\n",
                encoding="utf-8",
            )
            bad = scan_desc_pure_chinese(Path(td))
            self.assertEqual(len(bad), 1)
            self.assertIn("船夫", bad[0][0])

    def test_pure_chinese_descriptions_pass(self):
        with tempfile.TemporaryDirectory() as td:
            desc = Path(td) / "场景描述.md"
            desc.write_text(
                "# 场景描述（按序号）\n\n"
                "## 001. 茶楼\n\n景别：全景；角度：平视；布光：清晨暖光。负面限制：无人物。\n\n---\n",
                encoding="utf-8",
            )
            self.assertEqual(scan_desc_pure_chinese(Path(td)), [])


if __name__ == "__main__":
    unittest.main()
