"""日志隔离测试：测试绝不写真实 state/logs/transcript.log，也不动调用者的 handler。"""

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import logger as skill_logger
from scripts import paths
from tests import support


class LogIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(skill_logger.reset_logging)
        self.target = Path(self._tmp.name) / "nested" / "test.log"

    def test_injected_log_file_receives_output_and_redacts_secrets(self):
        log = skill_logger.configure_logging("secret-value-123456",
                                             log_file=self.target)
        log.warning("隔离日志消息")
        log.warning("泄漏尝试 secret-value-123456 结束")
        content = self.target.read_text(encoding="utf-8")
        self.assertIn("隔离日志消息", content)
        self.assertNotIn("secret-value-123456", content)
        self.assertIn(skill_logger._REDACTED, content)

    def test_injected_logging_never_touches_production_log(self):
        paths.ensure_runtime_dirs()
        production = paths.LOG_DIR / "transcript.log"
        before = production.stat().st_mtime_ns if production.exists() else None
        log = skill_logger.configure_logging(log_file=self.target)
        log.warning("本条不得进入生产日志")
        after = production.stat().st_mtime_ns if production.exists() else None
        self.assertEqual(before, after)
        self.assertNotIn("本条不得进入生产日志",
                         production.read_text(encoding="utf-8")
                         if production.exists() else "")

    def test_caller_handler_is_preserved(self):
        root = logging.getLogger()
        sentinel = logging.NullHandler()
        root.addHandler(sentinel)
        self.addCleanup(root.removeHandler, sentinel)
        skill_logger.configure_logging(log_file=self.target)
        self.assertIn(sentinel, root.handlers)

    def test_repeated_configuration_does_not_stack_handlers(self):
        skill_logger.configure_logging(log_file=self.target)
        first = len(logging.getLogger().handlers)
        skill_logger.configure_logging(log_file=self.target)
        skill_logger.configure_logging(log_file=self.target)
        self.assertEqual(len(logging.getLogger().handlers), first)

    def test_reset_logging_removes_owned_handlers_only(self):
        root = logging.getLogger()
        sentinel = logging.NullHandler()
        root.addHandler(sentinel)
        self.addCleanup(root.removeHandler, sentinel)
        skill_logger.configure_logging(log_file=self.target)
        skill_logger.reset_logging()
        self.assertIn(sentinel, root.handlers)
        self.assertTrue(all(not getattr(h, skill_logger._MARKER, False)
                            for h in root.handlers))

    def test_default_log_path_is_production_path_when_not_overridden(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(skill_logger.LOG_FILE_ENV, None)
            self.assertEqual(skill_logger.production_log_file(),
                             paths.LOG_DIR / "transcript.log")

    def test_env_override_redirects_default_log_path(self):
        # 测试与 CI 靠这个开关保证不写真实 state/logs/transcript.log
        outer = skill_logger.production_log_file()
        with support.isolate_logging(self._tmp.name) as target:
            self.assertEqual(skill_logger.production_log_file(), target)
            log = skill_logger.configure_logging()
            log.info("重定向探针")
            self.assertIn("重定向探针", target.read_text(encoding="utf-8"))
        self.assertEqual(skill_logger.production_log_file(), outer)


class Utf8StdioTests(unittest.TestCase):
    def test_ensure_utf8_stdio_is_idempotent_and_utf8(self):
        # 继承 B站版长处：Windows GBK 控制台下 emoji/中文 JSON 不得崩溃
        import sys

        skill_logger.ensure_utf8_stdio()
        skill_logger.ensure_utf8_stdio()
        for stream in (sys.stdout, sys.stderr):
            enc = (getattr(stream, "encoding", "") or "").lower()
            self.assertTrue(enc.startswith("utf-8"), f"{stream} encoding={enc}")


if __name__ == "__main__":
    unittest.main()
