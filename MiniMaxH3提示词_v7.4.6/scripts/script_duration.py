#!/usr/bin/env python3
"""Authoritative screenplay target-duration parsing for MiniMax H3 Craft.

The screenplay header declaration (``精准时长``/``总时长``/``时长``) is the ONLY
authoritative target duration. Internal scene timecodes are parsed for warnings
and re-allocation evidence; they never override the header.

Fail-Closed rules:

1. No positive, exactly-convertible declaration -> ``MissingScriptDurationError``.
   The end of the last scene timecode is NOT a silent fallback.
2. Only exactly convertible forms are accepted (seconds, minutes, minutes plus
   seconds, clock). A declaration whose number continues past the accepted form
   (``2.5分钟``, ``1分钟30.5秒``, ``01:30:5``, ``90秒+15秒``) is blocked instead of
   being truncated to its first term.
3. Only script-level declarations count. A keyword glued into a longer word
   (``每段时长：5秒``) or sitting in a markdown table cell (``| 时长：3秒 |``)
   describes a shot, not the screenplay target.
4. The earliest script-level declaration wins; later conflicting declarations
   never silently take over.
5. Timecode lookalikes (``【环境光影】``, ``[镜头 1]``) are never read as segments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

MISSING_SCRIPT_DURATION = "MISSING_SCRIPT_DURATION"
INTERNAL_TIMECODE_MISMATCH = "INTERNAL_TIMECODE_MISMATCH"

_DECLARATION_KEYWORDS = r"(?:精准时长|总时长|时长)"
# A script-level declaration opens a line or follows whitespace/decoration; it is
# never glued onto a preceding word char, because `每段时长` / `参考宣传片时长`
# describe some other length. `**精准时长**：120秒`, `## 总时长：120秒` and
# `- 时长: 90 秒` all satisfy this head.
_DECLARATION_HEAD = r"(?:^|(?<=[\s*#>\-]))"
# Between keyword and number only horizontal space and decorative `*`/colons are
# tolerated, so a number can never be carved out of a longer numeric token:
# `2.5分钟` yields no match at all (it does not degrade to `5分钟`), and `1:30`
# is never read out of `1:30:00`.
_DECLARATION_PREFIX = rf"{_DECLARATION_HEAD}{_DECLARATION_KEYWORDS}[ \t]*[*：:]*[ \t]*"

_CLOCK_SEPARATOR = r"[：:]"
_TIME_TOKEN = rf"(?:\d{{1,3}}{_CLOCK_SEPARATOR}\d{{2}}{_CLOCK_SEPARATOR}\d{{2}}|\d{{1,3}}{_CLOCK_SEPARATOR}\d{{2}})"

# Declaration forms, most specific first: an earlier entry wins when two forms
# match at the same position (`1分钟30秒` must not be read as `1分钟`).
_HEADER_PATTERNS: Tuple["re.Pattern[str]", ...] = (
    re.compile(rf"{_DECLARATION_PREFIX}(\d{{1,2}}){_CLOCK_SEPARATOR}(\d{{2}}){_CLOCK_SEPARATOR}(\d{{2}})", re.MULTILINE),
    re.compile(rf"{_DECLARATION_PREFIX}(\d+)\s*分钟\s*(\d+)\s*秒", re.MULTILINE),
    re.compile(rf"{_DECLARATION_PREFIX}(\d+)\s*秒", re.MULTILINE),
    re.compile(rf"{_DECLARATION_PREFIX}(\d+)\s*分钟", re.MULTILINE),
    re.compile(rf"{_DECLARATION_PREFIX}(\d{{1,2}}){_CLOCK_SEPARATOR}(\d{{2}})", re.MULTILINE),
)

# What may follow a complete declaration. Anything in this set means the number
# keeps going (`1分钟30.5秒`, `90秒+15秒`, `01:30:5`, `2分钟半`), so the form is not
# exactly convertible and must not be truncated to its first term.
_DECLARATION_CONTINUATION = re.compile(r"[ \t]*[0-9.:+\-–—零加半又]")

_SEGMENT_PATTERN = re.compile(
    rf"[【\[（(]\s*(?P<start>{_TIME_TOKEN})\s*[-–—~～]\s*(?P<end>{_TIME_TOKEN})\s*[】\]）)]"
)

_LINE_DECORATION = "#* \t\r"


class MissingScriptDurationError(RuntimeError):
    """Raised when a screenplay declares no usable target duration."""

    pass


@dataclass(frozen=True)
class TimeSegment:
    """One internal screenplay timecode range, in seconds."""

    start_seconds: int
    end_seconds: int
    label: str


@dataclass(frozen=True)
class ScriptDurationContract:
    """Authoritative target duration plus internal timecode evidence."""

    target_seconds: int
    internal_segments: Tuple[TimeSegment, ...]
    warnings: Tuple[str, ...]


def _time_token_to_seconds(token: str) -> int:
    """Convert ``MM:SS`` / ``HH:MM:SS`` (half- or full-width colon) to seconds."""
    parts = [int(part) for part in re.split(_CLOCK_SEPARATOR, token)]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


def _match_to_seconds(kind: int, match: "re.Match[str]") -> int:
    """Convert a declaration match to seconds, keyed by its pattern kind."""
    if kind == 0:
        return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
    if kind == 1:
        return int(match.group(1)) * 60 + int(match.group(2))
    if kind == 2:
        return int(match.group(1))
    if kind == 3:
        return int(match.group(1)) * 60
    return int(match.group(1)) * 60 + int(match.group(2))


def _is_usable_declaration(text: str, match: "re.Match[str]") -> bool:
    """True only for a script-level, exactly-convertible declaration match.

    Two matches describe something other than the screenplay target and are
    therefore skipped rather than read as the target:

    * a markdown table cell, where ``| 时长：3秒 |`` is a per-shot row;
    * a declaration whose number continues past the accepted form, where reading
      the first term would silently understate the length.
    """
    line_start = text.rfind("\n", 0, match.start()) + 1
    if "|" in text[line_start:match.start()]:
        return False
    return _DECLARATION_CONTINUATION.match(text, match.end()) is None


def _locate_declaration(text: str) -> Optional[int]:
    """Return the value of the earliest script-level declaration, or None.

    Document order is decisive: a later, more specific declaration never
    overrides the header line the screenplay actually opens with. Pattern order
    only breaks ties at the same offset.
    """
    best: Optional[Tuple[Tuple[int, int], int]] = None
    for kind, pattern in enumerate(_HEADER_PATTERNS):
        for match in pattern.finditer(text):
            if not _is_usable_declaration(text, match):
                continue
            candidate = ((match.start(), kind), _match_to_seconds(kind, match))
            if best is None or candidate[0] < best[0]:
                best = candidate
    return None if best is None else best[1]


def _segment_label(text: str, match: "re.Match[str]") -> str:
    line_end = text.find("\n", match.end())
    if line_end == -1:
        line_end = len(text)
    after = text[match.end():line_end].strip(_LINE_DECORATION)
    if after:
        return after
    line_start = text.rfind("\n", 0, match.start()) + 1
    return text[line_start:match.start()].strip(_LINE_DECORATION)


def _parse_internal_timecodes(text: str) -> Tuple[TimeSegment, ...]:
    """Collect bracket timecode ranges in document order, with line labels."""
    return tuple(
        TimeSegment(
            _time_token_to_seconds(match.group("start")),
            _time_token_to_seconds(match.group("end")),
            _segment_label(text, match),
        )
        for match in _SEGMENT_PATTERN.finditer(text)
    )


def parse_script_duration(text: str) -> ScriptDurationContract:
    """Parse the authoritative target duration and internal timecode evidence.

    Args:
        text: Full screenplay text.

    Returns:
        ScriptDurationContract: ``target_seconds`` (header declaration),
        ``internal_segments`` (document-order timecodes), and non-blocking
        ``warnings``.

    Raises:
        MissingScriptDurationError: If ``text`` is not a string, or declares no
            positive exactly-convertible target duration.
    """
    if not isinstance(text, str):
        raise MissingScriptDurationError(
            f"{MISSING_SCRIPT_DURATION}: screenplay must be text, got {type(text).__name__}"
        )

    declared = _locate_declaration(text)
    if declared is None:
        raise MissingScriptDurationError(
            f"{MISSING_SCRIPT_DURATION}: no 精准时长/总时长/时长 declaration found; "
            "accepted forms are 'X秒', 'X分钟', 'X分钟Y秒', 'MM:SS' and 'HH:MM:SS' "
            "(for example '**精准时长**：120秒'). Invent nothing — ask the user for "
            "the screenplay target duration"
        )
    if declared <= 0:
        raise MissingScriptDurationError(
            f"{MISSING_SCRIPT_DURATION}: declared target duration must be positive, "
            f"got {declared} seconds"
        )

    segments = tuple(_parse_internal_timecodes(text))
    warnings = []
    if segments and (
        segments[0].start_seconds != 0 or segments[-1].end_seconds != declared
    ):
        warnings.append(INTERNAL_TIMECODE_MISMATCH)

    return ScriptDurationContract(declared, segments, tuple(warnings))
