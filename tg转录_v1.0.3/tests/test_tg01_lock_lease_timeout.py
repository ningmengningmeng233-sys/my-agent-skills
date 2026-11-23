# -*- coding: utf-8 -*-
"""TG-01 回归：tdl 子进程超时 + 会话锁租约，杜绝永久 LockBusy 死锁。

缺陷：
① state/media 的默认子进程 Runner 无 timeout，一旦 tdl 挂起，任务在独占
   会话锁内永久阻塞；
② acquire_lock 不抢占、不超时自动破锁，锁持有者异常终止后锁文件永久残留，
   同账号后续任务永久 LockBusy。

受控 RED：以下断言在修复前必须失败，修复后通过。全 mock/stub，不调真实
tdl/网络/GPU。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import media
import state


DEAD_PID = 2 ** 30  # 跨平台必定不存在的 pid（posix ESRCH / Windows 参数非法）


def _old_iso(hours=3):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _write_lock(root: Path, marker: str, *, acquired_at: str, pid: int,
                nonce: str = "stale-nonce", kind: str = "session") -> Path:
    prefix = state.LOCK_PREFIXES[kind]
    path = root / state.LOCK_SUBDIR / ("%s%s.lock" % (prefix, marker))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"marker": marker, "nonce": nonce,
                                "acquired_at": acquired_at, "process_pid": pid}),
                    encoding="utf-8")
    return path


def test_export_runner_timeout_is_network_error(tmp_path, monkeypatch):
    """导出子进程挂起超时 → ExportError(kind=network，可有限重试），且传了 timeout。"""
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(state.subprocess, "run", fake_run)
    with pytest.raises(state.ExportError) as excinfo:
        state._subprocess_runner(["fake-tdl", "chat", "export"], tmp_path / "seg.part")
    assert excinfo.value.kind == "network", excinfo.value.kind
    assert seen.get("timeout") == state.TDL_SUBPROCESS_TIMEOUT_SECONDS, seen


def test_download_runner_timeout_is_network_error(tmp_path, monkeypatch):
    """下载子进程挂起超时 → DownloadError(kind=network），且传了 timeout。"""
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    with pytest.raises(media.DownloadError) as excinfo:
        media._subprocess_runner(["fake-tdl", "dl"])
    assert excinfo.value.kind == "network", excinfo.value.kind
    assert seen.get("timeout") == state.TDL_SUBPROCESS_TIMEOUT_SECONDS, seen


def test_fresh_lock_still_busy(tmp_path):
    """新鲜锁（持有者存活、租约内）仍互斥：不误破活锁。"""
    root = tmp_path / "locks-root"
    token = state.acquire_lock(root, marker="m1", owner={"pid": os.getpid()})
    assert (Path(token["path"])).is_file()
    with pytest.raises(state.LockBusy):
        state.acquire_lock(root, marker="m1", owner={"pid": os.getpid()})
    state.release_lock(root, token)


def test_expired_lease_auto_breaks_even_with_live_pid(tmp_path):
    """租约过期 → 自动破锁恢复（即使 pid 看似存活），不永久阻塞。"""
    root = tmp_path / "locks-root"
    stale = _write_lock(root, "m2", acquired_at=_old_iso(3), pid=os.getpid())
    old_mtime = time.time() - 3 * 3600
    os.utime(stale, (old_mtime, old_mtime))

    token = state.acquire_lock(root, marker="m2", owner={"pid": os.getpid()})
    assert token.get("recovered_from", {}).get("reason") == "lease_expired"
    current = json.loads(Path(token["path"]).read_text(encoding="utf-8"))
    assert current["nonce"] == token["nonce"] != "stale-nonce"
    state.release_lock(root, token)
    assert not Path(token["path"]).exists()


def test_dead_pid_breaks_fresh_lease(tmp_path):
    """持有者进程已死 → 即使租约新鲜也允许破锁恢复。"""
    root = tmp_path / "locks-root"
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_lock(root, "m3", acquired_at=fresh, pid=DEAD_PID)

    token = state.acquire_lock(root, marker="m3", owner={"pid": os.getpid()})
    assert token.get("recovered_from", {}).get("reason") == "dead_pid"
    state.release_lock(root, token)
    # 恢复后再次加锁正常：无永久死锁
    token2 = state.acquire_lock(root, marker="m3", owner={"pid": os.getpid()})
    state.release_lock(root, token2)


def test_attempt_segment_threads_timeout(tmp_path, monkeypatch):
    """run_export 链路把 timeout 透传给默认 Runner（注入式 Runner 不受影响）。"""
    calls = []

    def stub(argv, temporary, timeout=None):
        calls.append(timeout)
        return 0

    monkeypatch.setattr(state, "_subprocess_runner", stub)
    root = tmp_path / "root"
    root.mkdir()
    outcome = state._attempt_segment(root, {}, {"segment_id": "s-1"},
                                     lambda d, s, t: ["fake-tdl"], None, 0,
                                     lambda s: None, subprocess_timeout=123)
    assert calls == [123], calls
    assert outcome == "failed"  # 桩未写临时文件 → 按既有口径判失败，非超时路径


def test_attempt_download_threads_timeout(tmp_path, monkeypatch):
    """run_download 链路把 timeout 透传给默认 Runner。"""
    calls = []

    def stub(argv, timeout=None):
        calls.append(timeout)
        return 0

    monkeypatch.setattr(media, "_subprocess_runner", stub)
    lente = media._attempt_download({"tdl": "fake", "namespace": "ns", "storage": "bolt"},
                                    tmp_path / "idx.json", tmp_path / "media",
                                    None, 0, lambda s: None, (),
                                    subprocess_timeout=123)
    assert calls == [123], calls
    assert lente["started"] and not lente["failed"]
