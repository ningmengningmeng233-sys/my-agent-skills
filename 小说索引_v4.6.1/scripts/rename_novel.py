#!/usr/bin/env python3
import argparse
import re
import sys
from pathlib import Path

if __name__ == "__main__":  # 只有 CLI 直跑才改；被导入时不得动宿主 stdout（会吞掉测试框架的汇总行）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from _common import parse_frontmatter, path_replace_book_key, prefixed_files
from _logging import add_log_format_argument, configure_logging, get_logger
from index_db import IndexDB


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重命名一本书的小说名/作者，并同步更新所有文件与断点记录")
    parser.add_argument("--old-novel", required=True, help="原小说名称")
    parser.add_argument("--old-author", required=True, help="原作者名称")
    parser.add_argument("--new-novel", required=True, help="新小说名称")
    parser.add_argument("--new-author", required=True, help="新作者名称")
    parser.add_argument("--lib", required=True, help="索引库根目录")
    parser.add_argument("--yes", action="store_true", help="跳过二次确认直接重命名")
    add_log_format_argument(parser)
    return parser.parse_args()


def _update_frontmatter_text(text: str, old_na: str, new_na: str, new_novel: str, new_author: str) -> str:
    """更新 frontmatter 中的小说名称、作者、源文件路径。"""
    if not text.startswith("---"):
        return text

    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return text

    new_lines = []
    for line in lines[1:end]:
        stripped = line.strip()
        if stripped.startswith("小说名称:"):
            new_lines.append(f"小说名称: 《{new_novel}》")
        elif stripped.startswith("作者:"):
            new_lines.append(f"作者: {new_author}")
        elif stripped.startswith("源文件:"):
            _, value = stripped.split(":", 1)
            value = value.strip().strip("'\"")
            # 逐段字面量替换（不做无限次任意子串 replace）
            new_value = path_replace_book_key(value, old_na, new_na)
            new_lines.append(f"源文件: {value if new_value is None else new_value}")
        else:
            new_lines.append(line)

    body = "\n".join(lines[end + 1:])
    fm = "---\n" + "\n".join(new_lines) + "\n---"
    return fm + ("\n" + body if body else "")


def _collect_targets(lib: Path, old_na: str, new_na: str) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []

    raw_dir = lib / "小说原文" / old_na
    if raw_dir.exists():
        moves.append((raw_dir, lib / "小说原文" / new_na))

    idx_dir = lib / "章节索引"
    # 字面量前缀匹配：书名含 `[`、`?`、`*` 时 glob 会把它当模式，连带改走他书的索引文件
    for src in prefixed_files(idx_dir, old_na, "-idx.md"):
        new_name = path_replace_book_key(src.name, old_na, new_na) or src.name
        moves.append((src, src.with_name(new_name)))

    nav_dir = lib / "小说索引"
    src_nav = nav_dir / f"{old_na}.md"
    if src_nav.exists():
        moves.append((src_nav, nav_dir / f"{new_na}.md"))

    return moves


def _confirm(old_na: str, new_na: str, moves: list[tuple[Path, Path]]) -> bool:
    print(f"即将把 {old_na} 重命名为 {new_na}：")
    for src, dst in moves:
        print(f"  {src} -> {dst}")
    answer = input("确认重命名？(yes/no): ").strip().lower()
    return answer in ("yes", "y")


def main() -> int:
    args = _parse_args()
    configure_logging(fmt=args.log_format)
    logger = get_logger("rename_novel")

    lib = Path(args.lib).resolve()
    if not lib.exists():
        logger.error("索引库不存在: %s", lib)
        return 1

    old_na = f"{args.old_novel}-{args.old_author}"
    new_na = f"{args.new_novel}-{args.new_author}"

    moves = _collect_targets(lib, old_na, new_na)
    if not moves:
        logger.warning("未找到 %s 的相关文件", old_na)
        return 0

    if not args.yes and not _confirm(old_na, new_na, moves):
        logger.info("用户取消重命名")
        return 0

    for src, dst in moves:
        try:
            if src.is_dir():
                src.rename(dst)
                logger.info("已重命名目录: %s -> %s", src, dst)
                continue

            text = src.read_text(encoding="utf-8")
            text = _update_frontmatter_text(text, old_na, new_na, args.new_novel, args.new_author)
            dst.write_text(text, encoding="utf-8")
            if src != dst:
                src.unlink()
            logger.info("已更新: %s -> %s", src, dst)
        except OSError as e:
            logger.error("处理失败 %s: %s", src, e)
            return 1

    try:
        with IndexDB(str(lib)) as db:
            with db._lock:
                # 字面量逐段比对 + 单次前缀替换：LIKE 里的 `%`、`_` 是通配符，
                # 原写法会把别的书的断点行一起改写
                rows = db.conn.execute("SELECT segment_path, out_path FROM segment_tasks").fetchall()
                updated = 0
                for segment_path, out_path in rows:
                    new_seg = path_replace_book_key(segment_path, old_na, new_na)
                    new_out = path_replace_book_key(out_path, old_na, new_na)
                    if new_seg is None and new_out is None:
                        continue
                    cur = db.conn.execute(
                        "UPDATE segment_tasks SET segment_path = ?, out_path = ? WHERE segment_path = ?",
                        (segment_path if new_seg is None else new_seg,
                         out_path if new_out is None else new_out,
                         segment_path),
                    )
                    updated += max(cur.rowcount, 0)
                db.conn.commit()
            logger.info("已更新 %d 条断点记录路径", updated)
    except OSError as e:
        logger.error("更新断点库失败: %s", e)
        return 1

    logger.info("%s 重命名为 %s 完成", old_na, new_na)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
