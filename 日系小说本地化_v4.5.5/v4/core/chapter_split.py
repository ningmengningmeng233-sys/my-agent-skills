"""章节边界切分。

- 显式中文章节标题优先：第X章/第X话/第X回/序章/终章/番外/间章/幕间/楔子/尾声等；
- 文件夹输入按文件名自然排序读取，但仍需识别文件内章节边界；
- 无可靠标题时只生成候选边界（``awaiting_confirmation``），等待用户确认；
  绝不静默按字数伪造章节；
- 超长章节只提供内部技术分段（句子边界优先），不创建独立事件图/质检/交付状态
  （v4 spec §6 / §7.1）。

思想复用 v3 ``split_ja_segments.py`` 的纯函数（标题识别、数字规范化、超长章内部子切），
但按 v4 契约重写，且绝不导入 v3 运行时。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

_CN_NUM = "零〇一二两三四五六七八九十百千万亿0-9０-９"
_NUMBER = f"[{_CN_NUM}]+"

# 显式章节标题标记（简体为主，兼容繁体残留）。顺序：第X章/话/回 → 第X卷 → 无编号标记。
_MARKERS = (
    "序章|終章|终章|終幕|终幕|尾声|尾聲|楔子|引子|後記|后记|番外|外传|外傳|間章|间章|幕間|幕间"
)
_SIMPLIFIED = {
    "終章": "终章",
    "終幕": "终幕",
    "尾聲": "尾声",
    "後記": "后记",
    "外傳": "外传",
    "間章": "间章",
    "幕間": "幕间",
}

_TITLE_MARKER_RE = re.compile(
    r"^[ \t]*(?:"
    r"第\s*" + _NUMBER + r"\s*[章回话节集]"
    r"|第\s*" + _NUMBER + r"\s*卷"
    r"|" + _MARKERS + r")"
)
CHAPTER_TITLE_RE = re.compile(
    r"^[ \t]*(?:"
    r"第\s*" + _NUMBER + r"\s*[章回话节集][ \t]*[^\n]{0,60}"
    r"|第\s*" + _NUMBER + r"\s*卷[ \t]*[^\n]{0,60}"
    r"|" + _MARKERS + r"[ \t]*[^\n]{0,60}"
    r")",
    re.M,
)
# 文件名中的章节标题（用于文件夹输入且文件内无标题时兜底识别）
FILENAME_TITLE_RE = re.compile(
    r"(?:第\s*" + _NUMBER + r"\s*[章回话节集卷]|" + _MARKERS + r")"
)
# 结尾标记（"第1章（完）" "第1章完" 不应被当作新章节标题）
_END_MARKER_RE = re.compile(r"[（(]?(完|终|大结局)[）)]?\s*$|[（(]完[）)]")
# 紧跟章节标记的功能词 → 大概率是正文句子而不是标题（降低误切）
_FUNCTION_WORD_CHARS = "是只就都也被把在了吗呢啊吧还与或很而并这那它它们却怎么然而不过但等"

_CN_DIGIT_MAP = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "０": 0, "１": 1, "２": 2, "３": 3, "４": 4, "５": 5, "６": 6, "７": 7, "８": 8, "９": 9,
}

DEFAULT_MAX_CHAPTER_CHARS = 20000


def cn2ar(value: str) -> int:
    """中文/全角数字 → 阿拉伯数字（亿级内）。纯函数，思想复用 v3，不导入 v3 运行时。"""
    normalized = value.strip()
    if normalized.isdigit():
        return int(normalized)
    total = 0
    section = 0
    current = 0
    for ch in normalized:
        if ch in _CN_DIGIT_MAP:
            current = _CN_DIGIT_MAP[ch]
        elif ch == "十":
            section += (current or 1) * 10
            current = 0
        elif ch == "百":
            section += (current or 1) * 100
            current = 0
        elif ch == "千":
            section += (current or 1) * 1000
            current = 0
        elif ch == "万":
            section = (section + current) * 10000
            total += section
            section = 0
            current = 0
        elif ch == "亿":
            total = (total + section + current) * 100000000
            section = 0
            current = 0
    return total + section + current


@dataclass
class ChapterCandidate:
    """一个章节候选。``explicit=False`` 表示边界来自候选而非显式标题。"""

    ordinal: int
    title: str | None
    source_path: str | None
    start_line: int
    end_line: int
    text: str
    explicit: bool
    segments: list[str] | None = None  # 超长章节的内部技术分段
    appendix: bool = False  # 后记/附录等非正文，走本地化后附在正文之后输出


@dataclass
class SplitResult:
    mode: Literal["explicit", "candidate", "blocked"]
    boundary_status: Literal["confirmed", "awaiting_confirmation", "blocked"]
    chapters: list[ChapterCandidate]
    reasons: list[str] = field(default_factory=list)


def normalize_title(line: str) -> tuple[str | None, str]:
    """返回 (规范化章节标记, 完整标题行)。无法识别时返回 (None, 原行)。"""
    title = line.strip()
    match = re.match(r"第\s*(" + _NUMBER + r")\s*([章回话节集卷])", title)
    if match:
        return f"第{cn2ar(match.group(1))}{match.group(2)}", title
    for marker in _MARKERS.split("|"):
        if title.startswith(marker):
            return _SIMPLIFIED.get(marker, marker), title
    return None, title


def find_title_lines(text: str) -> list[tuple[int, str, str]]:
    """返回 [(行号 1-based, 规范化标记, 完整标题), ...]。"""
    results: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        match = _TITLE_MARKER_RE.match(line)
        if not match:
            continue
        title = line.strip()
        if _END_MARKER_RE.search(title):
            continue
        after = line[len(match.group(0)):][:1]
        if after and after in _FUNCTION_WORD_CHARS:
            continue
        normalized, full = normalize_title(title)
        if normalized is None:
            continue
        results.append((line_no, normalized, full))
    return results


def _strip_frontmatter(text: str) -> tuple[str, bool]:
    """剥离文本头部 frontmatter 元数据；frontmatter 不参与章节正文。"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:], True
    return text, False


