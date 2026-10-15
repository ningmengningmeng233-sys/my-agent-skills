#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉事件日志（阶段 5 工单 5.1，规格 15.4）。

- 事件是**不可变事实**：并发工作器只产生事件，绝不直接改计划；协调器是唯一把事件
  写进计划的对象。
- 事件日志（`.process/视觉事件.jsonl`）先于计划落盘，是**写前日志**：进程在
  「图片已保存、计划未写入」之间中断时，恢复靠重放事件补齐状态，不重复请求图片。
- 同一 `event_id` 幂等：event_id 由「事件类型 + 任务 + 负载摘要」确定性推导，
  同一事实重放多少次都只落盘一次、只应用一次。
- 异常退出留下的半行 JSON 一律忽略：只有完整行才是事实。

事件负载只携带**可重建状态的最小字段**（轮次、路径、哈希、判定、版本号），
因此 `apply_event` 是纯函数，可在恢复时对任意计划增量重放。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .visual_pipeline import LEGAL_TRANSITIONS, InvalidVisualTransition, transition
from .visual_schema import AttemptRecord, VisualPlan, VisualTask, VisualTaskStatus

GENERATION_STARTED = "GENERATION_STARTED"
TRANSPORT_FAILED = "TRANSPORT_FAILED"
IMAGE_STORED = "IMAGE_STORED"
REVIEW_REQUESTED = "REVIEW_REQUESTED"
REVIEW_IMPORTED = "REVIEW_IMPORTED"
PUBLISHED = "PUBLISHED"
TASK_BLOCKED = "TASK_BLOCKED"

EVENT_KINDS: tuple[str, ...] = (GENERATION_STARTED, TRANSPORT_FAILED, IMAGE_STORED,
                                REVIEW_REQUESTED, REVIEW_IMPORTED, PUBLISHED,
                                TASK_BLOCKED)

# 只审计、不改任务状态的事件（崩溃恢复时重放它们是空操作）
AUDIT_ONLY_KINDS: tuple[str, ...] = (GENERATION_STARTED, TRANSPORT_FAILED, REVIEW_REQUESTED)

EVENT_LOG_REL = (".process", "视觉事件.jsonl")

# WAL 有界阈值：活动事件日志超过该字节数即触发轮转（快照固化 + gzip 压缩旧段），
# 活动文件保持 O(1) 追加且不再无限膨胀；单测可用小阈值覆盖。
EVENT_LOG_ROTATE_BYTES = 512 * 1024
# 轮转归档命名：`视觉事件轮转-<UTC紧凑时间戳>-<纳秒>-<pid>.jsonl.gz`，按文件名排序即时间序。
EVENT_LOG_ARCHIVE_PREFIX = "视觉事件轮转-"
EVENT_LOG_ARCHIVE_SUFFIX = ".jsonl.gz"

# 追加临界区：保护「增量追尾 dedup → 纯追加 → 轮转」全链路（同进程线程安全）。
_APPEND_LOCK = threading.Lock()
# lib 绝对路径 → {"seen": set[event_id], "size": 已扫描字节数}：增量追尾状态。
_SEEN_BY_LIB: dict[str, dict[str, Any]] = {}
# 增量追尾只提取 event_id（正则扫描，不做全量 JSON 解析，更不调用 read_events）。
_EVENT_ID_RE = re.compile(r'"event_id"\s*:\s*"([^"]+)"')

S = VisualTaskStatus

_DECISION_STATUS = {
    "ACCEPTED": S.ACCEPTED.value,
    "PASS_WITH_NOTES": S.ACCEPTED.value,
    "NEEDS_REVISION": S.NEEDS_REVISION.value,
    "NEEDS_HUMAN_DECISION": S.NEEDS_HUMAN_DECISION.value,
}

# 「已越过该阶段」的状态：重放旧事件时不得把已发布资产退回审核阶段
_PAST_ACCEPTED = (S.READY_AS_REFERENCE.value,)
_PAST_BLOCKED = (S.ACCEPTED.value, S.READY_AS_REFERENCE.value)

# 正式版本号形如 `attempt-03`，可反解出轮次
_ATTEMPT_VERSION_RE = re.compile(r"attempt-(\d+)$")


