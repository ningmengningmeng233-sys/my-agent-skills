"""v1 → v2 数据库迁移与备份。

迁移必须无损、幂等、事务内完成：v1 `transcripts` 表原样保留，
v2 表按 `aweme_id` 建立 `videos` + 一个 `version_no=1` 的当前版本。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.models import (LEGACY_PROCESSING_SIGNATURE, ArtifactKind,
                            CorrectionStatus, LifecycleStatus, QualityStatus,
                            SummaryMode, SummaryStatus, utc_now)

SCHEMA_VERSION = 2
BACKUP_PREFIX = "transcripts-before-v2-"

_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    aweme_id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    publish_date TEXT NOT NULL DEFAULT '',
    duration_seconds REAL NOT NULL DEFAULT 0,
    current_version_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL DEFAULT 'chat',
    source_label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    requires_confirmation INTEGER NOT NULL DEFAULT 0,
    counts_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    batch_id TEXT REFERENCES batches(batch_id),
    position INTEGER NOT NULL DEFAULT 0,
    raw_input TEXT NOT NULL DEFAULT '',
    normalized_input TEXT NOT NULL DEFAULT '',
    candidate_aweme_id TEXT NOT NULL DEFAULT '',
    aweme_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    stage TEXT NOT NULL DEFAULT 'resolving',
    duplicate_of_job_id TEXT,
    failure_code TEXT NOT NULL DEFAULT '',
    failure_message TEXT NOT NULL DEFAULT '',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    heartbeat_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_batch_position ON jobs(batch_id, position);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_aweme ON jobs(aweme_id);

CREATE TABLE IF NOT EXISTS transcript_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    aweme_id TEXT NOT NULL REFERENCES videos(aweme_id),
    version_no INTEGER NOT NULL,
    lifecycle_status TEXT NOT NULL DEFAULT 'staging',
    quality_status TEXT NOT NULL DEFAULT 'needs_review',
    selected_attempt_id INTEGER,
    raw_text TEXT NOT NULL DEFAULT '',
    corrected_text TEXT NOT NULL DEFAULT '',
    correction_status TEXT NOT NULL DEFAULT 'pending',
    summary TEXT NOT NULL DEFAULT '',
    summary_status TEXT NOT NULL DEFAULT 'pending',
    summary_mode TEXT NOT NULL DEFAULT 'normal',
    summary_meta_json TEXT NOT NULL DEFAULT '{}',
    quality_score REAL NOT NULL DEFAULT 0,
    quality_report_json TEXT NOT NULL DEFAULT '{}',
    processing_signature TEXT NOT NULL DEFAULT '',
    main_file TEXT NOT NULL DEFAULT '',
    evidence_file TEXT NOT NULL DEFAULT '',
    is_current INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT '',
    UNIQUE(aweme_id, version_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_current
    ON transcript_versions(aweme_id) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS asr_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES transcript_versions(version_id),
    attempt_no INTEGER NOT NULL,
    audio_variant TEXT NOT NULL DEFAULT 'original',
    strategy_json TEXT NOT NULL DEFAULT '{}',
    model TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    compute_type TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    transcript_text TEXT NOT NULL DEFAULT '',
    segments_json TEXT NOT NULL DEFAULT '[]',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    hard_failures_json TEXT NOT NULL DEFAULT '[]',
    score REAL NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    UNIQUE(version_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS ocr_observations (
    ocr_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES transcript_versions(version_id),
    start_seconds REAL NOT NULL DEFAULT 0,
    end_seconds REAL NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    bbox_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'unknown_visual_text',
    frame_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ocr_version
    ON ocr_observations(version_id, start_seconds);

CREATE TABLE IF NOT EXISTS speaker_turns (
    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES transcript_versions(version_id),
    start_seconds REAL NOT NULL DEFAULT 0,
    end_seconds REAL NOT NULL DEFAULT 0,
    speaker_label TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_turns_version
    ON speaker_turns(version_id, start_seconds);

CREATE TABLE IF NOT EXISTS correction_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES transcript_versions(version_id),
    source_segment_ref TEXT NOT NULL DEFAULT '',
    original_text TEXT NOT NULL DEFAULT '',
    corrected_text TEXT NOT NULL DEFAULT '',
    change_type TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_changes_version
    ON correction_changes(version_id);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES transcript_versions(version_id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    is_current INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_current
    ON artifacts(version_id, kind) WHERE is_current = 1;
"""


