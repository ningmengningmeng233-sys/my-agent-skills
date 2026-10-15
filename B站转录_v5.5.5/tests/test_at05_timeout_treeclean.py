"""AT-05 回归：单任务超时必须递归终止整个进程树，杜绝孤儿推理进程。

原缺陷：subprocess.run(timeout=7200) 超时只杀直接 shell 父进程，底层
推理/下载子进程成为孤儿，继续占显存、写文件，与后续任务冲突。
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import batch_transcribe as batch


class KillTreeTests(unittest.TestCase):
    """进程树清理：Windows 走 taskkill /T，POSIX 先子后父。"""

    def test_windows_uses_taskkill_with_tree_flag(self):
        self.assertTrue(hasattr(batch, "_kill_tree_windows"),
                        "缺少 Windows 进程树清理入口")
        with mock.patch.object(batch.subprocess, "run") as run:
            batch._kill_tree_windows(1234)
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["taskkill", "/F"])
        self.assertIn("/T", argv)
        self.assertIn("1234", argv)

    def test_windows_killer_never_raises(self):
        with mock.patch.object(batch.subprocess, "run",
                               side_effect=OSError("denied")):
            batch._kill_tree_windows(1234)  # 不应抛异常

    def test_posix_kills_children_before_parent(self):
        self.assertTrue(hasattr(batch, "_kill_tree_posix"),
                        "缺少 POSIX 进程树清理入口")
        order = []

        def fake_kill(pid, sig):
            order.append(pid)

        with mock.patch.object(batch, "_child_pids_posix",
                               side_effect=[[111, 222], [], [], []]), \
                mock.patch.object(batch.os, "kill", side_effect=fake_kill):
            batch._kill_tree_posix(999)
        self.assertEqual(order, [111, 222, 999])

    def test_kill_process_tree_dispatches_by_platform(self):
        self.assertTrue(hasattr(batch, "kill_process_tree"),
                        "缺少进程树清理统一入口")
        with mock.patch.object(batch, "_kill_tree_windows") as win, \
                mock.patch.object(batch.os, "name", "nt"):
            batch.kill_process_tree(4242)
        win.assert_called_once_with(4242)
        with mock.patch.object(batch, "_kill_tree_posix") as posix, \
                mock.patch.object(batch.os, "name", "posix"):
            batch.kill_process_tree(4242)
        posix.assert_called_once_with(4242)

    def test_kill_process_tree_ignores_garbage(self):
        batch.kill_process_tree(0)
        batch.kill_process_tree(-5)
        batch.kill_process_tree("not-a-pid")


class TranscribeTimeoutTests(unittest.TestCase):
    """transcribe_video 超时必须触发进程树清理并返回失败（不抛穿）。"""

    def test_timeout_triggers_tree_cleanup(self):
        self.assertTrue(hasattr(batch, "kill_process_tree"),
                        "缺少进程树清理入口，超时无法清理孤儿")
        fake_proc = mock.Mock()
        fake_proc.pid = 4242
        fake_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="x", timeout=7200),
            ("", ""),
        ]
        fake_proc.returncode = -9
        with mock.patch.object(batch.subprocess, "Popen",
                               return_value=fake_proc) as popen, \
                mock.patch.object(batch, "kill_process_tree") as killer, \
                mock.patch.object(batch, "OUTPUT_DIR",
                                  Path(tempfile.mkdtemp())), \
                contextlib.redirect_stdout(io.StringIO()):
            ok, output, source, used_stt, code = batch.transcribe_video(
                "BV1", 1)
        popen.assert_called_once()
        killer.assert_called_once_with(4242)
        self.assertFalse(ok)
        self.assertEqual(code, "timeout")

    def test_timeout_second_wait_kill_fallback(self):
        """清理后仍收不回进程时，补一次 kill 再回收，不抛异常。"""
        fake_proc = mock.Mock()
        fake_proc.pid = 4242
        fake_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="x", timeout=7200),
            subprocess.TimeoutExpired(cmd="x", timeout=60),
            ("", ""),
        ]
        fake_proc.returncode = -9
        with mock.patch.object(batch.subprocess, "Popen",
                               return_value=fake_proc), \
                mock.patch.object(batch, "kill_process_tree"), \
                mock.patch.object(batch, "OUTPUT_DIR",
                                  Path(tempfile.mkdtemp())), \
                contextlib.redirect_stdout(io.StringIO()):
            ok, *_rest = batch.transcribe_video("BV1", 1)
        self.assertFalse(ok)
        fake_proc.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
