#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉计划的数据契约类型（T07 从 visual_schema 机械搬移，不改行为）。

内容：枚举、常量、路径归一化与用户标签、全部 dataclass。
依赖方向：visual_types ← visual_validation / visual_io；本模块不得反向引用它们。


原契约口径（自 visual_schema.py 归位，逐字未改；T07 搬移时曾漏带这一段）：
- TOML 是视觉任务的**唯一机器事实源**；Markdown 阅读稿由 TOML 渲染（禁止双份手工维护）。
- 任务 ID 形如 `<资产ID>-<任务种类>-vNN`；所有路径保存为**相对资产库路径**。
内部枚举取英文稳定值（机器事实源），用户输出经 ``user_status_label`` 取中文（规格 8）。
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, replace

SCHEMA_VERSION = 1
MAX_AUTO_ROUNDS = 3

# 画幅契约（规格 6.3）
ASPECT_BY_KIND = {
    "正脸": "1:1",
    "后脑": "1:1",
    "道具": "1:1",
    "服装独立正面": "2:3",
    "服装独立背面": "2:3",
    "人物全身正面": "2:3",
    "人物全身背面": "2:3",
    "场景母版": "16:9",
    "拼版": "16:9",
    "剧情剧照": "16:9",
    "动作参考": "16:9",
    "材质特写": "1:1",
}

# 剧情类任务（设计 §11）：必须依据镜头组/分镜提示词与导演构图卡，不得从剧情摘要直接生图
STORY_TASK_KINDS = ("剧情剧照", "动作参考", "材质特写")

TASK_ID_RE = re.compile(r"^[A-Za-z]{2}_[^-]+-.+-v\d{2}$")


class VisualTaskKind(str, enum.Enum):
    FACE = "正脸"
    BACK_HEAD = "后脑"
    COSTUME_FRONT = "服装独立正面"
    COSTUME_BACK = "服装独立背面"
    FULL_BODY_FRONT = "人物全身正面"
    FULL_BODY_BACK = "人物全身背面"
    SCENE_MASTER = "场景母版"
    PROP = "道具"
    BOARD = "拼版"
    STORY_SHOT = "剧情剧照"
    ACTION_REF = "动作参考"
    MATERIAL_CLOSEUP = "材质特写"

    @classmethod
    def valid_values(cls) -> set[str]:
        return {item.value for item in cls}


class VisualTaskStatus(str, enum.Enum):
    PLANNED = "PLANNED"
    GENERATED = "GENERATED"
    QC_PENDING = "QC_PENDING"
    WAITING_REVIEW = "WAITING_REVIEW"
    NEEDS_REVISION = "NEEDS_REVISION"
    ACCEPTED = "ACCEPTED"
    READY_AS_REFERENCE = "READY_AS_REFERENCE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NEEDS_HUMAN_DECISION = "NEEDS_HUMAN_DECISION"
    AWAITING_MANUAL_RESULT = "AWAITING_MANUAL_RESULT"
    # 生成端点不得自签：当前运行环境（主模型）不能看图时停在这里等能看图的审核者，**不得签发**
    AWAITING_VISUAL_REVIEW = "AWAITING_VISUAL_REVIEW"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"

    @classmethod
    def valid_values(cls) -> set[str]:
        return {item.value for item in cls}


# 审核判定（规格 9）：**不是**任务状态。「通过但有备注」发布后任务仍是可作为参考，
# 备注进入正式清单；只有下面四种判定，未知判定一律拒绝。
REVIEW_DECISIONS = ("ACCEPTED", "PASS_WITH_NOTES", "NEEDS_REVISION", "NEEDS_HUMAN_DECISION")

# 构图区域词表（阶段 3 导演构图卡的最小取值；语义区域而非像素坐标）
ZONE_VALUES = ("左", "中", "右", "左中", "中右")
DEPTH_VALUES = ("前景", "中景", "后景")
# 事实来源层级（设计 §2.3）：分镜不得覆盖剧本与资产事实，导演推导必须标记来源
SOURCE_LEVELS = ("原始剧本", "资产索引", "分镜提示词", "导演推导")

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReferenceRole(str, enum.Enum):
    IDENTITY = "identity"
    STRUCTURE = "structure"
    STYLE = "style"
    MATERIAL = "material"
    LIGHTING = "lighting"
    SPACE = "space"

    @classmethod
    def valid_values(cls) -> set[str]:
        return {item.value for item in cls}


class QcDecision(str, enum.Enum):
    ACCEPTED = "ACCEPTED"
    PASS_WITH_NOTES = "PASS_WITH_NOTES"
    NEEDS_REVISION = "NEEDS_REVISION"
    NEEDS_HUMAN_DECISION = "NEEDS_HUMAN_DECISION"
    UNTRUSTWORTHY = "QC_UNTRUSTWORTHY"

    @classmethod
    def valid_values(cls) -> set[str]:
        return {item.value for item in cls}


