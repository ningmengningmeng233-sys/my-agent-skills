#!/usr/bin/env python3
"""
日系小说本地化 — 源文件体检脚本 v1.0

对拆批后的批文件目录做体检，输出异常清单供用户确认后再进入锚点定标：
- 批文件数与总字数
- 超长批（>6000 字，拆批上限 5000）/ 异常空批
- 乱码字符（U+FFFD �、锟斤拷、烫烫烫 等）
- 重复章节标题（同一章节标题出现在 ≥2 个批文件）
- 批号连续性（文件名末尾 -NNN，缺号提示）

用法:
  python check_source_health.py "<批文件目录>" [--out <报告输出路径>]

退出码: 0=无异常, 1=发现异常（不阻断——是否继续由用户决定）
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 与 split_ja_segments.py 同源：简体为主 + 兼容繁体残留
CHAPTER_RE = re.compile(
    r"^第[一二三四五六七八九十百千万0-9０-９]+[話话章回][^\n]{0,40}"
    r"|^第[一二三四五六七八九十百千万0-9０-９]+[話话章回][（(][^\n]{0,30}"
    r"|^(?:[一二三四五六七八九十百千万0-9０-９]+卷|第[一二三四五六七八九十百千万0-9０-９]+卷)\s*(?:序章|終章|間章|番外|第[^\n]{0,20})[^\n]{0,40}"
    r"|^(?:プロローグ|エピローグ|序章|終章|終幕|番外|間章|幕間|後書き|あとがき)[^\n]{0,40}",
    re.M,
)

GARBLED = ["�", "锟斤拷", "烫烫烫", "屯屯屯", "口口口"]

MAX_BATCH_CHARS = 6000  # 拆批上限 5000，体检阈值放宽到 6000


def strip_frontmatter(text):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def extract_batch_no(fname):
    """从 小说-作者-第1话~第3话-001.md 提取批号 001"""
    m = re.search(r"-(\d{3,})\.md$", fname)
    return int(m.group(1)) if m else None


def scan_dir(seg_dir):
    md_files = sorted(f for f in os.listdir(seg_dir) if f.endswith(".md"))
    issues = []
    total_chars = 0
    batch_nos = []
    title_seen = {}  # 章节标题 -> [批文件名]

    for fname in md_files:
        fpath = os.path.join(seg_dir, fname)
        try:
            with open(fpath, encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            issues.append(f"[读取失败] {fname}: {e}")
            continue
        body = strip_frontmatter(text)
        chars = len(re.sub(r"\s", "", body))
        total_chars += chars

        if chars == 0:
            issues.append(f"[空批] {fname}: 正文为空")
        elif chars > MAX_BATCH_CHARS:
            issues.append(f"[超长批] {fname}: {chars} 字（拆批上限 5000）")

        for g in GARBLED:
            if g in body:
                issues.append(f"[乱码] {fname}: 发现「{g}」")
                break

        for m in CHAPTER_RE.finditer(body):
            t = m.group(0).strip()
            title_seen.setdefault(t, []).append(fname)

        no = extract_batch_no(fname)
        if no is not None:
            batch_nos.append(no)

    # 重复章节标题（跨批出现）
    for t, files in title_seen.items():
        if len(files) > 1:
            issues.append(f"[重复章节] 「{t}」出现在 {len(files)} 个批文件: {', '.join(files[:5])}")

    # 批号连续性
    if batch_nos:
        missing = [i for i in range(1, max(batch_nos) + 1) if i not in set(batch_nos)]
        if missing:
            issues.append(f"[批号缺失] 缺少批号: {missing[:20]}{'…' if len(missing) > 20 else ''}")

    return md_files, total_chars, issues


def main():
    ap = argparse.ArgumentParser(description="拆批结果体检")
    ap.add_argument("seg_dir", help="批文件目录")
    ap.add_argument("--out", default="", help="报告输出路径（默认打印到 stdout）")
    args = ap.parse_args()

    if not os.path.isdir(args.seg_dir):
        print(f"错误: 目录不存在 {args.seg_dir}")
        sys.exit(1)

    md_files, total_chars, issues = scan_dir(args.seg_dir)

    report = ["# 源文件体检报告\n",
              f"- 批文件数: {len(md_files)}",
              f"- 总字数: {total_chars}",
              f"- 异常项: {len(issues)}\n"]

    if issues:
        report.append("## 异常清单（需用户确认）")
        for i, it in enumerate(issues, 1):
            report.append(f"{i}. {it}")
        report.append("\n> 处理方式由用户决定：继续 / 换稿 / 修补后重拆批。"
                      "未确认前不得进入锚点定标。")
    else:
        report.append("✅ 无异常，可进入锚点定标。")

    text = "\n".join(report) + "\n"

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"体检报告已写入: {args.out}")
    print(text)

    sys.exit(0 if not issues else 1)


if __name__ == "__main__":
    main()
