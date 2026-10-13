"""状态机、复用条件与失败语义测试。"""

import tempfile
import unittest
from pathlib import Path

from scripts.transcript_db import TranscriptDB

AWEME = "7000000001"


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.db = TranscriptDB(root / "transcripts.db")
        self.addCleanup(self.db.close)
        self.txt_path = root / "out.txt"
        self._fetching()

    def _fetching(self):
        self.db.upsert_fetching(
            aweme_id=AWEME,
            original_input="https://v.douyin.com/x/",
            canonical_url=f"https://www.douyin.com/video/{AWEME}",
            metadata={"title": "T", "author": "A", "publish_date": "2026-09-15",
                      "duration_seconds": 10},
        )

    def _transcribe(self):
        self.db.mark_audio_ready(AWEME)
        self.db.mark_transcribed(
            AWEME,
            transcript_text="原稿",
            segments_json='[{"start":0,"end":1,"text":"原稿"}]',
            transcript_source="faster-whisper",
            whisper_model="base",
            language="zh",
        )

    def test_audio_ready_not_reusable(self):
        self.db.mark_audio_ready(AWEME)
        self.assertIsNone(self.db.get_reusable(AWEME))

    def test_failed_not_reusable(self):
        self.db.mark_failed(AWEME, "asr_failed", "识别失败")
        self.assertIsNone(self.db.get_reusable(AWEME))

    def test_transcribed_with_existing_txt_is_reusable(self):
        self._transcribe()
        self.txt_path.write_text("x", encoding="utf-8")
        self.db.set_transcript_file(AWEME, str(self.txt_path))
        record = self.db.get_reusable(AWEME)
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "transcribed")

    def test_transcribed_with_missing_txt_not_reusable(self):
        self._transcribe()
        self.db.set_transcript_file(AWEME, str(self.txt_path))  # 文件不存在
        self.assertIsNone(self.db.get_reusable(AWEME))

    def test_transcribed_with_empty_text_not_reusable(self):
        self.db.mark_transcribed(
            AWEME, transcript_text="", segments_json="[]",
            transcript_source="faster-whisper", whisper_model="base", language="zh",
        )
        self.txt_path.write_text("x", encoding="utf-8")
        self.db.set_transcript_file(AWEME, str(self.txt_path))
        self.assertIsNone(self.db.get_reusable(AWEME))

    def test_summarized_still_reusable(self):
        self._transcribe()
        self.txt_path.write_text("x", encoding="utf-8")
        self.db.set_transcript_file(AWEME, str(self.txt_path))
        self.db.mark_summarized(AWEME, "摘要内容")
        record = self.db.get_reusable(AWEME)
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "summarized")
        self.assertEqual(record["summary_status"], "summarized")
        self.assertEqual(record["summary"], "摘要内容")

    def test_mark_summary_pending_keeps_transcribed(self):
        self._transcribe()
        self.db.mark_summary_pending(AWEME)
        record = self.db.get_by_aweme_id(AWEME)
        self.assertEqual(record["status"], "transcribed")
        self.assertEqual(record["summary_status"], "pending")

    def test_failed_does_not_erase_completed_record(self):
        self._transcribe()
        self.txt_path.write_text("x", encoding="utf-8")
        self.db.set_transcript_file(AWEME, str(self.txt_path))
        self.db.mark_summarized(AWEME, "旧摘要")
        # 强制重转的新尝试失败，不得擦除旧完成记录
        self.db.mark_failed(AWEME, "platform_failed", "上游失败")
        record = self.db.get_by_aweme_id(AWEME)
        self.assertEqual(record["status"], "summarized")
        self.assertEqual(record["transcript_text"], "原稿")
        self.assertEqual(record["transcript_file"], str(self.txt_path))
        self.assertEqual(record["summary"], "旧摘要")
        self.assertIsNotNone(self.db.get_reusable(AWEME))

    def test_mark_failed_on_missing_row_creates_failed_record(self):
        self.db.mark_failed("8888888888", "invalid_input", "找不到链接")
        record = self.db.get_by_aweme_id("8888888888")
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["failure_code"], "invalid_input")

    def test_pending_summaries_lists_only_unsummarized(self):
        self._transcribe()
        self.txt_path.write_text("x", encoding="utf-8")
        self.db.set_transcript_file(AWEME, str(self.txt_path))
        self.db.upsert_fetching(
            aweme_id="7000000002", original_input="", canonical_url="",
            metadata={},
        )
        self.db.mark_transcribed(
            "7000000002", transcript_text="第二条", segments_json="[]",
            transcript_source="faster-whisper", whisper_model="base", language="zh",
        )
        self.db.mark_summarized(AWEME, "摘要")
        pending = self.db.pending_summaries()
        self.assertEqual([r["aweme_id"] for r in pending], ["7000000002"])

    def test_stats_counts(self):
        self._transcribe()
        self.txt_path.write_text("x", encoding="utf-8")
        self.db.set_transcript_file(AWEME, str(self.txt_path))
        self.db.mark_failed("7000000003", "asr_failed", "失败")
        stats = self.db.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["summarized"], 0)
        self.assertEqual(stats["pending_summary"], 1)
        self.assertEqual(stats["failed"], 1)


if __name__ == "__main__":
    unittest.main()
