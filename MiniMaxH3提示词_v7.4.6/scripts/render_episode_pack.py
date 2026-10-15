#!/usr/bin/env python3
"""Task 6: atomically render the four episode deliverables from a validated plan.

Pipeline position: script → beats (Task 3/4) → episode plan (Task 5 schema) →
THIS renderer → ``episode-timeline.md`` + ``prompts/GNNN.md`` +
``prompt-pack.md`` + ``duration-manifest.json`` (设计 §10.1—§10.4).

Contracts honored here:
- The duration / coverage verdict is NEVER re-judged locally: every plan goes
  through ``episode_contract.validate_episode_plan`` (Task 5, strict on-disk
  config). A FAIL plan renders nothing; ``REVIEW_REQUIRED`` renders only the
  duration review summary + machine manifest (never the production pack).
  The renderer accepts ``authoritative_target_seconds`` to arm the gate's
  anti-shrink cross-check; when it is not supplied the manifest records
  ``authoritative_target_check: "not_performed"`` so the disabled defense
  stays visible (the Task 9 total gate MUST pass the script-parsed target).
- Every ``prompt_text`` is audited through the existing audit chain
  (``generate_output.run_real_audit_cli``, byte-exact file hand-off). One
  failed prompt refuses the whole episode: nothing is swapped into the real
  directory, and the staging slot keeps an ``audit-refusal.json`` forensics
  document (clip id, failed rules, prompt sha). The audit channel is pinned
  by the plan's workflow; an UNKNOWN workflow is refused — never auto-detected
  — otherwise a plan author could downgrade the audit by renaming the
  workflow. The ``REVIEW_REQUIRED`` path emits no prompt artifacts, so it
  runs no audits either — its contract is the duration decision only.
- Atomicity reuses ``generate_output`` primitives: realpath boundary
  (``ensure_within_root``), fresh staging slot (``create_staging_dir``) and
  the backup/restore rename (``atomic_swap``). ``--replace`` completes every
  check (gate, policy, all audits) BEFORE touching the previous output, and
  the target-compatibility guard is re-checked immediately before each swap
  (the residual race window between re-check and rename is a documented
  boundary, shared with generate_output's semantics).
- Freeze decision #22: ``ClipDraft`` only carries ``dialogue_text``, so
  non-dialogue beat text must be reverse-looked-up via ``beat_ids``
  (``action_beats_for_clip``). The dict-plan path renders 动作节拍 from the
  plan's own action source units; the lookup contract is what ClipDraft-side
  writers (the Task 10 e2e glue) must call so action text cannot be silently
  dropped.
- Time codes appear ONLY in the ledger, prompt titles and the manifest —
  never injected into prompt bodies (设计 §9). Prompt files are byte-exact
  copies of ``prompt_text`` and their sha256 is bound into the manifest.

Exit codes: 0 rendered; 1 refused (FAIL plan, audit failure, unusable config,
non-empty target without --replace, unreadable plan file); 2 REVIEW_REQUIRED
(review pair written); 3 internal crash (CLI wrapper).

CLI:
    python minimax-h3-craft/scripts/render_episode_pack.py --plan episode-plan.json --out OUTPUT [--replace]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import sys
from typing import Any, Iterable, Optional, Sequence

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from generate_output import (  # noqa: E402
    atomic_swap,
    create_staging_dir,
    git_head,
    run_real_audit_cli,
)
from duration_planner import KIND_DIALOGUE, Beat  # noqa: E402
from episode_contract import (  # noqa: E402
    SOURCE_KIND_ACTION,
    SOURCE_KIND_CLUE,
    SOURCE_KIND_DIALOGUE,
    SOURCE_KIND_STATE_CHANGE,
    SOURCE_KIND_SYSTEM_TRANSITION,
    STATUS_PASS_EXACT,
    STATUS_PASS_WITH_TRIM,
    STATUS_REVIEW_REQUIRED,
    EpisodeContractError,
    load_model_capabilities,
    validate_episode_plan,
)
from versioning import load_duration_policy  # noqa: E402

EXIT_RENDERED = 0
EXIT_REFUSED = 1
EXIT_REVIEW_REQUIRED = 2
EXIT_CRASHED = 3

TIMELINE_NAME = "episode-timeline.md"
PROMPT_PACK_NAME = "prompt-pack.md"
MANIFEST_NAME = "duration-manifest.json"
REVIEW_NAME = "duration-review.md"
PROMPTS_DIRNAME = "prompts"

AUDIT_MODE_BY_WORKFLOW = {
    "native-drama-series": "native-zh",
    "native-general": "native-general",
    "ref2va-en": "ref2va-en",
}

KIND_LABELS = {
    SOURCE_KIND_DIALOGUE: "台词",
    SOURCE_KIND_ACTION: "动作",
    SOURCE_KIND_CLUE: "线索",
    SOURCE_KIND_STATE_CHANGE: "状态变化",
    SOURCE_KIND_SYSTEM_TRANSITION: "系统过渡",
}


class UnknownBeatReferenceError(RuntimeError):
    """A clip claims a beat_id that does not exist — fail closed, never drop."""


def audit_mode_for_workflow(workflow: str) -> Optional[str]:
    """Map the plan's workflow to the audit channel; ``None`` for unknown.

    An unknown workflow REFUSES (see :func:`render_episode_pack`) instead of
    falling back to ``auto``: auto-detection re-classifies the prompt by its
    text, which would let a plan author downgrade the audit channel just by
    renaming the workflow (评审 F1, Block).
    """
    return AUDIT_MODE_BY_WORKFLOW.get(workflow)


def action_beats_for_clip(beats: Sequence[Beat], beat_ids: Iterable[str]) -> tuple:
    """冻结决定 #22：按 ``beat_ids`` 反查 ``Beat``（保序），供台账「动作节拍」列。

    ``ClipDraft`` 只携带 ``dialogue_text``，非台词原文必须从这里拿 ``Beat.text``。
    未知 ID 与重复引用一律抛 :class:`UnknownBeatReferenceError`（隐性丢弃就是
    内容丢失，隐性重复就是二次渲染）；台词节拍被排除（它们已逐字存在于
    ``ClipDraft.dialogue_text``，不得二次渲染）。
    """
    by_id: dict = {}
    for beat in beats:
        if beat.id in by_id:
            raise UnknownBeatReferenceError(f"重复的 Beat id: {beat.id!r}")
        by_id[beat.id] = beat
    resolved: list = []
    missing: list = []
    claimed: set = set()
    for beat_id in beat_ids:
        if beat_id in claimed:
            raise UnknownBeatReferenceError(
                f"beat_ids 内重复引用同一 Beat: {beat_id!r}")
        claimed.add(beat_id)
        beat = by_id.get(beat_id)
        if beat is None:
            missing.append(beat_id)
        elif beat.kind != KIND_DIALOGUE:
            resolved.append(beat)
    if missing:
        raise UnknownBeatReferenceError(
            "beat_ids 反查失败，未知 Beat id: " + ", ".join(repr(m) for m in missing)
        )
    return tuple(resolved)


def _refuse(message: str) -> int:
    print(f"REFUSED: {message}", file=sys.stderr)
    return EXIT_REFUSED


def _load_plan(plan: Any) -> tuple:
    """Accept a parsed plan dict or a path to its JSON document."""
    if isinstance(plan, dict):
        return plan, None
    path = os.fspath(plan)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle), None
    except OSError as exc:
        return None, f"读取不到计划文件 {path}: {exc}"
    except ValueError as exc:
        return None, f"计划文件不是合法 JSON（{path}）: {exc}"


def _fmt_seconds(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _write_json(path: str, document: dict) -> None:
    _write_text(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def _units_by_id(document: dict) -> dict:
    return {
        unit["id"]: unit
        for unit in document.get("source_units") or []
        if isinstance(unit, dict) and isinstance(unit.get("id"), str)
    }


def _span_text(unit: Optional[dict], ref: dict) -> Optional[str]:
    if not isinstance(unit, dict):
        return None
    text = unit.get("text")
    if not isinstance(text, str):
        return None
    return text[ref["span_start"]: ref["span_end"]]


def _clip_row(clip: dict, units: dict, policy: dict) -> str:
    start = clip["start_seconds"]
    duration = clip["duration_seconds"]
    range_cells = []
    speakers: list = []
    dialogue_chars = 0
    action_texts: list = []
    for ref in clip["source_refs"]:
        unit = units.get(ref["source_id"])
        kind = unit.get("kind") if isinstance(unit, dict) else None
        label = KIND_LABELS.get(kind, str(kind))
        range_cells.append(
            f"{ref['source_id']}[{ref['span_start']}—{ref['span_end']}) {label}"
        )
        if kind == SOURCE_KIND_DIALOGUE:
            dialogue_chars += ref["span_end"] - ref["span_start"]
            raw_speakers = unit.get("speaker_ids")
            if raw_speakers is None:
                pass
            elif isinstance(raw_speakers, (list, tuple)):
                for speaker in raw_speakers:
                    if isinstance(speaker, str) and speaker and speaker not in speakers:
                        speakers.append(speaker)
                if any(not isinstance(speaker, str) or not speaker
                       for speaker in raw_speakers):
                    speakers.append("（speaker_ids 含非法项）")
            else:
                speakers.append("（speaker_ids 格式异常）")
        elif kind == SOURCE_KIND_ACTION:
            span_text = _span_text(unit, ref)
            action_texts.append(span_text if span_text else "（未申报原文）")

    cps = policy.get("speech_chars_per_second") or {}
    if dialogue_chars:
        speech = (
            f"{dialogue_chars}字（按{cps.get('min')}—{cps.get('max')}字/秒"
            f"≈{dialogue_chars / cps['max']:.1f}—{dialogue_chars / cps['min']:.1f}秒）"
        )
    else:
        speech = "本段无对白"

    padding = policy.get("padding_policy") or {}
    trims = clip["trim_candidates"]
    if trims:
        trim_cell = "；".join(
            f"{t['at_seconds']}秒（{t['reason']}）" for t in trims
        )
        padding_cell = (
            f"{_fmt_seconds(padding.get('min_seconds'))}—"
            f"{_fmt_seconds(padding.get('max_seconds'))}秒（选择性）"
        )
    else:
        trim_cell = "—"
        padding_cell = "—"

    transitions = clip["system_added_transitions"]
    return "|".join(
        [
            clip["id"],
            f"{_fmt_seconds(start)}—{_fmt_seconds(start + duration)}秒",
            f"{_fmt_seconds(duration)}秒",
            "；".join(range_cells) or "—",
            "、".join(speakers) if speakers else "未申报",
            speech,
            "；".join(action_texts) if action_texts else "—",
            clip["start_state"],
            clip["end_state"],
            "、".join(transitions) if transitions else "—",
            padding_cell,
            trim_cell,
        ]
    )


def _build_timeline(document: dict, report, policy: dict) -> str:
    units = _units_by_id(document)
    low, high = report.clip_range_seconds if report.clip_range_seconds else ("?", "?")
    padding = policy.get("padding_policy") or {}
    cps = policy.get("speech_chars_per_second") or {}
    header = "\n".join(
        [
            f"# 整集台账 — {document['episode_id']}",
            f"- 工作流格式：{document['workflow']}",
            f"- 目标模型：{document['model']}（有效片段范围 "
            f"{_fmt_seconds(low)}—{_fmt_seconds(high)} 秒）",
            f"- 目标时长：{_fmt_seconds(report.script_target_seconds)} 秒；"
            f"计划时长：{_fmt_seconds(report.planned_prompt_seconds)} 秒；"
            f"时长状态：{report.status}",
            f"- 覆盖率：{report.coverage_ratio * 100:.1f}%；"
            f"时间线连续：{'是' if report.timeline_contiguous else '否'}",
            f"- 台词语速口径：{_fmt_seconds(cps.get('min'))}—{_fmt_seconds(cps.get('max'))} 字/秒",
            f"- 剪辑余量政策：{padding.get('mode')}"
            f"（{_fmt_seconds(padding.get('min_seconds'))}—"
            f"{_fmt_seconds(padding.get('max_seconds'))} 秒，仅在标记处添加）",
            "",
            "| " + " | ".join(
                [
                    "片段ID",
                    "时间码",
                    "计划时长",
                    "剧本原文范围",
                    "在场角色与说话人",
                    "台词字数及估算语速",
                    "动作节拍",
                    "起始状态",
                    "结束状态",
                    "系统补充过渡",
                    "剪辑余量",
                    "安全裁剪位置",
                ]
            ) + " |",
            "|" + "---|" * 12,
        ]
    )
    rows = [_clip_row(clip, units, policy) for clip in document["clips"]]
    return header + "\n" + "\n".join(f"| {row} |" for row in rows) + "\n"


def _build_prompt_pack(document: dict, report) -> str:
    parts = [
        f"# 整集提示词包 — {document['episode_id']}",
        f"- 时长摘要：目标 {_fmt_seconds(report.script_target_seconds)} 秒 / "
        f"计划 {_fmt_seconds(report.planned_prompt_seconds)} 秒（{report.status}）；"
        f"覆盖率 {report.coverage_ratio * 100:.1f}%；片段数 {len(document['clips'])}",
        f"- 工作流格式：{document['workflow']}",
        "",
    ]
    for clip in document["clips"]:
        start = clip["start_seconds"]
        end = start + clip["duration_seconds"]
        parts.append(f"## {clip['id']}（{_fmt_seconds(start)}—{_fmt_seconds(end)}秒）")
        parts.append("")
        parts.append(clip["prompt_text"])
        parts.append("---")
    return "\n".join(parts) + "\n"


def _build_review(document: dict, report, policy: dict) -> str:
    trims = [
        f"{t.clip_id} {t.at_seconds}秒（{t.reason}）"
        for t in report.trim_candidates
    ]
    overage = report.overage_seconds or 0
    ratio = report.overage_ratio or 0.0
    review_ratio = policy.get("overage_review_ratio")
    ratio_percent = f"{review_ratio * 100:g}%" if isinstance(review_ratio, (int, float)) else "（政策值缺失）"
    ratio_multiple = f"{1 + review_ratio:g}" if isinstance(review_ratio, (int, float)) else "1.2"
    return "\n".join(
        [
            f"# 时长审查摘要 — {document['episode_id']}",
            f"- 状态：REVIEW_REQUIRED（计划时长超过目标的 {ratio_percent} 上限，自动渲染暂停）",
            f"- 目标时长：{_fmt_seconds(report.script_target_seconds)} 秒；"
            f"计划时长：{_fmt_seconds(report.planned_prompt_seconds)} 秒；"
            f"超出：{_fmt_seconds(overage)} 秒（超出比例 {ratio * 100:.1f}%）",
            f"- may_render_full_pack: {str(report.may_render_full_pack).lower()}",
            f"- 错误码：{'、'.join(report.error_codes) or '（无）'}",
            f"- 警告码：{'、'.join(report.warning_codes) or '（无）'}",
            f"- 安全裁剪位置：{'；'.join(trims) if trims else '（无）'}",
            f"- 处理指引：请人工裁决——按安全裁剪位置把计划修剪到 {ratio_multiple} 倍目标以内后重渲，"
            "或修订计划素材；不得为过门禁下调剧本声明时长。",
            "",
        ]
    )


def _build_manifest(document: dict, report, rendered_clips: Optional[list],
                    authoritative_check: str) -> dict:
    manifest = {
        "schema_version": 1,
        "episode_id": document["episode_id"],
        "model": document["model"],
        "workflow": document["workflow"],
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "commit": git_head(),
        "duration_status": report.status,
        "may_render_full_pack": report.may_render_full_pack,
        "authoritative_target_check": authoritative_check,
        "error_codes": list(report.error_codes),
        "warning_codes": list(report.warning_codes),
        "script_target_seconds": report.script_target_seconds,
        "planned_prompt_seconds": report.planned_prompt_seconds,
        "overage_seconds": report.overage_seconds,
        "overage_ratio": report.overage_ratio,
        "clip_range_seconds": (
            list(report.clip_range_seconds) if report.clip_range_seconds else None
        ),
        "timeline_contiguous": report.timeline_contiguous,
        "coverage_ratio": report.coverage_ratio,
        "missing_source_units": list(report.missing_source_units),
        "duplicate_source_units": list(report.duplicate_source_units),
        "overlapping_source_units": list(report.overlapping_source_units),
        "system_added_transitions": list(report.system_added_transitions),
        "trim_candidates": [
            {"clip_id": t.clip_id, "at_seconds": t.at_seconds, "reason": t.reason}
            for t in report.trim_candidates
        ],
        "real_model_render_status": document["real_model_render_status"],
        "actual_generated_seconds": document["actual_generated_seconds"],
        "final_edit_seconds": document["final_edit_seconds"],
    }
    if rendered_clips is not None:
        manifest["clips"] = rendered_clips
    return manifest


def _dump_audit_refusal(staging: str, document: dict, clip: dict, detail: dict) -> None:
    """Leave a forensics document for a refused prompt inside the staging slot."""
    document_note = {
        "episode_id": document.get("episode_id"),
        "clip_id": clip.get("id"),
        "refused_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    document_note.update(detail)
    _write_json(os.path.join(staging, "audit-refusal.json"), document_note)


def _audit_all_clips(document: dict, audit_mode: str, staging: str):
    """Audit every prompt through the real CLI; refuse the episode on one failure.

    Audits run concurrently (each prompt is judged independently, so verdicts
    are identical to serial auditing) while the verdict scan stays serial and
    in clip order, so the FIRST failing clip in document order is the one
    reported and the forensics dump is written single-threaded.
    """
    clips = document["clips"]
    if not clips:
        return [], None
    reports: list = [None] * len(clips)
    crash: Optional[RuntimeError] = None
    max_workers = min(8, len(clips))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(run_real_audit_cli, clip["prompt_text"], audit_mode): index
            for index, clip in enumerate(clips)
        }
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                reports[index] = future.result()
            except RuntimeError as exc:
                crash = exc

    audited = []
    for clip, audit_report in zip(clips, reports):
        if crash is not None:
            _dump_audit_refusal(staging, document, clip,
                                {"reason": "audit_process_error", "error": str(crash)})
            return None, f"审计进程异常（片段 {clip['id']}），整集拒绝交换: {crash}"
        if audit_report is None:
            _dump_audit_refusal(staging, document, clip,
                                {"reason": "audit_process_error", "error": "no report"})
            return None, f"审计进程异常（片段 {clip['id']}），整集拒绝交换: 无报告"
        failed = audit_report.get("failed")
        if failed is None:
            _dump_audit_refusal(staging, document, clip,
                                {"reason": "audit_report_missing_failed",
                                 "report_keys": sorted(audit_report.keys())})
            return None, (
                f"片段 {clip['id']} 的审计报告缺少 failed 计数（fail-closed），整集拒绝交换"
            )
        failed_rules = [
            result.get("rule_id")
            for result in audit_report.get("results") or []
            if result.get("status") == "FAIL"
        ]
        if failed > 0:
            _dump_audit_refusal(staging, document, clip,
                                {"reason": "audit_rules_failed", "failed_rule_ids": failed_rules})
            return None, (
                f"片段 {clip['id']} 审计未通过（{failed_rules}），"
                "任一单条审计失败整集不交换"
            )
        audited.append((clip, audit_report))
    return audited, None


def _target_guard_violation(out_abs: str, replace: bool) -> Optional[str]:
    """Compatibility of the target with a fresh render (checked twice: up
    front and immediately before each swap — the residual race window in
    between is a documented boundary, not a lock)."""
    if os.path.exists(out_abs) and not os.path.isdir(out_abs):
        return f"输出目标已存在且不是目录: {out_abs}"
    if os.path.isdir(out_abs) and os.listdir(out_abs) and not replace:
        return f"输出目录非空且未指定 --replace，拒绝覆盖: {out_abs}"
    return None


def _render_full_pack(document: dict, report, policy: dict, out_abs: str, replace: bool,
                      authoritative_check: str) -> int:
    audit_mode = audit_mode_for_workflow(document["workflow"])
    if audit_mode is None:
        return _refuse(
            f"未知工作流 {document['workflow']!r}：渲染器只能审计 "
            f"{sorted(AUDIT_MODE_BY_WORKFLOW)} 内的通道，拒绝靠 auto 探测降级审计"
        )

    out_parent = os.path.dirname(out_abs)
    os.makedirs(out_parent, exist_ok=True)
    staging = create_staging_dir(out_parent, os.path.basename(out_abs))

    audited, audit_error = _audit_all_clips(document, audit_mode, staging)
    if audit_error:
        return _refuse(audit_error)

    # 评审 F2：审计耗时不定，交换前一刻复检目标兼容性，收窄竞态窗口。
    guard_error = _target_guard_violation(out_abs, replace)
    if guard_error:
        return _refuse(f"交换前复检失败: {guard_error}")

    prompts_dir = os.path.join(staging, PROMPTS_DIRNAME)
    os.makedirs(prompts_dir, exist_ok=True)

    clip_entries = []
    for index, (clip, audit_report) in enumerate(audited, start=1):
        file_name = f"G{index:03d}.md"
        prompt_path = os.path.join(prompts_dir, file_name)
        _write_text(prompt_path, clip["prompt_text"])
        with open(prompt_path, "rb") as handle:
            disk_sha = hashlib.sha256(handle.read()).hexdigest()
        if disk_sha != audit_report.get("prompt_sha256"):
            raise RuntimeError(
                f"audit binding failed for clip {clip['id']}: prompt sha mismatch"
            )
        clip_entries.append(
            {
                "clip_id": clip["id"],
                "prompt_file": f"{PROMPTS_DIRNAME}/{file_name}",
                "prompt_sha256": audit_report.get("prompt_sha256"),
                "duration_seconds": clip["duration_seconds"],
                "audit_failed": audit_report.get("failed", 0),
            }
        )

    _write_text(os.path.join(staging, TIMELINE_NAME), _build_timeline(document, report, policy))
    _write_text(os.path.join(staging, PROMPT_PACK_NAME), _build_prompt_pack(document, report))
    _write_json(
        os.path.join(staging, MANIFEST_NAME),
        _build_manifest(document, report, rendered_clips=clip_entries,
                        authoritative_check=authoritative_check),
    )
    atomic_swap(staging, out_abs)
    return EXIT_RENDERED


def _render_review_pair(document: dict, report, policy: dict, out_abs: str, replace: bool,
                        authoritative_check: str) -> int:
    out_parent = os.path.dirname(out_abs)
    os.makedirs(out_parent, exist_ok=True)
    staging = create_staging_dir(out_parent, os.path.basename(out_abs))
    guard_error = _target_guard_violation(out_abs, replace)
    if guard_error:
        return _refuse(f"交换前复检失败: {guard_error}")
    _write_text(os.path.join(staging, REVIEW_NAME), _build_review(document, report, policy))
    _write_json(
        os.path.join(staging, MANIFEST_NAME),
        _build_manifest(document, report, rendered_clips=None,
                        authoritative_check=authoritative_check),
    )
    atomic_swap(staging, out_abs)
    return EXIT_REVIEW_REQUIRED


def render_episode_pack(
    plan: Any,
    out: Any,
    replace: bool = False,
    authoritative_target_seconds: Optional[float] = None,
) -> int:
    """Render one episode plan into the four deliverables, atomically.

    Args:
        plan: Parsed ``episode-plan.json`` dict, or a path to the JSON file.
        out: Output directory (created; must be empty or ``replace=True``).
        replace: Allow swapping an existing output after FULL re-validation.
        authoritative_target_seconds: The target parsed from the screenplay
            header (Task 3). Forwarded to the gate's ``SCRIPT_TARGET_MISMATCH``
            cross-check; ``None`` leaves that check OFF and the manifest
            records ``authoritative_target_check: "not_performed"``.

    Returns:
        Exit code: 0 rendered, 1 refused, 2 REVIEW_REQUIRED (review pair only).
    """
    document, load_error = _load_plan(plan)
    if load_error:
        return _refuse(load_error)

    # 优化轮：配置只读盘一次，policy/capabilities 同时喂给门禁与台账/审查文案，
    # 消除"两次读盘可能读到不同内容"的裂缝；fail-closed 顺序不变（配置先于计划判定）。
    try:
        policy = load_duration_policy()
        capabilities = load_model_capabilities()
    except Exception as exc:  # noqa: BLE001 — fail closed on any unusable config
        return _refuse(f"门禁配置不可用，整集拒绝渲染: {exc}")

    try:
        report = validate_episode_plan(
            document, model_capabilities=capabilities, policy=policy,
            authoritative_target_seconds=authoritative_target_seconds,
        )
    except EpisodeContractError as exc:
        return _refuse(f"门禁配置不可用，整集拒绝渲染: {exc}")

    out_abs = os.path.abspath(os.fspath(out))
    guard_error = _target_guard_violation(out_abs, replace)
    if guard_error:
        return _refuse(guard_error)

    authoritative_check = (
        "performed" if authoritative_target_seconds is not None else "not_performed"
    )
    if report.status == STATUS_REVIEW_REQUIRED:
        return _render_review_pair(document, report, policy, out_abs, replace,
                                   authoritative_check)
    if report.status not in (STATUS_PASS_EXACT, STATUS_PASS_WITH_TRIM):
        return _refuse(
            f"计划未通过门禁（status={report.status}，"
            f"error_codes={list(report.error_codes)}），不产出任何交付物"
        )
    return _render_full_pack(document, report, policy, out_abs, replace, authoritative_check)


class _RefusingParser(argparse.ArgumentParser):
    """Usage errors exit 1 so they cannot be mistaken for REVIEW_REQUIRED (rc=2)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"ERROR: {message}", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _RefusingParser(
        description=(
            "Render the four episode deliverables atomically "
            "(exit 0 rendered / 1 refused / 2 review-required / 3 crash)"
        )
    )
    parser.add_argument("--plan", required=True, help="Path to episode-plan.json")
    parser.add_argument("--out", required=True, help="Output directory for the episode pack")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Swap an existing output AFTER the new result fully validates "
             "(gate + policy + every prompt audit); default refuses non-empty targets",
    )
    args = parser.parse_args(argv)
    try:
        return render_episode_pack(args.plan, args.out, replace=args.replace)
    except Exception as exc:  # noqa: BLE001 — fail closed: crash loudly, swap nothing
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CRASHED


if __name__ == "__main__":
    sys.exit(main())
