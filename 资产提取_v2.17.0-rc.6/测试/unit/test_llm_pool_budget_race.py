#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AE-15（TICKET-47）：调用预算临界区互斥，杜绝并发超支。

回归门：10 线程并发调用 max_calls=3 的池，成功数必须严格等于 3，
其余被 BudgetExceeded 拦截，已用计数严格等于上限。
分发用实例桩代替（零真实网络）；失败分发不消耗预算（语义保持）。
"""
import threading
import time

import pytest

from engine import llm_pool


def _pool(max_calls, dispatch):
    node = llm_pool.Node(name="A", proto="openai", base_url="http://127.0.0.1:9/v1",
                         model="m", api_key="k", max_concurrency=16)
    pool = llm_pool.CallPool([node], global_limit=16, max_calls=max_calls)
    pool._dispatch = dispatch  # 实例桩：零真实网络
    return pool


def test_concurrent_calls_never_exceed_max_calls():
    start = threading.Barrier(10)

    def fake_dispatch(node, system_prompt, user_prompt, **kw):
        time.sleep(0.2)  # 拉宽“先查后改”竞态窗口：无锁实现必超支
        return ("ok", {})

    pool = _pool(3, fake_dispatch)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def worker():
        start.wait(timeout=15)
        try:
            pool.call("s", "u")
            result = "ok"
        except llm_pool.BudgetExceeded:
            result = "budget"
        with outcomes_lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for item in threads:
        item.start()
    for item in threads:
        item.join(timeout=60)
    assert all(not item.is_alive() for item in threads), "工作线程卡死"
    assert outcomes.count("ok") == 3, f"成功数偏离预算：{outcomes.count('ok')} != 3"
    assert outcomes.count("budget") == 7, f"拦截数偏离预期：{outcomes}"
    assert pool._used_calls == 3, f"预算超支：已用 {pool._used_calls} / 上限 3"


def test_failed_dispatch_does_not_consume_budget():
    """语义保持：分发失败（节点淘汰）不消耗预算。"""

    def failing(node, system_prompt, user_prompt, **kw):
        raise llm_pool._NodeFailure("fatal", "boom")

    pool = _pool(1, failing)
    with pytest.raises(llm_pool.AllNodesExhausted):
        pool.call("s", "u")
    assert pool._used_calls == 0
