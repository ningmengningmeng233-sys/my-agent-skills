#!/usr/bin/env python3
"""Pre-aggregate segment products and run LLM adjudication into the final asset report."""
import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
sys.path.insert(0, str(SKILL_DIR))

from engine.common import parse_frontmatter
from engine.prompt_rules import PLACEHOLDER_NAMES  # noqa: F401 — 表内空值唯一集合（见 parse_verdict）
from engine.llm import call_llm, log, MAX_TOKENS
from engine.asset_schema import (
    AssetSchemaValidator,
    FactLevel,
    ProductionPriority,
    StableKeyAllocator,
)

INDEX_SCHEMA_VERSION = 1
PENDING_CONFLICT_FACT = FactLevel.PENDING_CONFLICT.value
SCRIPT_FACT = FactLevel.SCRIPT_FACT.value

ASSET_LIB_DIR = Path(os.environ.get("ASSET_LIB_DIR", "").strip() or SKILL_DIR / "资产库")
PROCESS_DIR = ".process"
VERDICT_DIR = "裁决"
# 对称化补出的反向边专用 note：与「声明方」的边区分开，便于审计谁是真声明、谁是补全
CONNECTS_TO_BACKFILL_NOTE = "场景连通（反向自动补全）"

# 三张表正则已抽为单一真相源（工单 T8）：解析口径只有这一份
from engine.segment_format import (SUMMARY_RE, RECORD_RE, NEED_RE,  # noqa: F401
                                   classify_row, parse_summary_rows)
if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PUNCT = "，。！？；：、（）《》「」『』“”‘’·—…,.!?;:()\"' -—\t"


def _canonical(obj):
    """Convert unhashable aggregates (sets, nested) into deterministic JSON-safe forms."""
    if isinstance(obj, set):
        return sorted(obj, key=lambda x: str(x))
    if isinstance(obj, dict):
        return {k: _canonical(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_canonical(x) for x in obj]
    return obj


def final_inputs_hash(novel, author, total_chapters, verdicts, info, pending, groups_by_id, summaries, needs):
    """Deterministic hash over all final-render inputs; used to skip unchanged writes."""
    data = {
        "novel": novel,
        "author": author,
        "total_chapters": total_chapters,
        "verdicts": verdicts,
        "info": info,
        "pending": pending,
        "summaries": summaries,
        "needs": needs,
        "groups": {gid: _canonical(groups_by_id[gid]) for gid in sorted(groups_by_id)},
    }
    s = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def norm_text(s):
    """Strip punctuation and whitespace for fuzzy matching."""
    if not s:
        return ""
    return re.sub(rf"[{re.escape(PUNCT)}]", "", str(s))


def parse_products(prod_dir: Path) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], list[str]]:
    """Parse segment products into summaries, records, change-needs and unparsed lines."""
    summaries, records, needs, unparsed = [], [], [], []
    header_hints = ("章节", "概要", "别名", "摘句", "类型", "名称", "行号", "备注", "服装", "需求")
    for path in sorted(Path(prod_dir).iterdir()):
        if path.suffix != ".md":
            continue
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            m = SUMMARY_RE.match(line)
            if m:
                summaries.append({
                    "段文件": path.name.replace("-资产记录.md", ".md"),
                    "章节": m.group(1).strip(),
                    "概要": m.group(2).strip(),
                    "人物": m.group(3).strip(),
                    "场景": m.group(4).strip(),
                    "道具": m.group(5).strip(),
                    "服装": m.group(6).strip(),
                })
                continue
            m = RECORD_RE.match(line)
            if m:
                records.append({
                    "段文件": path.name.replace("-资产记录.md", ".md"),
                    "类型": m.group(1).strip(),
                    "名称": m.group(2).strip(),
                    "别名": m.group(3).strip(),
                    "出现章节": m.group(4).strip(),
                    "摘句": m.group(5).strip(),
                    "行号": m.group(6).strip(),
                    "备注": m.group(7).strip(),
                })
                continue
            m = NEED_RE.match(line)
            if m:
                needs.append({
                    "段文件": path.name.replace("-资产记录.md", ".md"),
                    "类型": m.group(1).strip(),
                    "资产": m.group(2).strip(),
                    "需求": m.group(3).strip(),
                    "章节": m.group(4).strip(),
                    "摘句": m.group(5).strip(),
                    "行号": m.group(6).strip(),
                })
                continue
            if line.startswith("|") and not line.startswith("|---"):
                if not any(h in line[:30] for h in header_hints):
                    unparsed.append(f"{path.name}: {line[:60]}")
    return summaries, records, needs, unparsed


def load_original_texts(orig_dir: Path) -> dict[str, str]:
    """Load normalized segment originals for citation verification."""
    texts = {}
    for path in Path(orig_dir).iterdir():
        if path.suffix == ".md" and "资产记录" not in path.name:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            texts[path.name] = norm_text(body)
    return texts


def verify_citation(record: dict[str, str], texts: dict[str, str]) -> tuple[bool, str]:
    """Check that the quote appears (normalized) in the source segment text."""
    quote = norm_text(record["摘句"])
    if not quote or len(quote) < 2:
        return False, "摘句为空或过短"
    seg_text = texts.get(record["段文件"], "")
    if not seg_text:
        return False, f"未找到段原文 {record['段文件']}"
    if quote in seg_text:
        return True, "通过"
    for cut in (0.9, 0.8, 0.7):
        k = max(4, int(len(quote) * cut))
        if k < len(quote) and quote[:k] in seg_text:
            return True, "通过（前缀截断匹配）"
    return False, "摘句未在原文中找到"


def in_source(name: str, texts: dict[str, str]) -> bool:
    """Return whether a name occurs in any source text. Single-character names are treated as present."""
    n = norm_text(name)
    if len(n) < 2:
        return True
    return any(n in t for t in texts.values())


def _chapter_digits(ch: str) -> set[str]:
    """Extract chapter numbers from a chapter-list string."""
    return {
        c.replace("第", "").replace("章", "").replace("回", "").replace("话", "")
        for c in ch.replace("、", "/").split("/")
        if c.strip()
    }


def group_records(records: list[dict[str, str]], texts: dict[str, str]) -> list[dict]:
    """Group records by type + exact/included name and compute group statistics."""
    groups_by_type: dict[str, list[list[dict[str, str]]]] = defaultdict(list)
    by_exact: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for r in records:
        by_exact[(r["类型"], r["名称"])].append(r)

    exact_keys = list(by_exact.keys())
    merged: set[tuple[str, str]] = set()
    for i, (t, name) in enumerate(exact_keys):
        if (t, name) in merged:
            continue
        members = list(by_exact[(t, name)])
        for t2, name2 in exact_keys[i + 1 :]:
            if t2 != t:
                continue
            a, b = norm_text(name), norm_text(name2)
            if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
                members.extend(by_exact[(t2, name2)])
                merged.add((t2, name2))
        groups_by_type[t].append(members)

    groups = []
    for t, gs in groups_by_type.items():
        for members in gs:
            names: dict[str, int] = defaultdict(int)
            cites_ok = cites_bad = 0
            ch_set: set[str] = set()
            for r in members:
                names[r["名称"]] += 1
                if r["别名"]:
                    for al in r["别名"].replace("、", "/").split("/"):
                        names[al.strip()] += 1
                ch_set.update(_chapter_digits(r["出现章节"]))
                ok, why = verify_citation(r, texts)
                if ok:
                    cites_ok += 1
                else:
                    cites_bad += 1
                    r["校验失败"] = why
            ranked = sorted(names.items(), key=lambda kv: -kv[1])
            primary = ranked[0][0] if ranked else ""
            violates = [n for n, _ in ranked if not in_source(n, texts)]
            if not in_source(primary, texts):
                for alt, _ in ranked[1:]:
                    if in_source(alt, texts):
                        primary = alt
                        break
            groups.append({
                "类型": t,
                "成员": members,
                "名称频次": dict(ranked),
                "候选主名": primary,
                "违规名": violates,
                "出现章节集": ch_set,
                "出处校验": {"通过": cites_ok, "失败": cites_bad},
                "失败记录": [r for r in members if "校验失败" in r],
            })
    return groups


