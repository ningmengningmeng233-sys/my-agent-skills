#!/usr/bin/env python3
"""Ref2VA English Verification Rules implementing P0-01 ~ P0-07."""

from __future__ import annotations

import re
from parser_ref2va import ParsedRef2VA, REF2VA_SECTIONS, parse_ref2va_prompt
from result import RuleResult

# Official allowed task types
OFFICIAL_TASK_TYPES = {
    "keyframe completion",
    "reference generation",
    "video editing",
    "video continuation",
    "audio reuse",
    "audio reference",
}

# Strict marker categories (P0-04)
VISUAL_MARKERS = {
    "fully_preserved",
    "partially_preserved",
    "attribute_transfer",
    "weak_reference",
}

AUDIO_MARKERS = {
    "fully_copy",
    "partially_copy",
    "reference",
    "weak_reference",
}


def rule_r_e00_sections(parsed: ParsedRef2VA) -> RuleResult:
    """Check that all 6 sections exist exactly once in order."""
    text = parsed.raw_text
    found_order = []
    pattern = re.compile(
        r"(?m)^(subject_definitions|summary|retention_analysis|detailed_description|overall_soundscape|non_diegetic_music):\s*"
    )
    for m in pattern.finditer(text):
        found_order.append(m.group(1))

    if found_order != REF2VA_SECTIONS:
        return RuleResult(
            rule_id="R-E00",
            status="FAIL",
            message=f"Sections must match exact order {REF2VA_SECTIONS}, found: {found_order}",
            evidence=str(found_order),
        )
    return RuleResult(
        rule_id="R-E00",
        status="PASS",
        message="All six sections present and in correct order",
    )


def rule_r_e01_language(parsed: ParsedRef2VA) -> RuleResult:
    """Check body is English except dialogue <d> and lyrics."""
    text = parsed.raw_text
    # Strip <d>...</d> and quoted strings
    clean = re.sub(r"<d>.*?</d>", "", text, flags=re.DOTALL)
    clean = re.sub(r'"[^"\n]*"', "", clean)
    zh_chars = re.findall(r"[\u4e00-\u9fff]", clean)
    if zh_chars:
        return RuleResult(
            rule_id="R-E01",
            status="FAIL",
            message=f"Forbidden non-English characters found in body: {zh_chars[:5]}",
            evidence="".join(zh_chars[:10]),
        )
    return RuleResult(
        rule_id="R-E01",
        status="PASS",
        message="Language conforms to English specification",
    )


def rule_r_e02_asset_lifecycle(parsed: ParsedRef2VA) -> RuleResult:
    """P0-03: Full lifecycle check for Subject, Picture, Video, Audio tags."""
    defined = parsed.defined_assets

    # 1. Check all used tags are defined
    for tag, line, sec in parsed.used_tags:
        if sec == "subject_definitions":
            continue
        if tag not in defined:
            return RuleResult(
                rule_id="R-E02",
                status="FAIL",
                message=f"Undefined asset reference {tag} used in section {sec} at line {line}",
                evidence=f"{tag} in {sec}",
                line=line,
            )

    # 2. Check each defined tag is used at least once in later sections
    used_tags_set = {t[0] for t in parsed.used_tags if t[2] != "subject_definitions"}
    for tag, asset in defined.items():
        if tag not in used_tags_set:
            return RuleResult(
                rule_id="R-E02",
                status="WARN",
                message=f"Asset {tag} defined on line {asset.defined_line} is never referenced in subsequent sections",
                evidence=tag,
                line=asset.defined_line,
            )

    # Check consecutive 1..N numbering for each asset kind
    from collections import defaultdict
    indices_by_kind = defaultdict(list)
    for tag, asset in defined.items():
        indices_by_kind[asset.kind].append(asset.index)

    for kind, indices in indices_by_kind.items():
        sorted_indices = sorted(indices)
        expected = list(range(1, len(sorted_indices) + 1))
        if sorted_indices != expected:
            return RuleResult(
                rule_id="R-E02",
                status="FAIL",
                message=f"Asset indices for <{kind}> must be strictly consecutive 1..N without gaps, got: {sorted_indices}",
                evidence=str(sorted_indices),
            )

    return RuleResult(
        rule_id="R-E02",
        status="PASS",
        message=f"All {len(defined)} defined assets follow clean definition-usage lifecycle and consecutive numbering",
    )


