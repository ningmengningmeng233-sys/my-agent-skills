"""ASR 质量评分与三轮重转决策（设计文档 11）。

分数先查硬异常，再按 0–100 扣分；阈值全部集中在不可变的 `QualityThresholds`，
不允许散落魔数。三轮决策只依据证据：清晰首轮即停，困难才升级。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field, replace
from typing import Sequence

from scripts.asr import AsrAttemptResult, AsrStrategy

PASS_STATUS = "passed"
REVIEW_STATUS = "needs_review"

MAX_ROUNDS = 3


@dataclass(frozen=True)
class QualityThresholds:
    """保守初值；发布前必须用验收样本校准并记录变更依据。"""

    pass_score: float = 75.0
    low_logprob_max: float = 30.0
    high_silence_max: float = 15.0
    repetition_max: float = 25.0
    language_max: float = 10.0
    subtitle_conflict_max: float = 15.0
    timeline_max: float = 5.0
    avg_logprob_floor: float = -1.0
    no_speech_ceiling: float = 0.6
    repetition_hard_ratio: float = 0.6
    language_probability_floor: float = 0.5
    overlap_hard_seconds: float = 1.0
    min_text_chars_per_second: float = 0.2
    subtitle_similarity_floor: float = 0.5


DEFAULT_THRESHOLDS = QualityThresholds()


@dataclass
class QualityReport:
    score: float
    status: str
    hard_failures: list = field(default_factory=list)
    deductions: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == PASS_STATUS and not self.hard_failures

    def as_dict(self) -> dict:
        return {"score": round(self.score, 2), "status": self.status,
                "hard_failures": list(self.hard_failures),
                "deductions": {key: round(value, 2)
                               for key, value in self.deductions.items()},
                "passed": self.passed}


def _duration(segment: dict) -> float:
    try:
        return max(float(segment.get("end", 0)) - float(segment.get("start", 0)), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _ratio_segments(segments: Sequence[dict], predicate) -> float:
    """按时间段占比加权，而不是按段数，避免长段被短段稀释。"""
    total = sum(_duration(item) for item in segments)
    if total <= 0:
        return 0.0
    flagged = sum(_duration(item) for item in segments if predicate(item))
    return min(flagged / total, 1.0)


def _text_of(segments: Sequence[dict]) -> str:
    return "".join(str(item.get("text", "")) for item in segments)


def _same_text(left: str, right: str) -> float:
    a = "".join(str(left or "").split())
    b = "".join(str(right or "").split())
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _timeline_anomalies(segments: Sequence[dict],
                        thresholds: QualityThresholds) -> tuple[bool, float]:
    """返回（是否硬异常，扣分比例）。"""
    ordered = list(segments)
    broken = False
    overlap_total = 0.0
    for index in range(1, len(ordered)):
        previous, current = ordered[index - 1], ordered[index]
        try:
            if float(current.get("start", 0)) < float(previous.get("start", 0)):
                broken = True
                break
        except (TypeError, ValueError):
            broken = True
            break
        gap = float(previous.get("end", 0)) - float(current.get("start", 0))
        if gap > thresholds.overlap_hard_seconds:
            broken = True
        elif gap > 0:
            overlap_total += gap
    total = sum(_duration(item) for item in ordered) or 1.0
    return broken, min(overlap_total / total, 1.0)


def _subtitle_conflict_ratio(segments: Sequence[dict],
                             ocr_subtitles: Sequence[dict],
                             thresholds: QualityThresholds) -> float:
    """只有稳定同步字幕（speech_subtitle）才算冲突证据。"""
    stable = [item for item in (ocr_subtitles or [])
              if str(item.get("kind", "")) == "speech_subtitle"
              and float(item.get("confidence", 0) or 0) >= 0.8]
    if not stable:
        return 0.0
    conflicts = 0
    for item in stable:
        window = [segment for segment in segments
                  if _duration(segment) > 0
                  and float(segment.get("end", 0)) >= float(item.get("start", 0))
                  and float(segment.get("start", 0)) <= float(item.get("end", 0))]
        if not window:
            continue
        best = max(_same_text(_text_of(window), str(item.get("text", ""))), 0.0)
        if best < thresholds.subtitle_similarity_floor:
            conflicts += 1
    return min(conflicts / len(stable), 1.0)


def evaluate_quality(attempt: AsrAttemptResult,
                     audio_analysis=None,
                     ocr_subtitles: Sequence[dict] | None = None,
                     *, thresholds: QualityThresholds | None = None,
                     expected_language: str = "zh") -> QualityReport:
    """硬异常优先，其余按证据扣分；结果稳定可复现。"""
    limits = thresholds or DEFAULT_THRESHOLDS
    segments = list(attempt.segments or [])
    hard: list[str] = list(attempt.hard_failures or [])
    metrics = dict(attempt.metrics or {})

    if not segments:
        if "empty_output" not in hard:
            hard.append("empty_output")
        return QualityReport(score=0.0, status=REVIEW_STATUS,
                             hard_failures=hard, deductions={})

    broken, overlap_ratio = _timeline_anomalies(segments, limits)
    if broken:
        hard.append("timeline_broken")

    repetitive_ratio = _ratio_segments(
        segments, lambda item: "repetitive" in (item.get("review_reasons") or []))
    if repetitive_ratio >= limits.repetition_hard_ratio:
        hard.append("repetitive_output")

    if all(float(item.get("no_speech_prob", 0) or 0) > limits.no_speech_ceiling
           for item in segments):
        hard.append("all_silence_prob")

    duration = float(metrics.get("duration") or 0.0)
    characters = len("".join(str(item.get("text", "")) for item in segments))
    silence_ratio = float(getattr(audio_analysis, "silence_ratio", 0.0) or 0.0)
    if (duration > 0 and silence_ratio < 0.5
            and characters / duration < limits.min_text_chars_per_second):
        hard.append("too_little_text")

    deductions = {
        "low_logprob": limits.low_logprob_max * _ratio_segments(
            segments,
            lambda item: float(item.get("avg_logprob", 0) or 0)
            < limits.avg_logprob_floor),
        "high_silence": limits.high_silence_max * _ratio_segments(
            segments,
            lambda item: float(item.get("no_speech_prob", 0) or 0)
            > limits.no_speech_ceiling),
        "repetition": limits.repetition_max * repetitive_ratio,
        "timeline": limits.timeline_max * overlap_ratio,
    }

    language_probability = float(metrics.get("language_probability") or 0.0)
    detected = str(metrics.get("language") or "")
    language_anomaly = language_probability < limits.language_probability_floor
    if expected_language and detected and detected != expected_language \
            and language_probability < 0.9:
        language_anomaly = True
    deductions["language"] = limits.language_max if language_anomaly else 0.0

    deductions["subtitle_conflict"] = limits.subtitle_conflict_max * \
        _subtitle_conflict_ratio(segments, ocr_subtitles or [], limits)

    score = max(0.0, 100.0 - sum(deductions.values()))
    status = (PASS_STATUS if score >= limits.pass_score and not hard
              else REVIEW_STATUS)
    return QualityReport(score=score, status=status, hard_failures=hard,
                         deductions=deductions)


# ----------------------------------------------------------------------
# 三轮决策
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class AsrAssets:
    """本轮可用的音轨与模型档位（由硬件与增强结果决定）。"""

    variants: dict = field(default_factory=dict)
    available_models: tuple = ("base", "small", "medium", "large-v3")

    def has(self, variant: str) -> bool:
        return variant in self.variants and bool(self.variants[variant])


@dataclass(frozen=True)
class HardwareBudget:
    device: str = "cpu"
    compute_type: str = "int8"
    max_model: str = "base"
    vram_gb: float | None = None
    fallback_device: str = "cpu"
    fallback_compute_type: str = "int8"


_VAD_RELAXED = {"threshold": 0.35, "min_silence_duration_ms": 500,
                "min_speech_duration_ms": 100}
_REPETITION_PENALTY = 1.15
_NO_REPEAT_NGRAM = 3


def _reasons(attempt: AsrAttemptResult, report: QualityReport) -> set:
    found = set(report.hard_failures)
    for segment in attempt.segments or []:
        found.update(segment.get("review_reasons") or [])
    return found


def _upgrade_model(current: str, budget: HardwareBudget,
                   assets: AsrAssets) -> str:
    ladder = [name for name in assets.available_models]
    if current in ladder:
        index = ladder.index(current)
        candidates = ladder[index + 1:]
    else:
        candidates = ladder
    ceiling = budget.max_model
    if ceiling in ladder:
        allowed = ladder[:ladder.index(ceiling) + 1]
        candidates = [name for name in candidates if name in allowed] or [
            name for name in (current, ceiling) if name in ladder]
    if not candidates:
        return current
    return candidates[-1]


def _best_variant(history, assets: AsrAssets) -> str:
    """沿用前两轮里表现最好的音轨。"""
    best_variant, best_score = "original", -1.0
    for attempt, report in history:
        if report.score > best_score and assets.has(attempt.audio_variant):
            best_variant, best_score = attempt.audio_variant, report.score
    return best_variant


def next_strategy(history, assets: AsrAssets, hardware: HardwareBudget, *,
                  audio_hint: str = "", oom_downgraded: bool = False,
                  expected_language: str = "zh",
                  thresholds: QualityThresholds | None = None
                  ) -> AsrStrategy | None:
    """决定下一轮策略；返回 None 表示停止（已达标或已用满三轮）。"""
    if not history:
        return None
    last_attempt, last_report = history[-1]

    if last_report.passed:
        return None

    # OOM 只触发一次硬件降级，同一配置不计为新的一轮
    if last_attempt.error_code == "asr_oom":
        if oom_downgraded:
            return None
        return replace(last_attempt.strategy,
                       device=hardware.fallback_device,
                       compute_type=hardware.fallback_compute_type,
                       model=min(last_attempt.strategy.model, "small",
                                 key=lambda name: assets.available_models.index(name)
                                 if name in assets.available_models else 0))

    if len(history) >= MAX_ROUNDS:
        return None

    base = last_attempt.strategy
    reasons = _reasons(last_attempt, last_report)

    if len(history) == 1:
        # 第二轮：只做一种主要修复
        if assets.has("vocals") and ({"high_silence_prob", "all_silence_prob"}
                                     & reasons):
            return replace(base, audio_variant="vocals")
        if assets.has("enhanced") and audio_hint == "loudness":
            return replace(base, audio_variant="enhanced")
        if "repetitive" in reasons or "repetitive_output" in reasons:
            return replace(base, condition_on_previous_text=False,
                           repetition_penalty=_REPETITION_PENALTY,
                           no_repeat_ngram_size=_NO_REPEAT_NGRAM)
        if "too_little_text" in reasons or last_report.deductions.get(
                "timeline", 0.0) > 0:
            return replace(base, vad_filter=True, vad_parameters=dict(_VAD_RELAXED))
        if last_report.deductions.get("language", 0.0) > 0:
            return replace(base, language=expected_language or base.language)
        # 兜底：优先换更好的音轨，其次放宽 VAD
        for variant in ("vocals", "enhanced"):
            if assets.has(variant) and variant != base.audio_variant:
                return replace(base, audio_variant=variant)
        if base.vad_parameters is None:
            return replace(base, vad_parameters=dict(_VAD_RELAXED))
        return None

    # 第三轮：用当前硬件可稳定运行的更高精度模型 + 前两轮最佳音轨/参数
    upgraded = _upgrade_model(base.model, hardware, assets)
    if upgraded == base.model and base.audio_variant == _best_variant(history, assets) \
            and base.vad_parameters is not None:
        return None
    return replace(base, model=upgraded,
                   audio_variant=_best_variant(history, assets),
                   device=hardware.device, compute_type=hardware.compute_type)