class EventError(ValueError):
    """事件非法：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def event_log_path(lib: Path) -> Path:
    """事件日志路径（固定 `.process/视觉事件.jsonl`）。"""
    return Path(lib).joinpath(*EVENT_LOG_REL)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


@dataclass(frozen=True)
class VisualEvent:
    """一条不可变事件。`revision` 是产出它的计划版本号（协调器据此校验旧事件）。"""

    event_id: str = ""
    kind: str = ""
    task_id: str = ""
    revision: int = 0
    at: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, kind: str, task_id: str, *, revision: int = 0, at: str = "",
               payload: Mapping[str, Any] | None = None) -> "VisualEvent":
        """构造事件并推导确定性 event_id（不含时间戳，保证重放幂等）。"""
        if kind not in EVENT_KINDS:
            raise EventError("EVENT_KIND_UNKNOWN", f"未知事件类型：{kind!r}")
        body = dict(payload or {})
        digest = hashlib.sha256(
            f"{kind}|{task_id}|{_canonical(body)}".encode("utf-8")).hexdigest()
        return cls(event_id=f"{kind}:{task_id}:{digest[:16]}", kind=kind,
                   task_id=str(task_id), revision=int(revision), at=at or _now(),
                   payload=body)

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "kind": self.kind, "task_id": self.task_id,
                "revision": int(self.revision), "at": self.at, "payload": dict(self.payload)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualEvent":
        payload = data.get("payload")
        return cls(event_id=str(data.get("event_id", "")), kind=str(data.get("kind", "")),
                   task_id=str(data.get("task_id", "")),
                   revision=int(data.get("revision", 0) or 0),
                   at=str(data.get("at", "")),
                   payload=dict(payload) if isinstance(payload, Mapping) else {})


def _parse_lines(text: str) -> list[VisualEvent]:
    """解析完整行；半行/损坏行只忽略该行，不抛出。"""
    events: list[VisualEvent] = []
    for line in text.splitlines():
        body = line.strip()
        if not body:
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue  # 异常退出留下的半行：不是事实
        if isinstance(data, Mapping) and data.get("kind"):
            events.append(VisualEvent.from_dict(data))
    return events


def _archive_segments(lib: Path) -> list[Path]:
    """按时间序返回轮转归档段（文件名排序即时间序；无归档返回空）。"""
    parent = event_log_path(lib).parent
    if not parent.is_dir():
        return []
    pattern = f"{EVENT_LOG_ARCHIVE_PREFIX}*{EVENT_LOG_ARCHIVE_SUFFIX}"
    return sorted(parent.glob(pattern), key=lambda p: p.name)


def read_events(lib: Path) -> tuple[VisualEvent, ...]:
    """读取已完整落盘的事件（轮转归档段 + 活动日志，按时间序）；半行/损坏行只忽略该行。"""
    events: list[VisualEvent] = []
    for segment in _archive_segments(lib):
        try:
            with gzip.open(segment, "rt", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue  # 归档段损坏不阻断恢复：跳过该段
        events.extend(_parse_lines(text))
    path = event_log_path(lib)
    if path.is_file():
        events.extend(_parse_lines(path.read_text(encoding="utf-8")))
    return tuple(events)


def _lib_key(lib: Path) -> str:
    try:
        return str(Path(lib).resolve())
    except OSError:
        return str(Path(lib).absolute())


def _refresh_seen_locked(path: Path, state: dict[str, Any]) -> None:
    """增量追尾刷新 event_id 集合（只读上次之后的新字节；调用方须持有 _APPEND_LOCK）。

    从不调用 read_events：只做正则 event_id 提取，不做全量 JSON 解析。
    末尾残缺行（无换行结尾）不计入，避免把崩溃半行误判为“已落盘事实”。
    """
    try:
        size = path.stat().st_size
    except OSError:
        state["seen"] = set()
        state["size"] = 0
        return
    known = int(state.get("size", 0) or 0)
    if size == known:
        return
    if size < known:
        # 文件被轮转/截断：全量重扫活动文件（低频操作，均摊可忽略）
        try:
            raw = path.read_bytes()
        except OSError:
            state["seen"] = set()
            state["size"] = 0
            return
        text = raw.decode("utf-8", errors="replace")
        cut = text.rfind("\n") + 1
        state["seen"] = set(_EVENT_ID_RE.findall(text[:cut]))
        # 已扫描偏移停在最后完整行边界：残缺尾部留待下次。
        state["size"] = len(raw) - len(text[cut:].encode("utf-8", errors="replace"))
        return
    try:
        with open(path, "rb") as handle:
            handle.seek(known)
            delta = handle.read()
    except OSError:
        return
    text = delta.decode("utf-8", errors="replace")
    if not text.endswith("\n"):
        # 末尾残缺行留待下次（等它写完整）；本次只扫描完整行
        cut = text.rfind("\n") + 1
        text = text[:cut]
    if text:
        state["seen"].update(_EVENT_ID_RE.findall(text))
        state["size"] = known + len(text.encode("utf-8", errors="replace"))


def _maybe_rotate_locked(path: Path, state: dict[str, Any]) -> None:
    """活动日志超阈值时轮转：快照 gzip 固化旧段，活动文件只留轮转期间追尾的新行。

    调用方须持有 _APPEND_LOCK（同进程线程安全）；跨进程轮转竞态下归档名唯一
    （时间戳 + 纳秒 + pid），最坏只是段顺序，不丢事实。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= EVENT_LOG_ROTATE_BYTES:
        return
    try:
        data = path.read_bytes()
    except OSError:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    archive = path.parent / (f"{EVENT_LOG_ARCHIVE_PREFIX}{stamp}-{time.time_ns()}-"
                             f"{os.getpid()}{EVENT_LOG_ARCHIVE_SUFFIX}")
    tmp = archive.with_name(archive.name + ".tmp")
    try:
        with open(tmp, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
                gz.write(data)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, archive)
        current = path.read_bytes()
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return
    tail = current[len(data):] if len(current) > len(data) else b""
    active_tmp = path.with_name(path.name + ".tmp")
    try:
        with open(active_tmp, "wb") as raw:
            raw.write(tail)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(active_tmp, path)
    except OSError:
        try:
            active_tmp.unlink()
        except OSError:
            pass
        return
    state["size"] = len(tail)  # seen 集合保持有效：归档过的 id 仍视为已落盘


