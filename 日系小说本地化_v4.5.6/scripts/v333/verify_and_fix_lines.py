#!/usr/bin/env python3
"""
行号自动校准 v3.33（直调模式验证兜底，ADR-0001）

LLM 直调生成的行号偶有偏差（实测约 1/3 金句错位，区间/短语引用约 6% 章外）。
两类校准：
  1. 金句引用 `"原文"（行NNN，简述）`：
     取引号内原文做去标点归一化，在对应段文件中定位真实行号 → 修正
     找不到原文 → 保留原标注并标记「行号待核」
  2. 区间/短语引用 `短语（行N）` / `短语（行N~M）`（v3.33 新增）：
     取括号前短语做文本指纹回查段文件；**唯一命中**且修正后不越界 → 修正
     （区间保持原长度）；未命中 → 标记「区间待核」；多命中/模糊 → 跳过不改

配置：INDEX_LIB_DIR（.env）或 --lib 参数。
用法:
  python verify_and_fix_lines.py "<索引文件或目录>"... [--lib <索引库根目录>]
"""
import argparse
import glob
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/v333/ → skill 根
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(SKILL_DIR, ".env"))
except Exception:
    pass

INDEX_LIB_DIR = os.environ.get("INDEX_LIB_DIR", "").strip() or os.path.join(SKILL_DIR, "索引库")
SEG_DIR = os.path.join(INDEX_LIB_DIR, "小说原文")

# 行号标注模式: "原文"（行NNN，简述）；逗号可省略（兼容 LLM 输出变体 "原文"（行N））
PATTERN = re.compile(r'("([^"\n]{8,60})")（行\s*(\d+)(?:\s*[,，]([^）]*))?）')
# v3.33 区间/短语引用模式: 短语（行N）或 短语（行N~M）；尾随（,、分隔的多行号等）单列为组4
RANGE_PATTERN = re.compile(
    r"([^\s（）：\n，。；、]{2,20}?)（行\s*(\d+)(?:\s*[~～\-—]\s*(\d+))?(?:\s*[,，、]([^）]*))?）"
)


def norm(s):
    """去标点/空白归一化（中文引号、英文引号、标点差异均不影响匹配）"""
    return re.sub(r"[\W_]+", "", s)


def find_real_line(seg_lines, quote, claimed):
    probe = norm(quote[:15])
    if not probe:
        return None
    hits = [i for i, line in enumerate(seg_lines, 1) if probe in norm(line)]
    if not hits:
        return None
    return min(hits, key=lambda n: abs(n - claimed))


