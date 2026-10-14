#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AE-10（TICKET-45）：视觉事件 WAL 纯追加流式写入 + 有界轮转。

回归门：
- append_events 不得全量调用 read_events（每次提交全量读解析是 O(n²) 瓶颈）。
- 幂等语义保持：同一 event_id 只落盘一次。
- 活动日志超过阈值时快照固化并 gzip 压缩旧段；read_events 仍能读全历史。
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.engine.visual_events as ve  # noqa: E402
from scripts.engine.visual_events import (  # noqa: E402
    IMAGE_STORED,
    VisualEvent,
    append_events,
    event_log_path,
    read_events,
)

TASK_ID = "CH_林姑娘-正脸-v01"


def _event(seq, task=TASK_ID):
    return VisualEvent.create(IMAGE_STORED, task, payload={"round": 1, "seq": seq})


def _seed_history(lib: Path, count: int) -> None:
    """直接落盘 N 行历史（不经过 append，避免污染调用计数）。"""
    path = event_log_path(lib)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for i in range(count):
            handle.write(_event(i).to_json() + "\n")


def test_append_never_calls_read_events(tmp_path, monkeypatch):
    """追加 1 条事件不得触发全量 read_events（O(n²) 回归门）。"""
    lib = tmp_path / "lib"
    _seed_history(lib, 200)
    calls: list = []
    original = ve.read_events

    def _spy(bound_lib):
        calls.append(bound_lib)
        return original(bound_lib)

    monkeypatch.setattr(ve, "read_events", _spy)
    assert append_events(lib, (_event(1000),)) == 1
    assert calls == [], f"append 追加时调用了全量 read_events：{len(calls)} 次"
    # 幂等：重复追加同一事件返回 0，且同样不得全量读
    assert append_events(lib, (_event(1000),)) == 0
    assert calls == []
    assert len(original(lib)) == 201


def test_append_idempotent_within_batch(tmp_path):
    """同批重复 event_id 只落盘一次（幂等语义保持）。"""
    lib = tmp_path / "lib"
    event = _event(7)
    assert append_events(lib, (event, event)) == 1
    assert append_events(lib, (event,)) == 0
    assert len(read_events(lib)) == 1


def test_rotation_bounds_active_log_and_preserves_history(tmp_path, monkeypatch):
    """超阈值轮转：活动日志有界 + 旧段 gzip 归档 + 历史可读全。"""
    monkeypatch.setattr(ve, "EVENT_LOG_ROTATE_BYTES", 1024, raising=False)
    lib = tmp_path / "lib"
    total = 60
    for i in range(total):
        append_events(lib, (_event(i, task=f"T-{i:03d}"),))
    archives = sorted(event_log_path(lib).parent.glob("视觉事件轮转-*.jsonl.gz"))
    assert archives, "超过轮转阈值后应产生 gzip 压缩归档"
    active_size = event_log_path(lib).stat().st_size
    assert active_size <= 2048, f"活动日志未有界：{active_size} 字节"
    events = read_events(lib)
    assert len(events) == total, f"轮转后历史丢失：读到 {len(events)} / {total}"
    assert len({e.event_id for e in events}) == total
