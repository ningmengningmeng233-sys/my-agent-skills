# -*- coding: utf-8 -*-
"""conftest 编码出口自检：clean_env / run_py 是常规子进程调用的唯一收敛点，
run_py_narrow_console 是刻意验窄编码终端的出口（它必须**不**钉编码）。

回归背景（B1）：中文 Windows 下子进程默认吐 GBK，测试硬写 encoding="utf-8"
解码 → UnicodeDecodeError → pytest 捕获使 stdout=None → TypeError。
"""
import os
import sys

from conftest import clean_env, run_py, run_py_narrow_console


def test_clean_env_pins_python_encoding_flags_even_if_caller_overrides():
    env = clean_env(PYTHONIOENCODING="gbk", PYTHONUTF8="0",
                    PYTHONLEGACYWINDOWSSTDIO="1", MY_EXTRA_FLAG="keep-me")
    # 调用方传的 extra 不得覆盖三个 Python 编码 flag
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONLEGACYWINDOWSSTDIO"] == "0"
    # 其余 extra 与 os.environ 底都要保留
    assert env["MY_EXTRA_FLAG"] == "keep-me"
    assert env["PATH"] == os.environ["PATH"]


def test_run_py_never_returns_none_streams(tmp_path):
    # 子进程向 stdout/stderr 各写中文（GBK 可编码的常用汉字），验证永不为 None
    script = tmp_path / "cn_stdio.py"
    script.write_text(
        "import sys\n"
        "print('标准输出：完成验收')\n"
        "sys.stderr.write('标准错误：伏笔未回收，闸门拦截' + chr(10))\n",
        encoding="utf-8",
    )
    res = run_py([str(script)])
    assert isinstance(res.stdout, str) and isinstance(res.stderr, str)
    assert "标准输出：完成验收" in res.stdout
    assert "标准错误：伏笔未回收" in res.stderr
    assert res.returncode == 0


def test_run_py_empty_streams_are_empty_str_not_none(tmp_path):
    script = tmp_path / "silent.py"
    script.write_text("pass\n", encoding="utf-8")
    res = run_py([str(script)])
    assert res.stdout == "" and res.stderr == ""
    assert res.returncode == 0


def test_narrow_console_outlet_does_not_pin_the_encoding(tmp_path):
    """窄终端出口的本分就是不钉编码：调用方给 gbk，子进程就得真是 gbk。"""
    script = tmp_path / "report_encoding.py"
    script.write_text("import sys\nprint(sys.stdout.encoding)\n", encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="gbk", PYTHONUTF8="0")
    res = run_py_narrow_console([str(script)], env=env)
    assert res.stdout.decode("gbk", "replace").strip() == "gbk"
    # 同一份 env 交给 run_py 就会被钉回 utf-8 —— 两个出口的分工才是可验的
    pinned = run_py([str(script)], env={"PYTHONIOENCODING": "gbk", "PYTHONUTF8": "0"})
    assert pinned.stdout.strip() == "utf-8"


def test_narrow_console_outlet_never_returns_none_streams(tmp_path):
    script = tmp_path / "silent.py"
    script.write_text("pass\n", encoding="utf-8")
    res = run_py_narrow_console([str(script)], env=dict(os.environ))
    assert isinstance(res.stdout, bytes) and isinstance(res.stderr, bytes)
    assert res.stdout == b"" and res.stderr == b""
