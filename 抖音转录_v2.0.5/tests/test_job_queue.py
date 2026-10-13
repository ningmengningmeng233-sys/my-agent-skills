"""持久队列测试：去重、预览阈值、串行领取、心跳中断与认证暂停恢复。"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from scripts.job_queue import (DEFAULT_SECONDS_PER_VIDEO, QueueRepository,
                               STALE_HEARTBEAT_SECONDS)
from scripts.models import (BatchStatus, JobStatus, NormalizedInput,
                            can_transition)
from scripts.transcript_db import TranscriptDB

AWEME_A = "7410000000000000001"
AWEME_B = "7410000000000000002"


def video(aweme_id: str) -> NormalizedInput:
    return NormalizedInput(
        raw_input=f"文案 https://www.douyin.com/video/{aweme_id}",
        url=f"https://www.douyin.com/video/{aweme_id}",
        candidate_aweme_id=aweme_id, kind="video")


def short_link(url: str = "https://v.douyin.com/aaa/") -> NormalizedInput:
    return NormalizedInput(raw_input=url, url=url, candidate_aweme_id="",
                           kind="short_link")


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = TranscriptDB(Path(self._tmp.name) / ".db" / "t.db")
        self.addCleanup(self.db.close)
        self.queue = QueueRepository(self.db)

    def _publish_deliverable(self, aweme_id: str) -> None:
        import hashlib
        from scripts.models import CorrectionStatus, PreparedArtifact

        version_id = self.db.create_staging_version(
            aweme_id, metadata={"title": "已转录"})
        self.db.set_raw_text(version_id, "原稿")
        self.db.save_correction(version_id, corrected_text="校正", changes=[],
                                status=CorrectionStatus.CORRECTED)
        self.db.save_summary(version_id, summary="摘要", status="summarized",
                             mode="normal", meta={})
        prepared = []
        for kind, name in (("main_txt", "主.txt"), ("evidence_txt", "证.txt")):
            path = Path(self._tmp.name) / name
            path.write_text(f"{kind} 内容", encoding="utf-8")
            raw = path.read_bytes()
            prepared.append(PreparedArtifact(kind=kind, path=str(path),
                                             sha256=hashlib.sha256(raw).hexdigest(),
                                             size_bytes=len(raw)))
        self.db.publish_version(version_id, prepared)


class CreateBatchTests(QueueTestCase):
    def test_jobs_keep_input_order(self):
        preview = self.queue.create_batch(
            [video(AWEME_A), video(AWEME_B)], source_type="chat",
            source_label="对话输入")
        jobs = self.queue.list_jobs(preview.batch_id)
        self.assertEqual([j.position for j in jobs], [0, 1])
        self.assertEqual([j.candidate_aweme_id for j in jobs],
                         [AWEME_A, AWEME_B])
        self.assertEqual([j.status for j in jobs], [JobStatus.PENDING] * 2)

    def test_duplicates_point_at_first_occurrence(self):
        preview = self.queue.create_batch(
            [video(AWEME_A), video(AWEME_B), video(AWEME_A)])
        self.assertEqual(preview.submitted, 3)
        self.assertEqual(preview.new, 2)
        self.assertEqual(preview.duplicates, 1)
        jobs = self.queue.list_jobs(preview.batch_id)
        self.assertEqual(jobs[2].status, JobStatus.DUPLICATE)
        self.assertEqual(jobs[2].duplicate_of_job_id, jobs[0].job_id)
        self.assertEqual(jobs[0].status, JobStatus.PENDING)

    def test_short_links_dedup_by_canonical_url(self):
        preview = self.queue.create_batch([short_link(), short_link()])
        self.assertEqual(preview.duplicates, 1)
        self.assertEqual(preview.new, 1)

    def test_reusable_video_is_counted_as_reused_not_new(self):
        self._publish_deliverable(AWEME_A)
        preview = self.queue.create_batch([video(AWEME_A), video(AWEME_B)])
        self.assertEqual(preview.reused, 1)
        self.assertEqual(preview.new, 1)
        jobs = self.queue.list_jobs(preview.batch_id)
        self.assertEqual(jobs[0].status, JobStatus.REUSED)
        self.assertEqual(jobs[1].status, JobStatus.PENDING)

class PreviewThresholdTests(QueueTestCase):
    def test_ten_items_run_without_confirmation(self):
        preview = self.queue.create_batch(
            [video(f"741000000000000{i:04d}") for i in range(10)])
        self.assertFalse(preview.requires_confirmation)
        self.assertEqual(preview.status, BatchStatus.PENDING)
        self.assertLessEqual(preview.estimated_seconds, 1800)

    def test_eleven_items_require_confirmation(self):
        preview = self.queue.create_batch(
            [video(f"741000000000000{i:04d}") for i in range(11)])
        self.assertTrue(preview.requires_confirmation)
        self.assertEqual(preview.status, BatchStatus.AWAITING_CONFIRMATION)
        self.assertGreater(preview.estimated_seconds, 1800)
        self.assertEqual(self.queue.list_jobs(preview.batch_id)[0].status,
                         JobStatus.PENDING)

    def test_explicit_confirmation_skips_preview(self):
        preview = self.queue.create_batch(
            [video(f"741000000000000{i:04d}") for i in range(11)],
            confirmed=True)
        self.assertFalse(preview.requires_confirmation)
        self.assertEqual(preview.status, BatchStatus.PENDING)

    def test_estimate_uses_history_when_available(self):
        # 历史平均：180 秒/条 → 8 条约 1440 秒
        for index in range(3):
            batch = self.queue.create_batch([video(f"7410000000000009{index:03d}")])
            job = self.queue.claim_next(batch.batch_id)
            self.queue.finish_job(job.job_id, JobStatus.COMPLETED)
            started = datetime.now() - timedelta(seconds=180)
            self.db.connect().execute(
                "UPDATE jobs SET created_at = ?, updated_at = ? WHERE job_id = ?",
                (started.strftime("%Y-%m-%d %H:%M:%S"),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), job.job_id))
            self.db.connect().commit()
        estimate = self.queue.estimate_seconds(new_count=8)
        self.assertGreater(estimate, 1300)
        self.assertLess(estimate, 1600)

    def test_estimate_falls_back_to_conservative_default(self):
        self.assertEqual(self.queue.estimate_seconds(new_count=4),
                         4 * DEFAULT_SECONDS_PER_VIDEO)


class ClaimTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.preview = self.queue.create_batch(
            [video(AWEME_A), video(AWEME_B)], confirmed=True)

    def test_claim_returns_lowest_position_then_next(self):
        first = self.queue.claim_next(self.preview.batch_id)
        self.assertEqual(first.candidate_aweme_id, AWEME_A)
        self.assertEqual(first.status, JobStatus.RESOLVING)
        second = self.queue.claim_next(self.preview.batch_id)
        self.assertEqual(second.candidate_aweme_id, AWEME_B)
        self.assertIsNone(self.queue.claim_next(self.preview.batch_id))

    def test_claim_marks_batch_running_and_sets_heartbeat(self):
        job = self.queue.claim_next(self.preview.batch_id)
        batch = self.queue.get_batch(self.preview.batch_id)
        self.assertEqual(batch["status"], BatchStatus.RUNNING)
        self.assertTrue(job.heartbeat_at)

    def test_claim_from_awaiting_confirmation_is_refused(self):
        preview = self.queue.create_batch(
            [video(f"741000000000000{i:04d}") for i in range(11)])
        self.assertIsNone(self.queue.claim_next(preview.batch_id))

    def test_heartbeat_updates_timestamp(self):
        job = self.queue.claim_next(self.preview.batch_id)
        self.queue.heartbeat(job.job_id)
        refreshed = self.queue.get_job(job.job_id)
        self.assertGreaterEqual(refreshed.heartbeat_at, job.heartbeat_at)

    def test_status_transitions_are_forward_only(self):
        job = self.queue.claim_next(self.preview.batch_id)
        self.queue.update_status(job.job_id, JobStatus.FETCHING)
        self.queue.update_status(job.job_id, JobStatus.MEDIA_READY)
        self.queue.update_status(job.job_id, JobStatus.TRANSCRIBING)
        with self.assertRaises(ValueError):
            self.queue.update_status(job.job_id, JobStatus.FETCHING)

    def test_terminal_job_cannot_move_again(self):
        job = self.queue.claim_next(self.preview.batch_id)
        self.queue.finish_job(job.job_id, JobStatus.COMPLETED)
        with self.assertRaises(ValueError):
            self.queue.update_status(job.job_id, JobStatus.TRANSCRIBING)

    def test_retry_wait_can_reenter_any_stage(self):
        job = self.queue.claim_next(self.preview.batch_id)
        self.queue.update_status(job.job_id, JobStatus.RETRY_WAIT)
        self.queue.update_status(job.job_id, JobStatus.TRANSCRIBING)
        self.assertEqual(self.queue.get_job(job.job_id).status,
                         JobStatus.TRANSCRIBING)


class TransitionRuleTests(unittest.TestCase):
    def test_pipeline_is_forward_only_but_skippable(self):
        self.assertTrue(can_transition(JobStatus.FETCHING, JobStatus.MEDIA_READY))
        self.assertFalse(can_transition(JobStatus.TRANSCRIBING, JobStatus.FETCHING))
        # 局部降级：OCR/说话人失败可以跳过
        self.assertTrue(can_transition(JobStatus.TRANSCRIBING, JobStatus.CORRECTING))

    def test_any_stage_can_fail_or_pause(self):
        self.assertTrue(can_transition(JobStatus.OCR, JobStatus.FAILED))
        self.assertTrue(can_transition(JobStatus.TRANSCRIBING, JobStatus.AUTH_REQUIRED))
        self.assertTrue(can_transition(JobStatus.PUBLISHING, JobStatus.COMPLETED))

    def test_auth_required_can_continue_after_login(self):
        self.assertTrue(can_transition(JobStatus.AUTH_REQUIRED, JobStatus.RETRY_WAIT))
        self.assertTrue(can_transition(JobStatus.AUTH_REQUIRED, JobStatus.FETCHING))
        self.assertFalse(can_transition(JobStatus.AUTH_REQUIRED, JobStatus.COMPLETED))

    def test_same_status_is_not_a_transition(self):
        self.assertFalse(can_transition(JobStatus.FETCHING, JobStatus.FETCHING))
        self.assertFalse(can_transition(JobStatus.COMPLETED, JobStatus.COMPLETED))


class AuthPauseTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.preview = self.queue.create_batch(
            [video(AWEME_A), video(AWEME_B), video("7410000000000000003")],
            confirmed=True)

    def test_pause_blocks_claiming_further_jobs(self):
        first = self.queue.claim_next(self.preview.batch_id)
        self.queue.finish_job(first.job_id, JobStatus.COMPLETED)
        second = self.queue.claim_next(self.preview.batch_id)
        self.queue.pause_for_auth(second.job_id)
        self.assertEqual(self.queue.get_batch(self.preview.batch_id)["status"],
                         BatchStatus.PAUSED_AUTH)
        self.assertEqual(self.queue.get_job(second.job_id).status,
                         JobStatus.AUTH_REQUIRED)
        self.assertIsNone(self.queue.claim_next(self.preview.batch_id))

    def test_resume_continues_from_the_auth_job_without_redoing_done(self):
        first = self.queue.claim_next(self.preview.batch_id)
        self.queue.finish_job(first.job_id, JobStatus.COMPLETED)
        second = self.queue.claim_next(self.preview.batch_id)
        self.queue.pause_for_auth(second.job_id)

        summary = self.queue.resume_batch(self.preview.batch_id)
        self.assertEqual(summary["resumed_jobs"], 1)
        self.assertEqual(self.queue.get_batch(self.preview.batch_id)["status"],
                         BatchStatus.RUNNING)
        self.assertEqual(self.queue.get_job(first.job_id).status,
                         JobStatus.COMPLETED)

        resumed = self.queue.claim_next(self.preview.batch_id)
        self.assertEqual(resumed.job_id, second.job_id)
        third = self.queue.claim_next(self.preview.batch_id)
        self.assertEqual(third.candidate_aweme_id, "7410000000000000003")

    def test_resume_refuses_unpaused_batch(self):
        with self.assertRaises(ValueError):
            self.queue.resume_batch(self.preview.batch_id)


class StaleRecoveryTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.preview = self.queue.create_batch([video(AWEME_A)], confirmed=True)

    def _age_heartbeat(self, job_id: str, seconds: int) -> None:
        stale = (datetime.now() - timedelta(seconds=seconds)).strftime(
            "%Y-%m-%d %H:%M:%S")
        self.db.connect().execute(
            "UPDATE jobs SET heartbeat_at = ? WHERE job_id = ?", (stale, job_id))
        self.db.connect().commit()

    def test_stale_processing_job_returns_to_retry_wait_with_checkpoint(self):
        job = self.queue.claim_next(self.preview.batch_id)
        self.queue.update_status(job.job_id, JobStatus.FETCHING,
                                 checkpoint={"audio": "media/a.mp3"})
        self._age_heartbeat(job.job_id, STALE_HEARTBEAT_SECONDS + 60)
        recovered = self.queue.recover_stale_jobs()
        self.assertEqual([item.job_id for item in recovered], [job.job_id])
        refreshed = self.queue.get_job(job.job_id)
        self.assertEqual(refreshed.status, JobStatus.RETRY_WAIT)
        self.assertEqual(refreshed.checkpoint, {"audio": "media/a.mp3"})

    def test_fresh_processing_job_is_untouched(self):
        job = self.queue.claim_next(self.preview.batch_id)
        self.queue.update_status(job.job_id, JobStatus.TRANSCRIBING)
        self.assertEqual(self.queue.recover_stale_jobs(), [])
        self.assertEqual(self.queue.get_job(job.job_id).status,
                         JobStatus.TRANSCRIBING)

    def test_recovered_job_is_claimable_again(self):
        job = self.queue.claim_next(self.preview.batch_id)
        self.queue.update_status(job.job_id, JobStatus.FETCHING)
        self._age_heartbeat(job.job_id, STALE_HEARTBEAT_SECONDS + 60)
        self.queue.recover_stale_jobs()
        reclaimed = self.queue.claim_next(self.preview.batch_id)
        self.assertEqual(reclaimed.job_id, job.job_id)


class PostIngestDedupTests(QueueTestCase):
    def test_short_link_merging_into_earlier_job(self):
        preview = self.queue.create_batch([video(AWEME_A), short_link()],
                                         confirmed=True)
        first = self.queue.claim_next(preview.batch_id)
        self.queue.update_status(first.job_id, JobStatus.MEDIA_READY,
                                 aweme_id=AWEME_A)
        second = self.queue.claim_next(preview.batch_id)
        merged = self.queue.resolve_aweme_id(second.job_id, AWEME_A)
        self.assertIsNotNone(merged)
        self.assertEqual(merged.job_id, first.job_id)
        refreshed = self.queue.get_job(second.job_id)
        self.assertEqual(refreshed.status, JobStatus.DUPLICATE)
        self.assertEqual(refreshed.duplicate_of_job_id, first.job_id)
        self.assertEqual(refreshed.aweme_id, AWEME_A)

    def test_new_aweme_id_is_recorded_without_merging(self):
        preview = self.queue.create_batch(
            [video(AWEME_A), video(AWEME_B)], confirmed=True)
        first = self.queue.claim_next(preview.batch_id)
        self.queue.update_status(first.job_id, JobStatus.MEDIA_READY,
                                 aweme_id=AWEME_A)
        second = self.queue.claim_next(preview.batch_id)
        self.assertIsNone(self.queue.resolve_aweme_id(second.job_id, AWEME_B))
        self.assertEqual(self.queue.get_job(second.job_id).aweme_id, AWEME_B)
        self.assertEqual(self.queue.get_job(second.job_id).status,
                         JobStatus.RESOLVING)


class BatchTotalsTests(QueueTestCase):
    def test_totals_count_every_outcome(self):
        preview = self.queue.create_batch(
            [video(f"741000000000000{i:04d}") for i in range(4)], confirmed=True)
        outcomes = [JobStatus.COMPLETED, JobStatus.REUSED,
                    JobStatus.NEEDS_REVIEW, JobStatus.FAILED]
        for outcome in outcomes:
            job = self.queue.claim_next(preview.batch_id)
            self.queue.finish_job(job.job_id, outcome)
        totals = self.queue.batch_totals(preview.batch_id)
        self.assertEqual(totals["completed"], 1)
        self.assertEqual(totals["reused"], 1)
        self.assertEqual(totals["needs_review"], 1)
        self.assertEqual(totals["failed"], 1)
        self.assertEqual(sum(totals.values()), 4)

    def test_batch_with_reused_jobs_still_finishes(self):
        self._publish_deliverable(AWEME_A)
        preview = self.queue.create_batch([video(AWEME_A), video(AWEME_B)],
                                         confirmed=True)
        reused = self.queue.list_jobs(preview.batch_id)[0]
        self.assertEqual(reused.status, JobStatus.REUSED)
        pending = self.queue.claim_next(preview.batch_id)
        self.assertEqual(pending.candidate_aweme_id, AWEME_B)
        self.queue.finish_job(pending.job_id, JobStatus.COMPLETED)
        # 复用项已是终态：批次必须结束，不能永远停在 running
        self.assertEqual(self.queue.get_batch(preview.batch_id)["status"],
                         BatchStatus.COMPLETED)

    def test_all_reused_batch_completes_immediately(self):
        self._publish_deliverable(AWEME_A)
        preview = self.queue.create_batch([video(AWEME_A)], confirmed=True)
        self.assertEqual(preview.reused, 1)
        self.assertEqual(preview.status, BatchStatus.COMPLETED)
        self.assertIsNone(self.queue.claim_next(preview.batch_id))

    def test_batch_finishes_as_completed_when_no_failure(self):
        preview = self.queue.create_batch([video(AWEME_A)], confirmed=True)
        job = self.queue.claim_next(preview.batch_id)
        self.queue.finish_job(job.job_id, JobStatus.COMPLETED)
        self.assertEqual(self.queue.get_batch(preview.batch_id)["status"],
                         BatchStatus.COMPLETED)

    def test_batch_finishes_with_errors_when_something_failed(self):
        preview = self.queue.create_batch(
            [video(AWEME_A), video(AWEME_B)], confirmed=True)
        first = self.queue.claim_next(preview.batch_id)
        self.queue.finish_job(first.job_id, JobStatus.FAILED)
        second = self.queue.claim_next(preview.batch_id)
        self.queue.finish_job(second.job_id, JobStatus.COMPLETED)
        self.assertEqual(self.queue.get_batch(preview.batch_id)["status"],
                         BatchStatus.COMPLETED_WITH_ERRORS)

    def test_raw_input_is_trimmed_after_batch_completion(self):
        long_text = "x" * 5000 + f" https://www.douyin.com/video/{AWEME_A}"
        preview = self.queue.create_batch(
            [NormalizedInput(raw_input=long_text,
                             url=f"https://www.douyin.com/video/{AWEME_A}",
                             candidate_aweme_id=AWEME_A, kind="video")],
            confirmed=True)
        job = self.queue.claim_next(preview.batch_id)
        self.queue.finish_job(job.job_id, JobStatus.COMPLETED)
        stored = self.queue.get_job(job.job_id).raw_input
        self.assertLessEqual(len(stored), 2000)
        self.assertNotIn("?", stored)


if __name__ == "__main__":
    unittest.main()
