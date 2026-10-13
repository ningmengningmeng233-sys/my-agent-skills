#!/usr/bin/env python3
"""
索引库 SQLite 断点层（完整移植转录技能 transcript_db.py 模式）

记录每个段任务的处理状态，实现断点续传：
- 任务前查状态：done → 跳过（不重复消耗 API）
- 失败写原因与重试次数
- 中断重跑不重做已完成段

一致性规则：
- DB 标记 done 但目标索引文件缺失 → 视为未完成（文件是最终真相）
- 段文件内容变更（追更并入新章）→ seg_hash 变化 → 视为未完成强制重跑
  （防止行号漂移导致旧索引静默失效）

v3.32 新增：seg_hash（内容哈希防陈旧）+ prompt_tokens/completion_tokens（token 统计）。

用法:
  from index_db import IndexDB
  with IndexDB(index_lib_dir) as db:
      db.should_skip(segment_path, out_path)  # 查断点（自动校验 hash）
      db.mark_pending(segment_path, out_path)
      db.mark_done(segment_path, out_path, prompt_tokens=..., completion_tokens=...)
      db.mark_failed(segment_path, error)
"""
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path

from _logging import get_logger

DB_NAME = ".index_state.db"

logger = get_logger(__name__)


def file_hash(path):
    """段文件内容 SHA-256（前 16 位 hex）。变更检测用，毫秒级。"""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return None


def _path_has_book_prefix(path: str | None, prefix: str) -> bool:
    """路径任一部分等于 prefix 或以 `prefix-` 开头（字面量，不解释元字符）。"""
    if not path or not prefix:
        return False
    parts = [part for part in Path(str(path)).parts if part]
    return any(part == prefix or part.startswith(prefix + "-") for part in parts)


def _rename_book_prefix(path: str | None, old_prefix: str, new_prefix: str) -> str | None:
    """替换路径中等于 old_prefix 或以 `old_prefix-` 开头的部分；无变化返回 None。"""
    if not path:
        return None
    parts = list(Path(str(path)).parts)
    changed = False
    for i, part in enumerate(parts):
        if part == old_prefix:
            parts[i] = new_prefix
            changed = True
        elif part.startswith(old_prefix + "-"):
            parts[i] = new_prefix + part[len(old_prefix):]
            changed = True
    if not changed:
        return None
    return os.path.join(*parts)


