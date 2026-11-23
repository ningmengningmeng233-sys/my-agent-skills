#!/usr/bin/env python3
"""
日系小说本地化 — 合并交付脚本 v1.0（机械强制：剥离 + 拼接 + 本地化说明）

把 03_改写中/ 的批稿合并为交付物：
1. 每份批稿剥离 frontmatter / 「## 本批爽点对照」区 / 「## 本批说明」区
   → 只保留 章节名+正文
2. 按批号排序拼接 → 《<书名>-<作者>-中国化版.md》（干净版，仅章节名+正文，
   可直接接 novel-to-script；批稿命名约定 <书名>-<作者>-<章节范围>-NNN(-改写).md）
3. 汇总批稿元数据 → 《<书名>-<作者>-本地化说明.md》（正文 + 本地化说明）
4. 校验：无盲审结论（"待盲审"）的批稿标红提示；批号缺号提示

用法:
  python merge_batches.py "<03_改写中目录>" --novel <书名> --author <作者> \
    --out <04_输出目录>

退出码: 0=成功（含警告=1，严重缺批=2）
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def strip_meta(text: str) -> str:
    """剥离 frontmatter"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def strip_notes(text: str) -> str:
    """剥离「本批说明」区（含其后所有内容）"""
    idx = text.find("## 本批说明")
    if idx != -1:
        return text[:idx]
    return text


def strip_front_notes(text: str) -> str:
    """剥离正文前的「本批爽点对照」元信息区（标题+清单行），只留正文"""
    idx = text.find("## 本批爽点对照")
    if idx == -1:
        return text
    head = text[:idx].splitlines()
    rest = text[idx:].splitlines()
    i = 0
    while i < len(rest):
        line = rest[i].strip()
        if line == "" or line.startswith("- ") or line.startswith("## "):
            i += 1
            continue
        break
    return "\n".join(head + rest[i:])


def extract_meta(fname: str, text: str) -> dict:
    """从批稿提取元数据（frontmatter + 本批说明）"""
    meta = {"file": fname}
    fm = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
    meta.update(fm)
    # 本批说明区
    note_idx = text.find("## 本批说明")
    if note_idx != -1:
        note = text[note_idx:]
        meta["notes"] = note
        # 结构判断而非子串：只看"盲审结论: 通过/返工"行——说明区回顾
        # "曾标记待盲审"等文字不影响判定
        meta["has_blind_review"] = bool(re.search(r"盲审结论[:：]\s*(通过|返工)", note))
        # 红线处理行（含确认来源）；去行首 "- " 前缀（说明版输出会再补 "- "）
        meta["redlines"] = []
        for line in note.splitlines():
            if "确认来源" in line or "红线" in line:
                meta["redlines"].append(line.strip().lstrip("- ").strip())
    else:
        meta["notes"] = ""
        meta["has_blind_review"] = False
        meta["redlines"] = []
    return meta


def extract_batch_no(fname: str):
    """兼容两种命名：书名-作者-章节范围-001.md 与 书名-作者-章节范围-001-改写.md"""
    m = re.search(r"-(\d{3,})(?:-改写)?\.md$", fname)
    return int(m.group(1)) if m else None