def rule_r_e03_task_types(parsed: ParsedRef2VA) -> RuleResult:
    """P0-05: Task types check: official validity and duplicate detection."""
    task_types = parsed.task_types
    if not task_types:
        return RuleResult(
            rule_id="R-E03",
            status="FAIL",
            message="summary must begin with a bracketed task type list like [reference generation]",
        )

    # Check duplicates
    if len(task_types) != len(set(task_types)):
        return RuleResult(
            rule_id="R-E03",
            status="FAIL",
            message=f"Duplicate task types in summary: {task_types}",
            evidence=str(task_types),
        )

    # Check validity
    for t in task_types:
        if t not in OFFICIAL_TASK_TYPES:
            return RuleResult(
                rule_id="R-E03",
                status="FAIL",
                message=f"Invalid task type '{t}'. Must be one of {sorted(OFFICIAL_TASK_TYPES)}",
                evidence=t,
            )

    return RuleResult(
        rule_id="R-E03",
        status="PASS",
        message=f"Task types valid and deduplicated: {task_types}",
    )


def rule_r_e04_retention_markers(parsed: ParsedRef2VA) -> RuleResult:
    """P0-04: Strict retention marker differentiation by asset type."""
    retention_text = parsed.sections.get("retention_analysis", "")
    lines = [l.strip() for l in retention_text.splitlines() if l.strip()]

    for line_idx, line in enumerate(lines):
        # Match asset tag at start of line
        m = re.match(r"^<((?:Subject|Picture|Video|Audio))\s+([1-9]\d*)>", line)
        if not m:
            continue
        kind = m.group(1)
        tag = f"<{kind} {m.group(2)}>"

        # Find which marker appears
        if kind in ("Subject", "Picture", "Video"):
            valid_set = VISUAL_MARKERS
            invalid_conflict = AUDIO_MARKERS - VISUAL_MARKERS
        else:  # Audio
            valid_set = AUDIO_MARKERS
            invalid_conflict = VISUAL_MARKERS - AUDIO_MARKERS

        has_valid = any(re.search(rf"\b{re.escape(k)}\b", line) for k in valid_set)
        has_invalid = any(re.search(rf"\b{re.escape(k)}\b", line) for k in invalid_conflict)

        if has_invalid:
            return RuleResult(
                rule_id="R-E04",
                status="FAIL",
                message=f"Asset {tag} uses illegal marker for its category in line: '{line}'",
                evidence=line,
            )
        if not has_valid:
            return RuleResult(
                rule_id="R-E04",
                status="FAIL",
                message=f"Asset {tag} in retention_analysis missing valid category marker in line: '{line}'",
                evidence=line,
            )

    return RuleResult(
        rule_id="R-E04",
        status="PASS",
        message="Retention markers strictly partitioned and valid by asset category",
    )


