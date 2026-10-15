"""doctor.py 测试（日志隔离测试见 tests/test_logging.py）。"""

import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from scripts import doctor


class DoctorTests(unittest.TestCase):
    def _run(self, which_map, run_results=None, python_version=(3, 12, 10)):
        run_results = run_results or {}

        def fake_which(name):
            return which_map.get(name)

        def fake_run(argv, **kwargs):
            key = argv[0]
            value = run_results.get(key)
            if isinstance(value, Exception):
                raise value
            return value or mock.Mock(stdout="", stderr="", returncode=0)

        with mock.patch("shutil.which", side_effect=fake_which), \
             mock.patch.object(subprocess, "run", side_effect=fake_run), \
             mock.patch.object(doctor.sys, "version_info", python_version):
            return doctor.run_checks()

    def test_json_shape_always_stable(self):
        checks = self._run({})
        self.assertIn("ok", checks)
        self.assertIn("checks", checks)
        for item in checks["checks"]:
            self.assertIn("name", item)
            self.assertIn("ok", item)
            self.assertIn("detail", item)
            self.assertIn("fix_command", item)

    def test_missing_ffmpeg_is_reported_with_fix(self):
        checks = self._run({"python": "x", "ffprobe": None})
        ffmpeg = next(c for c in checks["checks"] if c["name"] == "ffmpeg")
        self.assertFalse(ffmpeg["ok"])
        self.assertIn("winget", ffmpeg["fix_command"])
        self.assertFalse(checks["ok"])

    def test_all_present_passes(self):
        ok = mock.Mock(stdout="ffmpeg version 7.0\n", stderr="", returncode=0)
        checks = self._run(
            {"python": "x", "ffprobe": "C:/ff.exe", "douyin-ingest": "C:/di.exe",
             "git": "C:/git.exe"},
            {"ffprobe": ok, "douyin-ingest": mock.Mock(
                stdout='{"schema_version": "1.0"}', stderr="", returncode=0)})
        by_name = {c["name"]: c for c in checks["checks"]}
        self.assertTrue(by_name["ffmpeg"]["ok"])
        self.assertTrue(by_name["python"]["ok"])

    def test_old_python_fails(self):
        checks = self._run({"python": "x"}, python_version=(3, 10, 0))
        self.assertFalse(next(c for c in checks["checks"]
                              if c["name"] == "python")["ok"])

    def test_python_311_is_rejected_too(self):
        # v2 把下限提高到 3.12：3.11 必须判失败并给出重建命令
        checks = self._run({"python": "x"}, python_version=(3, 11, 9))
        python_check = next(c for c in checks["checks"] if c["name"] == "python")
        self.assertFalse(python_check["ok"])
        self.assertIn("3.12", python_check["fix_command"])
        self.assertIn("3", python_check["detail"])

    def test_main_initializes_utf8_stdio_before_checks(self):
        order = []

        def fake_checks():
            order.append("checks")
            return {"ok": True, "checks": []}

        with mock.patch.object(doctor, "ensure_utf8_stdio",
                               side_effect=lambda: order.append("utf8")), \
             mock.patch.object(doctor, "run_checks", side_effect=fake_checks), \
             mock.patch("builtins.print"):
            code = doctor.main(["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(order, ["utf8", "checks"])

    def test_playwright_chromium_missing_reports_fix(self):
        with mock.patch.object(doctor, "chromium_installed",
                               side_effect=FileNotFoundError):
            checks = self._run({"python": "x", "ffprobe": "f"})
        pw = next(c for c in checks["checks"] if c["name"] == "playwright_chromium")
        self.assertFalse(pw["ok"])
        self.assertIn("playwright install chromium", pw["fix_command"])

    def test_whisper_check_importable(self):
        with mock.patch.object(doctor, "whisper_available", return_value=True):
            checks = self._run({"python": "x", "ffprobe": "f"})
        w = next(c for c in checks["checks"] if c["name"] == "faster_whisper")
        self.assertTrue(w["ok"])

    def test_summary_config_missing_key_is_warning_not_fatal(self):
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False), \
             mock.patch.object(doctor, "load_dotenv"):
            checks = self._run({"python": "x", "ffprobe": "f"})
        s = next(c for c in checks["checks"] if c["name"] == "summary_config")
        self.assertFalse(s["ok"])
        self.assertEqual(s["severity"], "warning")

    def test_cli_prints_single_json_line(self):
        with mock.patch.object(doctor, "run_checks",
                               return_value={"ok": True, "checks": []}), \
             mock.patch("builtins.print") as fake_print:
            code = doctor.main(["--json"])
        self.assertEqual(code, 0)
        payload = json.loads(fake_print.call_args.args[0])
        self.assertEqual(payload["ok"], True)


if __name__ == "__main__":
    unittest.main()
