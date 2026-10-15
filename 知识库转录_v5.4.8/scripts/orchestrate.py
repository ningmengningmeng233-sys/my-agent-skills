#!/usr/bin/env python3
"""知识库索引编排器：集中式入口，封装 SKILL.md 状态机。

子命令：
  init     初始化索引库目录与 .env 配置模板
  build    从源文件完整建库（拆段 → 生成段索引 → 验证 → 书导航）
  verify   对已生成的段索引执行硬门禁验证
  resume   续跑失败的段（基于 SQLite 断点库）
  delete   删除一本书（或独立文档集整集/单篇）
  status   输出知识库全局状态面板
  semantic 语义索引构建/检索（可选模块，ADR-0004）
"""
import argparse
import datetime
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(SKILL_DIR / ".env")
except Exception:
    pass

from _logging import add_log_format_argument, configure_logging, get_logger  # noqa: E402
from _common import parse_frontmatter, read_text_auto  # noqa: E402

logger = get_logger("orchestrate")

DEFAULT_LIB = os.environ.get("INDEX_LIB_DIR", "").strip() or str(SKILL_DIR / "索引库")

DIRS = ("原文", "书导航", "段索引")


def _run(cmd: list[str], **kwargs) -> int:
    """Run a script and log the command. Returns exit code."""
    logger.info("执行: %s", " ".join(cmd))
    result = subprocess.run(cmd, check=False, **kwargs)
    if result.returncode != 0:
        logger.error("命令退出码 %d: %s", result.returncode, " ".join(cmd))
    return result.returncode


