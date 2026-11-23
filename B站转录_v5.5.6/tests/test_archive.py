"""归档环节的测试：同名不同内容不再报错，改写版本化文件名且不覆盖旧件。"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from scripts import batch_transcribe as bt
from scripts.transcript_db import TranscriptDB
from tests.support import SkillTestCase


class ArchiveTests(SkillTestCase):
    def setUp(self):
        super().setUp()
        self.add_row("BV1aaaaaaaaa", upload_date="2026-07-10",
                     transcript_text="库里的正文")
        self.out = self.root / "output"
        self.out.mkdir(parents=True, exist_ok=True)
        self.archive = self.root / "archive"
        self.archive.mkdir(parents=True, exist_ok=True)
        self.patch = mock.patch.multiple(
            bt, ARCHIVE_DIR=self.archive, OUTPUT_DIR=self.out,
            TranscriptDB=lambda *a, **k: TranscriptDB(db_path=self.db))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def render(self, text: str, name: str = "作品_BV1aaaaaaaaa_P01.txt") -> Path:
        path = self.out / "2026" / "07" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def file_of_db(self) -> str:
        conn = sqlite3.connect(str(self.db))
        try:
            return conn.execute(
                "SELECT transcript_file FROM transcripts WHERE bvid='BV1aaaaaaaaa'"
            ).fetchone()[0]
        finally:
            conn.close()

    def test_archives_into_year_month_and_updates_db(self):
        source = self.render("新稿正文")
        result = Path(bt.archive_transcript(str(source), "BV1aaaaaaaaa", 1))
        self.assertEqual(result.parent, self.archive / "2026" / "07")
        self.assertTrue(result.is_file())
        self.assertFalse(source.exists(), "归档后源文件应已移走")
        self.assertEqual(self.file_of_db(), str(result))

    def test_identical_copy_is_deduplicated(self):
        """同名同内容：保留既有文件，删掉多余的那份，不产生副本。"""
        first = self.render("同样的正文")
        archived = Path(bt.archive_transcript(str(first), "BV1aaaaaaaaa", 1))
        again = self.render("同样的正文")  # 重新渲染出一份同名同内容
        result = Path(bt.archive_transcript(str(again), "BV1aaaaaaaaa", 1))
        self.assertEqual(result, archived)
        self.assertFalse(again.exists())
        self.assertEqual(len(list((self.archive / "2026" / "07").iterdir())), 1)

    def test_conflicting_content_gets_versioned_name(self):
        """同名不同内容：写入 _v2，旧文件保留（重转录不再让归档报错）。"""
        first = self.render("旧稿正文")
        archived = Path(bt.archive_transcript(str(first), "BV1aaaaaaaaa", 1))
        second = self.render("重转录后的新稿正文")
        result = Path(bt.archive_transcript(str(second), "BV1aaaaaaaaa", 1))
        self.assertEqual(result.name, archived.stem + "_v2" + archived.suffix)
        self.assertTrue(archived.is_file(), "旧文件必须保留")
        self.assertEqual(archived.read_text(encoding="utf-8"), "旧稿正文")
        self.assertEqual(result.read_text(encoding="utf-8"), "重转录后的新稿正文")
        self.assertEqual(self.file_of_db(), str(result))

    def test_third_conflict_gets_v3(self):
        a = Path(bt.archive_transcript(str(self.render("第一版")), "BV1aaaaaaaaa", 1))
        b = Path(bt.archive_transcript(str(self.render("第二版")), "BV1aaaaaaaaa", 1))
        c = Path(bt.archive_transcript(str(self.render("第三版")), "BV1aaaaaaaaa", 1))
        self.assertEqual([a.name, b.name, c.name],
                         ["作品_BV1aaaaaaaaa_P01.txt",
                          "作品_BV1aaaaaaaaa_P01_v2.txt",
                          "作品_BV1aaaaaaaaa_P01_v3.txt"])

    def test_versioned_destination_skips_taken_names(self):
        target = self.archive / "x.txt"
        self.assertEqual(bt._versioned_destination(target).name, "x_v2.txt")
        (self.archive / "x_v2.txt").write_text("taken", encoding="utf-8")
        self.assertEqual(bt._versioned_destination(target).name, "x_v3.txt")

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            bt.archive_transcript(str(self.out / "nope.txt"), "BV1aaaaaaaaa", 1)


if __name__ == "__main__":
    unittest.main()
