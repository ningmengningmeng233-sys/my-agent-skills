# -*- coding: utf-8 -*-
r"""
build_card_index.py — 风格卡库索引生成器 + 覆盖率盘点 + 体检 + 缺口清单 + 审计日志
扫描卡库目录下所有 *-风格卡.md，解析 frontmatter：
1. 生成"场景→推荐卡"索引.md（含卡清单/验证状态/卡源）
2. 覆盖率盘点：基于"场景强项"统计每场景候选卡数（≥2 健康 / 1 缺口 / 0 严重缺口）
3. 体检：卡清单一致性 / frontmatter 字段完整性 / 验证状态一致性
4. 缺口清单：候选卡 ≤1 的场景写入缺口清单.md
5. 审计日志：每次运行写 audit_log.md（时间戳+卡数+缺口快照）——配置表必须引用日志编号

用法:
  python build_card_index.py [卡库目录] [输出目录]
  默认: 卡库目录=<skill>/索引库/文风蒸馏卡, 输出=同目录
"""
import os
import re
import sys
from datetime import date, datetime

_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LIB = os.path.join(_SKILL_ROOT, "索引库", "文风蒸馏卡")
SCENES = ["打脸碾压", "升级突破", "战斗对决", "围观震撼",
          "金句霸气", "复仇归来", "心境意境", "日常过渡",
          "身份反转", "装弱扮猪", "危机化解", "智斗布局",
          "感情线", "搞笑吐槽", "恐怖悬疑"]
REQUIRED_FIELDS = ["书名", "作者", "验证状态", "卡源"]

