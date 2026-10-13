# -*- coding: utf-8 -*-
"""文风范例防抄守卫（纯函数，无 I/O、无状态）。

背景：为了让模型学到"笔触"，改写 Prompt 开始注入用户索引库里的**真实网文片段**
作为风格范例。范例一旦可进 prompt，就存在被整句照抄的风险——那既是抄袭，
也会把别的小说的句子搬进成品。本模块用 n-gram 重合检测把这件事拦住。

判定口径：
- 只在**归一化后**（去空白与标点）做比对，避免排版差异造成漏检；
- 连续重合长度 ≥ ``n``（默认 12 个实义字符）才算命中，短共现（成语、常见短语）不报；
- 返回可定位的原文区间，供检查报告与人工复核使用。
"""
from __future__ import annotations

from typing import Iterable, Sequence

DEFAULT_NGRAM = 12
"""连续重合多少字算抄。12 个实义字已远超常见短语的自然共现长度。"""

_PUNCT = set(
    "，。、；：！？…—～·「」『』（）〈〉《》【】〔〕“”‘’\"'(),.;:!?<>[]{}"
    "-_=+*/\\|@#$%^&*~`\u3000"
)


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """返回 (归一化文本, 归一化字符 → 原串下标 的映射)。"""
    chars: list[str] = []
    positions: list[int] = []
    for index, ch in enumerate(text):
        if ch.isspace() or ch in _PUNCT:
            continue
        chars.append(ch)
        positions.append(index)
    return "".join(chars), positions


def _shingles(normalized: str, n: int) -> set[str]:
    if n <= 0 or len(normalized) < n:
        return set()
    return {normalized[i : i + n] for i in range(len(normalized) - n + 1)}


def find_verbatim_spans(
    output: str,
    exemplars: Sequence[str],
    *,
    n: int = DEFAULT_NGRAM,
    limit: int = 20,
) -> list[dict]:
    """找出 output 中与任一 exemplar 连续重合 ≥ n 字的片段。

    返回 ``[{text, start, end, chars, exemplar_index}]``，按出现位置排序。
    ``start``/``end`` 是 **output 原串**下标（已还原标点与空白）。
    """
    if not output or n <= 0:
        return []
    haystack, positions = _normalize_with_map(output)
    if len(haystack) < n:
        return []

    match_mask = bytearray(len(haystack))
    owners: dict[int, int] = {}
    for exemplar_index, exemplar in enumerate(exemplars):
        if not exemplar:
            continue
        pool = _shingles(_normalize_with_map(exemplar)[0], n)
        if not pool:
            continue
        for i in range(len(haystack) - n + 1):
            if match_mask[i]:
                continue
            if haystack[i : i + n] in pool:
                match_mask[i] = 1
                owners[i] = exemplar_index

    spans: list[dict] = []
    i = 0
    while i < len(match_mask):
        if not match_mask[i]:
            i += 1
            continue
        start = i
        owner = owners.get(i, -1)
        while i < len(match_mask) and match_mask[i]:
            i += 1
        end = i  # 归一化坐标，半开
        spans.append(
            {
                "text": output[positions[start] : positions[end - 1] + 1],
                "start": positions[start],
                "end": positions[end - 1] + 1,
                "chars": end - start,
                "exemplar_index": owner,
            }
        )
        if len(spans) >= limit:
            break

    spans.sort(key=lambda item: -item["chars"])
    return spans


def copy_report(
    output: str,
    exemplars: Sequence[str],
    *,
    n: int = DEFAULT_NGRAM,
) -> dict:
    """给检查报告用的摘要：命中的连续重合是否达到"必须拦"的门槛。"""
    spans = find_verbatim_spans(output, exemplars, n=n)
    longest = max((span["chars"] for span in spans), default=0)
    return {
        "verbatim_spans": spans,
        "longest_overlap_chars": longest,
        "threshold": n,
        "has_copy": bool(spans),
    }


def exemplars_from_parts(
    parts: Iterable[str],
    *,
    header: str = "【文风范例",
    trailer: str = "请输出这些段落的本地化结果",
) -> list[str]:
    """从已拼好的参考文本里回收范例正文（供 check 阶段复用，无需再读库）。

    注意：调用方传进来的可能是**整块 user prompt**，范例块未必位于开头
    （前面还有源稿段落、事件图、其它参考资产），因此按**行**定位头行，
    而不是对整个字符串做 ``startswith``——早前用 ``startswith`` 导致范例
    永远回收为空、防抄门恒为"不适用"。

    结束条件有两类：下一个块头（``【…】``）或 prompt 尾部的输出指令行。
    否则会把"请输出这些段落的本地化结果"当作范例正文一起回收，
    使防抄阈值被无关文本稀释。
    """
    out: list[str] = []
    for part in parts:
        text = str(part)
        if header not in text:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not line.startswith(header):
                continue
            body: list[str] = []
            for following in lines[index + 1 :]:
                stripped = following.strip()
                if stripped.startswith("【") and stripped.endswith("】"):
                    break
                if trailer and stripped.startswith(trailer):
                    break
                if stripped:
                    body.append(stripped)
            if body:
                out.append("\n".join(body))
    return out
