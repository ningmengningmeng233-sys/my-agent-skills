# -*- coding: utf-8 -*-
"""窄编码控制台（GBK）下跑 CLI：只许转义显示，不许把进程打成崩溃。

背景：中文 Windows 控制台默认 GBK，编不出 emoji 与 ✓/✗，`print()` 会抛 UnicodeEncodeError。
`dramacraft/__init__.py:harden_stdio()` 在包导入时把输出流的错误处理改成 backslashreplace，
是所有受支持入口（`python -m dramacraft.X`、`python run_batch.py …`）唯一的消毒点。

这一族用例盯的是**入口形态**而不是模块内容：v1.5.1 合并时曾把散落在 5 个模块里的
`sys.stdout.reconfigure` 收敛成一处中央消毒，收敛本身是对的，但当时**没有任何用例守住入口**，
所以「入口漏了消毒」只会以真跑崩溃的形式暴露。此处把口径钉成常驻测试。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from conftest import run_py_narrow_console  # noqa: E402
from dramacraft import harden_stdio  # noqa: E402

# 受支持的 CLI 入口：一律 `python -m dramacraft.<mod>`，参数用 --help（不碰文件、不烧额度）
CLI_MODULES = [
    "artifact_chain", "breaker", "continuity_lint", "doctor", "meeting_pack",
    "outline_lint", "plan_lint", "premise_lint", "self_healing", "shot_lint",
    "vocab_stats",
]


def _gbk_env():
    """伪造一个窄编码控制台：显式 GBK 且关掉 UTF-8 模式，绕过 tests/conftest 的编码钉死。"""
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "gbk", "PYTHONUTF8": "0",
                "PYTHONLEGACYWINDOWSSTDIO": "1"})
    return {k: v for k, v in env.items() if v is not None}


def _run(args):
    return run_py_narrow_console(args, cwd=ROOT, env=_gbk_env(), timeout=180)


def _decode(data):
    return data.decode("utf-8", "replace")


@pytest.mark.parametrize("mod", CLI_MODULES)
def test_module_cli_survives_gbk_console(mod):
    res = _run(["-m", f"dramacraft.{mod}", "--help"])
    err = _decode(res.stderr)
    assert "UnicodeEncodeError" not in err, f"-m dramacraft.{mod} 在 GBK 控制台崩了：{err[-400:]}"
    assert res.returncode == 0, _decode(res.stdout) + err


def test_run_batch_help_and_doctor_survive_gbk_console():
    """`run_batch.py --doctor` 的报告满屏 emoji（🎉/⚠️/✓/❌），是最容易炸的入口。"""
    for args in (["--help"], ["--doctor"]):
        res = _run(["run_batch.py"] + args)
        err = _decode(res.stderr)
        assert "UnicodeEncodeError" not in err, f"run_batch {' '.join(args)} 崩了：{err[-400:]}"
        # --doctor 环境不全时退出码本就为 1，这里只要求「打印完整、没有异常栈」
        out = _decode(res.stdout)
        assert out.strip(), f"run_batch {' '.join(args)} 什么都没打出来"
        assert "Traceback" not in err, err[-400:]


def test_harden_stdio_skips_streams_without_reconfigure():
    """消毒不得因为流被测试框架接管（没有 reconfigure）就抛异常。"""

    class Plain:
        pass

    class Boom:
        def reconfigure(self, **kw):
            raise ValueError("stream is not reconfigurable")

    assert harden_stdio([Plain(), Boom(), Boom()]) == []


def test_harden_stdio_reconfigures_error_handler_only():
    class Stream:
        def __init__(self):
            self.kwargs = None

        def reconfigure(self, **kw):
            self.kwargs = kw

    s = Stream()
    assert harden_stdio([s]) == [s]
    assert s.kwargs == {"errors": "backslashreplace"}, "只改错误处理，不改编码（UTF-8 终端要原样输出）"
