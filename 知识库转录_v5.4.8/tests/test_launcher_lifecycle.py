"""启动器生命周期：无PID/正确PID/他技能同名/失效PID/端口占用/重复start/stop/restart/日志轮转。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import launch_web


def test_no_pid_status_reports_not_running(capsys):
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="launcher-"))
    monkey = launch_web.PID_FILE
    try:
        launch_web.PID_FILE = tmp / ".web.pid"
        rc = launch_web.cmd_stop()
        assert rc == 0
    finally:
        launch_web.PID_FILE = monkey


def test_command_belongs_to_skill():
    skill = str(launch_web.SKILL_DIR.resolve())
    good = f'C:\\Python\\python.exe "{skill}\\scripts\\start_web.py" --lib X --port 5888'
    assert launch_web._command_belongs_to_skill(good) is True
    other = 'C:\\Python\\python.exe "D:\\other\\skills\\knowledge-index-v5\\scripts\\start_web.py" --port 5888'
    assert launch_web._command_belongs_to_skill(other) is False
    bare = 'python.exe start_web.py --port 5888'
    assert launch_web._command_belongs_to_skill(bare) is False
    assert launch_web._command_belongs_to_skill('') is False


def test_dead_pid_refuses_stop(tmp_path, monkeypatch):
    pid_file = tmp_path / ".web.pid"
    pid_file.write_text(json.dumps({"pid": 999999, "port": 5888}), encoding="utf-8")
    monkeypatch.setattr(launch_web, "PID_FILE", pid_file)
    rc = launch_web.cmd_stop()
    assert rc == 1
    assert pid_file.exists()


def test_log_rotation(tmp_path, monkeypatch):
    log = tmp_path / ".web.log"
    log.write_bytes(b"x" * (launch_web.LOG_MAX_BYTES + 10))
    (tmp_path / ".web.log.1").write_bytes(b"old1")
    (tmp_path / ".web.log.2").write_bytes(b"old2")
    monkeypatch.setattr(launch_web, "LOG_FILE", log)
    launch_web._rotate_logs()
    assert not log.exists() or log.stat().st_size < launch_web.LOG_MAX_BYTES
    assert (tmp_path / ".web.log.1").exists()
    assert not (tmp_path / ".web.log.4").exists()


def test_stop_verifies_before_kill(tmp_path, monkeypatch):
    pid_file = tmp_path / ".web.pid"
    pid_file.write_text(json.dumps({"pid": 12345, "port": 5888}), encoding="utf-8")
    monkeypatch.setattr(launch_web, "PID_FILE", pid_file)
    monkeypatch.setattr(launch_web, "_is_our_web", lambda pid: False)
    called = []
    monkeypatch.setattr(launch_web.os, "kill", lambda *a, **k: called.append(a))
    rc = launch_web.cmd_stop()
    assert rc == 1
    assert called == []
    assert pid_file.exists()


def test_app_version_not_index_version():
    import config as v5config
    assert v5config.APP_VERSION == "5.4.7"
    assert v5config.INDEX_VERSION == "5.4.0"
    assert v5config.APP_VERSION != v5config.INDEX_VERSION


def _stop_env(tmp_path, monkeypatch, *, alive_after, kill_rc=0):
    pid_file = tmp_path / ".web.pid"
    pid_file.write_text(json.dumps({"pid": 4242, "port": 5900}), encoding="utf-8")
    monkeypatch.setattr(launch_web, "PID_FILE", pid_file)
    calls = []

    class FakeProc:
        returncode = kill_rc
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return FakeProc()

    monkeypatch.setattr(launch_web.subprocess, "run", fake_run)
    # 杀之前必须认得「这是我们起的」（否则走拒杀分支），杀之后再按 alive_after 决定还活不活
    answers = iter([True, alive_after])
    monkeypatch.setattr(launch_web, "_is_our_web", lambda pid: next(answers))
    monkeypatch.setattr(launch_web.os, "name", "nt")
    monkeypatch.setattr(launch_web.time, "sleep", lambda *_a: None)
    fake = FakeProc()
    fake.alive = alive_after
    return pid_file, calls, fake


def test_stop_uses_well_formed_taskkill_flags(tmp_path, monkeypatch):
    """现场缺陷：源码里写的是 "//F" "//PID"，taskkill 直接报「无效参数」。

    那是 Git Bash 防路径转换的写法被带进了源码——在正常 Windows 调用里它根本不执行删除，
    于是 stop 打印「已停止」却把服务留在原地，还顺手删掉 PID 文件让人无从管理。
    """
    pid_file, calls, _ = _stop_env(tmp_path, monkeypatch, alive_after=False)
    rc = launch_web.cmd_stop()
    assert rc == 0, rc
    assert calls, "根本没调用 taskkill"
    argv = calls[-1]
    assert argv[0] == "taskkill"
    assert "/F" in argv and "/PID" in argv, argv
    assert not any(a.startswith("//") for a in argv), f"斜杠被改写过的痕迹：{argv}"


def test_stop_reports_failure_when_process_is_still_alive(tmp_path, monkeypatch):
    """杀不掉就必须失败并保留 PID 文件，不能打印「已停止」放行。"""
    pid_file, _, _ = _stop_env(tmp_path, monkeypatch, alive_after=True)
    rc = launch_web.cmd_stop()
    assert rc == 1, "服务还在却被判为停止成功"
    assert pid_file.exists(), "失败时必须保留 PID 文件供人工接管"
