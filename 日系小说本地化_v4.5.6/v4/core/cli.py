from __future__ import annotations
import argparse
import sys
from pathlib import Path
from typing import Callable
from . import card_factory, chapter_split

from .cli_support import (  # noqa: E402
    CLIError,
    _CN_DEFAULT_INPUT_LANGUAGES,
    _PAUSE_SOURCES,
    _RECOVERABLE_STATES,
    _build_coverage_judge,
    _build_event_judge,
    _build_fabrication_auditor,
    _chapter_ids_to_process,
    _collect_open_decisions,
    _decision_paths,
    _find_chapter,
    _load_continuity_snapshots,
    _load_exemplar_cache,
    _mark_chapter_error,
    _name_mappings_for_chapter,
    _precheck_blocker_open,
    _prepare_rewrite,
    _print_blocker_guidance,
    _refresh_chapter_meta,
    _repair_first_paragraph,
    _rerun_precheck_for_chapter,
    _run_check,
    _select_backend,
    _should_use_event_judge,
    _should_use_fabrication_audit,
    _withdraw_approval_pointer,
    _workspace_path,
    _write_chapter_blockers,
)
from .commands.workspace import (  # noqa: E402
    cmd_assets,
    cmd_blockers,
    cmd_card,
    cmd_ingest,
    cmd_init,
    cmd_lib,
    cmd_pause,
    cmd_recover,
    cmd_refresh,
    cmd_resume,
    cmd_status,
)
from .commands.chapter import (  # noqa: E402
    cmd_align_events,
    cmd_check,
    cmd_confirm_boundary,
    cmd_consistency,
    cmd_decide,
    cmd_repair_coverage,
    cmd_resolve_events,
    cmd_rewrite,
)
from .commands.review import (  # noqa: E402
    cmd_approve,
    cmd_blind_review,
    cmd_platform_review,
)
from .commands.delivery import (  # noqa: E402
    cmd_merge,
    cmd_publish_split,
    cmd_rollback,
    cmd_versions,
)