def _title_number(title: str) -> int | None:
    match = re.match(r"第\s*(" + _NUMBER + r")\s*[章回话节集卷]", title)
    return cn2ar(match.group(1)) if match else None


def _title_from_filename(source_path: str | None) -> tuple[str, str] | None:
    if not source_path:
        return None
    stem = Path(source_path).stem
    match = FILENAME_TITLE_RE.search(stem)
    if not match:
        return None
    normalized, full = normalize_title(match.group(0))
    if normalized:
        return normalized, full or stem
    return None


def technical_segments(text: str, max_chars: int = DEFAULT_MAX_CHAPTER_CHARS) -> list[str]:
    """超长章节内部技术分段：优先句子边界（。！？…」』），其次段落边界，最后硬切。

    只用于调度/资源分配，绝不创建独立工作项、事件图、质检或交付状态。
    """
    full = text
    segments: list[str] = []
    start = 0
    total = len(full)
    sentence_endings = "。！？!?…」』\"”"
    while start < total:
        end = min(start + max_chars, total)
        if end < total:
            search_from = max(start + int(max_chars * 0.8), start)
            cut = -1
            for index in range(end, search_from - 1, -1):
                if full[index - 1] in sentence_endings:
                    cut = index
                    break
            if cut == -1:
                cut = full.rfind("\n\n", search_from, end)
                if cut == -1:
                    cut = end
            end = cut
        segments.append(full[start:end])
        start = end
    return segments


def _make_chapter(
    ordinal: int,
    title: str | None,
    source_path: str | None,
    start_line: int,
    end_line: int,
    text: str,
    *,
    explicit: bool,
    max_chapter_chars: int,
) -> ChapterCandidate:
    segments = None
    if len(text) > max_chapter_chars:
        segments = technical_segments(text, max_chapter_chars)
    return ChapterCandidate(
        ordinal=ordinal,
        title=title,
        source_path=source_path,
        start_line=start_line,
        end_line=end_line,
        text=text,
        explicit=explicit,
        segments=segments,
    )


