#!/usr/bin/env python3
import argparse
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from _logging import add_log_format_argument, configure_logging, get_logger
from index_db import IndexDB


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="迁移索引库根目录，并更新断点库中的绝对路径")
    parser.add_argument("--from", dest="from_dir", required=True, help="原索引库根目录")
    parser.add_argument("--to", dest="to_dir", required=True, help="新索引库根目录")
    parser.add_argument("--copy", action="store_true", help="复制而非移动")
    parser.add_argument("--yes", action="store_true", help="跳过二次确认")
    add_log_format_argument(parser)
    return parser.parse_args()


def _confirm(from_dir: Path, to_dir: Path, copy: bool) -> bool:
    verb = "复制" if copy else "移动"
    print(f"即将{verb}索引库：\n  {from_dir}\n-> {to_dir}")
    answer = input("确认迁移？(yes/no): ").strip().lower()
    return answer in ("yes", "y")


def main() -> int:
    args = _parse_args()
    configure_logging(fmt=args.log_format)
    logger = get_logger("migrate_library")

    from_dir = Path(args.from_dir).resolve()
    to_dir = Path(args.to_dir).resolve()

    if not from_dir.exists():
        logger.error("原索引库不存在: %s", from_dir)
        return 1
    if to_dir.exists() and any(to_dir.iterdir()):
        logger.error("目标目录已存在且非空: %s", to_dir)
        return 1

    if not args.yes and not _confirm(from_dir, to_dir, args.copy):
        logger.info("用户取消迁移")
        return 0

    try:
        to_dir.parent.mkdir(parents=True, exist_ok=True)
        if args.copy:
            shutil.copytree(from_dir, to_dir)
            logger.info("已复制索引库到: %s", to_dir)
        else:
            shutil.move(str(from_dir), str(to_dir))
            logger.info("已移动索引库到: %s", to_dir)
    except OSError as e:
        logger.error("迁移目录失败: %s", e)
        return 1

    # v5.0 目录与结构升级：若存在旧版目录名，自动重命名
    dir_maps = [
        ("小说原文", "原文"),
        ("章节索引", "段索引"),
        ("小说索引", "书导航"),
    ]
    for old_d, new_d in dir_maps:
        src_d = to_dir / old_d
        dst_d = to_dir / new_d
        if src_d.is_dir():
            if not dst_d.exists():
                src_d.rename(dst_d)
                logger.info("旧版目录已升级: %s -> %s", old_d, new_d)
            else:
                for item in src_d.iterdir():
                    shutil.move(str(item), str(dst_d / item.name))
                src_d.rmdir()
                logger.info("旧版目录内容已合并升级: %s -> %s", old_d, new_d)

    # 升级段索引与书导航文件内部的旧字段与旧路径
    idx_dir = to_dir / "段索引"
    if idx_dir.is_dir():
        for f in idx_dir.glob("*-idx.md"):
            try:
                txt = f.read_text(encoding="utf-8")
                new_txt = txt.replace("索引类型: 章节索引", "索引类型: 段索引")
                new_txt = new_txt.replace("源文件: 小说原文/", "源文件: 原文/")
                if new_txt != txt:
                    f.write_text(new_txt, encoding="utf-8")
            except OSError as e:
                logger.warning("更新段索引字段失败 %s: %s", f, e)

    nav_dir = to_dir / "书导航"
    if nav_dir.is_dir():
        for f in nav_dir.glob("*.md"):
            try:
                txt = f.read_text(encoding="utf-8")
                new_txt = txt.replace("索引类型: 小说索引", "索引类型: 书导航")
                if new_txt != txt:
                    f.write_text(new_txt, encoding="utf-8")
            except OSError as e:
                logger.warning("更新书导航字段失败 %s: %s", f, e)

    try:
        with IndexDB(str(to_dir)) as db:
            with db._lock:
                cursor = db.conn.execute("SELECT segment_path FROM segment_tasks")
                rows = cursor.fetchall()
                updated = 0
                for (old_path,) in rows:
                    new_path = old_path.replace(str(from_dir), str(to_dir), 1)
                    new_path = new_path.replace("/小说原文/", "/原文/").replace("\\小说原文\\", "\\原文\\")
                    if new_path != old_path:
                        db.conn.execute(
                            "UPDATE segment_tasks SET segment_path = ? WHERE segment_path = ?",
                            (new_path, old_path),
                        )
                        updated += 1
                db.conn.commit()
            logger.info("已更新 %d 条断点记录路径", updated)
    except OSError as e:
        logger.error("更新断点库路径失败: %s", e)
        return 1

    logger.info("索引库迁移完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
