"""asr_runner 的行为测试：单轮执行、质量评分转接、多轮决策与退化路径。

这些测试不加载真实模型——WhisperModel 由 `loader` 注入假实现，
因此测试锁定的是「决策与契约」，不是推理质量。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import asr_runner


def fake_segments(text="测试文本", count=3, no_speech=0.0, words=None):
    """构造逐段结果；`words` 用于驱动重复/低置信诊断。"""
    return [
        {"start": float(i), "end": float(i) + 0.8, "text": text,
         "avg_logprob": -0.2, "no_speech_prob": no_speech,
         "words": words if words is not None else []}
        for i in range(count)
    ]


def repeated_words(token="重复", times=8):
    """同一 token 反复出现 → 触发 repetitive 诊断。"""
    return [{"word": token, "probability": 0.9} for _ in range(times)]


class FakeInfo:
    language = "zh"
    language_probability = 0.99
    duration = 12.0


class FakeWhisperModel:
    """替身模型：按构造时给定的结果返回，并可记录收到的解码参数。"""

    def __init__(self, segments=None, *, fail_times=0, calls=None):
        self._segments = segments if segments is not None else fake_segments()
        self._fail_times = fail_times
        self._calls = calls if calls is not None else []

    def __call__(self, model_name, *, device, compute_type):
        return self

    def transcribe(self, audio, **kwargs):
        self._calls.append(kwargs)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("模拟加载失败")
        return iter([_Seg(s) for s in self._segments]), FakeInfo()


class _Seg:
    def __init__(self, payload):
        self.start = payload["start"]
        self.end = payload["end"]
        self.text = payload["text"]
        self.avg_logprob = payload["avg_logprob"]
        self.no_speech_prob = payload["no_speech_prob"]
        self.words = [_Word(w) for w in payload.get("words") or []]


class _Word:
    def __init__(self, payload):
        self.word = payload["word"]
        self.probability = payload["probability"]


class SingleRoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asrrunner_"))

    def test_run_once_writes_txt_and_sidecar(self):
        audio = self.tmp / "audio.wav"
        audio.write_bytes(b"fake")
        model = FakeWhisperModel()
        result = asr_runner.run_once(
            audio, self.tmp, model=model, model_name="small",
            device="cpu", compute_type="int8", language="zh")
        self.assertTrue(result["txt"].is_file())
        self.assertTrue(result["segments_file"].is_file())
        payload = json.loads(result["segments_file"].read_text(encoding="utf-8"))
        self.assertEqual(len(payload["segments"]), 3)
        self.assertEqual(payload["model"], "small")

    def test_run_once_passes_decode_parameters(self):
        audio = self.tmp / "audio.wav"
        audio.write_bytes(b"fake")
        model = FakeWhisperModel()
        asr_runner.run_once(
            audio, self.tmp, model=model, model_name="small", device="cpu",
            compute_type="int8", language="zh",
            condition_on_previous_text=False, repetition_penalty=1.15,
            no_repeat_ngram_size=3)
        kwargs = model._calls[0]
        self.assertIs(kwargs["condition_on_previous_text"], False)
        self.assertEqual(kwargs["repetition_penalty"], 1.15)
        self.assertEqual(kwargs["no_repeat_ngram_size"], 3)

    def test_run_once_uses_relaxed_vad_when_asked(self):
        audio = self.tmp / "audio.wav"
        audio.write_bytes(b"fake")
        model = FakeWhisperModel()
        asr_runner.run_once(
            audio, self.tmp, model=model, model_name="small", device="cpu",
            compute_type="int8", language="zh", vad_relaxed=True)
        params = model._calls[0].get("vad_parameters")
        self.assertIsNotNone(params)
        self.assertLess(params["threshold"], 0.5)

    def test_empty_audio_yields_failed_status(self):
        audio = self.tmp / "empty.wav"
        audio.write_bytes(b"")
        model = FakeWhisperModel(segments=[])
        result = asr_runner.run_once(audio, self.tmp, model=model,
                                     model_name="small", device="cpu",
                                     compute_type="int8", language="zh")
        self.assertEqual(result["status"], "empty")

    def test_empty_output_is_not_written_to_disk(self):
        """空结果若留下只有换行的 TXT，bash 的 -s 会把它当成成功交付。"""
        audio = self.tmp / "empty.wav"
        audio.write_bytes(b"")
        result = asr_runner.run_once(
            audio, self.tmp, model=FakeWhisperModel(segments=[]),
            model_name="small", device="cpu", compute_type="int8", language="zh")
        self.assertIsNone(result["txt"])
        self.assertEqual(list(self.tmp.glob("*.txt")), [])

    def test_empty_output_stale_files_are_removed(self):
        """上一轮留下的 TXT 必须被清掉，否则会被误读为本次结果。"""
        audio = self.tmp / "empty.wav"
        audio.write_bytes(b"")
        stale = self.tmp / "empty.txt"
        stale.write_text("上一轮的内容\n", encoding="utf-8")
        asr_runner.run_once(
            audio, self.tmp, model=FakeWhisperModel(segments=[]),
            model_name="small", device="cpu", compute_type="int8", language="zh")
        self.assertFalse(stale.exists())


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asrrunner_"))

    def test_run_once_reports_quality_score(self):
        audio = self.tmp / "audio.wav"
        audio.write_bytes(b"fake")
        result = asr_runner.run_once(
            audio, self.tmp, model=FakeWhisperModel(), model_name="small",
            device="cpu", compute_type="int8", language="zh")
        self.assertIn("score", result)
        self.assertIsInstance(result["score"], float)
        self.assertIn("passed", result)

    def test_repetition_is_flagged_and_reported(self):
        audio = self.tmp / "audio.wav"
        audio.write_bytes(b"fake")
        repeated = fake_segments(text="重复的一句话", count=12,
                                 words=repeated_words())
        result = asr_runner.run_once(
            audio, self.tmp, model=FakeWhisperModel(segments=repeated),
            model_name="small", device="cpu", compute_type="int8", language="zh")
        self.assertFalse(result["passed"])
        self.assertTrue(any("repetitive" in (s.get("review_reasons") or [])
                            for s in result["segments"]))


class MultiRoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asrrunner_"))

    def test_clean_first_round_stops_immediately(self):
        audio = self.tmp / "audio.wav"
        audio.write_bytes(b"fake")
        calls = []
        outcome = asr_runner.run_rounds(
            audio, self.tmp, loader=lambda *a, **k: FakeWhisperModel(calls=calls),
            initial={"model": "small", "device": "cpu", "compute_type": "int8",
                     "language": "zh"}, max_rounds=3)
        self.assertEqual(outcome["rounds"], 1)
        self.assertEqual(len(calls), 1)

    def test_bad_first_round_triggers_second_with_changed_parameters(self):
        audio = self.tmp / "audio.wav"
        audio.write_bytes(b"fake")
        repeated = fake_segments(text="重复的一句话", count=12,
                                 words=repeated_words())
        calls = []

        def loader(*a, **k):
            return FakeWhisperModel(segments=repeated, calls=calls)

        outcome = asr_runner.run_rounds(
            audio, self.tmp, loader=loader,
            initial={"model": "small", "device": "cpu", "compute_type": "int8",
                     "language": "zh"},
            max_rounds=3, assets={}, expected_language="zh")
        self.assertGreaterEqual(outcome["rounds"], 2)
        second = calls[1]
        self.assertIs(second.get("condition_on_previous_text"), False)

    def test_rounds_never_exceed_the_budget(self):
        audio = self.tmp / "audio.wav"
        audio.write_bytes(b"fake")
        repeated = fake_segments(text="重复的一句话", count=12,
                                 words=repeated_words())
        calls = []

        def loader(*a, **k):
            return FakeWhisperModel(segments=repeated, calls=calls)

        outcome = asr_runner.run_rounds(
            audio, self.tmp, loader=loader,
            initial={"model": "small", "device": "cpu", "compute_type": "int8",
                     "language": "zh"},
            max_rounds=2, assets={}, expected_language="zh")
        self.assertLessEqual(outcome["rounds"], 2)
        self.assertLessEqual(len(calls), 2)

    def test_best_round_is_selected_not_merely_the_last(self):
        """第二轮的提升不应因第三轮变差而被丢弃。"""
        audio = self.tmp / "audio.wav"
        audio.write_bytes(b"fake")
        good = fake_segments(text="正常的一句话", count=6)
        bad = fake_segments(text="重复的一句话", count=14, words=repeated_words())
        outcomes = [bad, good, bad]
        state = {"i": 0}

        def loader(*a, **k):
            segs = outcomes[min(state["i"], len(outcomes) - 1)]
            state["i"] += 1
            return FakeWhisperModel(segments=segs)

        result = asr_runner.run_rounds(
            audio, self.tmp, loader=loader,
            initial={"model": "small", "device": "cpu", "compute_type": "int8",
                     "language": "zh"},
            max_rounds=3, assets={}, expected_language="zh")
        self.assertEqual(result["selected"]["round"], 2)


class DegradationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asrrunner_"))

    def test_loader_failure_returns_failed_without_raising(self):
        audio = self.tmp / "audio.wav"
        audio.write_bytes(b"fake")

        def loader(*a, **k):
            raise RuntimeError("CUDA 崩了")

        result = asr_runner.run_rounds(
            audio, self.tmp, loader=loader,
            initial={"model": "small", "device": "cuda", "compute_type": "float16",
                     "language": "zh"}, max_rounds=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["rounds"], 0)

    def test_missing_audio_is_reported_not_raised(self):
        result = asr_runner.run_rounds(
            self.tmp / "nope.wav", self.tmp,
            loader=lambda *a, **k: FakeWhisperModel(),
            initial={"model": "small", "device": "cpu", "compute_type": "int8",
                     "language": "zh"}, max_rounds=1)
        self.assertEqual(result["status"], "failed")

    def test_all_rounds_empty_is_a_failure_not_a_selection(self):
        """每轮都空时不能挑一个「最佳空轮」当结果。"""
        audio = self.tmp / "audio.wav"
        audio.write_bytes(b"fake")
        result = asr_runner.run_rounds(
            audio, self.tmp, loader=lambda *a, **k: FakeWhisperModel(segments=[]),
            initial={"model": "small", "device": "cpu", "compute_type": "int8",
                     "language": "zh"}, max_rounds=2)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["selected"])


class ModelCeilingTests(unittest.TestCase):
    """重试的模型上限：CPU 不能升到 medium，否则「自动重试」比原样失败更慢。"""

    def test_cpu_caps_at_base(self):
        self.assertEqual(asr_runner._default_max_model("cpu", None), "base")

    def test_gpu_with_enough_vram_allows_medium(self):
        self.assertEqual(asr_runner._default_max_model("cuda", 8.0), "medium")

    def test_gpu_with_little_vram_caps_at_small(self):
        self.assertEqual(asr_runner._default_max_model("cuda", 4.0), "small")
        self.assertEqual(asr_runner._default_max_model("cuda", None), "small")

    def test_cpu_rounds_never_escalate_past_the_ceiling(self):
        audio = Path(tempfile.mkdtemp(prefix="asrrunner_")) / "a.wav"
        audio.write_bytes(b"fake")
        repeated = fake_segments(text="重复", count=12, words=repeated_words())
        calls = []

        def loader(model, *, device, compute_type):
            calls.append(model)
            return FakeWhisperModel(segments=repeated)

        asr_runner.run_rounds(
            audio, audio.parent, loader=loader,
            initial={"model": "base", "device": "cpu", "compute_type": "int8",
                     "language": "zh", "max_model": "base"},
            max_rounds=3, assets={}, expected_language="zh")
        self.assertTrue(all(name == "base" for name in calls),
                        f"CPU 轮次升到了 {calls}")


class ExitCodeTests(unittest.TestCase):
    """退出码是 bash 决定「要不要退回单轮重跑」的依据，语义必须稳定。"""

    def test_ok_and_needs_review_are_distinct(self):
        self.assertEqual(asr_runner.exit_code({"status": "ok", "passed": True}),
                         asr_runner.RC_OK)
        self.assertEqual(asr_runner.exit_code({"status": "ok", "passed": False}),
                         asr_runner.RC_NEEDS_REVIEW)

    def test_no_speech_is_distinct_from_unrunnable(self):
        """无语音重跑无意义；执行器崩了才值得退回单轮。"""
        self.assertEqual(
            asr_runner.exit_code({"status": "failed", "error": "empty_output"}),
            asr_runner.RC_NO_SPEECH)
        self.assertEqual(
            asr_runner.exit_code({"status": "failed", "error": "RuntimeError"}),
            asr_runner.RC_UNRUNNABLE)
        self.assertNotEqual(asr_runner.RC_NO_SPEECH, asr_runner.RC_UNRUNNABLE)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asrrunner_"))

    def test_cli_emits_json(self):
        audio = self.tmp / "audio.wav"
        audio.write_bytes(b"fake")
        out_dir = self.tmp / "out"
        buffer = io.StringIO()
        with mock.patch.object(asr_runner, "_default_loader",
                              lambda *a, **k: FakeWhisperModel()):
            with contextlib.redirect_stdout(buffer):
                code = asr_runner.main([
                    "--audio", str(audio), "--output-dir", str(out_dir),
                    "--model", "small", "--device", "cpu", "--language", "zh",
                    "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue().strip().splitlines()[-1])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["rounds"], 1)


if __name__ == "__main__":
    unittest.main()
