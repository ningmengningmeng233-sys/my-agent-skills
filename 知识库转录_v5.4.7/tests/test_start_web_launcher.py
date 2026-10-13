# -*- coding: utf-8 -*-
"""T03: start_web.ps1 must locate its own skill root, not a hard-coded desk.

Covers: recipient layout with another user name, spaces and Chinese directory,
a foreign working directory, interpreter override, -Port pass-through and real
child exit propagation.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "start_web.ps1"

# The staged entrypoint reports what the launcher actually handed to python:
# argv[0] is the entrypoint path, argv[1:] are the forwarded arguments.
STUB = """import json, os, sys
print(json.dumps({"cwd": os.getcwd(), "argv0": sys.argv[0],
                  "rest": sys.argv[1:], "python": os.environ.get("STUB_PY", "real")},
                 ensure_ascii=False))
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""

PROBE = """import json, sys
print(json.dumps({"cwd": ".", "argv0": sys.argv[1] if len(sys.argv) > 1 else "",
                  "rest": sys.argv[2:], "python": "shim"}, ensure_ascii=False))
"""


def _decode(data: bytes) -> str:
    """PowerShell writes the OEM console codepage (cp936 here) for non-ASCII paths."""
    for enc in ("utf-8", "cp936"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _install(root: Path) -> tuple[Path, Path]:
    home = root / "用户乙"
    skill = home / "知识 目录" / "knowledge-index"
    (skill / "scripts").mkdir(parents=True)
    (skill / "start_web.ps1").write_text(LAUNCHER.read_text(encoding="utf-8"),
                                         encoding="utf-8")
    (skill / "scripts" / "launch_web.py").write_text(STUB, encoding="utf-8")
    third = root / "third place"
    third.mkdir()
    return skill, third


def _call(skill: Path, cwd: Path, extra: list[str], env: dict | None = None) -> dict:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(skill / "start_web.ps1")] + extra,
        cwd=str(cwd), capture_output=True,
        env=dict(os.environ, PYTHONUTF8="1", **(env or {})))
    stdout, stderr = _decode(proc.stdout), _decode(proc.stderr)
    line = next((l for l in stdout.splitlines() if l.strip().startswith("{")), "")
    assert line, (f"launcher printed no payload: exit={proc.returncode} "
                  f"stdout={stdout[:500]!r} stderr={stderr[:500]!r}")
    return {"payload": json.loads(line), "exit": proc.returncode}


needs_launcher = pytest.mark.skipif(not LAUNCHER.is_file(),
                                    reason="launcher not in this install")


@needs_launcher
def test_start_web_uses_its_own_script_directory(tmp_path):
    skill, third = _install(tmp_path)
    out = _call(skill, third, [])
    assert Path(out["payload"]["cwd"]) == skill


@needs_launcher
def test_start_web_passes_absolute_entrypoint(tmp_path):
    skill, third = _install(tmp_path)
    out = _call(skill, third, ["-Action", "status"])
    assert Path(out["payload"]["argv0"]) == skill / "scripts" / "launch_web.py"
    assert out["payload"]["rest"] == ["status"]


@needs_launcher
def test_start_web_forwards_port(tmp_path):
    skill, third = _install(tmp_path)
    out = _call(skill, third, ["-Action", "start", "-Port", "8137"])
    assert out["payload"]["rest"] == ["start", "--port", "8137"]


@needs_launcher
def test_start_web_propagates_child_exit_code(tmp_path):
    skill, third = _install(tmp_path)
    out = _call(skill, third, ["-Action", "status"], env={"STUB_EXIT": "7"})
    assert out["exit"] == 7


@needs_launcher
def test_start_web_interpreter_is_overridable(tmp_path):
    skill, third = _install(tmp_path)
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    shim = tmp_path / "named interpreter.cmd"
    shim.write_text(f'@echo off\r\npython "{probe}" %*\r\n', encoding="utf-8")
    out = _call(skill, third, ["-Action", "status", "-PythonExe", str(shim)])
    assert out["payload"]["python"] == "shim", "the named interpreter must be the one run"
    assert out["payload"]["rest"] == ["status"]


@needs_launcher
def test_start_web_has_no_personal_fallback(tmp_path):
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "Users" not in text, "launcher must not fall back to an author's home path"