def append_events(lib: Path, events: Sequence[VisualEvent]) -> int:
    """追加事件（同一 event_id 幂等）；返回真正新增的条数。落盘即 fsync（写前日志）。

    纯追加流式写入：只以 `a` 模式追加新事件行，从不调用全量 read_events；
    幂等靠批内去重 + 增量追尾的 event_id 集合保证，单次追加 O(1)。
    """
    batch = list(events)
    if not batch:
        return 0
    deduped: list[VisualEvent] = []
    batch_seen: set[str] = set()
    for event in batch:
        if event.event_id in batch_seen:
            continue
        batch_seen.add(event.event_id)
        deduped.append(event)
    key = _lib_key(lib)
    path = event_log_path(lib)
    with _APPEND_LOCK:
        state = _SEEN_BY_LIB.get(key)
        if state is None:
            state = {"seen": set(), "size": 0}
            _SEEN_BY_LIB[key] = state
        _refresh_seen_locked(path, state)
        seen: set[str] = state["seen"]
        fresh = [event for event in deduped if event.event_id not in seen]
        if not fresh:
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            for event in fresh:
                handle.write(event.to_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        for event in fresh:
            seen.add(event.event_id)
        try:
            state["size"] = path.stat().st_size
        except OSError:
            pass
        _maybe_rotate_locked(path, state)
    return len(fresh)


def _route(task: VisualTask, path: Sequence[str]) -> VisualTask:
    for step in path:
        if task.status == step:
            continue
        task = transition(task, step)
    return task


def _find_route(current: str, target: str) -> tuple[str, ...] | None:
    """在状态机白名单上做 BFS：返回最短合法路径（None = 不可达）。

    路径由 `LEGAL_TRANSITIONS` 现算，不另立路由表，故不可能与状态机脱节。
    """
    if current == target:
        return ()
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(current, ())])
    seen = {current}
    while queue:
        node, path = queue.popleft()
        for nxt in sorted(LEGAL_TRANSITIONS.get(node, ())):
            if nxt in seen:
                continue
            step = path + (nxt,)
            if nxt == target:
                return step
            seen.add(nxt)
            queue.append((nxt, step))
    return None


def _advance(task: VisualTask, target: str,
             tolerate: Sequence[str] = ()) -> VisualTask | None:
    """把任务推进到 target；已在 target 或已越过该阶段（tolerate）时返回 None（空操作）。"""
    if task.status == target or task.status in tolerate:
        return None
    route = _find_route(task.status, target)
    if route is None:
        raise EventError("EVENT_ILLEGAL_TRANSITION",
                         f"{task.task_id}：{task.status} 无法合法到达 {target}")
    return _route(task, route)