def parse_frontmatter(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    meta = {}
    if not m:
        return {"_raw": text[:200], "_missing": REQUIRED_FIELDS}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    meta["_missing"] = [f for f in REQUIRED_FIELDS if f not in meta]
    return meta

def parse_scene_strengths(meta):
    """解析场景强项字段（如 '打脸碾压:A 围观震撼:B'）→ {场景: 等级}"""
    out = {}
    raw = meta.get("场景强项", "")
    for part in raw.split():
        if ":" in part:
            scene, lvl = part.split(":", 1)
            out[scene.strip()] = lvl.strip()
    return out

def main():
    lib = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LIB
    out_dir = sys.argv[2] if len(sys.argv) > 2 else lib
    cards = []
    for fn in sorted(os.listdir(lib)):
        if fn.endswith("-风格卡.md") or fn.endswith("-场景卡.md"):
            meta = parse_frontmatter(os.path.join(lib, fn))
            strengths = parse_scene_strengths(meta)
            # 场景专卡：frontmatter 无场景强项，用「场景」字段登记 A 级（2026-08-17 增）
            if not strengths and meta.get("场景"):
                strengths = {meta["场景"].strip(): "A"}
            cards.append({"file": fn, "meta": meta, "strengths": strengths})

    # ============ 1. 索引.md ============
    lines = []
    lines.append("# 风格卡库索引\n")
    lines.append(f"> 自动生成：{date.today().isoformat()} ｜ 共 {len(cards)} 张卡 ｜ 由 build_card_index.py 维护，勿手改\n")
    lines.append("## 卡清单\n")
    lines.append("| 书名 | 作者 | 验证状态 | 锚点验证 | 卡源 | 场景强项 |")
    lines.append("|------|------|----------|----------|------|----------|")
    for c in cards:
        m = c["meta"]
        flag = "✅ 已验证" if m.get("验证状态") == "已验证" else "⚠️ 未验证"
        anc = m.get("锚点验证", "待验证")
        src = m.get("卡源", "?")
        s_disp = " ".join(f"{k}:{v}" for k, v in c["strengths"].items()) if c["strengths"] else "未标注（待判官验证）"
        lines.append(f"| {m.get('书名', '?')} | {m.get('作者', '?')} | {flag} | {anc} | {src} | {s_disp} |")

    lines.append("\n## 场景→推荐卡矩阵\n")
    lines.append("> 首推 = 该场景最强（判官验证结果）；备选 = 次选；不适用 = 需转换的卡。\n")
    for s in SCENES:
        lines.append(f"### {s}\n")
        strong = [c["file"] for c in cards if c["strengths"].get(s) in ("A", "A+", "S")]
        mid = [c["file"] for c in cards if c["strengths"].get(s) in ("B", "B+")]
        lines.append(f"- 首推（A级）: {('、'.join(strong)) if strong else '（待判官验证后填写）'}")
        lines.append(f"- 备选（B级）: {('、'.join(mid)) if mid else '（待判官验证后填写）'}")
        lines.append("- 不适用: （待判官验证后填写）")
        lines.append("")

    # ============ 2. 覆盖率盘点 ============
    lines.append("## 覆盖率盘点\n")
    lines.append("| 场景 | 候选卡数 | 状态 |")
    lines.append("|------|----------|------|")
    coverage = {}
    for s in SCENES:
        n = sum(1 for c in cards if s in c["strengths"] or s in c["meta"].get("场景强项", ""))
        coverage[s] = n
        st = "✅ 健康" if n >= 2 else ("⚠️ 缺口" if n == 1 else "❌ 严重缺口")
        lines.append(f"| {s} | {n} | {st} |")
    lines.append("")

    # ============ 3. 体检 ============
    lines.append("## 体检\n")
    problems = []
    for c in cards:
        if c["meta"].get("_missing"):
            problems.append(f"字段缺失: {c['file']}（缺 {','.join(c['meta']['_missing'])}）")
    for c in cards:
        m = c["meta"]
        if m.get("验证状态") == "已验证" and m.get("锚点验证") in ("失败", "待验证"):
            problems.append(f"状态矛盾: {c['file']} 已验证但锚点验证={m.get('锚点验证')}")
    lines.append(f"- 卡文件数: {len(cards)}；索引登记: {len(cards)}（索引由本脚本生成，一致性由脚本保证）")
    if problems:
        for p in problems:
            lines.append(f"- ⚠️ {p}")
    else:
        lines.append("- 体检通过：无字段缺失、无状态矛盾")
    lines.append("")

    # ============ 4. 缺口清单 ============
    gaps = [s for s, n in coverage.items() if n <= 1]
    lines.append("## 缺口清单\n")
    if gaps:
        lines.append("以下场景候选卡不足（0 张=严重缺口；1 张=有 A 级卡但缺备选双卡）：\n")
        for s in gaps:
            n = coverage[s]
            if n == 0:
                lines.append(f"- **{s}**: 候选卡 0 张（无卡可用）→ 需蒸馏至少 1 张")
            else:
                lines.append(f"- **{s}**: 候选卡 1 张（已有 A 级卡，缺备选）→ 可选补 1 张制衡")
    else:
        lines.append("当前无缺口（每场景候选卡 ≥2）。\n")
    lines.append("")

    # ============ 5. 审计日志 ============
    now = datetime.now().isoformat(timespec="seconds")
    gap_n = len(gaps)
    log_line = f"- {now} ｜ 卡 {len(cards)} 张 ｜ 缺口 {gap_n} 个（{'、'.join(gaps) if gaps else '无'}）"
    log_path = os.path.join(out_dir, "audit_log.md")
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            old = f.read()
    except Exception:
        old = "# 卡库审计日志\n\n> 由 build_card_index.py 自动追加；配置表须引用最近日志编号\n"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(old.rstrip() + "\n" + log_line + "\n")

    with open(os.path.join(out_dir, "索引.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(os.path.join(out_dir, "缺口清单.md"), "w", encoding="utf-8") as f:
        f.write("# 卡库缺口清单\n\n> 由 build_card_index.py 自动生成。缺口攒够 3-5 个 → 用户确认 → 批量蒸馏。\n\n")
        f.write("\n".join(f"- {s}: 候选卡 {coverage[s]} 张" for s in gaps) if gaps else "当前无缺口。\n")

    print(f"索引/盘点/体检/缺口清单已生成：{out_dir}（{len(cards)} 张卡，缺口 {gap_n} 个）")

if __name__ == "__main__":
    main()
