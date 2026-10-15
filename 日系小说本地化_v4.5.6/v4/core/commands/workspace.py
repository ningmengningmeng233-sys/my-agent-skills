from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any
from .. import card_factory, card_index, library_registry, fingerprints, ingest
from ..audit import append_audit, now_iso
from ..manifest import ManifestStore
from ..workspace import create_workspace
from ..state_machine import StateError, require_transition
from ..cli_support import (
    CLIError,
    _PAUSE_SOURCES,
    _RECOVERABLE_STATES,
    _collect_open_decisions,
    _find_chapter,
    _print_blocker_guidance,
    _rerun_precheck_for_chapter,
    _workspace_path,
)

def cmd_init(args: argparse.Namespace) -> int:
    create_workspace(
        args.workspace,
        title=args.title,
        author=args.author,
        input_language=args.language,
    )
    print(f"v4 工作区已创建: {args.workspace}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    source = Path(args.source)
    if not source.exists():
        raise CLIError(f"源稿不存在: {source}")
    if source.is_dir() and not any(source.iterdir()):
        raise CLIError(f"源稿目录为空: {source}")
    if not source.is_file() and not source.is_dir():
        raise CLIError(f"源稿必须是文件或目录: {source}")
    manifest = ingest.register_source(
        workspace,
        source,
        max_chapter_chars=args.max_chapter_chars,
        event_engine=args.event_engine,
    )
    print(
        f"已接入源稿: {source}\n"
        f"  章节数: {len(manifest['chapters'])}\n"
        f"  边界状态: {manifest['source']['boundary_status']}"
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()

    print(f"项目: {manifest['project']['title']}")
    print(f"manifest revision: {manifest['manifest_revision']}")
    print(f"source boundary: {manifest['source']['boundary_status']}")

    chapters = manifest.get("chapters", [])
    if not chapters:
        print("  无章节")
        return 0

    if args.chapter:
        chapter = _find_chapter(manifest, args.chapter)
        chapters = [chapter]

    for chapter in chapters:
        state = chapter["state"]
        checks_summary = chapter.get("checks", {})
        blockers = chapter.get("blockers", [])
        print(
            f"  {chapter['id']} (ordinal={chapter['ordinal']}): {state}"
        )
        if chapter.get("title"):
            print(f"    标题: {chapter['title']}")
        if checks_summary:
            print(
                f"    checks: hard_gate={checks_summary.get('hard_gate')}, "
                f"event_comparison={checks_summary.get('event_comparison')}"
            )
        for blocker in blockers:
            print(f"    blocker: [{blocker.get('code')}] {blocker.get('message')}")
        if blockers or args.verbose:
            _print_blocker_guidance(workspace, manifest, chapter)
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    """暂停章节：记录暂停前状态与原因，其他章节不受影响。"""
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    chapter = _find_chapter(manifest, args.chapter)
    if chapter["state"] not in _PAUSE_SOURCES:
        raise CLIError(
            f"{args.chapter} 状态为 {chapter['state']}，不能暂停"
            "（仅未完成章节可暂停）"
        )
    previous = chapter["state"]

    def mutate(updated: dict[str, Any]) -> None:
        current = next(c for c in updated["chapters"] if c["id"] == args.chapter)
        require_transition(current["state"], "paused")
        current["state"] = "paused"
        current["resume_target"] = previous
        current["pause_reason"] = args.reason
        current["updated_at"] = now_iso()

    store.update(mutate, event="chapter_paused")
    print(
        f"{args.chapter}: {previous} → paused\n"
        f"  原因: {args.reason}\n"
        "  最优下一步: 继续时运行 `resume`，系统会先体检再回到暂停前状态。"
    )
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """恢复暂停章节：指纹未变回到暂停前状态，变化则先进 needs_review。"""
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    chapter = _find_chapter(manifest, args.chapter)
    if chapter["state"] != "paused":
        raise CLIError(f"{args.chapter} 状态为 {chapter['state']}，不是 paused")
    stale = fingerprints.stale_input_chapters(manifest, workspace)
    changed = next(
        (keys for cid, keys in stale if cid == args.chapter), set()
    )
    target = chapter.get("resume_target") or "needs_review"
    if changed:
        target = "needs_review"
    try:
        require_transition(chapter["state"], target)
    except StateError:
        # resume 是 paused 的唯一出口：若 resume_target 失效（手工改坏，或跨版本升级后
        # 该状态已被删除——本版本恰好删除了 rewritten/checking 等），直接抛错会让章节
        # 永久卡死在 paused，pause/resume/recover 全都不再受理。回落到安全目标并留痕。
        append_audit(
            workspace / "audit.jsonl",
            "resume_target_fallback",
            chapter_id=args.chapter,
            invalid_target=str(target),
        )
        print(
            f"{args.chapter}: resume_target「{target}」在当前状态机下非法，"
            "已回落到 needs_review（避免永久卡死在 paused）"
        )
        target = "needs_review"
        require_transition(chapter["state"], target)
    store.transition_chapter(
        args.chapter, target, reason="cli_resume" + ("_stale" if changed else "")
    )
    blockers = [
        b for b in chapter.get("blockers", []) if b.get("code") != "STALE_INPUT_FINGERPRINT"
    ]
    if changed:
        blockers.append(
            {
                "code": "STALE_INPUT_FINGERPRINT",
                "kind": "fingerprint",
                "message": f"暂停期间输入变化: {sorted(changed)}，请人工确认后继续",
                "opened_at": now_iso(),
            }
        )

    def clear_pause(updated: dict[str, Any]) -> None:
        current = next(c for c in updated["chapters"] if c["id"] == args.chapter)
        current.pop("resume_target", None)
        current.pop("pause_reason", None)
        current["blockers"] = blockers
        current["updated_at"] = now_iso()

    store.update(clear_pause, event="chapter_resumed")
    print(
        f"{args.chapter}: paused → {target}"
        + ("（暂停期间输入有变化，先进 needs_review）" if changed else "")
    )
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    """检测输入指纹变化，降级过期章节，并重跑预检对账决策。"""
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    downgraded, refreshed = store.refresh_stale_chapters(manifest)
    if not downgraded and not refreshed:
        print("所有章节输入指纹均为最新")
        return 0
    rerun_targets = {
        chapter_id
        for chapter_id, _target, changed in downgraded
        if "source" in changed or "rules" in changed
    }
    for chapter_id, target, changed in downgraded:
        print(
            f"  {chapter_id}: 输入指纹变化 {sorted(changed)}，"
            f"已降级到 {target}"
        )
        if chapter_id in rerun_targets:
            chapter = _find_chapter(store.load(), chapter_id)
            _rerun_precheck_for_chapter(workspace, chapter)
    for chapter_id, changed in refreshed:
        chapter = _find_chapter(store.load(), chapter_id)
        if chapter["state"] == "approved":
            # 已批准章节的输入漂移不能靠刷新指纹掩盖：这里刻意不刷新，使 merge 的
            # 哈希复核继续拒绝该章；撤销须显式 rollback（事实源已提交）。
            print(
                f"  {chapter_id}: 输入已变化 {sorted(changed)}，但章节已批准 —— "
                "未刷新指纹，正式 merge 将继续拒绝该章；"
                "如需按新输入返工，请显式执行 rollback 后重跑"
            )
            continue
        print(f"  {chapter_id}: 输入指纹已刷新 {sorted(changed)}")
        if "source" in changed or "rules" in changed:
            _rerun_precheck_for_chapter(workspace, chapter)
    return 0


def cmd_blockers(args: argparse.Namespace) -> int:
    """列出所有含阻塞项的章节并打印修复指引。"""
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()

    blocked_chapters = [
        chapter for chapter in manifest.get("chapters", [])
        if chapter.get("blockers") or _collect_open_decisions(workspace, manifest, chapter)
    ]
    if not blocked_chapters:
        print("没有阻塞章节")
        return 0

    for chapter in blocked_chapters:
        state = chapter["state"]
        print(
            f"{chapter['id']} (ordinal={chapter['ordinal']}): {state}"
        )
        _print_blocker_guidance(workspace, manifest, chapter)
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    """从异常终止恢复：体检必需文件与指纹，通过后进 needs_review。

    可恢复状态：
    - ``error``：运行期异常写入的失败态；
    - ``rewriting``：进程被强制终止（kill / 断电）留下的中间态。该状态在状态机
      中的唯一出口是 ``awaiting_checks``，而该迁移只由 rewrite 内部提交触发，
      因此必须由 recover 收敛（§10.4「强制终止进程后可恢复」）。
    """
    workspace = _workspace_path(args.workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    chapter = _find_chapter(manifest, args.chapter)
    if chapter["state"] not in _RECOVERABLE_STATES:
        raise CLIError(
            f"{args.chapter} 状态为 {chapter['state']}，不是 "
            + " / ".join(sorted(_RECOVERABLE_STATES))
        )
    origin_state = chapter["state"]
    missing = [
        key
        for key in ("source", "events", "decisions")
        if not (workspace / chapter["paths"][key]).is_file()
    ]
    if missing:
        print(
            f"{args.chapter} 体检失败：缺少文件 {missing}\n"
            "最优下一步: 修复/恢复文件后重跑 recover；或删除该章节工作项重新 ingest。"
        )
        return 1
    stale = fingerprints.stale_input_chapters(manifest, workspace)
    changed = next(
        (keys for cid, keys in stale if cid == args.chapter), set()
    )
    store.transition_chapter(args.chapter, "needs_review", reason="cli_recover")
    blockers = [
        b
        for b in chapter.get("blockers", [])
        if b.get("code") not in
        {"RUNTIME_EXCEPTION", "STALE_INPUT_FINGERPRINT", "INTERRUPTED_REWRITE"}
    ]
    if origin_state == "rewriting":
        blockers.append(
            {
                "code": "INTERRUPTED_REWRITE",
                "kind": "runtime",
                "message": (
                    "检测到改写过程被异常终止（进程被杀/中断）；已收敛为待人工确认，"
                    "请重新 rewrite → check → approve"
                ),
                "opened_at": now_iso(),
            }
        )
    if changed:
        blockers.append(
            {
                "code": "STALE_INPUT_FINGERPRINT",
                "kind": "fingerprint",
                "message": f"恢复体检发现输入变化: {sorted(changed)}",
                "opened_at": now_iso(),
            }
        )

    def clear_error(updated: dict[str, Any]) -> None:
        current = next(c for c in updated["chapters"] if c["id"] == args.chapter)
        current["blockers"] = blockers
        current.pop("last_error", None)
        current["updated_at"] = now_iso()

    store.update(clear_error, event="chapter_recovered")
    if origin_state != "error":
        append_audit(
            workspace / "audit.jsonl",
            "chapter_recovered_from_interruption",
            chapter_id=args.chapter,
            from_state=origin_state,
        )
    print(
        f"{args.chapter}: {origin_state} → needs_review"
        + ("（输入指纹变化，需人工确认）" if changed else "")
    )
    return 0


def cmd_card(args: argparse.Namespace) -> int:
    """卡工厂：制作/取样/验证/判官/注册/索引/状态。"""
    sub = args.card_command
    if sub == "init":
        doc = card_factory.init_card(
            book=args.book,
            author=args.author,
            lib=args.lib,
            card_type=args.type,
            scene=getattr(args, "scene", None),
            workdir=args.workdir,
        )
        print(f"卡已初始化: {card_factory.card_dir(args.workdir, args.book, args.author, args.type, getattr(args, 'scene', None))}")
        print(f"  状态: {doc['state']}")
        return 0
    if sub == "samples":
        doc = card_factory.collect_samples(args.card)
        print(f"取样完成: {doc['slug']} -> {doc['state']}")
        return 0
    if sub == "verify-anchors":
        result = card_factory.verify_anchors(args.card)
        print(f"锚点验证: {result['hit']}/{result['total']} 命中")
        return 0 if result["verified"] else 1
    if sub == "judge":
        result = card_factory.run_judge(args.card, phase=args.phase)
        if args.phase == "reviewer":
            print(f"判官验证: average={result.get('average')}, passed={result.get('passed')}")
        else:
            print(f"写手样本: {len(result.get('writer_samples', []))} 段")
        return 0 if result.get("passed", True) else 1
    if sub == "register":
        target = card_factory.register_card(args.card)
        print(f"卡已注册: {target}")
        return 0
    if sub == "index":
        lib = Path(args.lib)
        card_index.write_card_index(lib)
        print(f"卡库索引已刷新: {lib / '文风蒸馏卡'}")
        return 0
    if sub == "status":
        doc = card_factory.load_card(args.card)
        print(f"slug: {doc['slug']}")
        print(f"state: {doc['state']}")
        print(f"book: {doc['book']} / {doc['author']}")
        print(f"blockers: {'; '.join(doc.get('blockers', [])) or '无'}")
        return 0
    if sub == "list":
        root = Path(args.workdir) if args.workdir else card_factory.DEFAULT_CARD_WORKDIR
        if not root.is_dir():
            print("卡工厂目录为空")
            return 0
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            card_file = child / "card.json"
            if not card_file.is_file():
                continue
            doc = card_factory.load_card(child)
            print(f"{doc['slug']}  {doc['state']}  {doc['book']} / {doc['author']}")
        return 0
    raise CLIError(f"未知 card 子命令: {sub}")


def cmd_lib(args: argparse.Namespace) -> int:
    """索引库管理：list/add/remove/refresh/status。"""
    sub = args.lib_command
    if sub == "list":
        libs = library_registry.list_libraries()
        if not libs:
            print("未登记任何索引库")
            return 0
        for item in libs:
            print(f"{item.get('name')}  {item.get('path')}  last_refreshed_at={item.get('last_refreshed_at')}")
        return 0
    if sub == "add":
        record = library_registry.add_library(args.path, name=args.name)
        print(f"已登记索引库: {record['name']} -> {record['path']}")
        return 0
    if sub == "remove":
        removed = library_registry.remove_library(args.name_or_path)
        if removed:
            print(f"已移除: {args.name_or_path}")
            return 0
        print(f"未找到: {args.name_or_path}")
        return 1
    if sub == "refresh":
        refreshed = library_registry.refresh_all()
        print(f"已刷新 {len(refreshed)} 个索引库")
        for item in refreshed:
            print(f"  {item.get('name')}  {item.get('path')}")
        return 0
    if sub == "status":
        target = args.name_or_path
        libs = library_registry.list_libraries()
        item = next(
            (
                x
                for x in libs
                if x.get("name") == target
                or Path(x["path"]).expanduser().resolve()
                == Path(target).expanduser().resolve()
            ),
            None,
        )
        if item is None:
            print(f"未找到索引库: {target}")
            return 1
        print(f"name: {item.get('name')}")
        print(f"path: {item.get('path')}")
        print(f"added_at: {item.get('added_at')}")
        print(f"last_refreshed_at: {item.get('last_refreshed_at')}")
        print(f"last_error: {item.get('last_error', '无')}")
        return 0
    raise CLIError(f"未知 lib 子命令: {sub}")


def cmd_assets(args: argparse.Namespace) -> int:
    """打印索引库/写作指导的自动发现结果与使用状态（启动检查清单专用）。"""
    workspace = None
    if getattr(args, "workspace", None) is not None:
        workspace = _workspace_path(str(args.workspace))

    try:
        from v4.enhance.index_lib import IndexLibrary, resolve_index_lib, validate_index_lib
        from v4.enhance.writing_guide import WritingGuide, resolve_writing_guide, validate_writing_guide
    except Exception as exc:  # noqa: BLE001
        raise CLIError(f"Enhance 模块不可用: {exc}")

    print("参考资产探测（skill 启动检查）")
    index_error = None
    index_root = None
    if not getattr(args, "no_index_lib", False):
        index_root = resolve_index_lib(
            getattr(args, "index_lib", None), workspace=workspace
        )
        if index_root is None:
            print("  索引库: 未找到")
            print("    - 查找顺序: CLI --index-lib > 工作区 .env > 环境变量 > 已登记索引库 > skill/索引库")
            index_error = "not_found"
        else:
            index_error = validate_index_lib(index_root)
            if index_error:
                print(f"  索引库: {index_root} 不可用: {index_error}")
            else:
                lib = IndexLibrary(index_root, base=workspace or index_root)
                records = lib.scan()
                print(f"  索引库: {index_root}")
                print(f"    资产: {len(records)} 条（风格卡 {len(lib.assets('style_card'))} / 章节索引 {len(lib.assets('index_chapter'))} / 小说索引 {len(lib.assets('index_novel'))}）")
    if index_error and getattr(args, "require_index_lib", True) and not getattr(args, "no_index_lib", False):
        print("  结论: rewrite 将拒绝执行（--require-index-lib 默认开启）")

    writing_root = resolve_writing_guide(
        getattr(args, "writing_guide", None), workspace=workspace
    )
    if writing_root is None:
        print("  写作指导: 未找到（可选）")
    else:
        error = validate_writing_guide(writing_root)
        if error:
            print(f"  写作指导: {writing_root} 不可用: {error}")
        else:
            guide = WritingGuide(writing_root, base=workspace or writing_root)
            records = guide.scan()
            print(f"  写作指导: {writing_root}（{len(records)} 个模块文件）")

    if workspace is not None:
        manifest = ManifestStore(workspace).load()
        assets = manifest.get("enhancements", {}).get("reference_assets", {})
        print(
            "  manifest 记录: "
            f"enabled={assets.get('enabled')}, used={assets.get('used')}, "
            f"description={assets.get('description', '')}"
        )
    return 1 if index_error else 0
