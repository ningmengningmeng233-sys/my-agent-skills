#!/usr/bin/env python3
"""长音频的显存/耗时预算：默认「快速失败」，分段转写是显式开启的可选项。

## 默认策略（用户 2026-09-18 Phase 0 拍板）：快速失败

faster-whisper 1.2 的 feature_extractor 对「整个音频数组」一次性算 log-mel 声谱图
（transcribe.py 里 `self.feature_extractor(audio)` 没有分段循环），产物随时长线性增长：

    显存需求 ≈ 音频分钟数 × 0.018 GiB（声谱图）+ ~1.5 GiB（medium 模型）

实测锚点：420 分钟音频报 `Unable to allocate 7.55 GiB for an array with shape
(1, 2520906, 201)`（2520906 帧 ÷ 100 帧/秒 = 420 分钟，×201 频点 ×16 字节 = 7.55 GiB）。
超阈值时**直接判为本机不可转并说明原因**，写进清单换机；不再让它 OOM 后退回 CPU
硬磨数小时（实测 2026-09-18 白烧近 3 小时）。

## 可选项：分段转写（BILI_ASR_CHUNKING=1）

按预算切段、逐段转写再把时间轴平移到绝对时间后拼接，切点吸附到低能量处。
2026-09-18 实测：10 分钟真实音频切 6 段，136 段结果时间轴严格递增、末段结束 600.06s
对得上音频 600.1s；6 小时真实音频切 13 段，整段需 21.1 GiB 的峰值被压到 3 GiB 以内，
首段 19 分钟跑完无 OOM。默认关闭是因为拍板时认为「切段的边界断词与时间轴错位风险
高于收益」——该判断在实测后由用户复核，代码这边只保证开关是显式的、默认不生效。
"""

from __future__ import annotations

import math
import os
import subprocess

# 整段声谱图（complex128）的内存系数，单位 GiB/分钟。
# 帧率 100/s（10ms hop）、201 个频点、每点 16 字节 ⇒ 6000×201×16 B/分钟。
MEL_GB_PER_MINUTE = 0.018
# 模型常驻占用（权重 + CUDA 上下文/工作区）的经验值。
MODEL_RESIDENT_GB = {"tiny": 0.6, "base": 0.8, "small": 1.2,
                     "medium": 1.6, "large-v3": 2.4}
_DEFAULT_MODEL_GB = 1.6
# CPU 上的峰值系数：complex128 声谱图之外还有 float32 分帧副本与 complex64 副本同时在场。
# 显存侧不需要这个系数——进显存的是算完的声谱图，实测锚点就是 1×（见模块开头）。
_CPU_PEAK_FACTOR = 3.0
# 空闲显存要留的余量：分配碎片与推理期间的临时张量。
_VRAM_SAFETY = 0.95
# 单段上限：与 Whisper 的 30 秒窗口是整数倍关系，留足上下文又不会太长。
DEFAULT_CHUNK_SECONDS = 1800.0
MIN_CHUNK_SECONDS = 120.0
# 未显式给出预算时的整机/显存允许峰值。
DEFAULT_MEMORY_BUDGET_GB = 8.0
# 预算下限（GiB）：比这更低时切段也救不了（模型本身就占 1 个多 GiB），
# 仍然按最小段长硬跑，而不是把段长算成负数或退化成「不分段」。
MIN_MEMORY_BUDGET_GB = 2.0
DEFAULT_CPU_BUDGET_SECONDS = 3 * 3600

# 实时率：处理 1 秒音频所需的墙钟秒数。用于「预估耗时」与 CPU 预算拦截。
# GPU 侧按 6 GB 级消费卡校准（medium + beam5 + 逐词时间戳，约 0.7 倍实时）；
# 其余档位按 Whisper 常见速度比缩放。换更强或更弱的卡请自行用 --probe 复测。
_RTF = {
    "cuda": {"tiny": 0.04, "base": 0.09, "small": 0.25,
             "medium": 0.70, "large-v3": 1.40},
    "cpu": {"tiny": 0.10, "base": 0.22, "small": 0.55,
            "medium": 1.40, "large-v3": 2.40},
}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        value = float(str(raw).strip())
    except ValueError:
        return float(default)
    return value if value > 0 else float(default)


def model_resident_gb(model: str) -> float:
    return MODEL_RESIDENT_GB.get(str(model or "").lower(), _DEFAULT_MODEL_GB)


def mel_gb(seconds: float) -> float:
    """整段 log-mel 声谱图的内存占用（GiB）。"""
    return max(0.0, float(seconds or 0.0)) / 60.0 * MEL_GB_PER_MINUTE


