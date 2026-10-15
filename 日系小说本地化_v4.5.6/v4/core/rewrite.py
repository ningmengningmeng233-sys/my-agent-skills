"""改写：只生成状态无载纯正文，不伪造 AI 生成（v4 spec §5.1 / §9 / §11.1）。

- 本模块只保证「正文产物契约」：``output.md`` 只有章节标题+正文，
  不含计划/质检/映射/状态/审批/审计元数据（spec §5.1 / R04）；
- 不伪造 AI 生成：默认提供确定性占位改写（按原文规范化复制，仅用于流水线集成与
  自测），真实本地化由注入的 ``generator``（AI 改写后端）完成；
- 工作流：``begin_rewrite``（ready_for_rewrite → rewriting）→
  ``rewrite_text``（生成纯正文）→ ``commit_output``（rewriting → awaiting_checks）。

改写输入必须同时满足：原文快照存在、事件图结构有效且无未确认高风险、
决策闭环、输入指纹与 manifest 记录一致（spec §9.1 / §12.3）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import checks, decisions, events, fingerprints
from .audit import append_audit, now_iso
from .hashing import sha256_file
from .jsonio import read_json, write_text_atomic
from .manifest import ManifestStore
from .paths import RULES_PATH
from .schemas import SchemaError
from .state_machine import require_transition


class RewriteError(ValueError):
    pass


@dataclass
class RewriteContext:
    """改写必需输入的有效视图（spec §9.1）。"""

    chapter_id: str
    title: str
    source_text: str
    events: dict[str, Any]
    project_decisions: dict[str, Any]
    chapter_decisions: dict[str, Any]
    rules_tiers: dict[str, list[str]]
    rules_version: str
    workspace: Path | None = None
    index_lib: Any | None = None
    writing_guide: Any | None = None
    knowledge_provider: Any | None = None


def _find_chapter(manifest: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    chapter = next(
        (item for item in manifest["chapters"] if item["id"] == chapter_id), None
    )
    if chapter is None:
        raise KeyError(f"章节不存在: {chapter_id}")
    return chapter


def load_rules_tiers() -> dict[str, list[str]]:
    from .checks import load_rules_tiers as _load

    return _load()


def resolve_context(
    workspace: Path, chapter_id: str, manifest: dict[str, Any] | None = None
) -> RewriteContext:
    workspace = Path(workspace)
    manifest = manifest or ManifestStore(workspace).load()
    chapter = _find_chapter(manifest, chapter_id)
    source_text = (workspace / chapter["paths"]["source"]).read_text(encoding="utf-8")
    events_doc = read_json(workspace / chapter["paths"]["events"])
    project_decisions = read_json(workspace / manifest["project_decisions"]["path"])
    chapter_decisions = read_json(workspace / chapter["paths"]["decisions"])
    return RewriteContext(
        chapter_id=chapter_id,
        title=chapter.get("title", ""),
        source_text=source_text,
        events=events_doc,
        project_decisions=project_decisions,
        chapter_decisions=chapter_decisions,
        rules_tiers=load_rules_tiers(),
        rules_version=manifest["rules"]["core_version"],
        workspace=workspace,
    )


def verify_rewrite_readiness(
    workspace: Path, chapter_id: str, manifest: dict[str, Any] | None = None
) -> list[str]:
    """改写前检查，返回问题列表（空 = 可改写）。"""
    workspace = Path(workspace)
    manifest = manifest or ManifestStore(workspace).load()
    chapter = _find_chapter(manifest, chapter_id)
    issues: list[str] = []
    if chapter["state"] != "ready_for_rewrite":
        issues.append(
            f"章节状态为 {chapter['state']}，仅 ready_for_rewrite 可开始改写"
        )
    for key in ("source", "events", "decisions"):
        path = workspace / chapter["paths"][key]
        if not path.is_file():
            issues.append(f"缺少必需文件 {chapter['paths'][key]}")
    if issues:
        return issues
    source_text = (workspace / chapter["paths"]["source"]).read_text(encoding="utf-8")
    if not source_text.strip():
        issues.append("章节原文为空")
    events_doc = read_json(workspace / chapter["paths"]["events"])
    try:
        events.validate_event_graph(events_doc)
    except SchemaError as exc:
        issues.append(f"事件图无效: {exc}")
        return issues
    if events.has_open_high_risk(events_doc):
        issues.append("存在未确认的高风险事件（decision_status=open）")
    project_decisions = read_json(workspace / manifest["project_decisions"]["path"])
    chapter_decisions = read_json(workspace / chapter["paths"]["decisions"])
    dec_result, dec_issues = decisions.closure_status(
        project_decisions, chapter_decisions, events_doc
    )
    if dec_result in {"blocked", "fail"}:
        issues.extend(f"决策未闭环: {item}" for item in dec_issues[:5])
    # 输入指纹核对：影响事实/表达的变化使旧正文失效（spec §12.3 / R16）
    recorded = chapter.get("fingerprints", {})
    current = fingerprints.chapter_fingerprints(
        source=workspace / chapter["paths"]["source"],
        events=workspace / chapter["paths"]["events"],
        project_decisions=workspace / manifest["project_decisions"]["path"],
        chapter_decisions=workspace / chapter["paths"]["decisions"],
        rules=RULES_PATH,
        output=workspace / chapter["paths"]["output"],
        checks=workspace / chapter["paths"]["checks"],
    )
    changed = fingerprints.changed_inputs(recorded, current, include_output=False)
    if changed:
        issues.append(f"输入指纹变化: {sorted(changed)}，需重新解析/决策后重跑")
    return issues


def begin_rewrite(
    workspace: Path, chapter_id: str, *, manifest: dict[str, Any] | None = None
) -> RewriteContext:
    """改写前置检查 + 状态迁移 ready_for_rewrite → rewriting，返回上下文。"""
    workspace = Path(workspace)
    manifest = manifest or ManifestStore(workspace).load()
    issues = verify_rewrite_readiness(workspace, chapter_id, manifest)
    if issues:
        raise RewriteError("改写前检查未通过: " + "; ".join(issues[:8]))
    store = ManifestStore(workspace)
    updated = store.transition_chapter(chapter_id, "rewriting", reason="begin_rewrite")
    return resolve_context(workspace, chapter_id, updated)


def deterministic_placeholder(context: RewriteContext) -> str:
    """确定性占位改写：原文规范化复制 + 已接受译名决策的确定性替换。

    明确不是 LLM 本地化改写，仅执行确定性规则：标题 + 规范化段落 +
    accepted 映射决策（``_decision_mappings``）替换，纯正文、无状态。
    真实改写必须注入 ``generator``。
    """
    paragraphs = checks.normalize_paragraphs(context.source_text)
    title = (context.title or "").strip()
    # v4.5.0-dev.3：占位改写同样必须执行已接受决策映射（确定性，无 LLM），
    # 否则锁定译名变更后占位流水线会残留旧译名并被 Terminology 门拦截。
    try:
        from .rewrite_backend import _apply_rule_replacement, _decision_mappings

        mappings = _decision_mappings(context)
    except Exception:  # noqa: BLE001 - 决策映射不可用时退化为纯规范化复制
        mappings = []
    body_paragraphs: list[str] = []
    for paragraph in paragraphs:
        if title and paragraph == title and not body_paragraphs:
            continue
        if mappings:
            paragraph = _apply_rule_replacement(paragraph, mappings)
        body_paragraphs.append(paragraph)
    body = "\n\n".join(body_paragraphs)
    if body:
        return f"{title}\n\n{body}" if title else body
    return title


def rewrite_text(
    context: RewriteContext, *, generator: Callable[[RewriteContext], str] | None = None
) -> str:
    """生成纯正文。

    未注入 ``generator`` 时使用确定性占位改写，绝不伪造 AI 生成；
    注入的生成器必须产出状态无载纯正文，否则抛 :class:`RewriteError`。
    """
    output = (
        generator(context)
        if generator is not None
        else deterministic_placeholder(context)
    )
    if not isinstance(output, str) or not output.strip():
        raise RewriteError("改写结果为空或非文本")
    issues = checks.purity_issues(output)
    if issues:
        raise RewriteError("改写结果含内部元数据: " + "; ".join(issues[:5]))
    return output


def commit_output(
    workspace: Path,
    chapter_id: str,
    output_text: str,
    *,
    manifest: dict[str, Any] | None = None,
    style_depth_required: bool = False,
) -> dict[str, Any]:
    """原子写 output.md，更新 output 指纹并迁移 rewriting → awaiting_checks。"""
    workspace = Path(workspace)
    store = ManifestStore(workspace)
    manifest = manifest or store.load()
    chapter = _find_chapter(manifest, chapter_id)
    # 先校验状态机迁移合法（rewriting → awaiting_checks），避免先落盘后失败
    require_transition(chapter["state"], "awaiting_checks")
    issues = checks.purity_issues(output_text)
    if issues:
        raise RewriteError("output.md 含内部元数据: " + "; ".join(issues[:5]))
    output_path = workspace / chapter["paths"]["output"]
    write_text_atomic(output_path, output_text)
    try:
        from .version_store import save_version

        save_version(output_path.parent, output_text, kind="draft")
    except Exception:  # noqa: BLE001 - 旧工作区兼容：版本目录失败不阻断原输出
        pass

    def mutate(updated: dict[str, Any]) -> None:
        current = next(item for item in updated["chapters"] if item["id"] == chapter_id)
        require_transition(current["state"], "awaiting_checks")
        current["state"] = "awaiting_checks"
        current["updated_at"] = now_iso()
        current.setdefault("enhancements", {})["style_depth_required"] = style_depth_required
        current["fingerprints"]["output"] = sha256_file(output_path)

    updated = store.update(mutate, event="output_committed")
    output_sha256 = next(
        item for item in updated["chapters"] if item["id"] == chapter_id
    )["fingerprints"]["output"]
    append_audit(
        store.audit_path,
        "output_committed_detail",
        chapter_id=chapter_id,
        output_sha256=output_sha256,
        manifest_revision=updated["manifest_revision"],
    )
    return updated
