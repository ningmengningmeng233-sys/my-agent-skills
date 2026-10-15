#!/usr/bin/env python3

import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SKILL_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from transcript_db import TranscriptDB  # noqa: E402

KNOWLEDGE_DIR = SKILL_DIR / "output"


def parse_txt(path: str | Path) -> dict | None:
    p = Path(path)
    try:
        content = p.read_text(encoding="utf-8")
    except OSError:
        return None

    data = {}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("📹 视频标题："):
            data["title"] = line.replace("📹 视频标题：", "").strip()
        elif line.startswith("🔗 B站链接："):
            data["url"] = line.replace("🔗 B站链接：", "").strip()
        elif line.startswith("👤 作者："):
            data["author"] = line.replace("👤 作者：", "").strip()
        elif line.startswith("📅 发布时间："):
            data["upload_date"] = line.replace("📅 发布时间：", "").strip()
        elif line.startswith("⏱️  视频时长："):
            data["duration"] = line.replace("⏱️  视频时长：", "").strip()
        elif line.startswith("🎬 分P序号："):
            data["page_text"] = line.replace("🎬 分P序号：", "").strip()
        elif line.startswith("📑 分P标题："):
            part = line.replace("📑 分P标题：", "").strip()
            if part and "（单P视频" not in part:
                data["part_title"] = part
        elif line.startswith("📝 转录来源："):
            data["transcript_source"] = line.replace("📝 转录来源：", "").strip()
        elif line.startswith("⏰ 转录时间："):
            data["transcribe_date"] = line.replace("⏰ 转录时间：", "").strip()

    bvid = None
    if data.get("url"):
        m = re.search(r"BV[a-zA-Z0-9]+", data["url"])
        if m:
            bvid = m.group(0)
    if not bvid:
        m = re.search(r"BV[a-zA-Z0-9]+", p.name)
        if m:
            bvid = m.group(0)
    if not bvid:
        return None

    page = 1
    if data.get("page_text"):
        m = re.search(r"P(\d+)", data["page_text"])
        if m:
            page = int(m.group(1))
    else:
        m = re.search(r"[?&]p=(\d+)", data.get("url", "")) or re.search(
            r"_P(\d{1,4})", p.name
        )
        if m:
            page = int(m.group(1))

    data["bvid"] = bvid
    data["page"] = page
    data["part_title"] = data.get("part_title", "")

    summary = ""
    in_summary = False
    in_transcript = False
    transcript_lines = []
    for line in content.splitlines():
        if "第一部分：视频摘要" in line:
            in_summary = True
            continue
        if "第二部分：完整原文" in line:
            in_summary = False
            in_transcript = True
            continue
        if "文档结束" in line:
            in_transcript = False
            continue
        if in_summary:
            stripped = line.strip()
            if (
                stripped
                and not re.match(r"^=+$", stripped)
                and "【AI待处理" not in stripped
                and "待处理" not in stripped
            ):
                summary += stripped + "\n"
        if in_transcript:
            transcript_lines.append(line)

    data["summary"] = summary.strip()
    data["transcript_text"] = "\n".join(transcript_lines).strip()
    data["title"] = data.get("title") or p.name
    data["author"] = data.get("author") or "未知"
    data["duration"] = data.get("duration") or "未知"
    data["upload_date"] = data.get("upload_date") or "未知"
    if data.get("transcribe_date"):
        data["created_at"] = data.pop("transcribe_date")
    return data


def migrate(knowledge_dir: str | Path, dry_run: bool = False) -> None:
    root = Path(knowledge_dir)
    if not root.is_dir():
        print(f"错误: 目录不存在 {root}")
        return

    txt_files = sorted(root.rglob("*.txt"))
    print(f"找到 {len(txt_files)} 个转录文件")

    skipped_no_bvid = 0
    skipped_exists = 0
    imported = 0
    has_summary = 0

    with TranscriptDB() as db:
        for path in txt_files:
            data = parse_txt(path)
            if not data:
                skipped_no_bvid += 1
                continue

            bvid = data["bvid"]
            page = data["page"]
            key = bvid if page == 1 else f"{bvid}_P{page}"

            if db.get_by_bvid(bvid, page):
                skipped_exists += 1
                continue

            if not dry_run:
                db.insert(
                    bvid=bvid,
                    page=page,
                    part_title=data.get("part_title", ""),
                    url=data.get("url", ""),
                    title=data.get("title", ""),
                    author=data.get("author", ""),
                    duration=data.get("duration", ""),
                    upload_date=data.get("upload_date", ""),
                    transcript_source=data.get("transcript_source", ""),
                    transcript_file=str(path),
                    transcript_text=data.get("transcript_text", ""),
                    summary=data.get("summary", ""),
                )
                if data.get("created_at"):
                    db.conn.execute(
                        "UPDATE transcripts SET created_at=? WHERE bvid=? AND page=?",
                        (data["created_at"], bvid, page),
                    )
                    db.conn.commit()

            imported += 1
            if data.get("summary"):
                has_summary += 1
            print(f"  {'[DRY] ' if dry_run else ''}✓ {data['title'][:40]} ({key})")

    print()
    print("=" * 50)
    if dry_run:
        print("[DRY RUN - 未实际写入]")
    print(f"总文件数:        {len(txt_files)}")
    print(f"成功导入:        {imported}")
    print(f"已有记录跳过:    {skipped_exists}")
    print(f"无法解析BVID:    {skipped_no_bvid}")
    print(f"已有摘要:        {has_summary}")
    print(f"缺少摘要:        {imported - has_summary}")


def main():
    parser = argparse.ArgumentParser(description="将旧转录TXT导入SQLite数据库")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不写入")
    parser.add_argument("--stats", action="store_true", help="显示DB统计")
    parser.add_argument(
        "--knowledge-dir", default=str(KNOWLEDGE_DIR), help="output目录路径"
    )
    args = parser.parse_args()

    if args.stats:
        with TranscriptDB() as db:
            s = db.stats()
            print("📊 数据库统计:")
            print(f"   总记录: {s['total']}")
            print(f"   已有摘要: {s['with_summary']}")
            print(f"   待补摘要: {s['pending_summary']}")
        return 0

    knowledge_dir = Path(args.knowledge_dir).expanduser()
    print(f"Knowledge 目录: {knowledge_dir}")
    print(f"模式: {'试运行 (不写入)' if args.dry_run else '正式导入'}")
    print()
    migrate(knowledge_dir, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
