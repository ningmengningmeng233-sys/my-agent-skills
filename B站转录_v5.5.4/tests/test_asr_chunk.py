"""长音频预算的测试：默认快速失败、可选分段、切点吸附、时间轴平移。

锁定的行为：
- **默认策略是快速失败**（用户拍板）：超阈值音频不切段、不退回 CPU 空磨，
  直接给出「本机不可转 + 换机／释放显存」的原因；
- 分段转写只在显式 `BILI_ASR_CHUNKING=1` 时生效，且段数与每段峰值都在预算内；
- 段的时间轴必须平移成绝对时间，否则分段侧车的时间全是错的；
- 音频解码不可用时整段回退，绝不因为「预算检查」而新增失败点。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from scripts import asr_budget, asr_chunk, asr_runner

# asr_runner/asr_budget 通过 sys.path 加载的是顶层 `asr_chunk`（另一模块实例）；
# mock 必须打在真正被执行的那个实例上，否则真实空闲显存会漏进闸门（GPU 繁忙机器偶发误红）。
import importlib  # noqa: E402

_impl_asr_chunk = importlib.import_module("asr_chunk")

CHUNKING_ON = {"BILI_ASR_CHUNKING": "1"}


class _Word:
    def __init__(self, word, start, end, probability=0.9):
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability


class _PlainSeg:
    """没有 `_replace` 的替身（真实 faster-whisper 用 NamedTuple，替身走包装路径）。"""

    def __init__(self, start, end, text, words=None):
        self.start = start
        self.end = end
        self.text = text
        self.words = words or []
        self.avg_logprob = -0.2
        self.no_speech_prob = 0.01


class _TupleSeg(tuple):
    """替身 NamedTuple：支持 _replace，用来覆盖真实对象那条路径。"""

    def __new__(cls, start, end, text, words=()):
        return super().__new__(cls, (start, end, text, tuple(words)))

    start = property(lambda self: self[0])
    end = property(lambda self: self[1])
    text = property(lambda self: self[2])
    words = property(lambda self: list(self[3]))

    def _replace(self, *, start=None, end=None, text=None, words=None):
        return _TupleSeg(start if start is not None else self.start,
                         end if end is not None else self.end,
                         text if text is not None else self.text,
                         words if words is not None else self.words)


class _Info:
    language = "zh"
    language_probability = 0.98
    duration = 0.0


class FakeModel:
    """按段数返回结果的替身，并记录每段收到的音频长度。"""

    def __init__(self, per_chunk=2, seg_factory=None):
        self.calls = []
        self.lengths = []
        self._per_chunk = per_chunk
        self._seg_factory = seg_factory or (
            lambda i: _PlainSeg(float(i), float(i) + 0.5, f"段{i}",
                                [_Word("词", float(i), float(i) + 0.2)]))

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        self.lengths.append(int(len(audio)))
        segments = [self._seg_factory(i) for i in range(self._per_chunk)]
        return segments, _Info()


class EstimateTests(unittest.TestCase):
    def test_mel_scales_with_minutes(self):
        """1 分钟≈0.018 GiB；8 小时≈8.6 GiB——超长音频爆内存的正是这一项。"""
        self.assertAlmostEqual(asr_chunk.mel_gb(60), 0.018, places=6)
        self.assertAlmostEqual(asr_chunk.mel_gb(480 * 60), 8.64, places=3)

    def test_vram_peak_is_mel_plus_model(self):
        """显存口径照实测锚点：声谱图 + 模型常驻（420 分钟 → 7.55 GiB 声谱图）。"""
        self.assertAlmostEqual(
            asr_chunk.peak_gb(420 * 60, "medium", device="cuda"),
            asr_chunk.mel_gb(420 * 60) + asr_chunk.model_resident_gb("medium"),
            places=3)

    def test_cpu_peak_counts_transient_copies(self):
        """CPU 侧还要算分帧副本与 complex64 副本，峰值高于显存口径。"""
        duration = 480 * 60
        self.assertGreater(asr_chunk.peak_gb(duration, "medium", device="cpu"),
                           asr_chunk.peak_gb(duration, "medium", device="cuda"))

    def test_estimate_wall_seconds_is_monotonic_in_duration(self):
        short = asr_chunk.estimate_wall_seconds(60, model="medium", device="cpu")
        long = asr_chunk.estimate_wall_seconds(3600, model="medium", device="cpu")
        self.assertLess(short, long)

    def test_six_hour_audio_declares_cpu_refusal(self):
        """6 小时音频在 CPU 上要磨好几个小时——必须被预算拦住并说明原因。"""
        message = asr_chunk.cpu_budget_error(6 * 3600, model="medium",
                                            device="cpu")
        self.assertIn("预计耗时", message)
        self.assertIn("BILI_ASR_CPU_BUDGET_SECONDS", message)

    def test_short_audio_passes_cpu_budget(self):
        self.assertEqual(
            asr_chunk.cpu_budget_error(300, model="base", device="cpu"), "")

    def test_cuda_is_never_cpu_budget_refused(self):
        self.assertEqual(
            asr_chunk.cpu_budget_error(8 * 3600, model="medium", device="cuda"),
            "")


class VramRefusalTests(unittest.TestCase):
    """默认策略：显存不够就快速失败，并把「怎么才能跑」说清楚。"""

    def test_over_budget_audio_is_refused_fast(self):
        message = asr_chunk.vram_refusal(420 * 60, model="medium",
                                        device="cuda", free_gb=3.5)
        self.assertIn("本机不可转", message)
        self.assertIn("换机", message)
        self.assertIn("BILI_ASR_CHUNKING=1", message)

    def test_audio_within_budget_passes(self):
        self.assertEqual(
            asr_chunk.vram_refusal(20 * 60, model="medium", device="cuda",
                                   free_gb=5.9), "")

    def test_more_free_vram_can_turn_a_refusal_into_a_pass(self):
        """清单里 B 类的结论：关掉占显存的程序后本机可过。"""
        duration = 193 * 60
        self.assertTrue(asr_chunk.vram_refusal(duration, model="medium",
                                              device="cuda", free_gb=3.5))
        self.assertEqual(asr_chunk.vram_refusal(duration, model="medium",
                                               device="cuda", free_gb=5.9), "")

    def test_cpu_device_is_not_vram_refused(self):
        self.assertEqual(
            asr_chunk.vram_refusal(600 * 60, model="medium", device="cpu"), "")

    def test_unknown_free_vram_does_not_block(self):
        """拿不到空闲显存时不拦——不把「探测失败」变成新的失败点。"""
        with mock.patch.object(asr_chunk, "free_vram_gb", lambda: None):
            self.assertEqual(
                asr_chunk.vram_refusal(600 * 60, model="medium",
                                       device="cuda"), "")

    def test_chunking_enabled_skips_the_refusal_entirely(self):
        """开了分段就不该再拒绝——否则开关等于失效。"""
        with mock.patch.dict(os.environ, CHUNKING_ON, clear=False):
            self.assertEqual(
                asr_chunk.vram_refusal(420 * 60, model="medium",
                                       device="cuda", free_gb=3.5), "")
            self.assertTrue(asr_chunk.plan_boundaries(
                420 * 60, model="medium", device="cuda", budget_gb=3.0))


class ChunkingSwitchTests(unittest.TestCase):
    """分段是显式可选项：默认关闭（拍板意见），开了才切段。"""

    def test_chunking_is_off_by_default(self):
        with mock.patch.dict(os.environ, {"BILI_ASR_CHUNKING": ""}, clear=False):
            self.assertFalse(asr_chunk.chunking_enabled())
            self.assertEqual(
                asr_chunk.plan_boundaries(8 * 3600, model="medium",
                                         device="cuda", budget_gb=3.0), [])

    def test_chunking_env_turns_splitting_on(self):
        with mock.patch.dict(os.environ, CHUNKING_ON, clear=False):
            self.assertTrue(asr_chunk.chunking_enabled())
            self.assertTrue(
                asr_chunk.plan_boundaries(8 * 3600, model="medium",
                                         device="cuda", budget_gb=3.0))

    def test_garbage_value_keeps_it_off(self):
        with mock.patch.dict(os.environ, {"BILI_ASR_CHUNKING": "maybe"},
                             clear=False):
            self.assertFalse(asr_chunk.chunking_enabled())


class PlanTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, CHUNKING_ON, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_short_audio_stays_single_call(self):
        boundaries = asr_chunk.plan_boundaries(
            20 * 60, model="medium", device="cuda", budget_gb=8.0)
        self.assertEqual(boundaries, [])

    def test_long_audio_is_split_and_each_chunk_fits_budget(self):
        budget = 4.0
        duration = 8 * 3600
        boundaries = asr_chunk.plan_boundaries(
            duration, model="medium", device="cuda", budget_gb=budget)
        self.assertTrue(boundaries)
        edges = [0.0] + boundaries + [float(duration)]
        for start, end in zip(edges, edges[1:]):
            self.assertLessEqual(asr_chunk.peak_gb(end - start, "medium"),
                                 budget + 1e-6)
        # 切点严格递增且在音频范围内
        self.assertEqual(sorted(boundaries), boundaries)
        self.assertTrue(all(0 < b < duration for b in boundaries))

    def test_chunk_seconds_env_caps_chunk_size(self):
        """预算很大时，单段长度仍受 BILI_ASR_MAX_CHUNK_SECONDS 上限约束。"""
        boundaries = asr_chunk.plan_boundaries(
            4 * 3600, model="medium", device="cuda", budget_gb=4.0,
            chunk_seconds=600.0)
        edges = [0.0] + boundaries + [4 * 3600.0]
        self.assertGreater(len(boundaries), 1)
        self.assertTrue(all(end - start <= 600.0 + 1e-6
                            for start, end in zip(edges, edges[1:])))

    def test_tiny_budget_still_yields_usable_minimum_chunk(self):
        boundaries = asr_chunk.plan_boundaries(
            3600, model="large-v3", device="cuda", budget_gb=1.0)
        edges = [0.0] + boundaries + [3600.0]
        self.assertTrue(all(end - start >= asr_chunk.MIN_CHUNK_SECONDS - 1e-6
                            for start, end in zip(edges, edges[1:])))

    def test_zero_duration_plans_nothing(self):
        self.assertEqual(asr_chunk.plan_boundaries(
            0, model="medium", device="cpu", budget_gb=4.0), [])


class QuietCutTests(unittest.TestCase):
    def test_cut_moves_into_the_silent_notch(self):
        rate = 16000
        audio = np.ones(rate * 60, dtype=np.float32) * 0.5
        notch = rate * 30 + 1234
        audio[notch:notch + rate] = 0.0
        cut = asr_chunk.quiet_cut(audio, rate * 30, rate, search_seconds=8.0)
        self.assertGreaterEqual(cut, notch - rate)
        self.assertLessEqual(cut, notch + rate * 2)

    def test_empty_audio_yields_zero(self):
        self.assertEqual(asr_chunk.quiet_cut(np.zeros(0, dtype=np.float32),
                                            100, 16000), 0)

    def test_slice_ranges_cover_audio_without_gaps(self):
        rate = 16000
        audio = np.random.default_rng(0).standard_normal(rate * 120).astype(
            np.float32) * 0.1
        boundaries = [30.0, 60.0, 90.0]
        ranges = asr_chunk.slice_ranges(audio, rate, boundaries)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], len(audio))
        for (_, end), (start, _) in zip(ranges, ranges[1:]):
            self.assertEqual(end, start)


class ShiftTests(unittest.TestCase):
    def test_plain_object_is_wrapped_with_absolute_times(self):
        shifted = asr_chunk.shift_segment(_PlainSeg(1.0, 2.0, "甲"), 100.0)
        self.assertAlmostEqual(shifted.start, 101.0)
        self.assertAlmostEqual(shifted.end, 102.0)
        self.assertEqual(shifted.text, "甲")

    def test_namedtuple_is_replaced_and_words_follow(self):
        segment = _TupleSeg(1.0, 2.0, "甲", [_Word("词", 1.0, 1.5)])
        shifted = asr_chunk.shift_segment(segment, 10.0)
        self.assertAlmostEqual(shifted.start, 11.0)
        self.assertAlmostEqual(shifted.words[0].start, 11.0)

    def test_zero_offset_keeps_object_identity(self):
        segment = _PlainSeg(1.0, 2.0, "甲")
        self.assertIs(asr_chunk.shift_segment(segment, 0.0), segment)


class TranscribeChunkedTests(unittest.TestCase):
    def setUp(self):
        self.rate = 16000
        self.audio = np.zeros(self.rate * 3600, dtype=np.float32)
        patcher = mock.patch.dict(os.environ, CHUNKING_ON, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_decode(self, array=None):
        payload = self.audio if array is None else array
        return mock.patch.object(
            asr_chunk, "read_audio",
            lambda audio, sample_rate=16000: payload)

    def test_decode_failure_falls_back_to_single_call(self):
        """解码不了（含测试替身）时，参数必须原样透传给模型。"""
        model = FakeModel()
        with mock.patch.object(asr_chunk, "read_audio",
                               side_effect=RuntimeError("no decoder")):
            segments, info = asr_chunk.transcribe_chunked(
                model, "audio.wav", model_name="medium", device="cuda",
                budget_gb=64.0, language="zh")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(segments), 2)

    def test_short_audio_uses_one_call_without_shifting(self):
        """预算内的音频走原路径：info 原样透传，段的时间轴不动。"""
        model = FakeModel()
        with self._patch_decode():
            segments, info = asr_chunk.transcribe_chunked(
                model, "audio.wav", model_name="medium", device="cuda",
                budget_gb=64.0, language="zh")
        self.assertEqual(len(model.calls), 1)
        self.assertAlmostEqual(segments[0].start, 0.0)
        self.assertEqual(info.language, "zh")
        self.assertEqual(model.lengths, [len(self.audio)])

    def test_over_budget_audio_is_split_and_times_are_absolute(self):
        model = FakeModel(per_chunk=2)
        with self._patch_decode():
            segments, info = asr_chunk.transcribe_chunked(
                model, "audio.wav", model_name="medium", device="cuda",
                budget_gb=1.5, language="zh")
        self.assertGreater(len(model.calls), 1)
        self.assertEqual(len(segments), 2 * len(model.calls))
        starts = [float(s.start) for s in segments]
        self.assertEqual(starts, sorted(starts))
        self.assertGreater(starts[-1], 0.0)
        self.assertAlmostEqual(info.duration, 3600.0)
        self.assertEqual(info.language, "zh")
        # 每段音频都短于整段，说明切分真的落到了模型入参上
        self.assertTrue(all(length < len(self.audio)
                            for length in model.lengths))

    def test_kwargs_are_forwarded_to_every_chunk(self):
        model = FakeModel()
        with self._patch_decode():
            asr_chunk.transcribe_chunked(
                model, "audio.wav", model_name="medium", device="cuda",
                budget_gb=1.5, language="zh", beam_size=5,
                word_timestamps=True)
        for kwargs in model.calls:
            self.assertEqual(kwargs["beam_size"], 5)
            self.assertEqual(kwargs["language"], "zh")


class RunnerRefusalTests(unittest.TestCase):
    """入口层的快速失败：显存不够时**在加载模型之前**就退出并说明原因。

    这是默认策略的落点——不 OOM、不退回 CPU 空磨数小时（实测白烧近 3 小时）。
    """

    def _run(self, duration, free_gb):
        buffer = io.StringIO()
        with mock.patch.object(asr_runner, "probe_duration",
                               lambda *a, **k: float(duration)), \
                mock.patch.object(asr_chunk, "free_vram_gb",
                                  lambda: free_gb), \
                mock.patch.object(_impl_asr_chunk, "free_vram_gb",
                                  lambda: free_gb), \
                mock.patch("scripts.fw_whisper_main.cuda_available",
                           lambda: True), \
                contextlib.redirect_stderr(buffer):
            code = asr_runner.main(["--audio", "fake.m4a", "--output-dir", ".",
                                    "--model", "medium", "--json"])
        return code, buffer.getvalue()

    def test_over_budget_audio_is_refused_before_loading_the_model(self):
        code, output = self._run(420 * 60, 3.0)
        self.assertEqual(code, asr_runner.RC_UNRUNNABLE)
        self.assertIn("⛔", output)
        self.assertIn("本机不可转", output)

    def test_short_audio_is_not_refused_at_the_gate(self):
        """短音频不该被这道闸门拦下（后续失败是模型不可用，不是预算）。"""
        code, output = self._run(20 * 60, 3.0)
        self.assertNotIn("本机不可转", output)


class BudgetCliTests(unittest.TestCase):
    """shell 侧闸门（下载前判定）的退出码契约：0 放行、3 拒绝。"""

    def _run(self, seconds, *, free_gb=3.0, extra=()):
        buffer = io.StringIO()
        with mock.patch.object(asr_chunk, "free_vram_gb", lambda: free_gb), \
                mock.patch.object(_impl_asr_chunk, "free_vram_gb",
                                  lambda: free_gb), \
                mock.patch("scripts.fw_whisper_main.cuda_available",
                           lambda: True), \
                contextlib.redirect_stdout(buffer):
            code = asr_budget.main(["--seconds", str(seconds),
                                    "--model", "medium", *extra])
        return code, buffer.getvalue()

    def test_over_budget_audio_is_refused(self):
        code, output = self._run(420 * 60)
        self.assertEqual(code, asr_budget.RC_REFUSED)
        self.assertIn("本机不可转", output)

    def test_within_budget_audio_passes(self):
        code, _ = self._run(600, free_gb=6.0)
        self.assertEqual(code, asr_budget.RC_OK)

    def test_json_mode_carries_the_reason(self):
        code, output = self._run(420 * 60, extra=("--json",))
        payload = json.loads(output)
        self.assertEqual(code, asr_budget.RC_REFUSED)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["reason"])
        self.assertEqual(payload["device"], "cuda")


class ProbeDurationTests(unittest.TestCase):
    def test_unreadable_audio_yields_zero(self):
        self.assertEqual(asr_chunk.probe_duration(Path("不存在.wav")), 0.0)


if __name__ == "__main__":
    unittest.main()
