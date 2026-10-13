"""长篇连续性账本：按章保存可追溯的轻量状态快照。

v4.5.0-dev.3：``continuity/state.json`` 只是 SQLite 的派生读缓存；
损坏时可从 ``state/novel_state.db`` 的 facts 表完整重建（Task 4.5）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .jsonio import read_json, write_json_atomic


def ledger_path(workspace: Path | str) -> Path:
    return Path(workspace) / "continuity" / "state.json"


def _empty_ledger() -> dict[str, Any]:
    return {"schema_version": "continuity-1", "chapters": [], "latest": {}}


def _snapshot_from_fact_events(chapter_id: str, events: list[Any]) -> dict[str, Any]:
    """从 facts 表的事件负载重建快照（与 snapshot_from_context 同构）。"""
    participants: list[str] = []
    facts: list[dict[str, str]] = []
    output_chars = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        for name in event.get("participants", []) or []:
            if str(name) and str(name) not in participants:
                participants.append(str(name))
        facts.append({"action": str(event.get("action", "")), "result": str(event.get("result", ""))})
    return {
        "chapter_id": chapter_id,
        "participants": participants[:30],
        "facts": facts[:40],
        "output_chars": output_chars,
    }


def rebuild_ledger_from_db(workspace: Path | str) -> dict[str, Any]:
    """从 SQLite facts 表重建整本连续性账本；数据库不存在时返回空账本。

    §8.9-7 / §9.7：只采信 SQLite 中处于 ``approved`` 的章节。已回滚
    （``rolled_back``）与依赖失效（``dependency_invalid``）的章节不再是长篇
    权威，其事实不得进入重建结果——否则回滚后的长效状态仍会"记得"被撤销的事实。
    """
    from .novel_state import db_path

    path = db_path(workspace)
    if not path.is_file():
        return _empty_ledger()
    grouped: dict[str, list[Any]] = {}
    try:
        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT f.chapter_id, f.object_value FROM facts f "
                "LEFT JOIN chapters c ON c.chapter_id = f.chapter_id "
                "WHERE c.status = 'approved' "
                "ORDER BY f.chapter_id, f.fact_id"
            ).fetchall()
    except sqlite3.Error:
        return _empty_ledger()
    for chapter_id, object_value in rows:
        try:
            event = json.loads(object_value)
        except (TypeError, ValueError):
            continue
        grouped.setdefault(str(chapter_id), []).append(event)
    chapters = [
        _snapshot_from_fact_events(chapter_id, events)
        for chapter_id, events in sorted(grouped.items())
    ]
    ledger = _empty_ledger()
    ledger["chapters"] = chapters
    ledger["latest"] = chapters[-1] if chapters else {}
    return ledger


def load_ledger(workspace: Path | str) -> dict[str, Any]:
    """读取连续性账本（派生缓存）。

    §9.3/§9.7：``continuity/state.json`` 只是 SQLite 的派生读缓存，因此
    **缺失** 或 **损坏** 都必须以 SQLite 为事实源重建并回写，而不是退化为空账本。
    """
    path = ledger_path(workspace)
    if not path.is_file():
        rebuilt = rebuild_ledger_from_db(workspace)
        if rebuilt.get("chapters"):
            _write_rebuilt_ledger(workspace, path, rebuilt, reason="missing")
        return rebuilt
    try:
        return read_json(path)
    except (OSError, ValueError):
        # 派生缓存损坏 → 从 SQLite（唯一事实源）重建并回写
        rebuilt = rebuild_ledger_from_db(workspace)
        write_json_atomic(path, rebuilt)
        _audit_rebuild(workspace, reason="corrupted", chapters=len(rebuilt.get("chapters", [])))
        return rebuilt


def _write_rebuilt_ledger(
    workspace: Path | str, path: Path, rebuilt: dict[str, Any], *, reason: str
) -> None:
    try:
        write_json_atomic(path, rebuilt)
    except OSError:
        return
    _audit_rebuild(workspace, reason=reason, chapters=len(rebuilt.get("chapters", [])))


def _audit_rebuild(workspace: Path | str, *, reason: str, chapters: int) -> None:
    """派生缓存重建留痕（重建失败不得影响读取路径）。"""
    try:
        from .audit import append_audit

        append_audit(
            Path(workspace) / "audit.jsonl",
            "continuity_rebuilt",
            reason=reason,
            chapters=chapters,
            source="sqlite",
        )
    except Exception:  # noqa: BLE001 - 审计失败不影响账本读取
        return


def snapshot_from_context(chapter_id: str, events: dict[str, Any], output: str) -> dict[str, Any]:
    event_rows = events.get("events", []) if isinstance(events, dict) else []
    participants: list[str] = []
    facts: list[dict[str, str]] = []
    for event in event_rows if isinstance(event_rows, list) else []:
        if not isinstance(event, dict):
            continue
        for name in event.get("participants", []) or []:
            if str(name) and str(name) not in participants:
                participants.append(str(name))
        facts.append({"action": str(event.get("action", "")), "result": str(event.get("result", ""))})
    return {
        "chapter_id": chapter_id,
        "participants": participants[:30],
        "facts": facts[:40],
        "output_chars": len(output or ""),
    }


def append_snapshot(workspace: Path | str, snapshot: dict[str, Any]) -> dict[str, Any]:
    doc = load_ledger(workspace)
    chapters = [item for item in doc.get("chapters", []) if item.get("chapter_id") != snapshot.get("chapter_id")]
    chapters.append(snapshot)
    chapters = chapters[-200:]
    doc["chapters"] = chapters
    doc["latest"] = snapshot
    path = ledger_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, doc)
    return doc


def relevant_context(workspace: Path | str, participants: list[str], limit: int = 3) -> list[dict[str, Any]]:
    """返回与当前角色重合的最近章节快照，供 Prompt 层按预算选择。"""
    rows = load_ledger(workspace).get("chapters", [])
    wanted = {str(item) for item in participants}
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        overlap = len(wanted.intersection(set(row.get("participants", []))))
        if overlap:
            scored.append((overlap, row))
    scored.sort(key=lambda item: (-item[0], item[1].get("chapter_id", "")), reverse=False)
    return [row for _score, row in scored[-limit:]]
