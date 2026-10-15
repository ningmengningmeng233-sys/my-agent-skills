"""外部知识库统一接口。

v4.5.0-dev.3（Batch 6）：
- ``KnowledgeHit``：跨 Provider 统一命中结构（带 ``source`` 来源隔离）；
- ``search_hits()``：统一检索入口（多查询词，返回 KnowledgeHit）；
- ``CompositeProvider``：按顺序尝试多个 Provider，故障自动降级并写审计，
  以本地 Markdown 索引库（IndexLibraryProvider）作为稳定保底；
- 旧 ``KnowledgeProvider`` 协议保持兼容；所有 Provider 对外部库严格只读。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from ..core.audit import append_audit


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    """统一知识命中：source 标识来源 Provider，实现来源隔离与可审计。"""

    source: str
    kind: str
    asset_path: str
    snippet: str
    sha256: str
    score: float = 0.0


class KnowledgeProvider(Protocol):
    def search_style_cards(self, queries: Sequence[str], *, limit: int = 2) -> list[Any]: ...
    def search_chapter_indexes(self, queries: Sequence[str], *, limit: int = 2) -> list[Any]: ...
    def get(self, asset_path: str) -> Any: ...
    def fingerprint(self, asset_path: str) -> str: ...
    def healthcheck(self) -> bool: ...


class UnifiedKnowledgeProvider(Protocol):
    """统一检索协议：多查询词 → KnowledgeHit 列表（来源隔离由 source 字段保证）。"""

    source_id: str

    def search_hits(self, queries: Sequence[str], *, limit: int = 2) -> list[KnowledgeHit]: ...
    def healthcheck(self) -> bool: ...


class IndexLibraryProvider:
    """把现有 IndexLibrary 映射到统一协议；保持原适配器只读。"""

    source_id = "index_lib"

    def __init__(self, library: Any) -> None:
        self.library = library

    def search_style_cards(self, queries: Sequence[str], *, limit: int = 2) -> list[Any]:
        rows: list[Any] = []
        seen: set[str] = set()
        for query in queries:
            for row in self.library.find_style_cards(query, limit=limit):
                key = getattr(row, "rel_path", repr(row))
                if key not in seen:
                    rows.append(row)
                    seen.add(key)
                if len(rows) >= limit:
                    return rows
        return rows

    def search_chapter_indexes(self, queries: Sequence[str], *, limit: int = 2) -> list[Any]:
        return list(self.library.search_tags(list(queries), kinds=["index_chapter"], limit=limit))

    def get(self, asset_path: str) -> Any:
        return self.library.get_style_card(asset_path)

    def fingerprint(self, asset_path: str) -> str:
        return self.library.hash_asset(asset_path)

    def healthcheck(self) -> bool:
        return bool(self.library.assets())

    def search_hits(self, queries: Sequence[str], *, limit: int = 2) -> list[KnowledgeHit]:
        """统一入口：文风卡 + 章节索引合并去重，映射为 KnowledgeHit。"""
        queries = [str(query) for query in queries if str(query)]
        hits: list[KnowledgeHit] = []
        seen: set[str] = set()

        def _record_hit(record: Any, kind: str) -> None:
            rel_path = str(getattr(record, "rel_path", ""))
            if not rel_path or rel_path in seen:
                return
            seen.add(rel_path)
            try:
                sha256 = str(self.library.hash_asset(rel_path))
            except Exception:  # noqa: BLE001
                sha256 = ""
            snippet = str(
                getattr(record, "summary", "")
                or getattr(record, "scene", "")
                or rel_path
            )
            hits.append(
                KnowledgeHit(
                    source=self.source_id,
                    kind=kind,
                    asset_path=rel_path,
                    snippet=snippet[:180],
                    sha256=sha256,
                    score=0.0,
                )
            )

        try:
            for record in self.search_style_cards(queries, limit=limit):
                _record_hit(record, "style_card")
        except Exception:  # noqa: BLE001 - 文风卡检索失败不阻塞章节索引
            pass
        try:
            for record in self.search_chapter_indexes(queries, limit=limit):
                _record_hit(record, "index_chapter")
        except Exception:  # noqa: BLE001
            pass
        return hits[:limit]


class CompositeProvider:
    """按顺序尝试多个 Provider；故障降级审计，Markdown 索引库稳定保底。

    - 供体顺序即优先级：前面的 Provider 命中优先；
    - 单个 Provider healthcheck 失败或抛异常 → 写 ``knowledge_provider_degraded``
      审计并继续后续 Provider，绝不因单个来源故障整体失败；
    - 命中按 (source, asset_path) 去重，来源隔离由 KnowledgeHit.source 保证。
    """

    def __init__(
        self,
        providers: Sequence[Any],
        *,
        audit_path: Path | str | None = None,
        chapter_id: str | None = None,
    ) -> None:
        self.providers = list(providers)
        self.audit_path = Path(audit_path) if audit_path is not None else None
        self.chapter_id = chapter_id

    def healthcheck(self) -> bool:
        return any(
            bool(provider.healthcheck())
            for provider in self.providers
            if hasattr(provider, "healthcheck")
        )

    def search_hits(self, queries: Sequence[str], *, limit: int = 2) -> list[KnowledgeHit]:
        results: list[KnowledgeHit] = []
        seen: set[tuple[str, str]] = set()
        for provider in self.providers:
            try:
                if hasattr(provider, "healthcheck") and not provider.healthcheck():
                    raise RuntimeError("healthcheck 失败")
                hits = provider.search_hits(queries, limit=limit)
            except Exception as exc:  # noqa: BLE001 - 降级不中断
                if self.audit_path is not None:
                    append_audit(
                        self.audit_path,
                        "knowledge_provider_degraded",
                        chapter_id=self.chapter_id or "",
                        provider=str(getattr(provider, "source_id", type(provider).__name__)),
                        reason=str(exc)[:200],
                    )
                continue
            for hit in hits:
                key = (hit.source, hit.asset_path)
                if key in seen:
                    continue
                seen.add(key)
                results.append(hit)
                if len(results) >= max(1, int(limit)):
                    return results
        return results[: max(1, int(limit))]