# 用户可见中文状态（规格 8）：内部英文枚举，对外一律中文
USER_STATUS_LABELS = {
    "PLANNED": "待生成",
    "GENERATED": "已生成待检查",
    "QC_PENDING": "质检中",
    "WAITING_REVIEW": "等待审核",
    "NEEDS_REVISION": "需修改",
    "ACCEPTED": "已通过",
    "READY_AS_REFERENCE": "可作为参考",
    "NEEDS_REVIEW": "需要重新检查",
    "NEEDS_HUMAN_DECISION": "需要人工决定",
    "AWAITING_MANUAL_RESULT": "等待手工结果",
    "AWAITING_VISUAL_REVIEW": "等待主模型审核（当前环境不能看图）",
    "BLOCKED": "已阻断",
    "FAILED": "生成失败",
}

# 可签发状态：只有审核通过（或已通过并可作为参考）才允许写正式版本清单。
# 审核者不可看图时任务停在 AWAITING_VISUAL_REVIEW，不在其中——这是"生成端点不得自签"的落点。
PUBLISHABLE_STATUSES = ("ACCEPTED", "READY_AS_REFERENCE")

# 已开工（产生过产物或已进入质检）的状态：此时上游未通过才是真问题；
# 纯 PLANNED 的下游只是规划顺序，不构成"用未通过图当参考"。
PRODUCED_STATUSES = (
    "GENERATED", "QC_PENDING", "ACCEPTED", "READY_AS_REFERENCE",
    "NEEDS_REVISION", "NEEDS_REVIEW", "FAILED", "AWAITING_VISUAL_REVIEW",
)

USER_DECISION_LABELS = {
    "ACCEPTED": "通过",
    "NEEDS_REVISION": "需修改",
    "NEEDS_HUMAN_DECISION": "需要人工决定",
    "QC_UNTRUSTWORTHY": "检查结果不可信",
}


def user_status_label(status: str) -> str:
    """内部状态 → 中文用户标签（未知值原样透出，不伪造）。"""
    return USER_STATUS_LABELS.get(str(status), str(status))


def user_decision_label(decision: str) -> str:
    return USER_DECISION_LABELS.get(str(decision), str(decision))


def normalize_rel_path(value: str | None) -> str:
    """相对路径归一化：反斜杠 → 正斜杠、去掉多余的 ./ 前缀。"""
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def is_relative_safe(path: str) -> bool:
    """相对且不越界（不含盘符、不以 / 开头、不含 .. 段）。"""
    p = normalize_rel_path(path)
    if not p:
        return False
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return False
    return ".." not in p.split("/")


@dataclass(frozen=True)
class ReferenceBinding:
    """参考图绑定：职责必须显式声明，不得越权（规格 7）。"""

    path: str = ""
    role: str = ReferenceRole.STYLE.value
    sha256: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_rel_path(self.path))


@dataclass(frozen=True)
class QcRecord:
    decision: str = QcDecision.ACCEPTED.value
    checker: str = ""
    checked_at: str = ""
    issues: tuple[dict, ...] = ()
    input_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubjectPlacement:
    """人物在画面中的站位（阶段 3 工单 3.1）：语义关系，不要求像素坐标。"""

    subject: str = ""
    zone: str = "中"
    depth: str = "中景"
    facing: str = ""
    gaze: str = ""
    action: str = ""
    occlusion: str = ""
    distance: str = ""

    def __post_init__(self) -> None:
        if not str(self.subject or "").strip():
            raise ValueError("SubjectPlacement 必须指明主体（资产 ID 或角色名）")
        if self.zone not in ZONE_VALUES:
            raise ValueError(f"人物左右区域只允许 {ZONE_VALUES}，当前 {self.zone!r}")
        if self.depth not in DEPTH_VALUES:
            raise ValueError(f"前中后景只允许 {DEPTH_VALUES}，当前 {self.depth!r}")


@dataclass(frozen=True)
class PropPlacement:
    """关键道具的位置、数量与持有关系（数量必须与资产事实一致）。"""

    name: str = ""
    zone: str = "中"
    holder: str = ""
    count: int = 1
    state: str = ""

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise ValueError("PropPlacement 必须指明道具")
        if self.zone not in ZONE_VALUES:
            raise ValueError(f"道具左右区域只允许 {ZONE_VALUES}，当前 {self.zone!r}")
        if type(self.count) is not int or self.count < 1:
            raise ValueError(f"道具数量必须是 ≥1 的整数，当前 {self.count!r}")


