"""参数化 ASR attempt：策略参数全量透传、诊断指标入库、OOM 单独定性。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import asr
from scripts.asr import (AsrAttemptResult, AsrStrategy, default_loader,
                         run_attempt)


class FakeGpuLock:
    """真实上下文的替身：__exit__ 必须返回 False，否则会吞掉异常。"""

    def __init__(self, *args, **kwargs):
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def patch_lock() -> mock.Mock:
    return mock.patch.object(asr, "GpuLock", FakeGpuLock)


class FakeInfo:
    def __init__(self, *, duration=60.0, duration_after_vad=55.0, language="zh",
                 language_probability=0.98):
        self.duration = duration
        self.duration_after_vad = duration_after_vad
        self.language = language
        self.language_probability = language_probability
        self.all_language_probs = [("zh", language_probability)]


class FakeSegment:
    def __init__(self, start, end, text, *, avg_logprob=-0.2, no_speech_prob=0.01,
                 words=None):
        self.start = start
        self.end = end
        self.text = text
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob
        self.words = words or [FakeWord(text, 0.95)]


class FakeWord:
    def __init__(self, word, probability):
        self.word = word
        self.probability = probability


class FakeModel:
    def __init__(self, segments, info=None, error=None):
        self.segments = segments
        self.info = info or FakeInfo()
        self.error = error
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return iter(self.segments), self.info


def strategy(**overrides) -> AsrStrategy:
    values = dict(model="medium", device="cuda", compute_type="float16",
                  audio_variant="original", language="zh", vad_filter=True,
                  vad_parameters=None, condition_on_previous_text=True,
                  repetition_penalty=None, no_repeat_ngram_size=None)
    values.update(overrides)
    return AsrStrategy(**values)


class StrategyTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audio = Path(self._tmp.name) / "speech.wav"
        self.audio.write_bytes(b"audio")

    def _run(self, model, *, strat=None, attempt_no=1):
        loader = mock.Mock(return_value=model)
        with patch_lock():
            result = run_attempt(self.audio, strat or strategy(), loader=loader,
                                 attempt_no=attempt_no)
        return result, model


class ParameterPassthroughTests(StrategyTestCase):
    def test_all_strategy_parameters_reach_faster_whisper(self):
        model = FakeModel([FakeSegment(0.0, 2.0, "你好")])
        self._run(model, strat=strategy(
            vad_parameters={"threshold": 0.35, "min_silence_duration_ms": 500},
            condition_on_previous_text=False, repetition_penalty=1.15,
            no_repeat_ngram_size=3))
        kwargs = model.calls[0]
        self.assertEqual(kwargs["language"], "zh")
        self.assertTrue(kwargs["vad_filter"])
        self.assertEqual(kwargs["vad_parameters"],
                         {"threshold": 0.35, "min_silence_duration_ms": 500})
        self.assertFalse(kwargs["condition_on_previous_text"])
        self.assertEqual(kwargs["repetition_penalty"], 1.15)
        self.assertEqual(kwargs["no_repeat_ngram_size"], 3)
        self.assertTrue(kwargs["word_timestamps"])

    def test_defaults_do_not_send_optional_parameters(self):
        model = FakeModel([FakeSegment(0.0, 2.0, "你好")])
        self._run(model, strat=strategy(language=None, vad_parameters=None,
                                        repetition_penalty=None,
                                        no_repeat_ngram_size=None))
        kwargs = model.calls[0]
        self.assertNotIn("language", kwargs)
        self.assertNotIn("vad_parameters", kwargs)
        self.assertNotIn("repetition_penalty", kwargs)
        self.assertNotIn("no_repeat_ngram_size", kwargs)

    def test_loader_receives_the_strategy_profile(self):
        model = FakeModel([FakeSegment(0.0, 2.0, "你好")])
        loader = mock.Mock(return_value=model)
        with patch_lock():
            run_attempt(self.audio, strategy(model="small", device="cpu",
                                             compute_type="int8"), loader=loader)
        profile = loader.call_args.args[0]
        self.assertEqual((profile.model, profile.device, profile.compute_type),
                         ("small", "cpu", "int8"))


class AttemptMetricsTests(StrategyTestCase):
    def test_metrics_capture_vad_duration_and_language_probability(self):
        model = FakeModel([FakeSegment(0.0, 2.0, "第一句"),
                           FakeSegment(2.0, 4.0, "第二句")],
                          FakeInfo(duration=60.0, duration_after_vad=42.5,
                                   language_probability=0.87))
        result, _ = self._run(model)
        self.assertEqual(result.metrics["duration_after_vad"], 42.5)
        self.assertEqual(result.metrics["duration"], 60.0)
        self.assertAlmostEqual(result.metrics["language_probability"], 0.87)
        self.assertEqual(result.metrics["segment_count"], 2)
        self.assertEqual(result.language, "zh")

    def test_per_segment_diagnostics_are_preserved(self):
        model = FakeModel([FakeSegment(0.0, 2.0, "端午粽子",
                                       avg_logprob=-1.4, no_speech_prob=0.8)])
        result, _ = self._run(model)
        segment = result.segments[0]
        self.assertAlmostEqual(segment["avg_logprob"], -1.4)
        self.assertAlmostEqual(segment["no_speech_prob"], 0.8)
        self.assertIn("low_avg_logprob", segment["review_reasons"])
        self.assertIn("high_silence_prob", segment["review_reasons"])

    def test_transcript_text_is_joined_from_segments(self):
        model = FakeModel([FakeSegment(0.0, 2.0, "第一句"),
                           FakeSegment(2.0, 4.0, "第二句")])
        result, _ = self._run(model)
        self.assertEqual(result.transcript_text, "第一句\n第二句")

    def test_empty_segments_are_a_hard_failure(self):
        model = FakeModel([])
        result, _ = self._run(model)
        self.assertIn("empty_output", result.hard_failures)
        self.assertFalse(result.passed)

    def test_attempt_records_strategy_and_variant(self):
        model = FakeModel([FakeSegment(0.0, 2.0, "你好")])
        result, _ = self._run(model, strat=strategy(audio_variant="vocals"),
                              attempt_no=2)
        self.assertEqual(result.attempt_no, 2)
        self.assertEqual(result.audio_variant, "vocals")
        self.assertEqual(result.strategy.model, "medium")
        self.assertEqual(result.started_at[:2], "20")

    def test_asr_error_is_reported_not_raised(self):
        model = FakeModel([], error=RuntimeError("boom"))
        result, _ = self._run(model)
        self.assertEqual(result.error_code, "asr_failed")
        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)

    def test_out_of_memory_is_flagged_separately(self):
        model = FakeModel([], error=RuntimeError(
            "CUDA failed with error out of memory"))
        result, _ = self._run(model)
        self.assertEqual(result.error_code, "asr_oom")
        self.assertIn("out_of_memory", result.hard_failures)

    def test_missing_audio_is_reported(self):
        loader = mock.Mock()
        result = run_attempt(Path(self._tmp.name) / "nope.wav", strategy(),
                             loader=loader)
        self.assertEqual(result.error_code, "audio_invalid")
        loader.assert_not_called()

    def test_gpu_strategy_takes_the_lock_and_cpu_does_not(self):
        model = FakeModel([FakeSegment(0.0, 2.0, "你好")])
        calls = []

        class RecordingLock(FakeGpuLock):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                calls.append("acquired")

        with mock.patch.object(asr, "GpuLock", RecordingLock):
            run_attempt(self.audio, strategy(device="cuda"),
                        loader=mock.Mock(return_value=model))
        self.assertEqual(calls, ["acquired"])
        calls.clear()
        with mock.patch.object(asr, "GpuLock", RecordingLock):
            run_attempt(self.audio, strategy(device="cpu", compute_type="int8"),
                        loader=mock.Mock(return_value=model))
        self.assertEqual(calls, [])

    def test_lazy_segments_are_materialized_inside_the_lock(self):
        class LazyModel(FakeModel):
            def transcribe(self, audio, **kwargs):
                def generator():
                    yield FakeSegment(0.0, 2.0, "延迟生成")
                return generator(), self.info

        with patch_lock():
            result = run_attempt(self.audio, strategy(),
                                 loader=mock.Mock(return_value=LazyModel([])))
        self.assertEqual(result.transcript_text, "延迟生成")


class ResultShapeTests(unittest.TestCase):
    def test_result_is_frozen(self):
        result = AsrAttemptResult(attempt_no=1, audio_variant="original",
                                  strategy=strategy())
        with self.assertRaises(Exception):
            result.attempt_no = 2

    def test_default_loader_is_lazy(self):
        source = Path(asr.__file__).read_text(encoding="utf-8")
        head = source.split("def default_loader")[0]
        self.assertNotIn("from faster_whisper import", head)
        self.assertIn("from faster_whisper import WhisperModel", source)

    def test_segments_json_is_serializable(self):
        import json

        result = AsrAttemptResult(
            attempt_no=1, audio_variant="original", strategy=strategy(),
            segments=[{"start": 0.0, "end": 1.0, "text": "中文"}])
        self.assertIn("中文", json.dumps(result.segments, ensure_ascii=False))


class LegacyTranscribeStillWorksTests(unittest.TestCase):
    """v1 入口必须继续可用（Task 11 才切换编排器）。"""

    def test_transcribe_signature_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.wav"
            audio.write_bytes(b"audio")
            model = FakeModel([FakeSegment(0.0, 2.0, "你好")])
            with mock.patch.object(asr, "detect_cuda", return_value=False), \
                 mock.patch.object(asr, "select_profile",
                                   return_value=asr.AsrProfile("base", "cpu", "int8")):
                text, segments, profile, language = asr.transcribe(
                    audio, 2.0, "中文标题", loader=mock.Mock(return_value=model))
        self.assertEqual(text, "你好")
        self.assertEqual(profile.model, "base")
        self.assertEqual(language, "zh")
        self.assertTrue(segments)


if __name__ == "__main__":
    unittest.main()
