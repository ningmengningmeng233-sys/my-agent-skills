#!/usr/bin/env python3
"""批量的控制面：逐条状态、认证暂停恢复、僵尸任务回收。

为什么是 JSON 而不是数据库表：B站 的批量原本就是「台账文件 + 串行循环」，
`processed_videos.txt` 记录「做完哪些」。这里补的是「每条现在处于什么状态」，
用于回答三个台账答不了的问题：Cookie 失效该不该继续、进程被杀后哪条要重跑、
登录之后从哪继续。轻量状态文件足够，且不必动生产库的 schema。

状态枚举与抖音版同名，便于两个仓库日后共用诊断与维护逻辑。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# 状态机
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
AUTH_REQUIRED = "auth_required"
RETRY_WAIT = "retry_wait"

BATCH_RUNNING = "running"
BATCH_PAUSED_AUTH = "paused_auth"
BATCH_PAUSED_RISK = "paused_risk"
BATCH_COMPLETED = "completed"

_PAUSED = (BATCH_PAUSED_AUTH, BATCH_PAUSED_RISK)
_CLAIMABLE = (PENDING, RETRY_WAIT)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class BatchState:
    """一个批次的逐条状态；每次变更即落盘，保证进程被杀也不丢进度。"""

    def __init__(self, path: Path, payload: dict):
        self.path = Path(path)
        self.payload = payload

    # ---- 构造与持久化 -------------------------------------------------

    @classmethod
    def create(cls, *, path: Path, batch_id: str, items: list[dict]) -> "BatchState":
        payload = {
            "batch_id": str(batch_id),
            "status": BATCH_RUNNING,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "items": [
                {**item, "status": PENDING, "attempts": 0,
                 "failure_code": "", "heartbeat_at": ""}
                for item in items
            ],
        }
        state = cls(path, payload)
        state.save()
        return state

    @classmethod
    def load(cls, path: Path) -> "BatchState | None":
        """读取状态文件；损坏时隔离为 `.corrupt` 并返回 None，绝不抛穿批量。"""
        path = Path(path)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or "items" not in payload:
                raise ValueError("结构不符")
        except (ValueError, OSError, UnicodeDecodeError):
            quarantine = path.with_suffix(path.suffix + ".corrupt")
            try:
                path.replace(quarantine)
            except OSError:
                pass
            return None
        return cls(path, payload)

    def save(self) -> None:
        self.payload["updated_at"] = utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- 读取 ---------------------------------------------------------

    @property
    def batch_id(self) -> str:
        return str(self.payload.get("batch_id", ""))

    @property
    def status(self) -> str:
        return str(self.payload.get("status", BATCH_RUNNING))

    @status.setter
    def status(self, value: str) -> None:
        self.payload["status"] = str(value)

    @property
    def is_paused(self) -> bool:
        return self.status in _PAUSED

    @property
    def pause_reason(self) -> str:
        return str(self.payload.get("pause_reason", ""))

    @property
    def paused_at(self) -> str:
        return str(self.payload.get("paused_at", ""))

    @property
    def items(self) -> list[dict]:
        return self.payload.setdefault("items", [])

    def get(self, key: str) -> dict | None:
        return next((i for i in self.items if i.get("key") == key), None)

    def next_pending(self) -> dict | None:
        """下一个可领取条目；暂停（认证或风控）时返回 None（停止领取）。"""
        if self.is_paused:
            return None
        return next((i for i in self.items if i.get("status") in _CLAIMABLE),
                    None)

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[str(item.get("status"))] = counts.get(
                str(item.get("status")), 0) + 1
        return counts

    # ---- 变更 ---------------------------------------------------------

    def claim(self, key: str) -> None:
        item = self.get(key)
        if item is None:
            return
        item["status"] = RUNNING
        item["attempts"] = int(item.get("attempts") or 0) + 1
        item["heartbeat_at"] = utc_now()
        self.save()

    def set_heartbeat(self, key: str, stamp: str) -> None:
        item = self.get(key)
        if item is None:
            return
        item["heartbeat_at"] = stamp
        self.save()

    def mark(self, key: str, status: str, *, failure_code: str = "") -> None:
        item = self.get(key)
        if item is None:
            return
        item["status"] = status
        item["failure_code"] = failure_code
        item["heartbeat_at"] = utc_now()
        self.save()

    def pause_for_auth(self, key: str) -> None:
        """检测到认证失效：整批暂停，后续条目不再领取。"""
        self.mark(key, AUTH_REQUIRED, failure_code="login_required")
        self.payload["pause_reason"] = "登录态失效"
        self.payload["paused_at"] = utc_now()
        self.status = BATCH_PAUSED_AUTH
        self.save()

    def pause_for_risk(self, key: str, *, reason: str = "") -> None:
        """检测到 B站 风控/限流且退避未解除：整批暂停，后续条目不再领取。

        与认证暂停分开记：风控是「等一段时间可能自己好」，认证是「必须人去登录」，
        恢复时给出的建议不同。当前条目留在待跑态，恢复后先重跑它。
        """
        self.mark(key, RETRY_WAIT, failure_code="risk_control")
        self.payload["pause_reason"] = str(reason or "B站风控/限流")
        self.payload["paused_at"] = utc_now()
        self.status = BATCH_PAUSED_RISK
        self.save()

    def resume(self) -> None:
        """恢复：把认证暂停的条目转回待跑，已完成的不重跑。"""
        if not self.is_paused:
            raise ValueError("批次未处于暂停状态")
        for item in self.items:
            if item.get("status") == AUTH_REQUIRED:
                item["status"] = RETRY_WAIT
                item["failure_code"] = ""
                item["heartbeat_at"] = utc_now()
        self.payload.pop("pause_reason", None)
        self.payload.pop("paused_at", None)
        self.status = BATCH_RUNNING
        self.save()

    def recover_stale(self, *, seconds: int) -> int:
        """回收心跳超时的 in-flight 条目（进程被杀留下的），返回回收数。"""
        now = datetime.now(timezone.utc)
        recovered = 0
        for item in self.items:
            if item.get("status") != RUNNING:
                continue
            stamp = _parse_time(item.get("heartbeat_at"))
            if stamp is None or (now - stamp).total_seconds() > seconds:
                item["status"] = RETRY_WAIT
                item["failure_code"] = "stale_recovered"
                recovered += 1
        if recovered:
            self.save()
        return recovered


def default_path(state_dir: Path) -> Path:
    return Path(state_dir) / "batch_state.json"