__all__ = [
    'CLIError',
    '_CN_DEFAULT_INPUT_LANGUAGES',
    '_COMMANDS',
    '_PAUSE_SOURCES',
    '_RECOVERABLE_STATES',
    '_build_coverage_judge',
    '_build_event_judge',
    '_build_fabrication_auditor',
    '_chapter_ids_to_process',
    '_collect_open_decisions',
    '_decision_paths',
    '_find_chapter',
    '_load_continuity_snapshots',
    '_load_exemplar_cache',
    '_mark_chapter_error',
    '_name_mappings_for_chapter',
    '_precheck_blocker_open',
    '_prepare_rewrite',
    '_print_blocker_guidance',
    '_refresh_chapter_meta',
    '_repair_first_paragraph',
    '_rerun_precheck_for_chapter',
    '_run_check',
    '_select_backend',
    '_should_use_event_judge',
    '_should_use_fabrication_audit',
    '_withdraw_approval_pointer',
    '_workspace_path',
    '_write_chapter_blockers',
    'build_parser',
    'cmd_align_events',
    'cmd_approve',
    'cmd_assets',
    'cmd_blind_review',
    'cmd_blockers',
    'cmd_card',
    'cmd_check',
    'cmd_confirm_boundary',
    'cmd_consistency',
    'cmd_decide',
    'cmd_ingest',
    'cmd_init',
    'cmd_lib',
    'cmd_merge',
    'cmd_pause',
    'cmd_platform_review',
    'cmd_publish_split',
    'cmd_recover',
    'cmd_refresh',
    'cmd_repair_coverage',
    'cmd_resolve_events',
    'cmd_resume',
    'cmd_rewrite',
    'cmd_rollback',
    'cmd_status',
    'cmd_versions',
    'main',
]

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="日系小说本地化 v4 Core")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="创建 v4 工作区")
    init.add_argument("workspace", type=Path)
    init.add_argument("--title", required=True)
    init.add_argument("--author", default="")
    init.add_argument("--language", default="unknown")

    ingest = subparsers.add_parser("ingest", help="接入源稿并生成章节工作项")
    ingest.add_argument("workspace", type=_workspace_path)
    ingest.add_argument("source", type=Path)
    ingest.add_argument(
        "--max-chapter-chars",
        type=int,
        default=chapter_split.DEFAULT_MAX_CHAPTER_CHARS,
        help="单章最大字符数",
    )
    ingest.add_argument(
        "--event-engine",
        choices=["rule", "llm"],
        default="rule",
        help="事件图引擎：rule=规则抽取；llm=LLM 抽取（失败自动回退 rule）",
    )

    confirm_boundary = subparsers.add_parser(
        "confirm-boundary", help="确认候选章节边界"
    )
    confirm_boundary.add_argument("workspace", type=_workspace_path)
    confirm_boundary.add_argument("--chapter", help="只确认指定章节")

    decide_cmd = subparsers.add_parser(
        "decide", help="一键确认/拒绝章节决策项（默认采用决策项最优方案）"
    )
    decide_cmd.add_argument("workspace", type=_workspace_path)
    decide_cmd.add_argument("chapter")
    decide_cmd.add_argument("decision_id")
    decide_cmd.add_argument(
        "--reject", action="store_true", help="拒绝该决策而不是接受"
    )
    decide_cmd.add_argument("--reason", default="", help="接受/拒绝理由")

    pause_cmd = subparsers.add_parser("pause", help="暂停未完成章节")
    pause_cmd.add_argument("workspace", type=_workspace_path)
    pause_cmd.add_argument("chapter")
    pause_cmd.add_argument("--reason", required=True, help="暂停原因")

    resume_cmd = subparsers.add_parser("resume", help="恢复暂停章节（先体检）")
    resume_cmd.add_argument("workspace", type=_workspace_path)
    resume_cmd.add_argument("chapter")

    recover_cmd = subparsers.add_parser(
        "recover", help="从异常终止恢复（error / 强杀残留的 rewriting）→ needs_review"
    )
    recover_cmd.add_argument("workspace", type=_workspace_path)
    recover_cmd.add_argument("chapter")

    resolve_events_cmd = subparsers.add_parser(
        "resolve-events", help="解析并确认事件图高风险节点"
    )
    resolve_events_cmd.add_argument("workspace", type=_workspace_path)
    resolve_events_cmd.add_argument("--chapter", help="只解析指定章节")
    resolve_events_cmd.add_argument("--list", action="store_true", help="列出所有待确认高风险事件")
    resolve_events_cmd.add_argument("--event-id", help="只处理指定事件（需 --chapter）")
    resolve_events_cmd.add_argument("--reject", action="store_true", help="与 --event-id 配合：拒绝该事件")

    resolve_events_cmd.add_argument(
        "--approve-all",
        action="store_true",
        help="批量批准所有高风险事件（需要显式声明）",
    )

    align_events_cmd = subparsers.add_parser(
        "align-events", help="按 output.md 重新对齐事件图"
    )
    align_events_cmd.add_argument("workspace", type=_workspace_path)
    align_events_cmd.add_argument("--chapter", help="只对齐指定章节")
    align_events_cmd.add_argument(
        "--confirm",
        action="store_true",
        help="确认以 output.md 为基准重定事件图（会写审计）",
    )

    status = subparsers.add_parser("status", help="查看工作区/章节状态")
    status.add_argument("workspace", type=_workspace_path)
    status.add_argument("--chapter", help="只查看指定章节")
    status.add_argument(
        "--verbose", "-v", action="store_true", help="始终打印可操作阻塞项指引"
    )

    rewrite_cmd = subparsers.add_parser("rewrite", help="改写章节")
    rewrite_cmd.add_argument("workspace", type=_workspace_path)
    rewrite_cmd.add_argument("--chapter", help="只改写指定章节")
    rewrite_cmd.add_argument(
        "--backend",
        choices=["placeholder", "default"],
        default="default",
        help="placeholder=确定性占位；default=规则优先+LLM兜底（dry-run安全）",
    )
    rewrite_cmd.add_argument(
        "--index-lib",
        help="索引库根目录；缺省自动发现：CLI>工作区.env>环境变量>已登记索引库>skill/索引库",
    )
    rewrite_cmd.add_argument(
        "--no-index-lib",
        action="store_true",
        help="本次改写不加载索引库（内置本地规则仍会注入 prompt）",
    )
    rewrite_cmd.add_argument(
        "--require-index-lib",
        action="store_true",
        default=True,
        help="没有可用索引库时直接失败（默认开启；--no-index-lib 可关闭）",
    )
    rewrite_cmd.add_argument("--writing-guide", help="写作指导根目录（可选，只读参考）")


    check_cmd = subparsers.add_parser("check", help="运行检查并推进状态机")
    check_cmd.add_argument("workspace", type=_workspace_path)
    check_cmd.add_argument("--chapter", help="只检查指定章节")

    repair_cmd = subparsers.add_parser(
        "repair-coverage", help="定向返修覆盖门判定缺失的段落（每段最多 2 次）"
    )
    repair_cmd.add_argument("workspace", type=_workspace_path)
    repair_cmd.add_argument("--chapter", required=True, help="章节 ID")
    repair_cmd.set_defaults(func=cmd_repair_coverage)

    versions_cmd = subparsers.add_parser("versions", help="列出章节不可变输出版本")
    versions_cmd.add_argument("workspace", type=_workspace_path)
    versions_cmd.add_argument("--chapter", required=True)

    approve_cmd = subparsers.add_parser("approve", help="批准一个章节输出版本")
    approve_cmd.add_argument("workspace", type=_workspace_path)
    approve_cmd.add_argument("--chapter", required=True)
    approve_cmd.add_argument("--version", required=True, help="版本号，例如 v001")

    rollback_cmd = subparsers.add_parser(
        "rollback", help="回滚批准：清指针、保留物理版本、标记下游失效"
    )
    rollback_cmd.add_argument("workspace", type=_workspace_path)
    rollback_cmd.add_argument("--chapter", required=True)

    blind_review_cmd = subparsers.add_parser(
        "blind-review", help="记录盲审结果（pass/blocked/fail）"
    )
    blind_review_cmd.add_argument("workspace", type=_workspace_path)
    blind_review_cmd.add_argument("chapter")
    blind_review_cmd.add_argument(
        "--result", required=True, choices=["pass", "blocked", "fail"], help="盲审结果"
    )

    platform_review_cmd = subparsers.add_parser(
        "platform-review", help="记录平台审核结果（pass/blocked/fail）"
    )
    platform_review_cmd.add_argument("workspace", type=_workspace_path)
    platform_review_cmd.add_argument("chapter")
    platform_review_cmd.add_argument("--platform", required=True, help="已配置的平台名")
    platform_review_cmd.add_argument(
        "--result", required=True, choices=["pass", "blocked", "fail"], help="平台审核结果"
    )


    publish_split_cmd = subparsers.add_parser(
        "publish-split", help="把已交付章节拆成单章发布文件"
    )
    publish_split_cmd.add_argument("workspace", type=_workspace_path)
    publish_split_cmd.add_argument("--platform", default="generic", help="发布平台目录名")
    publish_split_cmd.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="允许跳过未交付章节，只拆已交付部分",
    )

    merge_cmd = subparsers.add_parser("merge", help="合并已通过的章节")
    merge_cmd.add_argument("workspace", type=_workspace_path)
    merge_cmd.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="允许跳过不可合并章节，只合并连续前缀",
    )

    blockers_cmd = subparsers.add_parser("blockers", help="列出所有阻塞章节及修复指引")
    blockers_cmd.add_argument("workspace", type=_workspace_path)

    refresh_cmd = subparsers.add_parser(
        "refresh", help="检测输入指纹变化并降级已过期章节"
    )
    refresh_cmd.add_argument("workspace", type=_workspace_path)

    consistency_cmd = subparsers.add_parser(
        "consistency", help="全书级一致性旁车检查（只读）"
    )
    consistency_cmd.add_argument("workspace", type=_workspace_path)

    assets_cmd = subparsers.add_parser(
        "assets", help="打印索引库/写作指导自动发现结果与使用状态"
    )
    assets_cmd.add_argument("--workspace", type=Path, help="可选：同时显示该工作区 manifest 的参考资产记录")
    assets_cmd.add_argument("--index-lib", help="指定索引库根目录做探测")
    assets_cmd.add_argument("--writing-guide", help="指定写作指导根目录做探测")
    assets_cmd.add_argument("--no-index-lib", action="store_true", help="不探测索引库")
    assets_cmd.add_argument(
        "--require-index-lib",
        action="store_true",
        default=True,
        help="按 rewrite 的默认策略判断是否拒绝执行（--no-index-lib 可关闭）",
    )

    card_cmd = subparsers.add_parser(
        "card", help="卡工厂：制作/取样/验证/判官/注册/索引/状态"
    )
    card_sub = card_cmd.add_subparsers(dest="card_command", required=True)

    card_init = card_sub.add_parser("init", help="初始化一张卡草稿")
    card_init.add_argument("--book", required=True, help="书名")
    card_init.add_argument("--author", required=True, help="作者")
    card_init.add_argument("--lib", required=True, help="索引库根目录")
    card_init.add_argument("--type", choices=["style", "scene"], default="style")
    card_init.add_argument("--scene", help="场景卡必填：如 感情线/战斗对决")
    card_init.add_argument("--workdir", default=str(card_factory.DEFAULT_CARD_WORKDIR), help="卡工厂目录")

    card_samples = card_sub.add_parser("samples", help="从索引库取样")
    card_samples.add_argument("--card", required=True, help="卡工作区目录")

    card_verify = card_sub.add_parser("verify-anchors", help="锚点验证")
    card_verify.add_argument("--card", required=True, help="卡工作区目录")

    card_judge = card_sub.add_parser("judge", help="判官验证")
    card_judge.add_argument("--card", required=True, help="卡工作区目录")
    card_judge.add_argument("--phase", required=True, choices=["writer", "reviewer"])

    card_register = card_sub.add_parser("register", help="注册入库")
    card_register.add_argument("--card", required=True, help="卡工作区目录")

    card_index_cmd = card_sub.add_parser("index", help="重建卡库索引/覆盖率/缺口清单")
    card_index_cmd.add_argument("--lib", required=True, help="索引库根目录")

    card_status = card_sub.add_parser("status", help="查看单张卡状态")
    card_status.add_argument("--card", required=True, help="卡工作区目录")

    card_list = card_sub.add_parser("list", help="列出卡工厂全部卡")
    card_list.add_argument("--workdir", default=str(card_factory.DEFAULT_CARD_WORKDIR), help="卡工厂目录")

    lib_cmd = subparsers.add_parser("lib", help="索引库管理")
    lib_sub = lib_cmd.add_subparsers(dest="lib_command", required=True)
    lib_sub.add_parser("list", help="列出全部已登记索引库")
    lib_add = lib_sub.add_parser("add", help="登记一个索引库")
    lib_add.add_argument("path", help="索引库路径")
    lib_add.add_argument("--name", help="索引库名称（默认取目录名）")
    lib_remove = lib_sub.add_parser("remove", help="移除索引库登记")
    lib_remove.add_argument("name_or_path", help="名称或路径")
    lib_sub.add_parser("refresh", help="刷新全部已登记库的卡索引/缺口清单")
    lib_status = lib_sub.add_parser("status", help="查看索引库状态")
    lib_status.add_argument("name_or_path", help="名称或路径")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    command = _COMMANDS.get(args.command)
    if command is None:
        parser.print_help()
        return 2
    try:
        return command(args)
    except CLIError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 全局写操作异常标记 error
        workspace = getattr(args, "workspace", None)
        chapter_id = getattr(args, "chapter", None)
        if workspace is not None and isinstance(chapter_id, str):
            _mark_chapter_error(Path(workspace), chapter_id, exc)
        print(f"错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "init": cmd_init,
    "ingest": cmd_ingest,
    "confirm-boundary": cmd_confirm_boundary,
    "decide": cmd_decide,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "recover": cmd_recover,
    "resolve-events": cmd_resolve_events,
    "align-events": cmd_align_events,
    "status": cmd_status,
    "rewrite": cmd_rewrite,
    "check": cmd_check,
    "versions": cmd_versions,
    "repair-coverage": cmd_repair_coverage,
    "approve": cmd_approve,
    "rollback": cmd_rollback,
    "blind-review": cmd_blind_review,
    "platform-review": cmd_platform_review,
    "merge": cmd_merge,
    "publish-split": cmd_publish_split,
    "blockers": cmd_blockers,
    "refresh": cmd_refresh,
    "consistency": cmd_consistency,
    "assets": cmd_assets,
    "card": cmd_card,
    "lib": cmd_lib,
}


"""CLI：日系小说本地化 v4 Core 命令式入口（ADR 0002）。

当前支持的最小子命令集：
- init             创建 v4 工作区
- ingest           接入源稿并生成章节工作项
- confirm-boundary 确认候选章节边界
- resolve-events   解析并确认事件图高风险节点
- align-events     按 output.md 重新对齐事件图
- status           查看工作区/章节状态
- rewrite          改写章节（规则优先 + LLM 兜底，默认 dry-run 安全）
- check            运行检查并推进状态机
- merge            合并已通过的章节到 output/localized.md
- blockers         列出所有阻塞章节及修复指引
"""


if __name__ == "__main__":
    raise SystemExit(main())
