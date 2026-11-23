# -*- coding: utf-8 -*-
"""提取 / 生成 / 审核三队列、请求预算与自适应并发（v2.17.0 工单 9）。

- **三队列**各自并发上限，资源互不阻塞；任务带依赖，依赖满足后**自动推进**（不需要用户反复催）。
- **不饿死**：每个队列多槽位 + 待办按任务大小升序派发，慢端点上的长任务不会占死小任务。
- **自适应并发**：由上限、429/5xx、近窗延迟共同决定；429 立即降，恢复**每次一个**、需累计成功次数。
  上限来自 `MAX_PARALLEL`，**不把固定 32 当正确答案**。
  并发闸门**复用 `retry_policy.ConcurrencyGovernor`**（视觉队列早就在用它），本模块只做加性扩展
  （放宽上限的 `hard_max`、延迟反馈、5xx 降级），不另造第二套实现。
- **请求预算**：输入超预算时按事实边界拆分，**绝不截断单条事实**；单条自身超预算显式报错。
- **用量归一化**：端点不返回的字段一律记 `unknown`，不猜。
- **模型能力适配器**：可声明是否支持关闭思考；不支持时靠拆分、预算、缓存与任务大小路由控成本。
- **失败分类**：可重试 / 需换端点 / 需人工裁决；可重试者带退避与**幂等键**（用任务 id，避免重复产物）。
"""
from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, Tuple

from .retry_policy import ConcurrencyGovernor

QUEUE_EXTRACT = "extract"
QUEUE_GENERATE = "generate"
QUEUE_REVIEW = "review"
QUEUES: Tuple[str, ...] = (QUEUE_EXTRACT, QUEUE_GENERATE, QUEUE_REVIEW)

# 各队列默认并发上限：资源不同、瓶颈不同，因此不共用一个数字。
DEFAULT_CAPS: Dict[str, int] = {QUEUE_EXTRACT: 4, QUEUE_GENERATE: 2, QUEUE_REVIEW: 2}
MIN_PARALLEL = 1
MAX_PARALLEL = 8

STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"

RETRYABLE = "retryable"
SWITCH_ENDPOINT = "switch_endpoint"
NEEDS_HUMAN = "needs_human"
UNKNOWN = "unknown"

_SLOW_MS = 3000.0          # 近窗平均延迟高于此值即收敛并发
_RECOVER_EVERY = 3         # 每累计这么多次成功才 +1，保证「缓慢」恢复


def make_governor(cap: int, *, recover_after: int = _RECOVER_EVERY,
                  slow_ms: float = _SLOW_MS) -> ConcurrencyGovernor:
    """构造一个队列的并发闸门：上限放宽到 `MAX_PARALLEL`，其余沿用既有语义。"""
    return ConcurrencyGovernor(ceiling=cap, recover_after=recover_after,
                               hard_max=MAX_PARALLEL, slow_ms=slow_ms)


class BudgetError(ValueError):
    """超出请求预算且无法在不截断事实的前提下拆分。"""


class HttpFailure(RuntimeError):
    """带状态码的调用失败，供失败分类使用。"""

    def __init__(self, status_code: int, message: str = "") -> None:
        self.status_code = int(status_code)
        super().__init__(f"HTTP {self.status_code} {message}".strip())


@dataclass(frozen=True)
class RequestBudget:
    """一次请求的五维预算；任一项超出都不许靠截断事实蒙混过去。"""

    max_input_chars: int = 24_000
    max_expected_output_chars: int = 8_000
    timeout_seconds: float = 600.0
    no_progress_seconds: float = 120.0
    total_seconds: float = 1800.0


def split_by_budget(items: Sequence[Any], budget: RequestBudget, *, separator: str = "\n") -> List[List[Any]]:
    """按输入预算把条目切块。**绝不丢条目、绝不改顺序、绝不截断单条事实**。"""
    limit = int(budget.max_input_chars)
    if limit <= 0:
        raise BudgetError(f"输入预算必须为正：{limit}")
    chunks: List[List[Any]] = []
    current: List[Any] = []
    used = 0
    for item in items:
        text = str(item)
        cost = len(text) + (len(separator) if current else 0)
        if current and used + cost > limit:
            chunks.append(current)
            current, used = [], 0
            cost = len(text)
        if cost > limit:
            raise BudgetError(
                f"单条事实 {len(text)} 字符超过输入预算 {limit}——按纪律不得截断，请人工拆分该条")
        current.append(item)
        used += cost
    if current:
        chunks.append(current)
    return chunks


