#!/usr/bin/env python3
"""Strict Ref2VA and Base English Prompt Parser."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ShotBlock:
    index: int
    raw_header: str
    timestamp_str: str | None = None
    timestamp_ms: int | None = None
    content: str = ""
    line_number: int = 1


@dataclass
class AssetRef:
    kind: str  # Subject, Picture, Video, Audio
    index: int
    raw_tag: str
    defined_line: int | None = None
    defined_desc: str | None = None
    appearances: list[int] = field(default_factory=list)  # Shot indices


@dataclass
class ParsedRef2VA:
    raw_text: str
    sections: dict[str, str] = field(default_factory=dict)
    section_lines: dict[str, int] = field(default_factory=dict)
    task_types: list[str] = field(default_factory=list)
    summary_text: str = ""
    shots: list[ShotBlock] = field(default_factory=list)
    style_preamble: str = ""
    defined_assets: dict[str, AssetRef] = field(default_factory=dict)
    used_tags: list[tuple[str, int, str]] = field(default_factory=list)  # (tag, line, section)
    dialogue_tags: list[dict[str, Any]] = field(default_factory=list)


REF2VA_SECTIONS = [
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
]


def parse_timestamp_ms(ts_str: str) -> int | None:
    """Parse MM:SS.mmm to milliseconds."""
    m = re.match(r"^(\d{2}):(\d{2})\.(\d{3})$", ts_str.strip())
    if not m:
        return None
    mins, secs, ms = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return (mins * 60 + secs) * 1000 + ms


def parse_ref2va_prompt(text: str) -> ParsedRef2VA:
    """Parse Ref2VA English prompt into structured components."""
    parsed = ParsedRef2VA(raw_text=text)

    # 1. Section extraction
    # Regex matching exact field names at start of line
    pattern = re.compile(
        r"(?m)^(subject_definitions|summary|retention_analysis|detailed_description|overall_soundscape|non_diegetic_music):\s*"
    )
    matches = list(pattern.finditer(text))

    for i, m in enumerate(matches):
        sec_name = m.group(1)
        start_idx = m.end()
        end_idx = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sec_content = text[start_idx:end_idx].strip()
        parsed.sections[sec_name] = sec_content
        # Calculate line number
        line_no = text[: m.start()].count("\n") + 1
        parsed.section_lines[sec_name] = line_no

    # 2. Extract defined assets from subject_definitions
    if "subject_definitions" in parsed.sections:
        sub_def_text = parsed.sections["subject_definitions"]
        sub_line_offset = parsed.section_lines.get("subject_definitions", 1)
        for line_idx, line in enumerate(sub_def_text.splitlines()):
            line_str = line.strip()
            curr_line = sub_line_offset + line_idx + 1
            # Matches <Subject 1>, <Picture 1>, <Video 1>, <Audio 1>
            for m in re.finditer(r"<((?:Subject|Picture|Video|Audio))\s+([1-9]\d*)>", line_str):
                kind = m.group(1)
                idx = int(m.group(2))
                tag = f"<{kind} {idx}>"
                if tag not in parsed.defined_assets:
                    parsed.defined_assets[tag] = AssetRef(
                        kind=kind,
                        index=idx,
                        raw_tag=tag,
                        defined_line=curr_line,
                        defined_desc=line_str,
                    )

    # 3. Extract task types from summary
    if "summary" in parsed.sections:
        summary_text = parsed.sections["summary"]
        sm = re.match(r"^\[([^\]]+)\]\s*(.*)$", summary_text, flags=re.DOTALL)
        if sm:
            parsed.task_types = [t.strip() for t in sm.group(1).split("+")]
            parsed.summary_text = sm.group(2).strip()
        else:
            parsed.summary_text = summary_text

    # 4. Extract shots from detailed_description
    if "detailed_description" in parsed.sections:
        desc = parsed.sections["detailed_description"]
        desc_line_offset = parsed.section_lines.get("detailed_description", 1)

        shot_pattern = re.compile(r"(?m)^\[Shot\s+(\d+)\]\s*(.*)$")
        shot_matches = list(shot_pattern.finditer(desc))

        if shot_matches:
            # Preamble before Shot 1
            first_shot_start = shot_matches[0].start()
            parsed.style_preamble = desc[:first_shot_start].strip()

            for i, sm in enumerate(shot_matches):
                shot_idx = int(sm.group(1))
                line_rest = sm.group(2).strip()
                block_start = sm.start()
                block_end = shot_matches[i + 1].start() if i + 1 < len(shot_matches) else len(desc)
                shot_content = desc[block_start:block_end].strip()
                shot_line = desc_line_offset + desc[:block_start].count("\n")

                # Timestamp check on header
                # Expected formats:
                # Shot 1: [Shot 1] ... (no timestamp)
                # Shot N: [Shot N] At MM:SS.mmm, ...
                ts_m = re.match(r"^At\s+(\d{2}:\d{2}\.\d{3}),?\s*(.*)$", line_rest)
                if ts_m:
                    ts_str = ts_m.group(1)
                    ts_ms = parse_timestamp_ms(ts_str)
                else:
                    ts_str = None
                    ts_ms = None

                parsed.shots.append(
                    ShotBlock(
                        index=shot_idx,
                        raw_header=sm.group(0),
                        timestamp_str=ts_str,
                        timestamp_ms=ts_ms,
                        content=shot_content,
                        line_number=shot_line,
                    )
                )

    # 5. Extract all used tags across sections
    for sec_name, sec_content in parsed.sections.items():
        base_line = parsed.section_lines.get(sec_name, 1)
        for m in re.finditer(r"<((?:Subject|Picture|Video|Audio))\s+([1-9]\d*)>", sec_content):
            tag = f"<{m.group(1)} {m.group(2)}>"
            line_no = base_line + sec_content[: m.start()].count("\n")
            parsed.used_tags.append((tag, line_no, sec_name))

    # 6. Extract dialogue tags <d>
    full_text = parsed.raw_text
    d_open = list(re.finditer(r"<d>", full_text))
    d_close = list(re.finditer(r"</d>", full_text))
    parsed.dialogue_tags.append({"open_count": len(d_open), "close_count": len(d_close)})

    return parsed
