#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 5 工单 5.3：传输重试与自适应并发（规格 15.4 / 十）。

- 429／超时／临时 5xx 用**有上限的指数退避 + 抖动**重试，且**不消耗画面返工额度**
  （AttemptRecord 不新增、round 不变）；画面被审核否决才增加 round。
- 连续限流时并发 4→3→2→1，稳定成功后逐级恢复，但不得超过项目设置的上限。
"""
import base64
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.engine.visual_schema as schema_mod  # noqa: E402
from scripts.engine.image_executor import (  # noqa: E402
    EXECUTOR_NOT_CONFIGURED,
    GENERATION_REJECTED,
    RATE_LIMITED,
    GenerationError,
    GenerationRequest,
    GenerationResult,
)
from scripts.engine.retry_policy import (  # noqa: E402
    ConcurrencyGovernor,
    RetryPolicy,
    RetryingExecutor,
)
from scripts.engine.visual_events import read_events  # noqa: E402
from scripts.engine.visual_scheduler import VisualScheduler  # noqa: E402
from scripts.engine.visual_schema import (  # noqa: E402
    VisualPlan,
    VisualTask,
    load_visual_plan,
)

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")
STYLE_HASH = "a" * 64
STYLE_NAME = "仙侠国风电影CG"
TASK_ID = "CH_林姑娘-正脸-v01"


def _task(task_id=TASK_ID, **kw):
    base = dict(task_id=task_id, asset_id="CH_林姑娘", kind="正脸", status="PLANNED",
                aspect_ratio="1:1", style_slug=STYLE_NAME, style_version="1.0.0",
                style_hash=STYLE_HASH, prompt="【任务目标】正脸身份近景",
                fact_source=".process/资产索引.json#CH_林姑娘")
    base.update(kw)
    return VisualTask(**base)


def _plan(*tasks):
    return VisualPlan(schema_version=1, style_name=STYLE_NAME, style_slug=STYLE_NAME,
                      style_version="1.0.0", style_hash=STYLE_HASH, tasks=tuple(tasks))


def _tasks(count):
    return tuple(_task(task_id=f"CH_角色{i:02d}-正脸-v01", asset_id=f"CH_角色{i:02d}")
                 for i in range(1, count + 1))


def _request_factory(task, plan):
    _ = plan
    return GenerationRequest(task_id=task.task_id, prompt=task.prompt,
                             aspect_ratio=task.aspect_ratio, style_name=task.style_slug,
                             style_version=task.style_version)


class _FlakyExecutor:
    """先按脚本抛错，脚本用尽后成功；记录并发峰值与实际发生的退避等待。"""

    def __init__(self, codes=(), image=PNG_BYTES):
        self.codes = list(codes)
        self.image = image
        self.calls: list[str] = []
        self.delays: list[float] = []
        self.inflight = 0
        self.max_inflight = 0
        self.barrier = None
        self._lock = threading.Lock()

    def generate(self, request: GenerationRequest) -> GenerationResult:
        with self._lock:
            self.calls.append(request.task_id)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            code = self.codes.pop(0) if self.codes else ""
            seq = len(self.calls)
        try:
            if self.barrier is not None:
                try:
                    self.barrier.wait(timeout=10)
                except threading.BrokenBarrierError:
                    pass
            if code:
                raise GenerationError(code, f"注入传输故障 {code}")
            return GenerationResult(provider="fake", model="fake-model",
                                    image_bytes=self.image, mime_type="image/png",
                                    response_id=f"r{seq}", status="OK")
        finally:
            with self._lock:
                self.inflight -= 1


# --- 退避参数 -------------------------------------------------------------------

def test_delay_is_capped_exponential_backoff():
    policy = RetryPolicy(max_attempts=5, base_seconds=2.0, max_seconds=10.0, jitter_ratio=0.0)
    assert [policy.delay_for(n) for n in (1, 2, 3, 4, 5)] == [2.0, 4.0, 8.0, 10.0, 10.0], (
        "退避必须指数增长并有上限（无抖动时逐值可比）")


def test_jitter_stays_within_ratio():
    policy = RetryPolicy(base_seconds=4.0, max_seconds=100.0, jitter_ratio=0.25)
    samples = [policy.delay_for(2) for _ in range(300)]
    assert all(8.0 * 0.75 <= value <= 8.0 * 1.25 for value in samples), "抖动必须有界"
    assert len(set(round(v, 4) for v in samples)) > 1, "抖动必须真的随机（否则退避会同步打点）"


def test_delay_is_deterministic_when_jitter_disabled():
    policy = RetryPolicy(base_seconds=3.0, max_seconds=30.0, jitter_ratio=0.0)
    assert policy.delay_for(1) == policy.delay_for(1) == 3.0


def test_only_transport_failures_are_retryable():
    policy = RetryPolicy()
    assert policy.is_retryable(RATE_LIMITED)
    assert policy.is_retryable("GATEWAY_TIMEOUT")
    assert policy.is_retryable("HTTP_503")
    assert policy.is_retryable("CONNECTION_RESET")
    assert not policy.is_retryable(GENERATION_REJECTED), "安全拦截不得重试"
    assert not policy.is_retryable(EXECUTOR_NOT_CONFIGURED), "缺配置重试无意义"
    assert not policy.is_retryable("REFERENCE_UNRESOLVED")


def test_retry_policy_dict_is_serializable():
    payload = RetryPolicy().to_dict()
    assert payload["max_attempts"] >= 2
    assert payload["max_seconds"] >= payload["base_seconds"] > 0
    assert 0 <= payload["jitter_ratio"] < 1


# --- 重试执行器 -----------------------------------------------------------------

def test_retrying_executor_recovers_after_transient_failures():
    sleeps: list[float] = []
    inner = _FlakyExecutor(codes=(RATE_LIMITED, "GATEWAY_TIMEOUT"))
    attempts: list[tuple[str, int, float]] = []
    executor = RetryingExecutor(inner, RetryPolicy(base_seconds=1.0, jitter_ratio=0.0),
                                sleep=sleeps.append,
                                on_retry=lambda code, n, delay: attempts.append((code, n, delay)))
    result = executor.generate(GenerationRequest(task_id=TASK_ID, prompt="p"))
    assert result.succeeded
    assert len(inner.calls) == 3, "两次传输失败后第三次成功"
    assert executor.retries == 2
    assert sleeps == [1.0, 2.0], "退避必须逐次加大"
    assert [row[0] for row in attempts] == [RATE_LIMITED, "GATEWAY_TIMEOUT"]


def test_retrying_executor_gives_up_after_max_attempts():
    sleeps: list[float] = []
    inner = _FlakyExecutor(codes=(RATE_LIMITED,) * 10)
    executor = RetryingExecutor(inner, RetryPolicy(max_attempts=3, base_seconds=0.5,
                                                   jitter_ratio=0.0), sleep=sleeps.append)
    with pytest.raises(GenerationError) as excinfo:
        executor.generate(GenerationRequest(task_id=TASK_ID, prompt="p"))
    assert excinfo.value.code == RATE_LIMITED
    assert len(inner.calls) == 3, "尝试次数必须有上限"
    assert len(sleeps) == 2, "最后一次失败不再等待"


def test_retrying_executor_does_not_retry_policy_blocks():
    sleeps: list[float] = []
    inner = _FlakyExecutor(codes=(GENERATION_REJECTED,))
    executor = RetryingExecutor(inner, RetryPolicy(), sleep=sleeps.append)
    with pytest.raises(GenerationError):
        executor.generate(GenerationRequest(task_id=TASK_ID, prompt="p"))
    assert len(inner.calls) == 1, "安全拦截不得重试"
    assert sleeps == []


# --- 自适应并发 -----------------------------------------------------------------

def test_governor_steps_down_then_recovers_one_level_at_a_time():
    governor = ConcurrencyGovernor(ceiling=4, recover_after=3)
    assert governor.current == 4
    assert governor.on_rate_limited() == 3
    assert governor.on_rate_limited() == 2
    assert governor.on_rate_limited() == 1
    assert governor.on_rate_limited() == 1, "下限是 1，不得降到 0"
    for _ in range(3):
        governor.on_success()
    assert governor.current == 2, "连续 3 次成功才升一级"
    governor.on_rate_limited()
    assert governor.current == 1, "再次限流立刻降级"
    for _ in range(6):
        governor.on_success()
    assert governor.current == 3, "逐级恢复"


def test_governor_never_exceeds_project_ceiling():
    governor = ConcurrencyGovernor(ceiling=2, recover_after=1)
    for _ in range(10):
        governor.on_success()
    assert governor.current == 2, "恢复不得超过项目设置"
    assert governor.to_dict()["ceiling"] == 2


def test_governor_clamps_invalid_ceiling():
    assert ConcurrencyGovernor(ceiling=0).current == 1
    assert ConcurrencyGovernor(ceiling=99).current == 4, "并发上限固定为 4"


# --- 与调度器集成 ---------------------------------------------------------------

def _scheduler(lib, executor, policy, sleep):
    return VisualScheduler(lib, executor=executor, request_factory=_request_factory,
                           retry_policy=policy, sleep=sleep)


def test_transport_retries_do_not_consume_visual_rounds(tmp_path):
    """传输失败重试后成功：轮次仍为第 1 轮，attempts 只记一次。"""
    lib = tmp_path / "资产库"
    sleeps: list[float] = []
    inner = _FlakyExecutor(codes=(RATE_LIMITED, RATE_LIMITED))
    policy = RetryPolicy(base_seconds=1.0, jitter_ratio=0.0)
    summary = _scheduler(lib, inner, policy, sleeps.append).run_ready(
        _plan(_task()), max_parallel=1)
    stored = load_visual_plan(lib / "视觉资产" / "计划.toml").by_id(TASK_ID)
    assert stored.status == "WAITING_REVIEW"
    assert len(stored.attempts) == 1 and stored.attempts[0].round == 1, "重试不得新增轮次"
    assert inner.max_inflight == 1
    assert summary.generated == 1 and summary.transport_failed == 0
    assert summary.transport_retries == 2, "重试次数必须上报"
    assert sleeps == [1.0, 2.0]


def test_exhausted_transport_retries_keep_task_retryable(tmp_path):
    lib = tmp_path / "资产库"
    inner = _FlakyExecutor(codes=(RATE_LIMITED,) * 10)
    policy = RetryPolicy(max_attempts=2, base_seconds=0.5, jitter_ratio=0.0)
    summary = _scheduler(lib, inner, policy, lambda seconds: None).run_ready(
        _plan(_task()), max_parallel=1)
    stored = load_visual_plan(lib / "视觉资产" / "计划.toml").by_id(TASK_ID)
    assert stored.status == "PLANNED", "传输失败后任务必须仍可重试"
    assert stored.attempts == (), "传输失败不得消耗画面额度"
    assert summary.transport_failed == 1
    assert [e.kind for e in read_events(lib)].count("TRANSPORT_FAILED") == 1

    healthy = _FlakyExecutor()
    second = _scheduler(lib, healthy, policy, lambda seconds: None).run_ready(
        load_visual_plan(lib / "视觉资产" / "计划.toml"), max_parallel=1)
    retried = load_visual_plan(lib / "视觉资产" / "计划.toml").by_id(TASK_ID)
    assert retried.status == "WAITING_REVIEW"
    assert len(retried.attempts) == 1 and retried.attempts[0].round == 1
    assert second.generated == 1


def test_rate_limit_shrinks_concurrency_and_reports(tmp_path):
    """连续限流：并发 4→3→2→1；全部失败也不得消耗画面额度。"""
    lib = tmp_path / "资产库"
    inner = _FlakyExecutor(codes=(RATE_LIMITED,) * 40)
    policy = RetryPolicy(max_attempts=1, jitter_ratio=0.0)
    summary = _scheduler(lib, inner, policy, lambda seconds: None).run_ready(
        _plan(*_tasks(8)), max_parallel=4)
    assert summary.parallel_start == 4
    assert summary.parallel_end == 1, "连续限流必须把并发降到 1"
    assert summary.transport_failed == 8
    stored = load_visual_plan(lib / "视觉资产" / "计划.toml")
    assert all(t.attempts == () for t in stored.tasks), "传输失败一律不占轮次"
    assert all(t.status == "PLANNED" for t in stored.tasks)


def test_concurrency_recovers_after_successes(tmp_path):
    """限流后恢复：成功后逐级升回设置上限，且不越过上限。"""
    lib = tmp_path / "资产库"
    inner = _FlakyExecutor(codes=(RATE_LIMITED,) * 2)
    policy = RetryPolicy(max_attempts=1, jitter_ratio=0.0)
    # 2 次限流把并发降到 2；其后 12 次连续成功按「3 次升一级」足以上到上限 4
    summary = _scheduler(lib, inner, policy, lambda seconds: None).run_ready(
        _plan(*_tasks(14)), max_parallel=4)
    assert summary.parallel_end == 4, "稳定成功后必须恢复到设置上限"
    assert summary.generated == 12
    assert sum(1 for t in load_visual_plan(lib / "视觉资产" / "计划.toml").tasks
               if t.status == "WAITING_REVIEW") == 12


def test_retry_policy_is_recorded_in_summary(tmp_path):
    lib = tmp_path / "资产库"
    policy = RetryPolicy(max_attempts=3, base_seconds=0.25, max_seconds=2.0,
                         jitter_ratio=0.1)
    summary = _scheduler(lib, _FlakyExecutor(), policy, lambda seconds: None).run_ready(
        _plan(_task()), max_parallel=1)
    recorded = summary.to_dict()["retry_policy"]
    assert recorded == policy.to_dict(), "退避参数必须随运行摘要可追溯（压缩点 3）"


def test_sleep_is_injectable_so_tests_never_really_wait(tmp_path):
    lib = tmp_path / "资产库"
    started = time.monotonic()
    inner = _FlakyExecutor(codes=(RATE_LIMITED,) * 3)
    policy = RetryPolicy(max_attempts=4, base_seconds=30.0, jitter_ratio=0.0)
    _scheduler(lib, inner, policy, lambda seconds: None).run_ready(_plan(_task()),
                                                                   max_parallel=1)
    assert time.monotonic() - started < 5.0, "注入的 sleep 必须让测试不真等待"
    assert len(inner.calls) == 4


def test_transport_only_run_does_not_change_task_facts(tmp_path):
    """纯传输失败只留事件：任务事实不变、计划仍可读。"""
    lib = tmp_path / "资产库"
    schema_mod.write_visual_plan(_plan(_task()), lib / "视觉资产" / "计划.toml")
    policy = RetryPolicy(max_attempts=1, jitter_ratio=0.0)
    summary = _scheduler(lib, _FlakyExecutor(codes=(RATE_LIMITED,)), policy,
                         lambda seconds: None).run_ready(
        load_visual_plan(lib / "视觉资产" / "计划.toml"), max_parallel=1)
    after = load_visual_plan(lib / "视觉资产" / "计划.toml")
    assert after.tasks == _plan(_task()).tasks, "任务事实不得被传输失败改动"
    assert [e.kind for e in read_events(lib)] == ["GENERATION_STARTED",
                                                  "TRANSPORT_FAILED"]
    assert summary.plan.revision == after.revision
    assert after.revision > 0, "事件已落盘，计划修订必须前移"


def test_gateway_and_executor_codes_truth_table():
    """稳定码真值表：网关的 TRANSPORT_ERROR 必须重试，GATEWAY_BAD_RESPONSE/POLICY_BLOCK 不得重试。"""
    policy = RetryPolicy()
    retryable = ("RATE_LIMITED", "RATE_LIMIT_EXCEEDED", "TRANSPORT_ERROR",
                 "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED", "GATEWAY_TIMEOUT",
                 "SERVICE_UNAVAILABLE", "HTTP_503", "CONNECTION_RESET", "TIMEOUT")
    never = ("GENERATION_REJECTED", "POLICY_BLOCK", "GATEWAY_BAD_RESPONSE",
             "GENERATOR_NOT_CONFIGURED", "GENERATOR_UNKNOWN", "EXECUTOR_NOT_CONFIGURED",
             "REFERENCE_UNRESOLVED", "INVALID_IMAGE_RESPONSE", "")
    assert all(policy.is_retryable(code) for code in retryable), "这些码必须可退避重试"
    assert not any(policy.is_retryable(code) for code in never), "这些码重试无意义"


class _BlockingExecutor:
    """并发计数专用：每个线程进来都等齐后一起失败，用于验证计数不丢。"""

    def __init__(self, barrier):
        self.barrier = barrier
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0

    def generate(self, request):
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            self.barrier.wait(timeout=10)
            raise GenerationError(RATE_LIMITED, "并发限流")
        finally:
            with self.lock:
                self.inflight -= 1


def test_transport_retry_counter_is_exact_under_concurrency(tmp_path):
    """4 路并发同时重试：计数必须精确（旧实现会丢计数）。"""
    lib = tmp_path / "资产库"
    inner = _BlockingExecutor(threading.Barrier(4))
    policy = RetryPolicy(max_attempts=2, base_seconds=0.1, jitter_ratio=0.0)
    scheduler = VisualScheduler(lib, executor=inner, request_factory=_request_factory,
                                retry_policy=policy, sleep=lambda seconds: None)
    summary = scheduler.run_ready(_plan(*_tasks(4)), max_parallel=4)
    assert inner.max_inflight == 4, "四个任务必须同时开工"
    assert summary.transport_retries == 4, "每个任务恰好重试 1 次"
    assert summary.transport_failed == 4
    assert all(t.attempts == () for t in
               load_visual_plan(lib / "视觉资产" / "计划.toml").tasks)


def test_retry_does_not_multiply_inner_retries():
    """外层重试不得叠加内层重试：总调用次数以策略的 max_attempts 为上限。"""
    inner = _FlakyExecutor(codes=(RATE_LIMITED,) * 20)
    executor = RetryingExecutor(inner, RetryPolicy(max_attempts=3, base_seconds=0.1,
                                                   jitter_ratio=0.0),
                                sleep=lambda seconds: None)
    with pytest.raises(GenerationError):
        executor.generate(GenerationRequest(task_id=TASK_ID, prompt="p"))
    assert len(inner.calls) == 3, "总调用次数必须等于策略上限（不再乘以内层重试）"


def test_manifest_unreadable_fails_loudly_instead_of_blocking_silently(tmp_path):
    """正式版本清单损坏：必须显式失败，不得当作「什么都没发布」把下游静默停住。"""
    from scripts.engine.visual_scheduler import SchedulerError

    lib = tmp_path / "资产库"
    plan = _plan(_task("CH_甲-正脸-v01"),
                 _task("CH_乙-正脸-v01", depends_on=("CH_甲-正脸-v01",)))
    (lib / "视觉资产").mkdir(parents=True, exist_ok=True)
    (lib / "视觉资产" / "正式版本.json").write_text("{ 半截 JSON", encoding="utf-8")
    scheduler = VisualScheduler(lib, executor=_FlakyExecutor(),
                                request_factory=_request_factory,
                                retry_policy=RetryPolicy(max_attempts=1),
                                sleep=lambda seconds: None)
    with pytest.raises(SchedulerError) as excinfo:
        scheduler.ready_tasks(plan)
    assert excinfo.value.code == "RELEASE_MANIFEST_UNREADABLE"


def test_publish_while_scheduler_holds_the_lock_does_not_self_deadlock(tmp_path):
    """调度器持锁时发布器再取一次锁必须可重入（否则会等满超时并报 LOCK_BUSY）。"""
    from scripts.engine.review_contract import (
        build_review_request,
        load_review_result,
        result_path,
    )
    from scripts.engine.visual_publisher import publish_task
    from scripts.engine.visual_scheduler import plan_lock

    lib = tmp_path / "资产库"
    (lib / "视觉资产").mkdir(parents=True, exist_ok=True)
    schema_mod.write_visual_plan(_plan(_task()), lib / "视觉资产" / "计划.toml")
    from conftest import publish_task_with_review

    publish_task_with_review(lib, TASK_ID)
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    task = plan.by_id(TASK_ID)
    request = build_review_request(lib, plan, task)
    result = load_review_result(result_path(lib, TASK_ID))
    started = time.monotonic()
    with plan_lock(lib, timeout=1.0):
        publish_task(lib, plan, task, request, result)
    assert time.monotonic() - started < 1.0, "可重入锁不得等待"
