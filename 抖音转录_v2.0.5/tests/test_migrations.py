"""v1 → v2 迁移与数据库备份测试：迁移必须无损、幂等、事务内完成。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import migrations
from scripts.transcript_db import _SCHEMA as V1_SCHEMA

AWEME = "7410000000000001234"
LEGACY_TEXT = "端午就看我奶龙大侠赛龙舟"


def make_v1_database(path: Path, *, rows: list[dict] | None = None,
                     transcript_file: Path | None = None) -> sqlite3.Connection:
    """用 v1 的真实 schema 与行结构造一个遗留库。"""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(V1_SCHEMA)
    payload = rows if rows is not None else [{
        "aweme_id": AWEME,
        "original_input": "8.88 复制打开抖音 https://v.douyin.com/abc/",
        "canonical_url": f"https://www.douyin.com/video/{AWEME}",
        "title": "旧标题",
        "author": "旧作者",
        "publish_date": "2026-06-19",
        "duration_seconds": 70.0,
        "transcript_source": "faster-whisper",
        "whisper_model": "medium",
        "language": "zh",
        "transcript_text": LEGACY_TEXT,
        "segments_json": '[{"start": 0.0, "end": 2.0, "text": "五月五"}]',
        "summary": "旧摘要",
        "summary_status": "summarized",
        "transcript_file": str(transcript_file or ""),
        "status": "summarized",
        "failure_code": "",
        "failure_message": "",
    }]
    for row in payload:
        columns = {"created_at": "2026-09-15 10:00:00",
                   "updated_at": "2026-09-15 15:17:20", **row}
        conn.execute(
            f"INSERT INTO transcripts ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)}) "
            "ON CONFLICT(aweme_id) DO UPDATE SET "
            + ", ".join(f"{name}=excluded.{name}" for name in columns
                        if name != "aweme_id"),
            tuple(columns.values()))
    conn.commit()
    return conn


class BackupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "transcripts.db"
        conn = make_v1_database(self.source)
        conn.close()

    def test_backup_copies_rows_via_sqlite_api(self):
        backup_dir = self.root / "backups"
        target = migrations.backup_database(self.source, backup_dir)
        self.assertTrue(target.is_file())
        self.assertTrue(backup_dir.is_dir())
        copy = sqlite3.connect(str(target))
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(
            copy.execute("SELECT count(*) FROM transcripts").fetchone()[0], 1)
        self.assertIn("transcripts-before-v2-", target.name)

    def test_backup_name_is_unique_and_never_overwrites(self):
        first = migrations.backup_database(self.source, self.root / "backups",
                                           stamp="20260916-010101")
        second = migrations.backup_database(self.source, self.root / "backups",
                                            stamp="20260916-010101")
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file() and second.is_file())

    def test_backup_of_missing_database_raises(self):
        with self.assertRaises(FileNotFoundError):
            migrations.backup_database(self.root / "nope.db", self.root / "backups")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "transcripts.db"
        self.txt = self.root / "output" / "旧标题_7410000000000001234.txt"
        self.txt.parent.mkdir(parents=True, exist_ok=True)
        self.txt.write_text("旧 TXT 内容", encoding="utf-8")
        self.conn = make_v1_database(self.db_path, transcript_file=self.txt)
        self.addCleanup(self.conn.close)

    def _count(self, table: str) -> int:
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_migration_creates_all_v2_tables(self):
        migrations.migrate_to_v2(self.conn)
        names = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("schema_migrations", "videos", "batches", "jobs",
                      "transcript_versions", "asr_attempts", "ocr_observations",
                      "speaker_turns", "correction_changes", "artifacts"):
            self.assertIn(table, names)

    def test_migration_preserves_legacy_content_and_current(self):
        migrations.migrate_to_v2(self.conn)
        video = self.conn.execute("SELECT * FROM videos WHERE aweme_id = ?",
                                  (AWEME,)).fetchone()
        self.assertIsNotNone(video)
        video = dict(video)
        version = dict(self.conn.execute(
            "SELECT * FROM transcript_versions WHERE version_id = ?",
            (video["current_version_id"],)).fetchone())
        self.assertEqual(version["raw_text"], LEGACY_TEXT)
        self.assertEqual(version["summary"], "旧摘要")
        self.assertEqual(version["summary_status"], "summarized")
        self.assertEqual(version["is_current"], 1)
        self.assertEqual(version["lifecycle_status"], "published")
        self.assertEqual(version["quality_status"], "needs_review")
        self.assertEqual(version["processing_signature"], "legacy-v1")
        self.assertEqual(version["version_no"], 1)
        self.assertEqual(video["title"], "旧标题")
        self.assertEqual(video["author"], "旧作者")
        self.assertEqual(video["duration_seconds"], 70.0)
        # v1 表必须保留可读
        self.assertEqual(
            self.conn.execute("SELECT transcript_text FROM transcripts "
                              "WHERE aweme_id = ?", (AWEME,)).fetchone()[0],
            LEGACY_TEXT)

    def test_migration_registers_legacy_file_as_artifact(self):
        migrations.migrate_to_v2(self.conn)
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM artifacts WHERE kind = 'main_txt'")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], str(self.txt))
        self.assertEqual(rows[0]["sha256"],
                         migrations.file_sha256(self.txt))
        self.assertEqual(rows[0]["size_bytes"], self.txt.stat().st_size)
        self.assertEqual(rows[0]["is_current"], 1)

    def test_migration_without_file_registers_no_artifact(self):
        db_path = self.root / "no_file.db"
        conn = make_v1_database(db_path, transcript_file=None)
        self.addCleanup(conn.close)
        migrations.migrate_to_v2(conn)
        self.assertEqual(
            conn.execute("SELECT count(*) FROM artifacts").fetchone()[0], 0)
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM videos WHERE aweme_id = ?", (AWEME,)).fetchone())

    def test_migration_is_idempotent(self):
        first = migrations.migrate_to_v2(self.conn)
        second = migrations.migrate_to_v2(self.conn)
        self.assertEqual(first.migrated, 1)
        self.assertEqual(second.migrated, 0)
        self.assertEqual(second.skipped, 1)
        self.assertEqual(self._count("videos"), 1)
        self.assertEqual(self._count("transcript_versions"), 1)
        self.assertEqual(self._count("artifacts"), 1)
        self.assertEqual(self._count("schema_migrations"), 1)

    def test_migration_records_schema_version(self):
        migrations.migrate_to_v2(self.conn)
        versions = [row[0] for row in self.conn.execute(
            "SELECT version FROM schema_migrations")]
        self.assertEqual(versions, [migrations.SCHEMA_VERSION])

    def test_migration_after_v2_version_does_not_conflict(self):
        """v2 已经为该作品建过版本时，迁移必须顺延序号而不是撞 UNIQUE。"""
        conn = self.conn
        migrations.ensure_v2_schema(conn)
        conn.execute(
            "INSERT INTO videos (aweme_id, canonical_url, title, author, "
            "publish_date, duration_seconds, created_at, updated_at) "
            "VALUES (?, '', '', '', '', 0, 'now', 'now')", (AWEME,))
        conn.execute(
            "INSERT INTO transcript_versions (aweme_id, version_no, "
            "lifecycle_status, quality_status, processing_signature, "
            "is_current, created_at, raw_text) "
            "VALUES (?, 1, 'published', 'passed', 'v2', 1, 'now', '新原稿')",
            (AWEME,))
        conn.execute("UPDATE videos SET current_version_id = "
                     "(SELECT version_id FROM transcript_versions "
                     " WHERE aweme_id = ? AND is_current = 1) WHERE aweme_id = ?",
                     (AWEME, AWEME))
        conn.commit()
        report = migrations.migrate_to_v2(conn)
        self.assertEqual(report.migrated, 1)
        rows = [dict(r) for r in conn.execute(
            "SELECT version_no, processing_signature, is_current "
            "FROM transcript_versions WHERE aweme_id = ? ORDER BY version_no",
            (AWEME,))]
        self.assertEqual([r["version_no"] for r in rows], [1, 2])
        self.assertEqual(rows[1]["processing_signature"], "legacy-v1")
        # 已有 v2 current 时，迁移记录只作历史版本保留，绝不抢占 current
        self.assertEqual([r["is_current"] for r in rows], [1, 0])
        self.assertEqual(conn.execute(
            "SELECT current_version_id FROM videos WHERE aweme_id = ?",
            (AWEME,)).fetchone()[0], 1)

    def test_incomplete_legacy_row_is_not_migrated(self):
        rows = [{
            "aweme_id": "7410000000000007777", "original_input": "失败的",
            "canonical_url": "", "title": "失败标题", "author": "",
            "publish_date": "", "duration_seconds": 0.0,
            "transcript_source": "", "whisper_model": "", "language": "",
            "transcript_text": "", "segments_json": "[]", "summary": "",
            "summary_status": "pending", "transcript_file": "",
            "status": "failed", "failure_code": "asr_failed",
            "failure_message": "本地语音识别失败",
        }]
        db_path = self.root / "failed.db"
        conn = make_v1_database(db_path, rows=rows)
        self.addCleanup(conn.close)
        report = migrations.migrate_to_v2(conn)
        self.assertEqual(report.incomplete, 1)
        self.assertEqual(report.migrated, 0)
        self.assertEqual(
            conn.execute("SELECT count(*) FROM videos").fetchone()[0], 0)

    def test_failed_migration_rolls_back_completely(self):
        # 预置一个字段不符的 videos 表，迁移中途必须整体回滚
        self.conn.execute("DROP TABLE IF EXISTS videos")
        self.conn.execute("CREATE TABLE videos (aweme_id TEXT PRIMARY KEY, "
                          "bogus TEXT NOT NULL)")
        self.conn.commit()
        with self.assertRaises(sqlite3.Error):
            migrations.migrate_to_v2(self.conn)
        self.assertEqual(self._count("transcript_versions"), 0)
        self.assertEqual(self._count("schema_migrations"), 0)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM transcripts").fetchone()[0], 1)

    def test_migration_handles_multiple_legacy_records(self):
        rows = []
        for index in range(3):
            rows.append({
                "aweme_id": f"74100000000000{index:04d}",
                "original_input": f"文案 {index}",
                "canonical_url": f"https://www.douyin.com/video/74100000000000{index:04d}",
                "title": f"标题 {index}", "author": "作者",
                "publish_date": "", "duration_seconds": 10.0 + index,
                "transcript_source": "faster-whisper", "whisper_model": "base",
                "language": "zh", "transcript_text": f"文本 {index}",
                "segments_json": "[]", "summary": "", "summary_status": "pending",
                "transcript_file": "", "status": "transcribed",
                "failure_code": "", "failure_message": "",
            })
        db_path = self.root / "multi.db"
        conn = make_v1_database(db_path, rows=rows)
        self.addCleanup(conn.close)
        report = migrations.migrate_to_v2(conn)
        self.assertEqual(report.scanned, 3)
        self.assertEqual(report.migrated, 3)
        self.assertEqual(
            conn.execute("SELECT count(*) FROM videos").fetchone()[0], 3)
        self.assertEqual(
            conn.execute("SELECT count(*) FROM transcript_versions "
                         "WHERE version_no = 1").fetchone()[0], 3)


class SchemaConstraintTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = sqlite3.connect(str(Path(self._tmp.name) / "v2.db"))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        migrations.ensure_v2_schema(self.conn)
        self.conn.execute(
            "INSERT INTO videos (aweme_id, canonical_url, title, author, "
            "publish_date, duration_seconds, created_at, updated_at) "
            "VALUES ('1', '', '', '', '', 0, 'now', 'now')")
        self.conn.commit()

    def _insert_version(self, version_no: int, is_current: int) -> int:
        cursor = self.conn.execute(
            "INSERT INTO transcript_versions (aweme_id, version_no, "
            "lifecycle_status, quality_status, is_current, created_at) "
            "VALUES ('1', ?, 'staging', 'needs_review', ?, 'now')",
            (version_no, is_current))
        return int(cursor.lastrowid)

    def test_one_current_version_per_video(self):
        self._insert_version(1, 1)
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_version(2, 1)
        self.conn.rollback()

    def test_version_no_is_unique_per_video(self):
        self._insert_version(1, 0)
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_version(1, 0)

    def test_attempt_no_unique_per_version(self):
        version_id = self._insert_version(1, 1)
        self.conn.execute(
            "INSERT INTO asr_attempts (version_id, attempt_no, model) "
            "VALUES (?, 1, 'base')", (version_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO asr_attempts (version_id, attempt_no, model) "
                "VALUES (?, 1, 'base')", (version_id,))
        self.conn.rollback()

    def test_one_current_artifact_per_kind(self):
        version_id = self._insert_version(1, 1)
        self.conn.execute(
            "INSERT INTO artifacts (version_id, kind, path, sha256, "
            "size_bytes, is_current, created_at) "
            "VALUES (?, 'main_txt', 'p', 'h', 1, 1, 'now')", (version_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO artifacts (version_id, kind, path, sha256, "
                "size_bytes, is_current, created_at) "
                "VALUES (?, 'main_txt', 'p2', 'h', 1, 1, 'now')", (version_id,))
        self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