def backfill_summaries(summaries: list[dict], records: list[dict]) -> tuple[int, list[str]]:
    """ADR-0009 backfill: ensure every recorded asset appears in its chapter summary."""
    ch_map = {}
    for s in summaries:
        ch_map.setdefault(s["章节"], s)
    patched = 0
    details = []
    for r in records:
        typ = r["类型"]
        if typ not in ("人物", "场景", "道具", "服装"):
            continue
        names = [r["名称"]]
        if r["别名"]:
            names.extend(a.strip() for a in r["别名"].replace("、", "/").split("/") if a.strip())
        for ch in re.split(r"[、/，,]", r["出现章节"]):
            ch = ch.strip()
            if not ch:
                continue
            s = ch_map.get(ch)
            if not s:
                continue
            field_val = s.get(typ, "")
            field_names = {
                n.strip().replace("（脚本补入）", "")
                for n in field_val.replace("、", "/").split("/")
                if n.strip()
            }
            missing = [n for n in names if n not in field_names]
            if missing:
                name_to_add = missing[0]
                sep = "、" if field_val.strip() else ""
                s[typ] = field_val.strip() + sep + name_to_add + "（脚本补入）"
                patched += 1
                details.append(f"{ch} {typ}「{name_to_add}」")
    return patched, details


def cross_check_by_type(summaries: list[dict], groups: list[dict]) -> dict[str, tuple[list[str], list[str]]]:
    """Cross-check summary vs record names per asset type."""
    types = ("人物", "场景", "道具", "服装")
    rec_by_type = {t: set() for t in types}
    for g in groups:
        rec_by_type[g["类型"]].add(norm_text(g["候选主名"]))
        for r in g["成员"]:
            rec_by_type[g["类型"]].add(norm_text(r["名称"]))
            if r["别名"]:
                rec_by_type[g["类型"]].update(norm_text(a) for a in r["别名"].replace("、", "/").split("/"))
    sum_by_type = {t: set() for t in types}
    for s in summaries:
        for field, t in (("人物", "人物"), ("场景", "场景"), ("道具", "道具"), ("服装", "服装")):
            for n in s[field].replace("、", "/").split("/"):
                n = n.strip()
                if n and len(norm_text(n)) >= 2:
                    sum_by_type[t].add(norm_text(n))
    return {t: (sorted(sum_by_type[t] - rec_by_type[t]), sorted(rec_by_type[t] - sum_by_type[t])) for t in types}


def cross_check(summaries: list[dict], groups: list[dict]) -> tuple[list[str], list[str]]:
    """Flattened cross-check across all types."""
    by_type = cross_check_by_type(summaries, groups)
    missed = sorted(n for t in by_type.values() for n in t[0])
    orphans = sorted(n for t in by_type.values() for n in t[1])
    return missed, orphans


def format_positions(ch_set: set[str], total_chapters: int) -> str:
    """Format occurrence positions as full-run, interval, or per-chapter list."""
    chs = sorted(int(c) for c in ch_set if str(c).isdigit())
    if not chs:
        return ""
    if total_chapters and len(chs) == total_chapters:
        return "【全集出现】"
    ranges, start = [], chs[0]
    prev = chs[0]
    for c in chs[1:]:
        if c != prev + 1:
            ranges.append((start, prev))
            start = c
        prev = c
    ranges.append((start, prev))
    parts, singles = [], []
    for a, b in ranges:
        if a == b:
            singles.append(f"第{a}章")
        else:
            parts.append(f"第{a}章-第{b}章")
    return "【连续区间】" + "、".join(parts + singles) if parts else "【逐集列出】" + "、".join(singles)


def find_quote_overlaps(groups: list[dict]) -> list[tuple[str, str, str]]:
    """Detect overlapping quotes across groups (same type) as merge hints."""
    overlaps = []
    for i, g in enumerate(groups):
        for h in groups[i + 1 :]:
            if g["类型"] != h["类型"]:
                continue
            for r1 in g["成员"]:
                for r2 in h["成员"]:
                    q1, q2 = norm_text(r1["摘句"]), norm_text(r2["摘句"])
                    if len(q1) < 4 or len(q2) < 4:
                        continue
                    if q1 in q2 or q2 in q1:
                        overlaps.append((g["组ID"], h["组ID"], r1["摘句"] or r2["摘句"]))
                        break
                if overlaps and overlaps[-1][0] == g["组ID"] and overlaps[-1][1] == h["组ID"]:
                    break
    return overlaps


def build_chunk_input(
    chunk: str,
    novel: str,
    author: str,
    summaries: list[dict],
    groups: list[dict],
    by_type: dict[str, tuple[list[str], list[str]]],
    total_chapters: int,
    registry_lines: str | None = None,
) -> str:
    """Build per-type adjudication input (ADR-0002)."""
    lines = [
        f"# 聚合裁决输入（块: {chunk}）",
        f"小说: 《{novel}》 | 作者: {author} | 总章节: {total_chapters}",
        "",
    ]
    if registry_lines:
        lines.extend([
            "## 全局登记表锚点（ADR-0003，主名 = 原文词汇；合并/补建时优先采用登记表主名）",
            registry_lines,
            "",
        ])

    if chunk == "基础信息":
        lines.append("## 章节摘要（全部章节，用于项目基础信息与剧情概览）")
        lines.append("| 章节 | 概要 | 人物 | 场景 | 道具 | 服装 |")
        lines.append("|---|---|---|---|---|---|")
        for s in summaries:
            lines.append(f"| {s['章节']} | {s['概要']} | {s['人物']} | {s['场景']} | {s['道具']} | {s['服装']} |")
        lines.append("")
        lines.append("## 裁决要求")
        lines.append("输出【项目基础信息】表：项目名称/集数/题材类型/时代背景/核心设定/主线剧情(≤150字)/视觉风格。不输出资产裁决。")
        return "\n".join(lines)

    type_groups = [g for g in groups if g["类型"] == chunk]
    lines.append(f"## 预分组（{chunk}）")
    for g in type_groups:
        gid = g["组ID"]
        ok, bad = g["出处校验"]["通过"], g["出处校验"]["失败"]
        flags = []
        if bad > 0:
            flags.append(" ⚠️ 有出处校验失败记录")
        if g["违规名"]:
            flags.append(" ⚠️ 违规名（未在原文出现）: " + "/".join(g["违规名"]))
        lines.append(f"### {gid} [{g['类型']}] 候选主名: {g['候选主名'] or '（空）'} | 出现次数: {sum(g['名称频次'].values())} | 校验 {ok}通过/{bad}失败{''.join(flags)}")
        lines.append("名称频次: " + "、".join(f"{k}({v})" for k, v in g["名称频次"].items()))
        lines.append(f"出现章节: {format_positions(g['出现章节集'], total_chapters)}")
        for r in g["失败记录"]:
            lines.append(f"  - ⚠️ {r['名称']} 章节{r['出现章节']} 行号{r['行号']}: {r['校验失败']} | 摘句: {r['摘句'][:60]}")
    lines.append("")
    missed, orphans = by_type[chunk]
    lines.extend([
        f"## 交叉校验（{chunk}）",
        f"- 摘要提到但记录中缺失的名称: {('、'.join(missed)) if missed else '无'}",
        f"- 记录中有但摘要未提的名称（孤证候选）: {('、'.join(orphans)) if orphans else '无'}",
        "",
    ])
    overlaps = find_quote_overlaps(type_groups)
    if overlaps:
        lines.append("## 摘句重叠提示（疑似同实异名，应合并）")
        for a, b, q in overlaps:
            lines.append(f"- {a} 与 {b} 摘句重叠: 「{q}」→ 同一资产的名称漂移，合并时主名取摘句对应的原文称呼")
        lines.append("")
    if chunk == "服装":
        lines.append("## 章节摘要（供绑定人物推断：造型出现时出场的人物）")
        lines.append("| 章节 | 概要 | 人物 | 场景 | 道具 | 服装 |")
        lines.append("|---|---|---|---|---|---|")
        for s in summaries:
            lines.append(f"| {s['章节']} | {s['概要']} | {s['人物']} | {s['场景']} | {s['道具']} | {s['服装']} |")
        lines.append("")
    lines.extend([
        "## 裁决要求",
        "按 templates/聚合裁决模板.md 的规则，对该块每个组输出裁决行。",
        "仅输出【资产裁决】与【待确认】表；【项目基础信息】表只在「基础信息」块输出，本块禁止重复输出。",
    ])
    return "\n".join(lines)


def _dedup_pending(pending: list[dict]) -> list[dict]:
    """待确认项按（名称, 类型）去重，保留首次出现。"""
    seen, dedup = set(), []
    for p in pending:
        key = (p["名称"], p["类型"])
        if key not in seen:
            seen.add(key)
            dedup.append(p)
    return dedup