def peak_gb(seconds: float, model: str, *, device: str = "cpu") -> float:
    """整段转写的峰值占用估值（GiB）。

    显存侧按实测口径：进显存的是算完的声谱图，需求 = 声谱图 + 模型常驻。
    内存侧还要算上分帧副本与 complex64 副本，故乘系数。
    """
    mel = mel_gb(seconds)
    if str(device).lower() == "cuda":
        return mel + model_resident_gb(model)
    return mel * _CPU_PEAK_FACTOR + model_resident_gb(model)


def chunking_enabled() -> bool:
    """分段转写是否启用（默认关闭）。

    默认走「快速失败」：超阈值直接判本机不可转并说明原因，写进清单换机。
    开启 `BILI_ASR_CHUNKING=1` 才切段——切段的边界风险由使用者显式承担。
    """
    raw = str(os.environ.get("BILI_ASR_CHUNKING") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def estimate_wall_seconds(seconds: float, *, model: str, device: str) -> float:
    """预估墙钟耗时（秒）；仅用于提示与 CPU 预算拦截。"""
    table = _RTF.get("cuda" if str(device).lower() == "cuda" else "cpu")
    rtf = float(table.get(str(model or "").lower(), table["medium"]))
    return max(0.0, float(seconds or 0.0)) * rtf


def free_vram_gb() -> float | None:
    """当前空闲显存（GiB）；拿不到 nvidia-smi 时返回 None。"""
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        return float(output.stdout.strip().splitlines()[0]) / 1024.0
    except Exception:  # noqa: BLE001 无 nvidia-smi／无 GPU 都走这里
        return None


def memory_budget_gb(*, device: str) -> float:
    """允许的峰值占用：GPU 看空闲显存，CPU 看内存预算。"""
    override = _env_float("BILI_ASR_MEMORY_BUDGET_GB", 0.0)
    if override > 0:
        return override
    if str(device).lower() == "cuda":
        free = free_vram_gb()
        if free is None:
            return DEFAULT_MEMORY_BUDGET_GB
        # 留 15% 给分配碎片与推理期间的临时张量。注意这里的下限是「GiB」，
        # 不是最小段长（秒）——两者单位不同。可用显存只有 1 GiB 出头时，
        # 段长会被 plan_boundaries 收敛到 MIN_CHUNK_SECONDS。
        return max(MIN_MEMORY_BUDGET_GB, free * 0.85)
    return DEFAULT_MEMORY_BUDGET_GB


def max_chunk_seconds() -> float:
    return _env_float("BILI_ASR_MAX_CHUNK_SECONDS", DEFAULT_CHUNK_SECONDS)


def plan_boundaries(duration_s: float, *, model: str, device: str,
                    budget_gb: float | None = None,
                    chunk_seconds: float | None = None) -> list[float]:
    """需要切段时返回切点（秒，不含 0 与总时长）；不切段时返回空表。

    分段默认关闭：未开 `BILI_ASR_CHUNKING=1` 时永远返回空表，
    超阈值音频由 `vram_refusal` / `cpu_budget_error` 快速失败。
    """
    try:
        duration = float(duration_s or 0.0)
    except (TypeError, ValueError):
        return []
    if duration <= 0.0 or not chunking_enabled():
        return []
    budget = (memory_budget_gb(device=device)
              if budget_gb is None else float(budget_gb))
    limit = max_chunk_seconds() if chunk_seconds is None else float(chunk_seconds)
    if peak_gb(duration, model, device=device) <= budget:
        return []
    usable = budget - model_resident_gb(model)
    factor = 1.0 if str(device).lower() == "cuda" else _CPU_PEAK_FACTOR
    allowed = (usable / factor / MEL_GB_PER_MINUTE * 60.0
               if usable > 0 else MIN_CHUNK_SECONDS)
    size = min(limit, max(MIN_CHUNK_SECONDS, allowed))
    count = int(math.ceil(duration / size))
    if count <= 1:
        return []
    step = duration / count
    return [round(step * index, 3) for index in range(1, count)]


def quiet_cut(audio, target: int, sample_rate: int, *,
              search_seconds: float = 8.0, window_seconds: float = 0.4) -> int:
    """把切点吸附到目标位置附近的低能量处，避免把字词切两半。"""
    total = int(len(audio))
    if total <= 0:
        return 0
    window = max(1, int(window_seconds * sample_rate))
    if total <= window:
        return total
    span = int(max(0.0, search_seconds) * sample_rate)
    low = max(0, int(target) - span)
    high = min(total - window, int(target) + span)
    if high <= low:
        return max(0, min(int(target), total - window))
    step = max(1, int(0.1 * sample_rate))
    best_position = low
    best_score = None
    for position in range(low, high + 1, step):
        score = float(abs(audio[position:position + window]).mean())
        if best_score is None or score < best_score:
            best_position, best_score = position, score
    return best_position


def slice_ranges(audio, sample_rate: int, boundaries: list[float], *,
                 search_seconds: float = 8.0) -> list[tuple[int, int]]:
    """把切点转成互不重叠、无缝覆盖的样本区间。"""
    total = int(len(audio))
    if total <= 0:
        return []
    cuts: list[int] = []
    for boundary in boundaries or []:
        position = quiet_cut(audio, int(float(boundary) * sample_rate),
                            sample_rate, search_seconds=search_seconds)
        if cuts and position <= cuts[-1]:
            position = cuts[-1]
        if 0 < position < total:
            cuts.append(position)
    ranges = []
    start = 0
    for cut in cuts:
        ranges.append((start, cut))
        start = cut
    ranges.append((start, total))
    return [(a, b) for a, b in ranges if b > a]


class _Shifted:
    """给没有 `_replace` 的替身对象做时间轴平移（测试替身走这条）。"""

    def __init__(self, inner, offset: float):
        self._inner = inner
        self._offset = float(offset)

    def __getattr__(self, name):
        if name in ("start", "end"):
            return float(getattr(self._inner, name)) + self._offset
        if name == "words":
            inner = getattr(self._inner, "words", None)
            if not inner:
                return inner
            return [shift_segment(word, self._offset) for word in inner]
        return getattr(self._inner, name)


def shift_segment(segment, offset: float):
    """整体平移一段（含逐词时间轴）到绝对时间轴。"""
    if not offset:
        return segment
    try:
        words = getattr(segment, "words", None)
        shifted_words = ([shift_segment(word, offset) for word in words]
                         if words else words)
        return segment._replace(
            start=float(getattr(segment, "start", 0.0)) + offset,
            end=float(getattr(segment, "end", 0.0)) + offset,
            **({"words": shifted_words} if words else {}))
    except (AttributeError, TypeError):
        return _Shifted(segment, offset)


class AudioInfo:
    """分段结果的合并 info：字段与 faster-whisper 的 TranscriptionInfo 对齐。"""

    def __init__(self, *, language: str = "", language_probability: float = 0.0,
                 duration: float = 0.0):
        self.language = language
        self.language_probability = float(language_probability or 0.0)
        self.duration = float(duration or 0.0)
        self.duration_after_vad = self.duration


def read_audio(audio, sample_rate: int = 16000):
    """解码为单声道 float32 数组（faster-whisper 自带解码器）。"""
    from faster_whisper.audio import decode_audio

    return decode_audio(audio, sampling_rate=sample_rate)


def probe_duration(audio, sample_rate: int = 16000) -> float:
    """只读容器时长（不解码），拿不到就退回完整解码计数；仍失败返回 0。"""
    try:
        import av

        with av.open(str(audio)) as container:
            if container.duration:
                return float(container.duration) / float(av.time_base)
    except Exception:  # noqa: BLE001 缺少 PyAV／容器异常都走解码兜底
        pass
    try:
        return float(len(read_audio(audio, sample_rate))) / float(sample_rate)
    except Exception:  # noqa: BLE001 读不出时长就不做预算判断
        return 0.0


def _warn(log, message: str) -> None:
    if log is not None:
        log(message)


def plan_report(duration_s: float, *, model: str, device: str,
                budget_gb: float | None = None,
                chunk_seconds: float | None = None) -> str:
    """一行说明本次转写的分段与耗时预估，供日志留痕。"""
    budget = (memory_budget_gb(device=device)
              if budget_gb is None else float(budget_gb))
    boundaries = plan_boundaries(duration_s, model=model, device=device,
                                budget_gb=budget, chunk_seconds=chunk_seconds)
    count = len(boundaries) + 1
    chunk = (format_duration(float(duration_s) / count) if count > 1 else "整段")
    if count == 1:
        need = peak_gb(duration_s, model, device=device)
        if need > budget:
            policy = ("超出预算，按默认策略快速失败"
                      if not chunking_enabled() else "超出预算")
            shape = f"整段转写（需 {need:.1f} GiB，{policy}）"
        else:
            shape = f"整段转写（需 {need:.1f} GiB）"
    else:
        shape = f"分 {count} 段（每段约 {chunk}）"
    return (f"音频 {format_duration(duration_s)}｜峰值预算 {budget:.1f} GiB｜"
            f"{shape}｜"
            f"预计耗时 {format_duration(estimate_wall_seconds(duration_s, model=model, device=device))}")


def vram_refusal(seconds: float, *, model: str, device: str,
                 free_gb: float | None = None) -> str:
    """显存侧的快速失败判定；返回空串表示放行。

    默认策略（快速失败）：超阈值直接判「本机不可转」并说明换机，
    不切段、不退回 CPU 空磨。实测锚点：420 分钟音频需 7.55 GiB 声谱图 + 模型，
    6 GB 显存卡必然 OOM。

    开了分段（`BILI_ASR_CHUNKING=1`）就不再有「整段声谱图」这个天花板，
    此时一律放行——拒绝就等于让开关失效。
    """
    if str(device).lower() != "cuda":
        return ""
    if chunking_enabled():
        return ""
    need = peak_gb(seconds, model, device="cuda")
    free = free_vram_gb() if free_gb is None else float(free_gb)
    if free is None:
        return ""
    budget = free * _VRAM_SAFETY
    if need <= budget:
        return ""
    return (f"本机不可转：{format_duration(seconds)} 音频需约 {need:.1f} GiB 显存，"
            f"当前空闲仅 {free:.1f} GiB（阈值 {budget:.1f} GiB）"
            f"——请关掉占用显存的程序后重试，或按「无法转录清单」换机；"
            f"如确要本机跑完，可设 BILI_ASR_CHUNKING=1 启用分段转写"
            f"（切段有边界断词风险，需自行复核）")


def cpu_budget_error(seconds: float, *, model: str, device: str,
                     budget_seconds: float | None = None) -> str:
    """CPU 路径的耗时预算拦截；返回空串表示放行。

    为什么要有：显存不足回退 CPU 后，medium/int8 处理 6~8 小时音频要磨好几个
    小时才失败，期间没有任何产出。宁可立刻失败并说明原因，也不无声空转。
    """
    if str(device).lower() == "cuda":
        return ""
    budget = (DEFAULT_CPU_BUDGET_SECONDS if budget_seconds is None
              else float(budget_seconds))
    estimate = estimate_wall_seconds(seconds, model=model, device="cpu")
    if budget <= 0 or estimate <= budget:
        return ""
    return (f"CPU 预计耗时 {format_duration(estimate)}"
            f"（音频 {format_duration(seconds)}、模型 {model}）"
            f"超出预算 {format_duration(budget)}；"
            f"如需强行执行，请设 BILI_ASR_CPU_BUDGET_SECONDS={int(estimate) + 60}")


def transcribe_chunked(model, audio, *, model_name: str, device: str,
                       sample_rate: int = 16000, budget_gb: float | None = None,
                       chunk_seconds: float | None = None, log=None, **kwargs):
    """按预算决定是否分段转写；返回 (segments, info)。

    - 短音频或预算充足：与改造前完全一致，直接把整段交给模型；
    - 需要分段：逐段转写再把时间轴平移到绝对时间，拼接后返回；
    - 音频解码不可用（格式异常/缺少解码器/测试替身）：退化回整段调用，
      绝不让「预算检查」本身成为新的失败点。
    """
    array = audio
    if not hasattr(audio, "shape"):
        try:
            array = read_audio(audio, sample_rate)
        except Exception as exc:  # noqa: BLE001 解码失败不该阻断转写
            _warn(log, f"分段预算：音频解码不可用（{type(exc).__name__}），"
                       f"按整段转写")
            segments, info = model.transcribe(audio, **kwargs)
            return list(segments), info

    total = float(len(array)) / float(sample_rate)
    budget = (memory_budget_gb(device=device)
              if budget_gb is None else float(budget_gb))
    boundaries = plan_boundaries(total, model=model_name, device=device,
                                budget_gb=budget, chunk_seconds=chunk_seconds)
    if not boundaries:
        _warn(log, f"分段预算：{plan_report(total, model=model_name, device=device, budget_gb=budget, chunk_seconds=chunk_seconds)}")
        segments, info = model.transcribe(array, **kwargs)
        return list(segments), info

    ranges = slice_ranges(array, sample_rate, boundaries)
    _warn(log, f"分段预算：{format_duration(total)} → {len(ranges)} 段，"
               f"峰值预算 {budget:.1f} GiB"
               f"（整段需 {peak_gb(total, model_name, device=device):.1f} GiB）")
    collected: list = []
    language = ""
    language_probability = 0.0
    for index, (start_sample, end_sample) in enumerate(ranges, 1):
        offset = start_sample / float(sample_rate)
        chunk = array[start_sample:end_sample]
        segments, info = model.transcribe(chunk, **kwargs)
        got = [shift_segment(segment, offset) for segment in segments]
        collected.extend(got)
        if not language:
            language = str(getattr(info, "language", "") or "")
            language_probability = float(
                getattr(info, "language_probability", 0.0) or 0.0)
        _warn(log, f"   分段 {index}/{len(ranges)}：{format_duration(offset)} 起，"
                   f"{len(got)} 段")
    return collected, AudioInfo(language=language,
                                language_probability=language_probability,
                                duration=total)


def format_duration(seconds: float) -> str:
    total = int(max(0.0, float(seconds or 0.0)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"
