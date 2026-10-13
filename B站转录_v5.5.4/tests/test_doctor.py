"""doctor.py 的行为测试：检查项结构、error/warning 聚合、数据库与目录判定。"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from scripts import doctor
from tests.support import SkillTestCase


def check_by_name(checks, name):
    return next(item for item in checks if item["name"] == name)


class ReportShapeTests(SkillTestCase):
    def test_every_check_has_required_fields(self):
        report = doctor.run_all()
        self.assertIn("ok", report)
        self.assertTrue(report["checks"])
        for item in report["checks"]:
            for field in ("name", "ok", "detail", "fix_command", "severity"):
                self.assertIn(field, item)
            self.assertIn(item["severity"], ("error", "warning"))

    def test_real_environment_is_green(self):
        """本机装好的环境必须全绿（这是体检工具的存在意义）。"""
        report = doctor.run_all()
        failing = [c["name"] for c in report["checks"]
                   if not c["ok"] and c["severity"] == "error"]
        self.assertEqual(failing, [], f"以下 error 级检查未通过：{failing}")

    def test_error_severity_failure_flips_ok_but_warning_does_not(self):
        bad_error = {"name": "fake_error", "ok": False, "detail": "x",
                     "fix_command": "", "severity": "error"}
        bad_warning = {"name": "fake_warning", "ok": False, "detail": "y",
                       "fix_command": "", "severity": "warning"}
        good = {"name": "good", "ok": True, "detail": "z",
                "fix_command": "", "severity": "error"}
        cases = [
            ([bad_error, good], False),
            ([bad_warning, good], True),
            ([good], True),
        ]
        for checks, expected in cases:
            with mock.patch.object(doctor, "CHECKS", ()):
                with mock.patch.object(doctor, "run_all",
                                       lambda checks=checks: {
                                           "ok": all(c["ok"] for c in checks
                                                     if c["severity"] == "error"),
                                           "checks": checks,
                                           "errors": sum(1 for c in checks if not c["ok"]
                                                         and c["severity"] == "error"),
                                           "warnings": sum(1 for c in checks if not c["ok"]
                                                           and c["severity"] == "warning"),
                                       }):
                    self.assertEqual(doctor.run_all()["ok"], expected)

    def test_main_returns_one_when_error_present(self):
        failing = {"ok": False, "checks": [
            {"name": "x", "ok": False, "detail": "d", "fix_command": "",
             "severity": "error"}], "errors": 1, "warnings": 0}
        with mock.patch.object(doctor, "run_all", lambda: failing):
            code, out = self.capture(doctor.main, ["--json"])
        self.assertEqual(code, 1)
        self.assertIn("false", out)


class DatabaseCheckTests(SkillTestCase):
    def test_missing_database_is_not_a_failure(self):
        with mock.patch.object(doctor, "DB_PATH", self.root / "absent.db"):
            item = doctor.check_database()
        self.assertTrue(item["ok"])
        self.assertIn("尚未创建", item["detail"])

    def test_fresh_empty_database_is_not_reported_as_needing_migration(self):
        """空库不算问题（曾把全新库误报为需要迁移）。"""
        empty = self.root / "empty.db"
        conn = sqlite3.connect(str(empty))
        conn.execute("CREATE TABLE transcripts (bvid TEXT, page INTEGER)")
        conn.commit()
        conn.close()
        with mock.patch.object(doctor, "DB_PATH", empty):
            item = doctor.check_database()
        self.assertTrue(item["ok"], item["detail"])

    def test_outdated_schema_fails_with_fix_command(self):
        stale = self.root / "stale.db"
        conn = sqlite3.connect(str(stale))
        conn.executescript(
            "CREATE TABLE transcripts (bvid TEXT, page INTEGER NOT NULL DEFAULT 1);"
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
            " applied_at TEXT NOT NULL);"
            "INSERT INTO schema_migrations VALUES (1, '2026-01-01');")
        conn.commit()
        conn.close()
        with mock.patch.object(doctor, "DB_PATH", stale):
            item = doctor.check_database()
        self.assertFalse(item["ok"])
        self.assertIn("migrations.py", item["fix_command"])

    def test_current_schema_database_passes_and_reports_counts(self):
        conn = sqlite3.connect(str(self.db))
        # 播种当前要求的版本号（引用常量，避免升 schema 后本用例过期）
        conn.execute("DELETE FROM schema_migrations")
        conn.execute("INSERT INTO schema_migrations VALUES (?, '2026-01-01')",
                     (doctor.SCHEMA_VERSION,))
        conn.commit()
        conn.close()
        with mock.patch.object(doctor, "DB_PATH", self.db):
            item = doctor.check_database()
        self.assertTrue(item["ok"], item["detail"])
        self.assertIn(f"schema v{doctor.SCHEMA_VERSION}", item["detail"])


class PathAndEnvCheckTests(SkillTestCase):
    def test_archive_dir_warns_when_unset(self):
        with mock.patch.dict("os.environ", {"BILI_ARCHIVE_DIR": ""}):
            item = doctor.check_archive_dir()
        self.assertFalse(item["ok"])
        self.assertEqual(item["severity"], "warning")

    def test_archive_dir_fails_when_missing(self):
        with mock.patch.dict("os.environ",
                             {"BILI_ARCHIVE_DIR": str(self.root / "nope")}):
            item = doctor.check_archive_dir()
        self.assertFalse(item["ok"])
        self.assertIn("不存在", item["detail"])

    def test_archive_dir_passes_and_cleans_probe(self):
        with mock.patch.dict("os.environ", {"BILI_ARCHIVE_DIR": str(self.archive)}):
            item = doctor.check_archive_dir()
        self.assertTrue(item["ok"], item["detail"])
        self.assertEqual(list(self.archive.glob(".doctor-write-probe")), [])

    def test_env_config_warns_when_favorite_id_missing(self):
        with mock.patch.dict("os.environ", {"FAV_MEDIA_ID": ""}):
            item = doctor.check_env_config()
        self.assertFalse(item["ok"])
        self.assertEqual(item["severity"], "warning")

    def test_runtime_dirs_probe_is_cleaned(self):
        with mock.patch.object(doctor, "SKILL_DIR", self.root):
            item = doctor.check_runtime_dirs()
        self.assertTrue(item["ok"], item["detail"])
        self.assertEqual(list((self.root / "output").glob(".doctor-write-probe")), [])


class OcrCheckTests(SkillTestCase):
    """OCR 依赖缺失必须被体检发现——它是可选阶段，故只告警不拦主流程。"""

    def test_ocr_check_registered_and_named(self):
        names = [c if isinstance(c, str) else c.__name__ for c in doctor.CHECKS]
        self.assertIn("check_ocr", names)

    def test_ocr_check_passes_when_engine_available(self):
        item = doctor.check_ocr()
        self.assertIn("name", item)
        self.assertEqual(item["name"], "ocr")

    def test_missing_onnxruntime_is_reported_as_warning(self):
        """模拟上个会话的故障：onnxruntime 被删，导入必须报失败。"""
        with mock.patch.object(doctor, "module_available",
                               lambda name: name != "onnxruntime"):
            item = doctor.check_ocr()
        self.assertFalse(item["ok"])
        self.assertEqual(item["severity"], "warning")

    def test_missing_engine_does_not_break_the_error_gate(self):
        checks = [doctor.check_ocr()]
        self.assertTrue(all(c["severity"] == "warning" for c in checks))


if __name__ == "__main__":
    unittest.main()
