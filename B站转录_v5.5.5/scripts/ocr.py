"""自适应画面文字 OCR。

抽帧分三级：基础 1 fps 全画面 → 场景变化补帧 → 字幕激活后 4 fps 局部采样。
只有底部稳定同步字幕算 `speech_subtitle`，其余类别只进入画面文字区，
绝不作为语音纠错证据（见设计文档 9）。
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import shutil
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

logger = logging.getLogger("bilibili_transcript.ocr")

MERGE_SIMILARITY = 0.85
SUBTITLE_ACTIVATION_SIMILARITY = 0.5
SUBTITLE_BAND_TOP = 0.4           # 底部 60% 区域
SUBTITLE_MIN_WIDTH = 0.12   # 短句字幕很窄，只要在底部带内稳定出现即可
SCENE_THRESHOLD = 0.25
BASE_FPS = 1.0
SUBTITLE_FPS = 4.0
REDUCED_SUBTITLE_FPS = 2.0
MAX_FRAMES = 12_000
LONG_VIDEO_SECONDS = 20 * 60
_PUNCTUATION = re.compile(r"[\s，。、；：！？；：,.!?;:\-—_/\\|（）()\[\]【】<>《》“”\"'`~·…]+")
_PARAMETER_HINT = re.compile(r"\d")
_PARAMETER_UNITS = re.compile(
    r"\d\s*(?:GB|MB|KB|TB|秒|分|小时|元|%|℃|kg|g|km|m|万|亿)", re.IGNORECASE)
_DATE_LIKE = re.compile(r"\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?")


@dataclass(frozen=True)
class FramePolicy:
    base_fps: float = BASE_FPS
    scene_threshold: float = SCENE_THRESHOLD
    subtitle_fps: float = SUBTITLE_FPS
    reduced_subtitle_fps: float = REDUCED_SUBTITLE_FPS
    max_frames: int = MAX_FRAMES
    long_video_seconds: float = LONG_VIDEO_SECONDS


@dataclass(frozen=True)
class FrameRef:
    path: Path
    timestamp: float
    source: str
    region: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class OcrObservation:
    start: float
    end: float
    text: str
    bbox: tuple
    confidence: float
    kind: str
    frame_hash: str = ""
    asr_similarity: float = 0.0
    frame_size: tuple[int, int] = (0, 0)
    frame_count: int = 1

    def as_record(self) -> dict:
        """可 JSON 序列化：所有数值都转成 Python 原生类型（numpy 标量会失败）。

        `frame_count` 与 `frame_size` 必须一并带上：证据融合用 `frame_count` 判定
        「该字幕是否在多帧稳定出现」（`MIN_FRAMES`）；丢失它会让所有字幕证据被
        静默判为不稳定而丢弃，校正因此拿不到任何硬字幕证据。
        """
        return {"start": float(self.start), "end": float(self.end),
                "text": self.text,
                "bbox": [[float(point[0]), float(point[1])]
                         for point in self.bbox],
                "confidence": float(self.confidence), "kind": self.kind,
                "frame_hash": self.frame_hash,
                "asr_similarity": float(self.asr_similarity),
                "frame_count": int(self.frame_count),
                "frame_size": [int(self.frame_size[0]), int(self.frame_size[1])]}


@dataclass
class OcrOutcome:
    status: str
    observations: list[OcrObservation] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    frames_scanned: int = 0


def normalize_text(text: str) -> str:
    return _PUNCTUATION.sub("", str(text or ""))


def text_similarity(left: str, right: str) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _bounds(bbox) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in bbox]
    ys = [float(point[1]) for point in bbox]
    return min(xs), min(ys), max(xs), max(ys)


def _frame_hash(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def classify_text(text: str, bbox, frame_size: tuple[int, int], *,
                  stable_frames: int = 1, asr_similarity: float = 0.0,
                  line_count: int = 1) -> str:
    """按位置、稳定性与文本形态分类画面文字（保守优先）。"""
    height, width = frame_size
    if not height or not width or not bbox:
        return "unknown_visual_text"
    x0, y0, x1, y1 = _bounds(bbox)
    rel_top = y0 / height
    rel_height = (y1 - y0) / height
    rel_width = (x1 - x0) / width
    content = normalize_text(text)
    lines = max(int(line_count or 1), str(text or "").count("\n") + 1)
    # 参数、日期、数值单位：内容形态本身就说明它不是口播字幕
    parameter_like = bool(_PARAMETER_UNITS.search(text or "")
                          or _DATE_LIKE.search(text or "")
                          or (lines >= 2 and _PARAMETER_HINT.search(content)))

    # 底部稳定文字：B站视频硬字幕的典型位置；参数类文字只有与语音对上才算字幕
    if (stable_frames >= 2 and rel_top >= SUBTITLE_BAND_TOP
            and rel_width >= SUBTITLE_MIN_WIDTH and content
            and (not parameter_like
                 or asr_similarity >= SUBTITLE_ACTIVATION_SIMILARITY)):
        return "speech_subtitle"

    # 多行满宽：聊天/新闻/评论截图（先于标题判定，截图往往也很大）
    if lines >= 3 and rel_width >= 0.85:
        return "screenshot_text"

    # 顶部大字、行数很少：标题或强调
    if rel_height >= 0.08 and rel_top < SUBTITLE_BAND_TOP and rel_width >= 0.2 \
            and lines <= 2:
        return "title_emphasis"

    if parameter_like:
        return "static_info"

    return "unknown_visual_text"


def _group_streams(observations: Sequence[OcrObservation]
                   ) -> list[list[OcrObservation]]:
    """把观察拆成“文字流”：同类别且文本相似度达标的归为同一流。

    同一帧上可能同时有字幕、参数、截图等多种文字，逐帧交错出现；
    只有先分流，才能正确统计“连续出现帧数”并合并时间范围。
    """
    streams: list[list[OcrObservation]] = []
    for observation in sorted(observations, key=lambda item: (item.start, item.end)):
        for stream in streams:
            last = stream[-1]
            if (observation.kind == last.kind
                    and text_similarity(observation.text, last.text)
                    >= MERGE_SIMILARITY):
                stream.append(observation)
                break
        else:
            streams.append([observation])
    return streams


def _stable_runs(stream: Sequence[OcrObservation],
                 *, max_gap: float = 1.5) -> list[int]:
    """流内按时间计算“连续出现帧数”，间隔过大即重新计数。"""
    runs: list[int] = []
    for index, observation in enumerate(stream):
        if index and observation.start - stream[index - 1].end <= max_gap:
            runs.append(runs[-1] + 1)
        else:
            runs.append(1)
    return runs


def merge_observations(observations: Iterable[OcrObservation], *,
                       max_gap: float = 0.05) -> list[OcrObservation]:
    """同一文字流内时间连续时合并为一条观察（相似度 >= 0.85）。"""
    merged: list[OcrObservation] = []
    for stream in _group_streams(list(observations)):
        current = stream[0]
        for observation in stream[1:]:
            if observation.start - current.end <= max_gap:
                current = OcrObservation(
                    start=current.start, end=max(current.end, observation.end),
                    text=current.text if len(current.text) >= len(observation.text)
                    else observation.text,
                    bbox=current.bbox,
                    confidence=max(current.confidence, observation.confidence),
                    kind=current.kind,
                    frame_hash=current.frame_hash,
                    asr_similarity=max(current.asr_similarity,
                                       observation.asr_similarity),
                    frame_size=current.frame_size,
                    frame_count=current.frame_count + observation.frame_count)
            else:
                merged.append(current)
                current = observation
        merged.append(current)
    return sorted(merged, key=lambda item: (item.start, item.end))


# ----------------------------------------------------------------------
# 抽帧
# ----------------------------------------------------------------------

def _probe_duration(video: Path) -> float:
    """用 ffprobe 读取媒体时长（秒）；失败返回 0.0。"""
    binary = shutil.which("ffprobe") or "ffprobe"
    try:
        result = subprocess.run(
            [binary, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        return float((result.stdout or "").strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _default_runner(argv: list[str], **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=1800, **kwargs)


def _run_ffmpeg(argv: list[str], runner: Callable) -> tuple[bool, str]:
    try:
        completed = runner(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("抽帧失败：%s", type(exc).__name__)
        return False, ""
    ok = getattr(completed, "returncode", 1) == 0
    return ok, getattr(completed, "stderr", "") or ""


def _emitted(pattern: Path) -> list[Path]:
    stem, suffix = pattern.stem, pattern.suffix
    prefix = stem.replace("%06d", "")
    if not pattern.parent.is_dir():
        return []
    return sorted(path for path in pattern.parent.glob(f"{prefix}*{suffix}")
                  if path.is_file())


def _extract_base(video: Path, frame_dir: Path, policy: FramePolicy,
                  runner: Callable) -> list[FrameRef]:
    pattern = frame_dir / "base_%06d.jpg"
    ok, _ = _run_ffmpeg(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
         "-vf", f"fps={policy.base_fps:g}", "-q:v", "3", str(pattern)], runner)
    if not ok:
        return []
    interval = 1.0 / policy.base_fps if policy.base_fps else 1.0
    return [FrameRef(path=path, timestamp=round(index * interval, 3),
                     source="base")
            for index, path in enumerate(_emitted(pattern))]


def _extract_scene(video: Path, frame_dir: Path, policy: FramePolicy,
                   runner: Callable) -> list[FrameRef]:
    pattern = frame_dir / "scene_%06d.jpg"
    ok, stderr = _run_ffmpeg(
        ["ffmpeg", "-hide_banner", "-y", "-i", str(video), "-vf",
         f"select='gt(scene,{policy.scene_threshold:g})',showinfo",
         "-vsync", "vfr", "-q:v", "3", str(pattern)], runner)
    if not ok:
        return []
    timestamps = [float(value) for value in
                  re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", stderr or "")]
    paths = _emitted(pattern)
    frames = []
    for index, path in enumerate(paths):
        timestamp = timestamps[index] if index < len(timestamps) else 0.0
        frames.append(FrameRef(path=path, timestamp=round(timestamp, 3),
                               source="scene"))
    return frames


def _extract_subtitle(video: Path, frame_dir: Path, windows: Sequence[tuple],
                      policy: FramePolicy, fps: float, height: int,
                      runner: Callable) -> list[FrameRef]:
    frames: list[FrameRef] = []
    crop = "crop=iw:ih*0.6:0:ih*0.4"
    for index, (start, end) in enumerate(windows, start=1):
        begin, finish = max(0.0, float(start)), float(end)
        if finish <= begin:
            continue
        pattern = frame_dir / f"subtitle_{index:03d}_%06d.jpg"
        ok, _ = _run_ffmpeg(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{begin:.3f}", "-t", f"{finish - begin:.3f}",
             "-i", str(video), "-vf", f"fps={fps:g},{crop}", "-q:v", "3",
             str(pattern)], runner)
        if not ok:
            continue
        interval = 1.0 / fps if fps else 0.25
        frames.extend(
            FrameRef(path=path, timestamp=round(begin + offset * interval, 3),
                     source="subtitle",
                     region=(0, int(height * 0.4), 0, height))
            for offset, path in enumerate(_emitted(pattern)))
    return frames


def extract_adaptive_frames(video: Path, frame_dir: Path, policy: FramePolicy,
                            *, windows: Sequence[tuple] | None = None,
                            duration: float | None = None,
                            runner: Callable | None = None,
                            probe: Callable[[Path], float] | None = None,
                            frame_size: tuple[int, int] = (0, 0)) -> list[FrameRef]:
    """基础 1 fps + 场景补帧（+ 可选字幕窗口 4 fps）。"""
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"视频不存在：{video.name}")
    frame_dir = Path(frame_dir)
    frame_dir.mkdir(parents=True, exist_ok=True)
    runner = runner or _default_runner
    measured = float(duration if duration is not None else
                     (probe or _probe_duration)(video))
    if measured <= 0:
        raise ValueError("视频时长未知，无法抽帧")

    long_video = measured > policy.long_video_seconds
    base = _extract_base(video, frame_dir, policy, runner)
    scene = _extract_scene(video, frame_dir, policy, runner)
    frames = scene + base

    if windows:
        fps = policy.reduced_subtitle_fps if long_video else policy.subtitle_fps
        budget = max(policy.max_frames - len(frames), 0)
        height = frame_size[0] or 0
        if budget:
            subtitle = _extract_subtitle(video, frame_dir, windows, policy, fps,
                                         height, runner)
            frames.extend(subtitle[:budget])
    return frames


# ----------------------------------------------------------------------
# 识别与编排
# ----------------------------------------------------------------------

def _convert_result(result) -> list[dict]:
    """把 RapidOCR 的 boxes/txts/scores 转成本项目的普通结构。"""
    if result is None:
        return []
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if boxes is None:
        return []
    converted = []
    for index, box in enumerate(boxes):
        text = str(texts[index]) if texts is not None and index < len(texts) else ""
        try:
            score = float(scores[index]) if scores is not None else 0.0
        except (TypeError, ValueError, IndexError):
            score = 0.0
        cleaned = re.sub(r"\s+", " ", text).strip()
        if cleaned:
            # RapidOCR 返回的是 numpy float32，必须转成 Python float 才能入库/序列化
            converted.append({"box": _plain_box(box), "text": cleaned,
                              "score": float(score)})
    return converted


def _plain_box(box) -> tuple:
    """把 RapidOCR 的 numpy 点集转成纯 Python 浮点元组（可直接入库与序列化）。"""
    if box is None:
        return ()
    points = []
    for point in box:
        try:
            points.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return tuple(points)


def default_engine():
    """真实引擎只在被调用时导入，避免把 RapidOCR 对象扩散到编排器。"""
    from rapidocr import RapidOCR

    return RapidOCR()


def _frame_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
        return height, width
    except Exception:
        return 0, 0


def _segment_text_at(segments: Sequence[dict], start: float, end: float) -> str:
    parts = []
    for segment in segments or []:
        try:
            seg_start = float(segment.get("start", 0))
            seg_end = float(segment.get("end", 0))
        except (TypeError, ValueError):
            continue
        if seg_end >= start and seg_start <= end:
            parts.append(str(segment.get("text", "")))
    return "".join(parts)


def _scan(frames: Sequence[FrameRef], engine, frame_size: tuple[int, int],
          asr_segments: Sequence[dict] | None) -> list[OcrObservation]:
    observations: list[OcrObservation] = []
    for index, frame in enumerate(frames):
        # 一条观察的有效期覆盖到下一个采样帧之前，便于相邻同文本合并
        if index + 1 < len(frames):
            span = frames[index + 1].timestamp - frame.timestamp
        else:
            span = 0.25
        span = min(max(span, 0.25), 2.0)
        try:
            boxes = _convert_result(engine(frame.path))
        except Exception as exc:
            logger.warning("单帧 OCR 失败，跳过：%s", type(exc).__name__)
            raise
        digest = _frame_hash(frame.path)
        size = frame_size if frame_size[0] else _frame_size(frame.path)
        offset_y = frame.region[1] if frame.region else 0
        for item in boxes:
            bbox = item["box"]
            if offset_y:
                bbox = tuple((float(point[0]), float(point[1]) + offset_y)
                             for point in bbox)
            window_text = _segment_text_at(asr_segments or [], frame.timestamp,
                                           frame.timestamp + span)
            observations.append(OcrObservation(
                start=frame.timestamp, end=round(frame.timestamp + span, 3),
                text=item["text"], bbox=tuple(bbox),
                confidence=float(item["score"]), kind="unknown_visual_text",
                frame_hash=digest,
                asr_similarity=text_similarity(item["text"], window_text)
                if window_text else 0.0,
                frame_size=size))
    return observations


def _label(observations: list[OcrObservation], frame_size: tuple[int, int]
           ) -> list[OcrObservation]:
    """按文字流定类：一条流只取一个类别，避免同一句话前后被判成两类。"""
    labels: list[OcrObservation] = []
    for stream in _group_streams(list(observations)):
        runs = _stable_runs(stream)
        stable = max(runs) if runs else 1
        best = stream[runs.index(stable)] if runs else stream[0]
        size = best.frame_size if best.frame_size[0] else frame_size
        similar = max(item.asr_similarity for item in stream)
        kind = classify_text(best.text, best.bbox, size, stable_frames=stable,
                             asr_similarity=similar)
        for observation in stream:
            labels.append(OcrObservation(
                start=observation.start, end=observation.end,
                text=observation.text, bbox=observation.bbox,
                confidence=observation.confidence, kind=kind,
                frame_hash=observation.frame_hash,
                asr_similarity=observation.asr_similarity,
                frame_size=observation.frame_size,
                frame_count=observation.frame_count))
    return labels


def _subtitle_windows(base_observations: Sequence[OcrObservation], *,
                      pad: float = 1.0) -> list[tuple[float, float]]:
    """连续两帧在底部出现相近文字时，前后各扩 1 秒作为高频采样窗口。"""
    windows: list[tuple[float, float]] = []
    for index in range(1, len(base_observations)):
        previous, current = base_observations[index - 1], base_observations[index]
        if current.start - previous.start > 1.5:
            continue
        if text_similarity(previous.text, current.text) < SUBTITLE_ACTIVATION_SIMILARITY:
            continue
        windows.append((max(0.0, previous.start - pad), current.start + pad))
    merged: list[list[float]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def run_ocr(video: Path, frame_dir: Path, engine_loader: Callable[[], object],
            *, policy: FramePolicy | None = None,
            asr_segments: Sequence[dict] | None = None,
            duration: float | None = None, runner: Callable | None = None,
            probe: Callable[[Path], float] | None = None) -> OcrOutcome:
    """两阶段自适应 OCR；任何失败都降级为 `failed`，不抛穿编排器。"""
    policy = policy or FramePolicy()
    video = Path(video)
    measured = float(duration if duration is not None else
                     (probe or _probe_duration)(video))
    try:
        engine = engine_loader()
    except Exception as exc:
        logger.warning("OCR 引擎不可用：%s", type(exc).__name__)
        return OcrOutcome(status="failed", observations=[],
                          degradations=["ocr_engine_unavailable"])

    try:
        base_frames = extract_adaptive_frames(
            video, frame_dir, policy, duration=measured, runner=runner, probe=probe)
        frame_size = _frame_size(base_frames[0].path) if base_frames else (0, 0)
        base_observations = _scan(base_frames, engine, frame_size, asr_segments)
        windows = _subtitle_windows(
            [item for item in base_observations
             if classify_text(item.text, item.bbox, frame_size,
                              stable_frames=2) == "speech_subtitle"]
            or base_observations)
        extra_frames: list[FrameRef] = []
        if windows:
            budget = max(policy.max_frames - len(base_frames), 0)
            if budget:
                extra_frames = extract_adaptive_frames(
                    video, frame_dir, policy, windows=windows,
                    duration=measured, runner=runner, probe=probe,
                    frame_size=frame_size)[len(base_frames):][:budget]
        extra_observations = _scan(extra_frames, engine, frame_size, asr_segments)
    except Exception as exc:
        logger.warning("OCR 处理失败，已降级：%s", type(exc).__name__)
        return OcrOutcome(status="failed", observations=[],
                          degradations=["ocr_failed"])

    labelled = _label(base_observations + extra_observations, frame_size)
    merged = merge_observations(labelled)
    return OcrOutcome(status="ok", observations=merged,
                      frames_scanned=len(base_frames) + len(extra_frames))
