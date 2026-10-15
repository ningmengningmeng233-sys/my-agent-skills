# -*- coding: utf-8 -*-
"""证据保留的确定性源事实层（工单 1）。

规则扫描从原文里抽候选事实，逐条带**精确字符范围 + 原文摘句**；模型语义扫描按同一结构回填，
规则与模型取并集：任一方发现的候选都不因另一方缺失而消失，相同证据合并，同名跨类型冲突并存并标待裁决。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TypedDict

from engine import document_sections as ds

SCHEMA_VERSION = 1

# 与提取链路一致的确定性原型词（保守、可复算）
_ASSET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("person", re.compile(r"[一-龥]{2,4}(?:姑娘|公子|少年|老者|夫人|大夫|掌柜|船夫)")),
    ("scene", re.compile(r"[一-龥]{2,6}(?:街|厅|室|楼|城|码头|茶楼|客栈|天井)")),
    ("prop", re.compile(r"[一-龥]{0,1}(?:琴|剑|伞|壶|簪|灯|算盘|书|屏风|木门)")),
]

_RULE_CONFIDENCE = 0.9


class SourceFact(TypedDict, total=False):
    fact_id: str
    source_file: str
    segment_id: str
    start: int
    end: int
    quote: str
    section_kind: str
    candidate_type: str
    candidate_name: str
    extractor: str
    confidence: float


def _norm(quote: str) -> str:
    return re.sub(r"\s+", "", quote or "")


def make_fact(candidate_name: str, candidate_type: str, quote: str, *,
              source_file: str, segment_id: str, start: int | None = None,
              end: int | None = None, section_kind: str = ds.KIND_ACTION,
              extractor: str = "rule", confidence: float = 0.8) -> SourceFact:
    if start is None:
        start = 0
    if end is None:
        end = start + len(quote)
    fid = hashlib.sha1(
        f"{source_file}|{segment_id}|{candidate_type}|{candidate_name}|{_norm(quote)}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "fact_id": fid, "source_file": source_file, "segment_id": segment_id,
        "start": start, "end": end, "quote": quote, "section_kind": section_kind,
        "candidate_type": candidate_type, "candidate_name": candidate_name,
        "extractor": extractor, "confidence": confidence,
    }


def _kind_at(spans, pos: int) -> str:
    for s in spans:
        if s.start <= pos < s.end:
            return s.kind
    return ds.KIND_ACTION


def scan_deterministic_facts(text: str, *, source_file: str, segment_id: str) -> list[SourceFact]:
    """确定性扫描：制作说明→约束事实、后期文字→post_element、其余段落→资产候选。"""
    spans = ds.classify_sections(text)
    facts: list[SourceFact] = []

    for s in spans:
        if s.kind == ds.KIND_PRODUCTION_NOTE:
            # 只作为约束事实，绝不产出正式资产候选
            facts.append(make_fact(
                s.text.strip(), "constraint", text[s.start:s.end],
                source_file=source_file, segment_id=segment_id,
                start=s.start, end=s.end, section_kind=s.kind, confidence=_RULE_CONFIDENCE,
            ))
        elif s.kind == ds.KIND_POST_TEXT:
            marker = ds.POST_TEXT_RE.search(s.text)
            name = marker.group(1) if marker else s.text.strip()[:12]
            facts.append(make_fact(
                name, "post_element", text[s.start:s.end],
                source_file=source_file, segment_id=segment_id,
                start=s.start, end=s.end, section_kind=s.kind, confidence=_RULE_CONFIDENCE,
            ))

    # 资产候选：在 asset-bearing 段落上按原型词抽取，保留逐字范围
    for typ, rx in _ASSET_PATTERNS:
        for m in rx.finditer(text):
            kind = _kind_at(spans, m.start())
            if kind in (ds.KIND_PRODUCTION_NOTE, ds.KIND_POST_TEXT):
                continue
            facts.append(make_fact(
                m.group(0), typ, text[m.start():m.end()],
                source_file=source_file, segment_id=segment_id,
                start=m.start(), end=m.end(), section_kind=kind, confidence=_RULE_CONFIDENCE,
            ))
    return facts


def merge_semantic_facts(rule_facts, model_facts) -> list[SourceFact]:
    """并集合并：相同 fact_id 合并；同名跨类型冲突并存并标待裁决；任何一方发现都不消失。"""
    by_id: dict[str, SourceFact] = {}
    for f in list(rule_facts) + list(model_facts):
        by_id.setdefault(f["fact_id"], dict(f))
    merged = list(by_id.values())

    types_by_name: dict[str, set] = {}
    for f in merged:
        types_by_name.setdefault(f["candidate_name"], set()).add(f["candidate_type"])
    for f in merged:
        if len(types_by_name[f["candidate_name"]]) > 1:
            f["pending_adjudication"] = True
            f["confidence"] = min(f.get("confidence", 0.5), 0.4)
    return merged


def dump_source_facts_jsonl(facts, out_path) -> int:
    """把事实并集落盘为 .process/source-facts.jsonl（每行一条，UTF-8，稳定顺序）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(facts, key=lambda f: (f["source_file"], f["segment_id"],
                                           f["start"], f["candidate_type"], f["candidate_name"]))
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for f in ordered:
            rec = {"schema_version": SCHEMA_VERSION}
            rec.update(f)
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(p)
    return len(ordered)
