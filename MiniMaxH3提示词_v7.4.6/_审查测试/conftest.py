"""审计目录共享夹具：Python 子进程编码出口（问题清单 B1 范式）。

`run_all.py` 以 `python <suite>` 方式跑每个套件，pytest 也会把本目录放进
sys.path，因此两种入口都能 `from conftest import run_py`。
"""
import os
import subprocess
import sys

_PYTHON_ENCODING_FLAGS = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONLEGACYWINDOWSSTDIO": "0",
}


def clean_env(**extra):
    """以 os.environ 为底，强制钉死 Python 子进程编码 flag（merge 后再钉一次）。"""
    env = dict(os.environ)
    env.update(_PYTHON_ENCODING_FLAGS)
    env.update(extra)
    env.update(_PYTHON_ENCODING_FLAGS)
    return env


def run_py(args, cwd=None, **kw):
    """跑 Python 子进程的唯一出口：永不返回 None 流，永不受 GBK 控制台影响。"""
    env = clean_env(**(kw.pop("env", None) or {}))
    kw.pop("text", None)
    kw.pop("encoding", None)
    kw.pop("universal_newlines", None)
    res = subprocess.run([sys.executable, *args], cwd=cwd, env=env,
                         capture_output=True, **kw)
    res.stdout = (res.stdout or b"").decode("utf-8", "replace")
    res.stderr = (res.stderr or b"").decode("utf-8", "replace")
    return res
