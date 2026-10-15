"""将源稿接入 v4 工作区：快照、谨慎切分、预检和章节工作项登记。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import chapter_split, events
from .audit import append_audit, now_iso
from .decisions import decision_summary
from .fingerprints import chapter_fingerprints
from .hashing import sha256_file
from .jsonio import write_json_atomic
from .manifest import ManifestStore
from .paths import RULES_PATH
from .precheck import precheck_text, reconcile_auto_decisions
from .snapshot import snapshot_source, with_boundary_status


_APPENDIX_TITLE_RE = re.compile(
    r"(后记|附录|致谢|人物介绍|设定集|书评)"
)
_SKIP_TITLE_RE = re.compile(r"(插图|目录|版权|彩页)")


def _partition_texts(
    texts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """切分前把源文件分为正文、附录、跳过三类。

    - 后记/附录/致谢/人物介绍/设定集/书评 → 附录，走本地化后附在正文后输出；
    - 插图/目录/版权/彩页等非文本或索引页 → 跳过并登记 excluded_files。
    """
    body: list[dict[str, Any]] = []
    appendix: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for item in texts:
        source_path = str(item.get("source_path") or "")
        first_line = str(item.get("text") or "").splitlines()[0] if item.get("text") else ""
        name = f"{source_path}\n{first_line}"
        if _SKIP_TITLE_RE.search(name):
            skipped.append({"source_path": source_path, "reason": "non_prose"})
        elif _APPENDIX_TITLE_RE.search(name):
            appendix.append(item)
        else:
            body.append(item)
    return body, appendix, skipped

def empty_chapter_decisions(chapter_id: str) -> dict[str, Any]:
    return {
        "schema_version": "v4-chapter-decisions-1",
        "chapter_id": chapter_id,
        "revision": 1,
        "entries": [],
    }


def register_source(
    workspace: Path | str,
    source: Path | str,
    *,
    max_chapter_chars: int = chapter_split.DEFAULT_MAX_CHAPTER_CHARS,
    event_engine: str = "rule",
) -> dict[str, Any]:
    """创建章节工作项并登记 manifest；不伪造 AI 事件结论。"""
    workspace = Path(workspace)
    result = snapshot_source(workspace, source)
    body_texts, appendix_texts, skipped = _partition_texts(result.texts)
    texts_to_split = body_texts + appendix_texts
    if not texts_to_split:
        source_doc = with_boundary_status(result.source, "confirmed")
        source_doc["excluded_files"] = skipped

        def record_only(updated: dict[str, Any]) -> None:
            updated["source"] = source_doc

        store = ManifestStore(workspace)
        updated = store.update(record_only, event="source_non_body_registered")
        append_audit(
            workspace / "audit.jsonl",
            "ingest_completed",
            source=str(source),
            boundary_status="confirmed",
            chapter_ids=[],
            excluded_files=len(skipped),
            manifest_revision=updated["manifest_revision"],
        )
        return updated
    split = chapter_split.split_snapshot(
        texts_to_split, max_chapter_chars=max_chapter_chars
    )
    for candidate in split.chapters:
        source_name = candidate.source_path or candidate.title or ""
        candidate.appendix = bool(_APPENDIX_TITLE_RE.search(source_name))
    source_doc = with_boundary_status(result.source, split.boundary_status)
    body_candidates = [c for c in split.chapters if not c.appendix]
    appendix_candidates = [c for c in split.chapters if c.appendix]
    candidates = body_candidates + appendix_candidates
    for index, candidate in enumerate(candidates, start=1):
        candidate.ordinal = index
    if skipped:
        source_doc["excluded_files"] = skipped
    store = ManifestStore(workspace)
    manifest = store.load()
    project_decisions = workspace / manifest["project_decisions"]["path"]
    created: list[str] = []

    for candidate in candidates:
        chapter_id = f"ch-{candidate.ordinal:04d}"
        chapter_dir = workspace / "chapters" / chapter_id
        chapter_dir.mkdir(parents=True, exist_ok=True)
        source_path = chapter_dir / "source.md"
        source_path.write_text(candidate.text, encoding="utf-8", newline="\n")
        source_sha = sha256_file(source_path)
        graph = events.provisional_event_graph(
            chapter_id,
            source_sha,
            candidate.text,
            title=candidate.title,
        )
        if event_engine == "llm":
            try:
                graph = events.llm_event_graph(
                    chapter_id,
                    source_sha,
                    candidate.text,
                    title=candidate.title,
                    audit_path=workspace / "audit.jsonl",
                )
            except Exception as exc:  # noqa: BLE001 - LLM 失败回退规则图
                append_audit(
                    workspace / "audit.jsonl",
                    "llm_event_graph_fallback",
                    chapter_id=chapter_id,
                    reason=f"llm_event_graph_error: {exc}",
                )
        events_path = chapter_dir / "events.json"
        write_json_atomic(events_path, graph)
        decisions_path = chapter_dir / "decisions.json"
        precheck = precheck_text(candidate.text, chapter_id, title=candidate.title)
        write_json_atomic(chapter_dir / "precheck.json", precheck.to_dict())
        decisions_doc = empty_chapter_decisions(chapter_id)
        decisions_doc, _precheck_changes = reconcile_auto_decisions(precheck, decisions_doc)
        write_json_atomic(decisions_path, decisions_doc)
        (chapter_dir / "audit.jsonl").touch()

        state = "awaiting_boundary" if not candidate.explicit else "awaiting_decision"
        blockers = []
        if not candidate.explicit:
            blockers.append({
                "code": "BOUNDARY_PENDING",
                "kind": "boundary",
                "message": "章节边界来自候选切分，等待用户确认",
                "opened_at": now_iso(),
            })
        if events.has_open_high_risk(graph):
            blockers.append({
                "code": "EVENT_GRAPH_PENDING",
                "kind": "event",
                "message": "事件图为低置信度初稿，等待用户/AI确认高风险节点",
                "opened_at": now_iso(),
            })
        if precheck.blocked:
            blockers.append({
                "code": "SOURCE_PRECHECK_PENDING",
                "kind": "contradiction",
                "message": "源稿预检发现确定或疑似问题，等待 decisions.json 闭环",
                "opened_at": now_iso(),
            })

        chapter = {
            "id": chapter_id,
            "ordinal": candidate.ordinal,
            "title": candidate.title or f"第{candidate.ordinal}章（候选）",
            "kind": "appendix" if candidate.appendix else "body",
            "source_range": {"start": candidate.start_line, "end": candidate.end_line},
            "state": state,
            "paths": {
                "source": f"chapters/{chapter_id}/source.md",
                "events": f"chapters/{chapter_id}/events.json",
                "decisions": f"chapters/{chapter_id}/decisions.json",
                "output": f"chapters/{chapter_id}/output.md",
                "checks": f"chapters/{chapter_id}/checks.json",
                "audit": f"chapters/{chapter_id}/audit.jsonl",
                "precheck": f"chapters/{chapter_id}/precheck.json",
            },
            "fingerprints": chapter_fingerprints(
                source=source_path,
                events=events_path,
                project_decisions=project_decisions,
                chapter_decisions=decisions_path,
                rules=RULES_PATH,
                output=chapter_dir / "output.md",
                checks=chapter_dir / "checks.json",
            ),
            "checks": {
                "hard_gate": "not_run",
                "event_comparison": "not_run",
                "last_run": None,
            },
            "blockers": blockers,
            "decisions": decision_summary(decisions_doc),
            "enhancements": {
                "reference_assets_used": False,
                "style_depth_required": False,
                "blind_review": {"enabled": False, "result": "not_enabled"},
            },
            "updated_at": now_iso(),
        }
        created.append(chapter_id)

        def add_chapter(updated: dict[str, Any], chapter: dict[str, Any] = chapter) -> None:
            updated["chapters"] = [
                item for item in updated["chapters"] if item["id"] != chapter["id"]
            ] + [chapter]
            updated["chapters"].sort(key=lambda item: item["ordinal"])

        manifest = store.update(add_chapter, event="chapter_registered")
        append_audit(
            workspace / "audit.jsonl",
            "precheck_recorded",
            chapter_id=chapter_id,
            blocked=precheck.blocked,
            finding_count=len(precheck.findings),
        )

    def update_source(updated: dict[str, Any]) -> None:
        updated["source"] = source_doc

    manifest = store.update(update_source, event="source_snapshot_registered")
    append_audit(
        workspace / "audit.jsonl",
        "ingest_completed",
        source=str(source),
        boundary_status=split.boundary_status,
        chapter_ids=created,
        reasons=split.reasons[:10],
        manifest_revision=manifest["manifest_revision"],
    )
    return manifest
