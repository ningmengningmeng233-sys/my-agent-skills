#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""传输重试与自适应并发（阶段 5 工单 5.3，规格 15.4 / 十）。

- **传输错误不消耗画面额度**：429、超时与临时 5xx 走有上限的指数退避（带抖动）重试；
  只有画面被审核否决才增加轮次（`round`）。指数退避把同一批工作者的重试打散，
  避免"同时失败、同时重试"的风暴。
- **并发受限流约束**：连续限流时 4→3→2→1 逐级下降，稳定成功后逐级恢复，
  但任何时刻都不得超过项目设置给出的上限。
- 安全拦截（`GENERATION_REJECTED`）、缺配置、参考图问题一律**不重试**：
  重试不会让它们变好，只会拖慢失败。
"""

from __future__ import annotations

import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .image_executor import (
    GENERATION_REJECTED,
    RATE_LIMITED,
    GenerationError,
    GenerationResult,
)

# 传输暂态：**按稳定码显式白名单**（不靠猜正则），外加上限兜底用的文本模式
RETRYABLE_CODES = frozenset({
    RATE_LIMITED, "RATE_LIMIT_EXCEEDED", "TOO_MANY_REQUESTS", "TRANSPORT_ERROR",
    "TIMEOUT", "TIMED_OUT", "TIMEOUT_ERROR", "DEADLINE_EXCEEDED", "UNAVAILABLE",
    "SERVICE_UNAVAILABLE", "RESOURCE_EXHAUSTED", "OVERLOADED", "CONNECTION_RESET",
    "CONNECTION_ERROR", "CONNECTION_ABORTED", "SERVER_ERROR", "INTERNAL_ERROR",
    "INTERNAL_SERVER_ERROR", "BAD_GATEWAY", "GATEWAY_TIMEOUT", "SERVICE_ERROR",
    "TEMPORARY_FAILURE",
})

# 重试也救不回来的失败：安全拦截、配置缺失、请求/参考图本身不合法、网关返回内容坏
NEVER_RETRY_CODES = frozenset({
    GENERATION_REJECTED, "POLICY_BLOCK", "EXECUTOR_NOT_CONFIGURED",
    "GENERATOR_NOT_CONFIGURED", "GENERATOR_UNKNOWN", "GATEWAY_BAD_RESPONSE",
    "INVALID_IMAGE_RESPONSE", "REFERENCE_UNRESOLVED", "REFERENCE_MIME_INVALID",
    "REFERENCE_LIMIT_EXCEEDED", "IMAGE_TOO_LARGE", "INVALID_IMAGE", "EMPTY_IMAGE",
})

_RETRYABLE_PATTERN = re.compile(
    r"(TIMEOUT|TIMED_OUT|CONNECTION|UNAVAILABLE|DEADLINE|RATE_LIMIT|OVERLOAD|"
    r"RESOURCE_EXHAUSTED|TRANSPORT|HTTP_5\d\d|_5\d\d$|TEMPORARY)",
    re.IGNORECASE)

_NEVER_RETRY_PATTERN = re.compile(
    r"(REJECT|POLICY|BLOCKED|NOT_CONFIGURED|UNKNOWN|UNRESOLVED|MISMATCH|UNSAFE|"
    r"UNSUPPORTED|BAD_RESPONSE|INVALID|MISSING)",
    re.IGNORECASE)


@dataclass(frozen=True)
class RetryPolicy:
    """有上限的指数退避参数（默认值即规格口径）。"""

    max_attempts: int = 4
    base_seconds: float = 2.0
    max_seconds: float = 30.0
    jitter_ratio: float = 0.25

    def is_retryable(self, code: str) -> bool:
        """只有传输暂态可退避重试；安全拦截与配置错误一律不重试。

        判定次序：显式不可重试码 → 显式可重试码 → 文本兜底（未知码才靠模式猜）。
        """
        text = str(code or "").strip().upper()
        if not text:
            return False
        if text in NEVER_RETRY_CODES:
            return False
        if text in RETRYABLE_CODES:
            return True
        if _NEVER_RETRY_PATTERN.search(text):
            return False
        return bool(_RETRYABLE_PATTERN.search(text))

    def delay_for(self, attempt: int, rand: Callable[[], float] | None = None) -> float:
        """第 `attempt` 次尝试失败后的等待秒数（1 起算）：指数增长、带上限、带抖动。"""
        number = max(1, int(attempt))
        raw = min(float(self.base_seconds) * (2 ** (number - 1)), float(self.max_seconds))
        ratio = max(0.0, min(0.999, float(self.jitter_ratio)))
        if ratio <= 0:
            return round(raw, 3)
        draw = (rand or random.random)()
        factor = 1.0 + ratio * (2.0 * float(draw) - 1.0)
        return round(min(raw * factor, float(self.max_seconds)), 3)

    def to_dict(self) -> dict:
        return {"max_attempts": int(self.max_attempts),
                "base_seconds": float(self.base_seconds),
                "max_seconds": float(self.max_seconds),
                "jitter_ratio": float(self.jitter_ratio)}


DEFAULT_RETRY_POLICY = RetryPolicy()


@dataclass
class ConcurrencyGovernor:
    """并发闸门：连续限流逐级下降，稳定成功后逐级恢复，永不超过上限。

    工单 9 扩展（全部为**加性**，不传新参数时行为与原先完全一致）：
    - `hard_max` 可放宽上限（默认仍收敛到 1–4，供视觉队列沿用项目设置）；
    - `on_success(elapsed_ms=…)` 支持近窗延迟反馈：平均延迟过高时同样收敛并发；
    - `on_server_error()` 处理 5xx：与限流同样降级。
    """

    ceiling: int = 3
    recover_after: int = 3
    current: int = field(default=0)
    _streak: int = field(default=0, repr=False)

    _HARD_MAX = 4
    hard_max: int = 0                     # 0 = 用 _HARD_MAX
    slow_ms: float = 3000.0
    _latencies: deque = field(default_factory=lambda: deque(maxlen=8), repr=False)

    def __post_init__(self) -> None:
        hard = int(self.hard_max) or type(self)._HARD_MAX
        ceiling = int(self.ceiling)
        ceiling = max(1, min(max(1, hard), ceiling))
        object.__setattr__(self, "ceiling", ceiling)
        if not self.current:
            object.__setattr__(self, "current", ceiling)
        object.__setattr__(self, "current",
                           max(1, min(ceiling, int(self.current))))

    def effective(self) -> int:
        return int(self.current)

    def _shrink(self) -> int:
        object.__setattr__(self, "current", max(1, int(self.current) - 1))
        object.__setattr__(self, "_streak", 0)
        return int(self.current)

    def on_rate_limited(self) -> int:
        """被限流：立刻降一级（下限 1），并清零成功连击。"""
        return self._shrink()

    def on_server_error(self) -> int:
        """5xx/连接类故障：同样立刻降一级。"""
        return self._shrink()

    def on_success(self, elapsed_ms: float | None = None) -> int:
        """成功：连续 `recover_after` 次后升一级，上限为 `ceiling`。

        传入 `elapsed_ms` 时额外做延迟反馈：近窗平均延迟超过 `slow_ms` 则收敛而非恢复。
        """
        if elapsed_ms is not None:
            self._latencies.append(max(0.0, float(elapsed_ms)))
            average = sum(self._latencies) / len(self._latencies)
            if average > float(self.slow_ms):
                return self._shrink()
        streak = int(self._streak) + 1
        if streak >= max(1, int(self.recover_after)) and int(self.current) < int(self.ceiling):
            object.__setattr__(self, "current", int(self.current) + 1)
            streak = 0
        object.__setattr__(self, "_streak", streak)
        return int(self.current)

    def to_dict(self) -> dict:
        return {"ceiling": int(self.ceiling), "current": int(self.current),
                "recover_after": int(self.recover_after)}


class RetryingExecutor:
    """在生成器外层做传输重试：只改传输行为，不碰任务状态与轮次。

    多个工作者线程会同时调用 `generate`，故计数用锁保护（否则并发限流时少报重试次数）。
    内层执行器必须自己不再重试（例如 `GeminiImageConfig.max_retries=1`），
    否则最坏调用次数会变成「外层 × 内层」的乘积。
    """

    def __init__(self, inner: Any, policy: RetryPolicy | None = None, *,
                 sleep: Callable[[float], None] | None = None,
                 on_retry: Callable[[str, int, float], None] | None = None) -> None:
        self.inner = inner
        self.policy = policy or DEFAULT_RETRY_POLICY
        self.sleep = sleep or time.sleep
        self.on_retry = on_retry
        self._retries = 0
        self._lock = threading.Lock()

    @property
    def retries(self) -> int:
        with self._lock:
            return self._retries

    def generate(self, request: Any) -> GenerationResult:
        attempts = max(1, int(self.policy.max_attempts))
        for attempt in range(1, attempts + 1):
            try:
                return self.inner.generate(request)
            except GenerationError as exc:
                retryable = self.policy.is_retryable(exc.code)
                if not retryable or attempt >= attempts:
                    raise
                delay = self.policy.delay_for(attempt)
                with self._lock:
                    self._retries += 1
                if self.on_retry is not None:
                    self.on_retry(exc.code, attempt, delay)
                self.sleep(delay)
        raise GenerationError(RATE_LIMITED, "重试次数已用尽")  # pragma: no cover - 循环必有出口

    @property
    def policy_dict(self) -> Mapping[str, Any]:
        return self.policy.to_dict()
