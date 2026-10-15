"""本地 faster-whisper 语音识别：模型选择、GPU 锁与片段归一化。

输出完整原始口播稿与片段级诊断（时间戳、平均对数概率、低置信词、
静音/重复复核提示）。复核提示只建议人工核对，绝不改写原话。
faster_whisper 只在生产推理路径内导入，测试通过 loader 注入 fake model。
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import sys
import time
from collections import Counter
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

from scripts import paths
from scripts.models import utc_now

_logger = logging.getLogger("douyin_transcript.asr")

GPU_LOCK_PATH = paths.STATE_DIR / "gpu.lock"

AVG_LOGPROB_THRESHOLD = -1.0
WORD_PROBABILITY_THRESHOLD = 0.5
REPETITION_RATIO = 0.6
REPETITION_MIN_WORDS = 4
NO_SPEECH_THRESHOLD = 0.6
CPU_LONG_VIDEO_SECONDS = 1800

GPU_LOCK_WAIT_SECONDS = 3600
GPU_LOCK_POLL_SECONDS = 2.0


class AsrError(Exception):
    """语音识别失败或无有效文本。"""


class GpuLockTimeout(Exception):
    """等待 GPU 锁超时。"""


@dataclass(frozen=True)
class AsrProfile:
    model: str
    device: str
    compute_type: str


@dataclass(frozen=True)
class AsrStrategy:
    """一轮 ASR 的完整参数集；每轮只改一个主要变量（设计文档 11.4）。"""

    model: str
    device: str
    compute_type: str
    audio_variant: str = "original"
    language: str | None = None
    vad_filter: bool = True
    vad_parameters: dict | None = None
    condition_on_previous_text: bool | None = None
    repetition_penalty: float | None = None
    no_repeat_ngram_size: int | None = None
    beam_size: int = 5

    def as_profile(self) -> AsrProfile:
        return AsrProfile(self.model, self.device, self.compute_type)

    def as_record(self) -> dict:
        return {"model": self.model, "device": self.device,
                "compute_type": self.compute_type,
                "audio_variant": self.audio_variant, "language": self.language,
                "vad_filter": self.vad_filter,
                "vad_parameters": self.vad_parameters,
                "condition_on_previous_text": self.condition_on_previous_text,
                "repetition_penalty": self.repetition_penalty,
                "no_repeat_ngram_size": self.no_repeat_ngram_size,
                "beam_size": self.beam_size}


@dataclass(frozen=True)
class AsrAttemptResult:
    """一轮 ASR 的全部证据：文本、片段、诊断指标与失败定性。"""

    attempt_no: int
    audio_variant: str
    strategy: AsrStrategy
    transcript_text: str = ""
    segments: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    hard_failures: list = field(default_factory=list)
    score: float = 0.0
    passed: bool = False
    error_code: str = ""
    started_at: str = ""
    finished_at: str = ""

    @property
    def language(self) -> str:
        return str(self.strategy.language or "")

    def as_record(self) -> dict:
        return {"attempt_no": self.attempt_no,
                "audio_variant": self.audio_variant,
                "strategy": self.strategy.as_record(),
                "transcript_text": self.transcript_text,
                "segments": self.segments,
                "metrics": self.metrics,
                "hard_failures": self.hard_failures,
                "score": self.score, "passed": self.passed,
                "error_code": self.error_code,
                "started_at": self.started_at,
                "finished_at": self.finished_at}


_OOM_HINTS = ("out of memory", "cuda failed with error", "insufficient memory",
              "cublas_status_alloc_failed", "cudnn_status_alloc_failed")


def is_out_of_memory(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(hint in text for hint in _OOM_HINTS)


GPU_MEDIUM_MIN_VRAM_GB = 6.0


def gpu_vram_gb() -> float | None:
    """经 nvidia-smi 读取总显存（GB）；不可用时返回 None。"""
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10)
        if completed.returncode != 0:
            return None
        output = (completed.stdout or "").strip()
        if not output:
            return None
        import re
        match = re.search(r"\d+", output)
        return int(match.group()) / 1024.0 if match else None
    except OSError:
        return None


def _nvidia_bin_dirs(site_packages: Path) -> list[Path]:
    """pip 安装的 nvidia-* 运行时 wheel 把 DLL 放在 site-packages/nvidia/<pkg>/bin。"""
    root = Path(site_packages) / "nvidia"
    return sorted(d for d in root.glob("*/bin") if d.is_dir())


_registered_dll_dirs: list[object] = []


def _register_nvidia_dll_dirs(site_packages: Path | None = None) -> None:
    """把 nvidia wheel 的 bin 目录加入 DLL 搜索路径（Windows）。"""
    add_dll = getattr(os, "add_dll_directory", None)
    if add_dll is None:
        return
    if site_packages is None:
        site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    for bin_dir in _nvidia_bin_dirs(Path(site_packages)):
        try:
            _registered_dll_dirs.append(add_dll(str(bin_dir)))
        except OSError:
            continue


def cuda_runtime_available() -> bool:
    """探测 CTranslate2 CUDA 推理真正需要的运行时 DLL 是否可加载。

    CTranslate2 的 float16 能力声明不代表 cublas/cudnn 运行时存在；
    缺失时推理会在首次 encode 抛 RuntimeError，必须提前回退 CPU。
    """
    import ctypes

    _register_nvidia_dll_dirs()
    try:
        ctypes.CDLL("cublas64_12.dll")
    except OSError:
        return False
    for name in ("cudnn64_9.dll", "cudnn_ops64_9.dll"):
        try:
            ctypes.CDLL(name)
            return True
        except OSError:
            continue
    return False


def detect_cuda() -> bool:
    """仅当 CTranslate2 确认存在 CUDA 设备、支持 float16 且运行时 DLL 可加载时返回 True。"""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() <= 0:
            return False
        supported = set(ctranslate2.get_supported_compute_types("cuda", 0))
        if "float16" not in supported:
            return False
        return cuda_runtime_available()
    except Exception:
        return False


def select_profile(cuda_available: bool, duration_seconds: float,
                   vram_gb: float | None = None) -> AsrProfile:
    override = os.environ.get("DOUYIN_WHISPER_MODEL", "").strip()
    if cuda_available:
        device, compute_type = "cuda", "float16"
        # 对齐 B站策略：独显显存 ≥6GB 用 medium，否则 small
        default_model = ("medium" if vram_gb is not None
                         and vram_gb >= GPU_MEDIUM_MIN_VRAM_GB else "small")
    elif duration_seconds <= CPU_LONG_VIDEO_SECONDS:
        device, compute_type, default_model = "cpu", "int8", "base"
    else:
        device, compute_type, default_model = "cpu", "int8", "tiny"
    return AsrProfile(
        model=override or default_model, device=device, compute_type=compute_type
    )


def time_label(seconds: float) -> str:
    total = int(math.floor(max(0.0, float(seconds))))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


class GpuLock(AbstractContextManager):
    """跨平台独占文件锁，序列化 CUDA 推理；CPU 推理不持锁。"""

    def __init__(self, lock_file: Path = GPU_LOCK_PATH):
        self.lock_file = Path(lock_file)
        self._handle = None

    def acquire(self, timeout_seconds: float = GPU_LOCK_WAIT_SECONDS) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            handle = open(self.lock_file, "a+", encoding="utf-8")
            try:
                if self._try_lock(handle):
                    self._handle = handle
                    return
            except BaseException:
                handle.close()
                raise
            handle.close()
            if time.monotonic() >= deadline:
                raise GpuLockTimeout(f"等待 GPU 锁超时：{self.lock_file.name}")
            time.sleep(GPU_LOCK_POLL_SECONDS if timeout_seconds > 5 else 0.05)

    @staticmethod
    def _try_lock(handle) -> bool:
        import os

        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        import os

        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    def __enter__(self) -> "GpuLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def contains_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text or "")


def normalize_segment(segment) -> dict:
    words = []
    for word in getattr(segment, "words", None) or []:
        token = str(getattr(word, "word", "") or "").strip()
        probability = getattr(word, "probability", None)
        if token:
            words.append((token, float(probability)
                          if isinstance(probability, (int, float)) else 1.0))

    avg_logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
    no_speech_prob = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
    low_confidence_words = [token for token, prob in words
                            if prob < WORD_PROBABILITY_THRESHOLD]

    reasons = []
    if avg_logprob < AVG_LOGPROB_THRESHOLD:
        reasons.append("low_avg_logprob")
    if low_confidence_words:
        reasons.append("low_confidence_words")
    if len(words) >= REPETITION_MIN_WORDS:
        counts = Counter(token for token, _ in words)
        if counts and counts.most_common(1)[0][1] / len(words) >= REPETITION_RATIO:
            reasons.append("repetitive")
    if no_speech_prob > NO_SPEECH_THRESHOLD:
        reasons.append("high_silence_prob")

    return {
        "start": round(float(getattr(segment, "start", 0.0) or 0.0), 2),
        "end": round(float(getattr(segment, "end", 0.0) or 0.0), 2),
        "text": str(getattr(segment, "text", "") or "").strip(),
        "avg_logprob": round(avg_logprob, 4),
        "no_speech_prob": round(no_speech_prob, 4),
        "low_confidence_words": low_confidence_words,
        "needs_review": bool(reasons),
        "review_reasons": reasons,
    }


def default_loader(profile: AsrProfile):
    """生产路径：此处才导入 faster_whisper 并加载模型。"""
    from faster_whisper import WhisperModel

    return WhisperModel(
        profile.model,
        device=profile.device,
        compute_type=profile.compute_type,
        download_root=str(paths.STATE_DIR / "models"),
    )


def transcribe(
    audio_path: Path,
    duration_seconds: float,
    title: str,
    *,
    loader=default_loader,
) -> tuple[str, list[dict], AsrProfile, str]:
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise AsrError("音频文件不存在，无法识别")

    cuda = detect_cuda()
    vram_gb = gpu_vram_gb() if cuda else None
    profile = select_profile(cuda, duration_seconds, vram_gb)
    if os.environ.get("DOUYIN_WHISPER_MODEL", "").strip():
        reason = f"环境变量 DOUYIN_WHISPER_MODEL 覆盖为 {profile.model}"
    elif cuda:
        reason = (f"GPU 吞吐优先（显存约 {vram_gb:.0f}GB，"
                  f"{'≥6GB → medium' if profile.model == 'medium' else '<6GB → small'}）")
    elif duration_seconds <= CPU_LONG_VIDEO_SECONDS:
        reason = "CPU 且不超过 30 分钟 → base/int8"
    else:
        reason = "CPU 长视频 → tiny/int8（避免等待过久）"
    _logger.info("📐 语音识别档位：%s（%s / %s），原因：%s",
                 profile.model, profile.device, profile.compute_type, reason)
    force_language = "zh" if contains_cjk(title) else None

    try:
        if cuda:
            with GpuLock():
                model = loader(profile)
                raw_segments, info = _run_model(
                    model, audio_path, profile, force_language)
                raw_segments = list(raw_segments)  # 惰性生成器必须在锁内物化
        else:
            model = loader(profile)
            raw_segments, info = _run_model(
                model, audio_path, profile, force_language)
            raw_segments = list(raw_segments)
    except AsrError:
        raise
    except GpuLockTimeout:
        raise
    except Exception as exc:
        raise AsrError(f"Whisper 识别失败：{type(exc).__name__}: {exc}") from exc

    segments = []
    for raw in raw_segments:
        item = normalize_segment(raw)
        if item["text"]:
            segments.append(item)
    if not segments:
        raise AsrError("识别结果为空文本")

    language = force_language or str(getattr(info, "language", "") or "")
    transcript_text = "\n".join(item["text"] for item in segments)
    return transcript_text, segments, profile, language


def _transcribe_kwargs(strategy: AsrStrategy) -> dict:
    """把策略完整翻译成 faster-whisper 参数；None 表示不覆盖其默认值。"""
    kwargs: dict = {
        "vad_filter": strategy.vad_filter,
        "beam_size": strategy.beam_size,
        "word_timestamps": True,
    }
    if strategy.language:
        kwargs["language"] = strategy.language
    if strategy.vad_parameters:
        kwargs["vad_parameters"] = dict(strategy.vad_parameters)
    if strategy.condition_on_previous_text is not None:
        kwargs["condition_on_previous_text"] = strategy.condition_on_previous_text
    if strategy.repetition_penalty is not None:
        kwargs["repetition_penalty"] = strategy.repetition_penalty
    if strategy.no_repeat_ngram_size is not None:
        kwargs["no_repeat_ngram_size"] = strategy.no_repeat_ngram_size
    return kwargs


def _collect_metrics(info, segments: list, strategy: AsrStrategy) -> dict:
    word_count = sum(len(item.get("low_confidence_words", [])) or
                     len(str(item.get("text", "")).split())
                     for item in segments)
    try:
        duration = float(getattr(info, "duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    try:
        duration_after_vad = float(getattr(info, "duration_after_vad", 0.0) or 0.0)
    except (TypeError, ValueError):
        duration_after_vad = 0.0
    try:
        language_probability = float(
            getattr(info, "language_probability", 0.0) or 0.0)
    except (TypeError, ValueError):
        language_probability = 0.0
    return {
        "duration": duration,
        "duration_after_vad": duration_after_vad,
        "language": str(getattr(info, "language", "") or ""),
        "language_probability": language_probability,
        "segment_count": len(segments),
        "word_count": word_count,
        "strategy": strategy.as_record(),
    }


def run_attempt(audio_path: Path, strategy: AsrStrategy, *,
                loader=default_loader, attempt_no: int = 1,
                lock_factory=None) -> AsrAttemptResult:
    """执行一轮 ASR，返回完整 attempt；失败只定性不抛出。"""
    started = utc_now()
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        return AsrAttemptResult(attempt_no=attempt_no,
                                audio_variant=strategy.audio_variant,
                                strategy=strategy, error_code="audio_invalid",
                                hard_failures=["audio_invalid"],
                                started_at=started, finished_at=utc_now())

    lock_factory = lock_factory or GpuLock

    def _invoke():
        model = loader(strategy.as_profile())
        raw_segments, info = model.transcribe(
            str(audio_path), **_transcribe_kwargs(strategy))
        # 惰性生成器必须在锁内物化，否则会在释放锁后继续占用显存
        return list(raw_segments), info

    try:
        if strategy.device == "cuda":
            with lock_factory():
                raw_segments, info = _invoke()
        else:
            raw_segments, info = _invoke()
    except Exception as exc:
        code = "asr_oom" if is_out_of_memory(exc) else "asr_failed"
        failure = "out_of_memory" if code == "asr_oom" else "asr_error"
        _logger.warning("第 %d 轮识别失败（%s）：%s", attempt_no, code,
                        type(exc).__name__)
        return AsrAttemptResult(attempt_no=attempt_no,
                                audio_variant=strategy.audio_variant,
                                strategy=strategy, error_code=code,
                                hard_failures=[failure], score=0.0,
                                passed=False, started_at=started,
                                finished_at=utc_now(),
                                metrics={"error": type(exc).__name__})

    segments = [normalize_segment(raw) for raw in raw_segments]
    segments = [item for item in segments if item["text"]]
    metrics = _collect_metrics(info, segments, strategy)
    metrics["raw_segment_count"] = len(raw_segments)
    text = "\n".join(item["text"] for item in segments)
    hard_failures = [] if segments else ["empty_output"]
    return AsrAttemptResult(
        attempt_no=attempt_no, audio_variant=strategy.audio_variant,
        strategy=strategy, transcript_text=text, segments=segments,
        metrics=metrics, hard_failures=hard_failures, score=0.0,
        passed=False, error_code="" if segments else "asr_failed",
        started_at=started, finished_at=utc_now())


def _run_model(model, audio_path: Path, profile: AsrProfile, force_language):
    kwargs = {
        "vad_filter": True,
        "beam_size": 5,
        "word_timestamps": True,
    }
    if force_language:
        kwargs["language"] = force_language
    return model.transcribe(str(audio_path), **kwargs)
