"""AT-02 回归：复用批次状态时，不在本次待跑清单里的条目保持未运行状态。

原缺陷：旧状态里不在本次 pending 清单的条目被直接标成 done，以后永久
跳过；批次还可能被误判为 completed。用真实 main + BatchState、假扫描与
假任务复现（离线，不碰真实库/台账/日志/网络）。
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from scripts import batch_state
from scripts import batch_transcribe as batch
from tests.support import SkillTestCase


def _video(bvid: str, page: int = 1) -> dict:
    return {"bvid": bvid, "page": page, "title": "标题-%s" % bvid,
            "duration": "1分0秒", "upper": "作者", "cid": "",
            "part_title": ""}


class _DummyDB:
    """main 里的占位开库（AT-02 不测 DB 层，不碰真实库）。"""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ReusePendingTests(SkillTestCase):
    """两个待办视频 + limit=1 + 首项失败：尾项不得被标 done，批次不得报完工。"""

    def setUp(self):
        super().setUp()
        self.state_dir = self.root / "state"
        self.state_path = self.state_dir / "batch_state.json"
        batch_state.BatchState.create(
            path=self.state_path, batch_id="B-old",
            items=[{"key": "BV1", "bvid": "BV1", "page": 1},
                   {"key": "BV2", "bvid": "BV2", "page": 1}])

    def _run_main(self, videos, job_result, argv):
        run_job = mock.Mock(return_value=job_result)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                batch, "scan_videos", return_value=videos))
            stack.enter_context(mock.patch.object(batch, "run_job", run_job))
            stack.enter_context(mock.patch.object(
                batch, "STATE_DIR", self.state_dir))
            stack.enter_context(mock.patch.object(
                batch, "PROCESSED_FILE", self.state_dir / "processed.txt"))
            stack.enter_context(mock.patch.object(
                batch, "REPORT_FILE", self.state_dir / "report.csv"))
            stack.enter_context(mock.patch.object(
                batch, "OUTPUT_DIR", self.root / "output"))
            stack.enter_context(mock.patch.object(
                batch, "TranscriptDB", _DummyDB))
            stack.enter_context(mock.patch.object(
                batch, "verify_and_fix_titles", return_value=0))
            stack.enter_context(mock.patch.object(batch, "BASH", "bash-test"))
            for name in ("info", "success", "warn", "error"):
                stack.enter_context(mock.patch.object(batch, name))
            buffer = io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(buffer))
            rc = batch.main(argv)
        return rc, run_job

    def test_at_02_contract(self):
        fail = batch.JobResult(False, "boom", None, 1, "")
        rc, run_job = self._run_main(
            [_video("BV1"), _video("BV2")], fail, ["--limit", "1"])
        self.assertEqual(rc, 1, "首项失败时批次退出码应为 1")
        self.assertEqual(run_job.call_count, 1, "本次只应实际运行首项")
        state = batch_state.BatchState.load(self.state_path)
        first = state.get("BV1")
        second = state.get("BV2")
        self.assertEqual(first["status"], batch_state.FAILED,
                         "失败条目保留可重跑状态")
        self.assertEqual(second["status"], batch_state.PENDING,
                         "未实际运行的条目不得写 DONE")
        self.assertNotEqual(state.status, batch_state.BATCH_COMPLETED,
                            "还有未运行条目时批次不得报完工")

    def test_corrupted_done_is_revived_by_ledger_truth(self):
        """旧 bug 误标的 done（不在台账里）复用时转回待跑并实际执行。"""
        state = batch_state.BatchState.load(self.state_path)
        state.mark("BV2", batch_state.DONE)
        fail = batch.JobResult(False, "boom", None, 1, "")
        rc, run_job = self._run_main(
            [_video("BV1"), _video("BV2")], fail, [])
        self.assertEqual(rc, 1)
        self.assertEqual(run_job.call_count, 2, "误标条目应被复活并实际运行")
        state = batch_state.BatchState.load(self.state_path)
        self.assertEqual(state.get("BV2")["status"], batch_state.FAILED,
                         "复活后运行失败应记 failed（可重跑），而非留在 done")

    def test_ledger_completed_item_is_marked_done_without_running(self):
        """已落账条目补标 done 且不再运行（成功落账后崩溃的补救）。

        注：修复前它走「不在清单即 done」的旧分支同样为绿，语义靠本用例锁定。
        """
        (self.state_dir / "processed.txt").write_text("BV1\n", encoding="utf-8")
        fail = batch.JobResult(False, "boom", None, 1, "")
        rc, run_job = self._run_main(
            [_video("BV1"), _video("BV2")], fail, [])
        self.assertEqual(rc, 1)
        self.assertEqual(run_job.call_count, 1, "已落账条目不得再运行")
        state = batch_state.BatchState.load(self.state_path)
        self.assertEqual(state.get("BV1")["status"], batch_state.DONE)
        self.assertEqual(state.get("BV2")["status"], batch_state.FAILED)


class NextPendingFilterTests(unittest.TestCase):
    def test_keys_filter_limits_claims_to_this_run(self):
        tmp = Path(tempfile.mkdtemp(prefix="at02_"))
        state = batch_state.BatchState.create(
            path=tmp / "batch_state.json", batch_id="B-f",
            items=[{"key": "K1", "bvid": "K1", "page": 1},
                   {"key": "K2", "bvid": "K2", "page": 1}])
        self.assertEqual(state.next_pending({"K1"})["key"], "K1")
        state.mark("K1", batch_state.DONE)
        self.assertIsNone(state.next_pending({"K1"}),
                          "清单内无可领条目时应停下，而非领取清单外的条目")
        self.assertEqual(state.next_pending()["key"], "K2",
                         "不过滤时行为保持不变")
        self.assertEqual(state.get("K2")["status"], batch_state.PENDING)


if __name__ == "__main__":
    unittest.main()
