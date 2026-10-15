"""SQLite 事实源：转录记录的持久化、v2 版本仓库与发布事务。

数据库是唯一事实源；TXT 只能由数据库渲染。所有 SQL 使用参数化。
v1 `transcripts` 表保留可读（迁移不删除）；v2 以 `transcript_versions`
为核心，只有完整校验过的版本才能成为 current。
实例持有一条 WAL 连接；跨线程复用时为每个线程建立独立实例。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from scripts.migrations import ensure_v2_schema, file_sha256, json_dumps
from scripts.models import (CURRENT_PROCESSING_SIGNATURE, LEGACY_PROCESSING_SIGNATURE,
                            ArtifactKind, CorrectionStatus, LifecycleStatus,
                            PreparedArtifact, QualityStatus, ReuseAction,
                            SummaryMode, SummaryStatus, utc_now)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aweme_id TEXT NOT NULL UNIQUE,
    original_input TEXT NOT NULL DEFAULT '',
    canonical_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    publish_date TEXT NOT NULL DEFAULT '',
    duration_seconds REAL NOT NULL DEFAULT 0,
    transcript_source TEXT NOT NULL DEFAULT '',
    whisper_model TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    transcript_text TEXT NOT NULL DEFAULT '',
    segments_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    summary_status TEXT NOT NULL DEFAULT 'pending',
    transcript_file TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'fetching',
    failure_code TEXT NOT NULL DEFAULT '',
    failure_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_status ON transcripts(status);
CREATE INDEX IF NOT EXISTS idx_transcripts_summary_status ON transcripts(summary_status);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class PublishError(RuntimeError):
    """发布校验失败：旧 current 必须保持可用，绝不半发布。"""


class TranscriptDB:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        ensure_v2_schema(self._conn)
        self._conn.commit()

    def connect(self) -> sqlite3.Connection:
        """返回共享连接；可用 with 做事务提交，但不要调用 close。"""
        return self._conn

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _to_dict(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None

    def get_by_aweme_id(self, aweme_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM transcripts WHERE aweme_id = ?", (str(aweme_id),)
        ).fetchone()
        return self._to_dict(row)

    def get_reusable(self, aweme_id: str) -> dict | None:
        """仅完整转录且 TXT 实际存在的记录可复用。"""
        record = self.get_by_aweme_id(aweme_id)
        if record is None:
            return None
        if record["status"] not in ("transcribed", "summarized"):
            return None
        if not record["transcript_text"]:
            return None
        if not record["transcript_file"] or not Path(record["transcript_file"]).is_file():
            return None
        return record

    def mark_audio_ready(self, aweme_id: str) -> None:
        self._conn.execute(
            "UPDATE transcripts SET status = 'audio_ready', updated_at = ? "
            "WHERE aweme_id = ? AND status NOT IN ('transcribed', 'summarized')",
            (_now(), str(aweme_id)),
        )
        self._conn.commit()

    def upsert_fetching(
        self,
        *,
        aweme_id: str,
        original_input: str,
        canonical_url: str,
        metadata: dict,
    ) -> None:
        now = _now()
        self._conn.execute(
            """
            INSERT INTO transcripts (
                aweme_id, original_input, canonical_url, title, author,
                publish_date, duration_seconds, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'fetching', ?, ?)
                ON CONFLICT(aweme_id) DO UPDATE SET
                    original_input = excluded.original_input,
                    canonical_url = excluded.canonical_url,
                    title = excluded.title,
                    author = excluded.author,
                    publish_date = excluded.publish_date,
                    duration_seconds = excluded.duration_seconds,
                    status = CASE WHEN status IN ('transcribed', 'summarized')
                                  THEN status ELSE 'fetching' END,
                    updated_at = excluded.updated_at
            """,
            (
                str(aweme_id),
                original_input,
                canonical_url,
                str(metadata.get("title", "") or ""),
                str(metadata.get("author", "") or ""),
                str(metadata.get("publish_date", "") or ""),
                float(metadata.get("duration_seconds", 0) or 0),
                now,
                now,
            ),
        )
        self._conn.commit()

    def mark_transcribed(
        self,
        aweme_id: str,
        *,
        transcript_text: str,
        segments_json: str,
        transcript_source: str,
        whisper_model: str,
        language: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE transcripts SET
                transcript_text = ?, segments_json = ?,
                transcript_source = ?, whisper_model = ?, language = ?,
                summary = '', summary_status = 'pending',
                failure_code = '', failure_message = '',
                status = 'transcribed', updated_at = ?
            WHERE aweme_id = ?
            """,
            (
                transcript_text,
                segments_json,
                transcript_source,
                whisper_model,
                language,
                _now(),
                str(aweme_id),
            ),
        )
        self._conn.commit()

    def mark_summary_pending(self, aweme_id: str) -> None:
        self._conn.execute(
            "UPDATE transcripts SET summary_status = 'pending', updated_at = ? "
            "WHERE aweme_id = ?",
            (_now(), str(aweme_id)),
        )
        self._conn.commit()

    def mark_summarized(self, aweme_id: str, summary: str) -> None:
        self._conn.execute(
            "UPDATE transcripts SET summary = ?, summary_status = 'summarized', "
            "status = 'summarized', updated_at = ? WHERE aweme_id = ?",
            (summary, _now(), str(aweme_id)),
        )
        self._conn.commit()

    def mark_failed(self, aweme_id: str, code: str, message: str) -> None:
        """记录失败；绝不擦除已有的完整可复用记录。"""
        aweme_id = str(aweme_id)
        record = self.get_by_aweme_id(aweme_id)
        if record is not None and record["status"] in ("transcribed", "summarized") \
                and record["transcript_text"]:
            return
        now = _now()
        if record is None:
            self._conn.execute(
                "INSERT INTO transcripts (aweme_id, status, failure_code, "
                "failure_message, created_at, updated_at) "
                "VALUES (?, 'failed', ?, ?, ?, ?)",
                (aweme_id, code, message, now, now),
            )
        else:
            self._conn.execute(
                "UPDATE transcripts SET status = 'failed', failure_code = ?, "
                "failure_message = ?, updated_at = ? WHERE aweme_id = ?",
                (code, message, now, aweme_id),
            )
        self._conn.commit()

    def set_transcript_file(self, aweme_id: str, path: str) -> None:
        self._conn.execute(
            "UPDATE transcripts SET transcript_file = ?, updated_at = ? "
            "WHERE aweme_id = ?",
            (str(path), _now(), str(aweme_id)),
        )
        self._conn.commit()

    def pending_summaries(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM transcripts WHERE summary_status = 'pending' "
            "AND status IN ('transcribed', 'summarized') "
            "AND transcript_text != '' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        def _count(sql: str) -> int:
            return self._conn.execute(sql).fetchone()[0]

        return {
            "total": _count("SELECT COUNT(*) FROM transcripts"),
            "summarized": _count(
                "SELECT COUNT(*) FROM transcripts WHERE status = 'summarized'"),
            "pending_summary": len(self.pending_summaries()),
            "failed": _count("SELECT COUNT(*) FROM transcripts WHERE status = 'failed'"),
        }

    # ------------------------------------------------------------------
    # v2 版本仓库（设计文档 14）
    # ------------------------------------------------------------------

    def get_video(self, aweme_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM videos WHERE aweme_id = ?", (str(aweme_id),)).fetchone()
        return self._to_dict(row)

    def get_version(self, version_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM transcript_versions WHERE version_id = ?",
            (int(version_id),)).fetchone()
        return self._to_dict(row)

    def list_versions(self, aweme_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM transcript_versions WHERE aweme_id = ? "
            "ORDER BY version_no", (str(aweme_id),)).fetchall()
        return [dict(row) for row in rows]

    def create_staging_version(self, aweme_id: str, *, metadata: dict | None = None,
                               processing_signature: str = CURRENT_PROCESSING_SIGNATURE,
                               quality_status: str = QualityStatus.NEEDS_REVIEW,
                               ) -> int:
        """建立 staging 版本；绝不改变 `videos.current_version_id`。"""
        aweme_id = str(aweme_id)
        metadata = metadata or {}
        now = utc_now()
        self._conn.execute(
            """
            INSERT INTO videos (aweme_id, canonical_url, title, author,
                                publish_date, duration_seconds, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(aweme_id) DO UPDATE SET
                canonical_url = CASE WHEN excluded.canonical_url != ''
                                     THEN excluded.canonical_url
                                     ELSE videos.canonical_url END,
                title = CASE WHEN excluded.title != '' THEN excluded.title
                             ELSE videos.title END,
                author = CASE WHEN excluded.author != '' THEN excluded.author
                              ELSE videos.author END,
                publish_date = CASE WHEN excluded.publish_date != ''
                                    THEN excluded.publish_date
                                    ELSE videos.publish_date END,
                duration_seconds = CASE WHEN excluded.duration_seconds > 0
                                        THEN excluded.duration_seconds
                                        ELSE videos.duration_seconds END,
                updated_at = excluded.updated_at
            """,
            (aweme_id, str(metadata.get("canonical_url", "") or ""),
             str(metadata.get("title", "") or ""),
             str(metadata.get("author", "") or ""),
             str(metadata.get("publish_date", "") or ""),
             float(metadata.get("duration_seconds", 0) or 0), now, now),
        )
        next_no = self._conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM transcript_versions "
            "WHERE aweme_id = ?", (aweme_id,)).fetchone()[0]
        cursor = self._conn.execute(
            """
            INSERT INTO transcript_versions (
                aweme_id, version_no, lifecycle_status, quality_status,
                processing_signature, is_current, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (aweme_id, int(next_no), str(LifecycleStatus.STAGING),
             str(quality_status), str(processing_signature), now),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def set_raw_text(self, version_id: int, raw_text: str) -> None:
        self._conn.execute(
            "UPDATE transcript_versions SET raw_text = ? WHERE version_id = ?",
            (str(raw_text), int(version_id)))
        self._conn.commit()

    def set_quality(self, version_id: int, *, status: str, score: float,
                    report: object | None = None) -> None:
        self._conn.execute(
            "UPDATE transcript_versions SET quality_status = ?, quality_score = ?, "
            "quality_report_json = ? WHERE version_id = ?",
            (str(status), float(score), json_dumps(report or {}), int(version_id)))
        self._conn.commit()

    def add_asr_attempt(self, version_id: int, *, attempt_no: int,
                        audio_variant: str, strategy: object, model: str,
                        device: str, compute_type: str, language: str,
                        transcript_text: str, segments_json: object,
                        metrics: object, hard_failures: object, score: float,
                        passed: bool, error_code: str = "",
                        started_at: str = "", finished_at: str = "") -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO asr_attempts (
                version_id, attempt_no, audio_variant, strategy_json, model,
                device, compute_type, language, transcript_text, segments_json,
                metrics_json, hard_failures_json, score, passed, error_code,
                started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(version_id), int(attempt_no), str(audio_variant),
             json_dumps(strategy), str(model), str(device), str(compute_type),
             str(language), transcript_text,
             segments_json if isinstance(segments_json, str)
             else json_dumps(segments_json),
             json_dumps(metrics), json_dumps(hard_failures), float(score),
             1 if passed else 0, str(error_code), str(started_at),
             str(finished_at)),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def set_attempt_speakers(self, attempt_id: int, segments: Sequence[dict]) -> None:
        """把匿名说话人引用写回片段记录（设计文档 11.1 要求段内保留说话人引用）。

        只更新说话人字段，文本与时间保持原样。
        """
        payload = json_dumps(list(segments))
        self._conn.execute(
            "UPDATE asr_attempts SET segments_json = ? WHERE attempt_id = ?",
            (payload, int(attempt_id)))
        self._conn.commit()

    def get_attempt(self, attempt_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM asr_attempts WHERE attempt_id = ?",
            (int(attempt_id),)).fetchone()
        return self._to_dict(row)

    def list_attempts(self, version_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM asr_attempts WHERE version_id = ? ORDER BY attempt_no",
            (int(version_id),)).fetchall()
        return [dict(row) for row in rows]

    def select_attempt(self, version_id: int, attempt_id: int) -> None:
        """把某个 attempt 的文本变成版本正式原始稿；原始稿永不被 AI 覆盖。"""
        attempt = self.get_attempt(attempt_id)
        if attempt is None or int(attempt["version_id"]) != int(version_id):
            raise ValueError("attempt 不属于该版本")
        self._conn.execute(
            "UPDATE transcript_versions SET selected_attempt_id = ?, raw_text = ? "
            "WHERE version_id = ?",
            (int(attempt_id), str(attempt["transcript_text"]), int(version_id)))
        self._conn.commit()

    def replace_ocr_observations(self, version_id: int,
                                 observations: Iterable[dict]) -> None:
        version_id = int(version_id)
        with self._conn:
            self._conn.execute("DELETE FROM ocr_observations WHERE version_id = ?",
                               (version_id,))
            for item in observations:
                self._conn.execute(
                    "INSERT INTO ocr_observations (version_id, start_seconds, "
                    "end_seconds, text, bbox_json, confidence, kind, frame_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (version_id, float(item.get("start", 0) or 0),
                     float(item.get("end", 0) or 0), str(item.get("text", "") or ""),
                     json_dumps(item.get("bbox", [])),
                     float(item.get("confidence", 0) or 0),
                     str(item.get("kind", "") or ""),
                     str(item.get("frame_hash", "") or "")))

    def list_ocr_observations(self, version_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM ocr_observations WHERE version_id = ? "
            "ORDER BY start_seconds, ocr_id", (int(version_id),)).fetchall()
        return [dict(row) for row in rows]

    def replace_speaker_turns(self, version_id: int, turns: Iterable[dict]) -> None:
        version_id = int(version_id)
        with self._conn:
            self._conn.execute("DELETE FROM speaker_turns WHERE version_id = ?",
                               (version_id,))
            for turn in turns:
                self._conn.execute(
                    "INSERT INTO speaker_turns (version_id, start_seconds, "
                    "end_seconds, speaker_label, confidence) VALUES (?, ?, ?, ?, ?)",
                    (version_id, float(turn.get("start", 0) or 0),
                     float(turn.get("end", 0) or 0),
                     str(turn.get("speaker_label", "") or ""),
                     float(turn.get("confidence", 0) or 0)))

    def list_speaker_turns(self, version_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM speaker_turns WHERE version_id = ? "
            "ORDER BY start_seconds, turn_id", (int(version_id),)).fetchall()
        return [dict(row) for row in rows]

    def save_correction(self, version_id: int, *, corrected_text: str,
                        changes: Sequence[dict],
                        status: str = CorrectionStatus.CORRECTED) -> None:
        version_id = int(version_id)
        with self._conn:
            self._conn.execute(
                "UPDATE transcript_versions SET corrected_text = ?, "
                "correction_status = ? WHERE version_id = ?",
                (str(corrected_text), str(status), version_id))
            self._conn.execute(
                "DELETE FROM correction_changes WHERE version_id = ?", (version_id,))
            for change in changes:
                self._conn.execute(
                    "INSERT INTO correction_changes (version_id, source_segment_ref, "
                    "original_text, corrected_text, change_type, evidence_json, "
                    "confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (version_id, str(change.get("source_segment_ref", "") or ""),
                     str(change.get("original_text", "") or ""),
                     str(change.get("corrected_text", "") or ""),
                     str(change.get("change_type", "") or ""),
                     json_dumps(change.get("evidence", {})),
                     float(change.get("confidence", 0) or 0)))

    def list_correction_changes(self, version_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM correction_changes WHERE version_id = ? "
            "ORDER BY change_id", (int(version_id),)).fetchall()
        return [dict(row) for row in rows]

    def save_summary(self, version_id: int, *, summary: str, status: str,
                     mode: str = SummaryMode.NORMAL, meta: object | None = None) -> None:
        self._conn.execute(
            "UPDATE transcript_versions SET summary = ?, summary_status = ?, "
            "summary_mode = ?, summary_meta_json = ? WHERE version_id = ?",
            (str(summary), str(status), str(mode), json_dumps(meta or {}),
             int(version_id)))
        self._conn.commit()

    def list_artifacts(self, version_id: int,
                       kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM artifacts WHERE version_id = ?"
        params: list[object] = [int(version_id)]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(str(kind))
        sql += " ORDER BY artifact_id"
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def get_current_version(self, aweme_id: str) -> dict | None:
        """返回 current 版本及其 artifacts（按 kind 索引）。"""
        row = self._conn.execute(
            "SELECT * FROM transcript_versions WHERE aweme_id = ? AND is_current = 1",
            (str(aweme_id),)).fetchone()
        if row is None:
            return None
        version = dict(row)
        version["artifacts"] = {
            str(item["kind"]): item
            for item in self.list_artifacts(version["version_id"])}
        return version

    def artifacts_verified(self, version: dict) -> bool:
        """主文件与证据文件都存在且哈希、大小与库内记录一致。"""
        artifacts = version.get("artifacts") or {}
        for kind in (ArtifactKind.MAIN_TXT, ArtifactKind.EVIDENCE_TXT):
            artifact = artifacts.get(str(kind))
            if not artifact or not artifact.get("is_current"):
                return False
            path = Path(str(artifact.get("path", "")))
            if not path.is_file():
                return False
            if path.stat().st_size != int(artifact.get("size_bytes") or 0):
                return False
            if file_sha256(path) != str(artifact.get("sha256") or ""):
                return False
        return True

    def find_reuse_action(self, aweme_id: str, *,
                          reprocess_review: bool = False) -> ReuseAction:
        """复用判定，见设计文档 14.3。"""
        version = self.get_current_version(aweme_id)
        if version is None or not str(version.get("raw_text") or "").strip():
            return ReuseAction.FETCH
        if not self.artifacts_verified(version):
            return ReuseAction.RENDER_ONLY
        if version["processing_signature"] == LEGACY_PROCESSING_SIGNATURE:
            # v1 记录没有校正版，已按原始稿交付；补 AI 由 maintenance 显式触发
            return ReuseAction.REUSE
        if version["correction_status"] in (str(CorrectionStatus.PENDING),
                                            str(CorrectionStatus.FAILED_PENDING_RETRY)) \
                or version["summary_status"] == str(SummaryStatus.PENDING):
            return ReuseAction.FILL_AI
        if (version["quality_status"] == str(QualityStatus.NEEDS_REVIEW)
                and reprocess_review
                and version["processing_signature"] != CURRENT_PROCESSING_SIGNATURE):
            return ReuseAction.REPROCESS
        return ReuseAction.REUSE

    def publish_version(self, version_id: int,
                        prepared: Sequence[PreparedArtifact], *,
                        verifier: Callable[[PreparedArtifact], None] | None = None
                        ) -> None:
        """校验两个交付文件后，在一个事务里原子切换 current 版本。"""
        version = self.get_version(version_id)
        if version is None:
            raise PublishError("版本不存在，拒绝发布")
        kinds = {str(item.kind) for item in prepared}
        if kinds != {str(ArtifactKind.MAIN_TXT), str(ArtifactKind.EVIDENCE_TXT)}:
            raise PublishError("发布必须同时提供主文件与证据文件")
        check = verifier or self._verify_artifact
        for artifact in prepared:
            check(artifact)

        aweme_id = str(version["aweme_id"])
        now = utc_now()
        with self._conn:
            self._conn.execute(
                "UPDATE transcript_versions SET is_current = 0 WHERE aweme_id = ? "
                "AND is_current = 1", (aweme_id,))
            self._conn.execute(
                "UPDATE artifacts SET is_current = 0 WHERE version_id IN "
                "(SELECT version_id FROM transcript_versions WHERE aweme_id = ?)",
                (aweme_id,))
            self._conn.execute(
                "DELETE FROM artifacts WHERE version_id = ? AND kind IN (?, ?)",
                (int(version_id), str(ArtifactKind.MAIN_TXT),
                 str(ArtifactKind.EVIDENCE_TXT)))
            by_kind = {str(item.kind): item for item in prepared}
            self._conn.execute(
                "UPDATE transcript_versions SET is_current = 1, lifecycle_status = ?, "
                "published_at = ?, main_file = ?, evidence_file = ? "
                "WHERE version_id = ?",
                (str(LifecycleStatus.PUBLISHED), now,
                 str(by_kind[str(ArtifactKind.MAIN_TXT)].path),
                 str(by_kind[str(ArtifactKind.EVIDENCE_TXT)].path), int(version_id)))
            for artifact in prepared:
                self._conn.execute(
                    "INSERT INTO artifacts (version_id, kind, path, sha256, "
                    "size_bytes, is_current, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (int(version_id), str(artifact.kind), str(artifact.path),
                     str(artifact.sha256), int(artifact.size_bytes), now))
            self._conn.execute(
                "UPDATE videos SET current_version_id = ?, updated_at = ? "
                "WHERE aweme_id = ?", (int(version_id), now, aweme_id))

    @staticmethod
    def _verify_artifact(artifact: PreparedArtifact) -> None:
        path = Path(str(artifact.path))
        if not path.is_file():
            raise PublishError("交付文件不存在")
        if path.stat().st_size != int(artifact.size_bytes):
            raise PublishError("交付文件大小与预期不一致")
        if file_sha256(path) != str(artifact.sha256):
            raise PublishError("交付文件哈希与预期不一致")
