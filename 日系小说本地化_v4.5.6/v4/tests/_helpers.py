"""v4 Core 测试公共辅助函数。"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from v4.core import decisions, events, fingerprints  # noqa: E402
from v4.core.audit import append_audit, now_iso  # noqa: E402
from v4.core.hashing import sha256_bytes  # noqa: E402
from v4.core.jsonio import read_json, write_json_atomic  # noqa: E402
from v4.core.paths import RULES_PATH  # noqa: E402
from v4.core.workspace import create_workspace  # noqa: E402

SOURCE_TEXT = """第一章 相遇

森田推门进入大厅。

森田向老板说明来意，老板答应借宿。

两人在灯下交谈，森田心中平静。
"""


def _build_events(chapter_id: str = "ch-001") -> dict:
    source_sha = sha256_bytes(SOURCE_TEXT.encode("utf-8"))
    ev1 = events.new_event(
        "e-0001", 1, participants=["森田"], action="推门进入大厅",
        result="进入大厅", world_function="无", confidence=0.9,
    )
    ev2 = events.new_event(
        "e-0002", 2, participants=["森田", "老板"], prerequisites=["e-0001"],
        action="向老板说明来意", result="老板答应借宿", world_function="无",
        confidence=0.9,
    )
    return events.build_event_graph(chapter_id, source_sha, [ev1, ev2])


def _empty_chapter_decisions(chapter_id: str = "ch-001") -> dict:
    return {
        "schema_version": "v4-chapter-decisions-1",
        "chapter_id": chapter_id,
        "revision": 1,
        "entries": [],
    }


def _make_chapter(
    ws: Path,
    *,
    chapter_id: str,
    ordinal: int,
    title: str,
    state: str,
    source_text: str = SOURCE_TEXT,
    events_doc: dict | None = None,
    decisions_doc: dict | None = None,
    output_text: str | None = None,
    checks_summary: dict | None = None,
) -> dict:
    ch_dir = ws / "chapters" / chapter_id
    ch_dir.mkdir(parents=True, exist_ok=True)
    (ch_dir / "source.md").write_text(source_text, encoding="utf-8")
    (ch_dir / "events.json").write_text(
        json.dumps(events_doc or _build_events(chapter_id), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (ch_dir / "decisions.json").write_text(
        json.dumps(decisions_doc or _empty_chapter_decisions(chapter_id), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (ch_dir / "audit.jsonl").touch()
    if output_text is not None:
        (ch_dir / "output.md").write_text(output_text, encoding="utf-8")
    # v4.5.0-dev.3：approved/delivered 章节必须带不可变版本工件与批准指针
    if state in {"approved", "delivered"} and output_text is not None:
        from v4.core.version_store import save_version

        record = save_version(ch_dir, output_text, kind="draft")
        (ch_dir / "current.json").write_text(
            json.dumps(
                {
                    "latest_version": 1,
                    "latest_sha256": record["sha256"],
                    "latest_kind": "draft",
                    "approved_version": 1,
                    "approved_sha256": record["sha256"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    fingerprints_rec = fingerprints.chapter_fingerprints(
        source=ch_dir / "source.md",
        events=ch_dir / "events.json",
        project_decisions=ws / "project-decisions.json",
        chapter_decisions=ch_dir / "decisions.json",
        rules=RULES_PATH,
        output=ch_dir / "output.md",
        checks=ch_dir / "checks.json",
    )
    append_audit(ws / "audit.jsonl", "state_change_detail", chapter_id=chapter_id, to=state)
    return {
        "id": chapter_id,
        "ordinal": ordinal,
        "title": title,
        "state": state,
        "paths": {
            "source": f"chapters/{chapter_id}/source.md",
            "events": f"chapters/{chapter_id}/events.json",
            "decisions": f"chapters/{chapter_id}/decisions.json",
            "output": f"chapters/{chapter_id}/output.md",
            "checks": f"chapters/{chapter_id}/checks.json",
            "audit": f"chapters/{chapter_id}/audit.jsonl",
        },
        "fingerprints": fingerprints_rec,
        "checks": checks_summary or {
            "hard_gate": "not_run",
            "event_comparison": "not_run",
            "last_run": None,
        },
        "blockers": [],
        "decisions": decisions.decision_summary(decisions_doc or _empty_chapter_decisions(chapter_id)),
        "enhancements": {
            "reference_assets_used": False,
            "blind_review": {"enabled": False, "result": "not_enabled"},
        },
        "updated_at": now_iso(),
    }


def _build_workspace(tmp: Path, chapters: list[dict], *, name: str | None = None) -> Path:
    ws = tmp / (name or f"ws-{len(list(tmp.iterdir()))}")
    create_workspace(ws, title="测试小说", author="测试", input_language="zh-CN")

    # 测试里常先在 tmp_path 下创建章节，再创建子目录工作区；
    # 把外部 chapters/ 目录移进工作区，保证路径一致。
    external_chapters = tmp / "chapters"
    if external_chapters.exists() and external_chapters.is_dir():
        target_chapters = ws / "chapters"
        target_chapters.mkdir(parents=True, exist_ok=True)
        for item in external_chapters.iterdir():
            dest = target_chapters / item.name
            if dest.exists():
                shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
            shutil.move(str(item), str(dest))
        try:
            external_chapters.rmdir()
        except OSError:
            pass

    manifest = read_json(ws / "manifest.json")
    manifest["chapters"] = chapters
    project_decisions_path = ws / manifest["project_decisions"]["path"]
    # 重新计算指纹，使 project_decisions 等在工作区创建后才存在的文件哈希正确。
    for chapter in chapters:
        ch_dir = ws / "chapters" / chapter["id"]
        if not ch_dir.exists():
            continue
        chapter["fingerprints"] = fingerprints.chapter_fingerprints(
            source=ch_dir / "source.md",
            events=ch_dir / "events.json",
            project_decisions=project_decisions_path,
            chapter_decisions=ch_dir / "decisions.json",
            rules=RULES_PATH,
            output=ch_dir / "output.md",
            checks=ch_dir / "checks.json",
        )
    write_json_atomic(ws / "manifest.json", manifest)
    for chapter in chapters:
        append_audit(
            ws / "audit.jsonl",
            "state_change_detail",
            chapter_id=chapter["id"],
            to=chapter["state"],
        )
    return ws


def _passed_checks_summary() -> dict:
    return {
        "hard_gate": "pass",
        "event_comparison": "pass",
        "last_run": now_iso(),
        "warning_count": 0,
        "failure_count": 0,
    }
