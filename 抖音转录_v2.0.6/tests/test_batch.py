"""batch_transcribe.py 多链接串行处理与 JSON 输出测试。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import batch_transcribe, douyin_transcript, transcript_db
from tests import support

SHARED_TEXT = (
    "8.88 复制打开抖音，看看【张三的作品】 https://v.douyin.com/aaa/ "
    " 8.88 复制打开抖音 https://v.douyin.com/bbb/  # 精彩视频")

FULL_LINKS = (
    "看看 https://www.douyin.com/video/7000000001 和 "
    "https://v.douyin.com/bbb/")


class ExtractTests(unittest.TestCase):
    def test_two_links_extracted_and_deduplicated(self):
        urls = douyin_transcript.extract_douyin_urls(SHARED_TEXT)
        self.assertEqual(urls, ["https://v.douyin.com/aaa/",
                                "https://v.douyin.com/bbb/"])

    def test_dedup_preserves_first_occurrence(self):
        urls = douyin_transcript.extract_douyin_urls(
            "https://v.douyin.com/x/ 再看 https://v.douyin.com/x/")
        self.assertEqual(urls, ["https://v.douyin.com/x/"])

    def test_non_douyin_hosts_are_ignored(self):
        urls = douyin_transcript.extract_douyin_urls(
            "https://example.com/watch 抖音 https://www.douyin.com/video/123")
        self.assertEqual(urls, ["https://www.douyin.com/video/123"])


class RunBatchTests(unittest.TestCase):
    """v2 起 process_one 接收 JobRecord：断言改为看 job 的规范化链接与状态汇总。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = transcript_db.TranscriptDB(self.root / ".db" / "t.db")
        self.addCleanup(self.db.close)

    def _run(self, text, outcomes, *, force=False):
        seen = []

        def fake_process(job, services):
            seen.append(job)
            return outcomes[len(seen) - 1]

        with mock.patch.object(batch_transcribe, "process_one", fake_process):
            out = batch_transcribe.run_batch(text, force=force, db=self.db)
        return out, seen

    def test_serial_order_and_failure_does_not_stop_batch(self):
        results = [
            {"ok": False, "status": "failed",
             "error": {"code": "video_unavailable", "message": "上游失败"}},
            {"ok": True, "status": "completed", "aweme_id": "2",
             "title": "第二条", "main_file": "C:/o/2.txt",
             "summary_status": "summarized"},
        ]
        out, seen = self._run(
            "第一条 https://v.douyin.com/aaa/ 第二条 https://v.douyin.com/bbb/",
            results)
        self.assertEqual([job.normalized_input for job in seen],
                         ["https://v.douyin.com/aaa/", "https://v.douyin.com/bbb/"])
        self.assertEqual(out["totals"]["new_transcribed"], 1)
        self.assertEqual(out["totals"]["failed"], 1)
        self.assertFalse(out["ok"])

    def test_counts_for_every_category(self):
        outcomes = [
            {"ok": True, "status": "reused", "aweme_id": "1",
             "summary_status": "summarized", "transcript_file": "a.txt", "title": "t"},
            {"ok": True, "status": "awaiting_ai", "aweme_id": "2",
             "summary_status": "pending", "main_file": "b.txt", "title": "t"},
            {"ok": True, "status": "completed", "aweme_id": "3",
             "summary_status": "summarized", "main_file": "c.txt", "title": "t"},
            {"ok": False, "status": "login_required", "aweme_id": "4",
             "error": {"code": "login_required", "message": "需要扫码"}},
        ]
        out, _ = self._run(
            "https://v.douyin.com/1/ https://v.douyin.com/2/ "
            "https://v.douyin.com/3/ https://v.douyin.com/4/", outcomes)
        self.assertEqual(out["totals"]["reused"], 1)
        self.assertEqual(out["totals"]["pending_summary"], 1)
        self.assertEqual(out["totals"]["new_transcribed"], 1)
        self.assertEqual(out["totals"]["login_required"], 1)
        self.assertFalse(out["ok"])

    def test_no_links_reports_invalid_input(self):
        with mock.patch.object(batch_transcribe, "process_one") as p:
            out = batch_transcribe.run_batch("随便一句话", db=self.db)
        p.assert_not_called()
        # 输入级失败没有逐条结果，按 spec §17 走顶层 error
        self.assertEqual(out["error"]["code"], "invalid_input")

    def test_cli_emits_exactly_one_json_line(self):
        outcomes = [{"ok": True, "status": "reused", "aweme_id": "1",
                     "summary_status": "summarized", "reused": True,
                     "transcript_file": "a.txt", "title": "t"}]
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp,              support.isolate_logging(Path(tmp)),              mock.patch.object(batch_transcribe, "process_one",
                               side_effect=outcomes),              mock.patch.object(batch_transcribe.paths, "DB_PATH",
                               Path(tmp) / "t.db"),              mock.patch.object(batch_transcribe.paths, "STATE_DIR",
                               Path(tmp) / "state"),              mock.patch.object(batch_transcribe.paths, "OUTPUT_DIR",
                               Path(tmp) / "output"),              redirect_stdout(stdout), redirect_stderr(stderr):
            code = batch_transcribe.main([FULL_LINKS.split(" ")[1]])
        lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertIn("totals", payload)
        self.assertEqual(code, 0)
        # 人类日志不得污染 JSON
        self.assertNotIn("INFO", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