class IndexDB:
    def __init__(self, index_lib_dir):
        self.path = os.path.join(index_lib_dir, DB_NAME)
        self._lock = threading.Lock()
        # check_same_thread=False：直调引擎在 32 个工作线程中共享连接，用锁串行化
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS segment_tasks (
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
            self.conn.commit()
            self._migrate()

    def _migrate(self):
        """旧库（v3.31 无 seg_hash/token 列）在线迁移：ALTER TABLE 补列。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(segment_tasks)").fetchall()}
        for col, decl in (("seg_hash", "TEXT"), ("prompt_tokens", "INTEGER DEFAULT 0"),
                          ("completion_tokens", "INTEGER DEFAULT 0")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE segment_tasks ADD COLUMN {col} {decl}")
        self.conn.commit()
        # 迁移提示：旧 done 记录 hash 为空 → should_skip 判定为需重跑（安全策略）
        stale = self.conn.execute(
            "SELECT COUNT(*) FROM segment_tasks WHERE status='done' AND (seg_hash IS NULL OR seg_hash='')"
        ).fetchone()[0]
        if stale:
            logger.info(f"  ℹ️  断点库迁移：{stale} 条旧 done 记录无内容哈希，将按变更重新生成（安全策略）")

    def close(self):
        with self._lock:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _now(self):
        return int(time.time())

    def get(self, segment_path):
        with self._lock:
            row = self.conn.execute(
                "SELECT segment_path, out_path, status, retries, error, seg_hash FROM segment_tasks WHERE segment_path=?",
                (segment_path,),
            ).fetchone()
        return row

    def should_skip(self, segment_path, out_path, seg_hash=None):
        """断点判断：done + 索引文件存在 + 段文件内容未变 → 跳过"""
        if seg_hash is None:
            seg_hash = file_hash(segment_path)
        row = self.get(segment_path)
        if row and row[2] == "done" and os.path.exists(segment_path) and os.path.exists(out_path):
            # 旧版本迁移记录没有 seg_hash，必须重跑一次建立安全锚点；
            # 缺失源文件也不能把 None==None 当成已完成。
            return bool(row[5]) and row[5] == seg_hash
        return False

    def mark_pending(self, segment_path, out_path, seg_hash=None):
        if seg_hash is None:
            seg_hash = file_hash(segment_path)
        now = self._now()
        with self._lock:
            self.conn.execute(
                """INSERT INTO segment_tasks
                   (segment_path, out_path, status, seg_hash, error, created_at, updated_at)
                   VALUES (?, ?, 'pending', ?, NULL, ?, ?)
                   ON CONFLICT(segment_path) DO UPDATE SET
                     out_path=excluded.out_path, status='pending', seg_hash=excluded.seg_hash,
                     error=NULL, updated_at=excluded.updated_at""",
                (segment_path, out_path, seg_hash, now, now),
            )
            self.conn.commit()

    def mark_done(self, segment_path, out_path, prompt_tokens=0, completion_tokens=0, seg_hash=None):
        if seg_hash is None:
            seg_hash = file_hash(segment_path)
        now = self._now()
        with self._lock:
            # v4.5 审查修复（P2）：UPSERT 对齐 mark_failed——此前只 UPDATE，对不存在的
            # 行静默 no-op（断点跳过判断失效重跑却无提示），正常流程有 mark_pending
            # 先行所以不触发，但误用无任何提示；现在与 mark_failed 语义一致
            self.conn.execute(
                """INSERT INTO segment_tasks (segment_path, out_path, status, seg_hash,
                                              prompt_tokens, completion_tokens, created_at, updated_at)
                   VALUES (?, ?, 'done', ?, ?, ?, ?, ?)
                   ON CONFLICT(segment_path) DO UPDATE SET
                     out_path=excluded.out_path, status='done', error=NULL,
                     seg_hash=excluded.seg_hash, prompt_tokens=excluded.prompt_tokens,
                     completion_tokens=excluded.completion_tokens, updated_at=excluded.updated_at""",
                (segment_path, out_path, seg_hash, prompt_tokens, completion_tokens, now, now),
            )
            self.conn.commit()

    def mark_failed(self, segment_path, error):
        now = self._now()
        with self._lock:
            self.conn.execute(
                """INSERT INTO segment_tasks (segment_path, status, retries, error, created_at, updated_at)
                   VALUES (?, 'failed', 1, ?, ?, ?)
                   ON CONFLICT(segment_path) DO UPDATE SET
                     status='failed', retries=retries+1, error=?, updated_at=?""",
                (segment_path, error[:500], now, now, error[:500], now),
            )
            self.conn.commit()

    def stats(self):
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) FROM segment_tasks GROUP BY status"
            ).fetchall()
            toks = self.conn.execute(
                "SELECT SUM(prompt_tokens), SUM(completion_tokens) FROM segment_tasks WHERE status='done'"
            ).fetchone()
        return dict(rows), (toks[0] or 0, toks[1] or 0)

    def delete_by_prefix(self, prefix: str) -> int:
        """删除断点记录中路径包含 `<prefix>` 目录或 `<prefix>-` 文件名的行（字面量匹配）。

        供 delete_novel 使用；不把书名拼进 SQL LIKE / glob，避免 `[`、`%`、`_`
        等元字符误匹配其他书。
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT segment_path, out_path FROM segment_tasks"
            ).fetchall()
            targets = [
                seg for seg, out in rows
                if _path_has_book_prefix(seg, prefix) or _path_has_book_prefix(out, prefix)
            ]
            for seg in targets:
                self.conn.execute(
                    "DELETE FROM segment_tasks WHERE segment_path = ?", (seg,)
                )
            self.conn.commit()
            return len(targets)

    def rename_prefix(self, old_prefix: str, new_prefix: str) -> int:
        """重命名断点记录中包含 `<old_prefix>` 目录/文件名的路径（字面量匹配）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT segment_path, out_path FROM segment_tasks"
            ).fetchall()
            updated = 0
            for seg, out in rows:
                new_seg = _rename_book_prefix(seg, old_prefix, new_prefix)
                new_out = _rename_book_prefix(out, old_prefix, new_prefix)
                if new_seg is None and new_out is None:
                    continue
                self.conn.execute(
                    "UPDATE segment_tasks SET segment_path = ?, out_path = ? WHERE segment_path = ?",
                    (new_seg or seg, new_out or out, seg),
                )
                updated += 1
            self.conn.commit()
            return updated
