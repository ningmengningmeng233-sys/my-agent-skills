"""render_delivery.py 的行为测试：双文件交付、原始记录内容、dry-run 无副作用。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from scripts import render_delivery
from tests.support import SkillTestCase, make_raw_file


def seed_versions(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transcript_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            bvid TEXT NOT NULL,
            page INTEGER NOT NULL DEFAULT 1,
            version_no INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'raw',
            source_file TEXT NOT NULL DEFAULT '',
            source_hash TEXT NOT NULL DEFAULT '',
            transcript_text TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            is_current INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(bvid, page, version_no)
        )
        """
    )
    conn.execute(
        "INSERT INTO transcript_versions (bvid, page, version_no, kind, source_file,"
        " source_hash, transcript_text, summary, is_current, created_at)"
        " VALUES ('BV1aaaaaaaaa', 1, 1, 'raw', 'C:/raw.txt', '', '原始稿正文。',"
        " '原始摘要。', 1, '2026-02-03 04:05:06')")
    conn.execute(
        "INSERT INTO transcript_versions (bvid, page, version_no, kind, source_file,"
        " source_hash, transcript_text, summary, is_current, created_at)"
        " VALUES ('BV1aaaaaaaaa', 1, 2, 'corrected',"
        " 'C:/校对版/x--deadbeef_校对版.txt', 'deadbeef', '校对后正文。',"
        " '校对摘要。', 0, '2026-03-04 05:06:07')")
    conn.commit()
    conn.close()


class RenderDeliveryTests(SkillTestCase):
    def setUp(self):
        super().setUp()
        self.add_row("BV1aaaaaaaaa")
        seed_versions(self.db)
        self.out = self.root / "deliver"

    def render(self, *extra):
        args = ["--json", "--db", str(self.db), "--out", str(self.out), *extra]
        code, out = self.capture(render_delivery.main, args)
        return code, json.loads(out.strip().splitlines()[-1])

    def test_renders_main_and_evidence_files(self):
        _, report = self.render("--bvid", "BV1aaaaaaaaa")
        self.assertEqual(report["rendered"], 1)
        item = report["results"][0]
        main, evidence = Path(item["main_file"]), Path(item["evidence_file"])
        self.assertTrue(main.is_file())
        self.assertTrue(evidence.is_file())
        self.assertTrue(evidence.name.endswith("_原始记录.txt"))

    def test_main_file_keeps_legacy_naming_and_payload(self):
        _, report = self.render("--bvid", "BV1aaaaaaaaa")
        main = Path(report["results"][0]["main_file"])
        self.assertIn("BV1aaaaaaaaa", main.name)
        self.assertIn("_P01", main.name)
        content = main.read_text(encoding="utf-8")
        self.assertIn("第一部分：视频摘要", content)
        self.assertIn("第二部分：完整原文", content)
        # 有校对版时主文件交付校对版正文，并给出交付说明
        self.assertIn("校对后正文。", content)
        self.assertIn("交付说明", content)

    def test_evidence_lists_every_version_with_provenance(self):
        _, report = self.render("--bvid", "BV1aaaaaaaaa")
        content = Path(report["results"][0]["evidence_file"]).read_text(encoding="utf-8")
        self.assertIn("原始记录（可追溯证据）", content)
        self.assertIn("第一部分：版本清单", content)
        self.assertIn("第二部分：各版本全文", content)
        self.assertIn("原始稿（当前）", content)
        self.assertIn("校对版", content)
        self.assertIn("deadbeef", content)
        self.assertIn("原始稿正文。", content)
        self.assertIn("校对后正文。", content)

    def test_evidence_reports_empty_version_table(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("DELETE FROM transcript_versions")
        conn.commit()
        conn.close()
        _, report = self.render("--bvid", "BV1aaaaaaaaa")
        content = Path(report["results"][0]["evidence_file"]).read_text(encoding="utf-8")
        self.assertIn("版本表为空", content)

    def test_dry_run_writes_nothing(self):
        _, report = self.render("--bvid", "BV1aaaaaaaaa", "--dry-run")
        self.assertEqual(report["rendered"], 1)
        self.assertTrue(report["results"][0]["dry_run"])
        self.assertFalse(self.out.exists())

    def test_dry_run_tolerates_missing_file_path(self):
        """库中未记录文件路径时，预览不能崩。"""
        conn = sqlite3.connect(str(self.db))
        conn.execute("UPDATE transcripts SET transcript_file=''")
        conn.commit()
        conn.close()
        _, report = self.render("--bvid", "BV1aaaaaaaaa", "--dry-run")
        self.assertEqual(report["rendered"], 1)

    def test_main_file_falls_back_to_raw_when_no_corrected(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("DELETE FROM transcript_versions WHERE kind='corrected'")
        conn.commit()
        conn.close()
        _, report = self.render("--bvid", "BV1aaaaaaaaa")
        content = Path(report["results"][0]["main_file"]).read_text(encoding="utf-8")
        self.assertIn("这是正文。", content)
        self.assertFalse("交付说明" in content)
        self.assertEqual(report["results"][0]["delivered_version"], "v1（原始稿）")

    def test_missing_key_is_reported_as_failure(self):
        code, report = self.render("--bvid", "BV1zzzzzzzzz")
        self.assertEqual(report["rendered"], 0)
        self.assertEqual(code, 1)
        self.assertIn("库中无此记录", report["failed"][0]["error"])

    def test_all_with_limit_processes_subset(self):
        self.add_row("BV1bbbbbbbbb")
        _, report = self.render("--all", "--limit", "1")
        self.assertEqual(report["requested"], 1)

    def test_requires_target_argument(self):
        with self.assertRaises(SystemExit):
            self.run_quiet(render_delivery.main, ["--dry-run"])

    def test_missing_database_returns_two(self):
        code, _ = self.capture(render_delivery.main,
                               ["--bvid", "BV1aaaaaaaaa", "--json",
                                "--db", str(self.root / "nope.db")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
