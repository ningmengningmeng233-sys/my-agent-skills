"""ASR 质量评分与三轮决策表：阈值集中、扣分按时长加权、第三轮后停止。"""

import unittest
from dataclasses import replace
from unittest import mock

from scripts.asr_types import AsrAttemptResult, AsrStrategy
from scripts.quality import (AsrAssets, HardwareBudget, QualityReport,
                             QualityThresholds, evaluate_quality, next_strategy)

THRESHOLDS = QualityThresholds()


SPEECH = "端午就看我奶龙大侠赛龙舟敲大鼓"   # 真实密度：约 0.5 字/秒


def segment(start, end, text=SPEECH, *, avg_logprob=-0.2, no_speech_prob=0.01,
            reasons=(), words=4):
    return {"start": start, "end": end, "text": text,
            "avg_logprob": avg_logprob, "no_speech_prob": no_speech_prob,
            "low_confidence_words": [], "needs_review": bool(reasons),
            "review_reasons": list(reasons), "word_count": words}


def attempt(*, segments=None, metrics=None, error_code="", attempt_no=1,
            variant="original", model="medium"):
    return AsrAttemptResult(
        attempt_no=attempt_no, audio_variant=variant,
        strategy=AsrStrategy(model=model, device="cuda", compute_type="float16",
                             audio_variant=variant, language="zh"),
        segments=list(segments if segments is not None else [
            segment(0.0, 30.0), segment(30.0, 60.0)]),
        transcript_text=SPEECH * 2,
        metrics={"duration": 60.0, "duration_after_vad": 58.0,
                 "language_probability": 0.98, "segment_count": 2,
                 "word_count": 8, **(metrics or {})},
        error_code=error_code)


class PerfectTranscriptTests(unittest.TestCase):
    def test_clear_transcript_passes(self):
        report = evaluate_quality(attempt(), None, [])
        self.assertEqual(report.score, 100.0)
        self.assertEqual(report.status, "passed")
        self.assertEqual(report.hard_failures, [])

    def test_report_has_stable_shape(self):
        report = evaluate_quality(attempt(), None, [])
        self.assertIsInstance(report, QualityReport)
        self.assertIn("deductions", report.as_dict())
        self.assertEqual(report.as_dict()["status"], "passed")


class HardFailureTests(unittest.TestCase):
    def test_no_valid_segments_is_a_hard_failure(self):
        report = evaluate_quality(attempt(segments=[]), None, [])
        self.assertIn("empty_output", report.hard_failures)
        self.assertEqual(report.status, "needs_review")

    def test_reversed_timeline_is_a_hard_failure(self):
        report = evaluate_quality(attempt(segments=[
            segment(10.0, 20.0), segment(0.0, 5.0)]), None, [])
        self.assertIn("timeline_broken", report.hard_failures)

    def test_heavy_overlap_is_a_hard_failure(self):
        report = evaluate_quality(attempt(segments=[
            segment(0.0, 10.0), segment(1.0, 6.0), segment(1.5, 6.5)]), None, [])
        self.assertIn("timeline_broken", report.hard_failures)

    def test_continuous_repetition_is_a_hard_failure(self):
        report = evaluate_quality(attempt(segments=[
            segment(0.0, 10.0, reasons=["repetitive"]),
            segment(10.0, 20.0, reasons=["repetitive"]),
            segment(20.0, 30.0, reasons=["repetitive"]),
            segment(30.0, 40.0, reasons=["repetitive"])]), None, [])
        self.assertIn("repetitive_output", report.hard_failures)

    def test_almost_no_text_on_audible_audio_is_a_hard_failure(self):
        report = evaluate_quality(
            attempt(segments=[segment(0.0, 2.0, "嗯")],
                    metrics={"duration": 300.0, "duration_after_vad": 280.0,
                             "segment_count": 1, "word_count": 1}),
            None, [])
        self.assertIn("too_little_text", report.hard_failures)

    def test_all_segments_high_silence_is_a_hard_failure(self):
        report = evaluate_quality(attempt(segments=[
            segment(0.0, 10.0, no_speech_prob=0.9),
            segment(10.0, 20.0, no_speech_prob=0.85)]), None, [])
        self.assertIn("all_silence_prob", report.hard_failures)

    def test_silent_audio_with_little_text_is_not_penalised(self):
        # 音频本身几乎全是静音时，文本少不算异常
        silent = unittest.mock.Mock(silence_ratio=0.95, duration=300.0)
        report = evaluate_quality(
            attempt(segments=[segment(0.0, 2.0, "嗯")],
                    metrics={"duration": 300.0, "duration_after_vad": 8.0,
                             "segment_count": 1, "word_count": 1}),
            silent, [])
        self.assertNotIn("too_little_text", report.hard_failures)


