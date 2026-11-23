# -*- coding: utf-8 -*-
"""转录索引 · 术语表清洗（2026-08-14 污染治理）

背景：切条目自动沉淀的过滤规则「源词含 ASCII 即收」太宽松，916 行术语表被 LLM 劣质建议污染
（中文同音错字 751 条、危险多义词替换 Cloud→Claude/template→temperature 等）。

判定规则（与 common.is_acceptable_term 同一套，后续自动沉淀也用它）：
1. 中文源/目标词：仅人工白名单（洛拉/过你河/欠你河/1万部/2000到3000部）
2. 英文：源词 ≥4 字母、目标词 ≥4 字母、目标词含大写或数字（专名形态）
3. 通用词黑名单（cloud/scale/test/...）一律拒绝——多义词全局替换会误伤其它语境

用法: python _清洗术语表.py [--dry-run]
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import TERM_TABLE, is_acceptable_term

def main():
    dry = "--dry-run" in sys.argv
    lines = TERM_TABLE.read_text(encoding="utf-8").splitlines()
    keep, drop, reasons = [], [], {}
    for l in lines:
        if not l.strip() or l.startswith(("#", ">", "##")):
            keep.append(l)
            continue
        if "→" not in l:
            keep.append(l)
            continue
        w, r = [x.strip() for x in l.split("→", 1)]
        ok, why = is_acceptable_term(w, r)
        if ok:
            keep.append(f"{w} → {r}")
        else:
            drop.append(l)
            reasons[why] = reasons.get(why, 0) + 1
    print(f"原 {len(lines)} 行 → 保留 {len(keep)} 行 → 删除 {len(drop)} 条")
    for why, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  ✗ {why}: {n} 条")
    if drop:
        print("\n删除样本（各原因前 3 条）:")
        seen = {}
        for l in drop:
            w, r = [x.strip() for x in l.split("→", 1)]
            ok, why = is_acceptable_term(w, r)
            seen.setdefault(why, []).append(l)
        for why, items in list(seen.items())[:4]:
            print(f"  [{why}]")
            for l in items[:3]:
                print(f"    {l}")
    if not dry:
        TERM_TABLE.write_text("\n".join(keep), encoding="utf-8")
        print("\n已写回术语表 ✓")

if __name__ == "__main__":
    main()
