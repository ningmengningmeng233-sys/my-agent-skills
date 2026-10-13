"""TranscriptDB 初始化、查询与基本写入测试。"""

import tempfile
import unittest
from pathlib import Path

from scripts.transcript_db import TranscriptDB


class TranscriptDbBasicTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "transcripts.db"
        self.db = TranscriptDB(self.db_path)
        self.addCleanup(self.db.close)

    def _fetching(self, aweme_id="7000000001"):
        self.db.upsert_fetching(
            aweme_id=aweme_id,
            original_input="看看这个 https://v.douyin.com/abc/",
            canonical_url="https://www.douyin.com/video/7000000001",
            metadata={
                "title": "测试作品",
                "author": "测试作者",
                "publish_date": "2026-09-15",
                "duration_seconds": 42.5,
            },
        )

    def test_schema_and_wal_enabled(self):
        with self.db.connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(int(fk), 1)

    def test_upsert_fetching_then_transcribed(self):
        self._fetching()
        self.db.mark_transcribed(
            "7000000001",
            transcript_text="完整口播原稿。",
            segments_json='[{"start": 0.0, "end": 2.0, "text": "完整口播原稿。"}]',
            transcript_source="faster-whisper",
            whisper_model="base",
            language="zh",
        )
        record = self.db.get_by_aweme_id("7000000001")
        self.assertIsNotNone(record)
        self.assertEqual(record["aweme_id"], "7000000001")
        self.assertEqual(record["transcript_text"], "完整口播原稿。")
        self.assertEqual(record["title"], "测试作品")
        self.assertEqual(record["author"], "测试作者")
        self.assertEqual(record["publish_date"], "2026-09-15")
        self.assertAlmostEqual(record["duration_seconds"], 42.5)
        self.assertEqual(record["status"], "transcribed")
        self.assertEqual(record["summary_status"], "pending")
        self.assertTrue(record["created_at"])
        self.assertTrue(record["updated_at"])

    def test_upsert_same_aweme_id_keeps_single_row(self):
        self._fetching()
        self._fetching()
        with self.db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM transcripts WHERE aweme_id = ?",
                ("7000000001",),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_get_by_aweme_id_missing_returns_none(self):
        self.assertIsNone(self.db.get_by_aweme_id("9999999999"))


if __name__ == "__main__":
    unittest.main()