class DeductionTests(unittest.TestCase):
    def test_low_logprob_deduction_is_weighted_by_duration_and_capped(self):
        # 一半时长低置信 → 扣 30 分的一半
        report = evaluate_quality(attempt(segments=[
            segment(0.0, 30.0, avg_logprob=-2.0),
            segment(30.0, 60.0, avg_logprob=-0.1),
            segment(60.0, 90.0, avg_logprob=-0.1),
            segment(90.0, 120.0, avg_logprob=-0.1)]), None, [])
        self.assertAlmostEqual(report.deductions["low_logprob"], 7.5, places=1)

    def test_full_low_logprob_never_exceeds_thirty(self):
        report = evaluate_quality(attempt(segments=[
            segment(0.0, 30.0, avg_logprob=-3.0),
            segment(30.0, 60.0, avg_logprob=-3.0)]), None, [])
        self.assertLessEqual(report.deductions["low_logprob"], 30.0)

    def test_high_silence_deduction_capped_at_fifteen(self):
        report = evaluate_quality(attempt(segments=[
            segment(0.0, 30.0, no_speech_prob=0.9),
            segment(30.0, 60.0, no_speech_prob=0.01)]), None, [])
        self.assertAlmostEqual(report.deductions["high_silence"], 7.5, places=1)
        self.assertLessEqual(report.deductions["high_silence"], 15.0)

    def test_repetition_deduction_capped_at_twenty_five(self):
        report = evaluate_quality(attempt(segments=[
            segment(0.0, 30.0, reasons=["repetitive"]),
            segment(30.0, 60.0)]), None, [])
        self.assertAlmostEqual(report.deductions["repetition"], 12.5, places=1)
        self.assertLessEqual(report.deductions["repetition"], 25.0)

    def test_language_anomaly_deduction_capped_at_ten(self):
        report = evaluate_quality(
            attempt(metrics={"language_probability": 0.2}), None, [],
            expected_language="zh")
        self.assertLessEqual(report.deductions["language"], 10.0)
        self.assertGreater(report.deductions["language"], 0.0)

    def test_stable_subtitle_conflict_deduction_capped_at_fifteen(self):
        ocr = [{"start": 0.0, "end": 30.0, "text": "完全不一样的字幕内容啊",
                "kind": "speech_subtitle", "confidence": 0.95}]
        report = evaluate_quality(attempt(), None, ocr)
        self.assertGreater(report.deductions["subtitle_conflict"], 0.0)
        self.assertLessEqual(report.deductions["subtitle_conflict"], 15.0)

    def test_matching_subtitles_are_not_a_conflict(self):
        ocr = [{"start": 0.0, "end": 60.0, "text": SPEECH + SPEECH,
                "kind": "speech_subtitle", "confidence": 0.95}]
        report = evaluate_quality(attempt(), None, ocr)
        self.assertEqual(report.deductions["subtitle_conflict"], 0.0)

    def test_static_info_never_counts_as_conflict(self):
        ocr = [{"start": 0.0, "end": 60.0, "text": "参数 2026-06-19 1.2 GB",
                "kind": "static_info", "confidence": 0.95}]
        report = evaluate_quality(attempt(), None, ocr)
        self.assertEqual(report.deductions["subtitle_conflict"], 0.0)

    def test_timeline_deduction_capped_at_five(self):
        report = evaluate_quality(attempt(segments=[
            segment(0.0, 10.0), segment(9.5, 20.0), segment(19.6, 30.0)]),
            None, [])
        self.assertGreater(report.deductions["timeline"], 0.0)
        self.assertLessEqual(report.deductions["timeline"], 5.0)

    def test_score_is_clamped_to_zero(self):
        report = evaluate_quality(attempt(segments=[
            segment(0.0, 30.0, avg_logprob=-3.0, no_speech_prob=0.9,
                    reasons=["repetitive"]),
            segment(30.0, 60.0, avg_logprob=-3.0, no_speech_prob=0.9,
                    reasons=["repetitive"])], metrics={"language_probability": 0.1}),
            None, [], expected_language="zh")
        self.assertGreaterEqual(report.score, 0.0)

    def test_passing_needs_both_score_and_no_hard_failure(self):
        borderline = attempt(segments=[
            segment(0.0, 40.0, avg_logprob=-3.0),
            segment(40.0, 60.0)])
        report = evaluate_quality(borderline, None, [])
        self.assertEqual(report.status,
                         "passed" if report.score >= 75 else "needs_review")
        self.assertFalse(report.passed and report.hard_failures)


