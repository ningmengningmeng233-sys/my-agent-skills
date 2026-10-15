#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 references/样本库/SOURCE-NOTES.md：逐文件登记 书名/作者/来源/用途/分发限制。

用法：python scripts/make_source_notes.py [--check]
  --check  只校验覆盖度（现有 SOURCE-NOTES 是否覆盖全部样本文件），不重写
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "references" / "样本库"
OUT = SAMPLES / "SOURCE-NOTES.md"

HEADER = """# 样本库来源说明（SOURCE-NOTES）

本目录存放**内部研究副本**：从已出版中文网文中摘取的分段文本，仅用于本技能的风格蒸馏与规则校准。
**版权归原作者与出版方所有；本目录正文不随任何发布包分发**（发布走 `scripts/make_release.py`，默认剔除）。

- 用途：内部研究（风格取样、规则校准、蒸馏卡的依据）
- 获取方式：本地已有素材库摘录（非购买、非转载、非公开分发）
- 分发限制：**不随包分发、不上传、不外发**；发布包内仅保留本说明与蒸馏产物（`索引库/文风蒸馏卡/`）
- 覆盖度校验：本表行数必须等于 `references/样本库/*.md`（不含本文件）的文件数

| # | 文件 | 书名 | 作者 | 来源 | 用途 | 分发 |
|---|------|------|------|------|------|------|
"""

FOOTER = """
## 发布剔除规则

`scripts/make_release.py`（默认 `--exclude-samples`）：

1. 发布目录**不包含**本目录的任何样本正文（仅保留本 `SOURCE-NOTES.md`）；
2. 保留蒸馏产物：`索引库/文风蒸馏卡/`、`references/文风蒸馏/`、`references/示范库/`；
3. 规则文件中对书名的**出处标注**（如「参考《仙逆》的打脸节奏」）保留——那是引用标注，不是正文复制；
4. 打包后自动跑正文泄漏校验：逐样本取正文指纹串在发布目录全文搜索，命中必须为 0（`SAMPLE_BODY_LEAK=0`）。
"""


def rows():
    files = sorted(p for p in SAMPLES.glob("*.md") if p.name != OUT.name)
    out = []
    for i, p in enumerate(files, 1):
        stem = p.stem
        if "-" in stem:
            book, author = stem.rsplit("-", 1)
        else:
            book, author = stem, "（未标注）"
        out.append((i, p.name, book, author, p.stat().st_size))
    return out


def main(check=False):
    rs = rows()
    if check:
        if not OUT.exists():
            print("SOURCE-NOTES.md 不存在")
            return 2
        t = OUT.read_text(encoding="utf-8")
        missing = [r[1] for r in rs if r[1] not in t]
        print(f"样本文件数={len(rs)} 未登记={len(missing)}")
        print("COVERAGE_OK" if not missing else "COVERAGE_FAIL " + str(missing))
        return 0 if not missing else 1
    body = HEADER
    for i, name, book, author, size in rs:
        body += f"| {i} | `{name}` | 《{book}》 | {author} | 本地素材库摘录（内部研究副本） | 风格蒸馏与规则校准 | **不随包分发** |\n"
    body += f"\n> 合计 {len(rs)} 份，覆盖本目录全部样本文件（{len(rs)}/{len(rs)}）。\n"
    body += FOOTER
    OUT.write_text(body, encoding="utf-8")
    print(f"已写入 {OUT.relative_to(ROOT)}（{len(rs)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
