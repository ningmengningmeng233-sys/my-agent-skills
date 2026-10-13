"""批处理入口接入统一输入规范化：相似域拒绝、modal_id 归一化、估算与预览。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import batch_transcribe, douyin_transcript, transcript_db
from tests import support

AWEME = "7410000000000001234"
MODAL_URL = ("https://www.douyin.com/user/self?from_tab_name=main"
             f"&modal_id={AWEME}&showTab=favorite_collection")


class ExtractionWiringTests(unittest.TestCase):
    def test_extract_douyin_urls_is_backed_by_normalizer(self):
        urls = douyin_transcript.extract_douyin_urls(
            f"看这个 {MODAL_URL}")
        self.assertEqual(urls, [f"https://www.douyin.com/video/{AWEME}"])

    def test_lookalike_domain_is_not_extracted(self):
        self.assertEqual(
            douyin_transcript.extract_douyin_urls(
                "https://www.douyin.com.evil.test/video/7410000000000001234"),
            [])

    def test_candidate_aweme_id_reads_modal_id(self):
        self.assertEqual(douyin_transcript.candidate_aweme_id(MODAL_URL), AWEME)


class RunBatchNormalizationTests(unittest.TestCase):
    """批处理必须使用临时数据库，绝不触碰真实 .db/transcripts.db。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = transcript_db.TranscriptDB(Path(self._tmp.name) / "t.db")
        self.addCleanup(self.db.close)

    def test_process_one_receives_normalized_modal_url(self):
        seen = []

        def fake_process(job, services):
            seen.append(job)
            return {"ok": True, "status": "reused", "aweme_id": AWEME}

        with mock.patch.object(batch_transcribe, "process_one", fake_process):
            batch_transcribe.run_batch(MODAL_URL, db=self.db)
        self.assertEqual([job.normalized_input for job in seen],
                         [f"https://www.douyin.com/video/{AWEME}"])

    def test_lookalike_only_input_reports_invalid_input(self):
        with mock.patch.object(batch_transcribe, "process_one") as fake:
            out = batch_transcribe.run_batch(
                "https://douyin.com.evil.test/video/7410000000000001234")
        fake.assert_not_called()
        self.assertEqual(out["error"]["code"], "invalid_input")

    def test_duplicate_links_are_processed_once(self):
        seen = []

        def fake_process(job, services):
            seen.append(job)
            return {"ok": True, "status": "reused", "aweme_id": AWEME}

        with mock.patch.object(batch_transcribe, "process_one", fake_process):
            out = batch_transcribe.run_batch(f"{MODAL_URL} 再看 {MODAL_URL}",
                                             db=self.db)
        self.assertEqual(len(seen), 1)
        self.assertEqual(out["results"][0]["status"], "reused")


class FileInputCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_file_input_is_supported(self):
        path = self.root / "list.txt"
        path.write_text(f"第一条 https://www.douyin.com/video/{AWEME}\n",
                        encoding="utf-8")
        seen = []

        def fake_process(job, services):
            seen.append(job)
            return {"ok": True, "status": "reused", "aweme_id": AWEME,
                    "reused": True}

        stdout, stderr = io.StringIO(), io.StringIO()
        with support.isolate_logging(self.root), \
             mock.patch.object(batch_transcribe.paths, "DB_PATH",
                               self.root / "t.db"), \
             mock.patch.object(batch_transcribe.paths, "STATE_DIR",
                               self.root / "state"), \
             mock.patch.object(batch_transcribe.paths, "OUTPUT_DIR",
                               self.root / "output"), \
             mock.patch.object(batch_transcribe, "process_one", fake_process), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = batch_transcribe.main(["--file", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual([job.normalized_input for job in seen],
                         [f"https://www.douyin.com/video/{AWEME}"])
        payload = json.loads([ln for ln in stdout.getvalue().splitlines()
                             if ln.strip()][0])
        self.assertIn("totals", payload)

    def test_encoding_error_is_reported_without_crashing(self):
        path = self.root / "gbk.txt"
        path.write_bytes("链接 https://v.douyin.com/aaa/".encode("gbk"))
        stdout, stderr = io.StringIO(), io.StringIO()
        with support.isolate_logging(self.root), \
             mock.patch.object(batch_transcribe, "process_one") as fake, \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = batch_transcribe.main(["--file", str(path)])
        fake.assert_not_called()
        self.assertEqual(code, 1)
        payload = json.loads([ln for ln in stdout.getvalue().splitlines()
                             if ln.strip()][0])
        self.assertEqual(payload["error"]["code"],
                         "input_encoding_error")


if __name__ == "__main__":
    unittest.main()
