"""ASR 一轮结果与策略的最小定义——质量评分的输入契约。

来源：抖音版 `scripts/asr.py` 的两个数据类（该版本由本仓库的
`scripts/fw_whisper_main.py` 承担转写，但质量评分需要统一的输入结构）。
独立成模块是为了让质量层不依赖任何具体转写实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AsrStrategy:
    """一轮 ASR 的完整参数集；每轮只改一个主要变量。"""

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

    def as_record(self) -> dict:
        return {
            "model": self.model, "device": self.device,
            "compute_type": self.compute_type,
            "audio_variant": self.audio_variant, "language": self.language,
            "vad_filter": self.vad_filter,
            "vad_parameters": self.vad_parameters,
            "condition_on_previous_text": self.condition_on_previous_text,
            "repetition_penalty": self.repetition_penalty,
            "no_repeat_ngram_size": self.no_repeat_ngram_size,
            "beam_size": self.beam_size,
        }


@dataclass
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