@dataclass(frozen=True)
class CompositionSpec:
    """导演构图卡的结构化事实（阶段 2 契约 / 阶段 3 生成）。

    `source_level` 记录该构图的最高事实层级：原始剧本 > 资产索引 > 分镜提示词 > 导演推导。
    """

    shot_size: str = ""
    camera_angle: str = ""
    camera_height: str = ""
    subjects: tuple[SubjectPlacement, ...] = ()
    props: tuple[PropPlacement, ...] = ()
    anchors: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    rationale: str = ""
    source_level: str = ""

    def __post_init__(self) -> None:
        if not str(self.shot_size or "").strip():
            raise ValueError("构图卡必须写明景别")
        if self.source_level not in SOURCE_LEVELS:
            raise ValueError(f"构图事实来源只允许 {SOURCE_LEVELS}，当前 {self.source_level!r}")


@dataclass(frozen=True)
class ReviewBinding:
    """审核绑定的六项输入哈希：任一变化，审核立即失效（设计 §15.5）。

    空值允许（旧计划没有绑定），是否闭环由审核契约与发布器判定；
    非空值必须是 64 位十六进制，长度或字符不符即拒绝。
    """

    task_id: str = ""
    image_sha256: str = ""
    prompt_sha256: str = ""
    reference_set_sha256: str = ""
    style_sha256: str = ""
    composition_sha256: str = ""

    def __post_init__(self) -> None:
        for name in ("image_sha256", "prompt_sha256", "reference_set_sha256",
                     "style_sha256", "composition_sha256"):
            value = str(getattr(self, name) or "").strip().lower()
            object.__setattr__(self, name, value)
            if value and not SHA256_RE.match(value):
                raise ValueError(f"审核绑定 {name} 必须是 64 位十六进制 SHA-256，当前 {value!r}")

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "image_sha256": self.image_sha256,
            "prompt_sha256": self.prompt_sha256,
            "reference_set_sha256": self.reference_set_sha256,
            "style_sha256": self.style_sha256,
            "composition_sha256": self.composition_sha256,
        }

    @property
    def is_complete(self) -> bool:
        return all(self.as_dict().values())


@dataclass(frozen=True)
class AttemptRecord:
    round: int = 1
    image: str = ""
    request: str = ""
    response: str = ""
    qc: str = ""
    prompt_hash: str = ""
    image_sha256: str = ""
    model: str = ""
    provider: str = ""
    response_id: str = ""

    def __post_init__(self) -> None:
        for name in ("image", "request", "response", "qc"):
            object.__setattr__(self, name, normalize_rel_path(getattr(self, name)))


@dataclass(frozen=True)
class VisualTask:
    task_id: str = ""
    asset_id: str = ""
    kind: str = VisualTaskKind.FACE.value
    status: str = VisualTaskStatus.PLANNED.value
    aspect_ratio: str = "1:1"
    style_slug: str = ""
    style_version: str = ""
    style_hash: str = ""
    engine: str = "gemini"
    model: str = ""
    depends_on: tuple[str, ...] = ()
    materials: tuple[str, ...] = ()
    references: tuple[ReferenceBinding, ...] = ()
    attempts: tuple[AttemptRecord, ...] = ()
    max_rounds: int = MAX_AUTO_ROUNDS
    fact_source: str = ""
    user_locked: tuple[str, ...] = ()
    notes: str = ""
    # 阶段 2 工单 2.1 新增（旧计划缺省为空，读取兼容，写出统一新 schema）
    purpose: str = ""
    script_source: str = ""
    asset_index_key: str = ""
    storyboard_source: str = ""
    prompt: str = ""
    negative_prompt: str = ""
    composition_path: str = ""
    composition_sha256: str = ""
    blocking_path: str = ""
    blocking_sha256: str = ""
    current_version: str = ""

    def __post_init__(self) -> None:
        for name in ("composition_path", "blocking_path"):
            object.__setattr__(self, name, normalize_rel_path(getattr(self, name)))

    @property
    def round_count(self) -> int:
        return len(self.attempts)

    @property
    def latest_attempt(self) -> AttemptRecord | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def is_accepted(self) -> bool:
        return self.status in (VisualTaskStatus.ACCEPTED.value,
                               VisualTaskStatus.READY_AS_REFERENCE.value)


@dataclass(frozen=True)
class VisualPlan:
    schema_version: int = SCHEMA_VERSION
    revision: int = 0
    style_name: str = ""
    style_slug: str = ""
    style_version: str = ""
    style_hash: str = ""
    engine_profile: str = "gemini"
    lib: str = "."
    asset_index_hash: str = ""
    updated_at: str = ""
    tasks: tuple[VisualTask, ...] = ()

    def by_id(self, task_id: str) -> VisualTask | None:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        return None

    def with_task(self, task: VisualTask) -> "VisualPlan":
        kept = tuple(t for t in self.tasks if t.task_id != task.task_id)
        return replace(self, tasks=tuple(sorted(kept + (task,), key=lambda t: t.task_id)))

    def sorted_tasks(self) -> tuple[VisualTask, ...]:
        return tuple(sorted(self.tasks, key=lambda t: t.task_id))

    def counts_by_status(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for task in self.tasks:
            out[task.status] = out.get(task.status, 0) + 1
        return dict(sorted(out.items()))
