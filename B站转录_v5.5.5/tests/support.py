"""测试公共辅助：让测试永不写真实库、真实日志。"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent.parent
REAL_DB = SKILL_DIR / ".db" / "transcripts.db"


def make_raw_file(path: Path, *, bvid: str, page: int | None = None,
                  title: str = "示例标题", author: str = "示例作者",
                  upload_date: str = "2026-01-02", duration: str = "3分5秒",
                  source: str = "Whisper small 模型（GPU加速）",
                  transcribed_at: str = "2026-02-03 04:05:06",
                  body: str = "这是正文。", summary: str = "### 摘要\n- 要点",
                  include_page_field: bool = True,
                  link_bv: str | None = None) -> Path:
    """生成一份符合归档格式的 TXT，用于测试解析器。"""
    lines = [
        "=" * 80,
        "B站视频转录文档",
        "=" * 80,
        "",
        f"📹 视频标题：{title}",
        f"🔗 B站链接：https://www.bilibili.com/video/{link_bv or bvid}/",
        f"👤 作者：{author}",
        f"📅 发布时间：{upload_date}",
        f"⏱️  视频时长：{duration}",
    ]
    if page is not None and include_page_field:
        lines.append(f"🎬 分P序号：P{page:02d}")
        lines.append("📑 分P标题：示例分P")
    lines += [
        f"📝 转录来源：{source}",
        f"⏰ 转录时间：{transcribed_at}",
        "",
        "=" * 80,
        "第一部分：视频摘要（AI生成）",
        "=" * 80,
        "",
        summary,
        "",
        "=" * 80,
        "第二部分：完整原文",
        "=" * 80,
        "",
        body,
        "",
        "=" * 80,
        "文档结束",
        "=" * 80,
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_db(path: Path, rows: list[dict] | None = None) -> Path:
    """创建一份最小 transcripts 库（结构与生产一致）。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
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
    )
    conn.executescript(
        """
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
            quality_score REAL,
            metrics_json TEXT NOT NULL DEFAULT '',
            UNIQUE(bvid, page, version_no)
        );
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
        INSERT OR IGNORE INTO schema_migrations VALUES (4, '2026-01-01 00:00:00');
        """
    )
    for row in rows or []:
        conn.execute(
            """
            INSERT INTO transcripts
                (bvid, page, url, title, author, duration, upload_date,
                 transcript_source, transcript_file, transcript_text, summary,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["bvid"], row.get("page", 1),
                row.get("url", f"https://www.bilibili.com/video/{row['bvid']}/"),
                row.get("title", "示例标题"), row.get("author", "示例作者"),
                row.get("duration", "3分5秒"), row.get("upload_date", "2026-01-02"),
                row.get("transcript_source", "Whisper small 模型（GPU加速）"),
                row.get("transcript_file", ""), row.get("transcript_text", "这是正文。"),
                row.get("summary", "摘要"), row.get("status", "summarized"),
                row.get("created_at", "2026-02-03 04:05:06"),
                row.get("created_at", "2026-02-03 04:05:06"),
            ),
        )
    conn.commit()
    conn.close()
    return path


class SkillTestCase(unittest.TestCase):
    """提供隔离的临时目录、库与归档目录，并隔离日志路径。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = make_db(self.root / "transcripts.db")
        self.archive = self.root / "archive"
        self.archive.mkdir(parents=True, exist_ok=True)
        self.log_file = self.root / "transcript.log"
        patcher = mock.patch.dict(
            os.environ, {"BILI_TRANSCRIPT_LOG_FILE": str(self.log_file)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_row(self, bvid: str, page: int = 1, **kwargs) -> None:
        """向测试库插入一行（结构与生产一致）。"""
        make_db(self.db, [dict(bvid=bvid, page=page, **kwargs)])

    def run_quiet(self, function, *args, **kwargs):
        """执行 CLI 入口并吞掉其 stdout（测试只需返回码与副作用）。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            return function(*args, **kwargs)

    def capture(self, function, *args, **kwargs) -> tuple:
        """执行 CLI 入口并返回 (返回码, stdout)。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = function(*args, **kwargs)
        return code, buffer.getvalue()

    def copy_real_db(self, rows_limit: int | None = None) -> Path:
        """真实库的副本（只读用途）；生产库不可被测试修改。"""
        target = self.root / "real-copy.db"
        shutil.copy2(REAL_DB, target)
        return target