def parse_verdict(text: str) -> tuple[list[dict], dict[str, str], list[dict], int]:
    """Parse LLM verdict into verdicts, project info, pending items and bad-row count."""
    verdict_types = ("人物", "场景", "道具", "服装")
    placeholder_names = PLACEHOLDER_NAMES
    verdicts, info, pending = [], {}, []
    bad = []
    cur = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("## "):
            cur = line[3:].strip()
            continue
        if line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cur == "资产裁决" and cells[0] != "组ID":
                if len(cells) < 9:
                    bad.append(f"列数不足({len(cells)}列): {line[:60]}")
                    continue
                if cells[1] not in verdict_types:
                    bad.append(f"类型出格「{cells[1]}」: {line[:60]}")
                    continue
                if cells[2] in placeholder_names and cells[7] != "附录":
                    # Silently skip placeholder formal rows; they are not format errors.
                    continue
                verdicts.append({
                    "组ID": cells[0], "类型": cells[1], "主名": cells[2],
                    "等级": cells[3], "合并": cells[4], "别名": cells[5],
                    "绑定": cells[6], "处理": cells[7], "备注": cells[8],
                })
                if cells[7] == "附录":
                    name = cells[2] if cells[2] not in placeholder_names else cells[0]
                    pending.append({"名称": name, "类型": cells[1], "说明": cells[8]})
            elif cur == "待确认" and len(cells) >= 3 and cells[0] != "名称":
                if cells[0] in placeholder_names:
                    continue
                pending.append({"名称": cells[0], "类型": cells[1], "说明": cells[2]})
            elif cur == "项目基础信息" and len(cells) >= 2:
                info[cells[0]] = cells[1]

    if bad:
        print(f"⚠️ 裁决解析丢弃 {len(bad)} 行（LLM 输出格式出格——请检查 .process/裁决/裁决_<块>.md 并修正后重跑）:")
        for d in bad[:5]:
            print(f"     - {d}")

    return verdicts, info, _dedup_pending(pending), len(bad)


def _ch_int(c: str) -> int:
    m = re.match(r"第(\d+)章", c or "")
    return int(m.group(1)) if m else 0


def _asset_positions_from_summaries(name: str, summaries: list[dict]) -> set[int]:
    """Infer occurrence chapters from summaries when no pre-group exists."""
    chs = set()
    for s in summaries:
        for field in ("人物", "场景", "道具", "服装"):
            if name in [n.strip() for n in s[field].replace("、", "/").split("/")]:
                m = re.match(r"第(\d+)章", s["章节"])
                if m:
                    chs.add(int(m.group(1)))
                break
    return chs


def render_final(
    novel: str,
    author: str,
    verdicts: list[dict],
    info: dict[str, str],
    pending: list[dict],
    groups_by_id: dict[str, dict],
    summaries: list[dict],
    total_chapters: int,
    needs: list[dict] | None = None,
    ledger: list[dict] | None = None,
) -> str:
    """Render the final reverse-extracted asset report.

    旧四表（人物/场景/道具/服装）由类型化总账渲染（工单 2 L139b），本函数不再各自维护名单；
    `ledger` 缺省时由同一批输入就地构建，保持既有调用方签名兼容。
    """
    from engine.asset_ledger import build_ledger, render_legacy_tables

    out = [f"# 《{novel}》资产反推 · 最终（{author}）\n"]

    out.append("## 一、项目基础信息表")
    out.append("| 字段 | 内容 |\n|---|---|")
    for k, v in info.items():
        out.append(f"| {k} | {v} |")
    out.append("")

    if ledger is None:
        ledger = build_ledger(build_asset_index(
            verdicts, groups_by_id, summaries, total_chapters, needs=needs))
    pid_map = {"人物": "CH", "场景": "SC", "道具": "PR", "服装": "CO"}
    for ledger_key in ("person", "scene", "prop", "costume"):
        out.append(render_legacy_tables(ledger, only=ledger_key).rstrip("\n"))
        out.append("")

    out.append("## 六、分集/章节资产使用表")
    out.append("| 章节 | 剧情概要 | 出场人物 | 主要场景 | 关键道具 | 服装造型 |")
    out.append("|---|---|---|---|---|---|")
    for s in summaries:
        out.append(f"| {s['章节']} | {s['概要']} | {s['人物']} | {s['场景']} | {s['道具']} | {s['服装']} |")
    out.append("")

    out.append("## 衍生需求汇总（剧本驱动，供调度清单）")
    out.append("| 类型 | 资产 | 需求 | 出现章节 |")
    out.append("|---|---|---|---|")
    if needs:
        name_map = {}
        for g in groups_by_id.values():
            name_map.setdefault(norm_text(g["候选主名"]), g["候选主名"])
            for n in g["名称频次"]:
                name_map.setdefault(norm_text(n), g["候选主名"])
        agg: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        # 类型列**以总账为准**：模型写的类型只是提示。实测（2026-09-21 全新库）模型把两件服装
        # 写成「道具」，这张表是交付物的一部分，错类型一路流到调度清单 → 衍生步骤按「道具」
        # 去找父资产基础提示词 → 找不到 → 抛错停链。
        from engine.asset_ledger import KEY_TO_TYPE
        type_by_name = {norm_text(str(e.get("canonical_name") or "")):
                        KEY_TO_TYPE.get(str(e.get("asset_type") or ""), "")
                        for e in ledger}
        for nd in needs:
            key = name_map.get(norm_text(nd["资产"]))
            if not key:
                continue
            kind = type_by_name.get(norm_text(key)) or nd["类型"]
            agg[(kind, key, nd["需求"])].append(nd["章节"])
        for (t, name, need), chs in sorted(agg.items(), key=lambda kv: (kv[0][0], _ch_int(sorted(set(kv[1]), key=_ch_int)[0]) if kv[1] else 0)):
            chs_sorted = "、".join(sorted(set(chs), key=_ch_int))
            out.append(f"| {t} | {name} | {need} | {chs_sorted} |")
    out.append("")

    out.append("## 七、导入资产总表")
    out.append("| 资产ID | 资产类型 | 资产名称 | 出现位置 | 重要等级 | Prompt编号 |")
    out.append("|---|---|---|---|---|---|")
    for v in verdicts:
        if v["处理"] == "附录":
            continue
        g = groups_by_id.get(v["组ID"])
        if g:
            pos = format_positions(g.get("出现章节集", set()), total_chapters)
        else:
            pos = format_positions(_asset_positions_from_summaries(v["主名"], summaries), total_chapters)
        pfx = pid_map.get(v["类型"], "ZZ")
        out.append(f"| {pfx}_{v['主名']} | {v['类型']} | {v['主名']} | {pos} | {v['等级']} | P_{pfx}_{v['主名']} |")
    out.append("")

    out.append("## 八、文件夹结构建议")
    out.append(f"```\n[{novel}]_国漫三维资产库/\n├── 01_项目基础信息/\n├── 02_人物资产/\n├── 03_场景资产/\n├── 04_道具资产/\n├── 05_服装妆造/\n├── 06_提示词库/\n│   ├── 基础提示词/\n│   └── 衍生提示词/\n└── 07_定妆图与成片/\n```")
    out.append("")

    out.extend([
        "## 九、交付物说明",
        "| 目录/文件 | 阶段 | 用途 |",
        "|---|---|---|",
        "| `提示词/基础/人物提示词.md` | ✅ 可直接使用 | 全部人物基础提示词（按序号分节，已绑定正式服装），复制即用 |",
        "| `提示词/基础/场景提示词.md` | ✅ 可直接使用 | 全部场景基础提示词（按序号分节），复制即用 |",
        "| `提示词/基础/道具提示词.md` | ✅ 可直接使用 | 全部道具基础提示词（按序号分节），复制即用 |",
        "| `提示词/衍生/`（人物/场景/道具提示词.md） | ✅ 可直接使用 | 资产×变体衍生提示词（造型/时段/状态），复制即用 |",
        "| `描述/人物描述.md 等三文档` | 📋 中间产物 | 资产视觉描述（供基础提示词引用，也可用于表格资产管理） |",
        "| `服饰设计/` | ✅ 可直接使用 | 服饰标准库/角色服饰标准表（仅当配置服饰设计模块时产出） |",
        "| `登记表/` | 🔧 过程文件 | 候选遍/合并产物（原文证据），供裁决与对账使用，可留存可清理 |",
        "| `.process/裁决/裁决输入_*.md` | 🔧 过程文件 | 裁决输入（预分组+校验+交叉提示），断点复用 |",
        "| `.process/裁决/裁决_*.md` | 🔧 过程文件 | 裁决输出（含待确认），人工复核的依据 |",
        "| `.process/登记表对账报告.md` | 🔧 过程文件 | 四层对账结果（漂移/新资产申报），人工复核依据 |",
        "| `反推_最终.md` | 📋 汇总 | 全部资产的权威清单（本文件），后续步骤以此为输入 |",
        "",
        "> 最终交付 = `提示词/基础/` + `提示词/衍生/` + `服饰设计/`；其余为过程文件，不影响交付。",
    ])

    out.append("## 待确认附录（出处未验证/违规名，未进入正式表）")
    out.append("| 名称 | 类型 | 说明 |")
    out.append("|---|---|---|")
    if pending:
        seen = set()
        formal_names = {(v["主名"], v["类型"]) for v in verdicts if v["处理"] != "附录"}
        for p in pending:
            name = p["名称"]
            if name.startswith("G") and name in groups_by_id:
                name = groups_by_id[name].get("候选主名") or name
            key = (name, p["类型"])
            if key in seen or key in formal_names:
                continue
            seen.add(key)
            out.append(f"| {name} | {p['类型']} | {p['说明']} |")
    out.append("")

    groups_all = list(groups_by_id.values())
    records_total = sum(len(g["成员"]) for g in groups_all)
    cites_fail = sum(g["出处校验"]["失败"] for g in groups_all)
    violates = sum(len(g["违规名"]) for g in groups_all)
    missed_now, _ = cross_check(summaries, [g for g in groups_all])
    formal_count = len([v for v in verdicts if v["处理"] != "附录"])
    out.extend([
        "## 合并统计",
        "| 项目 | 数值 |",
        "|---|---|",
        f"| 章节总数 | {len(summaries)} |",
        f"| 资产记录总数 | {records_total} |",
        f"| 预分组数 | {len(groups_all)} |",
        f"| 出处校验失败记录 | {cites_fail} |",
        f"| 违规名数 | {violates} |",
        f"| 漏提候选 | {len(missed_now)} |",
        f"| 正式资产 | {formal_count} |",
        f"| 待确认附录 | {len(pending)} |",
        "",
        "---",
        "> 生成方式: 脚本预聚合 + LLM 裁决 | 出处强制: 正式表仅收录通过原文校验的资产",
    ])
    return "\n".join(out)


