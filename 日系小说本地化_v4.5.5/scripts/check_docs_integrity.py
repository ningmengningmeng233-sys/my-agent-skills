#!/usr/bin/env python3
"""文档-资产一致性自检：防引用悬空（历史教训：18 份规则未挂接主流程）。

检查:
  1. SKILL.md 引用的 references/ 文件全部存在
  2. 改写维度规则索引引用的规则文件全部存在
  3. 必读文件表关键资产存在（示范库/文风蒸馏/平台审查规则/CHANGELOG）
  4. 流程锚点存在（写前计划区模板/新书提醒模块/版本字段）
  5. 跨文件锚点有效（references/docs 里「SKILL.md 阶段X步骤Y」「SKILL.md「小节」」
     必须命中 SKILL.md 现有标题——防主文档重写后指针集体悬空）

用法:
  python check_docs_integrity.py
退出码: 0=通过, 1=发现问题
"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SKILL = Path(__file__).resolve().parent.parent


def refs_in(text: str) -> list[str]:
    """提取文本中的 references/ 文件引用"""
    return list(dict.fromkeys(re.findall(r"references/[A-Za-z0-9_\-一-龥/]+\.md", text)))


def main() -> int:
    problems: list[str] = []
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    # 1. SKILL.md 引用存在性（排除 humanizer-zh 子模块——随 skill 分发）
    for ref in refs_in(skill):
        if not (SKILL / ref).exists():
            problems.append(f"SKILL.md 引用缺失: {ref}")

    # 2. 索引引用存在性（标注"待生成"的引用是预期缺口，跳过）
    idx = (SKILL / "references" / "改写维度规则索引.md")
    if idx.exists():
        idx_text = idx.read_text(encoding="utf-8")
        for ref in refs_in(idx_text):
            if (SKILL / ref).exists():
                continue
            # 引用所在行是否标了"待生成"
            line = next((l for l in idx_text.splitlines() if ref in l), "")
            if "待生成" in line:
                continue
            problems.append(f"规则索引引用缺失: {ref}")
    else:
        problems.append("改写维度规则索引.md 缺失")

    # 3. 关键资产存在性
    for asset in ["references/示范库/对白示范库.md",
                  "references/示范库/叙述者人格示范库.md",
                  "references/示范库/章末示范库.md",
                  "references/示范库/README.md",
                  "references/文风蒸馏/蒸馏方法论.md",
                  "references/文风蒸馏/判官验证规范.md",
                  "references/平台审查规则/红果-内容审查要求.md",
                  "references/去AI味-问题清单.md",
                  "references/副词密度基准.md",
                  "CHANGELOG.md"]:
        if not (SKILL / asset).exists():
            problems.append(f"关键资产缺失: {asset}")

    # 4. 流程锚点
    for anchor in ["## 当前已完成", "## 运行入口", "## 硬边界", "## 质量声明"]:
        if anchor not in skill:
            problems.append(f"SKILL.md 缺流程锚点: {anchor}")
    if not re.search(r'version: "\d+\.\d+\.\d+(?:[-+.]?[A-Za-z0-9.-]+)?"', skill):
        problems.append("SKILL.md 缺 version 字段")

    # 5. 跨文件指针锚点（P30：v4.5.1 重写 SKILL.md 后，references 里 8 处
    #    「见 SKILL.md 阶段三步骤 5」式指针全部悬空却无人拦——文件在、锚点没了）
    skill_headings = re.findall(r"^#{1,4}\s+(.+?)\s*$", skill, re.M)
    stale_anchor = re.compile(r"SKILL\.md\s*(?:第[一二三四五六七八九十]+阶段|阶段[一二三四五六七八九十]"
                              r"|步骤\s*\d|规则优先级)")
    for md in sorted((SKILL / "references").rglob("*.md")) + \
            sorted((SKILL / "docs").glob("*.md")):
        if "humanizer-zh" in md.parts:  # vendored 子技能，引用的是它自己的 SKILL.md
            continue
        text = md.read_text(encoding="utf-8", errors="ignore")
        rel = md.relative_to(SKILL)
        for lineno, line in enumerate(text.splitlines(), 1):
            if stale_anchor.search(line):
                problems.append(f"{rel}:{lineno} 指向已废弃的 SKILL.md 阶段/步骤编号")
        for sec in re.findall(r"SKILL\.md「([^」]+)」", text):
            if not any(sec in h for h in skill_headings):
                problems.append(f"{rel} 指向不存在的 SKILL.md 小节: 「{sec}」")

    if problems:
        print(f"❌ 发现 {len(problems)} 个问题:")
        for p in problems:
            print(f"   - {p}")
        return 1
    print("✅ 文档-资产一致性通过（引用无悬空、关键资产齐备、流程锚点存在、跨文件锚点有效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