def rule_r_e05_timeline_and_shots(parsed: ParsedRef2VA, max_duration_ms: int | None = None) -> RuleResult:
    """P0-01, P0-02: Complete AST timeline validation."""
    desc = parsed.sections.get("detailed_description", "")
    if not desc:
        return RuleResult(rule_id="R-E05", status="FAIL", message="detailed_description section is missing or empty")

    # Global custom time window check: e.g. [0s-2s]
    if re.search(r"\[\d+s?[-~]\d+s?\]", parsed.raw_text) or re.search(r"\[\d+:\d+[-~]\d+:\d+\]", parsed.raw_text):
        return RuleResult(
            rule_id="R-E05",
            status="FAIL",
            message="Custom bracketed time windows like [0s-2s] are forbidden across all sections",
        )

    shots = parsed.shots
    if not shots:
        return RuleResult(rule_id="R-E05", status="FAIL", message="detailed_description contains no [Shot N] blocks")

    # Style preamble check before Shot 1
    if not parsed.style_preamble:
        return RuleResult(
            rule_id="R-E05",
            status="FAIL",
            message="detailed_description must include global stylistic description before [Shot 1]",
        )

    # Validate shot numbering sequence: must be 1..len(shots)
    for i, shot in enumerate(shots):
        expected_idx = i + 1
        if shot.index != expected_idx:
            return RuleResult(
                rule_id="R-E05",
                status="FAIL",
                message=f"Shot index sequence jump: expected [Shot {expected_idx}], got [Shot {shot.index}]",
                evidence=f"Shot {shot.index}",
                line=shot.line_number,
            )

    # Shot 1 check: strictly no timestamp
    shot1 = shots[0]
    if shot1.timestamp_str is not None:
        return RuleResult(
            rule_id="R-E05",
            status="FAIL",
            message=f"[Shot 1] must NOT have a timestamp, found '{shot1.timestamp_str}'",
            evidence=shot1.raw_header,
            line=shot1.line_number,
        )

    # Subsequent shots: must have strictly increasing timestamps
    prev_ms = 0
    for shot in shots[1:]:
        if shot.timestamp_str is None or shot.timestamp_ms is None:
            return RuleResult(
                rule_id="R-E05",
                status="FAIL",
                message=f"[Shot {shot.index}] must specify timestamp in format At MM:SS.mmm,",
                evidence=shot.raw_header,
                line=shot.line_number,
            )
        if shot.timestamp_ms <= prev_ms:
            return RuleResult(
                rule_id="R-E05",
                status="FAIL",
                message=f"[Shot {shot.index}] timestamp {shot.timestamp_str} ({shot.timestamp_ms}ms) not strictly increasing from previous ({prev_ms}ms)",
                evidence=shot.timestamp_str,
                line=shot.line_number,
            )
        if max_duration_ms and shot.timestamp_ms > max_duration_ms:
            return RuleResult(
                rule_id="R-E05",
                status="FAIL",
                message=f"[Shot {shot.index}] timestamp {shot.timestamp_str} exceeds maximum video duration {max_duration_ms}ms",
                evidence=shot.timestamp_str,
                line=shot.line_number,
            )
        prev_ms = shot.timestamp_ms

    return RuleResult(
        rule_id="R-E05",
        status="PASS",
        message=f"Timeline valid: {len(shots)} shots sequentially ordered and strictly increasing",
    )