def _json_default(obj):
    """numpy 标量（float32/int64 等）→ Python 原生类型；其它仍报错。"""
    converter = getattr(obj, "item", None)
    if callable(converter):
        try:
            return converter()
        except Exception:  # pragma: no cover - 非标量 numpy 对象
            pass
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def json_dumps(value: object) -> str:
    """所有 JSON 入库前统一转义策略，保证中文可读且可序列化。"""
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(source: Path, backup_dir: Path,
                    *, stamp: str | None = None) -> Path:
    """用 SQLite backup API 生成带时间戳的副本；绝不覆盖已有备份。"""
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"数据库不存在：{source}")
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{BACKUP_PREFIX}{stamp}.db"
    suffix = 2
    while target.exists():
        target = backup_dir / f"{BACKUP_PREFIX}{stamp}-{suffix}.db"
        suffix += 1
    source_conn = sqlite3.connect(str(source))
    try:
        target_conn = sqlite3.connect(str(target))
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()
    return target


def ensure_v2_schema(conn: sqlite3.Connection) -> None:
    """幂等建表：只创建缺失的表与索引，不改动任何既有数据。"""
    conn.executescript(_V2_SCHEMA)
    conn.commit()


@dataclass
class MigrationReport:
    """迁移结果：`migrated` 为本次真正写入的版本数。"""

    scanned: int = 0
    migrated: int = 0
    skipped: int = 0
    incomplete: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _legacy_summary_status(value: str) -> str:
    return (SummaryStatus.SUMMARIZED if value == SummaryStatus.SUMMARIZED
            else SummaryStatus.PENDING)


def _legacy_rows(conn: sqlite3.Connection) -> list[dict]:
    """不依赖调用方 row_factory，把 v1 行读成普通 dict。"""
    cursor = conn.execute("SELECT * FROM transcripts ORDER BY id")
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, raw)) for raw in cursor.fetchall()]


