#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单写者视觉调度器（阶段 5 工单 5.1，规格 15.4 / 八）。

职责边界：

- **协调器是唯一可写 `视觉资产/计划.toml` 的对象**。并发工作器只接收不可变任务快照，
  返回事件（`GENERATION_STARTED` / `TRANSPORT_FAILED` / `IMAGE_STORED` /
  `REVIEW_REQUESTED` / `REVIEW_IMPORTED` / `PUBLISHED` / `TASK_BLOCKED`）。
- 提交次序固定为「先追加事件（写前日志）→ 再应用事件 → 最后原子写计划」，
  进程在任意一步中断都能靠重放事件恢复，不重复请求已生成的图片。
- 提交前持锁并校验计划 revision：磁盘上的计划版本比本次基线新时，把事件重新应用到
  最新版本（事件按内容幂等），绝不整份覆盖他人的更新。
- 上游失败只阻塞依赖它的任务；无关任务继续。输入未变的已通过任务直接复用。

锁文件固定 `.process/视觉调度.lock`，事件日志固定 `.process/视觉事件.jsonl`。
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from . import visual_schema
from .image_executor import GENERATION_REJECTED, RATE_LIMITED, GenerationError
from .project_settings import DEFAULT_MAX_PARALLEL, MAX_PARALLEL, MIN_PARALLEL
from .review_contract import (
    ReviewContractError,
    build_review_request,
    result_path,
    sha256_text,
)
from .review_provider import FileReviewProvider
from .retry_policy import (
    DEFAULT_RETRY_POLICY,
    ConcurrencyGovernor,
    RetryPolicy,
    RetryingExecutor,
)
from .visual_events import (
    GENERATION_STARTED,
    IMAGE_STORED,
    PUBLISHED,
    REVIEW_IMPORTED,
    REVIEW_REQUESTED,
    TASK_BLOCKED,
    TRANSPORT_FAILED,
    VisualEvent,
    append_events,
    apply_events,
    read_events,
)
from .visual_pipeline import run_task
from .visual_publisher import (
    PublishError,
    current_release,
    load_release_manifest,
    publish_task,
)
from .visual_request_chain import VisualCompileError
from .visual_schema import VisualPlan, VisualTask, VisualTaskStatus, normalize_rel_path
from .visual_store import StoreError

PLAN_LOCK_REL = (".process", "视觉调度.lock")
PAUSE_REL = (".process", "视觉暂停.json")
LOCK_STALE_SECONDS = 300.0
LOCK_POLL_SECONDS = 0.05

COMPILE_FAILED = "COMPILE_FAILED"
ROUNDS_EXHAUSTED = "ROUNDS_EXHAUSTED"
REVIEW_NO_IMPROVEMENT = "REVIEW_NO_IMPROVEMENT"

S = VisualTaskStatus
READY_SOURCE_STATUSES = (S.PLANNED.value, S.NEEDS_REVISION.value, S.FAILED.value)
REUSABLE_STATUSES = (S.ACCEPTED.value, S.READY_AS_REFERENCE.value)
PENDING_REVIEW_STATUSES = (S.WAITING_REVIEW.value, S.NEEDS_REVIEW.value)


