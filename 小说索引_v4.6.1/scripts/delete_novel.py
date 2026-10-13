#!/usr/bin/env python3
import argparse
import shutil
import sys
from pathlib import Path

if __name__ == "__main__":  # 只有 CLI 直跑才改；被导入时不得动宿主 stdout（会吞掉测试框架的汇总行）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from _common import path_has_book_key, prefixed_files
from _logging import add_log_format_argument, configure_logging, get_logger
from index_db import IndexDB


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="删除一本书的所有索引产物与断点记录")
    parser.add_argument("--novel", required=True, help="小说名称")
    parser.add_argument("--author", required=True, help="作者名称")
    parser.add_argument("--lib", required=True, help="索引库根目录")
    parser.add_argument("--yes", action="store_true", help="跳过二次确认直接删除")
    add_log_format_argument(parser)
    return parser.parse_args()


def _collect_paths(lib: Path, novel: str, author: str) -> list[Path]:
    novel_author = f"{novel}-{author}"
    paths: list[Path] = []

    raw_dir = lib / "小说原文" / novel_author
    if raw_dir.exists():
        paths.append(raw_dir)

    # 字面量前缀匹配：书名含 `[`、`?`、`*` 时 glob 会把它当模式，连带删掉他书产物
    paths.extend(prefixed_files(lib / "章节索引", novel_author, "-idx.md"))

    nav_file = lib / "小说索引" / f"{novel_author}.md"
    if nav_file.exists():
        paths.append(nav_file)

    return sorted(paths)


def _confirm(novel: str, author: str, paths: list[Path]) -> bool:
    print(f"即将删除《{novel}》- {author} 的所有索引产物：")
    for p in paths:
        print(f"  - {p}")
    answer = input("确认删除？(yes/no): ").strip().lower()
    return answer in ("yes", "y")


def main() -> int:
    args = _parse_args()
    configure_logging(fmt=args.log_format)
    logger = get_logger("delete_novel")

    lib = Path(args.lib).resolve()
    if not lib.exists():
        logger.error("索引库不存在: %s", lib)
        return 1

    paths = _collect_paths(lib, args.novel, args.author)
    if not paths:
        logger.warning("未找到《%s》- %s 的任何索引产物", args.novel, args.author)
        return 0

    if not args.yes and not _confirm(args.novel, args.author, paths):
        logger.info("用户取消删除")
        return 0

    for p in paths:
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            logger.info("已删除: %s", p)
        except OSError as e:
            logger.error("删除失败 %s: %s", p, e)
            return 1

    try:
        with IndexDB(str(lib)) as db:
            prefix = f"{args.novel}-{args.author}"
            with db._lock:
                # 字面量逐段比对，不把书名拼进 SQL LIKE：`%`、`_` 在 LIKE 里是通配符，
                # 原写法会把别的书的断点行一起删掉（数据丢失级）
                rows = db.conn.execute("SELECT segment_path, out_path FROM segment_tasks").fetchall()
                removed = 0
                for segment_path, out_path in rows:
                    if not (path_has_book_key(segment_path, prefix)
                            or path_has_book_key(out_path, prefix)):
                        continue
                    cur = db.conn.execute(
                        "DELETE FROM segment_tasks WHERE segment_path = ?",
                        (segment_path,),
                    )
                    removed += max(cur.rowcount, 0)
                db.conn.commit()
            logger.info("已清理 %d 条断点记录", removed)
    except OSError as e:
        logger.error("清理断点库失败: %s", e)
        return 1

    logger.info("《%s》- %s 删除完成", args.novel, args.author)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