def process_file(idx_path, args=None):
    seg_name = os.path.basename(idx_path).replace("-idx.md", "")
    # --lib 优先（修复 v3.32 及之前 --lib 不生效的问题）
    lib = (args.lib if args and getattr(args, "lib", None) else INDEX_LIB_DIR)
    seg_dir = os.path.join(lib, "小说原文")
    seg_candidates = glob.glob(os.path.join(seg_dir, "**", seg_name + ".md"), recursive=True)
    if not seg_candidates:
        print(f"⚠️ {os.path.basename(idx_path)}: 找不到对应段文件 {seg_name}")
        return 0, 0
    with open(seg_candidates[0], encoding="utf-8") as f:
        seg_lines = f.read().splitlines()
    with open(idx_path, encoding="utf-8") as f:
        content = f.read()

    fixed, kept, missing = 0, 0, 0

    def repl(m):
        nonlocal fixed, kept, missing
        quote, claimed = m.group(2), int(m.group(3))
        tail = f"，{m.group(4)}" if m.group(4) else ""
        real = find_real_line(seg_lines, quote, claimed)
        if real is None:
            missing += 1
            return f'"{m.group(2)}"（行{m.group(3)}{tail}[行号待核]）'
        if real != claimed:
            fixed += 1
            return f'"{m.group(2)}"（行{real}{tail}）'
        kept += 1
        return m.group(0)

    new_content = PATTERN.sub(repl, content)
    rf, rm, rs = 0, 0, 0
    if not (args and getattr(args, "no_range", False)):
        new_content, rf, rm, rs = fix_range_refs(seg_lines, new_content)
    if new_content != content:
        with open(idx_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    print(f"  {os.path.basename(idx_path)}: 修正 {fixed} 条, 正确 {kept} 条, 待核 {missing} 条"
          f" | 区间: 修正 {rf} 待核 {rm} 跳过 {rs}")
    return fixed, missing


def fix_range_refs(seg_lines, content, max_lines=None):
    """v3.33 区间/短语引用校准：短语（行N~M）→ 短语文本指纹回查段文件。
    原则：只做高置信修正，不污染原文——未命中/多命中一律保持原样（不标记）。
    规则：短语回查**唯一命中**且修正后不越界 → 修正（区间保持原长度）；
    短语太短（<6）或含引号（金句格式已处理）→ 跳过；
    多行号引用（行N、行M 顿号/逗号分隔）→ 跳过。
    返回 (new_content, fixed, missing, skipped)，missing 仅统计不改写。"""
    if max_lines is None:
        max_lines = len(seg_lines)
    fixed = missing = skipped = 0

    def repl(m):
        nonlocal fixed, missing, skipped
        phrase, n1, n2 = m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None
        # 金句格式（引号开头）已被 PATTERN 处理，跳过；短语太短不可靠
        if '"' in phrase or "“" in phrase:
            skipped += 1
            return m.group(0)
        # 多行号引用（行N、行M）跳过
        tail = m.group(4) or ""
        if re.search(r"行\s*\d+", tail):
            skipped += 1
            return m.group(0)
        probe = norm(phrase)
        if len(probe) < 6:
            skipped += 1
            return m.group(0)
        hits = [i for i, line in enumerate(seg_lines, 1) if probe in norm(line)]
        if len(hits) != 1:
            if not hits:
                missing += 1  # 未命中：保持原样，仅统计
            else:
                skipped += 1  # 模糊匹配，不自动改
            return m.group(0)
        real = hits[0]
        if real != n1:
            # 修正：区间保持原长度；修正后必须不越界
            end = (real + (n2 - n1)) if n2 else real
            if end <= max_lines:
                fixed += 1
                rng = f"行{real}~{end}" if n2 else f"行{real}"
                return f"{m.group(1)}（{rng}{m.group(4) or ''}）"
            skipped += 1
        return m.group(0)

    new_content = RANGE_PATTERN.sub(repl, content)
    return new_content, fixed, missing, skipped


# ---- v3.32 新增：金句引用一致性检查（P0-4，模板「引用一致性规则」的自动验证） ----
# 其他小节（剧情概要/爽点/伏笔）引用了行号原文 → 金句小节必须收录该行号。
# 只报告，不自动改（引用存在性检查，防「引用了却不收金句」复发）。
HEAD_RE = re.compile(r"^###\s+(.+)$")
RANGE_RE = re.compile(r"（行\s*(\d+)\s*[~～\-—]\s*(\d+)")
CH_RANGE_RE = re.compile(r"（行号：\s*(\d+)\s*[~～\-—]\s*(\d+)")


def check_consistency(idx_path):
    """金句引用一致性：返回缺失行号数量（0 = 通过）。"""
    with open(idx_path, encoding="utf-8") as f:
        content = f.read()
    sections, cur_title, cur = [], None, []
    for line in content.splitlines():
        m = HEAD_RE.match(line)
        if m:
            if cur_title is not None:
                sections.append((cur_title, "\n".join(cur)))
            cur_title, cur = m.group(1), []
        elif cur_title is not None:
            cur.append(line)
    if cur_title is not None:
        sections.append((cur_title, "\n".join(cur)))

    quote_secs = [s for t, s in sections if "金句" in t or "名场面" in t]
    other_secs = [(t, s) for t, s in sections if not ("金句" in t or "名场面" in t)]
    quote_nums = {int(m.group(3)) for s in quote_secs for m in PATTERN.finditer(s)}
    missing = sorted({
        int(m.group(3)) for t, s in other_secs for m in PATTERN.finditer(s)
        if int(m.group(3)) not in quote_nums
    })
    if missing:
        print(f"  ⚠️ [{os.path.basename(idx_path)}] 引用一致性: {len(missing)} 处引用未进金句小节（行号 {missing}）")
        return len(missing)
    return 0


def check_ranges(idx_path):
    """P1-6 check-only：章边界连续性 + 剧情概要块区间重叠检测（防缺陷 3 复发，不自动改）。
    只检测「剧情概要」小节内的相邻块区间——爽点/伏笔的引用式区间（行N~M，简述）不参与。"""
    with open(idx_path, encoding="utf-8") as f:
        content = f.read()
    issues = []
    ch_ranges = [(int(m.group(1)), int(m.group(2))) for m in CH_RANGE_RE.finditer(content)]
    for a, b in zip(ch_ranges, ch_ranges[1:]):
        if a[1] + 1 != b[0]:
            issues.append(f"章边界不连续: ({a[0]}~{a[1]}) → ({b[0]}~{b[1]})")

    # 按小节分割，只取「剧情概要」小节
    sections, cur_title, cur = [], None, []
    for line in content.splitlines():
        m = HEAD_RE.match(line)
        if m:
            if cur_title is not None:
                sections.append((cur_title, "\n".join(cur)))
            cur_title, cur = m.group(1), []
        elif cur_title is not None:
            cur.append(line)
    if cur_title is not None:
        sections.append((cur_title, "\n".join(cur)))
    for title, sec in sections:
        if "剧情概要" not in title:
            continue
        ranges = [(int(m.group(1)), int(m.group(2))) for m in RANGE_RE.finditer(sec)]
        for a, b in zip(ranges, ranges[1:]):
            if b[0] <= a[1]:
                issues.append(f"概要区间重叠（{title}）: ({a[0]}~{a[1]}) ∩ ({b[0]}~{b[1]})")

    if issues:
        print(f"  ⚠️ [{os.path.basename(idx_path)}] 区间检查 {len(issues)} 项（check-only，不自动改）:")
        for i in issues[:10]:
            print(f"      - {i}")
    return len(issues)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="+", help="索引文件或目录")
    parser.add_argument("--lib", default=INDEX_LIB_DIR, help="索引库根目录")
    parser.add_argument("--no-check", action="store_true", help="跳过一致性/区间检查（只做行号校准）")
    parser.add_argument("--no-range", action="store_true", help="跳过区间/短语引用校准（只做金句校准）")
    args = parser.parse_args()

    targets = []
    for a in args.targets:
        if os.path.isdir(a):
            targets.extend(glob.glob(os.path.join(a, "*-idx.md")))
        else:
            targets.append(a)

    total_fixed = total_missing = total_consistency = total_ranges = 0
    for t in sorted(set(targets)):
        f, m = process_file(t, args)
        total_fixed += f
        total_missing += m
        if not args.no_check:
            total_consistency += check_consistency(t)
            total_ranges += check_ranges(t)
    print(f"\n📊 校准完成: 共修正 {total_fixed} 条行号, 待人工核 {total_missing} 条")
    if not args.no_check:
        print(f"🔍 检查完成: 引用一致性 {total_consistency} 处缺失, 区间问题 {total_ranges} 项（均为 check-only）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
