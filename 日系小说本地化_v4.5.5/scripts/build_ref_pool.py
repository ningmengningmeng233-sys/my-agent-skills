#!/usr/bin/env python3
"""
日系小说本地化 — 参考池构建脚本 v1.0（全库参考协议·机械强制）

全库检索 → 按书分组 → 避开轮换记录已参考书 → 选 3-5 本不同书 × 每本 2-3 段
→ 输出参考池文本 + 更新轮换记录。

AI 职责：按参考池条目精读（索引「爽点/看点」+「金句/名场面」区 + 原文行号段落），
并把参考池文本写入批稿 frontmatter `参考池:` 字段。

用法:
  python build_ref_pool.py --scene 打脸碾压 --tags 装逼打脸,打脸 \
    --rotation 05_映射表/爽点轮换记录.md [--index-lib <索引库>] [--out <输出文件>]

退出码: 0=成功, 1=零命中（标签失效，需换同义标签重跑）
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCENE_ALIAS = {  # 样本库 8 场景 ↔ 索引标签 对应（防检索错位）
    "打脸碾压": ["装逼打脸", "打脸"],
    "升级突破": ["实力提升"],
    "战斗对决": ["危机化解", "反杀"],
    "围观震撼": ["震慑全场", "满场"],
    "金句霸气": ["金句"],
    "复仇归来": ["身份反转"],
    "心境意境": ["回忆杀", "虐心"],
    "日常过渡": ["日常"],
}

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(SKILL_DIR, ".env"))
except Exception:
    pass


def find_index_lib(custom: str) -> str:
    """索引库路径：--index-lib > .env INDEX_LIB_DIR > skill 内 索引库/"""
    if custom:
        return custom
    env = os.environ.get("INDEX_LIB_DIR", "").strip()
    if env:
        return env
    return os.path.join(SKILL_DIR, "索引库")


def load_rotation(path: str) -> dict:
    """读轮换记录：{场景: [已参考书,...]}；文件不存在返回空"""
    done = {}
    if not os.path.exists(path):
        return done
    cur = None
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line.startswith("## "):
            cur = line[3:].strip()
            done.setdefault(cur, [])
        elif cur and line.startswith("- 已参考:"):
            books = re.findall(r"([^(),\s]+)\(批[^)]*\)", line)
            done[cur].extend(books)
    return done


def update_rotation(path: str, scene: str, picks: list, reset: bool = False):
    """把本批选择的书追加进轮换记录（按场景分组）。
    reset=True：先清空该场景全部历史行（轮完一轮重置时用，防记录无限膨胀）"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = []
    if os.path.exists(path):
        lines = open(path, encoding="utf-8").read().splitlines()
    if reset:
        # 清除该场景节（节标题 + 其下所有已参考行），保留其他场景
        out, skip = [], False
        for l in lines:
            if l.strip().startswith("## "):
                skip = l.strip() == f"## {scene}"
                if not skip:
                    out.append(l)
                continue
            if not skip:
                out.append(l)
        lines = out
    # 找场景节位置
    sec_idx = None
    for i, l in enumerate(lines):
        if l.strip() == f"## {scene}":
            sec_idx = i
            break
    entry = "- 已参考: " + ", ".join(f"{b}(批新)" for b in picks) + " → 下批避开，轮完再循环"
    if sec_idx is not None:
        lines.insert(sec_idx + 1, entry)
    else:
        lines.append(f"\n## {scene}")
        lines.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="参考池构建（全库检索→选书→轮换）")
    ap.add_argument("--scene", required=True, help="场景类型（样本库8场景之一或自定义）")
    ap.add_argument("--tags", required=True, help="索引标签，逗号分隔（如 装逼打脸,打脸）")
    ap.add_argument("--rotation", required=True, help="爽点轮换记录.md 路径")
    ap.add_argument("--index-lib", default="", help="索引库路径（默认 .env INDEX_LIB_DIR 或 skill 内 索引库/）")
    ap.add_argument("--out", default="", help="参考池文本输出文件（默认 stdout）")
    args = ap.parse_args()

    lib = find_index_lib(args.index_lib)
    seg_dir = os.path.join(lib, "章节索引")
    if not os.path.isdir(seg_dir):
        print(f"错误: 章节索引目录不存在 {seg_dir}")
        sys.exit(1)

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    if not tags:
        print("错误: 未提供有效标签")
        sys.exit(1)

    def frontmatter_of(text: str) -> str:
        """提取 frontmatter 区（--- 之间的内容），标签只在「包含元素」区匹配"""
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                return text[3:end]
        return ""

    # 1. 全库检索：frontmatter「包含元素」标签匹配 → 按书分组（命中文件计数）
    #    （不用全文子串匹配——正文里的"日常/打脸"等普通词会造成选书漂移）
    books = {}
    for fname in sorted(os.listdir(seg_dir)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(seg_dir, fname)
        try:
            text = open(fpath, encoding="utf-8-sig", errors="replace").read()
        except OSError:
            continue
        fm = frontmatter_of(text)
        if any(t in fm for t in tags):
            book = re.sub(r"-.*$", "", fname)
            books.setdefault(book, []).append(fname)

    if not books:
        print(f"❌ 零命中：标签 {tags} 在 {seg_dir} 的「包含元素」区无命中")
        print("   提示：换同义标签（如 升级突破→实力提升、围观震撼→震慑全场/满场）")
        sys.exit(1)
    if len(books) < 3:
        print(f"❌ 场景「{args.scene}」命中书仅 {len(books)} 本（参考池要求 ≥3 本不同书）")
        print(f"   命中: {sorted(books)}")
        print("   提示：换更宽的同义标签，或检查索引库标签体系；盲审会按 ≥3 本审计")
        sys.exit(1)

    # 2. 避开轮换记录已参考书
    rotation = load_rotation(args.rotation)
    done_books = set(rotation.get(args.scene, []))
    fresh = {b: f for b, f in books.items() if b not in done_books}
    rotated = False
    if not fresh:  # 全库轮完一轮 → 重置（清空该场景历史后重新开始）
        fresh = books
        rotated = True
        print(f"ℹ️ 场景「{args.scene}」已轮完一轮，重置轮换（清空历史）。")

    # 3. 选 3-5 本（命中数降序），每本取 2-3 个命中文件
    ranked = sorted(fresh.items(), key=lambda kv: len(kv[1]), reverse=True)
    picks = ranked[:5] if len(ranked) >= 3 else ranked
    pool_lines = []
    for book, files in picks:
        n = min(3, len(files))
        chosen = files[:n]
        for f in chosen:
            pool_lines.append(f"{book}/{f}")
        print(f"  📚 {book}: 命中 {len(files)} 段，本批取 {n} 段")

    pool_text = "参考池: " + ", ".join(
        f"{b}/{f.replace('-idx.md', '')}-idx" for b, f in
        [(re.sub(r'/(.*)$', '', x), x.split('/', 1)[1]) for x in pool_lines]
    )

    # 4. 更新轮换记录（轮完一轮时清空该场景历史）
    update_rotation(args.rotation, args.scene, [b for b, _ in picks], reset=rotated)

    # 5. 输出
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(pool_text + "\n")
    print(f"\n{pool_text}\n")
    print(f"轮换记录已更新: {args.rotation}")
    print("AI 下一步：按参考池条目精读（索引「爽点/看点」+「金句/名场面」区 + 原文行号段落），")
    print("参考池文本写入批稿 frontmatter `参考池:` 字段。")
    sys.exit(0)


if __name__ == "__main__":
    main()
