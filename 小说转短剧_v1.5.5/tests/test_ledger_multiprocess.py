# -*- coding: utf-8 -*-
"""T4 Review Focus #4：账本 append-only 底线（**跨进程**）。

背景：`Ledger` 的进程内写锁是 `threading.Lock`——跨进程无效；工作流里每道门是独立 python
进程，跨进程互斥由 `_LockedAppend` 的文件锁承担。本测验两件事：① append-only 底线不破
（四个进程各写 100 行，最终 400 行一条不少、每行都能 json.loads、每进程恰 100 行）；
② 文件锁被别的进程占住时，写方**等到**再写而不是丢账。
"""
import json
import multiprocessing as mp
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dramacraft.ledger import Ledger, load_events  # noqa: E402


def _writer(path, tag, n=100):
    ledger = Ledger(path)
    for i in range(n):
        ledger.emit("probe", tag=tag, i=i)


def test_four_processes_append_four_hundred_intact_lines(tmp_path):
    path = os.path.join(str(tmp_path), "state", "ledger.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(path, f"p{k}")) for k in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    assert [p.exitcode for p in procs] == [0, 0, 0, 0], "子进程写账必须整批活着回来"

    raw = open(path, encoding="utf-8").read().splitlines()
    lines = [ln for ln in raw if ln.strip()]
    assert len(lines) == 400, "一行都不能丢（丢行=账本不可信）"
    for idx, line in enumerate(lines, start=1):
        json.loads(line)  # 半截行/交错行会在这里炸

    events = load_events(path)
    assert len(events) == 400
    for k in range(4):
        got = [e for e in events if e.get("tag") == f"p{k}"]
        assert len(got) == 100, f"p{k} 的行数不对"
    tags = sorted({e.get("tag") for e in events})
    assert tags == ["p0", "p1", "p2", "p3"]
    assert all(e.get("event") == "probe" for e in events), "事件类型不得被并发写花"


def _hold_lock(path, hold_seconds, ready_q):
    """子进程持锁一段时间（模拟另一个进程正在写账）。"""
    from dramacraft.ledger import _LockedAppend
    with _LockedAppend(path) as handle:
        assert handle.locked, "夹具自己都没拿到锁，测了个假的"
        ready_q.put(bool(handle.locked))
        time.sleep(hold_seconds)


@pytest.mark.parametrize("hold_seconds", [0.3, 0.5])
def test_emit_waits_for_a_lock_held_by_another_process(tmp_path, capsys, hold_seconds):
    """另一个进程持锁零点几秒时，写方必须**等到**锁再写，而不是丢行。

    真跑实测（2026-09-23 v1.5.1 验收）：满负载下 4 进程 × 100 行的压力用例反复丢行——
    非阻塞锁只退避 3×50ms，抢不到就打印「台账被占用」直接 return，那一行就没了。
    账本是伏笔核销与预算熔断的真值源，丢行=账本不可信，必须等到（有上限，不许挂死）。
    两个持锁时长都要守：0.3 秒是候选侧口径，0.5 秒是分发侧回归用例（提交 139ff71）的口径，
    合并时若只留短的，预算被偷偷调小就不会红。
    """
    ctx = mp.get_context("spawn")
    path = os.path.join(str(tmp_path), "state", "ledger.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ready = ctx.Queue()
    holder = ctx.Process(target=_hold_lock, args=(path, hold_seconds, ready))
    holder.start()
    try:
        assert ready.get(timeout=30) is True, "子进程没拿到锁，测试前提不成立"
        time.sleep(0.05)                       # 确保写方开始抢锁时，锁确实被占着
        Ledger(path).emit("probe", tag="waiter")
    finally:
        holder.join(30)

    out = capsys.readouterr().out
    assert "台账被占用" not in out and "台账正被其他任务占用" not in out, out
    events = load_events(path)
    assert [e.get("tag") for e in events] == ["waiter"], "等锁的那一行必须落账，不能丢"


def test_lock_budget_covers_a_whole_gate():
    """等锁预算是指死在代码里的合同：小于 2.5 秒即判红（单向棘轮，只准升不准降）。

    合并时最容易悄悄回退的就是这个数——分发侧 2.5 秒是被 4 进程 × 100 行实测逼出来的，
    候选侧一度写回 1.0 秒。行为用例最多测到 0.5 秒持锁，测不出预算缩小，故在此钉住常量。
    """
    from dramacraft import ledger
    assert ledger.LOCK_WAIT_SECONDS >= 2.5, ledger.LOCK_WAIT_SECONDS