def _legacy_asset_id(asset_type: str, name: str) -> str:
    """旧展示 ID：CH_/SC_/PR_/CO_ + 原文主名（保持下游解析兼容）。"""
    prefix = {"人物": "CH_", "场景": "SC_", "道具": "PR_", "服装": "CO_"}.get(asset_type, "AS_")
    return f"{prefix}{name}"


def _parse_connects_tags(remark: str) -> list[str]:
    """从裁决备注中解析 [CONNECTS_TO:SC_x] / [连接:SC_x] 等显式场景连接声明。"""
    targets: list[str] = []
    for m in re.finditer(r"\[(?:CONNECTS_TO|连接|connects_to)\s*[:=]\s*([^\]]+)\]", remark or ""):
        for part in re.split(r"[、/，,]", m.group(1)):
            part = part.strip()
            if part and part not in targets:
                targets.append(part)
    return targets


def _parse_subtype_tag(remark: str) -> str:
    m = re.search(r"\[(?:子类|subtype)\s*[:=]\s*([^\]]+)\]", remark or "")
    return m.group(1).strip() if m else ""


def _parse_function_tag(remark: str) -> str:
    m = re.search(r"\[(?:功能|story_function)\s*[:=]\s*([^\]]+)\]", remark or "")
    return m.group(1).strip() if m else ""


def _infer_production_priority(
    asset_type: str, level: str, member_count: int, has_variant_or_need: bool,
) -> str:
    """S/A/B/C 叙事等级 → P0-P3 生产优先级的保守默认映射。

    S（主角/贯穿关键）→ P0（被依赖项，需优先建模）；
    A（重要配角/高频）→ P1；B（普通配角/短暂）→ P2；
    C（群演/一次性/背景）→ P3。

    工单 4：生产优先级**不再**因「只出现一次且无衍生需求」推断为 P3——单次出现的资产仍是正式资产、
    仍要产出基础提示词（P3 也不吞提示词，见 generate_prompts.should_generate_base）。
    """
    text = (level or "").strip()
    if text.startswith("S") or "主角" in text or "核心" in text:
        return ProductionPriority.P0.value
    if text.startswith("A") or "重要" in text or "关键" in text:
        return ProductionPriority.P1.value
    if text.startswith("C") or "群演" in text or "一次" in text or "背景" in text:
        return ProductionPriority.P3.value
    return ProductionPriority.P2.value


_IMPORTANCE_ALIASES = {
    "S": "main", "主角": "main", "核心": "main", "主人公": "main",
    "A": "supporting", "重要": "supporting", "关键": "supporting", "反派": "supporting",
    "B": "minor", "C": "minor", "普通": "minor", "配角": "minor", "群演": "minor",
}


def infer_narrative_importance(level: str) -> str:
    """等级文本 → 剧情重要度（main/supporting/minor），与「出几张图」解耦。"""
    text = (level or "").strip()
    for key, val in _IMPORTANCE_ALIASES.items():
        if text.startswith(key) or key in text:
            return val
    return "minor"


def infer_production_mode(asset_type: str, bound: bool, formal: bool) -> str:
    """类型 + 是否绑定 + 是否正式 → 生产方式（图片规格），与叙事重要度互不推断。

    bound 服装随人物呈现（bound）；正式人物/场景/道具与独立关键服装出图（image）；
    非正式（待审）不投产（pending）。
    """
    if asset_type == "服装" and bound:
        return "bound"
    if not formal:
        return "pending"
    return "image"


def _symmetrize_connects_to(assets: list[dict]) -> int:
    """补齐 CONNECTS_TO 的反向边，返回补出的边数。

    relation 矩阵把该关系定义为**场景双向连通**，`continuity.check_connects_consistency`
    也按双向校验；而裁决只会为「声明方」写一条边。生产侧不补这一边，索引就永远自相矛盾
    （实测：一次真实全链跑到 verify，两个场景被判「CONNECTS_TO 单向」而阻断）。

    只补**双端都在索引内的场景**之间的边：目标不存在/非场景照旧交给判据报错，
    生产侧不在这一步掩盖问题。幂等（已有反向边则跳过）；补全结果与遍历顺序无关。
    """
    scenes = {a.get("asset_id", "") for a in assets if a.get("type") == "场景"}
    by_id = {a.get("asset_id", ""): a for a in assets}
    added = 0
    for a in assets:
        src = a.get("asset_id", "")
        for r in list(a.get("relations", []) or []):
            if not isinstance(r, dict) or r.get("kind") != "CONNECTS_TO":
                continue
            tgt_id = r.get("target", "")
            if not tgt_id or tgt_id == src or tgt_id not in scenes:
                continue
            tgt = by_id.get(tgt_id)
            if tgt is None:
                continue
            back = tgt.setdefault("relations", [])
            if any(isinstance(x, dict) and x.get("kind") == "CONNECTS_TO"
                   and x.get("target") == src for x in back):
                continue
            back.append({"kind": "CONNECTS_TO", "target": src,
                         "note": CONNECTS_TO_BACKFILL_NOTE})
            deps = tgt.setdefault("dependencies", [])
            if src not in deps:
                deps.append(src)
                deps.sort()
            added += 1
    return added


