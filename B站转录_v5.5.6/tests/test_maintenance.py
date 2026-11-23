"""maintenance 子命令的行为测试：统一入口、僵尸回收、暂停恢复、状态查看。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import batch_state, maintenance


class MaintenanceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="maint_"))
        self.state_path = self.tmp / "batch_state.json"

    def make_state(self, status="running"):
        state = batch_state.BatchState.create(
            path=self.state_path, batch_id="B-1",
            items=[{"key": "BV1", "bvid": "BV1", "page": 1},
                   {"key": "BV2", "bvid": "BV2", "page": 1}])
        if status != "running":
            state.status = status
            state.save()
        return state

    def run_cmd(self, argv):
        buffer = io.StringIO()
        with mock.patch.object(maintenance, "STATE_PATH", self.state_path):
            with contextlib.redirect_stdout(buffer):
                code = maintenance.main(argv)
        return code, buffer.getvalue()


class CommandSurfaceTests(MaintenanceCase):
    def test_all_documented_commands_exist(self):
        for name in ("doctor", "recover-stale", "resume-batch",
                     "list-batches", "verify"):
            with self.subTest(command=name):
                self.assertIn(name, maintenance.COMMANDS)

    def test_unknown_command_is_rejected(self):
        code, out = self.run_cmd(["nonsense"])
        self.assertNotEqual(code, 0)

    def test_no_command_prints_usage(self):
        code, out = self.run_cmd([])
        self.assertNotEqual(code, 0)
        self.assertIn("--help", out + str(code))


class RecoverStaleTests(MaintenanceCase):
    def test_recover_stale_requeues_running_items(self):
        state = self.make_state()
        state.claim("BV1")
        state.set_heartbeat("BV1", "2020-01-01T00:00:00+00:00")
        code, out = self.run_cmd(["recover-stale", "--seconds", "60", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload["recovered"], 1)

    def test_recover_stale_without_state_file_is_not_an_error(self):
        code, out = self.run_cmd(["recover-stale", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload["recovered"], 0)


class ResumeBatchTests(MaintenanceCase):
    def test_resume_requires_a_paused_batch(self):
        self.make_state(status="running")
        code, out = self.run_cmd(["resume-batch", "--json"])
        self.assertNotEqual(code, 0)

    def test_resume_moves_auth_items_to_retry(self):
        state = self.make_state()
        state.claim("BV1")
        state.pause_for_auth("BV1")
        code, out = self.run_cmd(["resume-batch", "--json"])
        self.assertEqual(code, 0)
        reloaded = batch_state.BatchState.load(self.state_path)
        self.assertEqual(reloaded.status, "running")
        self.assertEqual(reloaded.get("BV1")["status"], "retry_wait")

    def test_resume_missing_state_file_is_an_error(self):
        code, out = self.run_cmd(["resume-batch", "--json"])
        self.assertNotEqual(code, 0)


class ListBatchesTests(MaintenanceCase):
    def test_list_reports_status_counts(self):
        state = self.make_state()
        state.mark("BV1", "done")
        code, out = self.run_cmd(["list-batches", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload["batch_id"], "B-1")
        self.assertEqual(payload["counts"]["done"], 1)

    def test_list_without_state_file_is_empty_not_error(self):
        code, out = self.run_cmd(["list-batches", "--json"])
        self.assertEqual(code, 0)


class VerifyTests(MaintenanceCase):
    def test_verify_reports_database_and_archive(self):
        code, out = self.run_cmd(["verify", "--json"])
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertIn("database", payload)
        self.assertIn("archive", payload)
        self.assertIsInstance(code, int)


if __name__ == "__main__":
    unittest.main()