def rule_r_e06_dialogue_and_tags(parsed: ParsedRef2VA) -> RuleResult:
    """P0-06: Strict dialogue tag checking."""
    text = parsed.raw_text
    open_count = text.count("<d>")
    close_count = text.count("</d>")

    if open_count != close_count:
        return RuleResult(
            rule_id="R-E06",
            status="FAIL",
            message=f"Unbalanced dialogue tags: found {open_count} '<d>' but {close_count} '</d>'",
            evidence=f"<d>: {open_count}, </d>: {close_count}",
        )

    # Check for unclosed single <d>
    pattern = re.compile(r"<d>(.*?)</d>", flags=re.DOTALL)
    for m in pattern.finditer(text):
        content = m.group(1).strip()
        if not content:
            return RuleResult(rule_id="R-E06", status="FAIL", message="Dialogue tag <d></d> cannot be empty")
        # Disallow nested <d>
        if "<d>" in content or "</d>" in content:
            return RuleResult(rule_id="R-E06", status="FAIL", message="Nested dialogue tags forbidden inside <d>")

        # P1-02 fix: Enforce valid [Language] header
        lang_m = re.match(r"^\[([A-Za-z]+)\]\s*(.+)$", content)
        if not lang_m:
            return RuleResult(
                rule_id="R-E06",
                status="FAIL",
                message=f"Dialogue payload must begin with valid [Language] header like '[English] ...', got: '{content[:30]}'",
                evidence=content,
            )
        lang_name = lang_m.group(1).capitalize()
        VALID_LANGUAGES = {"English", "Chinese", "Japanese", "Korean", "Spanish", "French", "German", "Russian", "Italian", "Portuguese"}
        if lang_name not in VALID_LANGUAGES:
            return RuleResult(
                rule_id="R-E06",
                status="FAIL",
                message=f"Unknown or unsupported dialogue language '[{lang_m.group(1)}]'",
                evidence=lang_m.group(1),
            )

    # Check off-screen voiceover lip conflict
    desc = parsed.sections.get("detailed_description", "")
    if "off-screen voiceover" in desc and "<d>" in desc:
        if not re.search(r"(?:lip|mouth|jaw)\s+(?:remains?\s+closed|does\s+not\s+move)", desc, flags=re.IGNORECASE):
            return RuleResult(
                rule_id="R-E06",
                status="FAIL",
                message="Voiceover with dialogue must explicitly specify lips/mouth remain closed to avoid lip sync conflict",
            )

    return RuleResult(
        rule_id="R-E06",
        status="PASS",
        message="Dialogue tags balanced and voiceover lip synchronization sound",
    )


def rule_r_e07_soundscape_and_silent(parsed: ParsedRef2VA, audio_policy: str = "normal") -> RuleResult:
    """P0-07: Soundscape validation supporting N/A when silent policy is requested."""
    sound = parsed.sections.get("overall_soundscape", "")
    if not sound:
        return RuleResult(rule_id="R-E07", status="FAIL", message="overall_soundscape section is missing or empty")

    sound_clean = sound.strip()

    # P0-07 Silent Policy support
    if sound_clean == "N/A":
        if audio_policy in ("silent", "user-specified"):
            return RuleResult(
                rule_id="R-E07",
                status="PASS",
                message="overall_soundscape is N/A under explicit silent audio policy",
            )
        else:
            return RuleResult(
                rule_id="R-E07",
                status="WARN",
                message="overall_soundscape is N/A; allowed only if audio policy is explicitly silent",
                evidence="N/A",
            )

    # Disallow bullet lists in soundscape
    lines = [l.strip() for l in sound.splitlines() if l.strip()]
    if any(l.startswith(("- ", "* ", "1.", "2.")) for l in lines):
        return RuleResult(
            rule_id="R-E07",
            status="FAIL",
            message="overall_soundscape must be a descriptive paragraph, not bullet points",
        )

    if len(sound.split()) < 5:
        return RuleResult(
            rule_id="R-E07",
            status="FAIL",
            message="overall_soundscape description is too short (fewer than 5 words)",
        )

    return RuleResult(
        rule_id="R-E07",
        status="PASS",
        message="overall_soundscape is a unified diegetic paragraph",
    )


def audit_ref2va_rules(text: str, audio_policy: str = "normal", max_duration_ms: int | None = None) -> list[RuleResult]:
    """Run all Ref2VA rules on the input text."""
    parsed = parse_ref2va_prompt(text)
    results = [
        rule_r_e00_sections(parsed),
        rule_r_e01_language(parsed),
        rule_r_e02_asset_lifecycle(parsed),
        rule_r_e03_task_types(parsed),
        rule_r_e04_retention_markers(parsed),
        rule_r_e05_timeline_and_shots(parsed, max_duration_ms=max_duration_ms),
        rule_r_e06_dialogue_and_tags(parsed),
        rule_r_e07_soundscape_and_silent(parsed, audio_policy=audio_policy),
    ]
    return results