def main():
    ap = argparse.ArgumentParser(description="批稿合并交付")
    ap.add_argument("batch_dir", help="批稿目录（03_改写中）")
    ap.add_argument("--novel", required=True, help="小说名")
    ap.add_argument("--author", required=True, help="作者名")
    ap.add_argument("--out", required=True, help="输出目录（04_输出）")
    args = ap.parse_args()

    if not os.path.isdir(args.batch_dir):
        print(f"错误: 批稿目录不存在 {args.batch_dir}")
        sys.exit(2)

    md_files = [f for f in os.listdir(args.batch_dir)
                if f.endswith(".md") and ("改写" in f or extract_batch_no(f) is not None)]
    md_files.sort(key=lambda f: extract_batch_no(f) or 0)
    if not md_files:
        print(f"❌ 错误: 批稿目录 {args.batch_dir} 无任何批稿文件"
              f"（命名约定: <书名>-<作者>-<章节范围>-NNN(-改写).md）")
        sys.exit(2)

    os.makedirs(args.out, exist_ok=True)

    body_parts = []
    all_meta = []
    no_review = []
    batch_nos = []

    for fname in md_files:
        fpath = os.path.join(args.batch_dir, fname)
        text = open(fpath, encoding="utf-8-sig", errors="replace").read()
        meta = extract_meta(fname, text)
        all_meta.append(meta)

        body = strip_front_notes(strip_notes(strip_meta(text)))
        body_parts.append(body.strip())

        no = extract_batch_no(fname)
        if no is not None:
            batch_nos.append(no)
        if not meta.get("has_blind_review", False):
            no_review.append(fname)

    # ── 1. 干净版 ──
    clean = "\n\n".join(body_parts) + "\n"
    clean_path = os.path.join(args.out, f"{args.novel}-{args.author}-中国化版.md")
    with open(clean_path, "w", encoding="utf-8") as f:
        f.write(clean)

    # ── 2. 本地化说明（说明版 = 正文 + 说明）──
    redlines_all = [r for r in
                    (x for m in all_meta for x in m.get("redlines", []))
                    if "红线" in r and "无" not in r]
    mappings_all = []
    for m in all_meta:
        notes = m.get("notes", "")
        for line in notes.splitlines():
            s = line.strip()
            if s.startswith("新增映射"):
                mappings_all.append(s)
            elif s.startswith("- 新增映射"):
                mappings_all.append(s.lstrip("- "))

    lines = []
    lines.append(f"# {args.novel} 本地化说明\n")
    lines.append(f"- 书名/作者: 《{args.novel}》-{args.author}")
    lines.append(f"- 处理批次: 共 {len(md_files)} 批")
    lines.append(f"- 盲审状态: {len(md_files) - len(no_review)} 批已盲审 / {len(no_review)} 批待盲审"
                 + ("（⚠️ 未完成盲审的批次未达交付标准）" if no_review else "（全部完成）"))
    if batch_nos:
        missing = [i for i in range(1, max(batch_nos) + 1) if i not in set(batch_nos)]
        if missing:
            lines.append(f"- ⚠️ 批号缺失: {missing}")
    lines.append("")
    lines.append("## 译名变更")
    if mappings_all:
        lines.extend(f"- {x}" for x in mappings_all[:50])
        if len(mappings_all) > 50:
            lines.append(f"- …（共 {len(mappings_all)} 条，详见各批稿）")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 红线处理（含确认来源）")
    confirmed = [r for r in redlines_all if "确认来源" in r]
    pending = [r for r in redlines_all if "确认来源" not in r]
    if confirmed:
        lines.append("### 已确认（用户拍板）")
        lines.extend(f"- {r}" for r in confirmed)
    if pending:
        lines.append("### ⚠️ 待确认（AI 提议，原文未处理）")
        lines.extend(f"- {r}" for r in pending)
    if not confirmed and not pending:
        lines.append("- 无红线记录")
    lines.append("")
    lines.append("## 索引库使用")
    lines.append("- 参考池: 见各批稿 frontmatter `参考池:` 字段")
    lines.append("- 轮换记录: 见 `05_映射表/爽点轮换记录.md`")
    lines.append("")
    lines.append("## 独立质检")
    lines.append("- 盲审结论: 见各批稿「本批说明」区（待盲审批次已标红）")
    lines.append("")
    lines.append("## 待确认遗留")
    lines.append("- 见 `06_质检/待确认清单-最终.md`（若有）")

    doc_path = os.path.join(args.out, f"{args.novel}-{args.author}-本地化说明.md")
    doc = "\n".join(lines) + "\n\n---\n\n" + clean
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write(doc)

    # ── 3. 校验报告 ──
    print(f"✅ 干净版: {clean_path}（{len(clean)} 字符）")
    print(f"✅ 说明版: {doc_path}")
    print(f"批稿数: {len(md_files)}")
    if no_review:
        print(f"⚠️ 待盲审批次 {len(no_review)}: {no_review[:5]}…（未达交付标准，需补盲审）")
    if batch_nos:
        missing = [i for i in range(1, max(batch_nos) + 1) if i not in set(batch_nos)]
        if missing:
            print(f"⚠️ 批号缺失: {missing}")
    sys.exit(1 if no_review else 0)


if __name__ == "__main__":
    main()
