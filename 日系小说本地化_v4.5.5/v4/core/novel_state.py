"""SQLite 长篇状态最小实现；只接受已批准章节的状态快照。

v4.5.0-dev.3：基于 ``state/approval-journal.json`` 的两阶段提交——
``begin``（intent）→ SQLite 事务 + continuity 追加 → ``committed``；
任何一步失败标记 ``failed`` 并回滚 continuity，状态绝不半提交。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .audit import now_iso
from .jsonio import read_json, write_json_atomic


SCHEMA = """
CREATE TABLE IF NOT EXISTS chapters (chapter_id TEXT PRIMARY KEY, version TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities (entity_id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, metadata_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS facts (fact_id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL, subject_id TEXT NOT NULL, predicate TEXT NOT NULL, object_value TEXT NOT NULL, source_version TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS terminology (term_id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state_deltas (delta_id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL);
"""


def db_path(workspace: Path | str) -> Path:
    return Path(workspace) / "state" / "novel_state.db"


def init_db(workspace: Path | str) -> Path:
    path = db_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
    return path


def commit_approved_chapter(
    workspace: Path | str,
    *,
    chapter_id: str,
    version: str,
    sha256: str,
    events: dict[str, Any],
) -> Path:
    """幂等提交已通过章节；重复提交不会生成重复事实。

    一个章节只有一个生效版本：提交新版本时必须先取代该章节的旧版本事实，
    否则旧版事实残留在 facts 表里，从 SQLite 重建账本会把同一章事件算两遍
    （§9.7「重复 approve 不产生重复事实」/ §10.2「重跑没有重复事实」）。
    """
    path = init_db(workspace)
    rows = events.get("events", []) if isinstance(events, dict) else []
    with sqlite3.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO chapters VALUES (?, ?, ?, 'approved', datetime('now')) "
            "ON CONFLICT(chapter_id) DO UPDATE SET version=excluded.version, sha256=excluded.sha256, status='approved', updated_at=excluded.updated_at",
            (chapter_id, version, sha256),
        )
        conn.execute(
            "DELETE FROM facts WHERE chapter_id=? AND source_version != ?",
            (chapter_id, version),
        )
        for index, event in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(event, dict):
                continue
            for participant in event.get("participants", []) or []:
                entity_id = str(participant)
                conn.execute(
                    "INSERT INTO entities(entity_id,name,kind,metadata_json) VALUES(?,?,?,?) ON CONFLICT(entity_id) DO NOTHING",
                    (entity_id, entity_id, "character", "{}"),
                )
            fact_id = f"{chapter_id}:{version}:event:{index}"
            conn.execute(
                "INSERT OR REPLACE INTO facts VALUES (?, ?, ?, ?, ?, ?, 'approved')",
                (fact_id, chapter_id, str((event.get("participants") or ["-"])[0]), "event", json.dumps(event, ensure_ascii=False), version),
            )
        conn.commit()
    return path


def mark_chapter_rolled_back(
    workspace: Path | str,
    *,
    chapter_id: str,
    downstream_ids: Iterable[str] = (),
) -> list[str]:
    """SQLite 侧回滚标记（§8.9-6/7）。

    - 回滚章节：status = ``rolled_back``，facts 保留供审计（Task 5.2）；
    - 下游已批准章节：status = ``dependency_invalid``，其事实不再作为长篇权威
      （``rebuild_ledger_from_db`` 只采信 ``approved``），直到重检重批。

    返回被标记为依赖失效的下游章节 ID 列表。
    """
    path = init_db(workspace)
    marked: list[str] = []
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE chapters SET status='rolled_back', updated_at=datetime('now') "
            "WHERE chapter_id=?",
            (chapter_id,),
        )
        for cid in downstream_ids:
            cursor = conn.execute(
                "UPDATE chapters SET status='dependency_invalid', updated_at=datetime('now') "
                "WHERE chapter_id=? AND status='approved'",
                (cid,),
            )
            if cursor.rowcount:
                marked.append(cid)
        conn.commit()
    return marked


def approval_journal_path(workspace: Path | str) -> Path:
    return Path(workspace) / "state" / "approval-journal.json"


def _load_journal(workspace: Path | str) -> list[dict[str, Any]]:
    path = approval_journal_path(workspace)
    if not path.is_file():
        return []
    try:
        doc = read_json(path)
    except (OSError, ValueError):
        return []
    return doc if isinstance(doc, list) else []


def _append_journal(workspace: Path | str, entry: dict[str, Any]) -> None:
    path = approval_journal_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = _load_journal(workspace)
    entries.append(entry)
    write_json_atomic(path, entries)


def commit_approval(
    workspace: Path | str,
    *,
    chapter_id: str,
    version: str,
    sha256: str,
    events: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """approve 的两阶段提交：intent → SQLite 事务 + continuity → committed。

    - SQLite 提交失败或 continuity 追加失败：journal 标记 ``failed``，
      回滚 continuity 至提交前内容，并向调用方抛出异常（approve fail-closed）；
    - 全部成功：journal 标记 ``committed``。重复提交幂等。
    """
    from .continuity import append_snapshot, ledger_path, load_ledger

    entry: dict[str, Any] = {
        "chapter_id": chapter_id,
        "version": version,
        "sha256": sha256,
        "phase": "intent",
        "started_at": now_iso(),
        "finished_at": None,
        "error": None,
    }
    _append_journal(workspace, entry)

    ledger_before: dict[str, Any] | None = None
    ledger_existed = ledger_path(workspace).is_file()
    if ledger_existed:
        ledger_before = load_ledger(workspace)
    try:
        # 顺序至关重要（清单 §14 停止条件 #2）：先写 JSON 派生缓存、后写 SQLite 事务源。
        # 反过来做时，若 continuity 写入失败，SQLite 已经落库 status='approved' 与 facts，
        # 而异常分支只回滚 continuity —— 结果就是「manifest 仍 passed 但 SQLite 已批准」的
        # 半批准状态，且 rebuild_ledger_from_db 只采信 approved，会把未批准事实复活。
        # 现在的顺序保证：continuity 阶段失败则 SQLite 从未被触碰；
        # SQLite 阶段失败则恢复 continuity 到提交前内容，两侧都不留痕。
        append_snapshot(workspace, snapshot)
        commit_approved_chapter(
            workspace,
            chapter_id=chapter_id,
            version=version,
            sha256=sha256,
            events=events,
        )
    except Exception as exc:  # noqa: BLE001 - 两阶段提交必须整体回滚
        if ledger_existed and ledger_before is not None:
            write_json_atomic(ledger_path(workspace), ledger_before)
        elif not ledger_existed:
            ledger_path(workspace).unlink(missing_ok=True)
        entry.update(phase="failed", error=str(exc), finished_at=now_iso())
        _append_journal(workspace, entry)
        raise

    entry.update(phase="committed", finished_at=now_iso())
    _append_journal(workspace, entry)
    return entry