def _latest_round(task: VisualTask) -> int:
    return int(task.latest_attempt.round) if task.latest_attempt else 0


def _is_stale_round(task: VisualTask, round_no: int) -> bool:
    """事件记录的轮次与任务当前最新轮次不一致 → 陈旧事实，重放时必须空操作。

    没有轮次信息的旧格式事件只允许作用于单轮任务：否则旧的「需要返工」判定
    会在恢复时把已经生成的新轮次打回去，导致重复消耗画面轮次。
    """
    if round_no <= 0:
        return task.round_count > 1
    return round_no != _latest_round(task)


def _require_task(plan: VisualPlan, task_id: str) -> VisualTask:
    task = plan.by_id(task_id)
    if task is None:
        raise EventError("EVENT_TASK_UNKNOWN", f"事件指向计划外的任务：{task_id}")
    return task


def _apply_stored(task: VisualTask, payload: Mapping[str, Any]) -> VisualTask:
    round_no = int(payload.get("round", 0) or 0)
    if any(int(a.round) == round_no for a in task.attempts):
        return task  # 同一轮次已登记：重放是空操作
    record = AttemptRecord(
        round=round_no, image=str(payload.get("image", "")),
        request=str(payload.get("request", "")), response=str(payload.get("response", "")),
        qc=str(payload.get("qc", "")), image_sha256=str(payload.get("image_sha256", "")),
        model=str(payload.get("model", "")), provider=str(payload.get("provider", "")),
        response_id=str(payload.get("response_id", "")))
    task = replace(task, attempts=tuple(sorted((*task.attempts, record),
                                               key=lambda a: int(a.round))))
    advanced = _advance(task, S.WAITING_REVIEW.value)
    return task if advanced is None else advanced


def apply_event(plan: VisualPlan, event: VisualEvent) -> VisualPlan:
    """把一条事件应用到计划（纯函数、幂等；未知任务显式失败）。"""
    if event.kind in AUDIT_ONLY_KINDS:
        return plan  # 只审计，不改状态（重放是空操作）
    task = _require_task(plan, event.task_id)
    payload = event.payload or {}

    if event.kind == IMAGE_STORED:
        return plan.with_task(_apply_stored(task, payload))
    if event.kind == TASK_BLOCKED:
        target = str(payload.get("status", "") or S.BLOCKED.value)
        advanced = _advance(task, target,
                            _PAST_BLOCKED if target == S.BLOCKED.value else ())
        return plan if advanced is None else plan.with_task(advanced)
    if event.kind == REVIEW_IMPORTED:
        if _is_stale_round(task, int(payload.get("round", 0) or 0)):
            return plan  # 陈旧判定：不得作用于更新的轮次
        target = _DECISION_STATUS.get(str(payload.get("decision", "")))
        if target is None:
            raise EventError("EVENT_DECISION_UNKNOWN",
                             f"未知审核判定：{payload.get('decision')!r}")
        advanced = _advance(task, target,
                            _PAST_ACCEPTED if target == S.ACCEPTED.value else ())
        return plan if advanced is None else plan.with_task(advanced)
    if event.kind == PUBLISHED:
        version = str(payload.get("version", ""))
        matched = _ATTEMPT_VERSION_RE.search(version)
        if matched and _is_stale_round(task, int(matched.group(1))):
            return plan  # 陈旧发布：不得把更新的轮次标成旧版本
        if task.status == S.READY_AS_REFERENCE.value and task.current_version == version:
            return plan
        advanced = _advance(task, S.READY_AS_REFERENCE.value)
        base = task if advanced is None else advanced
        return plan.with_task(replace(base, current_version=version
                                      or task.current_version))
    raise EventError("EVENT_KIND_UNKNOWN", f"未知事件类型：{event.kind!r}")


def apply_events(plan: VisualPlan, events: Sequence[VisualEvent]) -> VisualPlan:
    """按顺序重放事件；同一条事件重复出现是空操作。"""
    for event in events:
        try:
            plan = apply_event(plan, event)
        except InvalidVisualTransition as exc:
            raise EventError("EVENT_ILLEGAL_TRANSITION", str(exc)) from exc
    return plan
