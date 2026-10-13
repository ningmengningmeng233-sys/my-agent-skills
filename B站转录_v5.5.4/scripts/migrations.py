#!/usr/bin/env python3
"""数据库迁移与版本化（B站转录 · schema v3）。

在既有单表 schema 之上引入两个只增不改的结构：

- `schema_migrations`：记录已应用的 schema 版本，使迁移可幂等重跑；
- `transcript_versions`：把「原始稿」与「校对版」都作为版本保存，原始稿永不被覆盖；
- `correction_changes`：v3 新增，逐条记录受证据约束的校正改动（可追溯到证据）。

同时完成两项回填：

1. 为每个 `transcripts` 行建立 version_no=1、kind=raw 的版本（正文取库中现值）；
2. 把归档目录里的 `*_校对版.txt` 作为 kind=corrected 的派发版本入库。

    .venv/Scripts/python.exe scripts/migrations.py --dry-run --json
    .venv/Scripts/python.exe scripts/migrations.py --apply --json

契约：
- 默认 dry-run，未传 `--apply` 绝不写库；
- `--apply` 前自动备份数据库，备份失败即中止；
- 幂等：重复执行不重复建版本、不重复插校对版；
- 只新增表与行，不修改 `transcripts` 表的任何既有数据。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

try:
    from dotenv import load_dotenv

    load_dotenv(str(SKILL_DIR / ".env"))
except ImportError:
    pass

from transcript_db import DB_PATH  # noqa: E402
from logger import info  # noqa: E402
# 归档 TXT 的解析层与补录工具共用同一份实现，避免两套解析各自漂移。
from import_archive import (  # noqa: E402
    CORRECTED_MARK,
    PAGE_RE,
    SECTION_BODY,
    SECTION_END,
    SECTION_SUMMARY,
    _field,
    _first_bv,
    _section,
    archive_root,
)

SOURCE = "migrations.py"
SCHEMA_VERSION = 4

_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcript_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bvid TEXT NOT NULL,
    page INTEGER NOT NULL DEFAULT 1,
    version_no INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'raw',
    source_file TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    transcript_text TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    is_current INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(bvid, page, version_no)
);

CREATE INDEX IF NOT EXISTS idx_versions_key ON transcript_versions(bvid, page);
CREATE INDEX IF NOT EXISTS idx_versions_kind ON transcript_versions(kind);

CREATE TABLE IF NOT EXISTS correction_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bvid TEXT NOT NULL,
    page INTEGER NOT NULL DEFAULT 1,
    version_no INTEGER NOT NULL,
    segment_ref TEXT NOT NULL DEFAULT '',
    original TEXT NOT NULL DEFAULT '',
    corrected TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_changes_key ON correction_changes(bvid, page);

CREATE TABLE IF NOT EXISTS transcript_segments (
    segment_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bvid TEXT NOT NULL,
    page INTEGER NOT NULL DEFAULT 1,
    version_no INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    start_seconds REAL NOT NULL DEFAULT 0,
    end_seconds REAL NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    avg_logprob REAL,
    no_speech_prob REAL,
    created_at TEXT NOT NULL,
    UNIQUE(bvid, page, version_no, seq)
);

CREATE INDEX IF NOT EXISTS idx_segments_key ON transcript_segments(bvid, page);
"""


