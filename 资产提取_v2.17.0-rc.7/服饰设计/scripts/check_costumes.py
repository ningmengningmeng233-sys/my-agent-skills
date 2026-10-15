#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""短剧服装设计 · 校验脚本（只报不补）

机械检查角色服饰标准产物，输出阻断/警告分级报告。
用法:
    python check_costumes.py <服饰标准库.md> <角色服饰标准表.md>
检查项:
    [阻断] 黑名单词扫描   —— 角色条目出现剧种黑名单/禁忌清单元素
    [警告] 配色板内检查   —— 角色配色十六进制色值是否全部在标准库配色板内
    [警告] 头身比存在     —— 角色条目含数值化头身比（"X 头身"）
    [警告] 主色撞车检测   —— 多角色主色十六进制色值重复
    [警告] 三视图角度声明 —— 生图提示词含正面/侧面/背面三个角度
退出码: 0 = 无阻断项；1 = 存在阻断项（有阻断必须修复后重跑）
依赖: 仅 Python 3 标准库。
"""

import re
import sys


BACKGROUND_HEX = {"B8B8B8", "DCDCDC", "7F7F7F"}


def parse_hex(text):
    """提取文本中全部 hex 色值。"""
    return set(m.group(1).upper() for m in re.finditer(r'#([0-9A-Fa-f]{6})\b', text))


def load_standard_lib(path):
    """从标准库提取：配色板 hex 集合 + 黑名单词表（含用户额外禁忌）。"""
    text = open(path, encoding="utf-8").read()
    palette = parse_hex(text)
    blacklist = set()
    # 匹配"表头…：内容"行（表头与冒号之间允许括号标注如"黑名单并集（民国 ∪ 悬疑）"，冒号必需，排除"## 禁忌清单（红线）"这类无冒号标题行）
    for m in re.finditer(r"(?:剧种黑名单|黑名单|禁忌清单|用户额外禁忌)[^：:\n]*[：:]\s*([^\n]+)", text):
        for item in re.split(r"[、，,；;（）()\n]", m.group(1)):
            item = item.strip().strip("。")
            if len(item) >= 2 and "红线" not in item and "阻断" not in item:
                blacklist.add(item)
    return palette, blacklist


def split_roles(text):
    """按 '## 角色 ...' 标题分段，返回 [(角色名, 段落文本)]。"""
    heads = list(re.finditer(r"^##\s+角色[^\n]*$", text, re.M))
    if not heads:
        return [("整文", text)]
    roles = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        roles.append((h.group(0).replace("##", "").strip(), text[h.start():end]))
    return roles


def main_color_hex(role_text):
    """提取角色主色十六进制色值（兼容'主XX `#HEX`'与'主色：XX #HEX'），无则返回 None。"""
    m = re.search(r"主(?:色)?[^#，,；;\n]*#([0-9A-Fa-f]{6})", role_text)
    return m.group(1).upper() if m else None


def check_role(name, text, palette, blacklist):
    """对单个角色段落执行全部检查，返回 (severity, 描述) 列表。"""
    findings = []
    # [阻断] 黑名单词扫描（剔除"严禁/禁止/不得"引导的禁止声明，避免把规范文本当命中）
    # 修复：去掉「避免」——「他避免不了这身机甲」会被剥离从句吞掉黑名单词导致漏检
    scanned = re.sub(r"(?:严禁|禁止|不得(?:出现|使用)?)[^。，；]*", "", text)
    hits = [w for w in blacklist if w in scanned]
    if hits:
        findings.append(("阻断", f"黑名单命中：{'、'.join(hits)}"))
    # [警告] 配色板内（统一设定图背景色豁免）
    out = parse_hex(text) - palette - BACKGROUND_HEX
    if out:
        findings.append(("警告", f"配色出界（不在标准库配色板内）：{'、'.join('#' + c for c in sorted(out))}"))
    # [警告] 头身比
    if not re.search(r"\d+(?:\.\d+)?\s*头身", text):
        findings.append(("警告", "头身比缺失（需数值化，如'6.5 头身'）"))
    # [警告] 三视图角度
    if not (("正面" in text) and ("侧面" in text) and ("背面" in text)):
        findings.append(("警告", "生图提示词未逐项声明正面/侧面/背面三个角度"))
    return findings


def main():
    # 兼容 Windows GBK 控制台：强制 UTF-8 输出
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 3:
        print("用法: python check_costumes.py <服饰标准库.md> <角色服饰标准表.md>")
        return 2
    lib_path, roles_path = sys.argv[1], sys.argv[2]
    palette, blacklist = load_standard_lib(lib_path)
    roles_text = open(roles_path, encoding="utf-8").read()
    roles = split_roles(roles_text)

    blockers, warnings = [], []
    main_colors = {}
    for name, text in roles:
        findings = check_role(name, text, palette, blacklist)
        for sev, desc in findings:
            (blockers if sev == "阻断" else warnings).append((name, desc))
        mc = main_color_hex(text)
        if mc:
            main_colors.setdefault(mc, []).append(name)

    # [警告] 主色撞车
    for color, names in main_colors.items():
        if len(names) > 1:
            warnings.append(("（全表）", f"主色撞车：{'、'.join(names)} 共用主色 #{color}"))

    print("=" * 48)
    print(f"校验报告（机械检查）  标准库: {lib_path}")
    print(f"配色板 {len(palette)} 色 | 黑名单 {len(blacklist)} 词 | 角色 {len(roles)} 个")
    print("=" * 48)
    print(f"\n[阻断] {len(blockers)} 项")
    for name, desc in blockers:
        print(f"  ✗ {name}: {desc}")
    print(f"\n[警告] {len(warnings)} 项")
    for name, desc in warnings:
        print(f"  ⚠ {name}: {desc}")
    print("\n" + "=" * 48)
    if blockers:
        print("结论: 存在阻断项，禁止交付（须修复后重跑本脚本）")
        return 1
    if warnings:
        print("结论: 无阻断项，可交付（警告项建议修复）")
        return 0
    print("结论: 全部通过（0 阻断 0 警告）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
