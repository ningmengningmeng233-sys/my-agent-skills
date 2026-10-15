# -*- coding: utf-8 -*-
"""conftest 编码出口自检：clean_env / run_py 是全部 Python 子进程调用的唯一收敛点。

回归背景（问题清单 B1）：中文 Windows 下子进程默认吐 GBK，测试硬写
encoding="utf-8" 解码 → 中文断言拿到乱码或直接抛错。
"""
import os

from conftest import clean_env, run_py


def test_clean_env_pins_python_encoding_flags_even_if_caller_overrides():
    env = clean_env(PYTHONIOENCODING="gbk", PYTHONUTF8="0",
                    PYTHONLEGACYWINDOWSSTDIO="1", MY_EXTRA_FLAG="keep-me")
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONLEGACYWINDOWSSTDIO"] == "0"
    assert env["MY_EXTRA_FLAG"] == "keep-me"
    assert env["PATH"] == os.environ["PATH"]


def test_run_py_never_returns_none_streams(tmp_path):
    script = tmp_path / "cn_stdio.py"
    script.write_text(
        "import sys\n"
        "print('标准输出：分镜工单已产出')\n"
        "sys.stderr.write('标准错误：素材缺失，合成拒绝' + chr(10))\n",
        encoding="utf-8",
    )
    res = run_py([str(script)])
    assert isinstance(res.stdout, str) and isinstance(res.stderr, str)
    assert "标准输出：分镜工单已产出" in res.stdout
    assert "标准错误：素材缺失" in res.stderr
    assert res.returncode == 0


def test_run_py_empty_streams_are_empty_str_not_none(tmp_path):
    script = tmp_path / "quiet.py"
    script.write_text("pass\n", encoding="utf-8")
    res = run_py([str(script)])
    assert res.stdout == "" and res.stderr == ""
    assert res.returncode == 0
