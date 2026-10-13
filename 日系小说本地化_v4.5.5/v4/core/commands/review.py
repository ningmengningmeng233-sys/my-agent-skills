from __future__ import annotations
import argparse
from typing import Any
from ..audit import append_audit, now_iso
from ..jsonio import read_json, write_json_atomic
from ..manifest import ManifestStore
from ..cli_support import (
    CLIError,
    _find_chapter,
    _withdraw_approval_pointer,
    _workspace_path,
    _write_chapter_blockers,
)

def cmd_approve(args: argparse.Namespace) -> int:
    """批准一个已检查通过的章节版本：核验绑定 → 两阶段提交状态 → 推进 approved。"""
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    chapter = _find_chapter(manifest, args.chapter)
    if chapter["state"] != "passed":
        raise CLIError(
            f"章节 {args.chapter} 当前状态为 {chapter['state']}，只有 passed 状态的章节可以被批准 (approve)"
        )
    # 章节工件目录（含 current.json / versions / state_deltas）
    chapter_dir = (workspace / chapter["paths"]["output"]).parent
    try:
        from ..version_store import approve_version

        record = approve_version(chapter_dir, args.version)
    except Exception as exc:  # noqa: BLE001
        raise CLIError(f"批准版本失败: {exc}") from exc

    # v4.5.0-dev.3 两阶段提交第二阶段：approve 是 continuity / SQLite 的唯一提交点。
    approved_version_id = f"v{int(record['approved_version']):03d}"
    try:
        from ..novel_state import commit_approval
        from ..state_delta import load_proposal

        proposal = load_proposal(workspace, args.chapter, approved_version_id)
        if proposal is None:
            raise CLIError(
                f"章节 {args.chapter} 缺少 {approved_version_id} 的状态提案"
                "（state_deltas/），请先运行 novel check 再批准"
            )
        commit_approval(
            workspace,
            chapter_id=args.chapter,
            version=approved_version_id,
            sha256=record.get("approved_sha256", ""),
            events=proposal["events"],
            snapshot=proposal["snapshot"],
        )
    except CLIError:
        # 提案缺失等前置失败同样不得留下批准指针（清单 §14 停止条件 #1）
        _withdraw_approval_pointer(chapter_dir, reason="前置校验失败")
        raise
    except Exception as exc:  # noqa: BLE001 - 状态提交失败必须阻断批准
        # 清单 §14 停止条件 #1：批准指针已在上一步写入 current.json，若状态提交失败
        # 而不撤回，就会出现「manifest 仍 passed 但已有批准指针」的半批准状态。
        _withdraw_approval_pointer(chapter_dir, reason=f"状态两阶段提交失败: {exc}")
        append_audit(
            workspace / "audit.jsonl",
            "approve_rejected_state_commit_failed",
            chapter_id=args.chapter,
            version=args.version,
            error=str(exc)[:200],
        )
        raise CLIError(f"状态两阶段提交失败，批准被拒绝: {exc}") from exc

    store.transition_chapter(args.chapter, "approved", reason="cli_approve")
    append_audit(
        workspace / "audit.jsonl",
        "chapter_version_approved",
        chapter_id=args.chapter,
        version=record.get("approved_version"),
        sha256=record.get("approved_sha256"),
    )
    print(f"{args.chapter}: 已批准 v{int(record['approved_version']):03d}")
    return 0


