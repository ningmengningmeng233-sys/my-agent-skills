# -*- coding: utf-8 -*-
"""S05 定因后的保护回归：导入产品脚本不得改写宿主进程的 stdout 编码。

现象：整套跑完 rc=0 却**没有最终计数行**（进度行在、汇总行丢），accept 轮还同时冒出线程内
UnicodeDecodeError。根因是 scripts/*.py 在**模块导入期**执行 `sys.stdout.reconfigure(...)`；
测试导入这些模块时，把 pytest 的捕获流也一起改了，退出前汇总没落盘。
CLI 需要 UTF-8 是事实，但那是入口的事，不是库导入的副作用。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ["build_novel_index", "build_segment_index", "check_line_refs", "delete_novel",
           "migrate_library", "orchestrate", "rename_novel", "semantic_index"]

CHILD = (
    "import sys, pathlib\n"
    "sys.path.insert(0, r'{scripts}')\n"
    "before = sys.stdout.encoding\n"
    "import {mod}\n"
    "sys.stdout.write('RESULT:' + str(before) + '|' + str(sys.stdout.encoding) + '\\n')\n"
)


@pytest.mark.parametrize("mod", SCRIPTS)
def test_importing_module_does_not_take_over_host_stdout(mod, tmp_path):
    code = CHILD.format(scripts=str(ROOT / "scripts").replace("\\", "\\\\"), mod=mod)
    p = subprocess.run([sys.executable, "-X", "utf8=0", "-c", code], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={"PYTHONIOENCODING": "gbk", "SYSTEMROOT": str(Path(sys.executable).anchor or "."),
                            "PATH": ""}, timeout=120)
    line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT:")), None)
    assert line, (p.stdout[-300:], p.stderr[-300:])
    before, after = line[len("RESULT:"):].split("|")
    assert before == after, "导入 %s 改掉了宿主 stdout 编码（before=%s after=%s）" % (mod, before, after)


def test_suite_run_still_prints_final_count_line():
    """行为级复验：跑一个用得到这些脚本的用例，pytest 必须给出最终计数行。"""
    # 本仓 pyproject 的 addopts 已带 -q；这里再传 -q 会把 verbosity 压成 -2，汇总行必然消失（读数假象）
    p = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                        "tests/test_split.py::test_split_by_chars_when_no_chapters"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    text = p.stdout + p.stderr
    assert p.returncode == 0, text[-500:]
    assert ("passed" in text and " in " in text), "缺最终计数行＝汇总被吞，测试等于没跑完: %r" % text[-200:]
