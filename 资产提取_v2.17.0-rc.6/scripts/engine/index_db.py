#!/usr/bin/env python3
"""SQLite checkpoint layer for segment/registry tasks."""
import hashlib
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DB_NAME = ".asset_state.db"


def file_hash(path: str | Path) -> str | None:
    """Return first 16 hex chars of SHA-256 file content."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


# 规则单一真相源是 `engine/prompt_rules.py`（无版本常量），所以拿它的内容哈希当规则版本。
RULES_SOURCE = Path(__file__).resolve().parent / "prompt_rules.py"
RULESET_VERSION = file_hash(RULES_SOURCE) or ""


def current_channels() -> list[tuple[str, str, str]]:
    """执行通道的非密钥视图：节点 `(name, base_url, model)` 去重后稳定排序；无池配置时为空表。

    节点的 `api_key` 一律不进这个视图——R09-D05 的红线是「改密钥不得改变指纹」。
    池依赖缺失或配置文件读不到时退回空表：那种配置下 `llm` 用同一份文件做真实请求会自行报错，
    指纹不静默放行。
    """
    try:
        from .llm_pool import load_nodes

        nodes = load_nodes()
    except (ImportError, OSError, ValueError):
        return []
    return sorted({(node.name, node.base_url, node.model) for node in nodes})


def current_model_summary() -> str:
    """当前模型配置摘要（只含非密钥字段：模型名、max_tokens 与执行通道）。

    通道必须参与（R09-D05）：有 `llm_nodes.json` 时真正执行的是节点自带的模型/端点，
    只认 `SUMMARY_API_MODEL` 会让「换了通道却复用旧产物」静默通过。
    没有节点配置时不加 `channels` 键，摘要与旧口径逐字节一致（存量指纹不因此额外失效）。
    """
    from .artifact_graph import canonical_payload

    payload = {
        "model": os.environ.get("SUMMARY_API_MODEL", ""),
        "max_tokens": os.environ.get("SUMMARY_MAX_TOKENS", ""),
    }
    channels = current_channels()
    if channels:
        payload["channels"] = channels
    return canonical_payload(payload)


def task_node_fingerprint(segment_path: str | Path, *, rule_version: str = "",
                          template_version: str = "") -> str:
    """提取任务的节点指纹 = 源文内容哈希 + 规则版本 + 模板版本 + 模型配置摘要。

    模型配置参与指纹：换模型或改 `SUMMARY_MAX_TOKENS` 会让断点失效，
    避免"换了模型却复用旧产物"被静默当成正确（工单 9 / 工单 8 步骤 2）。
    规则版本由调用方以 `RULESET_VERSION`（规则源文件内容哈希）传入——两处生产入口
    `extract_segments` 与 `scan_registry` 都要传，不传就等于承诺失效（R09-D07）。
    """
    from .artifact_graph import node_fingerprint

    return node_fingerprint({"segment_hash": file_hash(segment_path)},
                            rule_version=rule_version, template_version=template_version,
                            model_summary=current_model_summary())


class IndexDB:
    def __init__(self, index_lib_dir: str | Path, table: str = "segment_tasks") -> None:
        self.path = Path(index_lib_dir) / DB_NAME
        self.table = table
        self._lock = threading.Lock()
        self.conn: sqlite3.Connection | None = None
        self.ready = False
        try:
            # Shared across threads; serialized by lock.
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            with self._lock:
                self.conn.execute(
                    f"""CREATE TABLE IF NOT EXISTS {table} (
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
            self.ready = True
        except sqlite3.Error as e:
            print(f"❌ 断点库初始化失败: {self.path}（{e}）")
            self.ready = False

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({self.table})").fetchall()}
        for col, decl in (
            ("seg_hash", "TEXT"),
            ("prompt_tokens", "INTEGER DEFAULT 0"),
            ("completion_tokens", "INTEGER DEFAULT 0"),
            ("node_fp", "TEXT"),
        ):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {self.table} ADD COLUMN {col} {decl}")
        self.conn.commit()
        stale = self.conn.execute(
            f"SELECT COUNT(*) FROM {self.table} WHERE status='done' AND (seg_hash IS NULL OR seg_hash='')"
        ).fetchone()[0]
        if stale:
            print(f"  ℹ️  断点库迁移：{stale} 条旧 done 记录无内容哈希，将按变更重新生成（安全策略）")
        no_fp = self.conn.execute(
            f"SELECT COUNT(*) FROM {self.table} WHERE status='done' AND (node_fp IS NULL OR node_fp='')"
        ).fetchone()[0]
        if no_fp:
            print(f"  ℹ️  断点库迁移：{no_fp} 条旧 done 记录无节点指纹，将按规则/模型版本重新生成（安全策略）")

    def close(self) -> None:
        with self._lock:
            if self.conn:
                self.conn.close()

    def __enter__(self) -> "IndexDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _now(self) -> int:
        return int(time.time())

    def get(self, segment_path: str) -> tuple | None:
        if not self.ready or not self.conn:
            return None
        segment_path = str(segment_path)
        with self._lock:
            row = self.conn.execute(
                f"SELECT segment_path, out_path, status, retries, error, seg_hash, node_fp "
                f"FROM {self.table} WHERE segment_path=?",
                (segment_path,),
            ).fetchone()
        return row

    def should_skip(self, segment_path: str, out_path: str | Path, seg_hash: str | None = None,
                    node_fp: str | None = None) -> bool:
        """内容未变且**节点指纹**未变才跳过。

        `node_fp` 由 `task_node_fingerprint` → `artifact_graph.node_fingerprint` 产生，四段参与：
        源文哈希、规则摘要（`RULESET_VERSION` = 规则源文件内容哈希，两处生产入口都传）、
        模板哈希、模型配置摘要（含执行通道，不含任何密钥）。任一段变了即便源文一字未改也不再命中
        ——否则"换了规则/模板/模型却复用旧产物"会被静默当成正确。
        调用方不传 `node_fp`（旧调用方）时跳过指纹比较，保持旧行为。
        """
        if not self.ready:
            return False
        segment_path = str(segment_path)
        if seg_hash is None:
            seg_hash = file_hash(segment_path)
        row = self.get(segment_path)
        if row and row[2] == "done" and Path(out_path).exists():
            if row[5] != seg_hash:
                return False
            if node_fp is not None and row[6] != node_fp:
                return False
            return True
        return False

    def mark_pending(self, segment_path: str, out_path: str | Path, seg_hash: str | None = None) -> None:
        if not self.ready or not self.conn:
            return
        segment_path = str(segment_path)
        if seg_hash is None:
            seg_hash = file_hash(segment_path)
        now = self._now()
        with self._lock:
            self.conn.execute(
                f"INSERT OR IGNORE INTO {self.table} (segment_path, out_path, status, seg_hash, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (segment_path, str(out_path), "pending", seg_hash, now, now),
            )
            self.conn.commit()

    def mark_done(
        self,
        segment_path: str,
        out_path: str | Path,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        seg_hash: str | None = None,
        node_fp: str | None = None,
    ) -> None:
        if not self.ready or not self.conn:
            return
        segment_path = str(segment_path)
        if seg_hash is None:
            seg_hash = file_hash(segment_path)
        now = self._now()
        with self._lock:
            self.conn.execute(
                f"""INSERT INTO {self.table} (segment_path, out_path, status, seg_hash, node_fp, prompt_tokens, completion_tokens, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(segment_path) DO UPDATE SET
                      status='done', out_path=excluded.out_path, error=NULL, seg_hash=excluded.seg_hash,
                      node_fp=excluded.node_fp,
                      prompt_tokens=excluded.prompt_tokens, completion_tokens=excluded.completion_tokens,
                      updated_at=excluded.updated_at""",
                (segment_path, str(out_path), "done", seg_hash, node_fp or "",
                 prompt_tokens, completion_tokens, now, now),
            )
            self.conn.commit()

    def mark_failed(self, segment_path: str, error: str) -> None:
        if not self.ready or not self.conn:
            return
        segment_path = str(segment_path)
        now = self._now()
        truncated = error[:500]
        with self._lock:
            self.conn.execute(
                f"""INSERT INTO {self.table} (segment_path, status, retries, error, created_at, updated_at)
                   VALUES (?, 'failed', 1, ?, ?, ?)
                   ON CONFLICT(segment_path) DO UPDATE SET
                     status='failed', retries=retries+1, error=?, updated_at=?""",
                (segment_path, truncated, now, now, truncated, now),
            )
            self.conn.commit()

    def stats(self) -> tuple[dict[str, int], tuple[int, int]]:
        if not self.ready or not self.conn:
            return {}, (0, 0)
        with self._lock:
            rows = self.conn.execute(f"SELECT status, COUNT(*) FROM {self.table} GROUP BY status").fetchall()
            toks = self.conn.execute(
                f"SELECT SUM(prompt_tokens), SUM(completion_tokens) FROM {self.table} WHERE status='done'"
            ).fetchone()
        return dict(rows), (toks[0] or 0, toks[1] or 0)
