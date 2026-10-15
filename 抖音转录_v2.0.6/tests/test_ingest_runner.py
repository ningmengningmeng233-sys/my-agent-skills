"""ingest_runner 子进程边界测试（全部 mock，不真正调用上游）。

v2 起把上游失败细分为契约变化 / 作品不可用 / 限流 / 超时等稳定分类，
因此原“一律 platform_failed”的断言按 spec §7.4、§18 更新。
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ingest_runner
from scripts.ingest_runner import RetryPolicy

FAST_POLICY = RetryPolicy(sleep=lambda _delay: None)


def _completed(stdout: str, returncode: int = 0) -> mock.Mock:
    return mock.Mock(stdout=stdout, stderr="", returncode=returncode)


class RunIngestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job_dir = Path(self._tmp.name) / "job1"
        (self.job_dir / "media").mkdir(parents=True)
        self.patcher = mock.patch.object(
            ingest_runner, "resolve_executable",
            return_value=Path("C:/venv/Scripts/douyin-ingest.exe"))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _run(self, stdout="{}", returncode=0, side_effect=None):
        with mock.patch.object(ingest_runner.subprocess, "run") as fake:
            if side_effect is not None:
                fake.side_effect = side_effect
            else:
                fake.return_value = _completed(stdout, returncode)
            result = ingest_runner.run_ingest(
                "https://v.douyin.com/abc/", self.job_dir, headless=True,
                retry_policy=FAST_POLICY)
        return result, fake

    def test_command_flags_and_env(self):
        payload = json.dumps({"ok": True, "videos": []})
        result, fake = self._run(payload)
        argv = fake.call_args.args[0]
        kwargs = fake.call_args.kwargs
        self.assertEqual(Path(argv[0]), Path("C:/venv/Scripts/douyin-ingest.exe"))
        self.assertEqual(argv[1], "https://v.douyin.com/abc/")
        self.assertIn("--json", argv)
        self.assertIn("--headless", argv)
        idx = argv.index("--speech-audio-dir")
        self.assertEqual(argv[idx + 1], str(self.job_dir / "media"))
        self.assertNotIn("shell", kwargs)  # 绝不使用 shell=True
        self.assertTrue(kwargs["capture_output"] and kwargs["text"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["timeout"], 300)
        self.assertEqual(Path(kwargs["env"]["DOUYIN_INGEST_HOME"]),
                         (Path(ingest_runner.__file__).parents[1]
                          / "state" / "ingest"))

    def test_headless_false_omits_flag(self):
        with mock.patch.object(ingest_runner.subprocess, "run",
                               return_value=_completed('{"ok": true}')) as fake:
            ingest_runner.run_ingest("x", self.job_dir, headless=False,
                                     retry_policy=FAST_POLICY)
        argv = fake.call_args.args[0]
        self.assertNotIn("--headless", argv)

    def test_success_payload_returns_ok(self):
        payload = {"ok": True, "videos": [{"aweme_id": "1"}]}
        result, _ = self._run(json.dumps(payload))
        self.assertEqual(result.kind, "ok")
        self.assertEqual(result.payload, payload)

    def test_authentication_error_maps_to_login_required(self):
        payload = {"ok": False,
                   "error": {"type": "AuthenticationError", "message": "需要登录"}}
        result, _ = self._run(json.dumps(payload), returncode=1)
        self.assertEqual(result.kind, "login_required")
        self.assertNotIn("douyin-ingest.exe", result.message)

    def test_other_error_maps_to_platform_failed(self):
        payload = {"ok": False, "error": {"type": "MediaExtractionError",
                                          "message": "无法解析口播音轨"}}
        result, _ = self._run(json.dumps(payload), returncode=1)
        self.assertEqual(result.kind, "platform_failed")

    def test_capture_timeout_message_is_classified_as_timeout(self):
        payload = {"ok": False, "error": {"type": "CaptureError", "message": "页面超时"}}
        result, fake = self._run(json.dumps(payload), returncode=1)
        self.assertEqual(result.kind, "ingest_timeout")
        self.assertEqual(fake.call_count, 3)

    def test_non_json_stdout_is_contract_changed(self):
        result, fake = self._run("not json at all")
        self.assertEqual(result.kind, "ingest_contract_changed")
        self.assertEqual(fake.call_count, 1)

    def test_multiple_json_output_is_contract_changed(self):
        result, _ = self._run('{"ok": true}\n{"ok": true}')
        self.assertEqual(result.kind, "ingest_contract_changed")

    def test_empty_stdout_is_contract_changed(self):
        result, _ = self._run("")
        self.assertEqual(result.kind, "ingest_contract_changed")

    def test_timeout_is_retried_then_reported_as_timeout(self):
        result, fake = self._run(side_effect=subprocess.TimeoutExpired(cmd="x", timeout=300))
        self.assertEqual(result.kind, "ingest_timeout")
        self.assertEqual(fake.call_count, 3)
        self.assertTrue(result.message)

    def test_message_never_leaks_job_dir(self):
        payload = {"ok": False, "error": {"type": "CaptureError",
                                          "message": f"写入 {self.job_dir} 失败"}}
        result, _ = self._run(json.dumps(payload), returncode=1)
        self.assertNotIn(str(self.job_dir), result.message)


if __name__ == "__main__":
    unittest.main()