def build_asset_index(
    verdicts: list[dict],
    groups_by_id: dict[str, dict],
    summaries: list[dict],
    total_chapters: int,
    source_fingerprint: str = "",
    existing_key_store: dict | None = None,
    pending: list[dict] | None = None,
    needs: list[dict] | None = None,
) -> dict:
    """由裁决结果构建规范化资产索引（ADR-0084）。

    旧 Markdown 四表结构与旧 ID 不变；索引是关系与事实等级的单一结构化来源。
    needs 为显式参数（衍生需求列表），禁止使用函数对象隐式共享状态。
    """
    from engine.entity_resolution import ALIAS_OF, AMBIGUOUS, STATE_OF, resolve_entity
    from engine.extract_tags import parse_metadata_tags_from_remark as _parse_tags

    pending_names = {(p.get("名称", ""), p.get("类型", "")) for p in (pending or [])}
    needs = list(needs or [])
    allocator = StableKeyAllocator(existing_key_store)
    assets: list[dict] = []
    unresolved: list[dict] = []

    # 先收集全部旧 ID，用于悬空关系过滤
    formal = [v for v in verdicts if v.get("处理") != "附录"]
    asset_ids = {_legacy_asset_id(v["类型"], v["主名"]) for v in formal}
    # 旧四表行序＝裁决文件顺序（工单 2 L139b：总账承接渲染后必须原样保留，不得改成字典序）
    order_by_id: dict[str, int] = {}
    for _i, _v in enumerate(verdicts):
        order_by_id.setdefault(_legacy_asset_id(_v["类型"], _v["主名"]), _i)

    # 章节 → 出场人物/场景/道具/服装映射（用于 APPEARS_IN / LOCATED_IN）
    chapter_cast: dict[str, dict[str, list[str]]] = {}
    for s in summaries:
        ch = s.get("章节", "")
        chapter_cast[ch] = {
            "人物": [n.strip() for n in re.split(r"[、/，,]", s.get("人物", "")) if n.strip()],
            "场景": [n.strip() for n in re.split(r"[、/，,]", s.get("场景", "")) if n.strip()],
            "道具": [n.strip() for n in re.split(r"[、/，,]", s.get("道具", "")) if n.strip()],
            "服装": [n.strip() for n in re.split(r"[、/，,]", s.get("服装", "")) if n.strip()],
        }

    # 工单 3 L172：消歧裁决统一走 entity_resolution，aggregate 不再自建 name/alias 判定表
    existing_assets = [
        {"asset_id": _legacy_asset_id(v["类型"], v["主名"]), "type": v["类型"], "name": v["主名"],
         "aliases": [a.strip() for a in re.split(r"[、/，,]", v.get("别名", "") or "") if a.strip()]}
        for v in formal
    ]

    def _resolve_name(n: str, expect_type: str) -> tuple[str, bool]:
        """解析引用名 → (资产ID, 是否歧义)。歧义一律待审，禁止静默任选。"""
        n = (n or "").strip()
        # 先剥登记表回填标记，再剥未配对括号尾巴（成对括号是名字本身）
        n = re.sub(r"（脚本补入）$", "", n).strip()
        while n and n[-1] in "）)":
            close = n[-1]
            open_ch = "（" if close == "）" else "("
            if n.count(open_ch) < n.count(close):
                n = n[:-1].strip()
            else:
                break
        if not n:
            return "", False
        verdict = resolve_entity({"name": n, "type": expect_type}, existing_assets)
        if verdict["decision"] == ALIAS_OF:
            return verdict["target_id"], False
        if verdict["decision"] == AMBIGUOUS:
            return "", True
        return "", False

    # 状态需求按母资产聚合，主循环结束后统一并入母资产的 variant_needs（母资产可能尚未处理）
    deferred_variant_needs: dict[str, list[dict]] = {}

    for v in formal:
        asset_type, name = v["类型"], v["主名"]
        asset_id = _legacy_asset_id(asset_type, name)
        g = groups_by_id.get(v["组ID"], {})
        members = g.get("成员", []) or []

        evidence: list[dict] = []
        for r in members:
            quote = (r.get("摘句", "") or "").strip()
            if quote:
                evidence.append({
                    "segment": r.get("段文件", ""),
                    "chapter": r.get("出现章节", ""),
                    "line": r.get("行号", ""),
                    "quote": quote,
                })
        # 证据存在性 → 事实等级：有可验证摘句 = 剧本事实，否则保守降为强推断
        fact_level = SCRIPT_FACT if evidence else FactLevel.STRONG_INFERENCE.value
        if (name, asset_type) in pending_names:
            fact_level = PENDING_CONFLICT_FACT

        # 备注标签 → 子类 / 故事功能
        remark = v.get("备注", "") or ""
        subtype = _parse_subtype_tag(remark)
        story_function = _parse_function_tag(remark)

        # 是否有衍生需求（影响 P 级与故事功能兜底；显式参数，禁止隐式共享）
        has_need = any(
            (nd.get("资产") == name or nd.get("资产") in (v.get("别名", "") or ""))
            for nd in needs
        )
        priority = _infer_production_priority(asset_type, v.get("等级", ""), len(members), has_need)
        if not story_function and asset_type in ("场景", "道具"):
            story_function = "剧本驱动变体载体" if has_need else "普通陈设/背景"
        if not story_function:
            # P3 排除说明必需：人物/服装同样需要兜底故事功能
            story_function = "背景陈设/文字足够" if priority == ProductionPriority.P3.value else "待补充"

        first_line = 1
        if evidence:
            digits = "".join(filter(str.isdigit, str(evidence[0].get("line", ""))))
            first_line = int(digits) if digits else 1
        aliases = [a.strip() for a in re.split(r"[、/，,]", v.get("别名", "") or "") if a.strip()]
        stable_key = allocator.allocate(asset_type, asset_id, {"line": f"{first_line:05d}"}, aliases)

        # 关系构建（含空间标签证据；无法解析的关系进 unresolved，不静默丢弃）
        relations: list[dict] = []
        dependencies: list[str] = []
        member_tags = [_parse_tags(r.get("备注", "") or "") for r in members]
        space_facts: dict[str, list[dict]] = {"space_structure": [], "anchor": [], "entry_exit": [],
                                              "lighting": [], "material": [], "objects": [],
                                              "orientation": [], "time": [], "connects": []}
        for r, tags in zip(members, member_tags):
            ev_ref = {"segment": r.get("段文件", ""), "chapter": r.get("出现章节", ""),
                      "line": r.get("行号", ""), "quote": (r.get("摘句", "") or "").strip()}
            for field in ("space_structure", "anchor", "entry_exit", "lighting", "material",
                          "objects", "orientation", "time"):
                if tags.get(field):
                    space_facts[field].append({"value": tags[field], **ev_ref})
            for target in (tags.get("connects") or []):
                space_facts["connects"].append({"value": target, **ev_ref})

        def _record_unresolved(kind: str, raw: str, reason: str) -> None:
            unresolved.append({"asset_id": asset_id, "kind": kind, "raw": raw, "reason": reason})

        # WEARS：服装绑定人物（悬空绑定进 unresolved，不猜人）
        if asset_type == "服装":
            bind = (v.get("绑定", "") or "").strip()
            if bind and bind != "待确认":
                target, amb = _resolve_name(bind, "人物")
                if amb:
                    _record_unresolved("WEARS", bind, "绑定名歧义，需人工裁决")
                elif target and target in asset_ids:
                    relations.append({"kind": "WEARS", "target": target, "note": "服装穿戴于人物"})
                    dependencies.append(target)
                else:
                    _record_unresolved("WEARS", bind, "绑定人物在索引中不存在")
        if asset_type == "人物":
            # 反向 WEARS 由服装侧表达；人物侧记录 APPEARS_IN（出场场景）
            pass

        # APPEARS_IN / LOCATED_IN：人物/道具 → 出现章节中的场景
        if asset_type in ("人物", "道具"):
            kind = "APPEARS_IN" if asset_type == "人物" else "LOCATED_IN"
            scenes: list[str] = []
            for r in members:
                chs = [c.strip() for c in re.split(r"[、/，,]", r.get("出现章节", "") or "") if c.strip()]
                for ch in chs:
                    for key, cast in chapter_cast.items():
                        if key == ch or key.startswith(ch) or ch in key:
                            for sc in cast.get("场景", []):
                                tid, amb = _resolve_name(sc, "场景")
                                if amb:
                                    _record_unresolved(kind, sc, "场景名歧义，需人工裁决")
                                elif tid and tid in asset_ids and tid != asset_id and tid not in scenes:
                                    scenes.append(tid)
                                elif sc and not tid:
                                    _record_unresolved(kind, sc, "同章节场景名无法解析到索引资产")
            for tid in sorted(scenes):
                relations.append({"kind": kind, "target": tid, "note": "同章节出场"})
                if tid not in dependencies:
                    dependencies.append(tid)

        # CONNECTS_TO：场景备注显式声明 + 空间标签连接点（仅保留双端存在的边）
        if asset_type == "场景":
            declared = list(_parse_connects_tags(remark))
            for fact in space_facts["connects"]:
                if fact["value"] not in declared:
                    declared.append(fact["value"])
            for target_name in declared:
                tid, amb = _resolve_name(target_name, "场景")
                if amb:
                    _record_unresolved("CONNECTS_TO", target_name, "连接目标歧义，需人工裁决")
                    continue
                if not tid:
                    tid = target_name if target_name in asset_ids else ""
                if tid and tid in asset_ids and tid != asset_id:
                    relations.append({"kind": "CONNECTS_TO", "target": tid, "note": "场景连通"})
                    if tid not in dependencies:
                        dependencies.append(tid)
                else:
                    _record_unresolved("CONNECTS_TO", target_name, "连接目标在索引中不存在")

        # R4b：变体需求记入 variant_needs（衍生需求），不制造 STATE_OF 母资产自环；
        # 仅当出现独立变体节点后才允许发 变体→母资产 边（RELATION_MATRIX 校验）
        variant_needs = []
        for nd in needs:
            if (nd.get("资产") == name or nd.get("资产") in (v.get("别名", "") or "")) and nd.get("需求"):
                variant_needs.append({"需求": nd.get("需求"), "章节": nd.get("章节", "")})

        # 工单 3 L171/L172：显式 [状态:…] 交 entity_resolution 裁决为 STATE_OF，
        # 需求写入母资产的变体列表（上方 variant_needs），绝不伪造母资产自环。
        # 标签可能落在段级成员备注或裁决备注任一处，两处都认。
        state_names: list[str] = []
        for tag in member_tags:
            value = str(tag.get("state") or "").strip()
            if value and value not in state_names:
                state_names.append(value)
        verdict_state = str(_parse_tags(remark).get("state") or "").strip()
        if verdict_state and verdict_state not in state_names:
            state_names.append(verdict_state)
        for state in state_names:
            peers = [a for a in existing_assets if a["asset_id"] != asset_id]
            verdict = resolve_entity({"name": asset_id, "type": asset_type, "state_of": state}, peers)
            if verdict["decision"] == STATE_OF:
                chapter = members[0].get("出现章节", "") if members else ""
                deferred_variant_needs.setdefault(verdict["target_id"], []).append(
                    {"需求": f"状态：{state}", "章节": chapter})
            else:
                _record_unresolved("STATE_OF", state, "状态母资产命中非唯一或不存在，需人工裁决")

        # 工单 4：叙事重要度与生产方式两个独立字段（不再由优先级推断是否出图/出提示词）
        is_formal = fact_level != PENDING_CONFLICT_FACT
        bound_costume = any(r.get("kind") == "WEARS" for r in relations)
        narrative_importance = infer_narrative_importance(v.get("等级", ""))
        production_mode = infer_production_mode(asset_type, bound_costume, is_formal)

        # 出现位置：沿用旧四表的算法（有分组用分组章节集，否则回退摘要定位），保证渲染逐字一致
        group_row = groups_by_id.get(v.get("组ID"))
        if group_row:
            display_positions = format_positions(group_row.get("出现章节集", set()), total_chapters)
        else:
            display_positions = format_positions(
                _asset_positions_from_summaries(v.get("主名", ""), summaries), total_chapters)

        assets.append({
            "asset_id": asset_id,
            "stable_key": stable_key,
            "type": asset_type,
            "subtype": subtype,
            "name": name,
            "story_function": story_function,
            "priority": priority,
            "narrative_importance": narrative_importance,
            "production_mode": production_mode,
            "fact_level": fact_level,
            "evidence": evidence,
            "dependencies": sorted(dependencies),
            "relations": relations,
            "space_facts": {k: val for k, val in space_facts.items() if val},
            "variant_needs": variant_needs,
            "status": "CONFIRMED" if fact_level != PENDING_CONFLICT_FACT else "PENDING",
            # 旧四表展示字段（工单 2 L139b）：总账成为唯一库存源后，四表由这些字段渲染，不再各自维护名单
            "positions": display_positions,
            "importance_level": v.get("等级", ""),
            "remark": remark,
            "alias_display": v.get("别名", ""),
            "bind_display": v.get("绑定", "") if asset_type == "服装" else "",
            "display_order": order_by_id.get(asset_id, len(verdicts)),
        })

    if deferred_variant_needs:
        by_id = {a["asset_id"]: a for a in assets}
        for target_id, extra in sorted(deferred_variant_needs.items()):
            parent = by_id.get(target_id)
            if parent is None:
                unresolved.append({"asset_id": target_id, "kind": "STATE_OF", "raw": extra[0]["需求"],
                                   "reason": "状态母资产不在索引中"})
                continue
            parent["variant_needs"].extend(extra)

    assets.sort(key=lambda a: (a["type"], a["stable_key"]))
    _symmetrize_connects_to(assets)
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "source_fingerprint": source_fingerprint,
        "assets": assets,
        "unresolved_relations": sorted(unresolved, key=lambda u: (u["asset_id"], u["kind"], u["raw"])),
        # R4a：mapping 由同一次分配显式返回，废除 last_allocator 函数对象侧信道
        "stable_key_mapping": allocator.to_dict(),
    }


