"""批次预览阈值、认证暂停续跑、逐条容错与报告。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import batch_transcribe as bt
from scripts.job_queue import QueueRepository
from scripts.models import (BatchStatus, JobStatus, NormalizedInput,
                            QualityStatus)
from scripts.transcript_db import TranscriptDB
from tests import support

AWEME = "7410000000000001234"


def items(count: int, *, offset: int = 0) -> list[NormalizedInput]:
    return [NormalizedInput(raw_input=f"第 {index} 条",
                            url=f"https://www.douyin.com/video/74{index + offset:017d}",
                            candidate_aweme_id=f"74{index + offset:017d}",
                            kind="video")
            for index in range(count)]


def completed(aweme_id: str, *, status="completed") -> dict:
    return {"schema_version": 2, "ok": True, "status": status,
            "aweme_id": aweme_id, "title": f"标题 {aweme_id}",
            "main_file": f"C:/out/{aweme_id}.txt",
            "evidence_file": f"C:/out/{aweme_id}_原始记录.txt",
            "quality_status": "passed", "correction_status": "corrected",
            "summary_status": "summarized"}


class BatchTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = TranscriptDB(self.root / ".db" / "t.db")
        self.addCleanup(self.db.close)
        self.queue = QueueRepository(self.db)
        self.patchers = [
            mock.patch.object(bt.paths, "DB_PATH", self.root / ".db" / "t.db"),
            mock.patch.object(bt.paths, "STATE_DIR", self.root / "state"),
            mock.patch.object(bt.paths, "OUTPUT_DIR", self.root / "output"),
            mock.patch.object(bt, "default_services", side_effect=self._services),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _services(self, db, **kwargs):
        return mock.Mock(db=db)


class PreviewThresholdTests(BatchTestCase):
    def test_eleven_items_require_confirmation_without_running_anything(self):
        with mock.patch.object(bt, "process_one") as fake:
            out = bt.run_batch(text=" ".join(item.url for item in items(11)),
                               db=self.db)
        fake.assert_not_called()
        self.assertEqual(out["status"], "batch_confirmation_required")
        self.assertFalse(out["ok"])
        self.assertEqual(out["preview"]["submitted"], 11)
        self.assertEqual(out["preview"]["new"], 11)
        self.assertGreater(out["preview"]["estimated_seconds"], 1800)
        self.assertEqual(out["schema_version"], 2)

    def test_jobs_are_kept_in_the_queue_for_later_confirmation(self):
        out = bt.run_batch(text=" ".join(item.url for item in items(11)),
                           db=self.db)
        batch = self.queue.get_batch(out["batch_id"])
        self.assertEqual(batch["status"], str(BatchStatus.AWAITING_CONFIRMATION))
        self.assertEqual(len(self.queue.list_jobs(out["batch_id"])), 11)

    def test_ten_items_run_directly(self):
        with mock.patch.object(bt, "process_one",
                               side_effect=[completed("1")] * 10) as fake:
            out = bt.run_batch(text=" ".join(item.url for item in items(10)),
                               db=self.db)
        self.assertEqual(fake.call_count, 10)
        self.assertTrue(out["ok"])
        self.assertEqual(out["totals"]["new_transcribed"], 10)

    def test_explicit_confirmation_skips_preview(self):
        with mock.patch.object(bt, "process_one",
                               side_effect=[completed("1")] * 11) as fake:
            out = bt.run_batch(text=" ".join(item.url for item in items(11)),
                               confirm=True, db=self.db)
        self.assertEqual(fake.call_count, 11)
        self.assertEqual(out["totals"]["new_transcribed"], 11)

    def test_confirm_flag_is_honoured_by_cli(self):
        with mock.patch.object(bt, "process_one",
                               side_effect=[completed("1")] * 11) as fake:
            self._cli(["--text", " ".join(item.url for item in items(11)),
                       "--confirm"])
        self.assertEqual(fake.call_count, 11)

    def test_cli_without_confirm_returns_preview_json(self):
        code, payload, lines = self._cli(
            ["--text", " ".join(item.url for item in items(11))])
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "batch_confirmation_required")
        self.assertEqual(len(lines), 1)

    def _cli(self, argv):
        stdout = io.StringIO()
        with support.isolate_logging(self.root), \
             redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = bt.main(argv)
        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        return code, json.loads(lines[-1]), lines


class AuthPauseTests(BatchTestCase):
    def setUp(self):
        super().setUp()
        self.preview = self.queue.create_batch(items(3, offset=100),
                                               confirmed=True)
        self.batch_id = self.preview.batch_id

    def _process_results(self, results):
        def fake(job, services):
            return results.pop(0)

        return fake

    def test_login_required_pauses_batch_and_skips_remaining(self):
        results = [completed("a"),
                   {"ok": False, "status": "login_required",
                    "error": {"code": "login_required", "message": "需要扫码"}}]
        with mock.patch.object(bt, "process_one",
                               side_effect=self._process_results(results)) as fake:
            out = bt.run_batch(resume_batch_id=self.batch_id, db=self.db)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(out["status"], "paused_auth")
        self.assertEqual(self.queue.get_batch(self.batch_id)["status"],
                         str(BatchStatus.PAUSED_AUTH))
        jobs = self.queue.list_jobs(self.batch_id)
        self.assertEqual(jobs[2].status, JobStatus.PENDING)

    def test_resume_continues_from_the_paused_job(self):
        results = [completed("a"),
                   {"ok": False, "status": "login_required",
                    "error": {"code": "login_required", "message": "需要扫码"}}]
        with mock.patch.object(bt, "process_one",
                               side_effect=self._process_results(results)):
            bt.run_batch(resume_batch_id=self.batch_id, db=self.db)
        seen = []

        def fake(job, services):
            seen.append(job.position)
            return completed("b", status="reused")

        with mock.patch.object(bt, "process_one", fake):
            out = bt.run_batch(resume_batch_id=self.batch_id, db=self.db)
        self.assertEqual(seen, [1, 2])          # 第一项不重跑
        self.assertEqual(out["totals"]["reused"], 2)
        self.assertEqual(self.queue.get_batch(self.batch_id)["status"],
                         str(BatchStatus.COMPLETED))

    def test_resume_reports_unknown_batch(self):
        out = bt.run_batch(resume_batch_id="missing-batch", db=self.db)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["error"]["code"], "invalid_input")

    def test_visible_flag_propagates_to_services(self):
        captured = []

        def fake(job, services):
            captured.append(getattr(services, "headless", None))
            return completed("a")

        with mock.patch.object(bt, "process_one", fake):
            bt.run_batch(resume_batch_id=self.batch_id, headless=False, db=self.db)
        self.assertTrue(all(value is False for value in captured))


class ReportTests(BatchTestCase):
    def _run(self, outcomes):
        preview = self.queue.create_batch(items(len(outcomes), offset=200),
                                          confirmed=True)
        results = list(outcomes)

        def fake(job, services):
            return results.pop(0)

        with mock.patch.object(bt, "process_one", fake):
            return bt.run_batch(resume_batch_id=preview.batch_id, db=self.db)

    def test_every_outcome_is_listed_in_order_with_ids_and_files(self):
        out = self._run([
            completed("a"),
            {"ok": True, "status": "reused", "aweme_id": "b", "title": "标题 b",
             "main_file": "b.txt", "evidence_file": "b_原始记录.txt",
             "quality_status": "passed", "reuse_action": "reuse"},
            {"ok": True, "status": "needs_review", "aweme_id": "c", "title": "标题 c",
             "main_file": "c.txt", "quality_status": "needs_review",
             "quality_score": 60.0},
            {"ok": True, "status": "awaiting_ai", "aweme_id": "d",
             "summary_status": "pending", "main_file": "d.txt"},
            {"ok": True, "status": "visual_text_only", "aweme_id": "e",
             "main_file": "e.txt"},
            {"ok": False, "status": "failed", "aweme_id": "f",
             "error": {"code": "video_unavailable", "message": "作品不可用"}},
        ])
        self.assertEqual([item["aweme_id"] for item in out["results"]],
                         ["a", "b", "c", "d", "e", "f"])
        self.assertEqual(out["totals"]["new_transcribed"], 1)
        self.assertEqual(out["totals"]["reused"], 1)
        self.assertEqual(out["totals"]["needs_review"], 1)
        self.assertEqual(out["totals"]["pending_summary"], 1)
        self.assertEqual(out["totals"]["visual_text_only"], 1)
        self.assertEqual(out["totals"]["failed"], 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["results"][5]["error"]["code"], "video_unavailable")
        self.assertIn("main_file", out["results"][0])

    def test_processing_is_always_serial(self):
        order = []

        def fake(job, services):
            order.append(("start", job.position))
            order.append(("end", job.position))
            return completed("a")

        preview = self.queue.create_batch(items(3, offset=300), confirmed=True)
        with mock.patch.object(bt, "process_one", fake):
            bt.run_batch(resume_batch_id=preview.batch_id, db=self.db)
        self.assertEqual(order, [("start", 0), ("end", 0), ("start", 1),
                                 ("end", 1), ("start", 2), ("end", 2)])

    def test_duplicate_items_are_reported_not_processed(self):
        urls = [items(1, offset=400)[0], items(1, offset=400)[0]]
        preview = self.queue.create_batch(urls, confirmed=True)
        with mock.patch.object(bt, "process_one",
                               return_value=completed("a")) as fake:
            out = bt.run_batch(resume_batch_id=preview.batch_id, db=self.db)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(out["preview"]["duplicates"], 1)

    def test_every_job_appears_even_when_not_processed(self):
        """真实批次实测：复用/重复项此前不出现在 results，报告必须逐条列出。"""
        from scripts.models import NormalizedInput

        urls = [items(1, offset=800)[0], items(1, offset=800)[0],
                items(1, offset=801)[0]]
        preview = self.queue.create_batch(urls, confirmed=True)
        with mock.patch.object(bt, "process_one",
                               return_value=completed("x")) as fake:
            out = bt.run_batch(resume_batch_id=preview.batch_id, db=self.db)
        self.assertEqual(fake.call_count, 2)          # 重复项不处理
        self.assertEqual(len(out["results"]), 3)      # 但必须逐条汇报
        statuses = [item["status"] for item in out["results"]]
        self.assertIn("duplicate", statuses)
        self.assertEqual(out["totals"]["duplicate"], 1)
        self.assertEqual(out["totals"]["new_transcribed"], 2)
        duplicate = next(item for item in out["results"]
                         if item["status"] == "duplicate")
        self.assertTrue(duplicate["duplicate_of_job_id"])

    def _publish_deliverable(self, aweme_id: str) -> None:
        """造一个已发布版本，让入队时就判定为可复用。"""
        import hashlib

        from scripts.artifacts import (build_version_bundle, publish_artifacts,
                                      render_evidence, render_main)
        from scripts.models import CorrectionStatus

        version_id = self.db.create_staging_version(
            aweme_id, metadata={"title": "已转录标题", "author": "作者",
                                "publish_date": "2026-06-19"})
        self.db.add_asr_attempt(
            version_id, attempt_no=1, audio_variant="original", strategy={},
            model="medium", device="cuda", compute_type="float16", language="zh",
            transcript_text="原稿", segments_json=[{"start": 0.0, "end": 1.0,
                                                    "text": "原稿"}],
            metrics={}, hard_failures=[], score=90.0, passed=True)
        attempt = self.db.list_attempts(version_id)[0]
        self.db.select_attempt(version_id, attempt["attempt_id"])
        self.db.set_quality(version_id, status="passed", score=90.0, report={})
        self.db.save_summary(version_id, summary="摘要", status="summarized",
                             mode="normal", meta={})
        self.db.save_correction(version_id, corrected_text="校正稿", changes=[],
                                status=CorrectionStatus.CORRECTED)
        output = self.root / "output"
        bundle = build_version_bundle(self.db, version_id)
        publish_artifacts(self.db, version_id,
                          [render_main(bundle, output),
                           render_evidence(bundle, output)], output_root=output)

    def test_reused_jobs_are_listed_with_their_files(self):
        self._publish_deliverable("7410000000000012345")
        item = NormalizedInput(
            raw_input="已转录", url="https://www.douyin.com/video/7410000000000012345",
            candidate_aweme_id="7410000000000012345", kind="video")
        preview = self.queue.create_batch([item], confirmed=True)
        with mock.patch.object(bt, "process_one") as fake:
            out = bt.run_batch(resume_batch_id=preview.batch_id, db=self.db)
        fake.assert_not_called()
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["results"][0]["status"], "reused")
        self.assertTrue(out["results"][0]["main_file"])
        self.assertEqual(out["totals"]["reused"], 1)
        self.assertTrue(out["ok"])

    def test_report_includes_batch_and_schema(self):
        out = self._run([completed("a")])
        self.assertEqual(out["schema_version"], 2)
        self.assertTrue(out["batch_id"])
        self.assertIn("preview", out)
        self.assertIn("results", out)


class RetryFailedTests(BatchTestCase):
    def test_retry_failed_creates_new_jobs_and_keeps_history(self):
        preview = self.queue.create_batch(items(2, offset=500), confirmed=True)
        outcomes = [{"ok": False, "status": "failed",
                     "error": {"code": "video_unavailable", "message": "x"}},
                    completed("a")]
        results = list(outcomes)

        def fake(job, services):
            return results.pop(0)

        with mock.patch.object(bt, "process_one", fake):
            bt.run_batch(resume_batch_id=preview.batch_id, db=self.db)

        jobs = self.queue.list_jobs(preview.batch_id)
        self.assertEqual([job.status for job in jobs],
                         [JobStatus.FAILED, JobStatus.COMPLETED])

        with mock.patch.object(bt, "process_one",
                               return_value=completed("a")) as retry:
            out = bt.run_batch(retry_failed=preview.batch_id, db=self.db)
        self.assertEqual(retry.call_count, 1)
        # totals 描述整个批次现状（含此前已完成项），results 逐条列出全部 job
        self.assertEqual(out["totals"]["new_transcribed"], 2)
        self.assertEqual(out["totals"]["failed"], 1)
        self.assertEqual(len(out["results"]), 3)
        jobs = self.queue.list_jobs(preview.batch_id)
        self.assertEqual(len(jobs), 3)          # 历史失败项被保留
        self.assertEqual(jobs[0].status, JobStatus.FAILED)

    def test_retry_failed_without_failures_is_a_noop(self):
        preview = self.queue.create_batch(items(1, offset=600), confirmed=True)
        with mock.patch.object(bt, "process_one", return_value=completed("a")):
            bt.run_batch(resume_batch_id=preview.batch_id, db=self.db)
        with mock.patch.object(bt, "process_one") as fake:
            out = bt.run_batch(retry_failed=preview.batch_id, db=self.db)
        fake.assert_not_called()
        self.assertTrue(out["ok"])

    def test_retry_failed_reports_unknown_batch(self):
        out = bt.run_batch(retry_failed="missing", db=self.db)
        self.assertEqual(out["error"]["code"], "invalid_input")


class RecoveryTests(BatchTestCase):
    def test_stale_jobs_are_recovered_before_running(self):
        preview = self.queue.create_batch(items(2, offset=700), confirmed=True)
        job = self.queue.claim_next(preview.batch_id)
        self.queue.update_status(job.job_id, JobStatus.FETCHING)
        self.db.connect().execute(
            "UPDATE jobs SET heartbeat_at = '2000-01-01 00:00:00' WHERE job_id = ?",
            (job.job_id,))
        self.db.connect().commit()
        processed = []

        def fake(item, services):
            processed.append(item.job_id)
            return completed("a")

        with mock.patch.object(bt, "process_one", fake):
            out = bt.run_batch(resume_batch_id=preview.batch_id, db=self.db)
        self.assertIn(job.job_id, processed)
        self.assertEqual(out["recovered_jobs"], 1)


if __name__ == "__main__":
    unittest.main()
