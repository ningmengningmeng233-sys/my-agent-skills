"""AT-04 回归：批量单实例排他锁 + 看门狗探测失败保守跳过。

原缺陷：
- 批量入口无运行锁，看门狗误判或双击会拉起并发批次，抢显存、撞风控。
- 看门狗 batch_processes() 探测异常时返回 []，被当成“无进程”强行续跑。
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from scripts import batch_transcribe as batch
from scripts import watchdog_resume as watchdog
from tests.support import SkillTestCase


def _video(bvid: str) -> dict:
    return {"bvid": bvid, "page": 1, "title": "标题-%s" % bvid,
            "duration": "1分0秒", "upper": "作者", "cid": "",
            "part_title": ""}


class _DummyDB:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class BatchLockTests(SkillTestCase):
    """存活 PID 持有锁时，第二个 batch 必须被拦截且不跑任何任务。"""

    def setUp(self):
        super().setUp()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _run_main(self, argv=None):
        run_job = mock.Mock(
            return_value=batch.JobResult(True, str(self.root / "o.txt"),
                                         "whisper", 1))
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                batch, "scan_videos",
                return_value=[_video("BV1"), _video("BV2")]))
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
            stack.enter_context(mock.patch.object(
                batch, "archive_transcript",
                side_effect=lambda p, b, pg: p))
            stack.enter_context(mock.patch.object(batch, "BASH", "bash-test"))
            for name in ("info", "success", "warn", "error"):
                stack.enter_context(mock.patch.object(batch, name))
            (self.root / "o.txt").write_text("x", encoding="utf-8")
            buffer = io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(buffer))
            rc = batch.main(argv or [])
        return rc, run_job, buffer.getvalue()

    def test_live_pid_blocks_second_batch(self):
        # 用父进程 PID（存活且不同于自身）模拟“已有批次进程”。
        (self.state_dir / "batch.pid").write_text(
            str(os.getppid()), encoding="utf-8")
        rc, run_job, out = self._run_main([])
        self.assertEqual(run_job.call_count, 0,
                         "已有存活批次时不得领取任何任务")
        self.assertEqual(rc, 0, "被锁拦截应优雅退出（0），不是报错")
        self.assertTrue(
            any(k in out for k in ("已有", "运行中", "锁", "PID", "pid")),
            "应提示已有批次运行中")

    def test_stale_pid_allows_new_batch(self):
        (self.state_dir / "batch.pid").write_text(
            "999999", encoding="utf-8")
        rc, run_job, _ = self._run_main(["--limit", "1"])
        self.assertGreaterEqual(run_job.call_count, 1,
                                "僵尸 PID 不得永久锁死批量")


class PidAliveTests(unittest.TestCase):
    """PID 存活探测：自身存活、已退出子进程判死（真探测，不 mock）。"""

    def test_self_is_alive(self):
        self.assertTrue(batch._pid_is_alive(os.getpid()))

    def test_dead_pid_is_dead(self):
        # 超出系统最大 PID 的值：从未存在，与已回收的僵尸走同一错误码路径
        #（Windows winerror=87，POSIX ESRCH）。
        self.assertFalse(batch._pid_is_alive(99999999))

    def test_garbage_pid_is_dead(self):
        self.assertFalse(batch._pid_is_alive(-1))
        self.assertFalse(batch._pid_is_alive(0))


class WatchdogProbeFailureTests(unittest.TestCase):
    """探测失败（权限/查询异常）必须保守跳过，不得判为无进程。"""

    def test_probe_exception_returns_none_not_empty(self):
        with mock.patch.object(watchdog.subprocess, "run",
                               side_effect=OSError("denied")):
            result = watchdog.batch_processes()
        self.assertIsNone(result, "探测异常应返回 None（未知），不是 []（无进程）")

    def test_probe_nonzero_exit_returns_none(self):
        failed = subprocess.CompletedProcess(
            args=["ps"], returncode=1, stdout="", stderr="denied")
        with mock.patch.object(watchdog.subprocess, "run",
                               return_value=failed):
            result = watchdog.batch_processes()
        self.assertIsNone(result, "探测命令非零退出应视为未知，不得续跑")

    def test_main_skips_resume_when_probe_unknown(self):
        tmp = Path(self._tmp_dir()) / "wd"
        tmp.mkdir(parents=True, exist_ok=True)
        state_path = tmp / "batch_state.json"
        state_path.write_text(
            '{"batch_id": "B-1", "status": "running", '
            '"items": [{"status": "pending"}]}', encoding="utf-8")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                watchdog, "batch_processes", return_value=None))
            stack.enter_context(mock.patch.object(
                watchdog, "LOCK_PATH", tmp / "watchdog.lock"))
            stack.enter_context(mock.patch.object(
                watchdog, "LOG_PATH", tmp / "watchdog.log"))
            launched = mock.Mock()
            stack.enter_context(mock.patch.object(
                watchdog, "launch_batch", launched))
            stack.enter_context(mock.patch.object(
                watchdog, "recover_stale", return_value=0))
            rc = watchdog.main(["--state", str(state_path)])
        self.assertEqual(rc, 0)
        launched.assert_not_called()

    def _tmp_dir(self):
        import tempfile
        path = tempfile.mkdtemp(prefix="at04_")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            path, ignore_errors=True))
        return path


if __name__ == "__main__":
    unittest.main()
