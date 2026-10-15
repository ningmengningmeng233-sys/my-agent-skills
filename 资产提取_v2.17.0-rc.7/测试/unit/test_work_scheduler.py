#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 9：提取 / 生成 / 审核三队列、请求预算与自适应并发。

覆盖：
- 三种队列各自并发上限；资源互不阻塞，依赖满足后自动推进；
- 慢端点上一个长任务不会饿死小任务（最短作业优先 + 多槽位）；
- 并发随 429/5xx/延迟/任务大小动态调整：429 降并发，稳定后**缓慢**恢复，且上限不是固定 32；
- 请求预算：输入超预算时**按事实边界拆分，绝不截断单条事实**；
- 用量归一化：端点不返回的字段记 `unknown`，不猜；
- 失败分类：可重试 / 需换端点 / 需人工裁决；重试带退避与幂等键；
- 除真实阻断（依赖失败）外自动继续，不需要用户反复催。

模块按 pytest fixture 惰性导入：红测阶段模块尚未实现时只让本文件红，不中断整个回归。
"""
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="module")
def ws():
    from scripts.engine import work_scheduler as module

    return module


# --- 三队列与依赖推进 ----------------------------------------------------------

def test_three_queues_are_fixed(ws):
    assert ws.QUEUES == (ws.QUEUE_EXTRACT, ws.QUEUE_GENERATE, ws.QUEUE_REVIEW)


def test_concurrency_cap_is_not_a_hardcoded_32(ws):
    for queue in ws.QUEUES:
        assert 1 <= ws.DEFAULT_CAPS[queue] <= ws.MAX_PARALLEL < 32


def test_blocked_dependency_never_runs_and_does_not_hang(ws):
    s = ws.WorkScheduler()
    ran = []
    s.submit(ws.QUEUE_EXTRACT, "bad", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    s.submit(ws.QUEUE_GENERATE, "child", lambda: ran.append("child"), deps=("bad",))
    s.run()
    assert ran == []
    assert s.results["child"].status == ws.STATUS_BLOCKED
    assert s.results["bad"].status == ws.STATUS_FAILED


def test_dependency_satisfied_advances_automatically(ws):
    s = ws.WorkScheduler()
    order = []
    s.submit(ws.QUEUE_EXTRACT, "a", lambda: order.append("a"))
    s.submit(ws.QUEUE_GENERATE, "b", lambda: order.append("b"), deps=("a",))
    s.run()
    assert order == ["a", "b"]
    assert all(r.status == ws.STATUS_DONE for r in s.results.values())


# --- 慢端点不饿死小任务 --------------------------------------------------------

def test_short_tasks_are_not_starved_by_one_long_task(ws):
    """并发上限 2：一个长任务占一个槽，两个小任务必须在它结束前跑完。"""
    s = ws.WorkScheduler(caps={ws.QUEUE_EXTRACT: 2, ws.QUEUE_GENERATE: 1, ws.QUEUE_REVIEW: 1})
    done_at = {}
    started = time.time()
    s.submit(ws.QUEUE_EXTRACT, "long", lambda: (time.sleep(0.45), done_at.setdefault("long", time.time())))
    s.submit(ws.QUEUE_EXTRACT, "small-1", lambda: done_at.setdefault("small-1", time.time()))
    s.submit(ws.QUEUE_EXTRACT, "small-2", lambda: done_at.setdefault("small-2", time.time()))
    s.run()
    assert set(done_at) == {"long", "small-1", "small-2"}
    assert done_at["small-1"] < done_at["long"], done_at
    assert done_at["small-2"] < done_at["long"], done_at
    assert done_at["long"] - started >= 0.4


def test_queues_do_not_block_each_other(ws):
    """生成队列被长任务占满时，提取与审核队列仍必须推进。"""
    s = ws.WorkScheduler(caps={ws.QUEUE_EXTRACT: 1, ws.QUEUE_GENERATE: 1, ws.QUEUE_REVIEW: 1})
    seen = []
    s.submit(ws.QUEUE_GENERATE, "slow", lambda: (time.sleep(0.3), seen.append("generate")))
    s.submit(ws.QUEUE_EXTRACT, "fast", lambda: seen.append("extract"))
    s.submit(ws.QUEUE_REVIEW, "review", lambda: seen.append("review"))
    s.run()
    assert "extract" in seen and "review" in seen


# --- 自适应并发 ----------------------------------------------------------------

def test_concurrency_drops_on_429_and_recovers_slowly(ws):
    c = ws.make_governor(8)
    assert c.effective() == 8
    c.on_rate_limited()
    assert c.effective() < 8, "429 必须降并发"
    dropped = c.effective()
    for _ in range(20):
        c.on_success(elapsed_ms=100)
    assert c.effective() >= dropped
    assert c.effective() <= 8, "恢复不得越过上限"
    # 缓慢恢复：一次成功不足以回到上限
    c.on_rate_limited()
    after = c.effective()
    c.on_success(elapsed_ms=100)
    assert c.effective() <= after + 1


def test_concurrency_never_below_one(ws):
    c = ws.make_governor(2)
    for _ in range(20):
        c.on_rate_limited()
    assert c.effective() == 1


def test_slow_endpoint_shrinks_concurrency_and_fast_one_recovers(ws):
    c = ws.make_governor(6)
    for _ in range(6):
        c.on_success(elapsed_ms=9000)
    slow = c.effective()
    assert slow < 6, "近窗延迟高必须收敛并发"
    for _ in range(40):
        c.on_success(elapsed_ms=80)
    assert c.effective() > slow


def test_server_errors_also_reduce_concurrency(ws):
    c = ws.make_governor(6)
    c.on_server_error()
    assert c.effective() < 6


def test_effective_concurrency_respects_cap(ws):
    c = ws.make_governor(3)
    for _ in range(50):
        c.on_success(elapsed_ms=10)
    assert c.effective() == 3


# --- 请求预算与拆分 ------------------------------------------------------------

def test_budget_exceeded_splits_without_truncating_facts(ws):
    budget = ws.RequestBudget(max_input_chars=22)
    facts = ["甲" * 10, "乙" * 10, "丙" * 10]
    chunks = ws.split_by_budget(facts, budget)
    # 前两条放得下（10 + 1 + 10 = 21 ≤ 22），第三条必须另起一块
    assert [[len(item) for item in chunk] for chunk in chunks] == [[10, 10], [10]]
    joined = [item for chunk in chunks for item in chunk]
    assert joined == facts, "拆分不得丢条目、不得改顺序、不得截断"
    for chunk in chunks:
        assert sum(len(i) for i in chunk) + (len(chunk) - 1) <= 22


def test_single_oversized_fact_is_reported_not_truncated(ws):
    budget = ws.RequestBudget(max_input_chars=10)
    with pytest.raises(ws.BudgetError) as ctx:
        ws.split_by_budget(["超" * 100], budget)
    assert "不得截断" in str(ctx.value)


def test_budget_has_all_five_dimensions(ws):
    b = ws.RequestBudget()
    for field in ("max_input_chars", "max_expected_output_chars", "timeout_seconds",
                  "no_progress_seconds", "total_seconds"):
        assert getattr(b, field) > 0, field


# --- 用量归一化：不知道就记 unknown --------------------------------------------

def test_usage_records_reasoning_and_completion_tokens(ws):
    raw = {"prompt_tokens": 100, "completion_tokens": 50, "reasoning_tokens": 20}
    u = ws.normalize_usage(raw, input_chars=400, landed_chars=100)
    assert u["prompt_tokens"] == 100
    assert u["completion_tokens"] == 50
    assert u["reasoning_tokens"] == 20
    assert u["effective_landed_chars"] == 100


def test_missing_usage_fields_are_unknown_not_guessed(ws):
    u = ws.normalize_usage({}, input_chars=400, landed_chars=100)
    for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
        assert u[field] == ws.UNKNOWN, field
    assert u["effective_ratio"] is not None


def test_effective_ratio_is_landed_over_completion(ws):
    u = ws.normalize_usage({"completion_tokens": 200}, input_chars=10, landed_chars=150)
    assert abs(u["effective_ratio"] - 0.75) < 1e-9
    assert ws.normalize_usage({}, input_chars=1, landed_chars=1)["effective_ratio"] == ws.UNKNOWN


# --- 模型能力适配器 ------------------------------------------------------------

def test_adapter_declares_thinking_off_support(ws):
    on = ws.ModelCapability(name="m1", supports_thinking_off=True)
    off = ws.ModelCapability(name="m2", supports_thinking_off=False)
    assert on.request_payload_flag() == {"thinking": {"type": "disabled"}}
    assert off.request_payload_flag() == {}


def test_unsupported_thinking_off_still_controls_cost_by_budget_and_cache(ws):
    cap = ws.ModelCapability(name="m2", supports_thinking_off=False)
    plan = ws.cost_control_plan(cap)
    assert plan["split_by_budget"] is True
    assert plan["use_cache"] is True
    assert plan["route_by_task_size"] is True


# --- 失败分类与重试 ------------------------------------------------------------

def test_failure_classification_covers_three_kinds(ws):
    assert ws.classify_failure(status_code=429) == ws.SWITCH_ENDPOINT
    assert ws.classify_failure(status_code=401) == ws.NEEDS_HUMAN
    assert ws.classify_failure(status_code=403) == ws.NEEDS_HUMAN
    assert ws.classify_failure(status_code=503) == ws.RETRYABLE
    assert ws.classify_failure(error=TimeoutError("timeout")) == ws.RETRYABLE
    assert ws.classify_failure(status_code=422) == ws.NEEDS_HUMAN


def test_retryable_task_is_retried_with_idempotency_key(ws):
    s = ws.WorkScheduler(max_retries=2, backoff_seconds=0.0)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("slow")
        return "ok"

    s.submit(ws.QUEUE_GENERATE, "t1", flaky)
    s.run()
    assert len(attempts) == 2
    assert s.results["t1"].status == ws.STATUS_DONE
    assert s.results["t1"].idempotency_key == "t1"


def test_needs_human_failure_is_not_retried(ws):
    s = ws.WorkScheduler(max_retries=3, backoff_seconds=0.0)
    attempts = []

    def auth_fail():
        attempts.append(1)
        raise ws.HttpFailure(status_code=401, message="bad key")

    s.submit(ws.QUEUE_GENERATE, "t2", auth_fail)
    s.run()
    assert len(attempts) == 1, "需人工裁决的失败不得重试"
    assert s.results["t2"].status == ws.STATUS_FAILED
    assert s.results["t2"].failure_kind == ws.NEEDS_HUMAN


def test_rate_limited_failure_lowers_the_queue_concurrency(ws):
    s = ws.WorkScheduler(backoff_seconds=0.0)
    state = []

    def limited():
        state.append(1)
        raise ws.HttpFailure(status_code=429, message="slow down")

    s.submit(ws.QUEUE_GENERATE, "t3", limited)
    s.run()
    assert s.controllers[ws.QUEUE_GENERATE].effective() < ws.DEFAULT_CAPS[ws.QUEUE_GENERATE]
