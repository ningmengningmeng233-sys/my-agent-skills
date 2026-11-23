"""音频分析与自适应音轨生成。

策略是“按需增强”：只有测量到的响度/削波异常才生成增强 WAV，
只有首轮 ASR 出现高静音概率或重复幻觉时才生成人声分离音轨。
任何增强失败都只禁用该候选，原音永远可用。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.media import verify_audio

logger = logging.getLogger("douyin_transcript.audio")

_PROBE_TIMEOUT = 120
_ENCODE_TIMEOUT = 900
_LOW_LOUDNESS_LUFS = -30.0
_CLIPPED_RATIO_LIMIT = 0.01
_TARGET_SAMPLE_RATE = 16000
_ENHANCE_FILTER = (
    "highpass=f=60,lowpass=f=8000,"
    "loudnorm=I=-16:TP=-1.5:LRA=11"
)
_LOUDNESS_PATTERN = re.compile(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS")
_PEAK_PATTERN = re.compile(r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS")
_SILENCE_PATTERN = re.compile(r"silence_duration:\s*(\d+(?:\.\d+)?)")
_CLIP_PATTERN = re.compile(r"(\d+)\s+clipped", re.IGNORECASE)


class AudioPipelineError(Exception):
    """音频分析/增强失败；`code` 为稳定错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AudioAnalysis:
    duration: float
    loudness_lufs: float
    peak_db: float
    clipped_ratio: float
    silence_ratio: float
    sample_rate: int = 0
    channels: int = 0

    @property
    def needs_enhancement(self) -> bool:
        return (self.loudness_lufs < _LOW_LOUDNESS_LUFS
                or self.clipped_ratio > _CLIPPED_RATIO_LIMIT
                or self.peak_db > 0.0)

    def as_dict(self) -> dict:
        return {"duration": self.duration, "loudness_lufs": self.loudness_lufs,
                "peak_db": self.peak_db, "clipped_ratio": self.clipped_ratio,
                "silence_ratio": self.silence_ratio,
                "sample_rate": self.sample_rate, "channels": self.channels}


@dataclass(frozen=True)
class AudioVariant:
    kind: str
    path: Path | None
    analysis: AudioAnalysis | None = None
    generator: str = ""
    usable: bool = True
    detail: str = ""

    def as_record(self) -> dict:
        return {"kind": self.kind,
                "path": str(self.path) if self.path else "",
                "generator": self.generator,
                "usable": self.usable,
                "detail": self.detail,
                "analysis": self.analysis.as_dict() if self.analysis else None}


def original_variant(path: Path, analysis: AudioAnalysis | None = None
                     ) -> AudioVariant:
    return AudioVariant(kind="original", path=Path(path), analysis=analysis,
                        generator="upstream")


def degraded_variant(kind: str, detail: str,
                     generator: str = "") -> AudioVariant:
    return AudioVariant(kind=kind, path=None, generator=generator, usable=False,
                        detail=detail)


def usable_variants(variants: list[AudioVariant]) -> list[AudioVariant]:
    return [item for item in variants if item.usable and item.path is not None]


def _run_probe(path: Path) -> dict:
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioPipelineError("audio_invalid", "无法读取音频元数据") from exc
    if completed.returncode != 0:
        raise AudioPipelineError("audio_invalid", "音频无法解析")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AudioPipelineError("audio_invalid", "音频元数据不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise AudioPipelineError("audio_invalid", "音频元数据格式异常")
    return payload


def _run_filter(path: Path, filter_chain: str) -> str:
    try:
        completed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
             "-af", filter_chain, "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_ENCODE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stderr or ""


def _last_measurement(pattern: re.Pattern, text: str,
                      default: float) -> float:
    """ebur128 逐帧打印 I/FTPK，末尾 Summary 才是整段结论：取最后一个匹配。"""
    matches = pattern.findall(text or "")
    if not matches:
        return default
    try:
        return float(matches[-1])
    except (TypeError, ValueError):
        return default


def analyze_audio(path: Path) -> AudioAnalysis:
    """测量时长、响度、峰值、削波与静音比例，用于选择音轨策略。"""
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise AudioPipelineError("audio_invalid", "音频文件不存在或为空")

    payload = _run_probe(path)
    streams = payload.get("streams")
    audio_stream = next(
        (item for item in streams if isinstance(item, dict)
         and item.get("codec_type") == "audio"), None) if isinstance(streams, list) \
        else None
    if audio_stream is None:
        raise AudioPipelineError("audio_invalid", "音频缺少可解码音轨")
    try:
        duration = float((payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise AudioPipelineError("audio_invalid", "音频时长为零")

    loudness = _run_filter(path, "ebur128=peak=true")
    peak_text = _run_filter(path, "astats=metadata=1")
    silence = _run_filter(path, "silencedetect=n=-40dB:d=1")

    lufs = _last_measurement(_LOUDNESS_PATTERN, loudness, -99.0)
    peak = _last_measurement(_PEAK_PATTERN, loudness, 0.0)
    silence_total = sum(float(value) for value in
                        _SILENCE_PATTERN.findall(silence))
    clip_match = _CLIP_PATTERN.search(peak_text)

    return AudioAnalysis(
        duration=duration,
        loudness_lufs=lufs,
        peak_db=peak,
        clipped_ratio=(int(clip_match.group(1)) / max(duration, 1.0) / 1000.0
                       if clip_match else 0.0),
        silence_ratio=min(silence_total / duration, 1.0),
        sample_rate=int(audio_stream.get("sample_rate") or 0),
        channels=int(audio_stream.get("channels") or 0),
    )


def create_enhanced(audio: Path, job_dir: Path, *,
                    analysis: AudioAnalysis | None = None) -> AudioVariant | None:
    """响度或削波异常时生成 16 kHz 单声道增强 WAV；清晰音频返回 None。"""
    audio = Path(audio)
    measured = analysis or analyze_audio(audio)
    if not measured.needs_enhancement:
        return None

    target = Path(job_dir) / "derived" / f"{audio.stem}.enhanced.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    argv = ["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", str(audio),
            "-vn", "-af", _ENHANCE_FILTER, "-ar", str(_TARGET_SAMPLE_RATE),
            "-ac", "1", "-c:a", "pcm_s16le", str(target)]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=_ENCODE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioPipelineError("audio_enhancement_failed",
                                 "增强音频生成失败") from exc
    if completed.returncode != 0 or not target.is_file():
        target.unlink(missing_ok=True)
        raise AudioPipelineError("audio_enhancement_failed", "增强音频生成失败")

    try:
        duration = verify_audio(target)
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise AudioPipelineError("audio_enhancement_failed",
                                 "增强音频未通过验证") from exc
    if not duration:
        target.unlink(missing_ok=True)
        raise AudioPipelineError("audio_enhancement_failed", "增强音频时长为零")
    return AudioVariant(kind="enhanced", path=target, analysis=measured,
                        generator="ffmpeg-loudnorm",
                        detail="响度或削波异常，使用增强音轨")


def _separator_class():
    import sherpa_onnx

    return sherpa_onnx.OfflineSourceSeparation


def _load_mono_samples(path: Path) -> tuple[list[float], int]:
    """用 ffmpeg 解码成 16 kHz 单声道浮点样本，避免额外音频库依赖。"""
    process = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(_TARGET_SAMPLE_RATE),
         "-f", "f32le", "-"],
        capture_output=True, timeout=_ENCODE_TIMEOUT)
    if process.returncode != 0:
        raise AudioPipelineError("audio_invalid", "音频解码失败")
    import array

    samples = array.array("f")
    samples.frombytes(process.stdout)
    return list(samples), _TARGET_SAMPLE_RATE


