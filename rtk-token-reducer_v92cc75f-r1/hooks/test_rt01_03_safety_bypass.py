# -*- coding: utf-8 -*-
"""RT-01~03:钩子安全旁路与退出码保全(受控 RED→GREEN)。

RT-01 压缩异常/失败命令必须透明回退:非零退出码的命令不改写输出;
    压缩器抛异常时钩子安静放行,原始 stdout/stderr 与退出码完好透传。
RT-02 二进制流/非文本自动 bypass 跳过压缩。
RT-03 无响应的 rtk 不得挂住钩子:超时保护可配,超时后原样放行。

全部 mock/stub:伪造 rtk_pipe / subprocess.run / stdin,
不依赖真实 rtk 可执行文件、GPU 与网络。
"""
import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import rtk_compress  # noqa: E402


LINE = u'构建日志 第{0:04d}行 模块编译通过 状态正常 耗时12毫秒\n'


def _long_text(lines=120):
    return u''.join(LINE.format(i) for i in range(lines))


def _payload(command, tool_response):
    return json.dumps({'session_id': 'rt-bypass-test', 'hook_event_name': 'PostToolUse',
                       'tool_name': 'Bash', 'tool_input': {'command': command},
                       'tool_response': tool_response}, ensure_ascii=False)


def _run_hook_main(monkeypatch, tmp_path, payload_text):
    """进程内执行钩子 main:伪造 fd0 stdin 并捕获 stdout。返回 (rc, stdout)。"""
    monkeypatch.setenv('RTK_REDUCER_HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('RTK_REDUCER_OFF', raising=False)
    monkeypatch.setattr(common, 'MIN_CHARS', 700)
    real_open = io.open

    def fake_open(file, *args, **kwargs):
        if file == 0:
            return io.StringIO(payload_text)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io, 'open', fake_open)
    buf = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', buf)
    rc = rtk_compress.main()
    return rc, buf.getvalue()


def _hook_stdout_json(stdout_text):
    payload = json.loads(stdout_text)
    return (payload.get('hookSpecificOutput') or {}).get('updatedToolOutput') or ''


# ---------------- RT-01:失败命令旁路 + 压缩异常回退 ----------------

def test_nonzero_exit_bypasses_compression(monkeypatch, tmp_path):
    """非零退出码的命令:即使压缩器可用也不得改写输出。"""
    long_out = _long_text()
    assert len(long_out.encode('utf-8')) > 700
    calls = []
    monkeypatch.setattr(common, 'rtk_pipe',
                        lambda text, filt: calls.append((text, filt)) or u'SHORT')
    rc, out = _run_hook_main(monkeypatch, tmp_path, _payload(
        u'pytest tests/', {'stdout': long_out, 'stderr': u'', 'exit_code': 1}))
    assert rc == 0
    assert out == '', '失败命令必须原样放行,钩子不得输出 updatedToolOutput'
    assert calls == [], '失败命令不得调用压缩器'


def test_compressor_exception_falls_through_silently(monkeypatch, tmp_path):
    """压缩器抛异常:钩子不得崩、不得改写,原始输出与退出码透传。"""
    def boom(text, filt):
        raise RuntimeError('fake rtk exploded')
    monkeypatch.setattr(common, 'rtk_pipe', boom)
    rc, out = _run_hook_main(monkeypatch, tmp_path, _payload(
        u'pytest tests/', {'stdout': _long_text(), 'stderr': u'', 'exit_code': 0}))
    assert rc == 0
    assert out == '', '压缩异常必须安静放行,不得吞掉原始输出'


def test_success_rewrite_preserves_exit_code(monkeypatch, tmp_path):
    """成功命令被压缩时:退出码字段必须原样保留在改写后的 JSON 中。"""
    long_out = _long_text()
    monkeypatch.setattr(common, 'rtk_pipe', lambda text, filt: u'SHORT-COMPRESSED')
    rc, out = _run_hook_main(monkeypatch, tmp_path, _payload(
        u'pytest tests/', {'stdout': long_out, 'stderr': u'', 'exit_code': 0,
                           'interrupted': False}))
    assert rc == 0
    inner = json.loads(_hook_stdout_json(out))
    assert inner.get('exit_code') == 0, '改写不得吞掉退出码'
    assert inner.get('interrupted') is False
    assert len(inner.get('stdout', '')) < len(long_out)


