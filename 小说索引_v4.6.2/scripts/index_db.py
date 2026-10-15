#!/usr/bin/env python3
r"""
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
R05-D02 修复（机制来源=本地同族成人索引 v4.6.5 等价实现，非 54Lynnn 作者代码）：
所有读写入口对路径做 os.path.normpath 归一化——Windows 下 `\` 与 `/` 混写曾绕过
segment_path 主键判重，同一段被记两次（status 面板虚报段数与 token）；旧库打开时
自动归并历史重复行（done > failed > pending、retries 取大、token 相加保留累计，
保留行路径改写为归一化字面量），归并幂等。

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

from _logging import get_logger

DB_NAME = ".index_state.db"

logger = get_logger(__name__)


def normalize_path(path):
    """统一路径分隔符，作断点表主键用。

    Windows 下同一段文件的 `\\` 与 `/` 两种写法是两个不同的原始字符串，直接入表
    会让主键判重失效（同段记两行 → 段数与 token 虚报）。
    """
    if not path:
        return path
    return os.path.normpath(str(path))


def file_hash(path):
    """段文件内容 SHA-256（前 16 位 hex）。变更检测用，毫秒级。"""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return None


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
        self._dedupe_separators()

    # 状态优先级：done > failed > pending（同一段被混写记两行时保留最进展的那个结论）
    _STATUS_RANK = {"done": 2, "failed": 1, "pending": 0}

    def _dedupe_separators(self):
        r"""归并历史 `\` / `/` 混写重复行；由 __init__ 调用，锁已持有，勿再加锁。

        规则：状态取优先级最高者、retries 取大、token 相加（两次执行都真实消耗了
        API，不能清零）、done 行不保留陈旧 error；保留行的路径与 out_path 改写为
        归一化字面量，因此再次打开时无重复可归并（幂等）。
        """
        groups = {}
        for rowid, seg in self.conn.execute(
            "SELECT rowid, segment_path FROM segment_tasks ORDER BY rowid"
        ).fetchall():
            groups.setdefault(normalize_path(seg), []).append(rowid)

        merged = 0
        for rowids in groups.values():
            primary, dupes = rowids[0], rowids[1:]
            for dup in dupes:
                pri = self.conn.execute(
                    "SELECT status, retries, error, seg_hash, prompt_tokens, completion_tokens"
                    " FROM segment_tasks WHERE rowid=?", (primary,)).fetchone()
                sec = self.conn.execute(
                    "SELECT status, retries, error, seg_hash, prompt_tokens, completion_tokens"
                    " FROM segment_tasks WHERE rowid=?", (dup,)).fetchone()
                status = max((pri[0], sec[0]), key=lambda s: self._STATUS_RANK.get(s, -1))
                self.conn.execute(
                    "UPDATE segment_tasks SET status=?, retries=?, error=?, seg_hash=?,"
                    " prompt_tokens=?, completion_tokens=?, updated_at=? WHERE rowid=?",
                    (status,
                     max(pri[1] or 0, sec[1] or 0),
                     None if status == "done" else (pri[2] or sec[2]),
                     pri[3] or sec[3],
                     (pri[4] or 0) + (sec[4] or 0),
                     (pri[5] or 0) + (sec[5] or 0),
                     self._now(), primary),
                )
                self.conn.execute("DELETE FROM segment_tasks WHERE rowid=?", (dup,))
                merged += 1

        # 保留行路径改写为归一化字面量，防止下次再以另一写法分叉
        rewritten = 0
        for col in ("segment_path", "out_path"):
            for rowid, value in self.conn.execute(
                f"SELECT rowid, {col} FROM segment_tasks WHERE {col} IS NOT NULL"
            ).fetchall():
                norm = normalize_path(value)
                if norm != value:
                    self.conn.execute(f"UPDATE segment_tasks SET {col}=? WHERE rowid=?", (norm, rowid))
                    rewritten += 1

        if merged or rewritten:
            self.conn.commit()
        if merged:
            logger.info(f"  ℹ️  断点库归并：{merged} 条因路径分隔符混写产生的重复记录")

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
                (normalize_path(segment_path),),
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
                (normalize_path(segment_path), normalize_path(out_path), seg_hash, now, now),
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
                (normalize_path(segment_path), normalize_path(out_path), seg_hash,
                 prompt_tokens, completion_tokens, now, now),
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
                (normalize_path(segment_path), error[:500], now, now, error[:500], now),
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
