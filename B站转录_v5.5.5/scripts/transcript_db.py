#!/usr/bin/env python3
"""DB 层唯一需要解释的设计：旧库自动迁移。其余行为由方法签名说明。"""

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent / ".db"
DB_PATH = DB_DIR / "transcripts.db"

_NEW_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bvid TEXT NOT NULL,
    page INTEGER NOT NULL DEFAULT 1,
    cid INTEGER,
    part_title TEXT DEFAULT '',
    url TEXT,
    title TEXT,
    author TEXT,
    duration TEXT,
    upload_date TEXT,
    transcript_source TEXT,
    transcript_file TEXT,
    transcript_text TEXT,
    summary TEXT,
    status TEXT DEFAULT 'transcribed',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(bvid, page)
)
"""

_COMPAT_COLUMNS = [
    ("url", "TEXT DEFAULT ''"),
    ("transcript_text", "TEXT DEFAULT ''"),
    ("page", "INTEGER NOT NULL DEFAULT 1"),
    ("cid", "INTEGER"),
    ("part_title", "TEXT DEFAULT ''"),
]


def _ensure_versioned_schema(conn) -> None:
    """AT-03：新库/旧库都必须带齐版本化表，否则写库入口必败。

    ensure_schema 幂等且只增不改（建缺失表/列，不碰既有行）。
    """
    try:
        from migrations import ensure_schema
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from migrations import ensure_schema
    ensure_schema(conn)


class TranscriptDB:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.row_factory = sqlite3.Row
        self._init_table()

    def _index_columns(self, index_name: str) -> list[str]:
        return [
            r[2]
            for r in self.conn.execute(f"PRAGMA index_info({index_name})").fetchall()
        ]

    def _has_old_bvid_unique(self) -> bool:
        for row in self.conn.execute("PRAGMA index_list(transcripts)").fetchall():
            if row[2] == 1 and self._index_columns(row[1]) == ["bvid"]:
                return True
        return False

    def _has_bvid_page_unique(self) -> bool:
        for row in self.conn.execute("PRAGMA index_list(transcripts)").fetchall():
            if row[2] == 1 and self._index_columns(row[1]) == ["bvid", "page"]:
                return True
        return False

    def _init_table(self):
        exists = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='transcripts'"
        ).fetchone()

        if not exists:
            self.conn.execute(_NEW_TABLE_SQL)
            self.conn.commit()
            _ensure_versioned_schema(self.conn)
            return

        original_cols = [
            r[1] for r in self.conn.execute("PRAGMA table_info(transcripts)").fetchall()
        ]
        for col, ddl in _COMPAT_COLUMNS:
            if col not in original_cols:
                self.conn.execute(f"ALTER TABLE transcripts ADD COLUMN {col} {ddl}")

        self.conn.execute("UPDATE transcripts SET page=1 WHERE page IS NULL")
        self.conn.commit()

        # 旧 UNIQUE(bvid) 会挡住 (bvid, page=2)，重建过程放进一个事务
        if "page" not in original_cols or self._has_old_bvid_unique():
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    "ALTER TABLE transcripts RENAME TO transcripts_old_v5"
                )
                self.conn.execute(_NEW_TABLE_SQL)
                self.conn.execute("""
                    INSERT INTO transcripts
                        (bvid, page, cid, part_title, url, title, author, duration,
                         upload_date, transcript_source, transcript_file,
                         transcript_text,
                         summary, status, created_at, updated_at)
                    SELECT
                        COALESCE(bvid, ''), COALESCE(page, 1), cid,
                        COALESCE(part_title, ''),
                        COALESCE(url, ''), COALESCE(title, ''), COALESCE(author, ''),
                        COALESCE(duration, ''), COALESCE(upload_date, ''),
                        COALESCE(transcript_source, ''), COALESCE(transcript_file, ''),
                        COALESCE(transcript_text, ''), COALESCE(summary, ''),
                        COALESCE(status, 'transcribed'),
                        COALESCE(created_at, datetime('now', 'localtime')),
                        COALESCE(updated_at, datetime('now', 'localtime'))
                    FROM transcripts_old_v5
                """)
                self.conn.execute("DROP TABLE transcripts_old_v5")
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        elif not self._has_bvid_page_unique():
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_bvid_page "
                "ON transcripts(bvid, page)"
            )
            self.conn.commit()

        _ensure_versioned_schema(self.conn)

    def insert(
        self,
        bvid: str,
        page: int = 1,
        cid: int | None = None,
        part_title: str = "",
        url: str = "",
        title: str = "",
        author: str = "",
        duration: str = "",
        upload_date: str = "",
        transcript_source: str = "",
        transcript_file: str = "",
        transcript_text: str = "",
        summary: str = "",
        status: str = "transcribed",
    ) -> None:
        page = int(page or 1)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            """
            INSERT INTO transcripts
                (bvid, page, cid, part_title, url, title, author, duration, upload_date,
                 transcript_source, transcript_file, transcript_text, summary, status,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bvid, page) DO UPDATE SET
                cid=excluded.cid,
                part_title=excluded.part_title,
                url=excluded.url,
                title=excluded.title,
                author=excluded.author,
                duration=excluded.duration,
                upload_date=excluded.upload_date,
                transcript_source=excluded.transcript_source,
                transcript_file=excluded.transcript_file,
                transcript_text=excluded.transcript_text,
                summary=excluded.summary,
                status=excluded.status,
                updated_at=excluded.updated_at
        """,
            (
                bvid,
                page,
                cid,
                part_title,
                url,
                title,
                author,
                duration,
                upload_date,
                transcript_source,
                transcript_file,
                transcript_text,
                summary,
                status,
                now,
                now,
            ),
        )
        self.conn.commit()

    def update_summary(self, bvid: str, page: int = 1, summary: str = "") -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE transcripts SET summary=?, status='summarized', "
            "updated_at=? WHERE bvid=? AND page=?",
            (summary, now, bvid, int(page or 1)),
        )
        self.conn.commit()

    def update_summary_by_file(self, transcript_file: str, summary: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE transcripts SET summary=?, status='summarized', "
            "updated_at=? WHERE transcript_file=?",
            (summary, now, transcript_file),
        )
        self.conn.commit()

    def get_pending_summaries(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM transcripts WHERE summary IS NULL OR summary = ''"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_by_bvid(self, bvid: str, page: int = 1) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM transcripts WHERE bvid=? AND page=?", (bvid, int(page or 1))
        ).fetchone()
        return dict(row) if row else None

    def get_all(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM transcripts ORDER BY created_at DESC, bvid, page"
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
        with_summary = self.conn.execute(
            "SELECT COUNT(*) FROM transcripts "
            "WHERE summary IS NOT NULL AND summary != ''"
        ).fetchone()[0]
        return {
            "total": total,
            "with_summary": with_summary,
            "pending_summary": total - with_summary,
        }

    def render_txt(
        self, bvid: str, page: int = 1, output_dir: str | Path | None = None,
        text_override: str | None = None, note: str = "",
    ) -> str | None:
        """渲染主文件。

        `text_override` 用于交付渲染：正文换成更优版本（如校对版）时，
        文件名与版式规则仍由本方法统一决定；`note` 会在头部追加一行醒目说明。
        """
        page = int(page or 1)
        record = self.get_by_bvid(bvid, page)
        if not record:
            return None

        output_root = (
            Path(output_dir)
            if output_dir
            else Path(__file__).resolve().parent.parent / "output"
        )
        upload_date = record.get("upload_date", "")
        if upload_date and upload_date != "未知时间":
            parts = upload_date.split("-")
            if len(parts) >= 2:
                output_root = output_root / parts[0] / parts[1]
        output_root.mkdir(parents=True, exist_ok=True)

        title = record.get("title", "untitled")
        author = record.get("author", "unknown")
        safe_title = re.sub(r'[\\/:*?"<>|]', "", title)
        safe_title = re.sub(r"[\s\W]+", "-", safe_title).strip("-")[:60] or "untitled"
        safe_author = re.sub(r'[\\/:*?"<>|]', "", author)
        safe_author = re.sub(r"[\s\W]+", "-", safe_author).strip("-")[:30] or "unknown"

        part_title = (record.get("part_title") or "").strip()
        safe_part = re.sub(r'[\\/:*?"<>|]', "", part_title)
        safe_part = re.sub(r"[\s\W]+", "-", safe_part).strip("-")[:20]

        page_tag = f"_P{page:02d}"
        part_tag = f"_{safe_part}" if safe_part else ""
        output_file = (
            output_root
            / f"{safe_title}_{safe_author}_{upload_date}_{record.get('bvid', '')}"
            f"{page_tag}{part_tag}.txt"
        )

        summary = record.get("summary", "") or ""
        summary_block = (
            summary.strip()
            if summary
            else "【AI待处理：请阅读全文后，替换此行，写结构化摘要】"
        )

        sep = "=" * 80
        note_line = f"📌 交付说明：{note}" if note else ""
        body_text = text_override if text_override is not None             else (record.get("transcript_text", "") or "")
        content = f"""{sep}
B站视频转录文档
{sep}

📹 视频标题：{record.get("title", "")}
🔗 B站链接：{record.get("url", "")}
👤 作者：{record.get("author", "")}
📅 发布时间：{record.get("upload_date", "")}
⏱️  视频时长：{record.get("duration", "")}
🎬 分P序号：P{page:02d}
📑 分P标题：{part_title or "（单P视频或无分P标题）"}
📝 转录来源：{record.get("transcript_source", "")}
⏰ 转录时间：{record.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}
{note_line}

================================================================================
第一部分：视频摘要（AI生成）
================================================================================

{summary_block}

================================================================================
第二部分：完整原文
================================================================================

{body_text}

{sep}
文档结束
================================================================================
"""

        output_file.write_text(content, encoding="utf-8")
        self.conn.execute(
            "UPDATE transcripts SET transcript_file=? WHERE bvid=? AND page=?",
            (str(output_file), bvid, page),
        )
        self.conn.commit()
        return str(output_file)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
