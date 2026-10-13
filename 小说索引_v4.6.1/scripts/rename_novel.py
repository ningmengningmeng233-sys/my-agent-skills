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
        elif stripped.startswith("对应索引:"):
            # IDX-02：段文件 frontmatter 的对应索引随改名同步指向新索引文件名
            _, value = stripped.split(":", 1)
            value = value.strip().strip("'\"")
            new_value = path_replace_book_key(value, old_na, new_na)
            new_lines.append(f"对应索引: {value if new_value is None else new_value}")
        else:
            new_lines.append(line)

    body = "\n".join(lines[end + 1:])
    fm = "---\n" + "\n".join(new_lines) + "\n---"
    return fm + ("\n" + body if body else "")


def _collect_targets(lib: Path, old_na: str, new_na: str) -> list[tuple[Path, Path]]:
    """按执行顺序收集改名操作：段文件（旧目录内先改名）→ 段目录 → 章节索引 → 导航。

    IDX-02（对齐同族成人索引 v4.6.3 重写机制）：段文件必须逐个同步重命名并更新
    frontmatter，严禁只改目录名而留下旧文件名/旧 frontmatter 造成悬空脱节。
    """
    moves: list[tuple[Path, Path]] = []

    raw_dir = lib / "小说原文" / old_na
    if raw_dir.is_dir():
        for src in prefixed_files(raw_dir, old_na, ".md"):
            if src.name.endswith("-idx.md"):
                continue
            new_name = path_replace_book_key(src.name, old_na, new_na) or src.name
            moves.append((src, src.with_name(new_name)))
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


def _precheck_targets(moves: list[tuple[Path, Path]]) -> list[Path]:
    """IDX-02 冲突预检：所有目标路径必须不存在，否则一个字节都不动。

    返回冲突目标列表（为空即通过）；src == dst 的原位对跳过。
    """
    conflicts: list[Path] = []
    for src, dst in moves:
        if src != dst and dst.exists():
            conflicts.append(dst)
    return conflicts


def _rollback_completed(completed: list[tuple], logger) -> None:
    """IDX-02 逆序回滚：按已完成操作的逆序撤销（文件恢复原字节、目录改回原名）。

    回滚自身失败只记录不抛——调用方已在失败路径上，不再叠加新异常。
    """
    for entry in reversed(completed):
        try:
            if entry[0] == "file":
                _, src, dst, original = entry
                if src != dst and dst.exists():
                    dst.unlink()
                src.write_bytes(original)
            elif entry[0] == "dir":
                _, src, dst = entry
                if dst.exists() and not src.exists():
                    dst.rename(src)
        except OSError as e:
            logger.error("回滚失败 %s: %s", entry[1], e)


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
    if old_na == new_na:
        logger.error("新旧书名/作者完全相同")
        return 1

    moves = _collect_targets(lib, old_na, new_na)
    if not moves:
        logger.warning("未找到 %s 的相关文件", old_na)
        return 0

    conflicts = _precheck_targets(moves)
    if conflicts:
        for dst in conflicts:
            logger.error("目标已存在，拒绝改名（零改动）: %s", dst)
        return 1

    if not args.yes and not _confirm(old_na, new_na, moves):
        logger.info("用户取消重命名")
        return 0

    completed: list[tuple] = []
    for src, dst in moves:
        try:
            if src.is_dir():
                src.rename(dst)
                completed.append(("dir", src, dst))
                logger.info("已重命名目录: %s -> %s", src, dst)
                continue

            original = src.read_bytes()
            text = original.decode("utf-8")
            text = _update_frontmatter_text(text, old_na, new_na, args.new_novel, args.new_author)
            dst.write_text(text, encoding="utf-8")
            if src != dst:
                src.unlink()
            completed.append(("file", src, dst, original))
            logger.info("已更新: %s -> %s", src, dst)
        except (OSError, UnicodeDecodeError) as e:
            logger.error("处理失败 %s: %s", src, e)
            _rollback_completed(completed, logger)
            return 1

    try:
        with IndexDB(str(lib)) as db:
            with db._lock:
                # 字面量逐段比对 + 单次前缀替换：LIKE 里的 `%`、`_` 是通配符，
                # 原写法会把别的书的断点行一起改写
                rows = db.conn.execute("SELECT segment_path, out_path FROM segment_tasks").fetchall()
                try:
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
                except Exception:
                    db.conn.rollback()
                    raise
            logger.info("已更新 %d 条断点记录路径", updated)
    except Exception as e:
        # IDX-02：断点库失败同样触发文件侧逆序回滚，不留半改名坏死状态
        logger.error("更新断点库失败: %s", e)
        _rollback_completed(completed, logger)
        return 1

    logger.info("%s 重命名为 %s 完成", old_na, new_na)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
