"""本仓测试的子进程范式工具（G10 编码范式一致性闸门要求的两件）。

`run_py` 是**唯一**允许的起 Python 子进程出口：子进程编码由 `clean_env` 钉死，
调用方既改不掉也不该改——否则「干净 shell 红、注入 utf-8 才绿」的 B1 类回归
又要靠人眼抓。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_PYTHON_ENCODING_FLAGS = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONLEGACYWINDOWSSTDIO": "0",
}


def clean_env(**extra):
    """以 os.environ 为底，强制钉死 Python 子进程编码 flag。

    先写三个 flag，merge 调用方 extra，再把这三个 key 钉一次——
    调用方即使显式传 PYTHONIOENCODING="gbk" 也覆盖不了。
    """
    env = dict(os.environ)
    env.update(_PYTHON_ENCODING_FLAGS)
    env.update(extra)
    env.update(_PYTHON_ENCODING_FLAGS)
    return env


@pytest.fixture
def run_py():
    """`_run_py` 的 fixture 出口：测试收一个 `run_py` 参数就能用。

    G10 认的是「conftest 里有 run_py 这件范式工具」，测试不必 import conftest。
    """
    return _run_py


def _run_py(args, cwd=None, **kw):
    """跑 Python 子进程的唯一出口：永不返回 None 流，永不受 GBK 控制台影响。

    内部禁止 text=True / encoding=：统一以 bytes 捕获再 decode 成 str。
    """
    env = clean_env(**(kw.pop("env", None) or {}))
    kw.pop("text", None)
    kw.pop("encoding", None)
    kw.pop("universal_newlines", None)
    res = subprocess.run([sys.executable, *args], cwd=cwd, env=env,
                         capture_output=True, **kw)
    res.stdout = (res.stdout or b"").decode("utf-8", "replace")
    res.stderr = (res.stderr or b"").decode("utf-8", "replace")
    return res
