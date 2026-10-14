#!/usr/bin/env python3
"""AE-01: 分段任务指纹失效必须强制重扫，禁止复用陈旧候选表（mock LLM，无外呼）。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

from scan_registry import candidate_one
from engine.index_db import IndexDB

SEG_V1 = """---
小说名称: 《测试小说》
作者: 测试作者
章节范围: 第1章
---
第一版正文：阿罗走进茶楼，要了一壶碧螺春。
"""

SEG_V2 = """---
小说名称: 《测试小说》
作者: 测试作者
章节范围: 第1章
---
第二版正文（已改写）：阿罗走进茶楼，又折返后院柴房。
"""

STALE_CANDIDATE = "| 候选主名 | 别名 | 首现章 | 一句定性 | 类别 |\n| 阿罗旧 | | 第1章 | 旧候选存档 | 人物 |\n"
FRESH_CANDIDATE = "## 候选主名表\n| 候选主名 | 别名 | 首现章 | 一句定性 | 类别 |\n| 阿罗 | | 第1章 | 新候选重扫 | 人物 |\n"


class TestStaleCandidateInvalidation(unittest.TestCase):
    def _make_lib(self, td: str) -> tuple[Path, Path, Path]:
        lib = Path(td) / "lib"
        seg_dir = lib / "原文段" / "测试小说-测试作者"
        seg_dir.mkdir(parents=True)
        seg = seg_dir / "段01.md"
        seg.write_text(SEG_V1, encoding="utf-8")
        out_dir = lib / "登记表" / "测试小说-测试作者" / "候选表"
        out_dir.mkdir(parents=True)
        return lib, seg, out_dir

    def test_fingerprint_mismatch_forces_rescan(self):
        """分段输入已变（指纹失效）+ 旧候选表仍在：必须重扫，不得复用旧表。"""
        with tempfile.TemporaryDirectory() as td:
            lib, seg, out_dir = self._make_lib(td)
            out_path = out_dir / "段01-候选表.md"
            out_path.write_text(STALE_CANDIDATE, encoding="utf-8")
            self.assertGreater(out_path.stat().st_size, 50)
            db = IndexDB(lib, table="registry_tasks")
            self.assertTrue(db.ready)
            try:
                # 旧断点记录：内容哈希与节点指纹均为陈旧值
                db.mark_done(str(seg), str(out_path), 0, 0,
                             seg_hash="deadbeefdeadbeef", node_fp="stale-fingerprint")
                # 模拟分段输入变更 → 当前指纹与断点记录必然不一致
                seg.write_text(SEG_V2, encoding="utf-8")
                llm_result = (FRESH_CANDIDATE, {"prompt_tokens": 1, "completion_tokens": 1})
                with mock.patch("scan_registry.call_llm", return_value=llm_result) as m:
                    out, st = candidate_one(str(seg), out_dir, False, db)
                self.assertEqual(st, "done")
                m.assert_called_once()
                self.assertEqual(out, str(out_path))
                self.assertIn("新候选重扫", out_path.read_text(encoding="utf-8"))
            finally:
                db.close()

    def test_missing_checkpoint_with_stale_file_forces_rescan(self):
        """无断点记录但旧候选文件存在（指纹无法校验）：必须重扫，不得盲目复用。"""
        with tempfile.TemporaryDirectory() as td:
            lib, seg, out_dir = self._make_lib(td)
            out_path = out_dir / "段01-候选表.md"
            out_path.write_text(STALE_CANDIDATE, encoding="utf-8")
            self.assertGreater(out_path.stat().st_size, 50)
            db = IndexDB(lib, table="registry_tasks")
            self.assertTrue(db.ready)
            try:
                self.assertIsNone(db.get(str(seg)))
                llm_result = (FRESH_CANDIDATE, {"prompt_tokens": 1, "completion_tokens": 1})
                with mock.patch("scan_registry.call_llm", return_value=llm_result) as m:
                    out, st = candidate_one(str(seg), out_dir, False, db)
                self.assertEqual(st, "done")
                m.assert_called_once()
                self.assertIn("新候选重扫", out_path.read_text(encoding="utf-8"))
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