_ADDED_COLUMNS = (
    ("transcript_versions", "quality_score", "REAL"),
    ("transcript_versions", "metrics_json", "TEXT NOT NULL DEFAULT ''"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等建表与补列：只新增缺失的表、索引与列，不改动任何既有数据。"""
    conn.executescript(_V2_SCHEMA)
    for table, column, ddl in _ADDED_COLUMNS:
        if has_table(conn, table) and column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.commit()


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    if not has_table(conn, "schema_migrations"):
        return []
    return [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]


def backup_database(source: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = source.parent / "backups" / f"transcripts-before-migrate-{stamp}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def ensure_raw_version(conn: sqlite3.Connection, bvid: str, page: int) -> int:
    """确保该键的原始稿 v1 已存在（缺则从 transcripts 行物化），返回其版本号。

    派生版本、分段与质量分都以原始稿版本为锚点，缺锚点会导致版本号撞号。
    """
    row = conn.execute(
        "SELECT version_no FROM transcript_versions"
        " WHERE bvid=? AND page=? AND kind='raw' ORDER BY version_no LIMIT 1",
        (bvid, page),
    ).fetchone()
    if row:
        return int(row[0])
    record = conn.execute(
        "SELECT transcript_text, summary, transcript_file, created_at"
        " FROM transcripts WHERE bvid=? AND page=?", (bvid, page)).fetchone()
    if not record:
        return 0
    created = str(record[3] or "") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO transcript_versions
            (bvid, page, version_no, kind, source_file, source_hash,
             transcript_text, summary, is_current, created_at)
        VALUES (?, ?, 1, 'raw', ?, '', ?, ?, 1, ?)
        """,
        (bvid, page, record[2] or "", record[0] or "", record[1] or "", created),
    )
    return 1


def current_versions(conn: sqlite3.Connection) -> dict:
    """现有版本：{(bvid,page): [version_no...]}。"""
    if not has_table(conn, "transcript_versions"):
        return {}
    result: dict[tuple[str, int], list[int]] = {}
    for bvid, page, no in conn.execute(
        "SELECT bvid, page, version_no FROM transcript_versions"
    ):
        result.setdefault((bvid, int(page)), []).append(int(no))
    return result


def plan_raw_versions(conn: sqlite3.Connection) -> list[dict]:
    """为每个 transcripts 行准备 version_no=1 的原始稿版本。"""
    have = current_versions(conn)
    rows = conn.execute(
        """
        SELECT bvid, page, transcript_text, summary, transcript_file, created_at
        FROM transcripts
        """
    ).fetchall()
    plan = []
    for bvid, page, text, summary, file, created in rows:
        key = (bvid, int(page or 1))
        if have.get(key):
            continue
        plan.append(
            {
                "bvid": bvid,
                "page": int(page or 1),
                "version_no": 1,
                "kind": "raw",
                "source_file": file or "",
                "source_hash": "",
                "transcript_text": text or "",
                "summary": summary or "",
                "is_current": 1,
                "created_at": created or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return plan


def _corrected_hash(name: str) -> str:
    match = re.search(r"--([0-9a-f]{8,})_校对版", name)
    return match.group(1) if match else ""


def _corrected_created(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def plan_corrected_versions(
    conn: sqlite3.Connection, root: Path, raw_keys: set | None = None
) -> tuple[list, list]:
    """把归档目录里的校对版文件作为派生版本入库。返回 (计划, 无法归属的清单)。

    同一键的多份校对版副本按文件路径字典序依次分配 version_no，绝不重号。
    """
    have = current_versions(conn)
    raw_keys = raw_keys if raw_keys is not None else set()
    known = {(row[0], int(row[1] or 1)) for row in conn.execute(
        "SELECT bvid, page FROM transcripts")}
    seen = set()
    if has_table(conn, "transcript_versions"):
        seen = {(row[0], int(row[1]), row[2]) for row in conn.execute(
            "SELECT bvid, page, source_file FROM transcript_versions"
            " WHERE kind='corrected'")}

    next_no: dict = {}
    for key in set(have) | set(raw_keys):
        numbers = have.get(key, [])
        if numbers:
            next_no[key] = max(numbers) + 1
        else:
            # 该键尚无任何版本：原始稿将占用 1，派生版本从 2 开始
            next_no[key] = 2 if key in raw_keys else 1

    plan, orphans = [], []
    for path in sorted(root.rglob(f"*{CORRECTED_MARK}.txt")):
        text = path.read_text(encoding="utf-8", errors="replace")
        bvid = _first_bv(path.name, _field(text, "B站链接"), path.parent.name)
        page_match = PAGE_RE.search(path.name)
        if page_match:
            page = int(page_match.group(1)[1:])
        else:
            digits = re.sub(r"\D", "", _field(text, "分P序号"))
            page = int(digits) if digits else 1
        key = (bvid, page)
        if not bvid or key not in known:
            orphans.append({"path": str(path), "bvid": bvid, "page": page})
            continue
        if (bvid, page, str(path)) in seen:
            continue
        body = _section(text, SECTION_BODY, SECTION_END)
        if not body:
            orphans.append({"path": str(path), "bvid": bvid, "page": page,
                            "reason": "缺少正文"})
            continue
        version_no = next_no.get(key, 2)
        next_no[key] = version_no + 1
        plan.append(
            {
                "bvid": bvid,
                "page": page,
                "version_no": version_no,
                "kind": "corrected",
                "source_file": str(path),
                "source_hash": _corrected_hash(path.name),
                "transcript_text": body,
                "summary": _section(text, SECTION_SUMMARY, SECTION_BODY),
                "is_current": 0,
                "created_at": _corrected_created(path),
            }
        )
    return plan, orphans


INSERT_SQL = """
INSERT INTO transcript_versions
    (bvid, page, version_no, kind, source_file, source_hash,
     transcript_text, summary, is_current, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def apply_plan(conn: sqlite3.Connection, raw: list, corrected: list) -> int:
    with conn:  # 单事务：异常整体回滚
        for row in raw + corrected:
            conn.execute(
                INSERT_SQL,
                (
                    row["bvid"], row["page"], row["version_no"], row["kind"],
                    row["source_file"], row["source_hash"], row["transcript_text"],
                    row["summary"], row["is_current"], row["created_at"],
                ),
            )
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)"
            " ON CONFLICT(version) DO NOTHING",
            (SCHEMA_VERSION, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
    return len(raw) + len(corrected)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="数据库迁移与版本化（schema v2）")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写库（默认）")
    parser.add_argument("--apply", action="store_true", help="写入数据库")
    parser.add_argument("--json", action="store_true", help="输出一行 JSON")
    parser.add_argument("--db", help="数据库路径（默认技能库）")
    parser.add_argument("--root", help="归档根目录（默认取 BILI_ARCHIVE_DIR）")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else Path(DB_PATH)
    root = Path(args.root) if args.root else archive_root()
    if not db_path.is_file():
        print(f"❌ 数据库不存在：{db_path}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"❌ 归档目录不存在：{root}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        applied_before = applied_versions(conn)
        if args.apply:
            try:
                backup = backup_database(db_path)
            except OSError as exc:
                print(f"❌ 备份失败，未迁移：{exc}", file=sys.stderr)
                return 2
            ensure_schema(conn)
        else:
            backup = None

        raw = plan_raw_versions(conn) if args.apply else _dry_raw(db_path)
        raw_keys = {(row["bvid"], row["page"]) for row in raw}
        corrected, orphans = plan_corrected_versions(conn, root, raw_keys)

        report = {
            "dry_run": not args.apply,
            "db": str(db_path),
            "schema_version": SCHEMA_VERSION,
            "applied_before": applied_before,
            "transcripts_rows": conn.execute(
                "SELECT COUNT(*) FROM transcripts").fetchone()[0],
            "versions_before": conn.execute(
                "SELECT COUNT(*) FROM transcript_versions").fetchone()[0]
            if has_table(conn, "transcript_versions") else 0,
            "raw_to_insert": len(raw),
            "corrected_to_insert": len(corrected),
            "orphan_corrected": len(orphans),
            "orphans": orphans[:20],
            "backup": str(backup) if backup else "",
            "inserted": 0,
            "versions_after": 0,
        }

        if args.apply:
            report["inserted"] = apply_plan(conn, raw, corrected)
            report["versions_after"] = conn.execute(
                "SELECT COUNT(*) FROM transcript_versions").fetchone()[0]
            info(SOURCE, f"迁移完成：新增版本 {report['inserted']} 行，"
                         f"schema v{SCHEMA_VERSION}")
        else:
            report["versions_after"] = report["versions_before"]
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        mode = "写入" if args.apply else "预览"
        print(f"🗂️  数据库迁移（{mode}）  schema v{SCHEMA_VERSION}")
        print(f"   transcripts 行数：{report['transcripts_rows']}")
        print(f"   待建原始稿版本：{report['raw_to_insert']}")
        print(f"   待建校对版版本：{report['corrected_to_insert']}")
        print(f"   无法归属的校对版：{report['orphan_corrected']}")
        print(f"   版本表：{report['versions_before']} → {report['versions_after']} 行")
        if report["backup"]:
            print(f"   备份：{report['backup']}")
    return 0


def _dry_raw(db_path: Path) -> list:
    """预览模式的原始稿版本计划：只读估算待建版本数。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT bvid, page FROM transcripts").fetchall()
        have = set()
        if has_table(conn, "transcript_versions"):
            have = {(r[0], int(r[1])) for r in conn.execute(
                "SELECT bvid, page FROM transcript_versions WHERE kind='raw'")}
        return [
            {"bvid": row[0], "page": int(row[1] or 1)}
            for row in rows
            if (row[0], int(row[1] or 1)) not in have
        ]
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