def normalize_usage(raw: Mapping[str, Any] | None, *, input_chars: int, landed_chars: int) -> dict:
    """把端点用量归一化；**缺字段记 `unknown`，不猜**。"""
    data = dict(raw or {})
    details = data.get("completion_tokens_details")
    if isinstance(details, Mapping) and "reasoning_tokens" not in data:
        data["reasoning_tokens"] = details.get("reasoning_tokens")

    def token(name: str) -> Any:
        value = data.get(name)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else UNKNOWN

    completion = data.get("completion_tokens")
    ratio: Any = UNKNOWN
    if isinstance(completion, (int, float)) and not isinstance(completion, bool) and completion > 0:
        ratio = round(float(landed_chars) / float(completion), 6)
    return {
        "prompt_tokens": token("prompt_tokens"),
        "completion_tokens": token("completion_tokens"),
        "reasoning_tokens": token("reasoning_tokens"),
        "input_chars": int(input_chars),
        "effective_landed_chars": int(landed_chars),
        "effective_ratio": ratio,
    }


@dataclass(frozen=True)
class ModelCapability:
    """模型能力声明：只声明事实，不假设「关掉思考」一定可用。"""

    name: str
    supports_thinking_off: bool = False

    def request_payload_flag(self) -> dict:
        return {"thinking": {"type": "disabled"}} if self.supports_thinking_off else {}


def cost_control_plan(capability: ModelCapability) -> dict:
    """成本控制方案：不支持关闭思考时，靠拆分/预算/缓存/路由而不是靠猜参数。"""
    return {
        "split_by_budget": True,
        "use_cache": True,
        "route_by_task_size": True,
        "thinking_off": bool(capability.supports_thinking_off),
    }


def classify_failure(*, status_code: int | None = None, error: BaseException | None = None) -> str:
    """失败三分类：可重试 / 需换端点 / 需人工裁决。

    没有状态码（超时、连接断开、OS 层错误等无状态码的传输失败）一律按可重试处理——
    原先这里单列了 `isinstance(error, (TimeoutError, ConnectionError, OSError))` 再返回
    RETRYABLE，与紧随其后的兜底 `return RETRYABLE` 同值，属恒同分支，已并入兜底。
    """
    code = status_code
    if code is None and isinstance(error, HttpFailure):
        code = error.status_code
    if code is not None:
        if code == 429:
            return SWITCH_ENDPOINT
        if code in (401, 403):
            return NEEDS_HUMAN
        if code >= 500:
            return RETRYABLE
        if code >= 400:
            return NEEDS_HUMAN
        return RETRYABLE
    return RETRYABLE


@dataclass
class TaskResult:
    task_id: str
    queue: str
    status: str
    attempts: int = 0
    failure_kind: str = ""
    idempotency_key: str = ""
    value: Any = None
    error: str = ""


@dataclass
class _Task:
    queue: str
    task_id: str
    run: Callable[[], Any]
    size: int = 0
    deps: Tuple[str, ...] = ()


