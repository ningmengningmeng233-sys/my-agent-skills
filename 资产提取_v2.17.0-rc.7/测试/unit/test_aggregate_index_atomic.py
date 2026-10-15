#!/usr/bin/env python3
"""AE-03: 最终产物与资产索引原子对齐，索引失败禁止哈希短路（mock LLM，无外呼）。"""
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

from aggregate import main as aggregate_main

GROUP = "测试小说-测试作者"

ORIG_SEG = """---
小说名称: 《测试小说》
作者: 测试作者
章节范围: 第1章
---
阿罗走进茶楼。店小二迎上来，阿罗要了一壶碧螺春。
"""

PROD_RECORD = (
    "| 第1章 | 阿罗进茶楼打尖 | 阿罗 | 茶楼 | 碧螺春 | 布衣 |\n"
    "| 人物 | 阿罗 | | 第1章 | 阿罗走进茶楼 | 00001 | |\n"
)

PERSON_VERDICT = """## 资产裁决
| 组ID | 类型 | 主名 | 等级 | 合并 | 别名 | 绑定 | 处理 | 备注 |
| G01 | 人物 | 阿罗 | 主角 | 否 | | | 正式 | 茶楼初登场 |
## 待确认
| 名称 | 类型 | 说明 |
"""

INFO_VERDICT = """## 项目基础信息
| 字段 | 内容 |
| 项目名称 | 测试小说 |
| 作者 | 测试作者 |
"""

EMPTY_VERDICT = """## 资产裁决
| 组ID | 类型 | 主名 | 等级 | 合并 | 别名 | 绑定 | 处理 | 备注 |
"""


def _fake_llm(system, user_prompt, **kwargs):
    if "块: 人物" in user_prompt:
        return PERSON_VERDICT, {"prompt_tokens": 1, "completion_tokens": 1}
    if "块: 基础信息" in user_prompt:
        return INFO_VERDICT, {"prompt_tokens": 1, "completion_tokens": 1}
    return EMPTY_VERDICT, {"prompt_tokens": 1, "completion_tokens": 1}


class TestFinalIndexAtomicity(unittest.TestCase):
    def _make_fixture(self, td: str) -> tuple[Path, Path, Path]:
        lib = Path(td) / "lib"
        prod = lib / "段产物" / GROUP
        orig = lib / "原文段" / GROUP
        prod.mkdir(parents=True)
        orig.mkdir(parents=True)
        (orig / "段01.md").write_text(ORIG_SEG, encoding="utf-8")
        (prod / "段01-资产记录.md").write_text(PROD_RECORD, encoding="utf-8")
        return lib, prod, orig

    def _run(self, lib: Path, prod: Path, orig: Path, index_mock) -> int:
        argv = ["aggregate.py", str(prod), str(orig), "--lib", str(lib)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch("aggregate.call_llm", side_effect=_fake_llm), \
                index_mock:
            return aggregate_main()

    def test_index_failure_returns_nonzero_without_shortcircuit_hash(self):
        """索引生成失败：必须非零退出且不写短路哈希，不得静默成功。"""
        with tempfile.TemporaryDirectory() as td:
            lib, prod, orig = self._make_fixture(td)
            boom = mock.patch("aggregate.write_asset_index",
                              side_effect=RuntimeError("mock 索引生成失败"))
            rc = self._run(lib, prod, orig, boom)
            self.assertNotEqual(rc, 0)
            self.assertTrue((lib / "反推_最终.md").is_file())
            self.assertFalse((lib / "反推_最终.md.hash").exists())

    def test_rerun_after_index_failure_rebuilds_index(self):
        """索引失败后的重跑：不得被哈希短路，必须如实重试并补齐索引与哈希。"""
        with tempfile.TemporaryDirectory() as td:
            lib, prod, orig = self._make_fixture(td)
            boom = mock.patch("aggregate.write_asset_index",
                              side_effect=RuntimeError("mock 索引生成失败"))
            rc1 = self._run(lib, prod, orig, boom)
            self.assertNotEqual(rc1, 0)
            self.assertFalse((lib / ".process" / "资产索引.json").exists())
            rc2 = self._run(lib, prod, orig, contextlib.nullcontext())
            self.assertEqual(rc2, 0)
            index_path = lib / ".process" / "资产索引.json"
            self.assertTrue(index_path.is_file())
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertTrue([a for a in index.get("assets", [])
                             if a.get("name") == "阿罗"])
            self.assertTrue((lib / "反推_最终.md.hash").is_file())


if __name__ == "__main__":
    unittest.main()
