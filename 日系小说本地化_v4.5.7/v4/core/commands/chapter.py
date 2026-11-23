from __future__ import annotations
import argparse
import json
import sys
from typing import Any
from .. import checks, consistency, decisions, events, rewrite
from ..audit import append_audit, now_iso
from ..hashing import sha256_file
from ..jsonio import read_json, write_json_atomic
from ..llm import load_config
from ..manifest import ManifestStore
from ..state_machine import require_transition
from ..cli_support import (
    CLIError,
    _CN_DEFAULT_INPUT_LANGUAGES,
    _chapter_ids_to_process,
    _find_chapter,
    _mark_chapter_error,
    _precheck_blocker_open,
    _prepare_rewrite,
    _print_blocker_guidance,
    _refresh_chapter_meta,
    _repair_first_paragraph,
    _run_check,
    _select_backend,
    _workspace_path,
    _write_chapter_blockers,
)

def _load_reference_asset(*, make, audit_log, asset_kind: str, root, validate_error):
    """按资产逐个加载增强件，返回 (对象或 None, 分类原因或 None)。

    这条函数存在的两个理由都来自真实误读：
    ① 旧兜底用一个 `except Exception` 罩住整段，写作指导读失败会连带把**已经可用**的
       索引库一起清空，用户被告知「索引库未找到」；
    ② 失败原因只留类别与异常类型名（见 `reference_audit.classify_load_failure`），
       异常原文里常夹着私密路径、凭据形状或正文片段，不进终端也不进审计。
    失败仍然中止：调用方拿不到对象就会按 `require_index_lib` 报错停下，不降级为空库。
    """
    from v4.enhance.reference_audit import (
        classify_load_failure,
        classify_validate_reason,
        record_enhance_unavailable,
    )

    if validate_error:
        record_enhance_unavailable(
            audit_log,
            chapter_id="*",
            asset_kind=asset_kind,
            asset_path=str(root),
            reason=validate_error,
        )
        return None, f"{classify_validate_reason(validate_error)}:{validate_error.split(':', 1)[0]}"
    try:
        asset = make()
        asset.scan()
        return asset, None
    except Exception as exc:  # noqa: BLE001 - 分类后交回调用方决定停不停
        reason = classify_load_failure(exc)
        record_enhance_unavailable(
            audit_log,
            chapter_id="*",
            asset_kind=asset_kind,
            asset_path=str(root),
            reason=reason,
        )
        return None, reason


