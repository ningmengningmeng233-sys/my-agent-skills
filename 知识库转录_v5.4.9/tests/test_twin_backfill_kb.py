# -*- coding: utf-8 -*-
"""双胞胎内核正向合入回归测试（知识库转录侧）：
1. _common.prefixed_files 字面量前缀匹配（书名含 `[` 等 glob 元字符时不失效）
2. index_db.normalize_path 与 _dedupe_separators 归并 Windows `/` 与 `\\` 混写重复行
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import _common
import index_db


def test_prefixed_files_handles_bracket_book_names(tmp_path: Path):
    (tmp_path / "[特殊]书名-001.md").write_text("a", encoding="utf-8")
    (tmp_path / "[特殊]书名-002.md").write_text("b", encoding="utf-8")
    (tmp_path / "[特殊]书名扩展-001.md").write_text("c", encoding="utf-8")
    matched = _common.prefixed_files(tmp_path, "[特殊]书名", ".md")
    assert [p.name for p in matched] == ["[特殊]书名-001.md", "[特殊]书名-002.md"]


def test_index_db_normalizes_and_dedupes_mixed_separators(tmp_path: Path):
    db_file = tmp_path / index_db.DB_NAME
    # 先手工构造一份含 `/` 与 `\` 混写重复行的旧库
    conn = sqlite3.connect(db_file)
    conn.execute(
        """CREATE TABLE segment_tasks (
            segment_path TEXT PRIMARY KEY,
            out_path TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            retries INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            seg_hash TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            created_at INTEGER,
            updated_at INTEGER
        )"""
    )
    conn.execute(
        "INSERT INTO segment_tasks VALUES (?, ?, 'failed', 1, 'err', '', 100, 50, 1, 1)",
        ("原文/测试书-001.md", "段索引/测试书-001-idx.md"),
    )
    conn.execute(
        "INSERT INTO segment_tasks VALUES (?, ?, 'done', 0, NULL, 'abc123', 200, 80, 2, 2)",
        ("原文\\测试书-001.md", "段索引\\测试书-001-idx.md"),
    )
    conn.commit()
    conn.close()

    with index_db.IndexDB(str(tmp_path)) as db:
        stats, (pt, ct) = db.stats()
        assert stats == {"done": 1}
        assert (pt, ct) == (300, 130)
        row_a = db.get("原文/测试书-001.md")
        row_b = db.get("原文\\测试书-001.md")
        assert row_a is not None and row_a == row_b
