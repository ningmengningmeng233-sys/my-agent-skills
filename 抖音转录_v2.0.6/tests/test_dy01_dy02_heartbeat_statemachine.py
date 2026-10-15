"""DY-01+02 回归：心跳混用格式恢复 + _Runner.enter 阶段持久化。

原缺陷：
- DY-01：heartbeat_at 以 ISO T 格式（utc_now）写入，却用空格格式 cutoff 做
  字符串比较，同日 stale 永远判不出来；float 时间戳心跳则因首字符 '1'<'2'
  被一律判为过期，新鲜心跳也被误回收。
- DY-02：_Runner.enter 用相同 status 调用 update_status，被 can_transition
  的同态拒绝挡掉，stage/checkpoint/心跳全部落不下去。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from scripts.douyin_transcript import _Runner
from scripts.job_queue import QueueRepository, STALE_HEARTBEAT_SECONDS
from scripts.models import JobStatus, NormalizedInput, Stage, utc_now
from scripts.transcript_db import TranscriptDB

AWEME = "7410000000000000001"


def _video() -> NormalizedInput:
    return NormalizedInput(
        raw_input=f"文案 https://www.douyin.com/video/{AWEME}",
        url=f"https://www.douyin.com/video/{AWEME}",
        candidate_aweme_id=AWEME, kind="video")


class _QueueCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = TranscriptDB(Path(self._tmp.name) / ".db" / "t.db")
        self.addCleanup(self.db.close)
        self.queue = QueueRepository(self.db)
        preview = self.queue.create_batch([_video()], confirmed=True)
        self.batch_id = preview.batch_id

    def _claim_fetching(self):
        job = self.queue.claim_next(self.batch_id)
        self.queue.update_status(job.job_id, JobStatus.FETCHING)
        return self.queue.get_job(job.job_id)

    def _set_heartbeat(self, job_id: str, value: str) -> None:
        conn = self.db.connect()
        conn.execute("UPDATE jobs SET heartbeat_at = ? WHERE job_id = ?",
                     (value, job_id))
        conn.commit()


class HeartbeatFormatTests(_QueueCase):
    """DY-01：ISO T 串与 float 时间戳都要能正确判断过期/新鲜。"""

    def test_stale_iso_t_heartbeat_is_recovered(self):
        job = self._claim_fetching()
        stale = (datetime.now() - timedelta(
            seconds=STALE_HEARTBEAT_SECONDS + 60)).strftime("%Y-%m-%dT%H:%M:%S")
        self._set_heartbeat(job.job_id, stale)
        recovered = self.queue.recover_stale_jobs()
        self.assertEqual([item.job_id for item in recovered], [job.job_id])
        self.assertEqual(self.queue.get_job(job.job_id).status,
                         JobStatus.RETRY_WAIT)

    def test_fresh_iso_t_heartbeat_is_not_recovered(self):
        job = self._claim_fetching()
        self._set_heartbeat(job.job_id, utc_now())
        self.assertEqual(self.queue.recover_stale_jobs(), [])
        self.assertEqual(self.queue.get_job(job.job_id).status,
                         JobStatus.FETCHING)

    def test_stale_float_heartbeat_is_recovered(self):
        job = self._claim_fetching()
        stale = str(time.time() - STALE_HEARTBEAT_SECONDS - 60)
        self._set_heartbeat(job.job_id, stale)
        recovered = self.queue.recover_stale_jobs()
        self.assertEqual([item.job_id for item in recovered], [job.job_id])
        self.assertEqual(self.queue.get_job(job.job_id).status,
                         JobStatus.RETRY_WAIT)

    def test_fresh_float_heartbeat_is_not_recovered(self):
        job = self._claim_fetching()
        self._set_heartbeat(job.job_id, str(time.time()))
        self.assertEqual(self.queue.recover_stale_jobs(), [])
        self.assertEqual(self.queue.get_job(job.job_id).status,
                         JobStatus.FETCHING)


class RunnerEnterTests(_QueueCase):
    """DY-02：enter 推进阶段必须落盘 stage/checkpoint/心跳，不被非法迁移拒绝。"""

    def _runner(self, job):
        return _Runner(job, SimpleNamespace(queue=self.queue))

    def test_enter_persists_stage_checkpoint_and_heartbeat(self):
        job = self.queue.claim_next(self.batch_id)
        before = self.queue.get_job(job.job_id).heartbeat_at
        runner = self._runner(job)
        runner.enter(Stage.FETCHING, audio="media/a.mp3")  # 不应抛非法迁移
        stored = self.queue.get_job(job.job_id)
        self.assertEqual(stored.stage, str(Stage.FETCHING))
        self.assertEqual(stored.checkpoint.get("audio"), "media/a.mp3")
        self.assertGreaterEqual(stored.heartbeat_at, before)
        self.assertTrue(stored.heartbeat_at)

    def test_enter_advances_status_forward_with_stage(self):
        job = self.queue.claim_next(self.batch_id)
        runner = self._runner(job)
        runner.enter(Stage.FETCHING)
        self.assertEqual(self.queue.get_job(job.job_id).status,
                         str(JobStatus.FETCHING))
        runner.enter(Stage.TRANSCRIBING)
        stored = self.queue.get_job(job.job_id)
        self.assertEqual(stored.status, str(JobStatus.TRANSCRIBING))
        self.assertEqual(stored.stage, str(Stage.TRANSCRIBING))

    def test_same_status_stage_write_is_not_rejected(self):
        job = self.queue.claim_next(self.batch_id)
        stored = self.queue.update_status(
            job.job_id, job.status, stage=Stage.FETCHING,
            checkpoint={"k": 1})
        self.assertEqual(stored.stage, str(Stage.FETCHING))
        self.assertEqual(stored.checkpoint.get("k"), 1)


if __name__ == "__main__":
    unittest.main()
