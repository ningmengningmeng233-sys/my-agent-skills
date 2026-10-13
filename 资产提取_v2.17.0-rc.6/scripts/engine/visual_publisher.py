#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正式版本发布器（阶段 2 工单 2.3）。

**只有发布器可以更新正式版本。** 把图片直接复制进正式目录、修改 Markdown 报告或手工改
计划状态字段都不能产生正式资产——正式事实源只有一个：

    <资产库>/视觉资产/正式版本.json

结构（每个任务最多一个 `current`，历史版本**只增不改**）：

```json
{
  "schema_version": 1,
  "updated_at": "...",
  "tasks": {
    "CH_林姑娘-正脸-v01": {
      "current": {"version": "attempt-01", "image": "视觉资产/任务/.../image.png",
                  "image_sha256": "...", "mime": "image/png", "width": 1024, "height": 1024,
                  "decision": "ACCEPTED", "reviewer": "...", "reviewed_at": "...",
                  "published_at": "...", "binding": {"..."},
                  "notes": "..."},
      "history": [ {"...":"同一个结构","superseded_at":"..."} ]
    }
  }
}
```

发布前置条件（缺一即稳定码拒绝）：
1. 审核判定为 `ACCEPTED` 或 `PASS_WITH_NOTES`，且六项绑定哈希与**当前输入**仍完全一致；
2. 产物可读、MIME 是 PNG/JPEG/WebP、尺寸可解析（不得是坏图）；
3. 上游依赖都已有 `current` 正式版本（不能拿未发布的图当参考）；
4. 替换当前版本时，旧版进入该任务的 `history`（保留路径与哈希），直接下游转 `NEEDS_REVIEW`。

不使用符号链接：清单只用相对路径 + 哈希指向轮次目录里的图片。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from datetime import datetime, timezone

from .reference_resolver import sniff_image_mime
from .review_contract import (
    ReviewContractError,
    ReviewRequest,
    ReviewResult,
    validate_review_binding,
)
from .visual_schema import VisualPlan, VisualTask, VisualTaskStatus

RELEASE_MANIFEST_NAME = "正式版本.json"
RELEASE_DIR_REL = ("视觉资产",)
SCHEMA_VERSION = 1

PUBLISH_DECISION_REQUIRED = "PUBLISH_DECISION_REQUIRED"
PUBLISH_BINDING_STALE = "PUBLISH_BINDING_STALE"
PUBLISH_IMAGE_MISSING = "PUBLISH_IMAGE_MISSING"
PUBLISH_IMAGE_UNREADABLE = "PUBLISH_IMAGE_UNREADABLE"
PUBLISH_MIME_UNSUPPORTED = "PUBLISH_MIME_UNSUPPORTED"
PUBLISH_UPSTREAM_INVALID = "PUBLISH_UPSTREAM_INVALID"
PUBLISH_TASK_UNKNOWN = "PUBLISH_TASK_UNKNOWN"
PUBLISH_RELEASE_MISSING = "PUBLISH_RELEASE_MISSING"
PUBLISH_HASH_STALE = "PUBLISH_HASH_STALE"
PUBLISH_NOT_ACCEPTED = "PUBLISH_NOT_ACCEPTED"
PUBLISH_AWAITING_VISUAL_REVIEW = "PUBLISH_AWAITING_VISUAL_REVIEW"

ALLOWED_MIME = ("image/png", "image/jpeg", "image/webp")
PUBLISHABLE_DECISIONS = ("ACCEPTED", "PASS_WITH_NOTES")