def write_asset_index(
    lib: Path,
    verdicts: list[dict],
    groups_by_id: dict[str, dict],
    summaries: list[dict],
    total_chapters: int,
    source_fingerprint: str = "",
    needs: list[dict] | None = None,
) -> Path | None:
    """将资产索引落盘到 <资产库>/.process/资产索引.json（幂等：复用既有 stable_key 映射）。"""
    proc_dir = lib / PROCESS_DIR
    proc_dir.mkdir(parents=True, exist_ok=True)
    index_path = proc_dir / "资产索引.json"
    existing_store = None
    if index_path.is_file():
        try:
            old = json.loads(index_path.read_text(encoding="utf-8"))
            mapping = old.get("stable_key_mapping")
            if isinstance(mapping, dict) and mapping.get("keys"):
                existing_store = mapping
        except (OSError, ValueError):
            existing_store = None
    index = build_asset_index(
        verdicts, groups_by_id, summaries, total_chapters,
        source_fingerprint=source_fingerprint,
        existing_key_store=existing_store, needs=needs,
    )
    # 校验：冲突项不得混入正式关系（悬空边已进 unresolved，此处做最终断言）
    result = AssetSchemaValidator().validate_index(index)
    if not result.is_valid:
        print("⚠️ 资产索引校验未通过（仅提示，不阻断旧表写入）:")
        for e in result.errors[:8]:
            print(f"     - {e}")
    if index.get("unresolved_relations"):
        print(f"⚠️ 未解析关系 {len(index['unresolved_relations'])} 条已记入 unresolved_relations（verify 将阻断受影响资产）")
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    # 工单 2/4：把类型化总账与后期元素清单落盘为机器层唯一库存源（附加、异常不阻断旧表交付）
    try:
        emit_ledger_and_post(lib, index)
    except Exception as e:  # noqa: BLE001 —— 附加层，任何异常都不得回退已写好的旧索引
        print(f"⚠️ asset-ledger/后期元素清单 生成失败（忽略，不影响旧表）: {e}")
    return index_path


def load_post_elements_from_facts(lib: Path) -> list[dict]:
    """从 .process/source-facts.jsonl 提取后期元素（字幕/片头/片尾/预告/界面文字）。"""
    path = lib / PROCESS_DIR / "source-facts.jsonl"
    if not path.is_file():
        return []
    seen: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("candidate_type") != "post_element":
            continue
        name = (rec.get("candidate_name") or rec.get("quote") or "").strip()
        if not name:
            continue
        seen.setdefault(name, {"name": name, "quote": (rec.get("quote") or name).strip(),
                               "line": str(rec.get("start", "")), "segment": rec.get("segment_id", "")})
    return [seen[k] for k in sorted(seen)]


def render_post_elements_md(post_elements: list[dict]) -> str:
    lines = ["# 后期元素清单", "",
             "> 字幕、片头、片尾、预告、界面文字等属后期制作元素，**不是道具**：不进道具提示词、不进生图计划。", ""]
    if not post_elements:
        lines.append("（本源未识别到后期元素。）")
    else:
        lines.append("| 序号 | 后期元素 | 出处摘句 |")
        lines.append("|---|---|---|")
        for i, pe in enumerate(sorted(post_elements, key=lambda p: p["name"]), 1):
            quote = (pe.get("quote") or "").replace("|", "｜").replace("\n", " ")
            lines.append(f"| {i:03d} | {pe['name']} | {quote} |")
    return "\n".join(lines) + "\n"


