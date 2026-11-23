#!/usr/bin/env python3
"""
小说索引自动生成 v3.32（P1-7）

扫描该小说的全部章节索引 frontmatter → 自动生成小说索引骨架：
- 导航表（编号 | 章节范围 | 索引文件 | 段文件）
- 包含元素聚合（组名 + 出现段数）
- 总章节数累加 + 「最后索引章节」推导

「全书概况 / 主线时间线 / 亮点速查」为文字性内容，由编排器补写（文件内留占位）。

用法:
  python build_novel_index.py --novel 影流之主-佚名 [--lib <索引库>] [--force]
  python build_novel_index.py --all [--lib <索引库>]    # 扫描全部小说
"""
import argparse
import datetime
import glob
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/v333/ → skill 根
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(SKILL_DIR, ".env"))
except Exception:
    pass

INDEX_LIB_DIR = os.environ.get("INDEX_LIB_DIR", "").strip() or os.path.join(SKILL_DIR, "索引库")


def read_frontmatter(path):
    """读取文件 frontmatter（简单 YAML 键值解析；`- ` 列表行收集为包含元素）。返回 (meta, body)。"""
    meta = {}
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            cur_key = None
            for line in text[3:end].splitlines():
                stripped = line.strip()
                if stripped.startswith("- ") and cur_key:
                    if not isinstance(meta.get(cur_key), list):
                        meta[cur_key] = []
                    meta[cur_key].append(stripped[2:].strip())
                elif stripped and ":" in line:
                    k, v = line.split(":", 1)
                    cur_key = k.strip()
                    meta[cur_key] = v.strip().strip('"')
    return meta


def parse_novel_arg(s):
    """'影流之主-佚名' → ('影流之主', '佚名')；最后一段视为作者。"""
    parts = s.rsplit("-", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")


def last_chapter_of(ch_range):
    """章节范围 '第1章~第5章' / '第序章~第1章' → 末章名 '第5章'。"""
    if "~" in ch_range:
        return ch_range.split("~")[-1].strip()
    return ch_range.strip()


def build_novel_index(novel, author, lib, force):
    prefix = f"{novel}-{author}" if author else novel
    idx_files = sorted(
        glob.glob(os.path.join(lib, "章节索引", prefix + "-*-idx.md")),
        key=lambda p: os.path.basename(p),
    )
    if not idx_files:
        print(f"❌ 未找到 {prefix} 的章节索引（{lib}/章节索引/）")
        return 1

    rows, tag_counts, total_chapters, last_idx = [], {}, 0, ""
    for p in idx_files:
        meta = read_frontmatter(p)
        seg_name = os.path.basename(p).replace("-idx.md", "")
        rows.append((os.path.basename(p), meta.get("章节范围", "?"), seg_name + ".md"))
        try:
            total_chapters += int(meta.get("总章节数", 0))
        except (TypeError, ValueError):
            pass
        tags = meta.get("包含元素", [])
        if isinstance(tags, str):
            tags = [t for t in tags.splitlines() if t.strip()]
        for tag in tags:
            tag = tag.strip()
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        rng = meta.get("章节范围", "")
        if rng:
            last_idx = last_chapter_of(rng)

    today = datetime.date.today().isoformat()
    out_path = os.path.join(lib, "小说索引", f"{prefix}.md")
    if os.path.exists(out_path) and not force:
        print(f"⏭️  {out_path} 已存在（--force 覆盖）")
        return 0

    lines = [
        "---",
        f"小说名称: 《{novel}》",
        f"作者: {author}",
        "索引类型: 小说索引",
        "章节范围: 全书",
        f"总章节数: {total_chapters}",
        "是否完结: 连载中",
        "包含元素:",
    ]
    for tag in sorted(tag_counts, key=lambda t: -tag_counts[t]):
        lines.append(f"  - {tag}")
    lines += [
        f"创建日期: {today}",
        f"最后更新: {today}",
        "状态: 完成",
        f"最后索引章节: {last_idx}",
        "---",
        "",
        f"# 《{novel}》创意索引（小说索引）",
        "",
        "> 全书级导航索引：一部小说一个文件，平铺于 `小说索引/`。",
        "> 由 `scripts/build_novel_index.py` 自动生成骨架；概况/亮点为文字性内容，由编排器补写。",
        "",
        "## 全书概况",
        "",
        f"- 章节数：{total_chapters}",
        f"- 索引段数：{len(rows)} 段",
        "- 主角：（编排器补写）",
        "- 主线：（编排器补写）",
        "",
        "## 章节索引导航",
        "",
        "| 编号 | 章节范围 | 索引文件 | 段文件 |",
        "|------|----------|----------|--------|",
    ]
    for i, (idx_name, rng, seg_name) in enumerate(rows, 1):
        lines.append(f"| {i:03d} | {rng} | {idx_name} | {seg_name} |")
    lines += [
        "",
        "## 全书亮点速查",
        "",
        "### 爽点密集区",
        "- （编排器补写）",
        "",
        "### 名场面",
        "- （编排器补写）",
        "",
        "### 金句梗",
        "- （编排器补写）",
        "",
    ]

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"✅ 已生成: {out_path}（{len(rows)} 段 | 总章节 {total_chapters} | 标签 {len(tag_counts)} 组）")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--novel", help="小说名-作者（如 影流之主-佚名）")
    parser.add_argument("--all", action="store_true", help="扫描索引库全部小说")
    parser.add_argument("--lib", default=INDEX_LIB_DIR, help="索引库根目录")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的小说索引")
    args = parser.parse_args()

    if args.all:
        # 书名从 小说原文/ 子目录名获取（目录名 = <书名>-<作者>）——
        # 不能从章节索引文件名反推（文件名含章节范围段，反推必错位）
        novel_dir = os.path.join(args.lib, "小说原文")
        if not os.path.isdir(novel_dir):
            print(f"❌ 索引库缺少 小说原文/ 目录: {novel_dir}")
            return 1
        novels = sorted(
            d for d in os.listdir(novel_dir)
            if os.path.isdir(os.path.join(novel_dir, d))
        )
        if not novels:
            print("❌ 索引库中无小说（小说原文/ 为空）")
            return 1
        rc = 0
        for n in novels:
            novel, author = parse_novel_arg(n)
            rc |= build_novel_index(novel, author, args.lib, args.force)
        return rc
    if args.novel:
        novel, author = parse_novel_arg(args.novel)
        return build_novel_index(novel, author, args.lib, args.force)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