def cmd_check(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()

    targets = _chapter_ids_to_process(
        manifest,
        args.chapter,
        {"awaiting_checks"},
    )
    if not targets:
        print("没有可检查的章节（状态需为 awaiting_checks）")
        return 0

    failures: list[str] = []
    for chapter_id in targets:
        try:
            checks_doc = _run_check(workspace, chapter_id, manifest)
            state = _find_chapter(ManifestStore(workspace).load(), chapter_id)["state"]
            print(
                f"  {chapter_id}: hard_gate={checks_doc['hard_gate']}, "
                f"event_comparison={checks_doc['event_comparison']} → {state}"
            )
            if checks_doc["warnings"]:
                for warning in checks_doc["warnings"]:
                    print(f"    warning [{warning['result']}]: {warning['message']}")
            if checks_doc["failures"]:
                for failure in checks_doc["failures"]:
                    print(f"    failure [{failure['result']}]: {failure['message']}")
            for soft in checks_doc.get("soft_evaluations", []):
                print(
                    f"    soft [{soft['dimension']}] {soft['result']}: "
                    f"{soft['evidence'][:120]}"
                )
            # 五项长篇质量门 findings：blocked 与 warning 同等呈现，
            # 保证操作者可读到 gate/severity/code/message/evidence 五要素
            # （warning 级不并入 failures，不得阻断；此处只做可见性输出）。
            for finding in checks_doc.get("long_novel_findings", []):
                print(
                    f"    gate [{finding['severity']}] "
                    f"{finding['gate']}/{finding['code']}: {finding['message']}"
                )
                if finding.get("evidence"):
                    print(f"      evidence: {finding['evidence']}")
            if state == "rework":
                print(f"  {chapter_id} 进入 rework，修复指引:")
                _print_blocker_guidance(
                    workspace, ManifestStore(workspace).load(), _find_chapter(ManifestStore(workspace).load(), chapter_id)
                )
        except Exception as exc:  # noqa: BLE001
            _mark_chapter_error(workspace, chapter_id, exc)
            failures.append(f"{chapter_id}: {exc}")

    if failures:
        print("检查失败:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    return 0


def cmd_rewrite(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()

    config = load_config() if args.backend != "placeholder" else None
    # 分段回退规则稿的结论必须显示出来：只写进审计等于没人看得见，
    # 而回退出来的正文读起来「正常」，最容易被当成一次成功改写交付。
    fallback_sink: list[str] = []
    backend = _select_backend(args.backend, config, summary_sink=fallback_sink)

    index_lib_obj = None
    writing_guide_obj = None
    index_reason = None
    audit_log = workspace / "audit.jsonl"
    try:
        from v4.enhance.index_lib import IndexLibrary, resolve_index_lib, validate_index_lib
        from v4.enhance.reference_audit import LOAD_KIND_MISSING
        from v4.enhance.writing_guide import WritingGuide, resolve_writing_guide, validate_writing_guide

        skip_index = bool(getattr(args, "no_index_lib", False))
        index_root = None if skip_index else resolve_index_lib(
            getattr(args, "index_lib", None), workspace=workspace
        )
        if index_root is not None:
            index_lib_obj, index_reason = _load_reference_asset(
                make=lambda: IndexLibrary(index_root, base=workspace),
                audit_log=audit_log,
                asset_kind="index_chapter",
                root=index_root,
                validate_error=validate_index_lib(index_root),
            )
        elif not skip_index:
            index_reason = f"{LOAD_KIND_MISSING}:既未配置也未登记"
        writing_root = resolve_writing_guide(
            getattr(args, "writing_guide", None), workspace=workspace
        )
        if writing_root is not None:
            writing_guide_obj, _ = _load_reference_asset(
                make=lambda: WritingGuide(writing_root, base=workspace),
                audit_log=audit_log,
                asset_kind="writing_module",
                root=writing_root,
                validate_error=validate_writing_guide(writing_root),
            )
    except Exception as exc:  # noqa: BLE001 - 增强模块自身不可用也要分类上报，不得静默
        from v4.enhance.reference_audit import classify_load_failure

        index_lib_obj = None
        writing_guide_obj = None
        index_reason = index_reason or classify_load_failure(exc)

    if (
        getattr(args, "require_index_lib", True)
        and not getattr(args, "no_index_lib", False)
        and index_lib_obj is None
    ):
        raise CLIError(
            f"rewrite 默认要求可用索引库（质量优先）：{index_reason or '缺失:原因未明'}。"
            "请用 --index-lib <路径> 指定，或把书放入 skill 的 索引库/ 目录；"
            "确认本次不使用请显式加 --no-index-lib。"
        )

    # 把参考资产接入情况写回 manifest（enabled 表示已加载并参与 prompt）
    def mark_reference_assets_loaded(updated):
        updated["enhancements"]["reference_assets"]["enabled"] = bool(
            index_lib_obj is not None or writing_guide_obj is not None
        )
        sources = []
        if index_lib_obj is not None:
            sources.append(f"index_lib={index_lib_obj.root}")
        if writing_guide_obj is not None:
            sources.append(f"writing_guide={writing_guide_obj.root}")
        updated["enhancements"]["reference_assets"]["description"] = (
            "；".join(sources) if sources else "未启用索引库参考资产"
        )

    store.update(mark_reference_assets_loaded, event="reference_assets_loaded")

    targets = _chapter_ids_to_process(
        manifest,
        args.chapter,
        {"ready_for_rewrite", "awaiting_decision", "rework", "needs_review"},
    )
    if not targets:
        print("没有可改写的章节（状态需为 ready_for_rewrite / awaiting_decision / rework / needs_review）")
        return 0

    failures: list[str] = []
    for chapter_id in targets:
        try:
            _prepare_rewrite(workspace, chapter_id)
            context = rewrite.begin_rewrite(workspace, chapter_id)
            context.index_lib = index_lib_obj
            context.writing_guide = writing_guide_obj
            if index_lib_obj is not None:
                try:
                    from v4.enhance.providers import IndexLibraryProvider

                    context.knowledge_provider = IndexLibraryProvider(index_lib_obj)
                except Exception:  # noqa: BLE001 - 统一协议包装失败时回退直连
                    context.knowledge_provider = None
            output_text = rewrite.rewrite_text(context, generator=backend)
            rewrite.commit_output(
                workspace,
                chapter_id,
                output_text,
                style_depth_required=args.backend == "default" and not config.dry_run,
            )
            try:
                from v4.core.rewrite_backend import _reference_block, index_surface_similarity_audit
                from v4.enhance.reference_audit import record_enhance_use

                _used_text, used_assets = _reference_block(context, context.source_text)
                for item in used_assets:
                    record_enhance_use(
                        workspace / "audit.jsonl",
                        **item,
                        context_rechecked=True,
                        borrowed_layer="structure",
                        copied_surface_text=False,
                    )
                index_surface_similarity_audit(context, output_text, used_assets)
                if used_assets:

                    def mark_chapter_reference_assets_used(updated):
                        current = next(
                            c for c in updated["chapters"] if c["id"] == chapter_id
                        )
                        current["enhancements"]["reference_assets_used"] = True
                        updated["enhancements"]["reference_assets"]["used"] = True

                    store.update(
                        mark_chapter_reference_assets_used,
                        event="reference_assets_used",
                    )
            except Exception as exc:  # noqa: BLE001 - 增强审计失败不影响改写结果
                print(
                    f"    警告: 参考资产审计/标记失败（不阻塞改写）: {exc}",
                    file=sys.stderr,
                )
            asset_info = []
            if index_lib_obj is not None:
                asset_info.append(f"index-lib={index_lib_obj.root}")
            if writing_guide_obj is not None:
                asset_info.append(f"writing-guide={writing_guide_obj.root}")
            asset_suffix = "；参考资产: " + "、".join(asset_info) if asset_info else ""
            print(f"  {chapter_id}: 改写完成 → awaiting_checks{asset_suffix}")
            for warning_line in fallback_sink:
                print(f"  ⚠️ {warning_line}", file=sys.stderr)
            fallback_sink.clear()
        except Exception as exc:  # noqa: BLE001
            _mark_chapter_error(workspace, chapter_id, exc)
            failures.append(f"{chapter_id}: {exc}")

    if failures:
        print("改写失败:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    """一键确认预检/章节决策：默认按决策项记录的最优方案执行。

    - 默认 --confirm：接受该决策项的 choice（矛盾默认保留原文，强元文本默认剥离）；
    - --reject：拒绝该决策，需给出原因；
    - 决策关闭后自动刷新 blockers/fingerprints；若章节其他决策与事件也闭环，
      awaiting_decision 自动推进到 ready_for_rewrite。
    """
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    chapter = _find_chapter(manifest, args.chapter)
    if chapter["state"] not in {"parsed", "awaiting_decision", "awaiting_boundary"}:
        raise CLIError(
            f"{args.chapter} 当前状态为 {chapter['state']}，"
            "不是可决策状态（应为 parsed / awaiting_decision）"
        )

    decisions_path = workspace / chapter["paths"]["decisions"]
    decisions_doc = read_json(decisions_path)
    status = "rejected" if args.reject else "accepted"
    reason = args.reason or (
        "拒绝该预检处理方案" if args.reject else "用户一键确认，采用默认处理方案"
    )
    decisions.set_entry_status(
        decisions_doc,
        args.decision_id,
        status,
        source_kind="user",
        source_value="cli_decide",
        reason=reason,
    )
    decisions.record_decision(
        decisions_path,
        decisions_doc,
        event_type="decision_confirmed" if status == "accepted" else "decision_rejected",
        workspace=workspace,
        chapter_id=args.chapter,
        decision_id=args.decision_id,
    )

    events_doc = read_json(workspace / chapter["paths"]["events"])
    project_decisions = read_json(workspace / manifest["project_decisions"]["path"])
    closure, issues = decisions.closure_status(
        project_decisions, decisions_doc, events_doc
    )
    blockers = [
        b
        for b in chapter.get("blockers", [])
        if b.get("code") != "SOURCE_PRECHECK_PENDING"
    ]
    if _precheck_blocker_open(decisions_doc):
        blockers.append(
            {
                "code": "SOURCE_PRECHECK_PENDING",
                "kind": "contradiction",
                "message": "源稿预检仍存在未确认决策，等待 decide 确认",
                "opened_at": now_iso(),
            }
        )
    _refresh_chapter_meta(
        store, workspace, args.chapter, decisions_doc=decisions_doc, blockers=blockers
    )

    if closure == "pass" and chapter["state"] in {"parsed", "awaiting_decision"}:
        store.transition_chapter(
            args.chapter, "ready_for_rewrite", reason="cli_decide_closed"
        )
        print(
            f"{args.chapter}: 决策 {args.decision_id} 已关闭，"
            f"章节已进入 ready_for_rewrite"
        )
    else:
        print(
            f"{args.chapter}: 决策 {args.decision_id} 已关闭；"
            f"闭环状态={closure}"
        )
        if closure != "pass":
            print("剩余阻塞:")
            for issue in issues[:5]:
                print(f"  {issue}")
            print("最优下一步: 运行 `blockers` 查看完整指引")
    return 0


def cmd_confirm_boundary(args: argparse.Namespace) -> int:
    """将 awaiting_boundary 的章节推进到 parsed，并清除边界阻塞项。"""
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    targets = _chapter_ids_to_process(
        manifest, args.chapter, {"awaiting_boundary"}
    )
    if not targets:
        print("没有待确认边界的章节")
        return 0

    for chapter_id in targets:
        store.transition_chapter(
            chapter_id, "parsed", reason="cli_confirm_boundary"
        )
        fresh = ManifestStore(workspace).load()
        chapter = _find_chapter(fresh, chapter_id)
        blockers = [
            blocker
            for blocker in chapter.get("blockers", [])
            if blocker.get("code") != "BOUNDARY_PENDING"
        ]
        _write_chapter_blockers(store, chapter_id, blockers)
        print(f"  {chapter_id}: awaiting_boundary → parsed")

    final = ManifestStore(workspace).load()
    if not any(
        chapter["state"] == "awaiting_boundary"
        for chapter in final.get("chapters", [])
    ):

        def confirm_source_boundary(updated: dict[str, Any]) -> None:
            updated["source"]["boundary_status"] = "confirmed"

        store.update(confirm_source_boundary, event="source_boundary_confirmed")
        print("全部章节边界已确认")
    return 0


def cmd_consistency(args: argparse.Namespace) -> int:
    """全书级一致性旁车检查：只读章节与输出，写 consistency.json 和审计。"""
    workspace = _workspace_path(args.workspace)
    report = consistency.check_book_consistency(workspace)
    write_json_atomic(workspace / "consistency.json", report)
    append_audit(
        workspace / "audit.jsonl",
        "book_consistency_checked",
        error_count=report["summary"]["error_count"],
        warning_count=report["summary"]["warning_count"],
    )
    print(
        f"全书一致性: {report['summary']['chapter_count']} 章, "
        f"error={report['summary']['error_count']}, "
        f"warning={report['summary']['warning_count']}"
    )
    for issue in report["issues"]:
        prefix = "ERROR" if issue["severity"] == "error" else "WARN"
        chapter = f" [{issue['chapter_id']}]" if issue["chapter_id"] else ""
        print(f"  {prefix}{chapter} [{issue['code']}] {issue['message']}")
    return 1 if report["summary"]["error_count"] else 0


def cmd_align_events(args: argparse.Namespace) -> int:
    """按 output.md 重新对齐事件图；必须显式 --confirm 才能重定基准。

    重定基准会把 output.md 当作事件图来源，因此属于 A 档确认动作，
    必须写审计，不能静默执行。
    """
    workspace = _workspace_path(args.workspace)
    if not getattr(args, "confirm", False):
        print(
            "align-events 会用 output.md 重新生成事件图基准；"
            "如果正文改动过剧情，这一步可能掩盖剧情变化。\n"
            "何时需要：仅当 event_comparison 因**源语言与正文语言不同**而无法定位"
            "（如日语源 → 中文正文）时的兜底手段；\n"
            "  通常跨语言源稿已由判官通道自动处理（需 NOVEL_LLM_ENABLED 且语言非中文），"
            "无需手工执行本命令。\n"
            "请先人工核对 output.md 与已确认事件图，确认无误后加 --confirm 执行。"
        )
        return 1
    store = ManifestStore(workspace)
    manifest = store.load()
    targets = _chapter_ids_to_process(
        manifest, args.chapter, {"awaiting_checks"}
    )
    if not targets:
        print("没有可对齐事件的章节（状态需为 awaiting_checks）")
        return 0

    language = str(manifest.get("project", {}).get("input_language", "") or "")
    if language not in _CN_DEFAULT_INPUT_LANGUAGES:
        print(
            f"注意：本工作区源语言为 {language}（非中文）。跨语言事件对照已由判官通道"
            "处理；只有判官不可用或你已人工确认正文无剧情改动时，才需要本命令重定基准。"
        )

    for chapter_id in targets:
        chapter = _find_chapter(manifest, chapter_id)
        output_path = workspace / chapter["paths"]["output"]
        output_text = output_path.read_text(encoding="utf-8")
        source_path = workspace / chapter["paths"]["source"]
        source_sha = sha256_file(source_path)
        events_path = workspace / chapter["paths"]["events"]
        events_doc = read_json(events_path)

        aligned = events.align_event_graph_to_output(
            events_doc, output_text, source_sha
        )
        write_json_atomic(events_path, aligned)
        _refresh_chapter_meta(
            store,
            workspace,
            chapter_id,
            blockers=[],
        )
        append_audit(
            workspace / "audit.jsonl",
            "events_rebaselined",
            chapter_id=chapter_id,
            event_count=len(aligned["events"]),
            source_sha256=source_sha,
            reason="user_confirmed_align_events",
        )
        print(f"  {chapter_id}: 已按 output 对齐 {len(aligned['events'])} 个事件")
    return 0


def cmd_resolve_events(args: argparse.Namespace) -> int:
    """高风险事件处理：--list / --event-id / --approve-all 三种模式。

    - --list：列出待确认事件及风险原因；
    - --event-id：一次确认/拒绝一个事件（需 --chapter）；
    - --approve-all：显式批量批准。
    """
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    targets = _chapter_ids_to_process(
        manifest,
        args.chapter,
        {"parsed", "awaiting_decision", "awaiting_checks", "rework"},
    )
    if not targets:
        print("没有可解析事件的高风险章节")
        return 0

    if args.list:
        for chapter_id in targets:
            chapter = _find_chapter(manifest, chapter_id)
            events_doc = read_json(workspace / chapter["paths"]["events"])
            open_events = events.open_high_risk_events(events_doc)
            if not open_events:
                print(f"{chapter_id}: 无待确认高风险事件")
                continue
            for event in open_events:
                risk = event.get("risk", {})
                print(
                    f"  {chapter_id}/{event['event_id']} 风险={', '.join(risk.get('reasons', [])) or '未知'} "
                    f"动作={event.get('action', '')[:60]}"
                )
        return 0

    if args.event_id:
        if not args.chapter:
            raise CLIError("--event-id 必须与 --chapter 一起使用")
        chapter_id = args.chapter
        chapter = _find_chapter(manifest, chapter_id)
        events_path = workspace / chapter["paths"]["events"]
        events_doc = read_json(events_path)
        event = next(
            (
                item
                for item in events.open_high_risk_events(events_doc)
                if item.get("event_id") == args.event_id
            ),
            None,
        )
        if event is None:
            print(f"{chapter_id}: 没有待确认事件 {args.event_id}")
            return 1
        risk = event.setdefault("risk", {})
        decision_id = risk.get("decision_id") or f"{chapter_id}-{event['event_id']}"
        risk["decision_id"] = decision_id
        approved = not args.reject
        risk["decision_status"] = "approved" if approved else "rejected"
        decisions_path = workspace / chapter["paths"]["decisions"]
        decisions_doc = read_json(decisions_path)
        existing = next(
            (
                item
                for item in decisions_doc.get("entries", [])
                if item.get("decision_id") == decision_id
            ),
            None,
        )
        if existing is not None:
            existing["status"] = "accepted" if approved else "rejected"
        else:
            decisions.add_entry(
                decisions_doc,
                decisions.new_entry(
                    category="review",
                    target=f"事件 {event['event_id']} 高风险确认",
                    choice="approved" if approved else "rejected",
                    source_kind="system",
                    source_value="cli_resolve_events",
                    decision_id=decision_id,
                    status="accepted" if approved else "rejected",
                    reason=(
                        f"CLI 逐事件{'approve' if approved else 'reject'}（风险："
                        f"{', '.join(risk.get('reasons', []))}）"
                    ),
                ),
            )
        write_json_atomic(events_path, events_doc)
        write_json_atomic(decisions_path, decisions_doc)
        blockers = [
            b
            for b in chapter.get("blockers", [])
            if b.get("code") != "EVENT_GRAPH_PENDING"
        ]
        if events.has_open_high_risk(events_doc):
            blockers.append(
                {
                    "code": "EVENT_GRAPH_PENDING",
                    "kind": "event",
                    "message": "事件图仍有高风险节点未确认",
                    "opened_at": now_iso(),
                }
            )
        if chapter["state"] == "parsed":
            store.transition_chapter(
                chapter_id, "awaiting_decision", reason="cli_resolve_events_single"
            )
        _refresh_chapter_meta(
            store,
            workspace,
            chapter_id,
            decisions_doc=decisions_doc,
            blockers=blockers,
        )
        print(
            f"{chapter_id}/{event['event_id']}: {'已批准' if approved else '已拒绝'}"
            f" 高风险事件"
        )
        return 0

    if not args.approve_all:
        print(
            "请选择操作：--list 查看待确认事件；"
            "--event-id <id> --chapter <id> [--reject] 逐条处理；"
            "或 --approve-all 显式批量批准。"
        )
        return 1

    for chapter_id in targets:
        chapter = _find_chapter(manifest, chapter_id)
        events_path = workspace / chapter["paths"]["events"]
        events_doc = read_json(events_path)
        decisions_path = workspace / chapter["paths"]["decisions"]
        decisions_doc = read_json(decisions_path)

        resolved = 0
        for event in events.open_high_risk_events(events_doc):
            risk = event.setdefault("risk", {})
            decision_id = risk.get("decision_id") or f"{chapter_id}-{event['event_id']}"
            risk["decision_id"] = decision_id
            risk["decision_status"] = "approved"
            print(
                f"    {chapter_id}/{event['event_id']}: 批准高风险事件（风险: "
                f"{', '.join(risk.get('reasons', [])) or '未知'}）"
            )
            existing = next(
                (
                    item
                    for item in decisions_doc.get("entries", [])
                    if item.get("decision_id") == decision_id
                ),
                None,
            )
            if existing is not None:
                existing["status"] = "accepted"
            else:
                decisions.add_entry(
                    decisions_doc,
                    decisions.new_entry(
                        category="review",
                        target=f"事件 {event['event_id']} 高风险确认",
                        choice="approved",
                        source_kind="system",
                        source_value="cli_resolve_events",
                        decision_id=decision_id,
                        status="accepted",
                        reason=(
                            "CLI 批量确认高风险事件（原因："
                            f"{', '.join(risk.get('reasons', []))}）"
                        ),
                    ),
                )
            resolved += 1

        if resolved:
            write_json_atomic(events_path, events_doc)
            write_json_atomic(decisions_path, decisions_doc)

            fresh = ManifestStore(workspace).load()
            chapter = _find_chapter(fresh, chapter_id)
            blockers = [
                blocker
                for blocker in chapter.get("blockers", [])
                if blocker.get("code") != "EVENT_GRAPH_PENDING"
            ]
            if chapter["state"] == "parsed":
                store.transition_chapter(
                    chapter_id, "awaiting_decision", reason="cli_resolve_events"
                )
            _refresh_chapter_meta(
                store,
                workspace,
                chapter_id,
                decisions_doc=decisions_doc,
                blockers=blockers,
            )
            print(
                f"  {chapter_id}: 已确认 {resolved} 个高风险事件"
            )
        else:
            print(f"  {chapter_id}: 无待确认高风险事件")
    return 0


def cmd_repair_coverage(args: argparse.Namespace) -> int:
    """定向返修：只修覆盖门判定为缺失/部分/不确定的段落窗口（Task 9.9.5）。

    与整章 rewrite 的区别：只发送问题段落窗口与必须补回的源句语义，产出新版本后
    必须重新跑 `novel check`；每段最多 2 次（工单硬约束），超限保持 rework 并停止调用。
    """
    import hashlib

    from ..coverage_repair import (
        apply_repair,
        build_repair_messages,
        plan_repairs,
        update_ledger,
    )
    from ..llm import call_llm, load_config

    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    chapter = _find_chapter(manifest, args.chapter)
    chapter_id = chapter["id"]
    chapter_dir = (workspace / chapter["paths"]["output"]).parent

    versions = sorted((chapter_dir / "versions").glob("v*.md"))
    if not versions:
        raise CLIError(f"{chapter_id} 没有任何版本，无法定向返修")
    latest_version = versions[-1].stem
    checks_path = chapter_dir / "checks" / f"{latest_version}.json"
    checks_doc = read_json(checks_path) if checks_path.is_file() else {}
    blocking = checks_doc.get("content_coverage_blocking") or []
    if not blocking:
        print(f"{chapter_id}（{latest_version}）：无覆盖阻断项，无需定向返修")
        return 0

    text = versions[-1].read_text(encoding="utf-8")
    pids = checks.paragraph_ids(text)
    ledger_path = chapter_dir / "coverage_repairs.json"
    # read_json 对缺失文件会抛错（不是宽容读取），账本首次运行时本来就不存在
    ledger = read_json(ledger_path) if ledger_path.is_file() else {}
    tasks, skipped = plan_repairs(
        blocking, {pid: para for pid, para in pids}, ledger=ledger, source_version=latest_version
    )
    for item in skipped:
        print(f"  跳过：{json.dumps(item, ensure_ascii=False)}")
    if not tasks:
        print(f"{chapter_id}：无可用返修任务（无定位证据或已达每段上限）：保持 rework")
        return 0

    config = load_config()
    if not config.enabled or config.dry_run or not config.api_key:
        raise CLIError("定向返修需要可用的 LLM 配置（NOVEL_LLM_ENABLED=1 且提供 API Key）")

    # 与 rewrite 一致：先把章节归一化到 ready_for_rewrite（rework/needs_review 都走这条路，
    # 含决策闭环检查），再进 rewriting；不这样做时 needs_review 章节会因非法迁移而失败。
    prepared = _prepare_rewrite(workspace, chapter_id, store.load())
    prepared_state = next(
        item for item in prepared["chapters"] if item["id"] == chapter_id
    )["state"]
    require_transition(prepared_state, "rewriting")

    def mark_rewriting(updated: dict[str, Any]) -> None:
        current = next(item for item in updated["chapters"] if item["id"] == chapter_id)
        require_transition(current["state"], "rewriting")
        current["state"] = "rewriting"
        current["updated_at"] = now_iso()

    store.update(mark_rewriting, event="coverage_repair_started")

    repaired = text
    done = []
    records: list[dict[str, Any]] = []
    for task in tasks:
        messages = build_repair_messages(task)
        prompt_sha = hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        try:
            raw = call_llm(
                messages,
                config=config,
                audit_path=workspace / "audit.jsonl",
                chapter_id=chapter_id,
            )
        except Exception as exc:  # noqa: BLE001 - 单段返修失败不影响其他段
            append_audit(
                workspace / "audit.jsonl",
                "coverage_repair_failed",
                chapter_id=chapter_id,
                paragraph_id=task.paragraph_id,
                reason=str(exc)[:200],
            )
            continue
        replacement = _repair_first_paragraph(raw)
        record = {
            "paragraph_id": task.paragraph_id,
            "requirement_ids": list(task.requirement_ids),
            "attempt": task.attempt,
            "prompt_sha256": prompt_sha,
            "raw_sha256": hashlib.sha256((raw or "").encode("utf-8")).hexdigest(),
            "changed": bool(replacement) and replacement != task.current_text,
        }
        if record["changed"]:
            repaired = apply_repair(
                repaired, task.paragraph_id, replacement, pid_list=checks.paragraph_ids(repaired)
            )
            done.append(task)
        records.append(record)

    if not done:
        # 没有产生实质变化：不要卡在 rewriting（该状态是「有未检查的新版本」）
        def back_to_rework(updated: dict[str, Any]) -> None:
            current = next(item for item in updated["chapters"] if item["id"] == chapter_id)
            require_transition(current["state"], "needs_review")
            current["state"] = "needs_review"
            current["updated_at"] = now_iso()

        store.update(back_to_rework, event="coverage_repair_noop")
        append_audit(
            workspace / "audit.jsonl",
            "coverage_repair_noop",
            chapter_id=chapter_id,
            attempted=[record["paragraph_id"] for record in records],
        )
        print(f"{chapter_id}：定向返修未产生变化，保持待返修（状态 → needs_review）")
        return 0

    # 与改写路径一致的后处理：用词归一 + 硬锁定重扫（否则返修会把词典违禁词写回新版本，
    # 被残留门判 fail 后该章永久无法通过——真实批次 ch-0005 v002/v004 即此因）。
    from ..coverage_repair import finalize_repaired_text
    from ..rewrite import commit_output, resolve_context

    repaired = finalize_repaired_text(repaired, resolve_context(workspace, chapter_id))
    purity = checks.purity_issues(repaired)
    if purity:
        raise CLIError("返修结果含内部元数据，拒绝提交: " + "; ".join(purity[:3]))

    commit_output(workspace, chapter_id, repaired, manifest=store.load())
    # commit_output 返回的是更新后的 manifest，版本号以 current.json 为准（save_version 写的）
    current_path = chapter_dir / "current.json"
    current_doc = read_json(current_path) if current_path.is_file() else {}
    latest_number = current_doc.get("latest_version")
    new_version = f"v{int(latest_number):03d}" if latest_number else ""
    write_json_atomic(ledger_path, update_ledger(ledger, done, source_version=latest_version))
    repairs_dir = chapter_dir / "coverage_repairs"
    repairs_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        repairs_dir / f"{new_version or latest_version}.json",
        {
            "chapter_id": chapter_id,
            "source_version": latest_version,
            "result_version": new_version,
            "coverage_requirements": sorted(
                rid for task in done for rid in task.requirement_ids
            ),
            "attempts_used": update_ledger(ledger, done, source_version=latest_version),
            "records": records,
        },
    )
    append_audit(
        workspace / "audit.jsonl",
        "coverage_repair",
        chapter_id=chapter_id,
        source_version=latest_version,
        result_version=new_version,
        repaired_paragraphs=[task.paragraph_id for task in done],
        requirement_ids=sorted(rid for task in done for rid in task.requirement_ids),
    )
    print(
        f"{chapter_id}：定向返修完成 → {new_version or '（未生成新版本）'}"
        f"，修补段落 {[task.paragraph_id for task in done]}"
    )
    print("  请重新运行 novel check（覆盖项必须全部 present 才能通过）")
    return 0