def emit_ledger_and_post(lib: Path, index: dict) -> None:
    """写 .process/asset-ledger.json、依赖图与 后期元素清单.md（总账为唯一库存源）。

    工单 13：总账与依赖图必须**一起**产出。只写总账会让库停在「有总账、无依赖图」的
    半截编译态，被签发门 `check_artifact_graph` 判死——那不是用户该修的错。
    """
    from engine import artifact_graph as _graph
    from engine import asset_ledger as _ledger
    post = load_post_elements_from_facts(lib)
    ledger = _ledger.build_ledger(index, post_elements=post)
    _ledger.write_ledger(lib, ledger)
    _graph.build_library_graph(lib).save(lib)
    (lib / "后期元素清单.md").write_text(render_post_elements_md(post), encoding="utf-8")


def registry_reconcile(lib: Path, group: str, records: list[dict], texts: dict[str, str]) -> tuple[list[str], list[str]]:
    """ADR-0003 reconcile: segment records vs global registry."""
    registry_path = lib / f"{group}-登记表.md"
    registry = []
    if registry_path.exists():
        for line in registry_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("|") and not line.startswith("|---"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) >= 2 and cells[0] not in ("候选主名", "主名", "资产ID"):
                    aliases = {cells[0]} | {a.strip() for a in re.split(r"[、/，,]", cells[1]) if a.strip()}
                    registry.append((cells[0], aliases))
    drift, claims = [], []
    for r in records:
        name = r["名称"]
        matched = any(name in aliases for _, aliases in registry)
        if not matched:
            similar = [m for m, aliases in registry if name and m and (name in m or m in name)]
            if similar:
                drift.append(f"疑似漂移: 段记录「{name}」与登记表主名「{similar[0]}」疑似同实异名（主名规则——段级提取必须用登记表主名；若确为不同资产请忽略本条）")
            else:
                drift.append(f"新资产: 「{name}」（登记表无此名，{r['类型']}，{r['出现章节']}）")
        if "新增" in r.get("备注", ""):
            ok, why = verify_citation(r, texts)
            status = "通过（可并入登记表）" if ok else f"未通过（{why}，降级待确认附录）"
            claims.append(f"[新增]申报: 「{name}」（{r['类型']}，{r['出现章节']}）出处校验{status}")
    return drift, claims


