"""持久队列：批次、job、去重、心跳、认证暂停与恢复。

进度只存在于 SQLite，不依赖内存列表：进程重启后 `recover_stale_jobs`
按心跳把中断的 job 放回可领取状态，`--resume` 从原位置继续。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Sequence

from scripts.input_normalizer import strip_query_parameters
from scripts.models import (ISO_TIMESTAMP_FORMAT, BatchPreview, BatchStatus,
                            JobRecord, JobStatus, NormalizedInput,
                            PROCESSING_JOB_STATUSES, ReuseAction, Stage,
                            TERMINAL_JOB_STATUSES, can_transition, utc_now)
from scripts.transcript_db import TranscriptDB

#: 无可复用版本时的保守估算：每条视频 3 分钟。
DEFAULT_SECONDS_PER_VIDEO = 180
MAX_NEW_WORK = 10
MAX_ESTIMATED_SECONDS = 30 * 60
STALE_HEARTBEAT_SECONDS = 10 * 60

#: 已有可用原稿、无需重新获取的复用判定。
_REUSABLE_ACTIONS = frozenset({ReuseAction.REUSE, ReuseAction.RENDER_ONLY,
                               ReuseAction.FILL_AI})

_CLAIMABLE_STATUSES = (str(JobStatus.PENDING), str(JobStatus.RETRY_WAIT))


def _timestamp(delta_seconds: float = 0) -> str:
    # 与 utc_now 同口径（ISO T 格式本地时间），避免字符串比较时 'T' > ' '。
    return (datetime.now() + timedelta(seconds=delta_seconds)).strftime(
        ISO_TIMESTAMP_FORMAT)


_FLOAT_RE = re.compile(r"[+-]?\d+(\.\d+)?([eE][+-]?\d+)?\Z")


def _parse_heartbeat(value: object) -> datetime | None:
    """兼容 ISO 8601 字符串与 float 时间戳的心跳解析。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if _FLOAT_RE.match(text):
        try:
            return datetime.fromtimestamp(float(text))
        except (OSError, OverflowError, ValueError):
            return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _heartbeat_age_seconds(value: object) -> float | None:
    """心跳距今秒数；统一转 float 对齐，解析失败返回 None。"""
    parsed = _parse_heartbeat(value)
    if parsed is None:
        return None
    try:
        return time.time() - parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


