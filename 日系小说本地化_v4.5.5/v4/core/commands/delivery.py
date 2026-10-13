from __future__ import annotations
import argparse
from typing import Any
from .. import merge, publish
from ..audit import append_audit, now_iso
from ..jsonio import read_json, write_json_atomic
from ..manifest import ManifestStore
from ..cli_support import (
    CLIError,
    _find_chapter,
    _workspace_path,
)

def cmd_merge(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    report = merge.merge_output(workspace, allow_incomplete=args.allow_incomplete)
    if report["ok"]:
        print(
            f"合并完成: {workspace / 'output/localized.md'}\n"
            f"  章节: {', '.join(report['merged'])}\n"
            f"  manifest revision: {report['manifest_revision']}"
        )
        return 0

    print("合并失败:")
    for issue in report["issues"]:
        print(f"  {issue}")
    if report["excluded"]:
        print("排除章节:")
        for item in report["excluded"]:
            print(f"  {item['id']}: {item['reason']}")
    return 1


def cmd_publish_split(args: argparse.Namespace) -> int:
    """把 delivered/passed 章节拆成单章发布文件。"""
    workspace = _workspace_path(args.workspace)
    report = publish.split_publish(
        workspace,
        platform=args.platform,
        allow_incomplete=args.allow_incomplete,
    )
    if not report["ok"]:
        print("发布拆章失败:")
        for issue in report["issues"]:
            print(f"  {issue}")
        return 1
    print(f"发布目录: {report['out_dir']}")
    for item in report["files"]:
        print(f"  {item['filename']}  ({item['kind']})")
    if report["issues"]:
        print("跳过章节:")
        for issue in report["issues"]:
            print(f"  {issue}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    """回滚批准：清指针、保物理版本、章节退回 needs_review 并标记下游失效。"""
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    chapter = _find_chapter(manifest, args.chapter)
    if chapter["state"] not in {"approved", "delivered"}:
        raise CLIError(
            f"章节 {args.chapter} 状态为 {chapter['state']}，只有 approved/delivered 章节可回滚"
        )
    chapter_dir = (workspace / chapter["paths"]["output"]).parent
    try:
        from ..version_store import clear_approval

        clear_approval(chapter_dir)
    except Exception as exc:  # noqa: BLE001
        raise CLIError(f"清除批准指针失败: {exc}") from exc

    # §8.9-6：已批准/已交付的下游章节依赖被撤销的事实，必须标记为依赖失效并要求重检。
    rolled_ordinal = chapter.get("ordinal", 0)
    downstream = [
        item["id"]
        for item in manifest["chapters"]
        if item.get("ordinal", 0) > rolled_ordinal
        and item["state"] in {"approved", "delivered"}
    ]
    try:
        from ..novel_state import mark_chapter_rolled_back

        invalidated = mark_chapter_rolled_back(
            workspace, chapter_id=args.chapter, downstream_ids=downstream
        )
    except Exception as exc:  # noqa: BLE001
        raise CLIError(f"SQLite 回滚标记失败: {exc}") from exc

    store.transition_chapter(args.chapter, "needs_review", reason="cli_rollback")
    for cid in invalidated:
        store.transition_chapter(cid, "needs_review", reason="upstream_rolled_back")

    def mutate(updated: dict[str, Any]) -> None:
        current = next(
            item for item in updated["chapters"] if item["id"] == args.chapter
        )
        current["blockers"] = [
            b for b in current.get("blockers", []) if b.get("code") != "ROLLED_BACK"
        ] + [
            {
                "code": "ROLLED_BACK",
                "kind": "manual",
                "message": "批准已回滚（物理版本保留）；需重新 check → approve 后才能合并",
                "opened_at": now_iso(),
            }
        ]
        # 下游章节：注入依赖失效阻塞项（保留各自 blocker，避免覆盖其他原因）
        for item in updated["chapters"]:
            if item["id"] not in invalidated:
                continue
            item["blockers"] = [
                b for b in item.get("blockers", [])
                if b.get("code") != "UPSTREAM_ROLLED_BACK"
            ] + [
                {
                    "code": "UPSTREAM_ROLLED_BACK",
                    "kind": "fingerprint",
                    "message": (
                        f"上游章节 {args.chapter} 的批准已回滚，本章依赖的事实已撤销；"
                        "需重新 check → approve 后才能再次合并"
                    ),
                    "opened_at": now_iso(),
                }
            ]
            item["updated_at"] = now_iso()
        updated["output"]["invalidated_by_rollback"] = {
            "chapters": [args.chapter, *invalidated],
            "at": now_iso(),
        }
        current["updated_at"] = now_iso()

    store.update(mutate, event="chapter_rolled_back")

    # 清单 §14 停止条件 #3：回滚必须同步失效**派生检索缓存**，否则下游仍能从
    # continuity/state.json 读到被撤销事实（load_ledger 在文件存在时直接返回缓存，
    # 而 Prompt 层的 relevant_context 正是走这条路径）。这里从 SQLite 重建，
    # 而 rebuild_ledger_from_db 只采信 status='approved'，天然排除已撤销章节。
    ledger_rebuilt = False
    try:
        from ..continuity import ledger_path, rebuild_ledger_from_db

        write_json_atomic(
            ledger_path(workspace), rebuild_ledger_from_db(workspace)
        )
        ledger_rebuilt = True
    except Exception as exc:  # noqa: BLE001 - 缓存不同步会留下可检索的撤销事实，必须阻断
        raise CLIError(f"回滚后重建连续性检索缓存失败: {exc}") from exc

    append_audit(
        workspace / "audit.jsonl",
        "chapter_rolled_back",
        chapter_id=args.chapter,
        from_state=chapter["state"],
        downstream_invalidated=invalidated,
        ledger_rebuilt=ledger_rebuilt,
    )
    print(f"{args.chapter}: 已回滚批准 → needs_review（物理历史版本保留）")
    if invalidated:
        print(
            f"  下游依赖失效 {len(invalidated)} 章："
            f"{'、'.join(invalidated[:10])}"
            + ("…" if len(invalidated) > 10 else "")
            + "（已要求重检）"
        )
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    """列出章节不可变输出版本及批准指针。"""
    workspace = _workspace_path(args.workspace)
    manifest = ManifestStore(workspace).load()
    chapter = _find_chapter(manifest, args.chapter)
    version_dir = workspace / chapter["paths"]["output"]
    version_dir = version_dir.parent / "versions"
    current_path = version_dir.parent / "current.json"
    current = read_json(current_path) if current_path.is_file() else {}
    files = sorted(version_dir.glob("v*.md")) if version_dir.is_dir() else []
    for path in files:
        # 键存在但值为 null（只有草稿、从未批准，或已回滚）时 .get 的默认值不生效，
        # int(None) 会把这条只读命令崩掉并把章节拖进 error。
        approved = int(current.get("approved_version") or 0)
        marker = " approved" if path.stem == f"v{approved:03d}" else ""
        print(f"{path.stem}{marker}: {path}")
    if not files:
        print("尚无不可变版本")
    return 0
