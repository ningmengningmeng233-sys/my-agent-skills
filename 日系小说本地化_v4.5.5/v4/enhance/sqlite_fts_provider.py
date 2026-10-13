"""零外部依赖的 SQLite FTS5 索引库后端。

v4.5.0-dev.3（Batch 6）：FTS 索引缓存绝不写入外部只读索引库目录——
默认落盘 ``<workspace>/.cache/knowledge/fts-<root-hash>.sqlite3``；
未提供 workspace 时退化为系统临时缓存目录。外部库目录只读零写入。
"""
from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

_SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass(frozen=True, slots=True)
class FTSHit:
    asset_id: str
    path: str
    snippet: str
    score: float
    sha256: str


class SQLiteFTSProvider:
    def __init__(
        self,
        root: Path | str,
        db_path: Path | str | None = None,
        *,
        workspace: Path | str | None = None,
        source_id: str | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        if db_path is not None:
            self.db_path = Path(db_path)
        else:
            # v4.5.0-dev.3：缓存重定向（Task 6.3）——外部库目录只读零写入
            root_hash = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:16]
            if workspace is not None:
                cache_dir = Path(workspace) / ".cache" / "knowledge"
            else:
                cache_dir = Path(tempfile.gettempdir()) / "jp-novel-knowledge-cache"
            self.db_path = cache_dir / f"fts-{root_hash}.sqlite3"
        self.source_id = source_id or f"fts:{self.root.name}"

    def _is_valid_sqlite(self) -> bool:
        """文件是否存在且带合法 SQLite 文件头。"""
        if not self.db_path.is_file():
            return False
        try:
            with self.db_path.open("rb") as handle:
                return handle.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
        except OSError:
            return False

    def healthcheck(self) -> bool:
        """缓存是否可用：不仅看文件存在，还要校验 SQLite 文件头。

        §10.7「缓存损坏可以重建」：只看存在性会把「文件在但内容已损坏」
        误判为健康，导致后续查询直接抛 DatabaseError。
        """
        return self._is_valid_sqlite()

    def search_hits(self, queries, *, limit: int = 5):
        """统一协议入口：多查询词 → KnowledgeHit（来源隔离，Batch 6）。"""
        from .providers import KnowledgeHit

        seen: set[str] = set()
        hits: list[KnowledgeHit] = []
        for query in queries:
            for row in self.search(str(query), limit=limit):
                key = f"{self.source_id}:{row.path}"
                if key in seen:
                    continue
                seen.add(key)
                hits.append(
                    KnowledgeHit(
                        source=self.source_id,
                        kind="corpus_excerpt",
                        asset_path=row.path,
                        snippet=row.snippet[:180],
                        sha256=row.sha256,
                        score=row.score,
                    )
                )
                if len(hits) >= max(1, int(limit)):
                    return hits
        return hits[: max(1, int(limit))]

    def build(self) -> Path:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 损坏缓存必须先丢弃：否则 CREATE VIRTUAL TABLE 会直接抛 DatabaseError
        if self.db_path.exists() and not self._is_valid_sqlite():
            try:
                self.db_path.unlink()
            except OSError:
                pass
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS documents USING fts5(path UNINDEXED, content)")
            conn.execute("DELETE FROM documents")
            for path in sorted(self.root.rglob("*.md")):
                if ".index" in path.parts:
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                conn.execute("INSERT INTO documents(path, content) VALUES (?, ?)", (path.relative_to(self.root).as_posix(), content))
            # closing() 不像 with-sqlite3.connect 那样自动提交：必须显式提交，
            # 否则连接关闭时未提交的写入会被丢弃（索引变空表）。
            conn.commit()
        return self.db_path

    def search(self, query: str, *, limit: int = 5) -> list[FTSHit]:
        """检索；缓存缺失或损坏时重建一次后重试，仍不可用则降级为空命中。

        §10.7：缓存损坏必须可重建，且 FTS 不可用时降级 Markdown（此处返回空
        命中交回 CompositeProvider 决策），绝不向调用方抛数据库异常。
        """
        if not self.healthcheck():
            self.build()
        query = " ".join(str(query or "").split())
        if not query:
            return []
        try:
            return self._search_once(query, limit=limit)
        except sqlite3.DatabaseError:
            # 损坏缓存在 build() 时已被覆盖重建；重试一次
            try:
                with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
                    conn.execute("DROP TABLE IF EXISTS documents")
                self.build()
                return self._search_once(query, limit=limit)
            except (sqlite3.DatabaseError, OSError):
                return []

    def _search_once(self, query: str, *, limit: int = 5) -> list[FTSHit]:
        hits: list[FTSHit] = []
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT path, snippet(documents, 1, '[', ']', '…', 18), bm25(documents) FROM documents WHERE documents MATCH ? ORDER BY bm25(documents) LIMIT ?",
                (query, max(1, int(limit))),
            ).fetchall()
            # SQLite 默认 tokenizer 对中文短语支持有限，空命中时回退确定性子串匹配。
            if not rows:
                like = f"%{query}%"
                rows = conn.execute(
                    "SELECT path, substr(content, 1, 180), 0 FROM documents WHERE content LIKE ? LIMIT ?",
                    (like, max(1, int(limit))),
                ).fetchall()
        for path, snippet, rank in rows:
            full = self.root / str(path)
            try:
                digest = hashlib.sha256(full.read_bytes()).hexdigest()
            except OSError:
                digest = ""
            hits.append(FTSHit(str(path), str(path), str(snippet), float(-rank), digest))
        return hits
