"""真实推理通道（语音识别 / 说话人分离）的端到端验证。

本仓其余 ASR 与分离用例一律注入假 loader / 假 runner（`test_asr_runner`、
`test_diarization`），只有这里的两条会真正加载本地 ONNX 模型。缺引擎或缺
本地模型时按 `SKIP(E)` 挂账；模型与引擎齐装的机器上必须实跑通过。

测的是「引擎 + 本地模型 + 归一化契约」是否真的成立，不测识别内容本身：
输入是合成音调而非真人语音，所以既可能出带文字的片段，也可能走已声明的
空文本降级——两者都算通过，崩栈、联网取模型才算失败。
"""

from __future__ import annotations

import importlib.util
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from scripts import asr as asr_module
from scripts import diarization, paths

SAMPLE_RATE = 16000
AUDIO_SECONDS = 8
#: 缓存目录名来自 faster-whisper 的 HF 快照布局，模型本体不入库
WHISPER_BASE_SNAPSHOT = "models--Systran--faster-whisper-base"


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def whisper_base_cached() -> bool:
    """本地是否已有 base 模型；没有就不该在测试里触发下载。"""
    return (paths.STATE_DIR / "models" / WHISPER_BASE_SNAPSHOT).is_dir()


def synthetic_wav(directory: Path, seconds: int = AUDIO_SECONDS) -> Path:
    """生成 16k 单声道合成音频：每 2 秒换一次基频与泛音结构，只写临时目录。

    2 秒切换是实测值：4 秒切换的同幅音调会让 pyannote 分割输出空片段。
    """
    block = 2.0
    path = directory / "synthetic_two_voices.wav"
    frames = bytearray()
    for index in range(SAMPLE_RATE * seconds):
        at = index / SAMPLE_RATE
        voice = int(at // block) % 2
        base = 110.0 if voice == 0 else 250.0
        sample = 0.30 * math.sin(2 * math.pi * base * at)
        if voice:
            sample += 0.18 * math.sin(2 * math.pi * base * 2.7 * at)
        else:
            sample += 0.05 * math.sin(2 * math.pi * base * 3.1 * at)
        edge = at % block
        envelope = min(1.0, edge * 3.0) * min(1.0, (block - edge) * 3.0)
        clipped = max(-1.0, min(1.0, sample * envelope))
        frames += struct.pack("<h", int(clipped * 18000))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames))
    return path


class RealDiarizationTests(unittest.TestCase):
    """真实 Sherpa-ONNX + 仓内已下载模型跑通分离，并守住匿名标签红线。"""

    def setUp(self):
        if not (_module_available("sherpa_onnx") and _module_available("numpy")):
            self.skipTest("SKIP(E): 缺说话人分离引擎（sherpa-onnx + numpy）")
        if not diarization.models_available():
            self.skipTest("SKIP(E): 本地未下载分离模型（state/ 不入库）")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audio = synthetic_wav(Path(self._tmp.name))

    def test_real_runner_returns_usable_segments(self):
        segments = diarization.default_runner(self.audio)
        self.assertTrue(
            segments,
            "真实 pyannote 分割在 8 秒连续音频上应至少切出一段，"
            "为空说明本地模型或 onnxruntime 已失效")
        for segment in segments:
            start, end = float(segment.start), float(segment.end)
            self.assertGreaterEqual(start, 0.0)
            self.assertLessEqual(end, AUDIO_SECONDS + 1.0)
            self.assertGreaterEqual(end, start)
        self.assertTrue(diarization.stable_labels(segments))

    def test_real_pipeline_keeps_anonymity_and_closed_status(self):
        outcome = diarization.diarize(self.audio)
        self.assertIn(outcome.status,
                      {"ok", "no_speakers", "skipped_or_failed"})
        if outcome.status == "skipped_or_failed":
            self.fail(f"模型齐备却降级：{outcome.degradations}")
        starts = [turn.start for turn in outcome.turns]
        self.assertEqual(starts, sorted(starts))
        for turn in outcome.turns:
            # 红线：只允许匿名标签或空，绝不出现自称真实身份的内容
            self.assertIn(turn.speaker_label, ("", "speaker_a", "speaker_b",
                                               "speaker_c"))
            if turn.speaker_label:
                self.assertTrue(diarization.display_label(turn.speaker_label)
                                .startswith("说话人 "))


class RealAsrTests(unittest.TestCase):
    """真实 faster-whisper 从本地缓存加载（绝不联网）并走完整归一化链路。"""

    def setUp(self):
        if not _module_available("faster_whisper"):
            self.skipTest("SKIP(E): 缺 faster-whisper 引擎")
        if not whisper_base_cached():
            self.skipTest("SKIP(E): 本地无 base 模型缓存，测试不许触发下载")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audio = synthetic_wav(Path(self._tmp.name))

    def _pinned_loader(self, profile):
        """忽略自动选档，钉死成磁盘上确实存在的那个模型，杜绝联网取权重。"""
        return asr_module.default_loader(
            asr_module.AsrProfile("base", "cpu", "int8"))

    def test_real_model_transcribes_through_the_record_contract(self):
        try:
            text, segments, profile, language = asr_module.transcribe(
                self.audio, duration_seconds=float(AUDIO_SECONDS),
                title="合成音频契约验证", loader=self._pinned_loader)
        except asr_module.AsrError as exc:
            self.assertIn("识别结果为空文本", str(exc),
                          f"非空文本之外的异常都算失败：{exc}")
            return
        self.assertEqual(profile.model, "base")
        self.assertTrue(text.strip())
        self.assertIn(language, ("zh", ""))
        for item in segments:
            self.assertEqual(
                set(item),
                {"start", "end", "text", "avg_logprob", "no_speech_prob",
                 "low_confidence_words", "needs_review", "review_reasons"})
            self.assertLessEqual(float(item["end"]), AUDIO_SECONDS + 1.0)
            self.assertIsInstance(item["needs_review"], bool)
            self.assertIsInstance(item["review_reasons"], list)


if __name__ == "__main__":
    unittest.main()