class QueueRepository:
    """batches / jobs 两张表的全部读写；不承载任何流水线逻辑。"""

    def __init__(self, db: TranscriptDB):
        self.db = db
        self._conn = db.connect()

    # ------------------------------------------------------------------
    # 批次创建
    # ------------------------------------------------------------------

    def create_batch(self, items: Sequence[NormalizedInput], *,
                     source_type: str = "chat", source_label: str = "",
                     confirmed: bool = False) -> BatchPreview:
        """入队前去除重复项、识别可复用项，并按阈值决定是否需要确认。"""
        batch_id = str(uuid.uuid4())
        now = utc_now()
        rows: list[tuple] = []
        seen: dict[str, str] = {}
        submitted = duplicates = reused = reprocess = 0

        for position, item in enumerate(items):
            submitted += 1
            key = item.candidate_aweme_id or item.url
            duplicate_of = seen.get(key, "")
            job_id = str(uuid.uuid4())
            if duplicate_of:
                duplicates += 1
                status = JobStatus.DUPLICATE
            else:
                seen[key] = job_id
                action = (self.db.find_reuse_action(item.candidate_aweme_id)
                          if item.candidate_aweme_id else ReuseAction.FETCH)
                if action in _REUSABLE_ACTIONS:
                    reused += 1
                    status = JobStatus.REUSED
                else:
                    status = JobStatus.PENDING
                    if action == ReuseAction.REPROCESS:
                        reprocess += 1
            rows.append((job_id, batch_id, position, item.raw_input, item.url,
                         item.candidate_aweme_id, "", str(status),
                         str(Stage.RESOLVING), duplicate_of, "", "", "{}", "",
                         now, now))

        new_count = submitted - duplicates - reused
        estimated = self.estimate_seconds(new_count=new_count)
        requires_confirmation = (not confirmed and (
            new_count > MAX_NEW_WORK or estimated > MAX_ESTIMATED_SECONDS))
        status = (BatchStatus.AWAITING_CONFIRMATION if requires_confirmation
                  else BatchStatus.PENDING)
        if new_count == 0:
            # 全部可复用或重复：没有待办工作，批次直接完成
            status = BatchStatus.COMPLETED
        counts = {"submitted": submitted, "new": new_count, "reused": reused,
                  "duplicates": duplicates, "reprocess": reprocess}
        with self._conn:
            self._conn.execute(
                "INSERT INTO batches (batch_id, source_type, source_label, status, "
                "requires_confirmation, counts_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, str(source_type), str(source_label), str(status),
                 1 if requires_confirmation else 0,
                 json.dumps(counts, ensure_ascii=False), now, now))
            self._conn.executemany(
                "INSERT INTO jobs (job_id, batch_id, position, raw_input, "
                "normalized_input, candidate_aweme_id, aweme_id, status, stage, "
                "duplicate_of_job_id, failure_code, failure_message, "
                "checkpoint_json, heartbeat_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return BatchPreview(batch_id=batch_id, status=str(status),
                            submitted=submitted, new=new_count, reused=reused,
                            duplicates=duplicates, estimated_seconds=estimated,
                            requires_confirmation=requires_confirmation,
                            reprocess=reprocess)

    def estimate_seconds(self, *, new_count: int) -> int:
        """优先用历史平均处理时长，无历史时用保守默认值。"""
        if new_count <= 0:
            return 0
        rows = self._conn.execute(
            "SELECT created_at, updated_at FROM jobs WHERE status IN (?, ?, ?) "
            "AND created_at != '' AND updated_at != ''",
            (str(JobStatus.COMPLETED), str(JobStatus.NEEDS_REVIEW),
             str(JobStatus.VISUAL_TEXT_ONLY))).fetchall()
        durations = []
        for row in rows:
            start = _parse_heartbeat(row["created_at"])
            end = _parse_heartbeat(row["updated_at"])
            if start is None or end is None:
                continue
            try:
                delta = end.timestamp() - start.timestamp()
            except (OSError, OverflowError, ValueError):
                continue
            if delta > 1:
                durations.append(delta)
        per_video = (sum(durations) / len(durations)) if durations \
            else float(DEFAULT_SECONDS_PER_VIDEO)
        return int(round(new_count * per_video))

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get_batch(self, batch_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM batches WHERE batch_id = ?",
                                 (str(batch_id),)).fetchone()
        return dict(row) if row is not None else None

    def get_job(self, job_id: str) -> JobRecord | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?",
                                 (str(job_id),)).fetchone()
        return JobRecord.from_row(row) if row is not None else None

    def list_jobs(self, batch_id: str) -> list[JobRecord]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE batch_id = ? ORDER BY position",
            (str(batch_id),)).fetchall()
        return [JobRecord.from_row(row) for row in rows]

    def find_job_by_aweme_id(self, batch_id: str, aweme_id: str) -> JobRecord | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE batch_id = ? AND aweme_id = ? "
            "AND status NOT IN (?, ?) ORDER BY position LIMIT 1",
            (str(batch_id), str(aweme_id), str(JobStatus.DUPLICATE),
             str(JobStatus.FAILED))).fetchone()
        return JobRecord.from_row(row) if row is not None else None

    def batch_totals(self, batch_id: str) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS total FROM jobs WHERE batch_id = ? "
            "GROUP BY status", (str(batch_id),)).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows
                if int(row["total"]) > 0}

    # ------------------------------------------------------------------
    # 领取与状态推进
    # ------------------------------------------------------------------

    def claim_next(self, batch_id: str) -> JobRecord | None:
        """串行领取：只取最小 position 的一条。认证暂停时拒绝领取。"""
        batch = self.get_batch(batch_id)
        if batch is None or batch["status"] not in (str(BatchStatus.PENDING),
                                                    str(BatchStatus.RUNNING)):
            return None
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE batch_id = ? AND status IN (?, ?) "
            "ORDER BY position LIMIT 1",
            (str(batch_id), *_CLAIMABLE_STATUSES)).fetchone()
        if row is None:
            return None
        now = utc_now()
        with self._conn:
            self._conn.execute(
                "UPDATE jobs SET status = ?, stage = ?, heartbeat_at = ?, "
                "updated_at = ? WHERE job_id = ?",
                (str(JobStatus.RESOLVING), str(Stage.RESOLVING), now, now,
                 str(row["job_id"])))
            self._conn.execute(
                "UPDATE batches SET status = ?, updated_at = ? WHERE batch_id = ?",
                (str(BatchStatus.RUNNING), now, str(batch_id)))
        return self.get_job(str(row["job_id"]))

    def heartbeat(self, job_id: str) -> None:
        now = utc_now()
        self._conn.execute(
            "UPDATE jobs SET heartbeat_at = ?, updated_at = ? WHERE job_id = ?",
            (now, now, str(job_id)))
        self._conn.commit()

    def update_status(self, job_id: str, status: JobStatus | str, *,
                      stage: Stage | str | None = None, aweme_id: str | None = None,
                      failure_code: str | None = None,
                      failure_message: str | None = None,
                      checkpoint: dict | None = None) -> JobRecord:
        """状态迁移必须符合设计文档 15.2；非法回退直接拒绝。

        同态写入（target == current）不是状态迁移，而是 stage/checkpoint/心跳
        的落盘：非终态允许，终态仍拒绝。
        """
        job = self.get_job(job_id)
        if job is None:
            raise ValueError("job 不存在")
        target = str(status)
        if target == str(job.status):
            if str(job.status) in {str(s) for s in TERMINAL_JOB_STATUSES}:
                raise ValueError(f"非法状态迁移：{job.status} → {target}")
        elif not can_transition(job.status, target):
            raise ValueError(f"非法状态迁移：{job.status} → {target}")
        now = utc_now()
        assignments = ["status = ?", "heartbeat_at = ?", "updated_at = ?"]
        params: list[object] = [target, now, now]
        if stage is not None:
            assignments.append("stage = ?")
            params.append(str(stage))
        if aweme_id is not None:
            assignments.append("aweme_id = ?")
            params.append(str(aweme_id))
        if failure_code is not None:
            assignments.append("failure_code = ?")
            params.append(str(failure_code))
        if failure_message is not None:
            assignments.append("failure_message = ?")
            params.append(str(failure_message))
        if checkpoint is not None:
            assignments.append("checkpoint_json = ?")
            params.append(json.dumps(checkpoint, ensure_ascii=False))
        params.append(str(job_id))
        with self._conn:
            self._conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?",
                params)
        return self.get_job(job_id)

    def finish_job(self, job_id: str, status: JobStatus | str) -> JobRecord:
        job = self.update_status(job_id, status)
        self._refresh_batch_status(job.batch_id)
        return job

    def _refresh_batch_status(self, batch_id: str) -> None:
        batch = self.get_batch(batch_id)
        if batch is None or batch["status"] not in (str(BatchStatus.PENDING),
                                                    str(BatchStatus.RUNNING)):
            return
        remaining = self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND status NOT IN ({})"
            .format(", ".join("?" for _ in TERMINAL_JOB_STATUSES)),
            (str(batch_id), *[str(status) for status in TERMINAL_JOB_STATUSES])
        ).fetchone()[0]
        if remaining:
            return
        totals = self.batch_totals(batch_id)
        failed = totals.get(str(JobStatus.FAILED), 0) + \
            totals.get(str(JobStatus.AUTH_REQUIRED), 0)
        status = BatchStatus.COMPLETED_WITH_ERRORS if failed else BatchStatus.COMPLETED
        now = utc_now()
        self._conn.execute(
            "UPDATE batches SET status = ?, counts_json = ?, updated_at = ? "
            "WHERE batch_id = ?",
            (str(status), json.dumps(totals, ensure_ascii=False), now,
             str(batch_id)))
        self._conn.commit()
        # 批次终态：裁剪原始输入，去掉查询串并限制长度
        self.trim_raw_inputs(batch_id)

    def trim_raw_inputs(self, batch_id: str) -> None:
        rows = self._conn.execute(
            "SELECT job_id, raw_input FROM jobs WHERE batch_id = ?",
            (str(batch_id),)).fetchall()
        changed = [(trim_raw_input(row["raw_input"]), str(row["job_id"]))
                   for row in rows
                   if trim_raw_input(row["raw_input"]) != row["raw_input"]]
        if not changed:
            return
        with self._conn:
            self._conn.executemany(
                "UPDATE jobs SET raw_input = ? WHERE job_id = ?", changed)

    # ------------------------------------------------------------------
    # 去重、认证与中断恢复
    # ------------------------------------------------------------------

    def resolve_aweme_id(self, job_id: str, aweme_id: str) -> JobRecord | None:
        """短链展开后按真实 aweme_id 二次去重；命中则返回主任务。"""
        job = self.get_job(job_id)
        if job is None:
            raise ValueError("job 不存在")
        aweme_id = str(aweme_id)
        master = self.find_job_by_aweme_id(job.batch_id, aweme_id)
        if master is not None and master.job_id != job.job_id:
            self.update_status(job_id, JobStatus.DUPLICATE, aweme_id=aweme_id,
                               checkpoint={**job.checkpoint,
                                           "duplicate_of": master.job_id})
            self._conn.execute(
                "UPDATE jobs SET duplicate_of_job_id = ? WHERE job_id = ?",
                (master.job_id, str(job_id)))
            self._conn.commit()
            return master
        now = utc_now()
        self._conn.execute(
            "UPDATE jobs SET aweme_id = ?, heartbeat_at = ?, updated_at = ? "
            "WHERE job_id = ?", (aweme_id, now, now, str(job_id)))
        self._conn.commit()
        return None

    def pause_for_auth(self, job_id: str) -> None:
        """认证失效：暂停整个批次，后续 job 不再领取。"""
        job = self.update_status(job_id, JobStatus.AUTH_REQUIRED,
                                 failure_code="login_required",
                                 failure_message="需要专用 Chromium 扫码登录")
        now = utc_now()
        self._conn.execute(
            "UPDATE batches SET status = ?, updated_at = ? WHERE batch_id = ?",
            (str(BatchStatus.PAUSED_AUTH), now, str(job.batch_id)))
        self._conn.commit()

    def resume_batch(self, batch_id: str) -> dict:
        """登录完成后从被打断的那条继续；此前完成项不重跑。"""
        batch = self.get_batch(batch_id)
        if batch is None:
            raise ValueError("批次不存在")
        if batch["status"] != str(BatchStatus.PAUSED_AUTH):
            raise ValueError("批次未处于认证暂停状态")
        rows = self._conn.execute(
            "SELECT job_id FROM jobs WHERE batch_id = ? AND status = ? "
            "ORDER BY position", (str(batch_id), str(JobStatus.AUTH_REQUIRED))
        ).fetchall()
        now = utc_now()
        with self._conn:
            for row in rows:
                self._conn.execute(
                    "UPDATE jobs SET status = ?, failure_code = '', "
                    "failure_message = '', updated_at = ? WHERE job_id = ?",
                    (str(JobStatus.RETRY_WAIT), now, str(row["job_id"])))
            self._conn.execute(
                "UPDATE batches SET status = ?, updated_at = ? WHERE batch_id = ?",
                (str(BatchStatus.RUNNING), now, str(batch_id)))
        return {"batch_id": str(batch_id), "resumed_jobs": len(rows),
                "status": str(BatchStatus.RUNNING)}

    def requeue_failed(self, batch_id: str) -> list[str]:
        """为重试失败项建立新的 job，保留原来的失败记录。"""
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE batch_id = ? AND status = ? ORDER BY position",
            (str(batch_id), str(JobStatus.FAILED))).fetchall()
        if not rows:
            return []
        next_position = self._conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM jobs WHERE batch_id = ?",
            (str(batch_id),)).fetchone()[0]
        now = utc_now()
        created: list[str] = []
        with self._conn:
            for index, row in enumerate(rows):
                job_id = str(uuid.uuid4())
                created.append(job_id)
                self._conn.execute(
                    "INSERT INTO jobs (job_id, batch_id, position, raw_input, "
                    "normalized_input, candidate_aweme_id, aweme_id, status, "
                    "stage, duplicate_of_job_id, failure_code, failure_message, "
                    "checkpoint_json, heartbeat_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', '{}', '', ?, ?)",
                    (job_id, str(batch_id), int(next_position) + index,
                     str(row["raw_input"]), str(row["normalized_input"]),
                     str(row["candidate_aweme_id"]), str(row["aweme_id"]),
                     str(JobStatus.PENDING), str(Stage.RESOLVING), now, now))
            self._conn.execute(
                "UPDATE batches SET status = ?, updated_at = ? WHERE batch_id = ?",
                (str(BatchStatus.RUNNING), now, str(batch_id)))
        return created

    def recover_stale_jobs(self, *, timeout_seconds: int = STALE_HEARTBEAT_SECONDS
                           ) -> list[JobRecord]:
        """心跳过期的进行中 job 视为中断，带着 checkpoint 回到可领取状态。

        心跳格式混用（ISO T 串 / 空格串 / float 时间戳），SQL 字符串比较会误判，
        因此全量取出进行中 job 后在 Python 侧用统一解析按 float 超时判断。
        空心跳与不可解析心跳一律视为中断，避免僵尸 job 永久卡住。
        """
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE status IN ({}) ORDER BY position".format(
                ", ".join("?" for _ in PROCESSING_JOB_STATUSES)),
            tuple(str(status) for status in PROCESSING_JOB_STATUSES)
        ).fetchall()
        stale_rows = []
        for row in rows:
            heartbeat = row["heartbeat_at"]
            if heartbeat is None or str(heartbeat).strip() == "":
                stale_rows.append(row)
                continue
            age = _heartbeat_age_seconds(heartbeat)
            if age is None or age > timeout_seconds:
                stale_rows.append(row)
        recovered = [JobRecord.from_row(row) for row in stale_rows]
        if not recovered:
            return []
        now = utc_now()
        with self._conn:
            for job in recovered:
                self._conn.execute(
                    "UPDATE jobs SET status = ?, heartbeat_at = ?, updated_at = ? "
                    "WHERE job_id = ?",
                    (str(JobStatus.RETRY_WAIT), now, now, job.job_id))
        return recovered


def trim_raw_input(text: str, *, limit: int = 2000) -> str:
    """批次完成后裁剪原始输入并去掉查询串，避免长期留存跟踪参数。"""
    return strip_query_parameters(text)[:limit]
