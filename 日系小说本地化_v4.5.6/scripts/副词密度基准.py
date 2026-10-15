# -*- coding: utf-8 -*-
"""
副词密度基准.py — AI 滥用副词密度检测与基准重建（2026-08-16）
模式1（重建基准）: python 副词密度基准.py --rebuild <索引库小说原文目录>
   扫描全库统计人类网文密度，输出基准（供文档更新）
模式2（检测文本）: python 副词密度基准.py <批文件>...
   计算词表合计密度，超阈值=AI味违规（单词>最高单书x1.5 或 全表>均值x2.5）

人类网文基准（2026-08-16 实测，全库6332万字/13本）:
- 全词表合计: 37.73 次/万字（AI输出超 94 次/万字 = 违规）
- 单词最高单书密度: 微微8.48/缓缓9.43/刹那9.66/瞬间7.65/顿时7.10
"""
import os
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WORDS = ["微微", "缓缓", "轻轻", "深深", "淡淡", "静静", "悄悄", "默默",
         "隐隐", "悠悠", "冷冷", "猛然", "骤然", "忽然", "突然", "顿时",
         "瞬间", "刹那", "不禁", "不由", "忍不住", "下意识", "莫名", "隐约",
         "仿佛", "似乎", "好像", "十分", "极其", "格外", "颇为", "相当"]

# 基准（2026-08-16 实测）
TOTAL_BASE = 37.73          # 全词表合计密度/万字
TOTAL_LIMIT = 94.0          # 全表阈值 = 均值 x 2.5
SINGLE_HIGHEST = {          # 每词最高单书密度 x 1.5
    "微微": 12.7, "缓缓": 14.1, "刹那": 14.5, "瞬间": 11.5, "顿时": 10.7,
    "突然": 8.0, "似乎": 10.0, "仿佛": 9.0, "轻轻": 3.8, "淡淡": 6.6,
    "隐隐": 3.6, "忽然": 4.6, "默默": 2.3, "深深": 1.7, "悄悄": 1.0,
    "猛然": 0.5, "骤然": 0.5, "冷冷": 0.5, "忍不住": 3.6, "下意识": 1.5,
    "莫名": 1.1, "隐约": 1.3, "十分": 3.9, "极其": 2.6, "格外": 0.9,
    "颇为": 3.2, "好像": 5.2, "不禁": 4.7, "不由": 3.7, "静静": 1.2,
    "悠悠": 0.5,
}

def calc(text):
    wan = max(1.0, len(text) / 10000)
    counts = {w: text.count(w) for w in WORDS}
    total = sum(counts.values())
    return counts, total, total / wan

def check(text, name="文本"):
    counts, total, density = calc(text)
    problems = []
    for w in WORDS:
        limit = SINGLE_HIGHEST.get(w, 10)
        if counts[w] / max(1.0, len(text) / 10000) > limit:
            problems.append(f"「{w}」{counts[w]/max(1.0,len(text)/10000):.1f}/万字 > 上限{limit}")
    if density > TOTAL_LIMIT:
        problems.append(f"词表合计 {density:.1f}/万字 > 阈值 {TOTAL_LIMIT}")
    return problems, density

def rebuild(root):
    all_counts = Counter()
    all_chars = 0
    for book in os.listdir(root):
        d = os.path.join(root, book)
        if not os.path.isdir(d):
            continue
        chars = 0
        counts = Counter()
        for r, _, files in os.walk(d):
            for fn in files:
                if not (fn.endswith(".md") or fn.endswith(".txt")):
                    continue
                try:
                    with open(os.path.join(r, fn), encoding="utf-8", errors="ignore") as f:
                        t = f.read()
                    chars += len(t)
                    for w in WORDS:
                        counts[w] += t.count(w)
                except Exception:
                    continue
        all_counts += counts
        all_chars += chars
        wan = max(1.0, chars / 10000)
        top = " ".join(f"{w}:{counts[w]/wan:.1f}" for w in WORDS[:8] if counts[w])
        print(f"{book}: {wan:.0f}万字 | {top}")
    wan = max(1.0, all_chars / 10000)
    total = sum(all_counts[w] for w in WORDS)
    print(f"\n全库 {wan:.0f} 万字 | 词表合计密度 {total/wan:.2f}/万字")
    print("单书最高密度（x1.5 即单词阈值）:")
    books = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    for w in WORDS:
        hi = 0
        for b in books:
            d = os.path.join(root, b)
            c = 0
            ch = 0
            for r, _, files in os.walk(d):
                for fn in files:
                    if fn.endswith((".md", ".txt")):
                        try:
                            t = open(os.path.join(r, fn), encoding="utf-8", errors="ignore").read()
                            c += t.count(w); ch += len(t)
                        except Exception:
                            pass
            h = c / max(1.0, ch / 10000)
            hi = max(hi, h)
        print(f"  {w}: {hi:.2f} -> x1.5 = {hi*1.5:.1f}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--rebuild":
        rebuild(sys.argv[2])
    else:
        files = sys.argv[1:] or ["-"]
        if files == ["-"]:
            text = sys.stdin.read()
        else:
            text = "\n".join(open(f, encoding="utf-8", errors="ignore").read() for f in files)
        problems, density = check(text)
        if problems:
            print(f"❌ AI 味副词密度异常: {density:.1f}/万字")
            for p in problems:
                print(f"   - {p}")
            sys.exit(1)
        else:
            print(f"✅ 副词密度正常: {density:.1f}/万字（阈值 94）")