def _python(script: str) -> str:
    return str(SCRIPTS_DIR / script)


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    """Initialize index library directories and .env template."""
    lib = Path(args.lib).resolve()
    lib.mkdir(parents=True, exist_ok=True)
    for d in DIRS:
        (lib / d).mkdir(exist_ok=True)

    env_file = SKILL_DIR / ".env"
    example = SKILL_DIR / ".env.example"
    if not env_file.exists() and example.exists():
        shutil.copy(example, env_file)
        logger.info("已复制 .env.example 到 .env，请编辑填入 API 配置")
    else:
        logger.info(".env 已存在，保留")

    logger.info("索引库初始化完成: %s", lib)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Full build: split -> index -> verify -> novel index."""
    lib = args.lib
    source = args.source
    novel = args.novel
    author = args.author
    volume = args.volume or ""
    chapters_per_seg = args.chapters_per_seg
    chars_per_seg = args.chars_per_seg
    force = args.force

    if not source or not novel or not author:
        logger.error("build 子命令需要 --source/--novel/--author")
        return 2

    # ROUTE/PREFLIGHT：非纯文本资料先转换为 UTF-8 文本；原始文件不覆盖。
    source_ext = Path(source).suffix.lower()
    if source_ext not in {".txt", ".md"}:
        try:
            from convert_docs import convert_file
            converted_dir = os.path.join(lib, ".converted")
            os.makedirs(converted_dir, exist_ok=True)
            converted = os.path.join(converted_dir, Path(source).stem + ".txt")
            convert_file(Path(source), Path(converted))
            logger.info("已将 %s 转换为 %s", source, converted)
            source = converted
        except Exception as exc:
            logger.error("多格式资料转换失败：%s", exc)
            return 2

    # PREFLIGHT: ensure library exists
    for d in DIRS:
        os.makedirs(os.path.join(lib, d), exist_ok=True)

    # EXECUTE: split segments
    split_cmd = [
        sys.executable, _python("split_segments.py"),
        source, "--novel", novel, "--author", author, "--lib", lib,
        "--chapters-per-seg", str(chapters_per_seg),
        "--chars-per-seg", str(chars_per_seg),
    ]
    if volume:
        split_cmd.extend(["--volume", volume])
    if force:
        split_cmd.append("--force")
    if args.log_format:
        split_cmd.extend(["--log-format", args.log_format])

    rc = _run(split_cmd)
    if rc != 0:
        logger.error("拆段失败，停止后续流程")
        return rc

    # EXECUTE: build segment indices
    novel_author = f"{novel}-{author}"
    seg_dir = os.path.join(lib, "原文", novel_author)
    if not os.path.isdir(seg_dir):
        logger.error("拆段后未找到段目录: %s", seg_dir)
        return 1

    index_cmd = [
        sys.executable, _python("build_segment_index.py"),
        seg_dir, "--lib", lib,
        "--workers", str(args.workers),
    ]
    if force:
        index_cmd.append("--force")
    if args.skip_existing:
        index_cmd.append("--skip-existing")
    if args.log_format:
        index_cmd.extend(["--log-format", args.log_format])

    rc = _run(index_cmd)
    if rc != 0:
        logger.warning("部分段索引生成失败，将在 VERIFY 前尝试 resume")
        cmd_resume(args)

    # VERIFY
    rc = cmd_verify(args)
    if rc != 0:
        logger.error("验证未通过，状态为 NEEDS_REVIEW")
        return rc

    # PUBLISH: novel navigation index
    nav_cmd = [
        sys.executable, _python("build_novel_index.py"),
        "--novel", novel_author, "--lib", lib, "--force",
    ]
    if args.log_format:
        nav_cmd.extend(["--log-format", args.log_format])

    return _run(nav_cmd)


def cmd_verify(args: argparse.Namespace) -> int:
    """Run all verification gates."""
    lib = args.lib
    overall = 0

    logger.info("=== VERIFY: 结构完整性校验 ===")
    overall |= _run([sys.executable, _python("_验证批次.py"), "--lib", lib])

    logger.info("=== VERIFY: 行号校准与一致性检查 ===")
    idx_dir = os.path.join(lib, "段索引")
    if os.path.isdir(idx_dir):
        overall |= _run([sys.executable, _python("verify_and_fix_lines.py"), idx_dir, "--lib", lib])

    logger.info("=== VERIFY: 行号引用体检 ===")
    overall |= _run([sys.executable, _python("check_line_refs.py"), "--lib", lib, "--detail"])

    if overall == 0:
        logger.info("所有验证通过")
    else:
        logger.error("验证发现异常，请查看上方输出")
    return overall


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume failed/pending segments by querying the breakpoint DB."""
    lib = args.lib
    db_path = os.path.join(lib, ".index_state.db")
    if not os.path.exists(db_path):
        logger.warning("断点库不存在，无需续跑: %s", db_path)
        return 0

    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT segment_path FROM segment_tasks WHERE status != 'done'"
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        logger.error("读取断点库失败: %s", e)
        return 1

    if not rows:
        logger.info("没有待续跑的段")
        return 0

    paths = [r[0] for r in rows]
    logger.info("发现 %d 个待续跑段", len(paths))

    # Batch to avoid too-long command line
    batch_size = 50
    overall = 0
    for i in range(0, len(paths), batch_size):
        batch = paths[i : i + batch_size]
        cmd = [
            sys.executable, _python("build_segment_index.py"),
            *batch, "--lib", lib, "--workers", str(args.workers),
        ]
        if args.log_format:
            cmd.extend(["--log-format", args.log_format])
        overall |= _run(cmd)

    return overall


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete a novel via delete_novel.py."""
    if not args.novel or not args.author:
        logger.error("delete 子命令需要 --novel/--author")
        return 2
    cmd = [
        sys.executable, _python("delete_novel.py"),
        "--novel", args.novel, "--author", args.author, "--lib", args.lib,
    ]
    if args.yes:
        cmd.append("--yes")
    if args.log_format:
        cmd.extend(["--log-format", args.log_format])
    return _run(cmd)


def _parse_novel_author(segment_path: str) -> tuple[str, str]:
    """兜底：按段文件名猜 (novel, author)。仅在前端 frontmatter 读取失败时使用。"""
    base = Path(segment_path).stem
    # segment filename pattern: novel-author-range-NNN
    parts = base.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        base = parts[0]
    # heuristic: split on '-' from the right, longest author candidate
    tokens = base.split("-")
    if len(tokens) >= 2:
        return tokens[0], "-".join(tokens[1:])
    return base, ""


def _book_from_segment_path(segment_path: str, cache: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """优先从段文件 frontmatter 取小说名/作者（v4.6.3，书名含 `-` 不再被猜错）。"""
    if segment_path in cache:
        return cache[segment_path]
    try:
        meta, _ = parse_frontmatter(read_text_auto(segment_path))
        novel = str(meta.get("小说名称", "")).strip("《》").strip()
        author = str(meta.get("作者", "")).strip()
        if novel and author:
            cache[segment_path] = (novel, author)
            return novel, author
    except Exception:
        pass
    fallback = _parse_novel_author(segment_path)
    cache[segment_path] = fallback
    return fallback


def cmd_semantic(args: argparse.Namespace) -> int:
    """Delegate semantic index build/search to the standalone scripts."""
    cmd: list[str]
    if args.semantic_command == "build":
        cmd = [sys.executable, _python("semantic_index.py"), "--build", "--lib", args.lib]
        if args.model:
            cmd.extend(["--model", args.model])
        if args.force:
            cmd.append("--force")
        if args.batch_size != 32:
            cmd.extend(["--batch-size", str(args.batch_size)])
        if args.log_format:
            cmd.extend(["--log-format", args.log_format])
        return _run(cmd)

    if args.semantic_command == "search":
        cmd = [
            sys.executable, _python("semantic_search.py"),
            args.query, "--lib", args.lib,
            "--top-k", str(args.top_k),
            "--threshold", str(args.threshold),
        ]
        if args.model:
            cmd.extend(["--model", args.model])
        if args.json:
            cmd.append("--json")
        if args.log_format:
            cmd.extend(["--log-format", args.log_format])
        return _run(cmd)

    logger.error("未知 semantic 子命令: %s", args.semantic_command)
    return 2


def cmd_status(args: argparse.Namespace) -> int:
    """Print a global status panel from the breakpoint database."""
    lib = args.lib
    db_path = os.path.join(lib, ".index_state.db")

    print(f"\n📊 索引库状态面板: {lib}")
    print(f"   生成时间: {_now()}")

    if not os.path.exists(db_path):
        print("   断点库尚未创建（还没有执行过索引任务）")
        # Still show filesystem-level counts
        for d in DIRS:
            p = Path(lib) / d
            count = len(list(p.iterdir())) if p.exists() else 0
            print(f"   {d}: {count} 项")
        return 0

    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """SELECT segment_path, status, retries, error, prompt_tokens,
                      completion_tokens, updated_at
               FROM segment_tasks"""
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        logger.error("读取断点库失败: %s", e)
        return 1

    # Aggregate by novel-author
    books: dict[tuple[str, str], dict] = {}
    book_cache: dict[str, tuple[str, str]] = {}
    for segment_path, status, retries, error, p_tokens, c_tokens, updated_at in rows:
        novel, author = _book_from_segment_path(segment_path, book_cache)
        key = (novel, author)
        book = books.setdefault(key, {
            "pending": 0, "done": 0, "failed": 0, "other": 0,
            "prompt": 0, "completion": 0, "errors": [], "last_update": 0,
        })
        book[status if status in ("pending", "done", "failed") else "other"] += 1
        book["prompt"] += p_tokens or 0
        book["completion"] += c_tokens or 0
        book["last_update"] = max(book["last_update"], updated_at or 0)
        if status == "failed" and error:
            book["errors"].append(f"{Path(segment_path).name}: {error}")

    total_done = sum(b["done"] for b in books.values())
    total_failed = sum(b["failed"] for b in books.values())
    total_prompt = sum(b["prompt"] for b in books.values())
    total_completion = sum(b["completion"] for b in books.values())

    print(f"\n   书籍数: {len(books)} | 总段数: {len(rows)}")
    print(f"   已完成: {total_done} | 失败: {total_failed}")
    print(f"   Token 累计: prompt {total_prompt:,} / completion {total_completion:,}")
    print()

    if not books:
        print("   暂无书籍记录")
        return 0

    header = f"{'资料名/书名':<20} {'作者':<12} {'待处理':>6} {'已完成':>6} {'失败':>6} {'Prompt':>10} {'Completion':>10} {'最后更新':>19}"
    print(header)
    print("-" * len(header))
    for (novel, author), b in sorted(books.items(), key=lambda kv: -kv[1]["last_update"]):
        last = datetime.datetime.fromtimestamp(b["last_update"]).strftime("%Y-%m-%d %H:%M:%S") if b["last_update"] else "-"
        print(
            f"{novel:<20} {author:<12} "
            f"{b['pending']:>6} {b['done']:>6} {b['failed']:>6} "
            f"{b['prompt']:>10,} {b['completion']:>10,} {last:>19}"
        )

    failed_books = [(k, b) for k, b in books.items() if b["failed"]]
    if failed_books:
        print("\n❌ 失败明细（最近 10 条）:")
        for (novel, author), b in failed_books[:10]:
            print(f"  《{novel}》- {author}: {b['failed']} 段失败")
            for err in b["errors"][:3]:
                print(f"      - {err}")

    print()
    return 0 if total_failed == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="知识库索引编排器：集中式入口",
    )
    parser.add_argument("--lib", default=DEFAULT_LIB, help="索引库根目录")
    add_log_format_argument(parser)

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化索引库")
    p_init.set_defaults(func=cmd_init)

    p_build = sub.add_parser("build", help="从源文件完整建库")
    p_build.add_argument("--source", required=True, help="源文件路径")
    p_build.add_argument("--novel", required=True, help="资料/书名称")
    p_build.add_argument("--author", required=True, help="作者名称")
    p_build.add_argument("--volume", default="", help="卷号前缀（分卷源文件）")
    p_build.add_argument("--chapters-per-seg", type=int, default=5)
    p_build.add_argument("--chars-per-seg", type=int, default=10000)
    p_build.add_argument("--workers", type=int, default=32)
    p_build.add_argument("--force", action="store_true", help="覆盖已有段/索引")
    p_build.add_argument("--skip-existing", action="store_true", help="段未变时跳过索引生成")
    p_build.set_defaults(func=cmd_build)

    p_verify = sub.add_parser("verify", help="执行验证门禁")
    p_verify.set_defaults(func=cmd_verify)

    p_resume = sub.add_parser("resume", help="续跑失败/未完成的段")
    p_resume.add_argument("--workers", type=int, default=32)
    p_resume.set_defaults(func=cmd_resume)

    p_delete = sub.add_parser("delete", help="删除一本书或资料")
    p_delete.add_argument("--novel", required=True)
    p_delete.add_argument("--author", required=True)
    p_delete.add_argument("--yes", action="store_true", help="跳过二次确认")
    p_delete.set_defaults(func=cmd_delete)

    p_status = sub.add_parser("status", help="索引库状态面板")
    p_status.set_defaults(func=cmd_status)

    p_semantic = sub.add_parser("semantic", help="语义索引构建/检索（可选模块，ADR-0004）")
    semantic_sub = p_semantic.add_subparsers(dest="semantic_command", required=True)

    p_sem_build = semantic_sub.add_parser("build", help="构建语义索引")
    p_sem_build.add_argument("--model", default=None, help="embedding 模型名称")
    p_sem_build.add_argument("--force", action="store_true", help="强制重建语义索引")
    p_sem_build.add_argument("--batch-size", type=int, default=32, help="编码批次大小")
    p_sem_build.set_defaults(func=cmd_semantic)

    p_sem_search = semantic_sub.add_parser("search", help="语义检索")
    p_sem_search.add_argument("query", help="自然语言查询")
    p_sem_search.add_argument("--model", default=None, help="embedding 模型名称")
    p_sem_search.add_argument("--top-k", type=int, default=5, help="返回结果数")
    p_sem_search.add_argument("--threshold", type=float, default=0.5, help="相似度阈值")
    p_sem_search.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_sem_search.set_defaults(func=cmd_semantic)

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    configure_logging(fmt=args.log_format)

    # Ensure library directories exist for all commands except init
    if args.command != "init":
        for d in DIRS:
            os.makedirs(os.path.join(args.lib, d), exist_ok=True)

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
