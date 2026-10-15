"""测试包初始化：把生产日志路径整体重定向到临时目录。

`python -m unittest discover -s tests -t .` 会导入本模块，`-s tests`（无 `-t`）不会；
因此每个会经 CLI 入口写日志的用例还必须显式使用 tests/support.isolate_logging，
见 tests/test_logging.py 的隔离断言。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests import support

SESSION_LOG_ROOT = Path(tempfile.mkdtemp(prefix="douyin-transcript-tests-"))
support.install_session_isolation(SESSION_LOG_ROOT)
