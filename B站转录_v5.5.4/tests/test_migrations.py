"""migrations.py 的行为测试：schema v2、原始稿回填、校对版版本号分配、幂等。"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from scripts import migrations
from tests.support import SkillTestCase, make_raw_file


def versions_of(db: Path, bvid: str, page: int = 1) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT version_no, kind FROM transcript_versions"
            " WHERE bvid=? AND page=? ORDER BY version_no",
            (bvid, page),
        ).fetchall()
    finally:
        conn.close()


class SchemaTests(SkillTestCase):
    def test_dry_run_does_not_write(self):
        make_raw_file(self.archive / "raw.txt", bvid="BV1aaaaaaaaa")
        self.add_row("BV1aaaaaaaaa")
        code = self.run_quiet(migrations.main, ["--dry-run", "--json", "--db", str(self.db),
                               "--root", str(self.archive)])
        self.assertEqual(code, 0)
        conn = sqlite3.connect(str(self.db))
        try:
            count = conn.execute("SELECT COUNT(*) FROM transcript_versions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0, "dry-run 不得写入任何版本行")

    def test_apply_creates_schema_and_records_version(self):
        self.add_row("BV1aaaaaaaaa")
        make_raw_file(self.archive / "raw.txt", bvid="BV1aaaaaaaaa")
        self.run_quiet(migrations.main, ["--apply", "--json", "--db", str(self.db),
                         "--root", str(self.archive)])
        conn = sqlite3.connect(str(self.db))
        try:
            self.assertTrue(migrations.has_table(conn, "schema_migrations"))
            self.assertEqual(migrations.applied_versions(conn), [migrations.SCHEMA_VERSION])
        finally:
            conn.close()

    def test_raw_backfill_preserves_transcripts_rows(self):
        self.add_row("BV1aaaaaaaaa")
        make_raw_file(self.archive / "raw.txt", bvid="BV1aaaaaaaaa")
        self.run_quiet(migrations.main, ["--apply", "--json", "--db", str(self.db),
                         "--root", str(self.archive)])
        conn = sqlite3.connect(str(self.db))
        try:
            total = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
            text = conn.execute(
                "SELECT transcript_text FROM transcripts WHERE bvid='BV1aaaaaaaaa'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(total, 1)
        self.assertEqual(text, "这是正文。")
        self.assertEqual(versions_of(self.db, "BV1aaaaaaaaa"), [(1, "raw")])

    def test_idempotent_second_apply_inserts_nothing(self):
        self.add_row("BV1aaaaaaaaa")
        self.run_quiet(migrations.main, ["--apply", "--json", "--db", str(self.db),
                         "--root", str(self.archive)])
        conn = sqlite3.connect(str(self.db))
        before = conn.execute("SELECT COUNT(*) FROM transcript_versions").fetchone()[0]
        conn.close()
        self.run_quiet(migrations.main, ["--apply", "--json", "--db", str(self.db),
                         "--root", str(self.archive)])
        conn = sqlite3.connect(str(self.db))
        after = conn.execute("SELECT COUNT(*) FROM transcript_versions").fetchone()[0]
        conn.close()
        self.assertEqual(before, after)


class CorrectedVersionTests(SkillTestCase):
    def _prepared(self):
        self.add_row("BV1aaaaaaaaa")
        make_raw_file(self.archive / "raw.txt", bvid="BV1aaaaaaaaa")
        make_raw_file(self.archive / "校对版" / "x--deadbeef_校对版.txt", bvid="BV1aaaaaaaaa", body="校对后的正文。")
        self.run_quiet(migrations.main, ["--apply", "--json", "--db", str(self.db),
                         "--root", str(self.archive)])

    def test_corrected_becomes_version_two_after_raw(self):
        self._prepared()
        self.assertEqual(versions_of(self.db, "BV1aaaaaaaaa"), [(1, "raw"), (2, "corrected")])

    def test_corrected_body_and_hash_are_stored(self):
        self._prepared()
        conn = sqlite3.connect(str(self.db))
        try:
            row = conn.execute(
                "SELECT transcript_text, source_hash, is_current FROM transcript_versions"
                " WHERE kind='corrected'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "校对后的正文。")
        self.assertEqual(row[1], "deadbeef")
        self.assertEqual(row[2], 0)

    def test_two_corrected_copies_never_share_version_no(self):
        """同一键的多份校对版副本必须分到不同 version_no（曾因重号导致 UNIQUE 冲突）。"""
        self.add_row("BV1aaaaaaaaa")
        make_raw_file(self.archive / "raw.txt", bvid="BV1aaaaaaaaa")
        make_raw_file(self.archive / "校对版" / "a--aaaa1111_校对版.txt", bvid="BV1aaaaaaaaa", body="副本甲。")
        make_raw_file(self.archive / "校对版" / "b--bbbb2222_校对版.txt", bvid="BV1aaaaaaaaa", body="副本乙。")
        self.run_quiet(migrations.main, ["--apply", "--json", "--db", str(self.db),
                         "--root", str(self.archive)])
        rows = versions_of(self.db, "BV1aaaaaaaaa")
        self.assertEqual([no for no, _ in rows], [1, 2, 3])
        self.assertEqual(len({no for no, _ in rows}), len(rows))

    def test_orphan_corrected_is_reported_not_inserted(self):
        """校对版指向的作品不在库中时，只报告不写入。"""
        make_raw_file(self.archive / "校对版" / "orphan--cccc3333_校对版.txt", bvid="BV1zzzzzzzzz")
        self.run_quiet(migrations.main, ["--apply", "--json", "--db", str(self.db),
                         "--root", str(self.archive)])
        conn = sqlite3.connect(str(self.db))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM transcript_versions WHERE bvid='BV1zzzzzzzzz'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_corrected_without_body_is_skipped(self):
        self.add_row("BV1aaaaaaaaa")
        path = make_raw_file(self.archive / "校对版" / "empty--dddd4444_校对版.txt", bvid="BV1aaaaaaaaa")
        path.write_text("只有头部没有正文段", encoding="utf-8")
        self.run_quiet(migrations.main, ["--apply", "--json", "--db", str(self.db),
                         "--root", str(self.archive)])
        conn = sqlite3.connect(str(self.db))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM transcript_versions WHERE kind='corrected'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