class PublishError(RuntimeError):
    """发布被拒绝：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


@dataclass(frozen=True)
class ReleaseRecord:
    """一项任务的正式版本记录（不可变；替换即新建记录并把旧的放进 history）。"""

    version: str = ""
    image: str = ""
    image_sha256: str = ""
    mime: str = ""
    width: int = 0
    height: int = 0
    decision: str = ""
    reviewer: str = ""
    reviewed_at: str = ""
    published_at: str = ""
    binding: dict = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "image": self.image,
            "image_sha256": self.image_sha256,
            "mime": self.mime,
            "width": int(self.width),
            "height": int(self.height),
            "decision": self.decision,
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at,
            "published_at": self.published_at,
            "binding": dict(self.binding),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReleaseRecord":
        return cls(
            version=str(data.get("version", "")), image=str(data.get("image", "")),
            image_sha256=str(data.get("image_sha256", "")), mime=str(data.get("mime", "")),
            width=int(data.get("width", 0) or 0), height=int(data.get("height", 0) or 0),
            decision=str(data.get("decision", "")), reviewer=str(data.get("reviewer", "")),
            reviewed_at=str(data.get("reviewed_at", "")),
            published_at=str(data.get("published_at", "")),
            binding=dict(data.get("binding") or {}), notes=str(data.get("notes", "")),
        )


def release_manifest_path(lib: Path) -> Path:
    return Path(lib) / RELEASE_DIR_REL[0] / RELEASE_MANIFEST_NAME


def load_release_manifest(lib: Path) -> dict:
    """读取正式版本清单；缺失返回空骨架（不创建文件）。"""
    path = release_manifest_path(lib)
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "updated_at": "", "tasks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": SCHEMA_VERSION, "updated_at": "", "tasks": {},
                "unreadable": True}
    if not isinstance(data, dict):
        return {"schema_version": SCHEMA_VERSION, "updated_at": "", "tasks": {},
                "unreadable": True}
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("updated_at", "")
    if not isinstance(data.get("tasks"), dict):
        data["tasks"] = {}
    return data


def current_release(lib: Path, task_id: str) -> ReleaseRecord | None:
    entry = load_release_manifest(lib).get("tasks", {}).get(task_id)
    if not isinstance(entry, dict) or not isinstance(entry.get("current"), dict):
        return None
    return ReleaseRecord.from_dict(entry["current"])


def history_of(lib: Path, task_id: str) -> tuple[ReleaseRecord, ...]:
    entry = load_release_manifest(lib).get("tasks", {}).get(task_id)
    if not isinstance(entry, dict):
        return ()
    return tuple(ReleaseRecord.from_dict(row) for row in (entry.get("history") or [])
                 if isinstance(row, dict))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _live_binding(lib: Path, task: VisualTask) -> dict:
    """由当前输入现算六项绑定（用于判断审核是否仍然有效）。"""
    from .review_contract import build_review_request

    return build_review_request(lib, VisualPlan(), task).binding.as_dict()


def _image_facts(lib: Path, rel: str) -> tuple[bytes, str, int, int]:
    path = Path(lib) / rel
    if not path.is_file():
        raise PublishError(PUBLISH_IMAGE_MISSING, f"产物文件不存在：{rel}")
    try:
        data = path.read_bytes()
    except OSError as e:
        raise PublishError(PUBLISH_IMAGE_UNREADABLE, f"产物不可读：{type(e).__name__}") from e
    mime = sniff_image_mime(data)
    if mime not in ALLOWED_MIME:
        raise PublishError(PUBLISH_MIME_UNSUPPORTED,
                           f"只接受 {ALLOWED_MIME}，检测到 {mime or '未知格式'}")
    try:
        from PIL import Image  # 视觉可选依赖（pyproject 的 visual extra）
    except ImportError as e:  # pragma: no cover - 仅在缺少 Pillow 的环境触发
        raise PublishError(PUBLISH_IMAGE_UNREADABLE,
                           "缺少 Pillow，无法核验图片尺寸（pip install '.[visual]'）") from e
    import io

    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
    except Exception as e:  # noqa: BLE001
        raise PublishError(PUBLISH_IMAGE_UNREADABLE, f"图片尺寸无法解析：{type(e).__name__}") from e
    if width <= 0 or height <= 0:
        raise PublishError(PUBLISH_IMAGE_UNREADABLE, f"图片尺寸非法：{width}x{height}")
    return data, mime, int(width), int(height)


def _require_upstream(plan: VisualPlan, task: VisualTask, manifest: dict) -> None:
    """上游必须已有正式版本：未发布的图不得成为下游参考。"""
    tasks = {t.task_id: t for t in plan.tasks}
    released = {tid for tid, entry in manifest.get("tasks", {}).items()
                if isinstance(entry, dict) and isinstance(entry.get("current"), dict)}
    for dep in task.depends_on:
        target = tasks.get(dep)
        if target is None:
            continue
        if dep not in released:
            raise PublishError(
                PUBLISH_UPSTREAM_INVALID,
                f"上游 {dep} 尚无正式版本（当前状态 {target.status}）——不得用未发布图片当参考")


def ensure_publishable_status(task: VisualTask) -> None:
    """审核者不可看图时不得签发（生成端点不得自签）。

    当前运行环境（主模型）不能看图 → 任务停在 `AWAITING_VISUAL_REVIEW`，
    此时任何"看起来通过了"的判定都不足以签发。
    """
    status = getattr(task, "status", "")
    status = str(getattr(status, "value", status))
    if status == "AWAITING_VISUAL_REVIEW":
        raise PublishError(
            PUBLISH_AWAITING_VISUAL_REVIEW,
            "当前环境不能看图：任务停在 AWAITING_VISUAL_REVIEW，须由能看图的主模型或用户显式添加的子代理审核后才能签发")


def publish_task(lib: Path, plan: VisualPlan, task: VisualTask, request: ReviewRequest,
                 result: ReviewResult, *, published_at: str = "") -> tuple[VisualPlan, ReleaseRecord]:
    """发布一项任务的正式版本；返回（更新后的计划, 新正式版本记录）。

    只有本函数会写 `正式版本.json`。计划状态由调用方经返回值落盘（同一协调器持锁写）。
    """
    lib = Path(lib)
    if task is None:
        raise PublishError(PUBLISH_TASK_UNKNOWN, "任务不存在")
    ensure_publishable_status(task)
    if str(result.decision) not in PUBLISHABLE_DECISIONS:
        raise PublishError(PUBLISH_DECISION_REQUIRED,
                           f"只有 {PUBLISHABLE_DECISIONS} 允许发布，当前 {result.decision!r}")
    try:
        validate_review_binding(request, result)
    except ReviewContractError as e:
        raise PublishError(PUBLISH_BINDING_STALE, e.detail or str(e)) from e

    attempt = task.latest_attempt
    if attempt is None or not str(attempt.image or "").strip():
        raise PublishError(PUBLISH_IMAGE_MISSING, f"任务 {task.task_id} 没有可发布的产物")
    rel = str(attempt.image).replace("\\", "/")
    data, mime, width, height = _image_facts(lib, rel)

    # 审核绑定必须对**当前输入**仍然成立：审核后被改过的提示词/参考/画风/构图/换图一律拒绝
    live = _live_binding(lib, task)
    stale = [key for key in ("image_sha256", "prompt_sha256", "reference_set_sha256",
                             "style_sha256", "composition_sha256")
             if str(live.get(key, "")) != str(request.binding.as_dict().get(key, ""))]
    if stale:
        if "image_sha256" in stale:
            raise PublishError(PUBLISH_HASH_STALE,
                               "图片内容与审核时不一致（不能在审核后换图再发布）")
        raise PublishError(PUBLISH_BINDING_STALE,
                           f"审核绑定已失效：{'、'.join(stale)} 在审核后发生变化")

    import hashlib

    image_sha = hashlib.sha256(data).hexdigest()
    version = f"attempt-{int(attempt.round):02d}"
    record = ReleaseRecord(
        version=version, image=rel, image_sha256=image_sha, mime=mime, width=width,
        height=height, decision=str(result.decision), reviewer=result.reviewer,
        reviewed_at=result.reviewed_at, published_at=published_at or _now(),
        binding=request.binding.as_dict(), notes=str(result.notes or ""),
    )

    # 正式版本清单是「读—改—写」，必须与计划共用同一把锁：两个进程同时发布会互相覆盖。
    # 延迟导入以避免模块级循环依赖（调度器 import 本模块）；锁在同一线程内可重入，
    # 所以调度器持锁时调用本函数不会自锁。
    from .visual_scheduler import plan_lock

    with plan_lock(lib):
        manifest = load_release_manifest(lib)
        _require_upstream(plan, task, manifest)
        entry = manifest["tasks"].get(task.task_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        old = entry.get("current")
        history = list(entry.get("history") or [])
        if isinstance(old, dict) and old != record.to_dict():
            superseded = dict(old)
            superseded["superseded_at"] = record.published_at
            history.append(superseded)
        entry["current"] = record.to_dict()
        entry["history"] = history
        manifest["tasks"][task.task_id] = entry
        manifest["schema_version"] = SCHEMA_VERSION
        manifest["updated_at"] = record.published_at

        _write_manifest(lib, manifest)
    updated_plan = _mark_published(plan, task, record)
    return updated_plan, record


def _mark_published(plan: VisualPlan, task: VisualTask, record: ReleaseRecord) -> VisualPlan:
    """发布成功后置任务为「可作为参考」并登记当前正式版本编号。"""
    status = VisualTaskStatus.READY_AS_REFERENCE.value
    updated = replace(task, status=status, current_version=record.version)
    return plan.with_task(updated)


def _write_manifest(lib: Path, manifest: dict) -> Path:
    target = release_manifest_path(lib)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return target


def validate_release(lib: Path, plan: VisualPlan) -> list[tuple[str, str, str]]:
    """交付门用的交叉验证：(task_id, 原因码, 说明)。

    计划说「已通过」但正式清单缺项、清单指向的图片不存在或哈希变化、上游未发布，
    都在此如实报出——交付门据此拒绝签发。
    """
    lib = Path(lib)
    manifest = load_release_manifest(lib)
    if manifest.get("unreadable"):
        return [("", "PUBLISH_MANIFEST_UNREADABLE", "正式版本清单不可解析")]
    import hashlib

    problems: list[tuple[str, str, str]] = []
    for task in plan.tasks:
        if not task.is_accepted:
            continue
        entry = manifest.get("tasks", {}).get(task.task_id)
        current = entry.get("current") if isinstance(entry, dict) else None
        if not isinstance(current, dict):
            problems.append((task.task_id, PUBLISH_RELEASE_MISSING,
                             "状态为已通过但没有正式版本记录（未经过发布器）"))
            continue
        rel = str(current.get("image", ""))
        path = lib / rel if rel else None
        if not rel or path is None or not path.is_file():
            problems.append((task.task_id, PUBLISH_IMAGE_MISSING, f"正式版本图片不存在：{rel}"))
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != str(current.get("image_sha256", "")):
            problems.append((task.task_id, PUBLISH_HASH_STALE,
                             "正式版本图片内容与发布记录不一致"))
    return problems


def downstream_of(plan: VisualPlan, task_id: str) -> tuple[str, ...]:
    return tuple(t.task_id for t in plan.tasks if task_id in t.depends_on)


def propagate_release_change(plan: VisualPlan, task_id: str) -> VisualPlan:
    """发布新版本后：直接下游的已通过任务转 NEEDS_REVIEW（旧图保留，取消参考资格）。"""
    affected = set(downstream_of(plan, task_id))
    if not affected:
        return plan
    updated = []
    for task in plan.tasks:
        if task.task_id in affected and task.is_accepted:
            updated.append(replace(task, status=VisualTaskStatus.NEEDS_REVIEW.value))
        else:
            updated.append(task)
    return replace(plan, tasks=tuple(updated))
