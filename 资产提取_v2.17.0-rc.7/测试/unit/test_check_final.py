#!/usr/bin/env python3
"""Unit tests for check_final.py module completeness checks."""
import tempfile
import unittest
from pathlib import Path

from check_final import check_modules


REQUIRED = [
    "## 一、项目基础信息表",
    "## 人物资产提取表",
    "## 场景资产提取表",
    "## 道具资产提取表",
    "## 服装妆造资产提取表",
    "## 六、分集/章节资产使用表",
    "## 七、导入资产总表",
    "## 八、文件夹结构建议",
    "## 衍生需求汇总",
    "## 合并统计",
    "## 待确认附录",
    "## 九、交付物说明",
]


class TestCheckModules(unittest.TestCase):
    def test_all_modules_present(self):
        text = "\n".join(REQUIRED)
        self.assertEqual(check_modules(text), [])

    def test_missing_modules_reported(self):
        text = "## 一、项目基础信息表\n## 人物资产提取表"
        missing = check_modules(text)
        self.assertIn("场景资产提取表", missing)
        self.assertIn("待确认附录", missing)

    def test_body_mention_is_not_a_heading(self):
        text = "正文提到人物资产提取表但不是标题"
        missing = check_modules(text)
        self.assertIn("人物资产提取表", missing)

    def test_check_modules_from_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "final.md"
            p.write_text("\n".join(REQUIRED), encoding="utf-8")
            self.assertEqual(check_modules(p), [])


if __name__ == "__main__":
    unittest.main()