class SchedulerError(RuntimeError):
    """调度失败：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lock_path(lib: Path) -> Path:
    return Path(lib).joinpath(*PLAN_LOCK_REL)


def _read_lock(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _os_lock(handle: int) -> None:
    """对打开的文件句柄加独占非阻塞锁（Windows 用 msvcrt，POSIX 用 flock）。

    交给内核而不是「建文件/删文件」协议：互斥由内核保证，进程崩溃时锁随句柄
    自动释放，既没有抢占竞态，也不会留下删不掉的锁文件。
    """
    os.lseek(handle, 0, os.SEEK_SET)
    if os.name == "nt":  # pragma: no cover - 平台分支
        import msvcrt

        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - 平台分支
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(handle: int) -> None:
    os.lseek(handle, 0, os.SEEK_SET)
    if os.name == "nt":  # pragma: no cover - 平台分支
        import msvcrt

        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - 平台分支
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)


_THREAD_LOCAL = threading.local()


def _held_depth(path: Path) -> int:
    held = getattr(_THREAD_LOCAL, "held", None)
    if held is None:
        held = {}
        _THREAD_LOCAL.held = held
    return held.get(str(path), 0)


def _set_held_depth(path: Path, depth: int) -> None:
    held = getattr(_THREAD_LOCAL, "held", None)
    if held is None:
        held = {}
        _THREAD_LOCAL.held = held
    if depth <= 0:
        held.pop(str(path), None)
    else:
        held[str(path)] = depth


@contextmanager
def plan_lock(lib: Path, *, timeout: float = 10.0, stale_seconds: float = LOCK_STALE_SECONDS,
              poll_seconds: float = LOCK_POLL_SECONDS) -> Iterator[Path]:
    """计划写锁（`.process/视觉调度.lock`）：内核级独占，持锁者崩溃即自动释放。

    同一线程内**可重入**（调度器已持锁时，发布器再取一次不会自锁——否则会等满超时
    并报 `SCHEDULER_LOCK_BUSY`）；不同线程/进程仍按内核锁互斥。
    `stale_seconds` 仅保留签名兼容：操作系统锁不需要过期抢占——进程死亡时内核会
    释放锁，因此不存在「上一个进程留下的死锁」需要超时回收。
    """
    del stale_seconds
    path = _lock_path(lib)
    path.parent.mkdir(parents=True, exist_ok=True)
    depth = _held_depth(path)
    if depth:
        _set_held_depth(path, depth + 1)
        try:
            yield path
        finally:
            _set_held_depth(path, depth)
        return
    handle = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                _os_lock(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SchedulerError(
                        "SCHEDULER_LOCK_BUSY",
                        f"计划锁被占用：{path}（持有者 pid={_read_lock(path).get('pid')}）")
                time.sleep(max(0.001, poll_seconds))
        _set_held_depth(path, 1)
        os.truncate(handle, 0)
        os.write(handle, json.dumps(
            {"pid": os.getpid(), "acquired_at": _now()},
            ensure_ascii=False).encode("utf-8"))
        os.fsync(handle)
        yield path
    finally:
        _set_held_depth(path, 0)
        try:
            _os_unlock(handle)
        except OSError:  # pragma: no cover - 解锁失败的兜底
            pass
        os.close(handle)


def pause_path(lib: Path) -> Path:
    """暂停标记路径（固定 `.process/视觉暂停.json`）。"""
    return Path(lib).joinpath(*PAUSE_REL)


def is_paused(lib: Path) -> bool:
    return pause_path(lib).is_file()


def pause_visual(lib: Path, reason: str = "") -> Path:
    """暂停后续派发（只写标记，已落盘的计划与产物一律不动）。"""
    path = pause_path(lib)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"paused_at": _now(), "reason": reason}, ensure_ascii=False),
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path


def clear_pause(lib: Path) -> bool:
    """解除暂停；返回是否真的删掉了标记。"""
    path = pause_path(lib)
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class SchedulerSummary:
    """一次调度运行的结果摘要（`status --json` 的数据源）。"""

    revision: int = 0
    generated: int = 0
    reused: int = 0
    published: int = 0
    blocked: int = 0
    transport_failed: int = 0
    recovered: int = 0
    waiting_review: int = 0
    waiting_upstream: int = 0
    rounds_exhausted: int = 0
    escalated: int = 0
    transport_retries: int = 0
    parallel_start: int = 0
    parallel_end: int = 0
    review_issues: tuple[str, ...] = ()
    paused: bool = False
    retry_policy: Mapping[str, Any] = field(default_factory=dict)
    plan: VisualPlan = field(default_factory=VisualPlan)
    events: tuple[VisualEvent, ...] = ()

    def to_dict(self) -> dict:
        return {"revision": int(self.revision), "generated": int(self.generated),
                "reused": int(self.reused), "published": int(self.published),
                "blocked": int(self.blocked),
                "transport_failed": int(self.transport_failed),
                "transport_retries": int(self.transport_retries),
                "parallel_start": int(self.parallel_start),
                "parallel_end": int(self.parallel_end),
                "recovered": int(self.recovered),
                "waiting_review": int(self.waiting_review),
                "waiting_upstream": int(self.waiting_upstream),
                "rounds_exhausted": int(self.rounds_exhausted),
                "escalated": int(self.escalated),
                "review_issues": list(self.review_issues),
                "paused": bool(self.paused),
                "retry_policy": dict(self.retry_policy),
                "events": [event.to_dict() for event in self.events]}


def _clamp_parallel(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SchedulerError(
            "SCHEDULER_PARALLEL_INVALID",
            f"并发数必须是 {MIN_PARALLEL}–{MAX_PARALLEL} 的整数：{value!r}") from exc
    if number < MIN_PARALLEL or number > MAX_PARALLEL:
        raise SchedulerError("SCHEDULER_PARALLEL_INVALID",
                             f"并发数 {number} 超出 {MIN_PARALLEL}–{MAX_PARALLEL}")
    return number


class VisualScheduler:
    """单写者协调器：批量调度、并发执行、逐事件提交。"""

    def __init__(self, lib: Path, *, executor: Any = None, request_factory: Any = None,
                 max_parallel: int = DEFAULT_MAX_PARALLEL, plan_path: Path | None = None,
                 provider_factory: Any = None, lock_timeout: float = 10.0,
                 pause_check: Any = None, retry_policy: RetryPolicy | None = None,
                 sleep: Any = None) -> None:
        self.lib = Path(lib)
        self.executor = executor
        self.max_parallel = _clamp_parallel(max_parallel)
        self.plan_path = (Path(plan_path) if plan_path
                          else self.lib / "视觉资产" / "计划.toml")
        self.request_factory = request_factory or self._default_request_factory
        self.provider_factory = provider_factory or (
            lambda plan: FileReviewProvider(self.lib, plan))
        self.lock_timeout = lock_timeout
        self.pause_check = pause_check or (lambda: is_paused(self.lib))
        self.retry_policy = retry_policy or DEFAULT_RETRY_POLICY
        self.sleep = sleep

    # --- 只读查询（供 status / 批量入口判定） ------------------------------------

    def ready_tasks(self, plan: VisualPlan) -> tuple[VisualTask, ...]:
        """当前可派发的任务（依赖已有正式版本、未超轮次）。"""
        released = self._released_ids()
        return tuple(t for t in plan.sorted_tasks() if self._is_ready(plan, t, released))

    def waiting_upstream_tasks(self, plan: VisualPlan) -> tuple[VisualTask, ...]:
        """依赖尚未正式通过、因此暂不派发的任务。"""
        released = self._released_ids()
        return tuple(t for t in plan.sorted_tasks()
                     if t.status in READY_SOURCE_STATUSES
                     and not self._deps_published(plan, t, released))

    def rounds_exhausted_tasks(self, plan: VisualPlan) -> tuple[VisualTask, ...]:
        released = self._released_ids()
        return tuple(t for t in plan.sorted_tasks()
                     if self._rounds_exhausted(plan, t, released))

    # --- 请求编译 ---------------------------------------------------------------

    def _default_request_factory(self, task: VisualTask, plan: VisualPlan) -> Any:
        from .visual_request_chain import compile_for_library

        return compile_for_library(task, self.lib, plan).request

    # --- 就绪判定 ---------------------------------------------------------------

    def _released_ids(self) -> set[str]:
        """已有正式版本（`current`）的任务集合。

        上游是否算「通过」以**正式版本清单**为准，而不是只看计划里的状态字段：
        规格 15.5 要求未发布的图不得成为下游参考，手工改状态不能绕过。
        清单损坏时**显式失败**而不是当作「什么都没发布」——否则整条依赖链会被静默停住。
        """
        try:
            manifest = load_release_manifest(self.lib)
        except (OSError, ValueError) as exc:
            raise SchedulerError("RELEASE_MANIFEST_UNREADABLE",
                                 f"正式版本清单不可读：{type(exc).__name__}") from exc
        if isinstance(manifest, dict) and manifest.get("unreadable"):
            raise SchedulerError(
                "RELEASE_MANIFEST_UNREADABLE",
                f"正式版本清单不可读：{self.lib / '视觉资产' / '正式版本.json'}")
        tasks = manifest.get("tasks", {}) if isinstance(manifest, dict) else {}
        return {tid for tid, entry in tasks.items()
                if isinstance(entry, dict) and isinstance(entry.get("current"), dict)}

    def _deps_published(self, plan: VisualPlan, task: VisualTask,
                        released: set[str] | None = None) -> bool:
        known = self._released_ids() if released is None else released
        for dep in task.depends_on:
            upstream = plan.by_id(dep)
            if upstream is None:
                return False
            if upstream.status != S.READY_AS_REFERENCE.value or dep not in known:
                return False
        return True

    def _is_ready(self, plan: VisualPlan, task: VisualTask,
                  released: set[str] | None = None) -> bool:
        if task.status not in READY_SOURCE_STATUSES:
            return False
        if task.round_count >= int(task.max_rounds):
            return False
        return self._deps_published(plan, task, released)

    def _rounds_exhausted(self, plan: VisualPlan, task: VisualTask,
                          released: set[str] | None = None) -> bool:
        """轮次用尽：已用满上限**且最新一轮已被审核否决**。

        还在等待审核的最后一轮不算用尽——否则第 3 轮的图刚生成就会被提前判成
        「需要人工决定」，等于凭空丢掉一次已花掉额度的审核机会。
        """
        return (task.status == S.NEEDS_REVISION.value
                and task.round_count >= int(task.max_rounds)
                and self._deps_published(plan, task, released))

    def _reusable(self, plan: VisualPlan, task: VisualTask) -> bool:
        """已通过且全部输入哈希未变 → 直接复用（不重新生成、不重新审核）。"""
        if task.status not in REUSABLE_STATUSES or task.latest_attempt is None:
            return False
        record = current_release(self.lib, task.task_id)
        if record is None:
            return False
        if task.status == S.ACCEPTED.value:
            return True
        try:
            live = build_review_request(self.lib, plan, task)
        except (ReviewContractError, OSError, ValueError):
            return False
        return dict(record.binding or {}) == live.binding.as_dict()

    # --- 事件构造 ---------------------------------------------------------------

    def _blocked_event(self, task: VisualTask, status: str, code: str, detail: str = "",
                       revision: int = 0) -> VisualEvent:
        return VisualEvent.create(TASK_BLOCKED, task.task_id, revision=revision,
                                  payload={"status": status, "code": code,
                                           "detail": detail})

    # --- 工作器：只返回事件，不写计划 -------------------------------------------

    def _work(self, plan: VisualPlan, task: VisualTask,
              executor: Any = None) -> tuple[VisualEvent, ...]:
        """在工作者线程里跑一个任务，返回不可变事件序列（绝不写计划）。"""
        revision = int(plan.revision)
        round_no = task.round_count + 1
        runner = self.executor if executor is None else executor
        try:
            request = self.request_factory(task, plan)
        except (VisualCompileError, ValueError, OSError) as exc:
            code = str(getattr(exc, "code", "") or COMPILE_FAILED)
            return (self._blocked_event(task, S.BLOCKED.value, code, str(exc), revision),)
        try:
            updated = run_task(task, runner, lib=self.lib, plan=plan,
                               request=request)
        except GenerationError as exc:
            if exc.code == GENERATION_REJECTED:
                return (self._blocked_event(task, S.BLOCKED.value, exc.code, exc.detail,
                                            revision),)
            return (VisualEvent.create(
                TRANSPORT_FAILED, task.task_id, revision=revision,
                payload={"round": round_no, "code": exc.code, "detail": exc.detail}),)
        except (StoreError, ValueError) as exc:
            code = str(getattr(exc, "code", "") or "GENERATION_FAILED")
            return (self._blocked_event(task, S.BLOCKED.value, code, str(exc), revision),)

        if updated.status == S.AWAITING_MANUAL_RESULT.value:
            return (self._blocked_event(task, S.AWAITING_MANUAL_RESULT.value,
                                        "AWAITING_MANUAL_RESULT", "", revision),)
        attempt = updated.latest_attempt
        if attempt is None:
            return (self._blocked_event(task, S.BLOCKED.value, "IMAGE_MISSING",
                                        "生成后无轮次记录", revision),)
        events: list[VisualEvent] = [VisualEvent.create(
            IMAGE_STORED, task.task_id, revision=revision,
            payload={"round": int(attempt.round), "image": attempt.image,
                     "request": attempt.request, "response": attempt.response,
                     "qc": attempt.qc, "image_sha256": attempt.image_sha256,
                     "model": attempt.model, "provider": attempt.provider,
                     "response_id": attempt.response_id})]
        local_plan = plan.with_task(updated)
        events.extend(self._export_request(local_plan, updated, revision))
        # 这里不接收 issues 列表（工作器线程不该写协调器的汇总）：生成阶段若已有
        # 审核结果文件但因绑定不符无法导入，会由本轮的 `_collect_reviews` 复核并上报。
        events.extend(self._review_events_of(local_plan, updated, revision, []))
        return tuple(events)

    def _export_request(self, plan: VisualPlan, task: VisualTask,
                        revision: int) -> tuple[VisualEvent, ...]:
        """导出结构化审核请求，供当前主模型读图后回写结果。"""
        provider = self.provider_factory(plan)
        try:
            path = provider.prepare(task)
        except (ReviewContractError, OSError, ValueError) as exc:
            code = str(getattr(exc, "code", "REVIEW_PREPARE_FAILED"))
            return (self._blocked_event(task, S.BLOCKED.value, code, str(exc), revision),)
        relative = str(path)
        try:
            relative = path.relative_to(self.lib).as_posix()
        except ValueError:
            pass
        return (VisualEvent.create(REVIEW_REQUESTED, task.task_id, revision=revision,
                                   payload={"request": normalize_rel_path(relative)}),)

    def _repeated_issues(self, task: VisualTask, issues: tuple[str, ...]) -> bool:
        """相同问题连续两轮无改善（规格十）：按事件日志里的上一轮判定比对问题码集合。"""
        codes = {str(code) for code in issues if str(code)}
        if not codes or task.latest_attempt is None:
            return False
        history = [event for event in read_events(self.lib)
                   if event.kind == REVIEW_IMPORTED and event.task_id == task.task_id]
        if not history:
            return False
        previous = history[-1].payload
        try:
            previous_round = int(previous.get("round", 0) or 0)
        except (TypeError, ValueError):
            return False
        current_round = int(task.latest_attempt.round)
        if previous_round != current_round - 1:
            return False  # 不是相邻两轮，不算「连续」
        previous_codes = {str(code) for code in (previous.get("issues") or [])}
        return bool(previous_codes) and previous_codes == codes

    def _review_events_of(self, plan: VisualPlan, task: VisualTask, revision: int,
                          issues: list[str]) -> tuple[VisualEvent, ...]:
        """导入已回写的审核结果；通过者（上游已发布时）立刻成为正式版本。

        结果无法读取或与当前输入不符时**不改状态、不发布**，但把稳定原因码记入
        `issues`（由 `SchedulerSummary` 上报），绝不让故障消失在空返回里。
        """
        if task.latest_attempt is None:
            return ()
        if task.status in PENDING_REVIEW_STATUSES:
            if not result_path(self.lib, task.task_id).is_file():
                return ()
            try:
                result = self.provider_factory(plan).collect(task)
            except (ReviewContractError, OSError, ValueError) as exc:
                issues.append(f"{task.task_id}: {getattr(exc, 'code', '') or type(exc).__name__}")
                return ()
            if result is None:
                return ()
            decision = str(result.decision)
            codes = tuple(str(row.get("code", ""))
                          for row in (getattr(result, "issues", ()) or ())
                          if isinstance(row, dict) and str(row.get("code", "")))
            payload: dict[str, Any] = {"round": int(task.latest_attempt.round),
                                       "decision": decision, "issues": list(codes),
                                       "reviewer": result.reviewer,
                                       "reviewed_at": result.reviewed_at}
            if decision == "NEEDS_REVISION" and self._repeated_issues(task, codes):
                # 连续两轮同样问题：不再消耗画面轮次，交人工决定
                payload["decision"] = "NEEDS_HUMAN_DECISION"
                payload["reason"] = REVIEW_NO_IMPROVEMENT
                issues.append(f"{task.task_id}: {REVIEW_NO_IMPROVEMENT}")
                events: tuple[VisualEvent, ...] = (VisualEvent.create(
                    REVIEW_IMPORTED, task.task_id, revision=revision,
                    payload=payload),)
                result = replace(result, decision="NEEDS_HUMAN_DECISION")
                return events
            events = (VisualEvent.create(REVIEW_IMPORTED, task.task_id, revision=revision,
                                         payload=payload),)
        elif task.status == S.ACCEPTED.value:
            try:
                result = self.provider_factory(plan).collect(task)
            except (ReviewContractError, OSError, ValueError) as exc:
                issues.append(f"{task.task_id}: {getattr(exc, 'code', '') or type(exc).__name__}")
                return ()
            if result is None:
                return ()
            events = ()
        else:
            return ()
        if not result.is_accepted:
            return events
        try:
            request = build_review_request(self.lib, plan, task)
            _, record = publish_task(self.lib, plan, task, request, result)
        except PublishError as exc:
            if exc.code == "PUBLISH_UPSTREAM_INVALID":
                return events  # 上游尚未发布：保持已通过，等下一轮
            # 已通过却发不出去：交人工决定，且必须是**有效**的状态推进
            issues.append(f"{task.task_id}: {exc.code}")
            repaired = self._blocked_event(task, S.NEEDS_HUMAN_DECISION.value, exc.code,
                                           exc.detail, revision)
            return tuple(events) + (repaired,)
        return tuple(events) + (VisualEvent.create(
            PUBLISHED, task.task_id, revision=revision,
            payload={"version": record.version, "image": record.image,
                     "decision": record.decision}),)

    # --- 协调器：唯一的计划写者 -------------------------------------------------

    def _commit(self, plan: VisualPlan, events: Sequence[VisualEvent]) -> VisualPlan:
        """持锁提交：写前日志 → 应用事件 → revision+1 → 原子写计划。"""
        if not events:
            return plan
        with plan_lock(self.lib, timeout=self.lock_timeout):
            base = plan
            if self.plan_path.is_file():
                latest = visual_schema.load_visual_plan(self.plan_path)
                if int(latest.revision) != int(plan.revision):
                    # 磁盘版本更新：事件重新应用到最新版本，绝不整份覆盖
                    base = latest
            append_events(self.lib, events)  # 写前日志：先落事件，再落计划
            updated = apply_events(base, events)
            updated = replace(updated, revision=int(base.revision) + 1,
                              updated_at=_now())
            visual_schema.write_visual_plan(updated, self.plan_path)
        return updated

    def _checkpoint(self, plan: VisualPlan) -> VisualPlan:
        """把事件重放得到的恢复结果落盘（仅在确有恢复变化时调用）。"""
        with plan_lock(self.lib, timeout=self.lock_timeout):
            base = plan
            if self.plan_path.is_file():
                latest = visual_schema.load_visual_plan(self.plan_path)
                if int(latest.revision) != int(plan.revision):
                    base = apply_events(latest, read_events(self.lib))
            updated = replace(base, revision=int(base.revision) + 1, updated_at=_now())
            visual_schema.write_visual_plan(updated, self.plan_path)
        return updated

    def _replay(self, plan: VisualPlan) -> tuple[VisualPlan, int]:
        """重放事件日志：补齐「事件已落盘、计划未写入」的中间状态。"""
        events = read_events(self.lib)
        if not events:
            return plan, 0
        before = {t.task_id: (t.status, len(t.attempts), t.current_version)
                  for t in plan.tasks}
        replayed = apply_events(plan, events)
        recovered = sum(
            1 for task in replayed.tasks
            if before.get(task.task_id) != (task.status, len(task.attempts),
                                            task.current_version))
        return replayed, recovered

    # --- 对外入口 ---------------------------------------------------------------

    def run_ready(self, plan: VisualPlan, max_parallel: int | None = None) -> SchedulerSummary:
        """按依赖图批量执行当前可执行任务。"""
        parallel = (self.max_parallel if max_parallel is None
                    else _clamp_parallel(max_parallel))
        plan, recovered = self._replay(plan)
        if recovered:
            plan = self._checkpoint(plan)
        produced: list[VisualEvent] = []
        counters = {"generated": 0, "published": 0, "blocked": 0, "transport_failed": 0,
                    "rounds_exhausted": 0, "escalated": 0}
        attempted: set[str] = set()
        review_issues: list[str] = []
        paused = False
        governor = ConcurrencyGovernor(ceiling=parallel)
        runner = self._retrying_executor()
        reused = sum(1 for task in plan.sorted_tasks() if self._reusable(plan, task))
        while True:
            collected = self._collect_reviews(plan, review_issues)
            if collected:
                plan = self._commit(plan, collected)
                produced.extend(collected)
                self._tally(collected, counters, governor)
            released = self._released_ids()
            saturated = [t for t in plan.sorted_tasks()
                         if t.task_id not in attempted
                         and self._rounds_exhausted(plan, t, released)]
            if saturated:
                events = tuple(
                    self._blocked_event(t, S.NEEDS_HUMAN_DECISION.value, ROUNDS_EXHAUSTED,
                                        f"已达最大画面轮次 {t.max_rounds}")
                    for t in saturated)
                for task in saturated:
                    attempted.add(task.task_id)
                plan = self._commit(plan, events)
                produced.extend(events)
                counters["rounds_exhausted"] += len(events)
                continue
            ready = [t for t in plan.sorted_tasks()
                     if t.task_id not in attempted and self._is_ready(plan, t, released)]
            if not ready:
                break
            if self.pause_check():
                paused = True  # 暂停：不再派发新任务，已落盘状态原样保留
                break
            batch = ready[:min(len(ready), governor.current)]
            for task in batch:
                attempted.add(task.task_id)
            started = tuple(
                VisualEvent.create(GENERATION_STARTED, t.task_id,
                                   revision=int(plan.revision),
                                   payload={"round": t.round_count + 1,
                                            "prompt_sha256": sha256_text(t.prompt)})
                for t in batch)
            plan = self._commit(plan, started)  # 派发前先记录（写前日志）
            produced.extend(started)
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                groups = [future.result()
                          for future in [pool.submit(self._work, plan, t, runner)
                                         for t in batch]]
            events = tuple(event for group in groups for event in group)
            plan = self._commit(plan, events)
            produced.extend(events)
            self._tally(events, counters, governor)
        waiting = sum(1 for t in plan.tasks if t.status == S.WAITING_REVIEW.value)
        released = self._released_ids()
        waiting_upstream = sum(
            1 for t in plan.tasks
            if t.status in READY_SOURCE_STATUSES
            and not self._deps_published(plan, t, released))
        return SchedulerSummary(revision=int(plan.revision), reused=reused,
                                waiting_review=waiting,
                                waiting_upstream=waiting_upstream, recovered=recovered,
                                review_issues=tuple(review_issues), paused=paused,
                                transport_retries=int(getattr(runner, "retries", 0)),
                                parallel_start=int(parallel),
                                parallel_end=int(governor.current),
                                retry_policy=self.retry_policy.to_dict(),
                                plan=plan, events=tuple(produced), **counters)

    def _tally(self, events: Sequence[VisualEvent], counters: dict[str, int],
               governor: Any = None) -> None:
        """按事件累计口径（只在提交成功后调用，口径与 `to_dict()` 一一对应）。"""
        for event in events:
            if event.kind == IMAGE_STORED:
                counters["generated"] += 1
                if governor is not None:
                    governor.on_success()
            elif event.kind == PUBLISHED:
                counters["published"] += 1
            elif event.kind == TRANSPORT_FAILED:
                counters["transport_failed"] += 1
                if (governor is not None
                        and str(event.payload.get("code", "")) == RATE_LIMITED):
                    governor.on_rate_limited()  # 连续限流：降并发，不占轮次
            elif event.kind == TASK_BLOCKED:
                counters["blocked"] += 1
            elif event.kind == REVIEW_IMPORTED:
                if event.payload.get("reason") == REVIEW_NO_IMPROVEMENT:
                    counters["escalated"] += 1

    def _retrying_executor(self) -> Any:
        """给执行器套上传输重试；没有执行器时返回 None（纯恢复/发布运行不触网）。"""
        if self.executor is None:
            return None
        if isinstance(self.executor, RetryingExecutor):
            return self.executor
        kwargs: dict[str, Any] = {"policy": self.retry_policy}
        if self.sleep is not None:
            kwargs["sleep"] = self.sleep
        return RetryingExecutor(self.executor, **kwargs)

    def _collect_reviews(self, plan: VisualPlan,
                         issues: list[str]) -> tuple[VisualEvent, ...]:
        """吸收已回写的审核结果，并尝试发布已通过但上游刚就绪的任务。"""
        events: list[VisualEvent] = []
        for task in plan.sorted_tasks():
            if task.status not in (*PENDING_REVIEW_STATUSES, S.ACCEPTED.value):
                continue
            events.extend(self._review_events_of(plan, task, int(plan.revision), issues))
        return tuple(events)

    def resume(self) -> SchedulerSummary:
        """从磁盘计划恢复：先重放事件日志补齐状态，再继续调度剩余任务。"""
        if not self.plan_path.is_file():
            raise SchedulerError("SCHEDULER_PLAN_MISSING",
                                 f"没有可恢复的视觉计划：{self.plan_path}")
        return self.run_ready(visual_schema.load_visual_plan(self.plan_path))