def cmd_blind_review(args: argparse.Namespace) -> int:
    """记录盲审结果：pass 进 passed，blocked/fail 进 rework。"""
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    chapter = _find_chapter(manifest, args.chapter)
    if chapter["state"] != "awaiting_review":
        raise CLIError(
            f"{args.chapter} 状态为 {chapter['state']}，不是 awaiting_review"
        )
    if not manifest.get("enhancements", {}).get("blind_review", {}).get("enabled"):
        raise CLIError("工作区未启用盲审（enhancements.blind_review.enabled=false）")
    result = args.result

    def record_result(updated: dict[str, Any]) -> None:
        current = next(c for c in updated["chapters"] if c["id"] == args.chapter)
        current["enhancements"]["blind_review"]["result"] = result
        current["updated_at"] = now_iso()

    store.update(record_result, event="blind_review_recorded")

    checks_path = workspace / chapter["paths"]["checks"]
    if checks_path.is_file():
        checks_doc = read_json(checks_path)
        checks_doc["blind_review_result"] = result
        write_json_atomic(checks_path, checks_doc)

    if result == "pass":
        store.transition_chapter(args.chapter, "passed", reason="blind_review_pass")
        _write_chapter_blockers(store, args.chapter, [])
    else:
        store.transition_chapter(args.chapter, "rework", reason="blind_review_rework")
        blockers = [
            b
            for b in chapter.get("blockers", [])
            if b.get("code") != "BLIND_REVIEW_REWORK"
        ] + [
            {
                "code": "BLIND_REVIEW_REWORK",
                "kind": "check",
                "message": f"盲审结果：{result}，需返工后重新 check",
                "opened_at": now_iso(),
            }
        ]
        _write_chapter_blockers(store, args.chapter, blockers)

    append_audit(
        workspace / "audit.jsonl",
        "blind_review_detail",
        chapter_id=args.chapter,
        result=result,
    )
    print(f"{args.chapter}: 盲审 {result} → {'passed' if result == 'pass' else 'rework'}")
    return 0


def cmd_platform_review(args: argparse.Namespace) -> int:
    """记录平台审核结果：pass 进 passed，blocked/fail 进 rework。"""
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    chapter = _find_chapter(manifest, args.chapter)
    if chapter["state"] != "awaiting_review":
        raise CLIError(
            f"{args.chapter} 状态为 {chapter['state']}，不是 awaiting_review"
        )
    platform_config = manifest.get("enhancements", {}).get("platform_review", {})
    if not platform_config.get("enabled"):
        raise CLIError("工作区未启用平台审核（enhancements.platform_review.enabled=false）")
    if args.platform not in platform_config.get("platforms", []):
        raise CLIError(
            f"平台 {args.platform} 不在已配置平台列表 "
            f"{platform_config.get('platforms', [])} 中"
        )
    result = args.result

    def record_result(updated: dict[str, Any]) -> None:
        current = next(c for c in updated["chapters"] if c["id"] == args.chapter)
        current["enhancements"]["platform_review"] = {
            "enabled": True,
            "platform": args.platform,
            "result": result,
        }
        current["updated_at"] = now_iso()

    store.update(record_result, event="platform_review_recorded")

    checks_path = workspace / chapter["paths"]["checks"]
    if checks_path.is_file():
        checks_doc = read_json(checks_path)
        checks_doc["platform_review"] = {
            "platform": args.platform,
            "result": result,
        }
        write_json_atomic(checks_path, checks_doc)

    if result == "pass":
        store.transition_chapter(args.chapter, "passed", reason="platform_review_pass")
        _write_chapter_blockers(store, args.chapter, [])
    else:
        store.transition_chapter(args.chapter, "rework", reason="platform_review_rework")
        blockers = [
            b
            for b in chapter.get("blockers", [])
            if b.get("code") != "PLATFORM_REVIEW_REWORK"
        ] + [
            {
                "code": "PLATFORM_REVIEW_REWORK",
                "kind": "check",
                "message": f"平台审核[{args.platform}]结果：{result}，需返工后重新 check",
                "opened_at": now_iso(),
            }
        ]
        _write_chapter_blockers(store, args.chapter, blockers)

    append_audit(
        workspace / "audit.jsonl",
        "platform_review_detail",
        chapter_id=args.chapter,
        platform=args.platform,
        result=result,
    )
    print(
        f"{args.chapter}: 平台审核[{args.platform}] {result} "
        f"→ {'passed' if result == 'pass' else 'rework'}"
    )
    return 0
