"""v2 公共类型：状态枚举、错误码与稳定 JSON 辅助。

枚举取值与设计文档第 15、18 节逐字一致，并直接以 `str` 参与 JSON 序列化。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum

ISO_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"

#: 当前质量管线签名；与历史版本比较即可判断是否需要重处理。
CURRENT_PROCESSING_SIGNATURE = "v2"

#: v1 迁移版本签名：无 AI 校正版，但必须保持可读、可重渲染、可复用。
LEGACY_PROCESSING_SIGNATURE = "legacy-v1"


class _StrEnum(str, Enum):
    """既是字符串常量又是枚举：JSON 序列化后就是裸字符串。"""

    def __str__(self) -> str:  # pragma: no cover - 便于日志输出
        return str(self.value)


class JobStatus(_StrEnum):
    """jobs.status，见设计文档 15.2。"""

    PENDING = "pending"
    RESOLVING = "resolving"
    FETCHING = "fetching"
    MEDIA_READY = "media_ready"
    ANALYZING = "analyzing"
    TRANSCRIBING = "transcribing"
    OCR = "ocr"
    DIARIZATION = "diarization"
    CORRECTING = "correcting"
    SUMMARIZING = "summarizing"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    AWAITING_AI = "awaiting_ai"
    VISUAL_TEXT_ONLY = "visual_text_only"
    RETRY_WAIT = "retry_wait"
    AUTH_REQUIRED = "auth_required"
    FAILED = "failed"
    DUPLICATE = "duplicate"
    REUSED = "reused"


#: 不再推进的状态：不会被重新领取，也不参与“中断恢复”。
TERMINAL_JOB_STATUSES = frozenset({
    JobStatus.COMPLETED, JobStatus.NEEDS_REVIEW, JobStatus.AWAITING_AI,
    JobStatus.VISUAL_TEXT_ONLY, JobStatus.FAILED, JobStatus.DUPLICATE,
    JobStatus.REUSED,
})

#: 正在进行中的状态：进程重启后需按心跳判断是否中断。
PROCESSING_JOB_STATUSES = frozenset({
    JobStatus.RESOLVING, JobStatus.FETCHING, JobStatus.MEDIA_READY,
    JobStatus.ANALYZING, JobStatus.TRANSCRIBING, JobStatus.OCR,
    JobStatus.DIARIZATION, JobStatus.CORRECTING, JobStatus.SUMMARIZING,
    JobStatus.PUBLISHING,
})


class BatchStatus(_StrEnum):
    """batches.status，见设计文档 15.1。"""

    DRAFT = "draft"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PENDING = "pending"
    RUNNING = "running"
    PAUSED_AUTH = "paused_auth"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"


#: 允许 `--resume` 继续推进的批次状态。
RESUMABLE_BATCH_STATUSES = frozenset({
    BatchStatus.PENDING, BatchStatus.RUNNING, BatchStatus.PAUSED_AUTH,
})


class Stage(_StrEnum):
    """jobs.stage：当前 job 已推进到哪个流水线阶段。"""

    RESOLVING = "resolving"
    FETCHING = "fetching"
    MEDIA_READY = "media_ready"
    ANALYZING = "analyzing"
    TRANSCRIBING = "transcribing"
    OCR = "ocr"
    DIARIZATION = "diarization"
    CORRECTING = "correcting"
    SUMMARIZING = "summarizing"
    PUBLISHING = "publishing"


class QualityStatus(_StrEnum):
    """版本质量状态，见设计文档 11.3 与 11.4。"""

    PASSED = "passed"
    NEEDS_REVIEW = "needs_review"
    VISUAL_TEXT_ONLY = "visual_text_only"


class LifecycleStatus(_StrEnum):
    """版本生命周期：只有完整校验过的版本才能从 staging 进入 published。"""

    STAGING = "staging"
    PUBLISHED = "published"
    FAILED = "failed"


class CorrectionStatus(_StrEnum):
    """AI 校正状态，见设计文档 12.3。"""

    PENDING = "pending"
    CORRECTED = "corrected"
    FAILED_PENDING_RETRY = "failed_pending_retry"


class SummaryStatus(_StrEnum):
    PENDING = "pending"
    SUMMARIZED = "summarized"


class SummaryMode(_StrEnum):
    """摘要模式，见设计文档 13。"""

    NORMAL = "normal"
    RESTRICTED = "restricted"
    VISUAL_TEXT_ONLY = "visual_text_only"


class ArtifactKind(_StrEnum):
    """交付文件类型：主文件与原始记录文件。"""

    MAIN_TXT = "main_txt"
    EVIDENCE_TXT = "evidence_txt"


class ReuseAction(_StrEnum):
    """复用判定，见设计文档 14.3。"""

    REUSE = "reuse"
    RENDER_ONLY = "render_only"
    FILL_AI = "fill_ai"
    REPROCESS = "reprocess"
    FETCH = "fetch"


@dataclass(frozen=True)
class PreparedArtifact:
    """已写入磁盘、等待校验与登记的交付文件。"""

    kind: ArtifactKind | str
    path: str
    sha256: str
    size_bytes: int


#: 流水线阶段的先后顺序；允许跳过（局部降级），但禁止回退。
PIPELINE_STAGES = (
    Stage.RESOLVING, Stage.FETCHING, Stage.MEDIA_READY, Stage.ANALYZING,
    Stage.TRANSCRIBING, Stage.OCR, Stage.DIARIZATION, Stage.CORRECTING,
    Stage.SUMMARIZING, Stage.PUBLISHING,
)
_STAGE_NAMES = tuple(str(stage) for stage in PIPELINE_STAGES)
_TERMINAL_NAMES = frozenset(str(status) for status in (
    JobStatus.COMPLETED, JobStatus.NEEDS_REVIEW, JobStatus.VISUAL_TEXT_ONLY,
    JobStatus.AWAITING_AI, JobStatus.FAILED, JobStatus.DUPLICATE,
    JobStatus.REUSED,
))


def can_transition(current: JobStatus | str, target: JobStatus | str) -> bool:
    """job 状态迁移判定，见设计文档 15.2。

    阶段只能前进（可跳过，用于局部降级），任意进行中阶段都能失败、重试或转认证；
    终态不可再迁移；`auth_required` 只能在登录后续跑。
    """
    current, target = str(current), str(target)
    if current == target:
        return False
    if current in _TERMINAL_NAMES:
        return False
    if current == str(JobStatus.AUTH_REQUIRED):
        return target == str(JobStatus.RETRY_WAIT) or target in _STAGE_NAMES
    if current in (str(JobStatus.PENDING), str(JobStatus.RETRY_WAIT)):
        return True
    if current in _STAGE_NAMES and target in _STAGE_NAMES:
        return _STAGE_NAMES.index(target) > _STAGE_NAMES.index(current)
    return True


@dataclass(frozen=True)
class NormalizedInput:
    """一条待处理的输入：保留原始文本，交给上游的永远是规范化 URL。"""

    raw_input: str
    url: str
    candidate_aweme_id: str = ""
    kind: str = "video"


@dataclass(frozen=True)
class JobRecord:
    """jobs 表一行的不可变视图。"""

    job_id: str
    batch_id: str
    position: int
    raw_input: str
    normalized_input: str
    candidate_aweme_id: str
    aweme_id: str
    status: str
    stage: str
    duplicate_of_job_id: str
    failure_code: str
    failure_message: str
    checkpoint: dict
    heartbeat_at: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "JobRecord":
        data = dict(row)
        raw_checkpoint = data.get("checkpoint_json") or "{}"
        try:
            checkpoint = json.loads(raw_checkpoint)
        except (TypeError, ValueError):
            checkpoint = {}
        return cls(
            job_id=str(data.get("job_id", "")),
            batch_id=str(data.get("batch_id", "") or ""),
            position=int(data.get("position", 0) or 0),
            raw_input=str(data.get("raw_input", "") or ""),
            normalized_input=str(data.get("normalized_input", "") or ""),
            candidate_aweme_id=str(data.get("candidate_aweme_id", "") or ""),
            aweme_id=str(data.get("aweme_id", "") or ""),
            status=str(data.get("status", "") or ""),
            stage=str(data.get("stage", "") or ""),
            duplicate_of_job_id=str(data.get("duplicate_of_job_id", "") or ""),
            failure_code=str(data.get("failure_code", "") or ""),
            failure_message=str(data.get("failure_message", "") or ""),
            checkpoint=checkpoint if isinstance(checkpoint, dict) else {},
            heartbeat_at=str(data.get("heartbeat_at", "") or ""),
            created_at=str(data.get("created_at", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
        )


@dataclass(frozen=True)
class BatchPreview:
    """批次预览结果，见设计文档 6.4。"""

    batch_id: str
    status: str
    submitted: int
    new: int
    reused: int
    duplicates: int
    estimated_seconds: int
    requires_confirmation: bool
    reprocess: int = 0

    def as_dict(self) -> dict:
        data = asdict(self)
        data["status"] = str(self.status)
        return data


class ErrorCode(_StrEnum):
    """稳定错误码，见设计文档第 18 节。"""

    INVALID_INPUT = "invalid_input"
    INPUT_ENCODING_ERROR = "input_encoding_error"
    BATCH_CONFIRMATION_REQUIRED = "batch_confirmation_required"
    LOGIN_REQUIRED = "login_required"
    VIDEO_UNAVAILABLE = "video_unavailable"
    PLATFORM_RATE_LIMITED = "platform_rate_limited"
    INGEST_TIMEOUT = "ingest_timeout"
    INGEST_CONTRACT_CHANGED = "ingest_contract_changed"
    MEDIA_DOWNLOAD_FAILED = "media_download_failed"
    AUDIO_INVALID = "audio_invalid"
    VIDEO_INVALID = "video_invalid"
    AUDIO_ENHANCEMENT_FAILED = "audio_enhancement_failed"
    ASR_FAILED = "asr_failed"
    OCR_FAILED = "ocr_failed"
    DIARIZATION_FAILED = "diarization_failed"
    CORRECTION_PENDING = "correction_pending"
    SUMMARY_PENDING = "summary_pending"
    DATABASE_FAILED = "database_failed"
    PUBLISH_FAILED = "publish_failed"
    ARTIFACT_MISSING = "artifact_missing"


#: 认证类错误会暂停整个批次，而不是终结单条。
AUTH_ERROR_CODES = frozenset({ErrorCode.LOGIN_REQUIRED})

#: 作品本身不可用：只终结这条 job，不做盲目重试。
VIDEO_ERROR_CODES = frozenset({
    ErrorCode.VIDEO_UNAVAILABLE, ErrorCode.INGEST_CONTRACT_CHANGED,
})


def utc_now() -> str:
    """本地时间戳，秒精度：数据库所有 created_at / updated_at 的唯一格式。"""
    return datetime.now(timezone.utc).astimezone().replace(
        tzinfo=None).strftime(ISO_TIMESTAMP_FORMAT)


def error_payload(code: ErrorCode | str, message: str) -> dict:
    """稳定错误结构；`message` 必须已脱敏，不得含媒体直链或凭据。"""
    return {"code": str(code), "message": str(message)}