def split_text(
    text: str,
    *,
    source_path: str | None = None,
    max_chapter_chars: int = DEFAULT_MAX_CHAPTER_CHARS,
) -> SplitResult:
    """切分单段文本的章节边界。

    显式标题 → ``explicit/confirmed``；无标题 → 仅生成整篇候选边界
    （``candidate/awaiting_confirmation``）；空输入 → ``blocked``。
    """
    body, has_frontmatter = _strip_frontmatter(text)
    reasons: list[str] = []
    if has_frontmatter:
        reasons.append("已剥离文本头部 frontmatter 元数据，不参与章节正文。")
    if not body.strip():
        return SplitResult(
            mode="blocked",
            boundary_status="blocked",
            chapters=[],
            reasons=[*reasons, "空输入，无法切分章节边界。"],
        )
    lines = body.splitlines()
    titles = find_title_lines(body)
    if titles:
        chapters: list[ChapterCandidate] = []
        for index, (line_no, _marker, title) in enumerate(titles):
            start = line_no
            end = titles[index + 1][0] - 1 if index + 1 < len(titles) else len(lines)
            chapter_text = "\n".join(lines[start - 1:end]).strip("\n")
            chapters.append(
                _make_chapter(
                    index + 1, title, source_path, start, end,
                    chapter_text, explicit=True, max_chapter_chars=max_chapter_chars,
                )
            )
        numbered = [num for num in (_title_number(ch.title) for ch in chapters) if num is not None]
        if numbered and any(b <= a for a, b in zip(numbered, numbered[1:])):
            reasons.append(
                "章节编号未严格递增，已按出现顺序切分；卷内重置属正常，否则需人工复核。"
            )
        seen: dict[str, list[int]] = {}
        for ch in chapters:
            seen.setdefault(ch.title or "", []).append(ch.ordinal)
        duplicates = [
            f"{name}（第{items[0]}、{items[1]}章）"
            for name, items in seen.items() if len(items) > 1
        ]
        if duplicates:
            reasons.append(
                f"检测到重复章节标题：{'；'.join(duplicates[:5])}。需人工确认，不影响本次切分。"
            )
        return SplitResult(
            mode="explicit",
            boundary_status="confirmed",
            chapters=chapters,
            reasons=reasons,
        )

    # 无显式标题：只生成候选边界，绝不静默按字数伪造章节。
    filename_title = _title_from_filename(source_path)
    if filename_title:
        marker, title = filename_title
        reasons.append(
            f"文件内未识别到章节标题，按文件名识别为章节「{title}」（边界仍需人工确认）。"
        )
        chapters = [
            _make_chapter(
                1, title, source_path, 1, len(lines),
                body.strip("\n"), explicit=True, max_chapter_chars=max_chapter_chars,
            )
        ]
        return SplitResult(
            mode="explicit",
            boundary_status="confirmed",
            chapters=chapters,
            reasons=reasons,
        )

    reasons.append(
        "未识别到任何章节标题：仅生成整篇候选边界，等待用户确认；绝不静默按字数伪造章节。"
    )
    chapters = [
        _make_chapter(
            1, "（未命名章节·候选）", source_path, 1, len(lines),
            body.strip("\n"), explicit=False, max_chapter_chars=max_chapter_chars,
        )
    ]
    return SplitResult(
        mode="candidate",
        boundary_status="awaiting_confirmation",
        chapters=chapters,
        reasons=reasons,
    )


def split_snapshot(
    texts: list[dict[str, Any]],
    *,
    max_chapter_chars: int = DEFAULT_MAX_CHAPTER_CHARS,
) -> SplitResult:
    """按快照顺序切分整个文件夹输入。任一分块 blocked → 整体 blocked。"""
    chapters: list[ChapterCandidate] = []
    reasons: list[str] = []
    mode: Literal["explicit", "candidate", "blocked"] = "explicit"
    boundary_status: Literal["confirmed", "awaiting_confirmation", "blocked"] = "confirmed"
    overall_ordinal = 0
    for item in texts:
        result = split_text(
            item["text"],
            source_path=item.get("source_path"),
            max_chapter_chars=max_chapter_chars,
        )
        reasons.extend(result.reasons)
        if result.mode == "blocked":
            return SplitResult(
                mode="blocked",
                boundary_status="blocked",
                chapters=[],
                reasons=reasons,
            )
        if result.mode == "candidate":
            mode = "candidate"
            boundary_status = "awaiting_confirmation"
        for chapter in result.chapters:
            overall_ordinal += 1
            chapter.ordinal = overall_ordinal
            chapters.append(chapter)
    if not chapters:
        return SplitResult(
            mode="blocked",
            boundary_status="blocked",
            chapters=[],
            reasons=[*reasons, "未生成任何章节候选。"],
        )
    return SplitResult(
        mode=mode,
        boundary_status=boundary_status,
        chapters=chapters,
        reasons=reasons,
    )