def _migrate_row(conn: sqlite3.Connection, row: dict) -> str:
    """迁移单条 v1 记录；返回 migrated / skipped / incomplete。调用方负责事务。"""
    aweme_id = str(row["aweme_id"])
    text = str(row["transcript_text"] or "")
    if not text.strip():
        return "incomplete"
    existing = conn.execute(
        "SELECT 1 FROM transcript_versions WHERE aweme_id = ? "
        "AND processing_signature = ?", (aweme_id, LEGACY_PROCESSING_SIGNATURE)
    ).fetchone()
    if existing is not None:
        return "skipped"

    now = utc_now()
    created = str(row["created_at"] or now)
    conn.execute(
        """
        INSERT INTO videos (aweme_id, canonical_url, title, author,
                            publish_date, duration_seconds, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(aweme_id) DO UPDATE SET
            canonical_url = excluded.canonical_url,
            title = excluded.title,
            author = excluded.author,
            publish_date = excluded.publish_date,
            duration_seconds = excluded.duration_seconds,
            updated_at = excluded.updated_at
        """,
        (aweme_id, str(row["canonical_url"] or ""), str(row["title"] or ""),
         str(row["author"] or ""), str(row["publish_date"] or ""),
         float(row["duration_seconds"] or 0), created, now),
    )
    # 该作品可能已经有 v2 版本：取下一个可用序号，绝不与既有版本冲突
    version_no = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) + 1 FROM transcript_versions "
        "WHERE aweme_id = ?", (aweme_id,)).fetchone()[0]
    # 已有 current（例如先跑过 v2）时，迁移记录只作为历史版本保留
    has_current = conn.execute(
        "SELECT 1 FROM transcript_versions WHERE aweme_id = ? AND is_current = 1",
        (aweme_id,)).fetchone() is not None
    is_current = 0 if has_current else 1
    cursor = conn.execute(
        """
        INSERT INTO transcript_versions (
            aweme_id, version_no, lifecycle_status, quality_status,
            raw_text, corrected_text, correction_status, summary,
            summary_status, summary_mode, summary_meta_json, quality_score,
            quality_report_json, processing_signature, main_file,
            evidence_file, is_current, created_at, published_at)
        VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, 0, '{}', ?, ?, '', ?, ?, ?)
        """,
        (aweme_id, int(version_no), str(LifecycleStatus.PUBLISHED),
         str(QualityStatus.NEEDS_REVIEW),
         text, str(CorrectionStatus.PENDING), str(row["summary"] or ""),
         _legacy_summary_status(str(row["summary_status"] or "")),
         str(SummaryMode.NORMAL),
         json_dumps({"migrated_from": "v1",
                     "whisper_model": str(row["whisper_model"] or ""),
                     "language": str(row["language"] or ""),
                     "transcript_source": str(row["transcript_source"] or ""),
                     "segments_json": str(row["segments_json"] or "[]")}),
         LEGACY_PROCESSING_SIGNATURE, str(row["transcript_file"] or ""),
         is_current, created, str(row["updated_at"] or now)),
    )
    version_id = int(cursor.lastrowid)
    if is_current:
        conn.execute("UPDATE videos SET current_version_id = ? WHERE aweme_id = ?",
                     (version_id, aweme_id))

    transcript_file = str(row["transcript_file"] or "")
    if transcript_file and Path(transcript_file).is_file():
        path = Path(transcript_file)
        conn.execute(
            "INSERT INTO artifacts (version_id, kind, path, sha256, size_bytes, "
            "is_current, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (version_id, ArtifactKind.MAIN_TXT, str(path), file_sha256(path),
             path.stat().st_size, is_current, now))
    return "migrated"


def migrate_to_v2(conn: sqlite3.Connection) -> MigrationReport:
    """把 v1 `transcripts` 全量迁移到 v2 表；整批在一个事务内完成。"""
    ensure_v2_schema(conn)
    if not _has_table(conn, "transcripts"):
        return MigrationReport()
    rows = _legacy_rows(conn)
    report = MigrationReport(scanned=len(rows))
    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # 显式控制事务边界
    try:
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            outcome = _migrate_row(conn, row)
            if outcome == "migrated":
                report.migrated += 1
            elif outcome == "skipped":
                report.skipped += 1
            else:
                report.incomplete += 1
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?) "
            "ON CONFLICT(version) DO NOTHING", (SCHEMA_VERSION, utc_now()))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = previous_isolation
    return report


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,)).fetchone() is not None


def main(argv: list[str] | None = None) -> int:
    from scripts import paths
    from scripts.logger import configure_logging, ensure_utf8_stdio

    ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="v1 → v2 数据库迁移")
    parser.add_argument("--db", default=str(paths.DB_PATH))
    parser.add_argument("--backup-dir", default=str(paths.DB_PATH.parent / "backups"))
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    paths.load_skill_env()
    configure_logging()

    db_path = Path(args.db)
    if not db_path.is_file():
        print(json.dumps({"ok": False, "error": {"code": "database_failed",
                                                 "message": "目标数据库不存在"}},
                         ensure_ascii=False))
        return 1
    backup = backup_database(db_path, Path(args.backup_dir))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        report = migrate_to_v2(conn)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()

    payload = {"ok": integrity == "ok", "db": str(db_path),
               "backup": str(backup), "integrity": integrity, **report.as_dict()}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
