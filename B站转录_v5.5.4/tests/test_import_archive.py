"""import_archive.py 的行为测试：BV 来源链、页号判定、去重、不覆盖契约。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from scripts import import_archive
from tests.support import SkillTestCase, make_raw_file


def run(self, *extra, **kwargs):
    args = ["--json", "--db", str(kwargs.get("db", self.db)),
            "--root", str(kwargs.get("root", self.archive)), *extra]
    code, out = self.capture(import_archive.main, args)
    return code, json.loads(out.strip().splitlines()[-1])


class BvSourceTests(SkillTestCase):
    def test_bv_from_filename(self):
        make_raw_file(self.archive / "作品_BV1aaaaaaaaa.txt", bvid="BV1aaaaaaaaa")
        _, report = run(self, "--dry-run")
        self.assertEqual(report["to_insert"], 1)

    def test_bv_from_header_link_when_filename_has_none(self):
        make_raw_file(self.archive / "P01.txt", bvid="BV1bbbbbbbbb", page=1,
                      link_bv="BV1bbbbbbbbb")
        _, report = run(self, "--dry-run")
        self.assertEqual(report["to_insert"], 1)

    def test_bv_from_parent_folder_when_name_and_header_lack_it(self):
        path = self.archive / "课程合集_某位UP_BV1ccccccccc" / "P03.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        make_raw_file(path, bvid="BV1ccccccccc", page=3, link_bv="")
        _, report = run(self, "--dry-run")
        self.assertEqual(report["to_insert"], 1)
        self.assertEqual(report["skipped_invalid"], 0)

    def test_file_without_any_bv_is_invalid(self):
        path = self.archive / "无名.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("没有任何 BV 的文本", encoding="utf-8")
        _, report = run(self, "--dry-run")
        self.assertEqual(report["skipped_invalid"], 1)


class PageResolutionTests(SkillTestCase):
    def test_page_from_filename_suffix(self):
        path = self.archive / "作品_BV1aaaaaaaaa_P07.txt"
        make_raw_file(path, bvid="BV1aaaaaaaaa", page=7)
        records, invalid, excluded = import_archive.scan(self.archive)
        self.assertEqual(records[("BV1aaaaaaaaa", 7)][0]["page_source"], "name")

    def test_page_from_header_field(self):
        make_raw_file(self.archive / "P05.txt", bvid="BV1aaaaaaaaa", page=5)
        records, _, _ = import_archive.scan(self.archive)
        self.assertEqual(records[("BV1aaaaaaaaa", 5)][0]["page_source"], "header")

    def test_page_defaults_to_one_and_is_reported(self):
        make_raw_file(self.archive / "老格式.txt", bvid="BV1aaaaaaaaa", page=None)
        records, _, _ = import_archive.scan(self.archive)
        self.assertEqual(records[("BV1aaaaaaaaa", 1)][0]["page_source"], "default")

    def test_defaulted_page_with_siblings_is_listed_as_risk(self):
        make_raw_file(self.archive / "老格式.txt", bvid="BV1aaaaaaaaa", page=None)
        make_raw_file(self.archive / "P02.txt", bvid="BV1aaaaaaaaa", page=2)
        _, report = run(self, "--dry-run")
        self.assertEqual(report["page_defaulted"], 1)


class SelectionTests(SkillTestCase):
    def test_longer_body_wins(self):
        make_raw_file(self.archive / "短_BV1aaaaaaaaa.txt", bvid="BV1aaaaaaaaa",
                      body="短正文")
        make_raw_file(self.archive / "长_BV1aaaaaaaaa.txt", bvid="BV1aaaaaaaaa",
                      body="这是一个明显更长的正文内容" * 5)
        records, _, _ = import_archive.scan(self.archive)
        chosen, multi = import_archive.choose(records[("BV1aaaaaaaaa", 1)])
        self.assertTrue(multi)
        self.assertGreater(len(chosen["text"]), 10)

    def test_equal_length_prefers_newer_transcription_time(self):
        make_raw_file(self.archive / "旧_BV1aaaaaaaaa.txt", bvid="BV1aaaaaaaaa",
                      body="等长正文甲", transcribed_at="2026-01-01 00:00:00")
        make_raw_file(self.archive / "新_BV1aaaaaaaaa.txt", bvid="BV1aaaaaaaaa",
                      body="等长正文乙", transcribed_at="2026-09-09 09:09:09")
        records, _, _ = import_archive.scan(self.archive)
        chosen, _ = import_archive.choose(records[("BV1aaaaaaaaa", 1)])
        self.assertEqual(chosen["transcribed_at"], "2026-09-09 09:09:09")

    def test_corrected_copy_is_excluded_not_invalid(self):
        make_raw_file(self.archive / "原稿_BV1aaaaaaaaa.txt", bvid="BV1aaaaaaaaa")
        make_raw_file(self.archive / "校对版" / "x--deadbeef_校对版.txt",
                      bvid="BV1aaaaaaaaa", body="校对稿")
        _, report = run(self, "--dry-run")
        self.assertEqual(report["skipped_corrected"], 1)
        self.assertEqual(report["skipped_invalid"], 0)
        self.assertEqual(report["to_insert"], 1)


class WriteContractTests(SkillTestCase):
    def test_existing_key_is_never_overwritten(self):
        self.add_row("BV1aaaaaaaaa", transcript_text="库里已有的正文")
        make_raw_file(self.archive / "作品_BV1aaaaaaaaa.txt", bvid="BV1aaaaaaaaa",
                      body="归档里的正文")
        _, report = run(self, "--apply")
        self.assertEqual(report["to_insert"], 0)
        self.assertEqual(report["skipped_existing"], 1)
        conn = sqlite3.connect(str(self.db))
        try:
            text = conn.execute("SELECT transcript_text FROM transcripts"
                                " WHERE bvid='BV1aaaaaaaaa'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(text, "库里已有的正文")

    def test_apply_inserts_and_writes_backup(self):
        make_raw_file(self.archive / "作品_BV1aaaaaaaaa.txt", bvid="BV1aaaaaaaaa",
                      body="归档里的正文")
        _, report = run(self, "--apply")
        self.assertEqual(report["inserted"], 1)
        self.assertEqual(report["rows_after"], 1)
        self.assertTrue(Path(report["backup"]).is_file())
        conn = sqlite3.connect(str(self.db))
        try:
            row = conn.execute(
                "SELECT transcript_text, summary, status, created_at, transcript_file"
                " FROM transcripts WHERE bvid='BV1aaaaaaaaa'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "归档里的正文")
        self.assertTrue(row[1])
        self.assertEqual(row[2], "summarized")
        self.assertEqual(row[3], "2026-02-03 04:05:06")
        self.assertTrue(Path(row[4]).is_file())

    def test_second_apply_is_idempotent(self):
        make_raw_file(self.archive / "作品_BV1aaaaaaaaa.txt", bvid="BV1aaaaaaaaa")
        run(self, "--apply")
        _, report = run(self, "--apply")
        self.assertEqual(report["to_insert"], 0)
        self.assertEqual(report["inserted"], 0)
        self.assertEqual(report["rows_after"], 1)

    def test_dry_run_writes_nothing(self):
        make_raw_file(self.archive / "作品_BV1aaaaaaaaa.txt", bvid="BV1aaaaaaaaa")
        _, report = run(self, "--dry-run")
        self.assertEqual(report["inserted"], 0)
        self.assertEqual(report["rows_after"], 0)

    def test_report_file_is_written(self):
        make_raw_file(self.archive / "作品_BV1aaaaaaaaa.txt", bvid="BV1aaaaaaaaa")
        target = self.root / "报告.md"
        self.run_quiet(import_archive.main,
                       ["--dry-run", "--db", str(self.db), "--root", str(self.archive),
                        "--report", str(target)])
        self.assertTrue(target.is_file())
        content = target.read_text(encoding="utf-8")
        self.assertIn("归档补录报告", content)
        self.assertIn("页号兜底清单", content)


if __name__ == "__main__":
    unittest.main()
