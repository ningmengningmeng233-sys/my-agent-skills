#!/usr/bin/env python3
"""
日系小说本地化 — 拆批脚本 v1.0

日语小说 → 本地化批文件：
- 日文章节识别：第X話/第X章/第X回/第X話(前)/プロローグ/エピローグ/序章/終章/番外
- 每批 ≤ 5000 字（句子边界切分，优先章节边界，其次句号，最后段落）
- 批文件带 frontmatter（小说名/作者/章节范围/批号/分段方式）
- 幂等：目标文件已存在 → 跳过（--force 覆盖）

用法:
  python split_ja_segments.py "<源文件>" --novel 小说名 --author 作者 \
    [--out <输出目录>] [--max-chars 5000] [--force]

依赖: 无（纯标准库）
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 日文章节标题模式 ─────────────────────────────────────────────
# 覆盖：第X話/第X章/第X回 + 日式"卷+序章"标题（一卷 序章 〇〇）+ 无编号（プロローグ等）
CHAPTER_RE = re.compile(
    r"^第[一二三四五六七八九十百千万0-9０-９]+[話话章回][^\n]{0,40}"
    r"|^第[一二三四五六七八九十百千万0-9０-９]+[話话章回][（(][^\n]{0,30}"
    r"|^(?:[一二三四五六七八九十百千万0-9０-９]+卷|第[一二三四五六七八九十百千万0-9０-９]+卷)\s*(?:序章|終章|間章|番外|第[^\n]{0,20})[^\n]{0,40}"
    r"|^(?:プロローグ|エピローグ|序章|終章|終幕|番外|間章|幕間|後書き|あとがき)[^\n]{0,40}",
    re.M,
)

# 章节名规范化：第X話 → 第X話（保留原名用于映射）；プロローグ → 序章
CH_NAME_RE = re.compile(
    r"第[一二三四五六七八九十百千万0-9０-９]+[話话章回]|プロローグ|エピローグ|序章|終章|番外|間章",
    re.I,
)

JP_KANA_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}

JP_SPECIAL = {"プロローグ": "序章", "エピローグ": "终章", "序章": "序章",
              "終章": "终章", "番外": "番外", "間章": "间章"}


def cn2ar(s):
    """中文数字 → 阿拉伯（1~999 够用）"""
    if s.isdigit() or s.isdecimal():
        return int(s)
    total, num = 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            num = CN_DIGITS[ch]
        elif ch == "十":
            total += num * 10 if num else 10
            num = 0
        elif ch == "百":
            total += num * 100 if num else 100
            num = 0
    return total + num


def normalize_ch_name(raw):
    """章名规范化：第X話→第X话、プロローグ→序章、第X章→第X章"""
    m = re.search(r"(?:第)?([一二三四五六七八九十百零0-9０-９]+)([話话章回])", raw)
    if m:
        num = cn2ar(m.group(1).replace("０", "0").replace("１", "1")
                    .replace("２", "2").replace("３", "3").replace("４", "4")
                    .replace("５", "5").replace("６", "6").replace("７", "7")
                    .replace("８", "8").replace("９", "9"))
        return f"第{num}{'话' if m.group(2) == '話' else m.group(2)}"
    for jp, zh in JP_SPECIAL.items():
        if raw.startswith(jp):
            return zh
    return raw.strip()


def norm_name(name):
    """文件名规范化：去《》、空白、非法字符"""
    return re.sub(r'[\\/:*?"<>|\s《》]', "", name or "")


def parse_frontmatter(text):
    """源文件可能自带 frontmatter；返回 (meta, body)"""
    meta = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            body = text[end + 4:]
            for line in fm.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"')
    return meta, body


def find_chapter_lines(body_lines):
    """返回 [(行号 1-based, 标题, 规范化章名), ...]"""
    out = []
    for i, line in enumerate(body_lines, 1):
        m = CHAPTER_RE.match(line.strip())
        if m:
            title = m.group(0).strip()
            nm = CH_NAME_RE.search(title)
            out.append((i, title, normalize_ch_name(nm.group(0)) if nm else f"第{i}节"))
    return out


def split_by_chars(body_lines, max_chars, novel, author, seg_dir, force):
    """无章节：按 ≤max_chars 字在句子边界切分（复用 novel-truncator 思路）"""
    created = 0
    full = "\n".join(body_lines)
    seg_no = 0
    start = 0
    total = len(full)
    # 中文/日文句子结束标记
    sentence_endings = "。！？!?…」』」"
    while start < total:
        end = min(start + max_chars, total)
        if end < total:
            # 从 end 往回找句子结束位置（至少留 max_chars*0.8 长度）
            search_from = max(start + int(max_chars * 0.8), start)
            cut = -1
            for i in range(end, search_from - 1, -1):
                if full[i - 1] in sentence_endings:
                    cut = i
                    break
            if cut == -1:
                # 段落边界
                cut = full.rfind("\n\n", search_from, end)
                if cut == -1:
                    cut = end
            end = cut
        seg_no += 1
        chunk = full[start:end]
        fname = f"{norm_name(novel)}-{norm_name(author)}-第{start//10000+1}万字~第{end//10000+1}万字-{seg_no:03d}.md"
        fpath = os.path.join(seg_dir, fname)
        if not (os.path.exists(fpath) and not force):
            frontmatter = {
                "novel": novel, "author": author,
                "chapter_range": f"第{start//10000+1}万字~第{end//10000+1}万字",
                "split_mode": "按字数切分",
            }
            make_seg_file(seg_dir, fname, frontmatter, chunk)
            created += 1
        start = end
    return created


def split_long_chapter(body_lines, ch_start, ch_end, max_chars):
    """超长章按句子边界子切，返回 [文本段, ...]（字符级切分，单行超长也能切）。
    优先句子边界（。！？…」』），其次段落边界（空行），最后硬切。"""
    full = "\n".join(body_lines[ch_start - 1:ch_end])
    segs = []
    start, total = 0, len(full)
    while start < total:
        end = min(start + max_chars, total)
        if end < total:
            search_from = max(start + int(max_chars * 0.8), start)
            cut = -1
            for i in range(end, search_from - 1, -1):
                if full[i - 1] in "。！？!?…」』":
                    cut = i
                    break
            if cut == -1:
                cut = full.rfind("\n\n", search_from, end)
                if cut == -1:
                    cut = end
            end = cut
        segs.append(full[start:end])
        start = end
    return segs


def make_seg_file(seg_dir, fname, frontmatter, body):
    fm = (
        "---\n"
        f"小说名称: 《{frontmatter['novel']}》\n"
        f"作者: {frontmatter['author']}\n"
        f"章节范围: {frontmatter['chapter_range']}\n"
        f"分段方式: {frontmatter['split_mode']}\n"
        f"批号: {fname.split('-')[-1].replace('.md','')}\n"
        "---\n"
    )
    with open(os.path.join(seg_dir, fname), "w", encoding="utf-8") as f:
        f.write(fm + body)


def main():
    ap = argparse.ArgumentParser(description="日系小说拆批（本地化前置）")
    ap.add_argument("source", help="源小说文件路径 (.txt/.md)")
    ap.add_argument("--novel", required=True, help="小说名")
    ap.add_argument("--author", required=True, help="作者名")
    ap.add_argument("--out", default="批文件", help="输出目录")
    ap.add_argument("--max-chars", type=int, default=5000, help="每批最大字数（默认5000）")
    ap.add_argument("--chapters-per-seg", type=int, default=0,
                    help="每批章数（0=自动：以max-chars为准切章）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在批文件")
    args = ap.parse_args()

    if not os.path.exists(args.source):
        print(f"错误: 源文件不存在 {args.source}")
        sys.exit(1)

    with open(args.source, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    _, body = parse_frontmatter(text)
    body_lines = body.splitlines()

    os.makedirs(args.out, exist_ok=True)

    chapters = find_chapter_lines(body_lines)
    if chapters:
        # 有章节：优先按章节边界，但单章超长时在句子边界切（5000字上限）
        n = len(chapters)
        print(f"识别到 {n} 个章节。")
        # 分组条件：字数超 max_chars 或 章数达 --chapters-per-seg（>0 时生效）
        per_seg = args.chapters_per_seg
        groups = []
        cur = []
        cur_len = 0
        for idx, ch in enumerate(chapters):
            ch_start = ch[0]
            ch_end = (chapters[idx + 1][0] - 1) if idx + 1 < len(chapters) else len(body_lines)
            ch_len = sum(len(l) for l in body_lines[ch_start - 1:ch_end])
            # 若当前组为空，直接加入（保证第一章不丢）
            if not cur:
                cur.append(ch)
                cur_len = ch_len
                continue
            if (per_seg and len(cur) >= per_seg) or cur_len + ch_len > args.max_chars:
                groups.append(cur)
                cur = [ch]
                cur_len = ch_len
            else:
                cur.append(ch)
                cur_len += ch_len
        if cur:
            groups.append(cur)
        created = 0
        file_no = 0
        for gi, grp in enumerate(groups):
            start_line = grp[0][0]
            end_line = (groups[gi + 1][0][0] - 1) if gi + 1 < len(groups) else len(body_lines)
            start_name = grp[0][2]
            last_name = grp[-1][2]
            # 超长章（单章独立成组且超过上限）→ 句子边界子切，保证每批 ≤max_chars
            if len(grp) == 1:
                ch = grp[0]
                ch_len = sum(len(l) for l in body_lines[ch[0] - 1:end_line])
                if ch_len > args.max_chars:
                    subs = split_long_chapter(body_lines, ch[0], end_line, args.max_chars)
                    for sub_i, seg_text in enumerate(subs, 1):
                        file_no += 1
                        fname = f"{norm_name(args.novel)}-{norm_name(args.author)}-{ch[2]}-{file_no:03d}.md"
                        fpath = os.path.join(args.out, fname)
                        if os.path.exists(fpath) and not args.force:
                            continue
                        make_seg_file(args.out, fname, {
                            "novel": args.novel, "author": args.author,
                            "chapter_range": f"{ch[2]}(子段{sub_i}/{len(subs)})",
                            "split_mode": "按章节切分（超长章子切）",
                        }, seg_text)
                        created += 1
                    continue
            file_no += 1
            fname = f"{norm_name(args.novel)}-{norm_name(args.author)}-{start_name}~{last_name}-{file_no:03d}.md"
            fpath = os.path.join(args.out, fname)
            if os.path.exists(fpath) and not args.force:
                continue
            body_part = "\n".join(body_lines[start_line - 1:end_line])
            make_seg_file(args.out, fname, {
                "novel": args.novel, "author": args.author,
                "chapter_range": f"{start_name}~{last_name}",
                "split_mode": "按章节切分",
            }, body_part)
            created += 1
        print(f"拆批完成: {len(groups)} 批（含超长章句子边界子切），新增 {created} 批。")
    else:
        print("未识别到章节标题，按字数切分。")
        created = split_by_chars(body_lines, args.max_chars, args.novel, args.author, args.out, args.force)
        print(f"拆批完成: 按 {args.max_chars} 字/批，新增 {created} 批。")


if __name__ == "__main__":
    main()
