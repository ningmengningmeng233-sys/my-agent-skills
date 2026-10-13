"""Skill 运行路径单元测试。"""

import unittest
from pathlib import Path

from scripts import paths


class RuntimeDirTests(unittest.TestCase):
    def test_runtime_dirs_are_created_under_skill_root(self):
        paths.ensure_runtime_dirs()
        for directory in (
            paths.DB_PATH.parent,
            paths.OUTPUT_DIR,
            paths.JOBS_DIR,
            paths.LOG_DIR,
            paths.INGEST_STATE_DIR,
        ):
            self.assertTrue(directory.is_dir(), f"{directory} 未被创建")
            # 只断言“挂在 skill 根下”：允许把 .db/output 做成指向别处的目录联接
            # （共享运行时的部署方式），删除安全性由 media.cleanup_job_directory
            # 的 resolve() 边界检查单独保证。
            self.assertTrue(
                Path(directory).is_relative_to(paths.ROOT),
                f"{directory} 不在 Skill 根目录内")

    def test_paths_root_is_skill_directory_not_cwd(self):
        """ROOT 必须由本文件位置推导，且与目录名、当前工作目录无关。"""
        import os

        expected_root = Path(paths.__file__).resolve().parents[1]
        self.assertEqual(paths.ROOT, expected_root)
        self.assertEqual(paths.DB_PATH, expected_root / ".db" / "transcripts.db")

        original = Path.cwd()
        try:
            os.chdir(paths.ROOT.parent)
            self.assertEqual(paths.ROOT, expected_root,
                             "ROOT 不应随工作目录变化")
        finally:
            os.chdir(original)


if __name__ == "__main__":
    unittest.main()