@pytest.mark.parametrize('resp', [
    {'stdout': u'x', 'exit_code': 2},
    {'stdout': u'x', 'exitCode': 1},
    {'stdout': u'x', 'code': 3},
    {'stdout': u'x', 'status': u'failed'},
    {'stdout': u'x', 'interrupted': True},
    {'stdout': u'x', 'error': u'boom'},
])
def test_response_failed_detects_failure_signals(resp):
    assert common.response_failed(resp) is True


@pytest.mark.parametrize('resp', [
    {'stdout': u'x', 'exit_code': 0},
    {'stdout': u'x', 'stderr': u'', 'interrupted': False},
    {'stdout': u'x'},
    u'plain string response',
    None,
])
def test_response_failed_ignores_success(resp):
    assert common.response_failed(resp) is False


# ---------------- RT-02:二进制流 bypass ----------------

def test_binary_output_bypasses_compression(monkeypatch, tmp_path):
    """含 NUL 的二进制输出:不得送入压缩器,直接放行。"""
    body = _long_text() + u'\x00\x01\x02BIN\xff\xfe' * 40
    calls = []
    monkeypatch.setattr(common, 'rtk_pipe',
                        lambda text, filt: calls.append(text) or u'SHORT')
    rc, out = _run_hook_main(monkeypatch, tmp_path,
                             _payload(u'cat payload.bin', body))
    assert rc == 0
    assert out == '', '二进制输出必须 bypass,不得改写'
    assert calls == [], '二进制输出不得送入压缩器'


def test_is_binary_text_rules():
    assert common.is_binary_text(u'') is False
    assert common.is_binary_text(_long_text()) is False
    assert common.is_binary_text(u'\x1b[31m红色错误\x1b[0m 普通日志\n' * 40) is False, \
        'ANSI 颜色转义是合法文本,不得误判为二进制'
    assert common.is_binary_text(u'abc\x00def') is True
    assert common.is_binary_text(u'\x01\x02\x03\x04' * 30) is True


# ---------------- RT-03:超时保护 ----------------

def test_rtk_timeout_env_override(monkeypatch):
    monkeypatch.setenv('RTK_REDUCER_TIMEOUT', '2')
    assert common.rtk_timeout() == 2
    monkeypatch.setenv('RTK_REDUCER_TIMEOUT', 'oops')
    assert common.rtk_timeout() == common.RTK_TIMEOUT
    monkeypatch.setenv('RTK_REDUCER_TIMEOUT', '0')
    assert common.rtk_timeout() == common.RTK_TIMEOUT
    monkeypatch.delenv('RTK_REDUCER_TIMEOUT', raising=False)
    assert common.rtk_timeout() == common.RTK_TIMEOUT


def test_hanging_rtk_aborts_on_configured_timeout(monkeypatch, tmp_path):
    """无响应的 rtk:超时后返回 None(调用方原样放行),且耗时受配置约束。"""
    monkeypatch.setenv('RTK_REDUCER_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('RTK_REDUCER_TIMEOUT', '1')
    monkeypatch.setattr(common, 'rtk_binary', lambda: u'/fake/rtk')
    seen = {}

    def fake_run(cmd, timeout=None, **kwargs):
        seen['timeout'] = timeout
        time.sleep(timeout + 0.05)  # 模拟挂死:永远不返回
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(common.subprocess, 'run', fake_run)
    t0 = time.monotonic()
    assert common.rtk_pipe(_long_text(), 'log') is None
    elapsed = time.monotonic() - t0
    assert seen.get('timeout') == 1, '必须使用配置的超时,而不是写死的 15 秒'
    assert elapsed < 8, '挂死的压缩器不得拖住钩子,实耗 %.1fs' % elapsed


# ---------------- 进程边界回归 ----------------

def test_hook_process_passthrough_without_rtk(tmp_path):
    """进程级:PATH 无 rtk 时钩子 exit 0 且无 stdout(原始输出完整保留给上游)。"""
    import os
    hook = str(Path(common.__file__).resolve().parent / 'rtk_compress.py')
    env = dict(os.environ)
    env['RTK_REDUCER_HOME'] = str(tmp_path / 'home')
    env.pop('RTK_REDUCER_OFF', None)
    env['PATH'] = str(tmp_path)  # 空目录:确保找不到 rtk
    env['PYTHONIOENCODING'] = 'utf-8'
    p = subprocess.run([sys.executable, '-X', 'utf8', hook],
                       input=_payload(u'find . -name x',
                                      {'stdout': _long_text(), 'exit_code': 0}).encode('utf-8'),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=env, timeout=60, cwd=str(tmp_path))
    assert p.returncode == 0
    assert p.stdout.decode('utf-8', 'replace') == ''
