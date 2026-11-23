# -*- coding: utf-8 -*-
"""段产物格式解析的单一真相源（加速改法工单 T8）。

- 三张表的**正则口径原样**自 `scripts/aggregate.py` 迁入：改这里 = 改聚合与段级，
  不允许任何一侧再自带一份正则。
- `parse_summary_rows` 供段级生成后就地自检：出现「看起来是表格行、却不符合三张表
  任何一张口径」的行（典型：章节列没以「第」开头）→ 立即抛 `SegmentFormatError`，
  不让坏格式流向聚合阶段。
"""
from __future__ import annotations

import re
from typing import Optional

SUMMARY_RE = re.compile(r"^\|\s*(第[^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|")
RECORD_RE = re.compile(r"^\|\s*(人物|场景|道具|服装)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|")
NEED_RE = re.compile(r"^\|\s*(场景|道具)\s*\|\s*([^|]+?)\s*\|\s*([^|\s]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|")

HEADER_HINTS = ("章节", "概要", "别名", "摘句", "类型", "名称", "行号", "备注", "服装", "需求")


class SegmentFormatError(RuntimeError):
    """段产物格式不符（稳定原因码），点名段 ID，不进入聚合。"""

    code = "CHAPTER_COLUMN_INVALID"

    def __init__(self, detail: str = "", code: str = "CHAPTER_COLUMN_INVALID"):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


def classify_row(line: str) -> tuple[str, Optional[dict]]:
    """按三张表口径归类一行。返回 (kind, row)；kind ∈ summary|record|need|unknown。"""
    m = SUMMARY_RE.match(line)
    if m:
        return "summary", {
            "章节": m.group(1).strip(),
            "概要": m.group(2).strip(),
            "人物": m.group(3).strip(),
            "场景": m.group(4).strip(),
            "道具": m.group(5).strip(),
            "服装": m.group(6).strip(),
        }
    m = RECORD_RE.match(line)
    if m:
        return "record", {
            "类型": m.group(1).strip(),
            "名称": m.group(2).strip(),
            "别名": m.group(3).strip(),
            "出现章节": m.group(4).strip(),
            "摘句": m.group(5).strip(),
            "行号": m.group(6).strip(),
            "备注": m.group(7).strip(),
        }
    m = NEED_RE.match(line)
    if m:
        return "need", {
            "类型": m.group(1).strip(),
            "资产": m.group(2).strip(),
            "需求": m.group(3).strip(),
            "章节": m.group(4).strip(),
            "摘句": m.group(5).strip(),
            "行号": m.group(6).strip(),
        }
    return "unknown", None


def parse_summary_rows(text: str) -> list[dict]:
    """解析段产物三张表的行；遇到不合规的表格行立即抛 SegmentFormatError。"""
    rows: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not line.startswith("|") or line.startswith("|---"):
            continue
        kind, row = classify_row(line)
        if kind != "unknown":
            rows.append(dict(row, kind=kind))
            continue
        if any(h in line[:30] for h in HEADER_HINTS):
            continue                      # 表头行（与聚合既有口径一致）
        raise SegmentFormatError(f"表格行不符合三张表口径: {line[:60]}")
    return rows