def candidate_ghost_check(lib: Path, group: str, records: list[dict]) -> list[str]:
    """ADR-0003 reconcile: candidate registry entries absent from extraction records."""
    cand_dir = lib / "登记表" / group / "候选表"
    ghosts = []
    if not cand_dir.is_dir():
        return ghosts
    cand_by_seg = {}
    for path in cand_dir.iterdir():
        if path.suffix != ".md" or not path.name.endswith("-候选表.md"):
            continue
        seg_name = path.name.replace("-候选表.md", "")
        names = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("|") and not line.startswith("|---"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if cells and cells[0] not in ("候选主名", "主名"):
                    names.add(cells[0])
        cand_by_seg[seg_name] = names
    rec_by_seg: dict[str, set[str]] = defaultdict(set)
    for r in records:
        seg = r["段文件"].replace(".md", "")
        rec_by_seg[seg].add(r["名称"])
        for a in re.split(r"[、/，,]", r["别名"]):
            if a.strip():
                rec_by_seg[seg].add(a.strip())
    for seg, cand_names in cand_by_seg.items():
        rec_names = rec_by_seg.get(seg, set())
        for n in sorted(cand_names):
            if n and n not in rec_names:
                ghosts.append(f"疑似幻觉: 候选遍登记「{n}」但提取遍同段无记录 @ {seg} —— 降级待确认附录，人工复核")
    return ghosts


def report_reconcile(
    drift: list[str], claims: list[str], ghosts: list[str], lib: Path, group: str, verbose: bool = False
) -> None:
    """Write the reconcile report to disk and print a summary (details only with --verbose)."""
    print("\n📋 登记表对账（ADR-0003 四项，明细见 .process/登记表对账报告.md）")
    print(f"  - 漂移/新资产预警: {len(drift)} 项")
    if verbose:
        for d in drift[:15]:
            print(f"     ⚠️  {d}")
        if len(drift) > 15:
            print(f"     ... 等 {len(drift)} 项")
    print(f"  - [新增]严格申报验证: {len(claims)} 项")
    if verbose:
        for c in claims[:10]:
            print(f"     📌 {c}")
    print(f"  - 候选遍疑似幻觉: {len(ghosts)} 项")
    if verbose:
        for g in ghosts[:10]:
            print(f"     👻 {g}")
    lines = [f"# 登记表对账报告（{group}）", ""]
    lines.append(f"- 漂移/新资产预警 {len(drift)} 项")
    lines += [f"  - {d}" for d in drift]
    lines.append(f"- [新增]严格申报验证 {len(claims)} 项")
    lines += [f"  - {c}" for c in claims]
    lines.append(f"- 候选遍疑似幻觉 {len(ghosts)} 项（候选遍登记/提取遍同段无记录 → 降级待确认附录）")
    lines += [f"  - {g}" for g in ghosts]
    proc_dir = lib / PROCESS_DIR
    proc_dir.mkdir(parents=True, exist_ok=True)
    out = proc_dir / "登记表对账报告.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    legacy_out = lib / "登记表对账报告.md"
    if legacy_out.exists():
        legacy_out.unlink()
    print(f"  📄 报告已落盘: {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description="聚合裁决")
    parser.add_argument("prod_dir", help="段产物目录")
    parser.add_argument("orig_dir", help="原文段目录")
    parser.add_argument("--lib", default=str(ASSET_LIB_DIR), help="资产库根目录")
    parser.add_argument("--no-llm", action="store_true", help="只做脚本部分，输出裁决输入，不调 LLM")
    parser.add_argument("--out", help="反推_最终.md 输出路径（默认 <lib>/反推_最终.md）")
    parser.add_argument("--verbose", action="store_true", help="打印过程明细（默认只输出汇总）")
    args = parser.parse_args()
    verbose = args.verbose

    prod_dir = Path(args.prod_dir)
    orig_dir = Path(args.orig_dir)
    lib = Path(args.lib)

    summaries, records, needs, unparsed = parse_products(prod_dir)
    if not records and not summaries:
        print("❌ 未解析到任何段产物（请先运行 extract_segments.py）")
        return 1
    texts = load_original_texts(orig_dir)
    print(f"📄 段产物: 章节摘要 {len(summaries)} 条, 资产记录 {len(records)} 条, 变化需求 {len(needs)} 条")
    if unparsed:
        print(f"⚠️  未解析表格行 {len(unparsed)} 条（LLM 输出格式不规范，可能静默丢资产——请人工检查段产物并修正模板/重跑）:")
        for u in unparsed[:5]:
            print(f"     - {u}")

    valid_needs = []
    for nd in needs:
        ok, why = verify_citation(nd, texts)
        if ok:
            valid_needs.append(nd)
        else:
            print(f"⚠️  需求出处校验失败，丢弃: {nd['类型']}「{nd['资产']}」{nd['需求']}（{why} | 摘句: {nd['摘句'][:40]}）")
    needs = valid_needs

    patched, patch_details = backfill_summaries(summaries, records)
    if patched:
        print(f"🔧 脚本补入摘要: {patched} 处（记录有但摘要缺失）")
        if verbose:
            for d in patch_details[:10]:
                print(f"     - {d}")
            if len(patch_details) > 10:
                print(f"     ... 等 {len(patch_details)} 处")

    groups = group_records(records, texts)
    print(f"🔗 预聚合: {len(groups)} 个分组（明细用 --verbose 查看）")
    if verbose:
        for g in groups:
            ok, bad = g["出处校验"]["通过"], g["出处校验"]["失败"]
            vf = f" ⚠️{len(g['违规名'])}违规名" if g["违规名"] else ""
            print(f"  - [{g['类型']}] {g['候选主名'] or '（空）'} | {sum(g['名称频次'].values())} 次 | 校验 {ok}✓/{bad}✗{vf}")

    by_type = cross_check_by_type(summaries, groups)
    total_missed = sum(len(v[0]) for v in by_type.values())
    total_orphans = sum(len(v[1]) for v in by_type.values())
    print(f"🔀 交叉校验: 摘要有记录无 {total_missed} 项, 记录有摘要无 {total_orphans} 项")

    total_chapters = len(summaries)
    group = prod_dir.name
    novel, author = group, "未知"
    for path in sorted(orig_dir.iterdir()):
        if path.suffix == ".md" and "资产记录" not in path.name:
            meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            novel = meta.get("小说名称", novel).strip("《》")
            author = meta.get("作者", author)
            break

    drift, claims = registry_reconcile(lib, group, records, texts)
    ghosts = candidate_ghost_check(lib, group, records)
    report_reconcile(drift, claims, ghosts, lib, group, verbose=verbose)

    for i, g in enumerate(groups, 1):
        g["组ID"] = f"G{i:02d}"

    registry_lines = ""
    reg_path = lib / f"{group}-登记表.md"
    if reg_path.exists():
        reg_rows = []
        for line in reg_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("|") and not line.startswith("|---"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) >= 5 and cells[0] not in ("候选主名", "主名", "资产ID"):
                    reg_rows.append("| " + " | ".join(cells[:5]) + " |")
        if reg_rows:
            registry_lines = "| 主名 | 别名 | 首现章 | 一句定性 | 类别 |\n|---|---|---|---|---|\n" + "\n".join(reg_rows)

    chunks = ["人物", "场景", "道具", "服装", "基础信息"]
    verdict_dir = lib / PROCESS_DIR / VERDICT_DIR
    verdict_dir.mkdir(parents=True, exist_ok=True)
    input_texts = {}
    for chunk in chunks:
        txt = build_chunk_input(chunk, novel, author, summaries, groups, by_type, total_chapters, registry_lines)
        p = verdict_dir / f"裁决输入_{chunk}.md"
        p.write_text(txt, encoding="utf-8")
        legacy_input = lib / f"裁决输入_{chunk}.md"
        if legacy_input.exists():
            legacy_input.unlink()
        input_texts[chunk] = txt
    print(f"📝 裁决输入: {len(chunks)} 块（已归档 {verdict_dir}）")

    if args.no_llm:
        print("ℹ️  --no-llm 模式：请人工/编排器将各块裁决输入交给 LLM 后重跑本命令")
        return 0

    template = (SKILL_DIR / "templates" / "聚合裁决模板.md").read_text(encoding="utf-8")
    parts = template.split("## 系统提示\n", 1)
    system_prompt = parts[1].split("\n## 用户输入模板\n", 1)[0].strip()
    user_tmpl = parts[1].split("\n## 用户输入模板\n", 1)[1].strip()

    results = {}
    print("⚖️  分块裁决（5 块并行，产物幂等断点）...")

    def _migrate_verdict(chunk: str, out_path: Path, hash_path: Path, cur_hash: str) -> str | None:
        legacy_out = lib / f"裁决_{chunk}.md"
        legacy_hash = lib / f"裁决_{chunk}.md.hash"
        if not legacy_out.exists():
            return None
        content = legacy_out.read_text(encoding="utf-8")
        if legacy_hash.exists() and legacy_hash.read_text(encoding="utf-8").strip() == cur_hash:
            out_path.write_text(content, encoding="utf-8")
            hash_path.write_text(cur_hash, encoding="utf-8")
            legacy_out.unlink()
            legacy_hash.unlink()
            return content
        legacy_out.unlink()
        if legacy_hash.exists():
            legacy_hash.unlink()
        return None

    def adjudicate(chunk: str) -> tuple[str, str, str]:
        out_path = verdict_dir / f"裁决_{chunk}.md"
        hash_path = out_path.parent / (out_path.name + ".hash")
        cur_hash = hashlib.sha256(input_texts[chunk].encode("utf-8")).hexdigest()[:16]
        if (out_path.exists() and out_path.stat().st_size > 100 and hash_path.exists()
                and hash_path.read_text(encoding="utf-8").strip() == cur_hash):
            return chunk, out_path.read_text(encoding="utf-8"), "skipped"
        legacy_content = _migrate_verdict(chunk, out_path, hash_path, cur_hash)
        if legacy_content is not None:
            return chunk, legacy_content, "skipped"
        user_prompt = user_tmpl.format(adjudication_input=input_texts[chunk])
        content, usage = call_llm(system_prompt, user_prompt, max_tokens=MAX_TOKENS, min_len=80, step="聚合裁决", role="judgement")
        out_path.write_text(content + "\n", encoding="utf-8")
        hash_path.write_text(cur_hash, encoding="utf-8")
        if verbose:
            log(f"  ⚖️ {chunk}块: 完成（{len(content)} 字符 | prompt {usage.get('prompt_tokens', 0)} | completion {usage.get('completion_tokens', 0)}）")
        return chunk, content, "done"

    done_blocks = skip_blocks = 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(adjudicate, c): c for c in chunks}
        for fut in as_completed(futs):
            chunk = futs[fut]
            try:
                chunk, content, _st = fut.result()
                done_blocks += _st == "done"
                skip_blocks += _st == "skipped"
            except Exception as e:
                print(f"❌ 裁决块 {chunk} 失败（API 重试耗尽）: {e}")
                print("   重跑同一命令即可续传（已完成块断点跳过；失败块修正后重跑）")
                return 1
            results[chunk] = content
    print(f"⚖️  分块裁决自动完成: 生成 {done_blocks} 块, 复用断点 {skip_blocks} 块")

    verdicts, info, pending = [], {}, []
    for chunk in chunks:
        v, i, p, n_dropped = parse_verdict(results[chunk])
        if n_dropped:
            print(f"❌ 裁决块 {chunk} 丢弃 {n_dropped} 行——请修正 .process/裁决/裁决_{chunk}.md 后重跑单块（不允许带病进入产物）")
            return 1
        verdicts.extend(v)
        info.update(i)
        pending.extend(p)
    pending = _dedup_pending(pending)

    if not verdicts:
        print("⚠️  全部裁决块解析失败——请检查 裁决_*.md 产物与聚合裁决模板")
        return 1

    groups_by_id = {g["组ID"]: g for g in groups}
    out_path = Path(args.out) if args.out else lib / "反推_最终.md"
    hash_path = out_path.parent / (out_path.name + ".hash")

    input_hash = final_inputs_hash(novel, author, total_chapters, verdicts, info, pending, groups_by_id, summaries, needs)
    # AE-03: 短路跳过必须以“最终产物与资产索引均已对齐”为前提，缺索引不得跳过。
    target_lib = out_path.parent if args.out else lib
    expected_index = target_lib / PROCESS_DIR / "资产索引.json"
    if (out_path.exists() and hash_path.exists()
            and hash_path.read_text(encoding="utf-8").strip() == input_hash
            and expected_index.is_file()):
        print(f"⏭️  反推_最终.md 输入未变更（hash {input_hash}），跳过写入")
        print(f"   正式资产 {len(verdicts)} 项 | 待确认附录 {len(pending)} 项")
        return 0

    final = render_final(novel, author, verdicts, info, pending, groups_by_id, summaries, total_chapters, needs=needs)
    out_path.write_text(final, encoding="utf-8")
    print(f"✅ 反推_最终.md: {out_path}（{len(final)} 字符）")
    print(f"   正式资产 {len(verdicts)} 项 | 待确认附录 {len(pending)} 项")
    try:
        from engine.continuity import write_scene_master

        texts_fp = "".join(sorted(texts.values())) if isinstance(texts, dict) else ""
        fingerprint = "sha256:" + hashlib.sha256(texts_fp.encode("utf-8")).hexdigest()[:16]
        index_path = write_asset_index(
            target_lib,
            verdicts, groups_by_id, summaries, total_chapters,
            source_fingerprint=fingerprint, needs=needs,
        )
        print(f"📇 资产索引: {index_path}（ADR-0084 规范化索引，旧表结构不变）")
        # 工单C：正常聚合落盘后即生成/安全合并场景母版（无需 migrate）
        import json as _json

        from engine.art_constitution import load_constitution

        index_data = _json.loads(index_path.read_text(encoding="utf-8"))
        master_path = write_scene_master(target_lib, index_data, summaries,
                                         constitution=load_constitution(target_lib))
        print(f"🗺️ 场景母版: {master_path}（人工字段安全合并，不静默覆盖）")
    except Exception as e:
        # AE-03: 索引/母版失败不得静默成功——清除完成标记并以非零退出，
        # 后续重跑不得被前置 Markdown 哈希短路，必须如实重试。
        print(f"❌ 资产索引/母版生成失败（最终产物与索引未对齐）: {e}")
        try:
            hash_path.unlink(missing_ok=True)
        except OSError:
            pass
        return 1
    # AE-03: 最终 Markdown 与下游资产索引均成功生成后，才写入完成标记/短路哈希。
    hash_path.write_text(input_hash, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