def _write_wav(path: Path, samples, sample_rate: int) -> None:
    import wave

    import numpy as np

    data = np.asarray(samples, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=0)
    clipped = np.clip(data, -1.0, 1.0)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes((clipped * 32767).astype("<i2").tobytes())


def create_vocals(audio: Path, job_dir: Path, *, model_path: Path,
                  config=None) -> AudioVariant:
    """用 Sherpa-ONNX UVR 生成人声轨；模型缺失或推理失败只降级，不抛穿。"""
    model_path = Path(model_path)
    if not model_path.is_file():
        logger.warning("人声分离模型缺失，跳过该音轨")
        return degraded_variant("vocals", "人声分离模型缺失",
                                generator="sherpa-uvr")
    target = Path(job_dir) / "derived" / f"{Path(audio).stem}.vocals.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        samples, sample_rate = _load_mono_samples(Path(audio))
        import numpy as np

        separator_cls = config or _separator_class()
        separator = separator_cls(_uvr_config(model_path))
        stereo = np.stack([np.asarray(samples, dtype="float32"),
                           np.asarray(samples, dtype="float32")])
        output = separator.process(sample_rate, stereo)
        stems = getattr(output, "stems", None)
        if not stems:
            raise AudioPipelineError("audio_enhancement_failed", "人声分离无输出")
        vocals = getattr(stems[0], "data", None)
        if vocals is None:
            raise AudioPipelineError("audio_enhancement_failed", "人声分离输出不可用")
        _write_wav(target, vocals, int(getattr(output, "sample_rate", sample_rate)))
    except Exception as exc:  # 增强失败只禁用该音轨
        logger.warning("人声分离失败，将退回原音：%s", type(exc).__name__)
        target.unlink(missing_ok=True)
        return degraded_variant("vocals", "人声分离推理失败",
                                generator="sherpa-uvr")

    try:
        duration = verify_audio(target)
    except Exception:
        duration = 0.0
    if not duration:
        target.unlink(missing_ok=True)
        return degraded_variant("vocals", "人声分离结果未通过验证",
                                generator="sherpa-uvr")
    return AudioVariant(kind="vocals", path=target, generator="sherpa-uvr",
                        detail="强背景音乐候选音轨")


def _uvr_config(model_path: Path):
    import sherpa_onnx

    return sherpa_onnx.OfflineSourceSeparationConfig(
        model=sherpa_onnx.OfflineSourceSeparationModelConfig(
            uvr=sherpa_onnx.OfflineSourceSeparationUvrModelConfig(
                model=str(model_path)),
            num_threads=2, debug=False, provider="cpu"))


def build_candidates(audio: Path, job_dir: Path, *, analysis: AudioAnalysis | None,
                     vocals_model: Path | None = None,
                     want_vocals: bool = False) -> list[AudioVariant]:
    """按策略给出本轮可用的音轨候选；原始音轨永远排在最前面。"""
    variants = [original_variant(audio, analysis)]
    if analysis is None:
        return variants
    if analysis.needs_enhancement:
        try:
            enhanced = create_enhanced(audio, job_dir, analysis=analysis)
        except AudioPipelineError as exc:
            logger.warning("增强音轨不可用：%s", exc.message)
            enhanced = None
        if enhanced is not None:
            variants.append(enhanced)
    if want_vocals and vocals_model is not None:
        variants.append(create_vocals(audio, job_dir, model_path=vocals_model))
    return variants
