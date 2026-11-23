"""doctor v2 检查矩阵：核心缺失为 error，可选能力缺失为 warning 并说明降级。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import doctor
from tests.conftest import requires_env


class DoctorMatrixTestCase(unittest.TestCase):
    def _run(self, *, python_version=(3, 12, 10), which=None, run_results=None,
             patches=None):
        which = which if which is not None else {}
        run_results = run_results or {}

        def fake_which(name):
            return which.get(name)

        def fake_run(argv, **kwargs):
            value = run_results.get(argv[0])
            if isinstance(value, Exception):
                raise value
            return value or mock.Mock(stdout="", stderr="", returncode=0)

        with mock.patch("shutil.which", side_effect=fake_which), \
             mock.patch.object(subprocess, "run", side_effect=fake_run), \
             mock.patch.object(doctor.sys, "version_info", python_version):
            for target, value in (patches or {}).items():
                patcher = mock.patch.object(doctor, target, value) \
                    if isinstance(target, str) and hasattr(doctor, target) else None
                if patcher:
                    patcher.start()
                    self.addCleanup(patcher.stop)
            return doctor.run_checks()

    def _names(self, report):
        return [item["name"] for item in report["checks"]]


class CheckMatrixTests(DoctorMatrixTestCase):
    def test_report_shape_is_stable(self):
        report = self._run()
        for item in report["checks"]:
            for key in ("name", "ok", "detail", "fix_command", "severity"):
                self.assertIn(key, item)
            self.assertIn(item["severity"], ("error", "warning"))

    def test_all_core_checks_are_present(self):
        names = self._names(self._run())
        for expected in ("python", "ffmpeg", "playwright_chromium",
                         "douyin_ingest", "faster_whisper", "cuda",
                         "rapidocr", "sherpa_onnx", "model_assets",
                         "summary_config", "database", "writable_dirs"):
            self.assertIn(expected, names)

    def test_python_311_is_rejected_with_rebuild_command(self):
        report = self._run(python_version=(3, 11, 9))
        item = next(i for i in report["checks"] if i["name"] == "python")
        self.assertFalse(item["ok"])
        self.assertEqual(item["severity"], "error")
        self.assertIn("3.12", item["fix_command"])

    def test_missing_ingest_reports_exact_fix(self):
        # PATH 与解释器同目录都找不到 CLI 时必须报错并给出安装命令
        with mock.patch.object(sys, "executable", "/nonexistent/python.exe"),              mock.patch("shutil.which", return_value=None),              mock.patch.object(subprocess, "run",
                               return_value=mock.Mock(stdout="", stderr="",
                                                      returncode=0)):
            report = doctor.run_checks()
        item = next(i for i in report["checks"] if i["name"] == "douyin_ingest")
        self.assertFalse(item["ok"])
        self.assertIn("requirements.lock", item["fix_command"])

    def test_ingest_version_is_reported(self):
        # P 级：doctor 报告里 ingest 项必须回显已装版本；喂确定夹具而非依赖本机恰好装上。
        report = self._run(which={"douyin-ingest": "C:/fake/douyin-ingest.exe"},
                           patches={"installed_version": lambda name: "0.3.0"})
        item = next(i for i in report["checks"] if i["name"] == "douyin_ingest")
        self.assertIn("0.3.0", item["detail"])
        self.assertTrue(item["ok"], "版本匹配锁定版时应判 ok")
        self.assertEqual(item["severity"], "error")

    def test_ingest_version_mismatch_is_blocking_error_and_exit_code(self):
        # ingest 版本红线（问题清单要求）：非锁定版本必须判 error 并由 --doctor 退出码阻断，
        # 而不是靠单测断言本机 ingest 恰好等于某版本。
        with mock.patch("shutil.which",
                        return_value="C:/fake/douyin-ingest.exe"), \
             mock.patch.object(subprocess, "run",
                               return_value=mock.Mock(stdout="", stderr="",
                                                      returncode=0)), \
             mock.patch.object(doctor, "installed_version",
                               lambda name: "0.2.9"):
            item = doctor.check_ingest()
            self.assertFalse(item["ok"])
            self.assertEqual(item["severity"], "error")
            self.assertIn("0.3.0", item["detail"])
            # 该 error 项进入报告后整体判未就绪，main 退出码 1
            with mock.patch.object(doctor, "run_checks",
                                   return_value={"ok": False, "checks": [item]}):
                self.assertEqual(doctor.main([]), 1)

    def test_cuda_absent_is_warning_not_error(self):
        report = self._run(patches={"detect_cuda": lambda: False})
        item = next(i for i in report["checks"] if i["name"] == "cuda")
        self.assertFalse(item["ok"])
        self.assertEqual(item["severity"], "warning")
        self.assertIn("CPU", item["detail"])

    def test_cuda_present_reports_vram_and_tier(self):
        report = self._run(patches={"detect_cuda": lambda: True,
                                    "gpu_vram_gb": lambda: 8.0})
        item = next(i for i in report["checks"] if i["name"] == "cuda")
        self.assertTrue(item["ok"])
        self.assertIn("8", item["detail"])
        self.assertIn("medium", item["detail"])

    def test_missing_ocr_library_is_error(self):
        report = self._run(patches={"module_available":
                                    lambda name: name != "rapidocr"})
        item = next(i for i in report["checks"] if i["name"] == "rapidocr")
        self.assertFalse(item["ok"])
        self.assertEqual(item["severity"], "error")
        self.assertIn("requirements.lock", item["fix_command"])

    def test_missing_sherpa_is_warning_with_degradation_note(self):
        report = self._run(patches={"module_available":
                                    lambda name: name != "sherpa_onnx"})
        item = next(i for i in report["checks"] if i["name"] == "sherpa_onnx")
        self.assertFalse(item["ok"])
        self.assertEqual(item["severity"], "warning")
        self.assertIn("降级", item["detail"])

    def test_missing_optional_models_downgrade_only_that_capability(self):
        report = self._run(patches={"model_report": lambda: {
            "ok": False, "broken_required": 0, "missing_optional": 2,
            "states": {}, "download_plan": [
                {"name": "uvr_vocals", "url": "https://example.test/x.onnx",
                 "relative_path": "state/models/uvr.onnx",
                 "sha256": "a" * 64, "size_bytes": 1, "source": "官方"}]}})
        item = next(i for i in report["checks"] if i["name"] == "model_assets")
        self.assertFalse(item["ok"])
        self.assertEqual(item["severity"], "warning")
        self.assertIn("人声分离", item["detail"])
        self.assertIn("model_assets", item["fix_command"])

    def test_broken_required_model_is_error(self):
        report = self._run(patches={"model_report": lambda: {
            "ok": False, "broken_required": 1, "missing_optional": 0,
            "states": {}, "download_plan": []}})
        item = next(i for i in report["checks"] if i["name"] == "model_assets")
        self.assertEqual(item["severity"], "error")
        self.assertIn("必需", item["detail"])
        self.assertTrue(item["fix_command"])

    def test_missing_ocr_copy_is_warning_not_error(self):
        """真实结论：OCR 用 wheel 内置模型，副本缺失只影响哈希校验。"""
        report = self._run(patches={"model_report": lambda: {
            "ok": False, "broken_required": 0, "missing_optional": 3,
            "states": {}, "download_plan": [
                {"name": "ocr_det", "url": "", "relative_path": "state/models/ocr/det.onnx",
                 "sha256": "a" * 64, "size_bytes": 1, "source": "wheel"}]}})
        item = next(i for i in report["checks"] if i["name"] == "model_assets")
        self.assertEqual(item["severity"], "warning")
        self.assertIn("wheel", item["detail"])
        self.assertIn("画面文字", item["detail"])

    def test_missing_summary_key_is_warning(self):
        report = self._run(patches={})
        item = next(i for i in report["checks"] if i["name"] == "summary_config")
        self.assertEqual(item["severity"], "warning")

    def test_database_quick_check_is_reported(self):
        report = self._run(patches={"database_status":
                                    lambda path: {"ok": True, "detail": "schema v2"}})
        item = next(i for i in report["checks"] if i["name"] == "database")
        self.assertTrue(item["ok"])
        self.assertIn("schema v2", item["detail"])

    def test_fresh_database_is_not_an_error(self):
        """全新安装（库不存在或为空）不应报错，否则新手第一步就被劝退。"""
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = doctor.database_status(root / "none.db")
            self.assertTrue(missing["ok"])
            empty = root / "empty.db"
            sqlite3.connect(str(empty)).close()
            blank = doctor.database_status(empty)
            self.assertTrue(blank["ok"], blank["detail"])

            legacy = root / "legacy.db"
            conn = sqlite3.connect(str(legacy))
            conn.executescript("CREATE TABLE transcripts (id INTEGER);"
                               "INSERT INTO transcripts VALUES (1);")
            conn.commit()
            conn.close()
            stale = doctor.database_status(legacy)
            self.assertFalse(stale["ok"])
            self.assertIn("v1", stale["detail"])

    def test_broken_database_is_error(self):
        report = self._run(patches={"database_status":
                                    lambda path: {"ok": False,
                                                  "detail": "quick_check 失败"}})
        item = next(i for i in report["checks"] if i["name"] == "database")
        self.assertFalse(item["ok"])
        self.assertEqual(item["severity"], "error")

    def test_unwritable_directory_is_error(self):
        report = self._run(patches={"dirs_writable": lambda: (False, "state/jobs")})
        item = next(i for i in report["checks"] if i["name"] == "writable_dirs")
        self.assertFalse(item["ok"])
        self.assertIn("state/jobs", item["detail"])

    def test_overall_ok_ignores_warnings(self):
        # 构造一个「只剩 warning 级失败」的合成矩阵：核心项全部喂成 ok，
        # 只有 cuda/models 等非核心项失败（warning），断言整体仍 ok。
        report = self._run(which={"ffprobe": "C:/ff.exe",
                                  "douyin-ingest": "C:/fake/douyin-ingest.exe"},
                           patches={
            "chromium_installed": lambda: True,
            "installed_version": lambda name: "0.3.0",
            "detect_cuda": lambda: False,
            "module_available": lambda name: True,
            "model_report": lambda: {
                "ok": False, "broken_required": 0, "missing_optional": 1,
                "states": {}, "download_plan": []},
            "database_status": lambda path: {"ok": True, "detail": "schema v2"}})
        self.assertTrue(report["ok"])
        warnings = [i["name"] for i in report["checks"]
                    if not i["ok"] and i["severity"] == "warning"]
        self.assertTrue(warnings, "本用例至少要制造一个 warning 级失败才有意义")


class CliTests(unittest.TestCase):
    def test_json_is_utf8_even_on_gbk_console(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with mock.patch.object(doctor, "run_checks",
                               return_value={"ok": True, "checks": [
                                   {"name": "python", "ok": True,
                                    "detail": "当前 Python 3.12.10",
                                    "fix_command": "", "severity": "error"}]}), \
             redirect_stdout(buffer):
            code = doctor.main(["--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue().strip())
        self.assertEqual(payload["checks"][0]["detail"], "当前 Python 3.12.10")

    def test_human_output_reports_overall_state(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with mock.patch.object(doctor, "run_checks",
                               return_value={"ok": False, "checks": [
                                   {"name": "ffmpeg", "ok": False,
                                    "detail": "未找到 ffprobe",
                                    "fix_command": "winget install",
                                    "severity": "error"}]}), \
             redirect_stdout(buffer):
            code = doctor.main([])
        self.assertEqual(code, 1)
        self.assertIn("未就绪", buffer.getvalue())
        self.assertIn("winget install", buffer.getvalue())


class RealEnvironmentTests(unittest.TestCase):
    """真实环境自检：本机应能通过全部核心检查。"""

    def test_core_checks_pass_on_an_installed_machine(self):
        """体检是自检工具：在装好的机器上必须全绿。

        未安装完整环境（缺模型副本、未迁移数据库）时，允许这些项失败，
        但每一项都必须给出可执行的修复命令——否则新手会卡死在这里。
        """
        report = doctor.run_checks()
        installable = {"model_assets", "database", "summary_config"}
        failed = [item for item in report["checks"]
                  if not item["ok"] and item["severity"] == "error"]
        unexpected = [item["name"] for item in failed
                      if item["name"] not in installable]
        self.assertEqual(unexpected, [], f"意外失败的核心检查：{unexpected}")
        for item in failed:
            self.assertTrue(item["fix_command"],
                            f"{item['name']} 失败但没有修复命令：{item['detail']}")


# 装机态断言 = 纯环境事实：缺依赖的机器上不可能全绿，豁免名必须来自 conftest 登记表。
RealEnvironmentTests.test_core_checks_pass_on_an_installed_machine = requires_env(
    "installed-doctor",
    "缺 douyin-ingest CLI 或 rapidocr/onnxruntime，装机态自检不适用",
)(RealEnvironmentTests.test_core_checks_pass_on_an_installed_machine)


class DoctorContractTests(unittest.TestCase):
    """降级契约（P 级，恒定跑）：任何体检项失败都必须可读、有修复命令、不崩栈。

    与上面被 skip 的「装机全绿」配套——即便核心依赖缺失，doctor 也必须给出
    可执行的修复路径，而不是抛异常或留空。
    """

    def test_every_failing_check_is_readable_with_fix(self):
        report = doctor.run_checks()
        for item in report["checks"]:
            self.assertIsInstance(item["detail"], str)
            self.assertTrue(item["detail"], f"{item['name']} detail 为空")
            self.assertIn(item["severity"], ("error", "warning"))
            if not item["ok"]:
                self.assertTrue(
                    item["fix_command"],
                    f"{item['name']} 失败却没给可执行修复命令：{item['detail']}")


if __name__ == "__main__":
    unittest.main()
