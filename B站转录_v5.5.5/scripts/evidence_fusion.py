"""证据融合：把 OCR 画面文字与 ASR 片段按时间对齐（设计文档 12.1）。

只有“稳定出现、置信度达标、与 ASR 文本具备合理相似性”的同步字幕
（`speech_subtitle`）才能成为自动修正证据；`static_info`、`screenshot_text`
等只进入画面文字区，永远不改写逐字稿。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Iterable, Sequence

EVIDENCE = "speech_subtitle"
MIN_CONFIDENCE = 0.80
MIN_FRAMES = 2
MIN_SIMILARITY = 0.3
VISUAL_KINDS = ("static_info", "screenshot_text", "title_emphasis",
                "unknown_visual_text")


def segment_ref(index: int) -> str:
    """稳定的原始段引用，供校正结果回填。"""
    return f"seg-{int(index) + 1}"


def _normalize(text: str) -> str:
    return "".join(str(text or "").split())


def _similarity(left: str, right: str) -> float:
    a, b = _normalize(left), _normalize(right)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


@dataclass(frozen=True)
class SegmentEvidence:
    segment_index: int
    segment_id: str
    start: float
    end: float
    text: str
    subtitle_text: str = ""
    confidence: float = 0.0
    strong: bool = False
    overlap_seconds: float = 0.0
    similarity: float = 0.0
    doubts: list = field(default_factory=list)
    visual_texts: list = field(default_factory=list)

    def as_record(self) -> dict:
        return {"segment_id": self.segment_id, "start": self.start,
                "end": self.end, "text": self.text,
                "subtitle_text": self.subtitle_text,
                "confidence": self.confidence, "strong": self.strong,
                "overlap_seconds": round(self.overlap_seconds, 3),
                "similarity": round(self.similarity, 4),
                "doubts": list(self.doubts),
                "visual_texts": list(self.visual_texts)}


def _frames_of(observation: dict) -> int:
    try:
        return int(observation.get("frame_count", 1) or 1)
    except (TypeError, ValueError):
        return 1


def align_evidence(segments: Sequence[dict],
                   ocr_observations: Iterable[dict] | None,
                   ) -> list[SegmentEvidence]:
    """按时间交集把画面文字挂到 ASR 片段上，并判定证据强度。"""
    observations = list(ocr_observations or [])
    result: list[SegmentEvidence] = []
    for index, segment in enumerate(segments or []):
        try:
            start = float(segment.get("start", 0) or 0)
            end = float(segment.get("end", 0) or 0)
        except (TypeError, ValueError):
            start = end = 0.0
        text = str(segment.get("text", "") or "")
        subtitle_parts: list[str] = []
        visual_texts: list[str] = []
        confidence = 0.0
        overlap_total = 0.0
        doubts: list[str] = []
        strong = False

        for observation in observations:
            try:
                o_start = float(observation.get("start", 0) or 0)
                o_end = float(observation.get("end", 0) or 0)
            except (TypeError, ValueError):
                continue
            overlap = _overlap(start, end, o_start, o_end)
            if overlap <= 0:
                continue
            kind = str(observation.get("kind", "") or "")
            content = str(observation.get("text", "") or "")
            if kind != EVIDENCE:
                if kind in VISUAL_KINDS and content:
                    visual_texts.append(content)
                continue
            try:
                score = float(observation.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                score = 0.0
            frames = _frames_of(observation)
            if frames < MIN_FRAMES:
                doubts.append("not_repeated")
            if score < MIN_CONFIDENCE:
                doubts.append("low_confidence_subtitle")
            if doubts:
                continue
            subtitle_parts.append(content)
            confidence = max(confidence, score)
            overlap_total += overlap

        subtitle_text = " ".join(part for part in subtitle_parts if part)
        similarity = _similarity(text, subtitle_text) if subtitle_text else 0.0
        if subtitle_text and similarity < MIN_SIMILARITY:
            doubts.append("subtitle_mismatch")
        elif subtitle_text:
            strong = True

        result.append(SegmentEvidence(
            segment_index=index, segment_id=segment_ref(index), start=start,
            end=end, text=text, subtitle_text=subtitle_text,
            confidence=confidence, strong=strong,
            overlap_seconds=overlap_total, similarity=similarity,
            doubts=doubts, visual_texts=visual_texts))
    return result


CHUNK_CHARS = 120


def _text_chunks(text: str, size: int) -> list[str]:
    stripped = (text or "").strip()
    if not stripped:
        return []
    return [stripped[i:i + size] for i in range(0, len(stripped), size)]


def _best_window_similarity(chunk: str, subtitle: str) -> float:
    """在文本块内滑动与字幕等长的窗口，取最高相似度。

    B站的文本块比一条硬字幕长得多（字幕通常 4~20 字、块 120 字），
    整体比相似度会恒低于阈值。窗口匹配回答的才是真正的问题：
    「这一段文字里是否存在与该字幕对应的位置」。
    """
    subtitle = (subtitle or "").strip()
    chunk = chunk or ""
    if not subtitle or not chunk:
        return 0.0
    if len(subtitle) >= len(chunk):
        return _similarity(chunk, subtitle)
    best = 0.0
    width = len(subtitle)
    for start in range(0, len(chunk) - width + 1):
        score = _similarity(subtitle, chunk[start:start + width])
        if score > best:
            best = score
            if best >= 0.999:
                break
    return best


def align_evidence_by_text(text: str, ocr_observations: Iterable[dict] | None,
                           *, chunk_chars: int = CHUNK_CHARS
                           ) -> list[SegmentEvidence]:
    """B站适配：库中不保存分段时间轴，故按文本相似度把画面字幕挂到文本块上。

    与 `align_evidence` 的**唯一差别是挂接依据**：那边用时间交集，这里用相似度取最佳匹配。
    证据强度的判定规则（重复帧数、置信度、相似度下限）与 `align_evidence` 保持一致，
    因此下游「有证据才允许改」的约束无需改动。
    时间字段只在匹配到字幕时才有值（取 OCR 的真实时间），未匹配时为 0。
    """
    observations = list(ocr_observations or [])
    subtitle_obs: list[dict] = []
    for observation in observations:
        kind = str(observation.get("kind", "") or "")
        content = str(observation.get("text", "") or "")
        if not content or kind != EVIDENCE:
            continue
        try:
            score = float(observation.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        if _frames_of(observation) < MIN_FRAMES or score < MIN_CONFIDENCE:
            continue
        subtitle_obs.append({**observation, "_confidence": score})

    result: list[SegmentEvidence] = []
    for index, chunk in enumerate(_text_chunks(text, chunk_chars)):
        best = None
        best_similarity = 0.0
        for observation in subtitle_obs:
            similarity = _best_window_similarity(chunk, str(observation.get("text", "")))
            if best is None or similarity > best_similarity:
                best, best_similarity = observation, similarity
        doubts: list[str] = []
        subtitle_text = ""
        start = end = confidence = 0.0
        strong = False
        if best is None:
            doubts.append("no_subtitle_evidence")
        else:
            subtitle_text = str(best.get("text", ""))
            try:
                start = float(best.get("start", 0) or 0)
                end = float(best.get("end", 0) or 0)
            except (TypeError, ValueError):
                start = end = 0.0
            confidence = best["_confidence"]
            if best_similarity < MIN_SIMILARITY:
                doubts.append("subtitle_mismatch")
            else:
                strong = True
        result.append(SegmentEvidence(
            segment_index=index, segment_id=segment_ref(index), start=start,
            end=end, text=chunk, subtitle_text=subtitle_text,
            confidence=confidence, strong=strong,
            overlap_seconds=0.0, similarity=best_similarity,
            doubts=doubts, visual_texts=[]))
    return result


def visual_context_from_text(ocr_observations: Iterable[dict] | None) -> list:
    """非语音画面文字汇总（去重保序），供交付说明与摘要使用。"""
    seen: list = []
    for observation in ocr_observations or []:
        kind = str(observation.get("kind", "") or "")
        content = str(observation.get("text", "") or "")
        if content and kind in VISUAL_KINDS and content not in seen:
            seen.append(content)
    return seen
