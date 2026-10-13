"""测试公共辅助：让自动测试永不写真实 state/logs/transcript.log。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from scripts import logger as skill_logger


def isolated_log_target(root: Path) -> Path:
    return Path(root) / "state" / "logs" / "transcript.log"


@contextmanager
def isolate_logging(root: Path):
    """把生产日志路径整体重定向到临时目录。

    覆盖任何经 CLI 入口调用 `configure_logging()` 的代码路径，
    即使该入口没有显式注入 log_file。
    """
    target = isolated_log_target(root)
    with mock.patch.dict(os.environ,
                         {skill_logger.LOG_FILE_ENV: str(target)}):
        try:
            yield target
        finally:
            skill_logger.reset_logging()


def install_session_isolation(root: Path, test=None) -> Path:
    """在测试进程内永久重定向日志路径（供 tests/__init__.py 与各用例使用）。"""
    target = isolated_log_target(root)
    os.environ[skill_logger.LOG_FILE_ENV] = str(target)
    if test is not None:
        test.addCleanup(skill_logger.reset_logging)
    return target
