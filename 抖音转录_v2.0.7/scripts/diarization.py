"""匿名说话人分离（Sherpa-ONNX）与 ASR 时间对齐。

只输出「说话人 A/B/C」这类匿名标签，不绑定真实身份（设计文档 10）。
保守发布：少于两个稳定聚类不加标签；总时长不足 2 秒的聚类不算新人物；
最大重叠不足片段时长 60% 时不强行赋值。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

logger = logging.getLogger("douyin_transcript.diarization")

MIN_STABLE_CLUSTER_SECONDS = 2.0
MIN_OVERLAP_RATIO = 0.6
#: 两个说话人重叠接近（次优重叠接近最优）时不强行命名
AMBIGUITY_RATIO = 0.6
#: 单人视频校准（真实样本实测）：最大聚类占比 ≥60% 且其余聚类都很短时视为单人。
#: 依据：2026-09-16 验收样本（单人口播 + 强背景音乐）被判出 5 个聚类
#: （17.7s / 7.4s / 5.6s / 4.2s / 4.1s），实际只有一位说话人。
DOMINANT_CLUSTER_SHARE = 0.6
FRAGMENT_CLUSTER_MAX_SECONDS = 8.0
#: 几乎没有停顿（强背景音乐/连续人声）时，声纹聚类不可靠：
#: 真实样本实测 silence_ratio=0.05，单人口播被判出 5 个聚类。
MUSIC_LIKE_SILENCE_RATIO = 0.08
#: 聚类数达到该值且音频连续无停顿 → 判定为分离器碎片，不发布标签。
FRAGMENTATION_CLUSTER_COUNT = 3
_UNKNOWN = ""

DiarizationStatus = str  # ok | no_speakers | skipped_or_failed


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker_label: str
    confidence: float = 0.0

    def as_record(self) -> dict:
        return {"start": self.start, "end": self.end,
                "speaker_label": self.speaker_label,
                "confidence": self.confidence}


@dataclass
class DiarizationOutcome:
    status: str
    turns: list[SpeakerTurn] = field(default_factory=list)
    speaker_count: int = 0
    degradations: list[str] = field(default_factory=list)


def display_label(label: str) -> str:
    """内部标签转成人话；未知标签一律留空，不猜身份。"""
    if not isinstance(label, str) or not label.startswith("speaker_"):
        return ""
    letter = label[len("speaker_"):]
    if len(letter) != 1 or not letter.isalpha():
        return ""
    return f"说话人 {letter.upper()}"


def models_available() -> bool:
    from scripts import model_assets

    return model_assets.ModelAssetManager().capability_usable("diarization")


def _model_paths() -> dict[str, str]:
    from scripts import model_assets

    manager = model_assets.ModelAssetManager()
    paths = {}
    for name, key in (("diarization_segmentation", "segmentation"),
                      ("diarization_embedding", "embedding")):
        state = manager.verify(name)
        if state.ok:
            paths[key] = state.path
    return paths


def default_runner(audio: Path) -> list:
    """真实推理：加载 manifest 指定的模型，解码音频并跑离线分离。"""
    import numpy as np
    import sherpa_onnx

    from scripts.audio_pipeline import _load_mono_samples

    paths = _model_paths()
    if "segmentation" not in paths or "embedding" not in paths:
        raise FileNotFoundError("说话人分离模型缺失")
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=paths["segmentation"])),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=paths["embedding"]),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1,
                                                    threshold=0.5),
        min_duration_on=0.3, min_duration_off=0.5)
    pipeline = sherpa_onnx.OfflineSpeakerDiarization(config)
    samples, sample_rate = _load_mono_samples(Path(audio))
    result = pipeline.process(np.asarray(samples, dtype="float32")).sort_by_start_time()
    return list(result)


def stable_labels(segments: Sequence) -> dict:
    """按首次出现顺序把上游 cluster id 映射成 speaker_a/b/c…（不用 id 排序）。"""
    mapping: dict = {}
    for segment in sorted(segments, key=lambda item: float(item.start)):
        cluster = getattr(segment, "speaker", None)
        if cluster is None or cluster in mapping:
            continue
        mapping[cluster] = f"speaker_{chr(ord('a') + len(mapping))}"
    return mapping


def _cluster_durations(segments: Sequence) -> dict:
    totals: dict = {}
    for segment in segments:
        cluster = getattr(segment, "speaker", None)
        if cluster is None:
            continue
        totals[cluster] = totals.get(cluster, 0.0) + max(
            float(segment.end) - float(segment.start), 0.0)
    return totals


def _stable_clusters(segments: Sequence, mapping: dict) -> set:
    """只保留总时长足够的聚类：短促片段不单独声明新人物。"""
    durations = _cluster_durations(segments)
    return {cluster for cluster, total in durations.items()
            if total >= MIN_STABLE_CLUSTER_SECONDS}


def _is_single_speaker(totals: dict) -> bool:
    """判定“一个人 + 多处碎片”的情形。

    真实校准：单人口播 + 强背景音乐会被分离器切成多个碎片聚类；
    只有当最大聚类占绝对多数、**且存在两处以上**碎片、且每处碎片都很短时，
    才判定为单人视频。真正的双人对白（两个可比聚类）不会被误并。
    """
    if not totals:
        return True
    total = sum(totals.values())
    if total <= 0:
        return True
    biggest = max(totals.values())
    others = [value for value in totals.values() if value != biggest]
    if biggest / total < DOMINANT_CLUSTER_SHARE:
        return False
    if len(others) < 2:
        return False
    return all(value <= FRAGMENT_CLUSTER_MAX_SECONDS for value in others)


def diarize(audio: Path, runner: Callable[[Path], Sequence] | None = None,
            *, manager=None, audio_analysis=None) -> DiarizationOutcome:
    """跑一次匿名分离；任何失败都只降级，不影响主体转录。"""
    audio = Path(audio)
    if not audio.is_file():
        return DiarizationOutcome(status="skipped_or_failed",
                                  degradations=["audio_invalid"])
    usable = manager.capability_usable("diarization") if manager is not None \
        else models_available()
    if not usable:
        logger.info("说话人分离模型缺失，跳过该能力")
        return DiarizationOutcome(status="skipped_or_failed",
                                  degradations=["model_missing"])

    invoke = runner or default_runner
    try:
        raw = invoke(audio)
    except Exception as exc:
        logger.warning("说话人分离失败，已降级：%s", type(exc).__name__)
        return DiarizationOutcome(status="skipped_or_failed",
                                  degradations=["diarization_failed"])
    if not raw:
        return DiarizationOutcome(status="skipped_or_failed",
                                  degradations=["empty_output"])

    mapping = stable_labels(raw)
    stable = _stable_clusters(raw, mapping)
    durations = _cluster_durations(raw)
    silence_ratio = float(getattr(audio_analysis, "silence_ratio", 0.0) or 0.0)
    fragmented = (len(stable) >= FRAGMENTATION_CLUSTER_COUNT
                  and silence_ratio <= MUSIC_LIKE_SILENCE_RATIO)
    if len(stable) >= 2 and (fragmented or _is_single_speaker(
            {cluster: durations.get(cluster, 0.0) for cluster in stable})):
        # 分离器被背景音乐带偏：要么聚类是碎片，要么最大聚类占绝对多数
        logger.info("说话人聚类不可靠（%d 个聚类，静音比例 %.2f），按单人处理",
                    len(stable), silence_ratio)
        stable = set()
    if len(stable) < 2:
        # 单人视频：不加任何说话人标签
        turns = [SpeakerTurn(float(getattr(seg, "start", 0) or 0),
                             float(getattr(seg, "end", 0) or 0), _UNKNOWN)
                 for seg in raw]
        return DiarizationOutcome(status="no_speakers", turns=turns,
                                  speaker_count=0)

    turns = []
    for segment in raw:
        cluster = getattr(segment, "speaker", None)
        label = mapping.get(cluster, _UNKNOWN) if cluster in stable else _UNKNOWN
        turns.append(SpeakerTurn(float(getattr(segment, "start", 0) or 0),
                                 float(getattr(segment, "end", 0) or 0),
                                 label,
                                 float(getattr(segment, "confidence", 0.0) or 0.0)))
    turns.sort(key=lambda item: item.start)
    return DiarizationOutcome(status="ok", turns=turns,
                              speaker_count=len(stable))


def _overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def assign_speakers(segments: Iterable[dict],
                    turns: Sequence[SpeakerTurn]) -> list[dict]:
    """按最大重叠给 ASR 片段分配说话人；证据不足则留空，绝不改写文本。"""
    labels = [turn for turn in turns if turn.speaker_label]
    assigned: list[dict] = []
    for segment in segments:
        item = dict(segment)
        try:
            start = float(segment.get("start", 0) or 0)
            end = float(segment.get("end", 0) or 0)
        except (TypeError, ValueError):
            item["speaker"] = _UNKNOWN
            item["speaker_overlap"] = 0.0
            assigned.append(item)
            continue
        duration = max(end - start, 0.0)
        overlaps: list[tuple[float, str]] = []
        for turn in labels:
            overlap = _overlap(start, end, turn.start, turn.end)
            if overlap > 0:
                overlaps.append((overlap, turn.speaker_label))
        overlaps.sort(reverse=True)
        best_overlap, best_label = overlaps[0] if overlaps else (0.0, _UNKNOWN)
        runner_up = overlaps[1][0] if len(overlaps) > 1 else 0.0
        ratio = (best_overlap / duration) if duration > 0 else 0.0
        # 覆盖不足或两人重叠接近时，都不强行命名
        ambiguous = best_overlap > 0 and runner_up >= AMBIGUITY_RATIO * best_overlap
        item["speaker"] = (best_label
                           if ratio >= MIN_OVERLAP_RATIO and not ambiguous
                           else _UNKNOWN)
        item["speaker_overlap"] = round(ratio, 4)
        assigned.append(item)
    return assigned
