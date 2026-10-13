"""维护命令：补 AI、重渲染、续跑、重试失败、回退版本（都不碰媒体）。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import maintenance
from scripts.job_queue import QueueRepository
from scripts.models import (CorrectionStatus, JobStatus, NormalizedInput,
                            QualityStatus)
from scripts.transcript_db import TranscriptDB
from tests import support

AWEME = "7410000000000001234"


class MaintenanceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = TranscriptDB(self.root / ".db" / "t.db")
        self.addCleanup(self.db.close)
        self.queue = QueueRepository(self.db)
        self.output = self.root / "output"

    def seed(self, *, aweme_id=AWEME, correction=CorrectionStatus.PENDING,
             summary_status="pending", publish=False) -> int:
        version_id = self.db.create_staging_version(
            aweme_id, metadata={"title": "标题", "author": "奶龙",
                                "publish_date": "2026-06-19",
                                "duration_seconds": 30.0})
        self.db.add_asr_attempt(
            version_id, attempt_no=1, audio_variant="original",
            strategy={"vad_filter": True}, model="medium", device="cuda",
            compute_type="float16", language="zh",
            transcript_text="五月五，过端午",
            segments_json=[{"start": 0.0, "end": 4.0, "text": "五月五，过端午",
                            "avg_logprob": -0.2, "no_speech_prob": 0.0,
                            "review_reasons": []}],
            metrics={"duration": 30.0}, hard_failures=[], score=90.0, passed=True)
        attempt = self.db.list_attempts(version_id)[0]
        self.db.select_attempt(version_id, attempt["attempt_id"])
        self.db.set_quality(version_id, status=QualityStatus.PASSED, score=90.0,
                            report={})
        self.db.save_summary(version_id, summary="**核心观点：**\n旧摘要",
                             status=summary_status, mode="normal", meta={})
        if correction != CorrectionStatus.PENDING:
            self.db.save_correction(version_id, corrected_text="五月五，过端午。",
                                    changes=[], status=correction)
        if publish:
            from scripts.artifacts import (publish_artifacts, render_evidence,
                                          render_main, build_version_bundle)

            bundle = build_version_bundle(self.db, version_id)
            publish_artifacts(self.db, version_id,
                              [render_main(bundle, self.output),
                               render_evidence(bundle, self.output)],
                              output_root=self.output)
        return version_id


class RepairArtifactsTests(MaintenanceTestCase):
    def test_missing_file_is_rebuilt_from_database(self):
        self.seed(publish=True, correction=CorrectionStatus.CORRECTED)
        version = self.db.get_current_version(AWEME)
        main = Path(self.db.list_artifacts(version["version_id"], "main_txt")[0]["path"])
        main.unlink()
        report = maintenance.repair_artifacts(self.db, output_root=self.output)
        self.assertEqual(report["repaired"], 1)
        self.assertTrue(main.is_file())
        self.assertEqual(report["checked"], 1)

    def test_healthy_delivery_is_left_untouched(self):
        self.seed(publish=True, correction=CorrectionStatus.CORRECTED)
        report = maintenance.repair_artifacts(self.db, output_root=self.output)
        self.assertEqual(report["repaired"], 0)
        self.assertEqual(report["ok"], 1)

    def test_repair_only_reads_the_database(self):
        self.seed(publish=True, correction=CorrectionStatus.CORRECTED)
        version = self.db.get_current_version(AWEME)
        Path(self.db.list_artifacts(version["version_id"], "main_txt")[0]["path"]).unlink()
        with mock.patch("scripts.ingest_runner.run_ingest",
                        side_effect=AssertionError("不得重新采集")), \
             mock.patch("scripts.asr.run_attempt",
                        side_effect=AssertionError("不得重新识别")):
            maintenance.repair_artifacts(self.db, output_root=self.output)

    def test_specific_aweme_id_can_be_targeted(self):
        self.seed(publish=True, correction=CorrectionStatus.CORRECTED)
        report = maintenance.repair_artifacts(self.db, aweme_id=AWEME,
                                              output_root=self.output)
        self.assertEqual(report["checked"], 1)


class FillAiTests(MaintenanceTestCase):
    def test_pending_correction_and_summary_are_filled(self):
        version_id = self.seed(publish=True)
        corrected = mock.Mock(status=str(CorrectionStatus.CORRECTED),
                              corrected_text="五月五，过端午。", changes=[], meta={})
        with mock.patch("scripts.correct_transcript.correct_or_pending",
                        return_value=corrected), \
             mock.patch("scripts.generate_summary.generate_or_pending",
                        return_value=("summarized", "**核心观点：**\n新摘要")):
            report = maintenance.fill_ai(self.db, output_root=self.output)
        self.assertEqual(report["filled"], 1)
        version = self.db.get_version(version_id)
        self.assertEqual(version["correction_status"],
                         str(CorrectionStatus.CORRECTED))
        self.assertEqual(version["summary_status"], "summarized")
        self.assertIn("新摘要", version["summary"])

    def test_main_file_is_rerendered_after_fill(self):
        version_id = self.seed(publish=True)
        corrected = mock.Mock(status=str(CorrectionStatus.CORRECTED),
                              corrected_text="五月五，过端午。", changes=[], meta={})
        with mock.patch("scripts.correct_transcript.correct_or_pending",
                        return_value=corrected), \
             mock.patch("scripts.generate_summary.generate_or_pending",
                        return_value=("summarized", "**核心观点：**\n新摘要")):
            maintenance.fill_ai(self.db, output_root=self.output)
        version = self.db.get_current_version(AWEME)
        main = Path(self.db.list_artifacts(version["version_id"], "main_txt")[0]["path"])
        body = main.read_text(encoding="utf-8")
        self.assertIn("校正版逐字稿", body)
        self.assertIn("新摘要", body)

    def test_fill_ai_never_touches_media(self):
        self.seed(publish=True)
        with mock.patch("scripts.video_media.materialize_video",
                        side_effect=AssertionError("不得下载媒体")), \
             mock.patch("scripts.audio_pipeline.analyze_audio",
                        side_effect=AssertionError("不得分析音频")):
            maintenance.fill_ai(self.db, output_root=self.output)

    def test_ai_failure_keeps_pending_without_raising(self):
        version_id = self.seed(publish=True)
        with mock.patch("scripts.correct_transcript.correct_or_pending",
                        side_effect=OSError("down")), \
             mock.patch("scripts.generate_summary.generate_or_pending",
                        side_effect=OSError("down")):
            report = maintenance.fill_ai(self.db, output_root=self.output)
        self.assertEqual(report["pending"], 1)
        self.assertEqual(report["filled"], 0)

    def test_already_filled_versions_are_skipped(self):
        self.seed(publish=True, correction=CorrectionStatus.CORRECTED,
                  summary_status="summarized")
        with mock.patch("scripts.correct_transcript.correct_or_pending") as corr:
            report = maintenance.fill_ai(self.db, output_root=self.output)
        corr.assert_not_called()
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["filled"], 0)


class RecoverInterruptedTests(MaintenanceTestCase):
    def test_stale_jobs_are_returned_to_the_queue(self):
        batch = self.queue.create_batch(
            [NormalizedInput(raw_input="x", url="https://www.douyin.com/video/1",
                             candidate_aweme_id="1")], confirmed=True)
        job = self.queue.claim_next(batch.batch_id)
        self.queue.update_status(job.job_id, JobStatus.TRANSCRIBING)
        self.db.connect().execute(
            "UPDATE jobs SET heartbeat_at = '2000-01-01 00:00:00' WHERE job_id = ?",
            (job.job_id,))
        self.db.connect().commit()
        report = maintenance.recover_interrupted(self.db)
        self.assertEqual(report["recovered"], 1)
        self.assertEqual(self.queue.get_job(job.job_id).status, JobStatus.RETRY_WAIT)

    def test_nothing_to_recover(self):
        report = maintenance.recover_interrupted(self.db)
        self.assertEqual(report["recovered"], 0)


class ReprocessReviewTests(MaintenanceTestCase):
    def test_review_versions_get_a_new_queue_item(self):
        self.seed(publish=True, correction=CorrectionStatus.CORRECTED)
        version = self.db.get_current_version(AWEME)
        self.db.set_quality(version["version_id"],
                            status=QualityStatus.NEEDS_REVIEW, score=60.0,
                            report={})
        # 让签名落后于当前管线，触发重处理判定
        self.db.connect().execute(
            "UPDATE transcript_versions SET processing_signature = 'v1.5' "
            "WHERE version_id = ?", (version["version_id"],))
        self.db.connect().commit()
        report = maintenance.reprocess_review(self.db)
        self.assertEqual(report["queued"], 1)
        self.assertTrue(report["batch_id"])
        jobs = self.queue.list_jobs(report["batch_id"])
        self.assertEqual([job.aweme_id for job in jobs], [AWEME])

    def test_current_pipeline_versions_are_left_alone(self):
        self.seed(publish=True, correction=CorrectionStatus.CORRECTED)
        report = maintenance.reprocess_review(self.db)
        self.assertEqual(report["queued"], 0)


class RollbackTests(MaintenanceTestCase):
    def _two_versions(self) -> tuple[int, int]:
        """真实两版：第一版已发布，第二版是更新的交付。"""
        old = self.seed(publish=True, correction=CorrectionStatus.CORRECTED)
        new = self.db.create_staging_version(
            AWEME, metadata={"title": "标题", "author": "奶龙",
                             "publish_date": "2026-06-19"})
        self.db.add_asr_attempt(
            new, attempt_no=1, audio_variant="original", strategy={},
            model="medium", device="cuda", compute_type="float16", language="zh",
            transcript_text="新原稿", segments_json=[], metrics={},
            hard_failures=[], score=91.0, passed=True)
        attempt = self.db.list_attempts(new)[0]
        self.db.select_attempt(new, attempt["attempt_id"])
        self.db.set_quality(new, status=QualityStatus.PASSED, score=91.0,
                            report={})
        self.db.save_summary(new, summary="新摘要", status="summarized",
                             mode="normal", meta={})
        self.db.save_correction(new, corrected_text="新校正", changes=[],
                                status=CorrectionStatus.CORRECTED)
        from scripts.artifacts import (build_version_bundle, publish_artifacts,
                                      render_evidence, render_main)

        bundle = build_version_bundle(self.db, new)
        publish_artifacts(self.db, new,
                          [render_main(bundle, self.output),
                           render_evidence(bundle, self.output)],
                          output_root=self.output)
        return old, new

    def test_rollback_switches_current_and_rerenders(self):
        old, new = self._two_versions()
        report = maintenance.rollback_version(self.db, AWEME, old,
                                              output_root=self.output)
        self.assertEqual(report["version_id"], old)
        self.assertEqual(self.db.get_current_version(AWEME)["version_id"], old)
        main = Path(report["main_file"]).read_text(encoding="utf-8")
        self.assertIn("五月五", main)

    def test_rollback_never_deletes_text_versions(self):
        old, new = self._two_versions()
        maintenance.rollback_version(self.db, AWEME, old, output_root=self.output)
        versions = self.db.list_versions(AWEME)
        self.assertEqual({item["version_id"] for item in versions}, {old, new})

    def test_rollback_rejects_unknown_version(self):
        self._two_versions()
        with self.assertRaises(Exception):
            maintenance.rollback_version(self.db, AWEME, 999999,
                                         output_root=self.output)


class CliTests(MaintenanceTestCase):
    def _cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with support.isolate_logging(self.root), \
             mock.patch.object(maintenance.paths, "DB_PATH",
                               self.root / ".db" / "t.db"), \
             mock.patch.object(maintenance.paths, "OUTPUT_DIR",
                               self.root / "output"), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = maintenance.main(argv)
        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        return code, json.loads(lines[-1]), lines

    def test_repair_artifacts_cli(self):
        self.seed(publish=True, correction=CorrectionStatus.CORRECTED)
        code, payload, lines = self._cli(["repair-artifacts", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["repair"]["ok"], 1)
        self.assertEqual(len(lines), 1)

    def test_fill_ai_cli(self):
        self.seed(publish=True)
        corrected = mock.Mock(status=str(CorrectionStatus.CORRECTED),
                              corrected_text="校正", changes=[], meta={})
        with mock.patch("scripts.correct_transcript.correct_or_pending",
                        return_value=corrected), \
             mock.patch("scripts.generate_summary.generate_or_pending",
                        return_value=("summarized", "摘要")):
            code, payload, _ = self._cli(["fill-ai", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["fill"]["filled"], 1)

    def test_rollback_requires_version_id(self):
        code, payload, _ = self._cli(["rollback-version", "--aweme-id", AWEME,
                                      "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "invalid_input")

    def test_unknown_command_is_usage_error(self):
        with self.assertRaises(SystemExit):
            maintenance.main(["nonsense"])


if __name__ == "__main__":
    unittest.main()
