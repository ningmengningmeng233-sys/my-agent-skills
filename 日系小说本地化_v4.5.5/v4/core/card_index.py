# -*- coding: utf-8 -*-
"""卡库索引生成器：解析文风卡/场景卡 frontmatter，生成索引/覆盖率/缺口清单。

迁移自 scripts/build_card_index.py，作为 v4 `card index` 的内部实现。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from .jsonio import write_text_atomic

SCENES = [
    "打脸碾压", "升级突破", "战斗对决", "围观震撼",
    "金句霸气", "复仇归来", "心境意境", "日常过渡",
    "身份反转", "装弱扮猪", "危机化解", "智斗布局",
    "感情线", "搞笑吐槽", "恐怖悬疑",
]
REQUIRED_FIELDS = ["书名", "作者", "验证状态", "卡源"]


def parse_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return {"_raw": text[:200], "_missing": REQUIRED_FIELDS}
    meta: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    meta["_missing"] = [f for f in REQUIRED_FIELDS if f not in meta]
    return meta


def parse_scene_strengths(meta: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    raw = meta.get("场景强项", "")
    for part in str(raw).split():
        if ":" in part:
            scene, lvl = part.split(":", 1)
            out[scene.strip()] = lvl.strip()
    return out


def scan_cards(lib: Path) -> list[dict[str, Any]]:
    card_dir = lib / "文风蒸馏卡"
    cards: list[dict[str, Any]] = []
    if not card_dir.is_dir():
        return cards
    for fn in sorted(card_dir.iterdir()):
        if fn.suffix != ".md":
            continue
        if not (fn.name.endswith("-风格卡.md") or fn.name.endswith("-场景卡.md")):
            continue
        meta = parse_frontmatter(fn)
        strengths = parse_scene_strengths(meta)
        if not strengths and meta.get("场景"):
            strengths = {str(meta["场景"]).strip(): "A"}
        cards.append({"file": fn.name, "meta": meta, "strengths": strengths})
    return cards


def build_card_index_report(lib: Path) -> dict[str, Any]:
    cards = scan_cards(lib)
    coverage: dict[str, int] = {}
    for scene in SCENES:
        coverage[scene] = sum(
            1 for c in cards
            if scene in c["strengths"] or scene in str(c["meta"].get("场景强项", ""))
        )
    problems: list[str] = []
    for c in cards:
        if c["meta"].get("_missing"):
            problems.append(f"字段缺失: {c['file']}（缺 {','.join(c['meta']['_missing'])}）")
        m = c["meta"]
        if m.get("验证状态") == "已验证" and m.get("锚点验证") in ("失败", "待验证"):
            problems.append(f"状态矛盾: {c['file']} 已验证但锚点验证={m.get('锚点验证')}")
    return {
        "generated_at": date.today().isoformat(),
        "card_count": len(cards),
        "cards": cards,
        "coverage": coverage,
        "problems": problems,
    }


def write_card_index(lib: Path) -> None:
    report = build_card_index_report(lib)
    card_dir = lib / "文风蒸馏卡"
    card_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# 风格卡库索引\n")
    lines.append(f"> 自动生成：{report['generated_at']} ｜ 共 {report['card_count']} 张卡 ｜ 由 v4 card index 维护\n")
    lines.append("## 卡清单\n")
    lines.append("| 书名 | 作者 | 验证状态 | 锚点验证 | 卡源 | 场景强项 |")
    lines.append("|------|------|----------|----------|------|----------|")
    for c in report["cards"]:
        m = c["meta"]
        flag = "✅ 已验证" if m.get("验证状态") == "已验证" else "⚠️ 未验证"
        s_disp = " ".join(f"{k}:{v}" for k, v in c["strengths"].items()) if c["strengths"] else "未标注"
        lines.append(f"| {m.get('书名', '?')} | {m.get('作者', '?')} | {flag} | {m.get('锚点验证', '待验证')} | {m.get('卡源', '?')} | {s_disp} |")
    lines.append("\n## 场景→推荐卡矩阵\n")
    for scene in SCENES:
        strong = [c["file"] for c in report["cards"] if c["strengths"].get(scene) in ("A", "A+", "S")]
        mid = [c["file"] for c in report["cards"] if c["strengths"].get(scene) in ("B", "B+")]
        lines.append(f"### {scene}\n")
        lines.append(f"- 首推（A级）: {'、'.join(strong) if strong else '（待判官验证后填写）'}")
        lines.append(f"- 备选（B级）: {'、'.join(mid) if mid else '（待判官验证后填写）'}")
        lines.append("")
    lines.append("## 覆盖率盘点\n")
    lines.append("| 场景 | 候选卡数 | 状态 |")
    lines.append("|------|----------|------|")
    for scene in SCENES:
        n = report["coverage"].get(scene, 0)
        st = "✅ 健康" if n >= 2 else ("⚠️ 缺口" if n == 1 else "❌ 严重缺口")
        lines.append(f"| {scene} | {n} | {st} |")
    lines.append("\n## 体检\n")
    if report["problems"]:
        for p in report["problems"]:
            lines.append(f"- ⚠️ {p}")
    else:
        lines.append("- 体检通过：无字段缺失、无状态矛盾")
    lines.append("")
    write_text_atomic(card_dir / "索引.md", "\n".join(lines))

    gap_lines = ["# 缺口清单\n"]
    for scene in SCENES:
        n = report["coverage"].get(scene, 0)
        if n <= 1:
            gap_lines.append(f"- {scene}: 候选卡 {n} 张，需补卡")
    write_text_atomic(card_dir / "缺口清单.md", "\n".join(gap_lines) + "\n")