class WorkScheduler:
    """三队列调度器：依赖满足即自动推进；只有**真实阻断**才停下。"""

    def __init__(self, *, caps: Mapping[str, int] | None = None, max_retries: int = 3,
                 backoff_seconds: float = 0.5) -> None:
        self.caps = dict(DEFAULT_CAPS)
        self.caps.update(caps or {})
        self.controllers = {q: make_governor(self.caps[q]) for q in QUEUES}
        self.max_retries = max(1, int(max_retries))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self._tasks: Dict[str, _Task] = {}
        self.results: Dict[str, TaskResult] = {}

    # --- 提交 -----------------------------------------------------------------

    def submit(self, queue: str, task_id: str, run: Callable[[], Any], *,
               size: int = 0, deps: Iterable[str] = ()) -> None:
        if queue not in QUEUES:
            raise ValueError(f"未知队列：{queue}（合法值 {QUEUES}）")
        if task_id in self._tasks:
            raise ValueError(f"任务 id 重复：{task_id}")
        self._tasks[task_id] = _Task(queue=queue, task_id=task_id, run=run,
                                     size=int(size), deps=tuple(deps))

    # --- 调度 -----------------------------------------------------------------

    def _deps_done(self, task: _Task) -> bool:
        return all(self.results.get(dep) and self.results[dep].status == STATUS_DONE
                   for dep in task.deps)

    def _mark_blocked(self, pending: Dict[str, _Task]) -> None:
        changed = True
        while changed:
            changed = False
            for task_id, task in list(pending.items()):
                for dep in task.deps:
                    result = self.results.get(dep)
                    if result and result.status in (STATUS_FAILED, STATUS_BLOCKED):
                        pending.pop(task_id)
                        self.results[task_id] = TaskResult(
                            task_id=task_id, queue=task.queue, status=STATUS_BLOCKED,
                            idempotency_key=task_id,
                            error=f"依赖 {dep} 未成功（{result.status}），不推测继续")
                        changed = True
                        break

    def run(self) -> Dict[str, TaskResult]:
        """执行到全部完成/失败/阻断。返回结果表。"""
        pending: Dict[str, _Task] = dict(self._tasks)
        executors = {q: ThreadPoolExecutor(max_workers=max(self.caps[q], MAX_PARALLEL)) for q in QUEUES}
        futures: Dict[Any, _Task] = {}
        running = {q: 0 for q in QUEUES}
        try:
            while pending or futures:
                self._mark_blocked(pending)
                for queue in QUEUES:
                    free = self.controllers[queue].effective() - running[queue]
                    if free <= 0:
                        continue
                    runnable = [t for t in pending.values()
                                if t.queue == queue and self._deps_done(t)]
                    runnable.sort(key=lambda t: (t.size, t.task_id))
                    for task in runnable[:free]:
                        pending.pop(task.task_id)
                        futures[executors[queue].submit(self._execute, task)] = task
                        running[queue] += 1
                if not futures:
                    break
                done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    task = futures.pop(future)
                    running[task.queue] -= 1
                    try:
                        future.result()
                    except BaseException:            # 结果已在 _execute 里归档
                        pass
        finally:
            for executor in executors.values():
                executor.shutdown(wait=True)
        return self.results

    def _execute(self, task: _Task) -> None:
        attempts = 0
        while True:
            attempts += 1
            started = time.time()
            try:
                value = task.run()
            except BaseException as exc:
                kind = classify_failure(status_code=getattr(exc, "status_code", None), error=exc)
                governor = self.controllers[task.queue]
                if kind == SWITCH_ENDPOINT:
                    governor.on_rate_limited()
                elif kind == RETRYABLE:
                    code = getattr(exc, "status_code", None)
                    if code is None or code >= 500:
                        governor.on_server_error()
                if kind == NEEDS_HUMAN or attempts > self.max_retries:
                    self.results[task.task_id] = TaskResult(
                        task_id=task.task_id, queue=task.queue, status=STATUS_FAILED,
                        attempts=attempts, failure_kind=kind, idempotency_key=task.task_id,
                        error=str(exc))
                    return
                if self.backoff_seconds:
                    time.sleep(self.backoff_seconds * attempts)
                continue
            self.controllers[task.queue].on_success(elapsed_ms=(time.time() - started) * 1000)
            self.results[task.task_id] = TaskResult(
                task_id=task.task_id, queue=task.queue, status=STATUS_DONE,
                attempts=attempts, idempotency_key=task.task_id, value=value)
            return

    # --- 统计 -----------------------------------------------------------------

    def stats(self) -> dict:
        counts = {status: 0 for status in (STATUS_DONE, STATUS_FAILED, STATUS_BLOCKED)}
        for result in self.results.values():
            counts[result.status] = counts.get(result.status, 0) + 1
        return {
            "total": len(self._tasks),
            "done": counts[STATUS_DONE],
            "failed": counts[STATUS_FAILED],
            "blocked": counts[STATUS_BLOCKED],
            "effective": {q: self.controllers[q].effective() for q in QUEUES},
        }
