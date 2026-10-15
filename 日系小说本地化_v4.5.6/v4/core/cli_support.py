from __future__ import annotations
import os
from pathlib import Path
from typing import Any
from . import checks, decisions, fingerprints, precheck, style_eval, rewrite
from .audit import append_audit, now_iso
from .jsonio import read_json, write_json_atomic
from .llm import LLMConfig, load_config
from .manifest import ManifestStore
from .paths import RULES_PATH
from .rewrite_backend import rule_then_llm_backend
from .schemas import SchemaError
from .state_machine import STATES, StateError

class CLIError(RuntimeError):
    """CLI 可恢复错误，退出码 1。"""


def _workspace_path(value: str) -> Path:
    path = Path(value)
    if not path.is_dir():
        raise CLIError(f"工作区不存在: {path}")
    if not (path / "manifest.json").is_file():
        raise CLIError(f"不是 v4 工作区（缺少 manifest.json）: {path}")
    return path


def _find_chapter(manifest: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    for chapter in manifest.get("chapters", []):
        if chapter["id"] == chapter_id:
            return chapter
    raise CLIError(f"章节不存在: {chapter_id}")


def _chapter_ids_to_process(
    manifest: dict[str, Any], chapter_id: str | None, allowed_states: set[str]
) -> list[str]:
    chapters = manifest.get("chapters", [])
    if chapter_id:
        _find_chapter(manifest, chapter_id)
        return [chapter_id]
    return [
        chapter["id"]
        for chapter in chapters
        if chapter["state"] in allowed_states
    ]


def _decision_paths(
    workspace: Path, manifest: dict[str, Any], chapter: dict[str, Any]
) -> dict[str, Path]:
    return {
        "project": workspace / manifest["project_decisions"]["path"],
        "chapter": workspace / chapter["paths"]["decisions"],
        "events": workspace / chapter["paths"]["events"],
        "source": workspace / chapter["paths"]["source"],
    }


def _collect_open_decisions(
    workspace: Path,
    manifest: dict[str, Any],
    chapter: dict[str, Any],
) -> list[dict[str, Any]]:
    """返回该章节所有开放的 A/C 档决策项（必须用户确认）。"""
    paths = _decision_paths(workspace, manifest, chapter)
    project_doc = read_json(paths["project"])
    chapter_doc = read_json(paths["chapter"])
    all_entries = list(project_doc.get("entries", [])) + list(
        chapter_doc.get("entries", [])
    )
    core_targets = {
        entry.get("target")
        for entry in project_doc.get("entries", [])
        if entry.get("category") == "mapping" and entry.get("status") == "accepted"
    }
    return [
        {
            "tier": decisions.classify_tier(entry, core_targets=core_targets),
            "decision_id": entry.get("decision_id"),
            "target": entry.get("target"),
            "choice": entry.get("choice"),
            "reason": entry.get("reason", ""),
            "category": entry.get("category"),
            "source": entry.get("source", {}),
        }
        for entry in all_entries
        if entry.get("status") == "open"
        and decisions.classify_tier(entry, core_targets=core_targets) in {"A", "C"}
    ]


def _write_chapter_blockers(
    store: ManifestStore,
    chapter_id: str,
    blockers: list[dict[str, Any]],
) -> None:
    """把阻塞项写回 manifest 对应章节，自动补齐 opened_at 字段。"""

    def mutate(updated: dict[str, Any]) -> None:
        current = next(
            item for item in updated["chapters"] if item["id"] == chapter_id
        )
        stamped = list(blockers)
        for blocker in stamped:
            blocker.setdefault("opened_at", now_iso())
        current["blockers"] = stamped
        current["updated_at"] = now_iso()

    store.update(mutate, event="blockers_updated")


def _print_blocker_guidance(
    workspace: Path,
    manifest: dict[str, Any],
    chapter: dict[str, Any],
) -> None:
    """打印可操作的阻塞项指引。"""
    paths = _decision_paths(workspace, manifest, chapter)
    print(f"    源稿: {paths['source']}")
    print(f"    事件图: {paths['events']}")
    print(f"    项目决策: {paths['project']}")
    print(f"    章节决策: {paths['chapter']}")

    open_items = _collect_open_decisions(workspace, manifest, chapter)
    if open_items:
        print("    待确认 A/C 档决策:")
        for item in open_items:
            print(
                f"      [{item['tier']}档] {item['decision_id']} "
                f"{item['category']} | {item['target']} → {item['choice']}"
            )
            if item["reason"]:
                print(f"        原因: {item['reason']}")

    blockers = chapter.get("blockers", [])
    if blockers:
        print("    当前阻塞项:")
        for blocker in blockers:
            code = blocker.get("code", "UNKNOWN")
            kind = blocker.get("kind", "blocker")
            message = blocker.get("message", "")
            print(f"      [{code}] ({kind}) {message}")
    elif not open_items:
        print("    无可操作阻塞项（状态未推进可能是其他原因）")


def _precheck_blocker_open(decisions_doc: dict[str, Any]) -> bool:
    """章节决策中是否还有 open 的预检自动决策。"""
    return any(
        isinstance(entry, dict)
        and entry.get("status") == "open"
        and str(entry.get("source", {}).get("value", "")).startswith("precheck:")
        for entry in decisions_doc.get("entries", [])
    )


def _mark_chapter_error(workspace: Path, chapter_id: str, exc: Exception) -> None:
    if isinstance(exc, (CLIError, rewrite.RewriteError, SchemaError, StateError)):
        return
    """写操作异常时把章节置为 error 并写入可操作 blocker。"""
    store = ManifestStore(workspace)
    try:
        manifest = store.load()
    except Exception:  # noqa: BLE001 - manifest 本身损坏时无法安全更新
        return
    chapter = next(
        (c for c in manifest.get("chapters", []) if c["id"] == chapter_id), None
    )
    if chapter is None or chapter["state"] in {"delivered", "error"}:
        return
    try:
        store.transition_chapter(
            chapter_id, "error", reason=f"runtime_exception: {type(exc).__name__}"
        )
        blockers = [
            b for b in chapter.get("blockers", []) if b.get("code") != "RUNTIME_EXCEPTION"
        ] + [
            {
                "code": "RUNTIME_EXCEPTION",
                "kind": "runtime",
                "message": f"{type(exc).__name__}: {exc}",
                "opened_at": now_iso(),
            }
        ]
        _write_chapter_blockers(store, chapter_id, blockers)
    except Exception:  # noqa: BLE001 - 错误状态写不进去不再扩大故障
        return


_PAUSE_SOURCES = STATES - {"new", "awaiting_boundary", "parsed", "delivered", "error"}


_RECOVERABLE_STATES = frozenset({"error", "rewriting"})


def _refresh_chapter_meta(
    store: ManifestStore,
    workspace: Path,
    chapter_id: str,
    *,
    decisions_doc: dict[str, Any] | None = None,
    blockers: list[dict[str, Any]] | None = None,
) -> None:
    """刷新 manifest 中的章节决策摘要、阻塞项与指纹。"""

    def mutate(updated: dict[str, Any]) -> None:
        chapter = next(
            item for item in updated["chapters"] if item["id"] == chapter_id
        )
        if decisions_doc is not None:
            chapter["decisions"] = decisions.decision_summary(decisions_doc)
        if blockers is not None:
            chapter["blockers"] = list(blockers)
        paths = _decision_paths(workspace, updated, chapter)
        chapter["fingerprints"] = fingerprints.chapter_fingerprints(
            source=paths["source"],
            events=paths["events"],
            project_decisions=paths["project"],
            chapter_decisions=paths["chapter"],
            rules=RULES_PATH,
            output=workspace / chapter["paths"]["output"],
            checks=workspace / chapter["paths"]["checks"],
        )
        chapter["updated_at"] = now_iso()

    store.update(mutate, event="chapter_meta_refreshed")


def _prepare_rewrite(
    workspace: Path,
    chapter_id: str,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将章节推进到 ready_for_rewrite（如已在该状态则跳过）。

    - awaiting_decision → ready_for_rewrite：要求决策闭环；
    - ready_for_rewrite → ready_for_rewrite：直接返回；
    - 其他状态报错。

    决策未闭环时会先把可操作阻塞项写回 manifest，再打印修复指引并抛错。
    """
    store = ManifestStore(workspace)
    manifest = manifest or store.load()
    chapter = _find_chapter(manifest, chapter_id)
    state = chapter["state"]

    if state == "ready_for_rewrite":
        return manifest

    if state == "rework":
        updated = store.transition_chapter(
            chapter_id, "ready_for_rewrite", reason="cli_prepare_rewrite_rework"
        )
        _write_chapter_blockers(store, chapter_id, [])
        return updated

    if state == "needs_review":
        # §8.9-5：回滚后的章节「必须重新 check 和 approve」，因此必须给出回到
        # 改写流水线的入口（needs_review → ready_for_rewrite）。人工确认后重跑改写。
        updated = store.transition_chapter(
            chapter_id, "ready_for_rewrite", reason="cli_prepare_rewrite_needs_review"
        )
        _write_chapter_blockers(store, chapter_id, [])
        append_audit(
            workspace / "audit.jsonl",
            "chapter_reentered_rewrite",
            chapter_id=chapter_id,
            from_state="needs_review",
        )
        return updated

    if state == "awaiting_decision":
        events_doc = read_json(workspace / chapter["paths"]["events"])
        project_decisions = read_json(
            workspace / manifest["project_decisions"]["path"]
        )
        chapter_decisions = read_json(workspace / chapter["paths"]["decisions"])
        status, issues = decisions.closure_status(
            project_decisions, chapter_decisions, events_doc
        )
        if status in {"blocked", "fail"}:
            blockers = decisions.derive_blockers(
                project_decisions, chapter_decisions, events_doc
            )
            _write_chapter_blockers(store, chapter_id, blockers)
            print(f"{chapter_id} 决策未闭环，无法进入改写。修复指引:")
            _print_blocker_guidance(
                workspace, ManifestStore(workspace).load(), chapter
            )
            raise CLIError(
                f"{chapter_id} 决策未闭环，无法进入改写:\n  "
                + "\n  ".join(issues[:5])
            )
        updated = store.transition_chapter(
            chapter_id, "ready_for_rewrite", reason="cli_prepare_rewrite"
        )
        _write_chapter_blockers(store, chapter_id, [])
        return updated

    raise CLIError(
        f"{chapter_id} 当前状态为 {state}，无法进入改写；"
        "需要先完成边界确认/决策"
    )


def _select_backend(
    name: str,
    config: LLMConfig | None,
    *,
    summary_sink: list[str] | None = None,
) -> rewrite.RewriteBackend:
    if name == "placeholder":
        return rewrite.deterministic_placeholder
    if name == "default":
        return rule_then_llm_backend(config, summary_sink=summary_sink)
    raise CLIError(f"未知后端: {name}")


def _load_exemplar_cache(
    workspace: Path, chapter_id: str, chapter: dict[str, Any]
) -> list[str]:
    """读取该章**实际注入过**的文风范例原文，供防抄门比对。

    优先按本章版本绑定的 ``prompt_run_id`` 精确读取；取不到则回退到该章最新
    一份缓存。缓存缺失（旧版本、离线运行、未接索引库）时返回空列表，
    防抄门据此判定为"不适用"，不阻断流程。
    """
    try:
        from .context_compiler import load_exemplar_cache
    except Exception:  # noqa: BLE001
        return []
    version = chapter.get("latest_version")
    run_id = None
    if version:
        try:
            version_doc = read_json(
                workspace / "chapters" / chapter_id / "versions" / f"v{int(version):03d}.json"
            )
            run_id = version_doc.get("prompt_run_id")
        except Exception:  # noqa: BLE001
            run_id = None
    return load_exemplar_cache(workspace, chapter_id, run_id)


def _load_continuity_snapshots(workspace: Path, *, limit: int = 10) -> list[dict[str, Any]]:
    """加载最近章节的连续性快照供长篇质量门使用（缓存不可用时空输入退化）。"""
    try:
        from .continuity import load_ledger

        return list(load_ledger(workspace).get("chapters", []))[-limit:]
    except Exception:  # noqa: BLE001 - 长篇门退化为空输入，不阻塞基础检查
        return []


_CN_DEFAULT_INPUT_LANGUAGES = frozenset({"zh", "zh-CN", "zh-TW", "zh-HK", "unknown"})


def _should_use_event_judge(manifest: dict[str, Any]) -> bool:
    """是否启用跨语言事件判官。

    只在源语言**显式声明为中文之外**时启用（如 ja / ja-JP）。``unknown``（init
    默认值）不启用，避免为既有工作区引入隐性 LLM 费用与行为变化。
    可用环境变量 ``NOVEL_EVENT_JUDGE`` 显式覆盖：``1`` 强制启用，``0`` 强制关闭。
    """
    override = os.environ.get("NOVEL_EVENT_JUDGE", "").strip()
    if override == "0":
        return False
    if override == "1":
        return True
    language = str(manifest.get("project", {}).get("input_language", "") or "").strip()
    if not language:
        return False
    return language not in _CN_DEFAULT_INPUT_LANGUAGES


def _build_event_judge(
    workspace: Path, manifest: dict[str, Any], chapter_id: str
) -> Any:
    """按需构造跨语言事件判官；未启用或构建失败时返回 None（退化为确定性判定）。"""
    if not _should_use_event_judge(manifest):
        return None
    try:
        from .event_judge import make_event_judge

        config = load_config()
        if not config.enabled or config.dry_run or not config.api_key:
            return None
        return make_event_judge(
            config, chapter_id=chapter_id, audit_path=workspace / "audit.jsonl"
        )
    except Exception:  # noqa: BLE001 - 判官不可用不得阻塞检查
        return None


def _build_coverage_judge(workspace: Path, manifest: dict[str, Any], chapter_id: str) -> Any:
    """按需构造逐句覆盖判官；未启用或构建失败时返回 None（由覆盖门按 fail-closed 处理）。"""
    if not _should_use_event_judge(manifest):
        return None
    try:
        from .event_judge import make_coverage_judge

        config = load_config()
        if not config.enabled or config.dry_run or not config.api_key:
            return None
        return make_coverage_judge(
            config,
            chapter_id=chapter_id,
            audit_path=workspace / "audit.jsonl",
            name_mappings=_name_mappings_for_chapter(workspace, manifest, chapter_id),
        )
    except Exception:  # noqa: BLE001 - 判官不可用不得阻塞检查
        return None


def _name_mappings_for_chapter(
    workspace: Path, manifest: dict[str, Any], chapter_id: str
) -> list[tuple[str, str]]:
    """汇总本章生效的「源语专名 → 目标语译名」对照，供覆盖判官理解译名。

    判官只看字面，会把「你怎么会在林德布尔姆？」对应到译文的「你怎么会在圣辉城？」
    判成 partial（源语写法未出现）。补上这层映射，判定才反映真实覆盖情况。
    """
    pairs: list[tuple[str, str]] = []
    try:
        from .checks import load_rules_tiers

        for token in load_rules_tiers().get("must_replace", []):
            if isinstance(token, dict):
                source = str(token.get("source", "")).strip()
                target = str(token.get("target", "")).strip()
                if source and target:
                    pairs.append((source, target))
    except Exception:  # noqa: BLE001 - 词典不可读时不阻塞检查
        pass
    try:
        chapter = _find_chapter(manifest, chapter_id)
        decisions = read_json(workspace / chapter["paths"]["decisions"])
        for entry in decisions.get("entries", []) or []:
            if not isinstance(entry, dict) or entry.get("status") != "accepted":
                continue
            source = str(entry.get("term_source", "")).strip()
            target = str(entry.get("term_target", "")).strip()
            if source and target:
                pairs.append((source, target))
    except Exception:  # noqa: BLE001
        pass
    # 去重但保序
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for pair in pairs:
        if pair not in seen:
            seen.add(pair)
            unique.append(pair)
    return unique


def _should_use_fabrication_audit(manifest: dict[str, Any]) -> bool:
    """是否启用「无中生有」忠实度审计。

    与事件判官同一启停策略：只在源语言**显式声明为中文之外**时启用，
    ``unknown`` 不启用，避免给既有工作区引入隐性 LLM 费用。
    环境变量 ``NOVEL_FABRICATION_AUDIT`` 可显式覆盖（``1`` 开 / ``0`` 关）。
    """
    override = os.environ.get("NOVEL_FABRICATION_AUDIT", "").strip()
    if override == "0":
        return False
    if override == "1":
        return True
    return _should_use_event_judge(manifest)


def _build_fabrication_auditor(
    workspace: Path, manifest: dict[str, Any], chapter_id: str
) -> Any:
    """按需构造忠实度审计器；未启用或构建失败时返回 None。"""
    if not _should_use_fabrication_audit(manifest):
        return None
    try:
        from .fabrication_audit import make_fabrication_auditor

        config = load_config()
        if not config.enabled or config.dry_run or not config.api_key:
            return None
        return make_fabrication_auditor(
            config, chapter_id=chapter_id, audit_path=workspace / "audit.jsonl"
        )
    except Exception:  # noqa: BLE001 - 审计不可用不得阻塞检查
        return None


def _run_check(
    workspace: Path,
    chapter_id: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    store = ManifestStore(workspace)
    chapter = _find_chapter(manifest, chapter_id)
    if chapter["state"] != "awaiting_checks":
        raise CLIError(f"{chapter_id} 状态为 {chapter['state']}，不是 awaiting_checks")

    # 审计绑定守卫：检查报告会用 manifest 里**记录**的输入指纹盖章，因此必须先确认
    # 记录指纹与实际输入一致。否则输入在 rewrite 之后被改动时，check 会「用新内容评估、
    # 却盖旧指纹的章」——报告声称的证据基础与实际不符，整套输入指纹审计前提失效。
    # 只校验报告真正绑定的 5 个输入键；output/checks 在流程中会自然变化，不在此列。
    stale = fingerprints.stale_input_chapters(manifest, workspace)
    input_changed = next(
        (keys for cid, keys in stale if cid == chapter_id), set()
    ) & {"source", "events", "project_decisions", "chapter_decisions", "rules"}
    if input_changed:
        raise CLIError(
            f"{chapter_id} 输入指纹已过期（{sorted(input_changed)}）："
            "检查报告必须绑定实际输入。请先运行 novel refresh"
            "（会把该章降级待重跑），再 rewrite → check"
        )

    source_text = (workspace / chapter["paths"]["source"]).read_text(encoding="utf-8")
    output_text = (workspace / chapter["paths"]["output"]).read_text(encoding="utf-8")
    events_doc = read_json(workspace / chapter["paths"]["events"])
    project_decisions = read_json(workspace / manifest["project_decisions"]["path"])
    chapter_decisions = read_json(workspace / chapter["paths"]["decisions"])

    recorded = chapter.get("fingerprints", {})
    input_hashes = {
        key: recorded[key]
        for key in ("source", "events", "project_decisions", "chapter_decisions", "rules")
    }

    writing_guide_provider = None
    writing_guide_obj = None
    try:
        from v4.enhance.writing_guide import WritingGuide, resolve_writing_guide, validate_writing_guide

        root = resolve_writing_guide(workspace=workspace)
        if root is not None:
            error = validate_writing_guide(root)
            if error is None:
                writing_guide_obj = WritingGuide(root, base=workspace)
                writing_guide_obj.scan()
                def provider(*, dimensions, output_text, source_text):
                    rows = []
                    for dimension in dimensions:
                        try:
                            rules = writing_guide_obj.module_rules(dimension)
                            loaded = len(rules.hard_rules)
                        except Exception:  # noqa: BLE001
                            loaded = 0
                        rows.append(
                            (
                                dimension,
                                "not_run",
                                f"写作指导已启用：加载 {loaded} 条硬规则（语义判定未自动执行，需人工复核）",
                            )
                        )
                    return rows

                writing_guide_provider = provider
    except Exception:  # noqa: BLE001
        writing_guide_provider = None

    checks_doc = checks.run_checks(
        chapter_id=chapter_id,
        source_text=source_text,
        output_text=output_text,
        events_graph=events_doc,
        chapter_title=chapter.get("title", ""),
        is_appendix=chapter.get("kind") == "appendix",
        style_depth_required=bool(
            chapter.get("enhancements", {}).get("style_depth_required", False)
        ),
        project_decisions=project_decisions,
        chapter_decisions=chapter_decisions,
        input_hashes=input_hashes,
        rules_tiers=checks.load_rules_tiers(),
        soft_evaluations=style_eval.evaluate(source_text, output_text),
        writing_guide_provider=writing_guide_provider,
        continuity_snapshots=_load_continuity_snapshots(workspace),
        judge=_build_event_judge(workspace, manifest, chapter_id),
        coverage_judge=_build_coverage_judge(workspace, manifest, chapter_id),
        # 跨语言正式交付必须有覆盖判官：没有判官时覆盖门按 blocked 处理（fail-closed），
        # 不允许「内容完整性无人验证」的章节进入交付。
        coverage_required=_should_use_event_judge(manifest),
        # 文风范例防抄：读改写时缓存的「实际注入范例原文」；无范例则为空，该门不适用。
        exemplars=_load_exemplar_cache(workspace, chapter_id, chapter),
    )

    # 「无中生有」独立审计（warning 级，不阻断）：事件对照只看「源事件是否被覆盖」，
    # 看不到事件之外的新增；实测有章节通过全部门禁却在译文里凭空写出情节。
    auditor = _build_fabrication_auditor(workspace, manifest, chapter_id)
    if auditor is not None:
        try:
            from .fabrication_audit import fabrication_finding

            items = auditor(source_text, output_text)
            if items:
                checks_doc.setdefault("long_novel_findings", []).append(
                    fabrication_finding(items)
                )
                append_audit(
                    workspace / "audit.jsonl",
                    "fabrication_audit",
                    chapter_id=chapter_id,
                    item_count=len(items),
                )
        except Exception:  # noqa: BLE001 - 审计失败绝不阻塞检查
            pass

    checks.record_checks(workspace, chapter_id, checks_doc)
    _refresh_chapter_meta(
        store,
        workspace,
        chapter_id,
        decisions_doc=chapter_decisions,
    )

    next_state = checks.next_state_after_checks(checks_doc, manifest)
    if next_state == "rework":
        blockers = checks.derive_blockers(checks_doc)
    else:
        blockers = []
        # 先清掉旧 blocker 再迁移到 passed，避免 schema 拒绝带阻塞项的通过状态。
        _write_chapter_blockers(store, chapter_id, blockers)
    store.transition_chapter(chapter_id, next_state, reason="cli_check")
    if next_state == "rework":
        _write_chapter_blockers(store, chapter_id, blockers)
    elif next_state == "passed":
        # v4.5.0-dev.3 两阶段提交第一阶段：只写 StateDeltaProposal，
        # 不直接写 continuity / SQLite；正式提交点在 novel approve。
        try:
            from .continuity import snapshot_from_context
            from .state_delta import propose_state_delta

            version_id = "v001"
            version_state = workspace / chapter["paths"]["output"]
            version_state = version_state.parent / "current.json"
            try:
                version_doc = read_json(version_state)
                version_id = f"v{int(version_doc.get('latest_version', 1)):03d}"
            except Exception:  # noqa: BLE001
                pass

            propose_state_delta(
                workspace,
                chapter_id,
                version=version_id,
                snapshot=snapshot_from_context(chapter_id, events_doc, output_text),
                events=events_doc,
            )
        except Exception as exc:  # noqa: BLE001 - 提案失败必须阻断通过（fail-closed）
            # 此刻章节已被推进到 passed。若不补偿，就会留下「passed 但无状态提案」的
            # 死路：approve 缺提案必拒、check 不能再跑（要求 awaiting_checks）、
            # rewrite/recover 也不接受 passed。因此退回 needs_review（passed→needs_review
            # 是合法迁移），保留已落盘的 output/versions/checks，并给出可操作指引。
            try:
                store.transition_chapter(
                    chapter_id, "needs_review", reason="proposal_write_failed"
                )
                _write_chapter_blockers(
                    store,
                    chapter_id,
                    [b for b in chapter.get("blockers", [])
                     if b.get("code") != "STATE_PROPOSAL_FAILED"] + [
                        {
                            "code": "STATE_PROPOSAL_FAILED",
                            "kind": "runtime",
                            "message": (
                                "状态提案写入失败；已退回 needs_review，避免留下无法批准的 "
                                "passed 状态。既有输出与检查报告保留，请重跑 rewrite → check "
                                "后再 approve"
                            ),
                            "opened_at": now_iso(),
                        }
                    ],
                )
            except Exception:  # noqa: BLE001 - 补偿失败不再扩大故障，但仍按失败上报
                pass
            raise CLIError(f"写入状态提案失败: {exc}") from exc
    return checks_doc


def _repair_first_paragraph(raw: str) -> str:
    """返修只允许返回单段替换正文：取首个非空段落，多余段落记入审计。"""
    from .rewrite_backend import _strip_code_fences

    cleaned = _strip_code_fences(raw or "")
    parts = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
    return parts[0] if parts else ""


def _withdraw_approval_pointer(chapter_dir: Path, *, reason: str) -> bool:
    """撤回刚写入的批准指针（清单 §14 停止条件 #1 的兜底）。

    撤回失败必须留下审计并返回 False —— 静默吞错会让「manifest=passed 但已批准」
    的半批准状态无人知晓。返回 True 表示指针已确认清除。
    """
    try:
        from .version_store import clear_approval

        clear_approval(chapter_dir)
        cleared = True
    except Exception:  # noqa: BLE001 - 撤回失败不再扩大故障，但必须留痕
        cleared = False
    try:
        current = read_json(chapter_dir / "current.json")
        still_set = bool(current.get("approved_version"))
    except Exception:  # noqa: BLE001
        still_set = True
    try:
        from .audit import append_audit

        append_audit(
            chapter_dir.parent.parent / "audit.jsonl",
            "approval_pointer_withdrawn" if cleared and not still_set
            else "approval_pointer_withdraw_failed",
            chapter_id=chapter_dir.name,
            reason=reason[:200],
        )
    except Exception:  # noqa: BLE001
        pass
    return cleared and not still_set


def _rerun_precheck_for_chapter(
    workspace: Path, chapter: dict[str, Any]
) -> None:
    """源稿/规则变化后重跑预检，并自动对账预检决策。"""
    source_path = workspace / chapter["paths"]["source"]
    source_text = source_path.read_text(encoding="utf-8")
    report = precheck.precheck_text(source_text, chapter["id"], title=chapter.get("title"))
    write_json_atomic(workspace / chapter["paths"]["precheck"], report.to_dict())

    decisions_path = workspace / chapter["paths"]["decisions"]
    decisions_doc = read_json(decisions_path)
    decisions_doc, changes = precheck.reconcile_auto_decisions(report, decisions_doc)
    write_json_atomic(decisions_path, decisions_doc)

    blockers = [
        b
        for b in chapter.get("blockers", [])
        if b.get("code") != "SOURCE_PRECHECK_PENDING"
    ]
    if report.blocked:
        blockers.append(
            {
                "code": "SOURCE_PRECHECK_PENDING",
                "kind": "contradiction",
                "message": "源稿预检发现确定或疑似问题，等待 decisions.json 闭环",
                "opened_at": now_iso(),
            }
        )
    _refresh_chapter_meta(
        ManifestStore(workspace),
        workspace,
        chapter["id"],
        decisions_doc=decisions_doc,
        blockers=blockers,
    )
    for change in changes[:5]:
        print(f"    {change}")
