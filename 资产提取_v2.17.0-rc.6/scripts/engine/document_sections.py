# -*- coding: utf-8 -*-
"""文档段落分类（工单 1）：把小说/剧本原文按语义切成带字符范围的段落片段。

纯确定性、可复算：对白 / 场景头 / 动作描述 / 制作说明 / 后期文字 / 叙述。
制作说明与后期文字各自独立成段，供源事实层区分"约束/后期元素"与"正式资产候选"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 后期文字标记（字幕/片头/片尾/预告/画外音/界面文字等）
POST_TEXT_RE = re.compile(r"[【\[(]?\s*(字幕|片头|片尾|预告|画外音|旁白|界面文字|上集回顾|下集预告)")
# 制作说明（提供约束，不得直接生成正式资产）
PRODUCTION_NOTE_RE = re.compile(r"[【\[(]?\s*制作说明")
# 场景/章节头
SCENE_HEADING_RE = re.compile(r"^\s*(第[一二三四五六七八九十0-9]+[章场回]|场景[：:]|INT\.|EXT\.|【场景[:：])")
# 对白：短主体名 + 冒号
DIALOGUE_RE = re.compile(r"^\s*[\u4e00-\u9fa5A-Za-z0-9（）()·]{1,8}[：:]")

KIND_PRODUCTION_NOTE = "production_note"
KIND_POST_TEXT = "post_text"
KIND_SCENE_HEADING = "scene_heading"
KIND_DIALOGUE = "dialogue"
KIND_ACTION = "action"


@dataclass
class SectionSpan:
    kind: str
    start: int
    end: int
    text: str


def classify_line(line: str) -> str:
    if PRODUCTION_NOTE_RE.search(line):
        return KIND_PRODUCTION_NOTE
    if POST_TEXT_RE.search(line):
        return KIND_POST_TEXT
    if SCENE_HEADING_RE.search(line.strip()):
        return KIND_SCENE_HEADING
    if DIALOGUE_RE.match(line) and not line.strip().startswith("【"):
        return KIND_DIALOGUE
    return KIND_ACTION


def classify_sections(text: str) -> list[SectionSpan]:
    """按行切段并保留精确字符范围（start 含、end 不含，span.text 不含换行）。"""
    spans: list[SectionSpan] = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        if body.strip():
            spans.append(SectionSpan(classify_line(body), offset, offset + len(body), body))
        offset += len(raw)
    return spans
