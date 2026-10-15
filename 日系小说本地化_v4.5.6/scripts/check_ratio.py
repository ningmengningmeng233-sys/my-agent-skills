#!/usr/bin/env python3
"""
日系小说本地化 — 篇幅守恒检查脚本 v1.0

对照原文与改写稿，检查字数比是否在 80%-120% 区间（规则：篇幅守恒硬约束）。

用法:
  python check_ratio.py "<原文批文件>" "<改写稿文件>"

退出码: 0=达标, 1=超限（需返工）
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MIN_RATIO = 0.80
MAX_RATIO = 1.20


def strip_frontmatter(text: str) -> str:
    """去掉 frontmatter，只统计正文"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def strip_notes(text: str) -> str:
    """去掉"本批说明"等元信息区（从 --- 分隔线开始）"""
    # 找正文结束的标记（## 本批说明 或 结尾的 ---）
    idx = text.find("## 本批说明")
    if idx != -1:
        return text[:idx]
    return text


def strip_front_notes(text: str) -> str:
    """去掉正文前的"本批爽点对照"元信息区（标题+清单行），只留正文"""
    idx = text.find("## 本批爽点对照")
    if idx == -1:
        return text
    head = text[:idx].splitlines()
    rest = text[idx:].splitlines()
    i = 0
    # 跳过标题行、"- " 开头的清单行、空行，直到正文起始（普通段落/对话）
    while i < len(rest):
        line = rest[i].strip()
        if line == "" or line.startswith("- ") or line.startswith("## "):
            i += 1
            continue
        break
    return "\n".join(head + rest[i:])


def count_chars(text: str) -> int:
    """统计中文字符数（排除空白/标点仅统计汉字+常用符号？不——统计全部非空白字符）"""
    # 去掉空白
    compact = re.sub(r"\s", "", text)
    return len(compact)


def main():
    ap = argparse.ArgumentParser(description="篇幅守恒检查")
    ap.add_argument("source", help="原文批文件")
    ap.add_argument("output", help="改写稿文件")
    args = ap.parse_args()

    for p in (args.source, args.output):
        if not os.path.exists(p):
            print(f"错误: 文件不存在 {p}")
            sys.exit(1)

    with open(args.source, encoding="utf-8-sig", errors="replace") as f:
        src_text = f.read()
    with open(args.output, encoding="utf-8-sig", errors="replace") as f:
        out_text = f.read()

    src_body = strip_notes(strip_frontmatter(src_text))
    out_body = strip_front_notes(strip_notes(strip_frontmatter(out_text)))

    src_len = count_chars(src_body)
    out_len = count_chars(out_body)

    if src_len == 0:
        print("错误: 原文为空")
        sys.exit(1)

    ratio = out_len / src_len

    print(f"原文字数: {src_len}")
    print(f"改写字数: {out_len}")
    print(f"字数比: {ratio:.1%}（要求 {MIN_RATIO:.0%}~{MAX_RATIO:.0%}）")

    if ratio < MIN_RATIO:
        print(f"❌ 篇幅不足：改写稿只有原文的 {ratio:.1%}，低于 {MIN_RATIO:.0%} 下限")
        print(f"   缺少约 {int(src_len * MIN_RATIO) - out_len} 字——检查是否漏写剧情/设定细节")
        sys.exit(1)
    elif ratio > MAX_RATIO:
        print(f"❌ 篇幅超标：改写稿是原文的 {ratio:.1%}，高于 {MAX_RATIO:.0%} 上限")
        print(f"   超出约 {out_len - int(src_len * MAX_RATIO)} 字——检查是否加戏/扩写")
        sys.exit(1)
    else:
        print("✅ 篇幅守恒达标")
        sys.exit(0)


if __name__ == "__main__":
    main()
