# -*- coding: utf-8 -*-
"""Batch 4 TDD：StateDeltaProposal 与 SQLite 两阶段提交。

- check（passed）只写提案 chapters/<id>/state_deltas/vNNN.proposed.json，
  不得提前写 continuity 或 SQLite；
- approve 是唯一提交点：intent → SQLite 事务 + continuity → committed；
  数据库写入失败时 approve 必须失败、状态不推进、continuity 不被污染；
- continuity/state.json 损坏时可从 SQLite 重建。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from v4.core.cli import main
from v4.core.manifest import ManifestStore
from tests.test_backend_cli import _build_workspace_with_chapter


def _run_cli(monkeypatch, ws: Path, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["novel", *[arg.replace("{ws}", str(ws)) for arg in args]])
    return main()


def _drive_to_passed(monkeypatch, ws: Path) -> None:
    assert _run_cli(monkeypatch, ws, "rewrite", str(ws), "--chapter", "ch-0001", "--backend", "placeholder") == 0
    assert _run_cli(monkeypatch, ws, "check", str(ws), "--chapter", "ch-0001") == 0


def test_check_passed_writes_proposal_not_db(tmp_path: Path, monkeypatch) -> None:
    """check 通过后只写 StateDeltaProposal；continuity 与 SQLite 保持零写入。"""
    ws = _build_workspace_with_chapter(tmp_path, state="ready_for_rewrite")
    _drive_to_passed(monkeypatch, ws)

    manifest = ManifestStore(ws).load()
    assert manifest["chapters"][0]["state"] == "passed"

    proposal_path = ws / "chapters" / "ch-0001" / "state_deltas" / "v001.proposed.json"
    assert proposal_path.is_file(), "check 通过必须落盘状态提案"
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert proposal["status"] == "proposed"
    assert proposal["chapter_id"] == "ch-0001"
    assert proposal["version"] == "v001"
    assert "snapshot" in proposal and "events" in proposal

    assert not (ws / "continuity" / "state.json").exists(), "check 不得提前写 continuity"
    assert not (ws / "state" / "novel_state.db").exists(), "check 不得提前写 SQLite"


def test_approve_two_phase_commit(tmp_path: Path, monkeypatch) -> None:
    """approve 提交 continuity + SQLite，journal 记录 committed。"""
    ws = _build_workspace_with_chapter(tmp_path, state="ready_for_rewrite")
    _drive_to_passed(monkeypatch, ws)
    assert _run_cli(monkeypatch, ws, "approve", str(ws), "--chapter", "ch-0001", "--version", "v001") == 0

    manifest = ManifestStore(ws).load()
    assert manifest["chapters"][0]["state"] == "approved"

    ledger = json.loads((ws / "continuity" / "state.json").read_text(encoding="utf-8"))
    assert any(row["chapter_id"] == "ch-0001" for row in ledger["chapters"])

    db = ws / "state" / "novel_state.db"
    assert db.is_file()
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status FROM chapters WHERE chapter_id='ch-0001'"
        ).fetchone()
    assert row is not None and row[0] == "approved"

    journal = json.loads((ws / "state" / "approval-journal.json").read_text(encoding="utf-8"))
    assert any(
        entry["chapter_id"] == "ch-0001" and entry["version"] == "v001" and entry["phase"] == "committed"
        for entry in journal
    )


def test_approve_rolls_back_on_db_failure(tmp_path: Path, monkeypatch) -> None:
    """SQLite 提交失败：approve 报错、状态留在 passed、continuity 不被写入。"""
    from v4.core import novel_state

    ws = _build_workspace_with_chapter(tmp_path, state="ready_for_rewrite")
    _drive_to_passed(monkeypatch, ws)

    def broken_commit(*args, **kwargs):
        raise RuntimeError("disk io error")

    monkeypatch.setattr(novel_state, "commit_approved_chapter", broken_commit)
    code = _run_cli(monkeypatch, ws, "approve", str(ws), "--chapter", "ch-0001", "--version", "v001")
    assert code == 1, "数据库失败时 approve 必须失败"

    manifest = ManifestStore(ws).load()
    assert manifest["chapters"][0]["state"] == "passed", "失败批准不得推进状态"
    assert not (ws / "continuity" / "state.json").exists(), "失败批准不得污染 continuity"

    journal = json.loads((ws / "state" / "approval-journal.json").read_text(encoding="utf-8"))
    assert any(
        entry["chapter_id"] == "ch-0001" and entry["phase"] == "failed"
        for entry in journal
    )


def test_corrupt_ledger_rebuilds_from_db(tmp_path: Path, monkeypatch) -> None:
    """continuity/state.json 损坏时，load_ledger 从 SQLite 重建。"""
    from v4.core import continuity, novel_state

    ws = _build_workspace_with_chapter(tmp_path, state="ready_for_rewrite")
    _drive_to_passed(monkeypatch, ws)
    assert _run_cli(monkeypatch, ws, "approve", str(ws), "--chapter", "ch-0001", "--version", "v001") == 0

    # 篡改账本为非法 JSON
    ledger_path = ws / "continuity" / "state.json"
    ledger_path.write_text("{corrupted!!", encoding="utf-8")

    rebuilt = continuity.load_ledger(ws)
    assert any(row["chapter_id"] == "ch-0001" for row in rebuilt["chapters"]), (
        "损坏账本必须能从 SQLite 重建"
    )
    # 重建结果回写账本
    fresh = continuity.load_ledger(ws)
    assert any(row["chapter_id"] == "ch-0001" for row in fresh["chapters"])
    _ = novel_state
