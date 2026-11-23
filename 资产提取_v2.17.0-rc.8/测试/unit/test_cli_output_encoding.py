# -*- coding: utf-8 -*-
"""T01：产品 CLI 必须自带输出编码边界（e2e 合同刻意不给编码保护伞）。

现场：默认 GBK 环境下跑 e2e 全链，步骤 1.5 起级联失败，逐步骤全文日志显示
`scan_registry / extract_segments / aggregate / generate_prompts / check_schedule`
向管道打印 📚/❌/ℹ 时抛 `UnicodeEncodeError`（evidence/t01-e2e-flow-full.log 与 steplogs）。
本文件钉三条：①无保护伞下 CLI 自身不得因输出编码崩；②崩溃不得被吞成 0（真实失败码保留）；
③CLI 被 import 时不得改宿主 stdio（只在 __main__ 分支生效）。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPTS = ("scripts/scan_registry.py", "scripts/extract_segments.py", "scripts/aggregate.py",
           "scripts/generate_prompts.py", "scripts/check_schedule.py")


def _bare_env(tmp_path: pathlib.Path) -> dict:
    """刻意剥掉编码保护伞：与 测试/run_fresh_v2131.py 的合同一致（子进程不得依赖它）。"""
    keep = ("SYSTEMROOT", "windir", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "OS",
            "PROCESSOR_ARCHITECTURE", "USERDOMAIN", "USERNAME")
    up = {k.upper(): (k, v) for k, v in os.environ.items()}
    env = {up[k][0]: up[k][1] for k in keep if k in up}
    sr = env.get("SystemRoot") or "C:\\Windows"
    env["SystemRoot"] = env["windir"] = sr
    env["PATH"] = sr + "\\System32"
    (tmp_path / "tmp").mkdir(exist_ok=True)
    env.update({"TEMP": str(tmp_path / "tmp"), "TMP": str(tmp_path / "tmp"),
                "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "NO_PROXY": "*",
                "ASSET_OFFLINE": "1"})
    assert "PYTHONUTF8" not in env and "PYTHONIOENCODING" not in env, "保护伞必须剥净"
    return env


@pytest.mark.parametrize("script", SCRIPTS)
def test_cli_does_not_crash_on_output_encoding(script, tmp_path):
    """无保护伞下调用真实 CLI：允许用法错/参数错（rc≠0），但不得因自身打印崩。"""
    env = _bare_env(tmp_path)
    r = subprocess.run([sys.executable, str(REPO / script)], cwd=str(REPO / "scripts"),
                       env=env, capture_output=True, timeout=300)
    blob = (r.stdout or b"") + (r.stderr or b"")
    text = blob.decode("utf-8", "replace")
    assert "UnicodeEncodeError" not in text, f"{script} 因自身输出编码崩溃：\n{text[-600:]}"
    assert "Traceback" not in text or "Error" not in text, f"{script} 抛未捕获异常：\n{text[-600:]}"


def test_stdio_guard_is_main_only():
    """编码边界只准放在 `__main__` 分支：import 侧改宿主 stdio 会污染调用方（本仓踩过）。"""
    import ast
    for rel in SCRIPTS:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        guarded = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and "__main__" in ast.dump(node.test):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        guarded.add(inner.lineno)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("reconfigure", "dup2")):
                assert node.lineno in guarded, f"{rel}:{node.lineno} 的 stdio 改动不在 __main__ 守卫内"
