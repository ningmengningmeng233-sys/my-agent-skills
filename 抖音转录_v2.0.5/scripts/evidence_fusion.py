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


def visual_context(evidence: Sequence[SegmentEvidence]) -> list:
    """非语音文字汇总（去重保序），只用于画面文字区与摘要上下文。"""
    seen: list = []
    for item in evidence:
        for text in item.visual_texts:
            if text not in seen:
                seen.append(text)
    return seen
