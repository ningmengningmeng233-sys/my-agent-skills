"""StateDeltaProposal：两阶段提交的第一阶段产物。

- ``check``（passed）只把即将发生的长篇状态变更写成提案
  ``chapters/<id>/state_deltas/vNNN.proposed.json``，绝不直接写
  continuity / SQLite；
- ``approve`` 是唯一提交点：读取提案 → 校验 → 事务提交（novel_state）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .audit import now_iso
from .jsonio import write_json_atomic

PROPOSAL_SCHEMA = "v4-state-delta-1"
PROPOSAL_STATUS = "proposed"


def deltas_dir(workspace: Path | str, chapter_id: str) -> Path:
    return Path(workspace) / "chapters" / chapter_id / "state_deltas"


def proposal_path(workspace: Path | str, chapter_id: str, version: str) -> Path:
    return deltas_dir(workspace, chapter_id) / f"{version}.proposed.json"


def validate_proposal(doc: Any) -> list[str]:
    """只读结构校验；返回问题列表（空列表 = 合法）。"""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["提案必须是 JSON 对象"]
    if doc.get("schema_version") != PROPOSAL_SCHEMA:
        errors.append(f"schema_version 必须是 {PROPOSAL_SCHEMA}")
    if doc.get("status") != PROPOSAL_STATUS:
        errors.append(f"status 必须是 {PROPOSAL_STATUS}")
    for key in ("chapter_id", "version", "proposed_at"):
        value = doc.get(key)
        if not isinstance(value, str) or not value:
            errors.append(f"{key} 必须是非空字符串")
    if not isinstance(doc.get("snapshot"), dict):
        errors.append("snapshot 必须是对象")
    if not isinstance(doc.get("events"), dict):
        errors.append("events 必须是对象")
    return errors


def propose_state_delta(
    workspace: Path | str,
    chapter_id: str,
    *,
    version: str,
    snapshot: dict[str, Any],
    events: dict[str, Any],
) -> Path:
    """把本章通过检查后的状态变更写为提案（原子写，覆盖同版本旧提案）。"""
    doc = {
        "schema_version": PROPOSAL_SCHEMA,
        "status": PROPOSAL_STATUS,
        "chapter_id": chapter_id,
        "version": version,
        "proposed_at": now_iso(),
        "snapshot": snapshot,
        "events": events,
    }
    path = proposal_path(workspace, chapter_id, version)
    write_json_atomic(path, doc)
    return path


def load_proposal(workspace: Path | str, chapter_id: str, version: str) -> dict[str, Any] | None:
    """读取并校验提案；不存在或非法时返回 None（approve 侧负责 fail-closed）。"""
    path = proposal_path(workspace, chapter_id, version)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if validate_proposal(doc):
        return None
    return doc
