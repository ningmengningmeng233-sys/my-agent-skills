#!/usr/bin/env python3
"""Strict Chinese Native Prompt Parser for general and drama-series profiles."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class PictureRef:
    index: int
    raw_line: str
    asset_name: str
    has_attribute: bool
    is_grid: bool
    line_number: int


@dataclass
class ParsedNative:
    raw_text: str
    non_empty_lines: list[tuple[int, str]] = field(default_factory=list)  # (line_no, line_str)
    opening_line: str = ""
    picture_refs: list[PictureRef] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)
    section_lines: dict[str, int] = field(default_factory=dict)
    section_order: list[str] = field(default_factory=list)
    soundscape_clean: str = ""
    dialogues: list[str] = field(default_factory=list)
    voice_roles: list[str] = field(default_factory=list)


DRAMA_REQUIRED_SECTIONS = [
    "【环境光影】",
    "【空间基准】",
    "【画面演进】",
    "【声音设计】",
    "【画面风格】",
]


def parse_native_prompt(text: str) -> ParsedNative:
    parsed = ParsedNative(raw_text=text)

    # 1. Non-empty lines with original line numbers
    for idx, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped:
            parsed.non_empty_lines.append((idx + 1, stripped))

    # 2. Extract sections
    pattern = re.compile(
        r"(?m)^[^\S\r\n]*(【(?:环境光影|空间基准|角色声音签名|画面演进|声音设计|画面风格)[^】\r\n]*】)\s*"
    )
    matches = list(pattern.finditer(text))
    for i, m in enumerate(matches):
        raw_name = m.group(1)
        # Normalize sec_name to the canonical header if it matches known prefix
        sec_name = raw_name
        for canon in [
            "【环境光影】",
            "【空间基准】",
            "【角色声音签名】",
            "【画面演进】",
            "【声音设计】",
            "【画面风格】",
        ]:
            if canon.strip("【】") in raw_name:
                sec_name = canon
                break
        start_idx = m.end()
        end_idx = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sec_content = text[start_idx:end_idx].strip()
        parsed.sections[sec_name] = sec_content
        parsed.section_order.append(sec_name)
        line_no = text[: m.start()].count("\n") + 1
        parsed.section_lines[sec_name] = line_no

    # 3. Opening line: first non-empty line that isn't a picture line
    for line_no, line_str in parsed.non_empty_lines:
        if not re.match(r"^图[1-9]\d*\s*[:：=]", line_str):
            parsed.opening_line = line_str
            break

    # 4. Picture references
    for line_no, line_str in parsed.non_empty_lines:
        m = re.match(r"^图([1-9]\d*)\s*[:：=]\s*(.+)$", line_str)
        if m:
            idx = int(m.group(1))
            rest = m.group(2).strip()
            is_grid = any(k in rest for k in ["宫格", "九宫格", "分镜表", "STORYBOARD"])
            # Check for visual attributes
            # Attribute must describe observable features: clothing, color, material, weapon, pose, face, etc.
            has_attr = bool(re.search(
                r"(?:锁定|保持|特征|身穿|手持|面戴|发型|劲装|长袍|鳞甲|战甲|武器|短剑|刀柄|背负|色调|质感|骨质|[，,;；]\s*[^\n]{2,})",
                rest,
            ))
            # Extract asset name (part before comma or delimiter)
            name_m = re.split(r"[，,;；=:]", rest)
            asset_name = name_m[0].strip() if name_m else rest

            parsed.picture_refs.append(
                PictureRef(
                    index=idx,
                    raw_line=line_str,
                    asset_name=asset_name,
                    has_attribute=has_attr,
                    is_grid=is_grid,
                    line_number=line_no,
                )
            )

    # 5. Extract voice roles
    if "【角色声音签名】" in parsed.sections:
        vs = parsed.sections["【角色声音签名】"]
        parsed.voice_roles = [
            m.strip()
            for m in re.findall(r"(?m)^([^\n：:]{1,12})[：:]", vs)
            if m.strip()
        ]

    # 6. Extract dialogues
    if "【画面演进】" in parsed.sections:
        ev = parsed.sections["【画面演进】"]
        parsed.dialogues = re.findall(r"[“\"]([^”\"]+)[”\"]", ev)

    return parsed