class ThresholdConfigTests(unittest.TestCase):
    def test_thresholds_are_immutable_and_centralised(self):
        with self.assertRaises(Exception):
            THRESHOLDS.pass_score = 10

    def test_thresholds_cover_every_deduction_bucket(self):
        report = evaluate_quality(attempt(), None, [])
        self.assertEqual(set(report.deductions),
                         {"low_logprob", "high_silence", "repetition", "language",
                          "subtitle_conflict", "timeline"})

    def test_custom_thresholds_change_the_verdict(self):
        strict = QualityThresholds(pass_score=99.0)
        report = evaluate_quality(attempt(segments=[
            segment(0.0, 30.0, avg_logprob=-2.0), segment(30.0, 60.0)]),
            None, [], thresholds=strict)
        self.assertEqual(report.status, "needs_review")


class NextStrategyTests(unittest.TestCase):
    def setUp(self):
        self.assets = AsrAssets(
            variants={"original": "a.wav", "enhanced": "b.wav", "vocals": "c.wav"},
            available_models=("base", "small", "medium", "large-v3"))
        self.hardware = HardwareBudget(device="cuda", compute_type="float16",
                                       max_model="medium", vram_gb=8.0)
        self.initial = AsrStrategy(model="medium", device="cuda",
                                   compute_type="float16",
                                   audio_variant="original", language="zh")

    def _with_report(self, item, report):
        return item, report

    def _failing_segments(self):
        """两段全低置信：得分 70，明确不达标，用于驱动决策分支。"""
        return [segment(0.0, 30.0, avg_logprob=-2.0),
                segment(30.0, 60.0, avg_logprob=-2.0)]

    def test_passing_first_round_stops(self):
        report = evaluate_quality(attempt(), None, [])
        self.assertIsNone(next_strategy([(attempt(), report)], self.assets,
                                        self.hardware))
        self.assertEqual(report.status, "passed")

    def test_strong_bgm_with_bad_asr_switches_to_vocals(self):
        item = attempt(segments=[
            segment(0.0, 30.0, no_speech_prob=0.8, avg_logprob=-1.5),
            segment(30.0, 60.0, no_speech_prob=0.7, avg_logprob=-1.4),
            segment(60.0, 90.0, no_speech_prob=0.7, avg_logprob=-1.4)])
        report = evaluate_quality(item, None, [])
        nxt = next_strategy([(item, report)], self.assets, self.hardware)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.audio_variant, "vocals")
        self.assertEqual(nxt.model, "medium")

    def test_loudness_anomaly_switches_to_enhanced(self):
        item = attempt(segments=self._failing_segments())
        report = evaluate_quality(item, None, [])
        nxt = next_strategy([(item, report)], self.assets, self.hardware,
                            audio_hint="loudness")
        self.assertEqual(nxt.audio_variant, "enhanced")

    def test_repetition_turns_off_previous_text_conditioning(self):
        item = attempt(segments=[
            segment(i * 10.0, i * 10.0 + 10.0, reasons=["repetitive"])
            for i in range(4)])
        report = evaluate_quality(item, None, [])
        nxt = next_strategy([(item, report)], self.assets, self.hardware)
        self.assertFalse(nxt.condition_on_previous_text)
        self.assertIsNotNone(nxt.repetition_penalty)

    def test_almost_no_text_relaxes_vad(self):
        item = attempt(segments=[segment(0.0, 3.0, "短句")],
                       metrics={"duration": 120.0, "duration_after_vad": 100.0,
                                "segment_count": 1, "word_count": 1})
        report = evaluate_quality(item, None, [])
        nxt = next_strategy([(item, report)], self.assets, self.hardware)
        self.assertIsNotNone(nxt.vad_parameters)
        self.assertLess(nxt.vad_parameters.get("min_silence_duration_ms", 2000),
                        2000)

    def test_language_anomaly_forces_chinese(self):
        item = attempt(segments=self._failing_segments(),
                       metrics={"language_probability": 0.2})
        report = evaluate_quality(item, None, [], expected_language="zh")
        nxt = next_strategy([(item, report)], self.assets, self.hardware,
                            expected_language="zh")
        self.assertEqual(nxt.language, "zh")

    def test_third_round_upgrades_model(self):
        first = attempt(attempt_no=1, model="small",
                        segments=self._failing_segments(),
                        metrics={"language_probability": 0.2})
        second = attempt(attempt_no=2, model="small", variant="vocals",
                         segments=self._failing_segments())
        history = [(first, evaluate_quality(first, None, [], expected_language="zh")),
                   (second, evaluate_quality(second, None, []))]
        nxt = next_strategy(history, self.assets, self.hardware,
                            expected_language="zh")
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.model, "medium")
        self.assertEqual(nxt.audio_variant, "vocals")

    def test_no_fourth_round_is_ever_proposed(self):
        history = []
        for index in range(3):
            item = attempt(attempt_no=index + 1, model="small",
                           segments=self._failing_segments())
            history.append((item, evaluate_quality(item, None, [])))
        self.assertIsNone(next_strategy(history, self.assets, self.hardware))

    def test_missing_variant_skips_that_strategy(self):
        assets = AsrAssets(variants={"original": "a.wav"},
                           available_models=("base", "small"))
        item = attempt(segments=[
            segment(0.0, 30.0, no_speech_prob=0.8, avg_logprob=-1.5),
            segment(30.0, 60.0, no_speech_prob=0.7, avg_logprob=-1.4),
            segment(60.0, 90.0, no_speech_prob=0.7, avg_logprob=-1.4)])
        report = evaluate_quality(item, None, [])
        nxt = next_strategy([(item, report)], assets, self.hardware)
        self.assertNotEqual(getattr(nxt, "audio_variant", "original"), "vocals")

    def test_oom_downgrades_hardware_without_counting_a_new_round(self):
        item = attempt(error_code="asr_oom", segments=[])
        history = [(item, evaluate_quality(item, None, []))]
        nxt = next_strategy(history, self.assets, self.hardware,
                            oom_downgraded=False)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.device, "cpu")
        self.assertEqual(nxt.compute_type, "int8")

    def test_oom_after_downgrade_stops(self):
        item = attempt(error_code="asr_oom", segments=[])
        history = [(item, evaluate_quality(item, None, []))]
        self.assertIsNone(next_strategy(history, self.assets, self.hardware,
                                        oom_downgraded=True))

    def test_history_is_not_mutated(self):
        item = attempt(segments=[segment(0.0, 30.0, avg_logprob=-1.6),
                                 segment(30.0, 60.0)])
        history = [(item, evaluate_quality(item, None, []))]
        snapshot = list(history)
        next_strategy(history, self.assets, self.hardware)
        self.assertEqual(history, snapshot)

    def test_model_upgrade_respects_hardware_ceiling(self):
        hardware = replace(self.hardware, max_model="small")
        first = attempt(model="small", segments=self._failing_segments())
        second = attempt(attempt_no=2, model="small",
                         segments=self._failing_segments())
        history = [(first, evaluate_quality(first, None, [])),
                   (second, evaluate_quality(second, None, []))]
        nxt = next_strategy(history, self.assets, hardware)
        self.assertEqual(nxt.model, "small")


if __name__ == "__main__":
    unittest.main()
