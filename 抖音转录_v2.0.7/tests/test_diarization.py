"""匿名说话人分离：保守发布规则与降级行为。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import diarization
from scripts.diarization import (DiarizationOutcome, SpeakerTurn,
                                 assign_speakers, diarize, display_label,
                                 stable_labels)


class RawSegment:
    def __init__(self, start, end, speaker):
        self.start = start
        self.end = end
        self.speaker = speaker


class DiarizeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audio = Path(self._tmp.name) / "speech.wav"
        self.audio.write_bytes(b"audio")

    def _diarize(self, segments, *, usable=True, silence_ratio=0.2):
        runner = mock.Mock(return_value=[
            RawSegment(start, end, speaker) for start, end, speaker in segments])
        analysis = SimpleNamespace(silence_ratio=silence_ratio)
        with mock.patch.object(diarization, "models_available",
                               return_value=usable), \
             mock.patch.object(diarization, "default_runner", runner):
            outcome = diarize(self.audio, runner=runner if usable else None,
                              manager=mock.Mock(), audio_analysis=analysis)
        return outcome, runner


class ConservativePublishingTests(DiarizeTestCase):
    def test_single_cluster_produces_no_labels(self):
        outcome, _ = self._diarize([(0.0, 8.0, 0), (8.0, 16.0, 0)])
        self.assertEqual(outcome.status, "no_speakers")
        self.assertEqual(outcome.speaker_count, 0)
        self.assertTrue(all(turn.speaker_label == "" for turn in outcome.turns))

    def test_two_stable_clusters_become_a_and_b(self):
        outcome, _ = self._diarize([
            (0.0, 5.0, 7), (5.0, 10.0, 3), (10.0, 15.0, 7), (15.0, 20.0, 3)])
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.speaker_count, 2)
        labels = [turn.speaker_label for turn in outcome.turns]
        self.assertEqual(labels, ["speaker_a", "speaker_b", "speaker_a",
                                  "speaker_b"])

    def test_labels_follow_first_appearance_not_cluster_id(self):
        # 上游 cluster id 顺序随机，标签必须按首次出现时间稳定映射
        labels = stable_labels([RawSegment(0.0, 2.0, 9),
                                RawSegment(2.0, 4.0, 2),
                                RawSegment(4.0, 6.0, 9)])
        self.assertEqual(labels[9], "speaker_a")
        self.assertEqual(labels[2], "speaker_b")

    def test_short_cluster_is_not_a_new_person(self):
        outcome, _ = self._diarize([
            (0.0, 6.0, 0), (6.0, 16.0, 1), (16.0, 16.8, 2)])
        self.assertEqual(outcome.speaker_count, 2)
        self.assertEqual([turn.speaker_label for turn in outcome.turns],
                         ["speaker_a", "speaker_b", ""])

    def test_single_brief_segment_cluster_is_dropped(self):
        outcome, _ = self._diarize([
            (0.0, 5.0, 0), (5.0, 10.0, 1), (10.0, 11.0, 0),
            (11.0, 11.5, 2)])
        self.assertEqual(outcome.speaker_count, 2)
        self.assertEqual(outcome.turns[-1].speaker_label, "")

    def test_music_heavy_fragmentation_is_treated_as_single_speaker(self):
        """真实验收样本（单人口播 + 强背景音乐，silence_ratio=0.05）实测：
        被测出 5 个聚类（17.7/7.4/5.6/4.2/4.1 秒），实际只有一位说话人。"""
        outcome, _ = self._diarize([
            (0.0, 17.7, 0),
            (18.0, 23.6, 1),
            (24.0, 31.4, 2),
            (32.0, 36.2, 3),
        ], silence_ratio=0.05)
        self.assertEqual(outcome.status, "no_speakers")
        self.assertEqual(outcome.speaker_count, 0)
        self.assertTrue(all(turn.speaker_label == "" for turn in outcome.turns))

    def test_two_balanced_speakers_are_still_labelled(self):
        outcome, _ = self._diarize([
            (0.0, 12.0, 0), (12.0, 24.0, 1), (24.0, 36.0, 0)],
            silence_ratio=0.25)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.speaker_count, 2)

    def test_one_comparable_partner_keeps_labels(self):
        """只有一个可比对手（真正的双人对白）时不能并成单人。"""
        outcome, _ = self._diarize([
            (0.0, 10.0, 0), (10.0, 16.0, 1), (16.0, 22.0, 0)],
            silence_ratio=0.25)
        self.assertEqual(outcome.speaker_count, 2)
        self.assertEqual(outcome.status, "ok")

    def test_long_second_speaker_is_not_treated_as_fragment(self):
        # 次聚类超过碎片阈值 → 确实是两个人，必须保留标签
        outcome, _ = self._diarize([
            (0.0, 20.0, 0), (20.0, 40.0, 1)], silence_ratio=0.25)
        self.assertEqual(outcome.speaker_count, 2)

    def test_unknown_labels_are_filtered_from_turns_output(self):
        outcome, _ = self._diarize([(0.0, 5.0, 0), (5.0, 10.0, 1),
                                    (10.0, 10.5, 2)])
        for turn in outcome.turns:
            self.assertIn(turn.speaker_label, ("speaker_a", "speaker_b", ""))


class DegradationTests(DiarizeTestCase):
    def test_missing_models_skip_without_calling_runner(self):
        runner = mock.Mock()
        with mock.patch.object(diarization, "models_available",
                               return_value=False):
            outcome = diarize(self.audio, runner=runner)
        self.assertEqual(outcome.status, "skipped_or_failed")
        self.assertEqual(outcome.turns, [])
        runner.assert_not_called()

    def test_inference_error_degrades(self):
        def boom(path):
            raise RuntimeError("onnx failed")

        with mock.patch.object(diarization, "models_available",
                               return_value=True):
            outcome = diarize(self.audio, runner=boom)
        self.assertEqual(outcome.status, "skipped_or_failed")
        self.assertIn("diarization_failed", outcome.degradations)

    def test_empty_output_is_skipped(self):
        outcome, _ = self._diarize([])
        self.assertEqual(outcome.status, "skipped_or_failed")
        self.assertEqual(outcome.turns, [])

    def test_degradation_never_raises(self):
        for broken in (ValueError("x"), OSError("y"), RuntimeError("z")):
            with self.subTest(broken=type(broken).__name__):
                def boom(path, exc=broken):
                    raise exc

                with mock.patch.object(diarization, "models_available",
                                       return_value=True):
                    outcome = diarize(self.audio, runner=boom)
                self.assertEqual(outcome.status, "skipped_or_failed")


class AssignSpeakersTests(unittest.TestCase):
    def setUp(self):
        self.segments = [
            {"start": 0.0, "end": 4.0, "text": "第一句", "avg_logprob": -0.2},
            {"start": 4.0, "end": 8.0, "text": "第二句", "avg_logprob": -0.3},
            {"start": 8.0, "end": 12.0, "text": "第三句", "avg_logprob": -0.4},
        ]

    def test_segments_get_speaker_by_max_overlap(self):
        turns = [SpeakerTurn(0.0, 4.2, "speaker_a"),
                 SpeakerTurn(4.0, 12.0, "speaker_b")]
        assigned = assign_speakers(self.segments, turns)
        self.assertEqual([item["speaker"] for item in assigned],
                         ["speaker_a", "speaker_b", "speaker_b"])

    def test_insufficient_overlap_leaves_speaker_empty(self):
        # 最大重叠不足片段时长的 60%：不强行赋值
        turns = [SpeakerTurn(0.0, 2.0, "speaker_a")]
        assigned = assign_speakers([self.segments[0]], turns)
        self.assertEqual(assigned[0]["speaker"], "")
        self.assertLess(assigned[0]["speaker_overlap"], 0.6)

    def test_overlapping_speakers_do_not_force_a_name(self):
        turns = [SpeakerTurn(0.0, 2.6, "speaker_a"),
                 SpeakerTurn(2.4, 5.0, "speaker_b")]
        assigned = assign_speakers([self.segments[0]], turns)
        self.assertEqual(assigned[0]["speaker"], "")

    def test_no_turns_leaves_segments_unchanged(self):
        assigned = assign_speakers(self.segments, [])
        self.assertEqual(assigned, [dict(item, speaker="", speaker_overlap=0.0)
                                    for item in self.segments])

    def test_text_and_timestamps_are_never_modified(self):
        turns = [SpeakerTurn(0.0, 4.2, "speaker_a"),
                 SpeakerTurn(4.0, 12.0, "speaker_b")]
        assigned = assign_speakers(self.segments, turns)
        for original, item in zip(self.segments, assigned):
            self.assertEqual(item["text"], original["text"])
            self.assertEqual(item["start"], original["start"])
            self.assertEqual(item["end"], original["end"])
            self.assertEqual(item["avg_logprob"], original["avg_logprob"])

    def test_original_segments_are_not_mutated(self):
        assign_speakers(self.segments, [SpeakerTurn(0.0, 4.2, "speaker_a")])
        self.assertNotIn("speaker", self.segments[0])

    def test_empty_segments_are_handled(self):
        self.assertEqual(assign_speakers([], [SpeakerTurn(0, 1, "speaker_a")]),
                         [])


class DisplayLabelTests(unittest.TestCase):
    def test_internal_labels_render_as_chinese(self):
        self.assertEqual(display_label("speaker_a"), "说话人 A")
        self.assertEqual(display_label("speaker_c"), "说话人 C")

    def test_unknown_label_renders_empty(self):
        self.assertEqual(display_label(""), "")
        self.assertEqual(display_label("cluster_7"), "")


class OutcomeTests(unittest.TestCase):
    def test_outcome_records_degradations(self):
        outcome = DiarizationOutcome(status="skipped_or_failed", turns=[],
                                     speaker_count=0,
                                     degradations=["model_missing"])
        self.assertEqual(outcome.degradations, ["model_missing"])

    def test_turn_records_are_json_ready(self):
        turn = SpeakerTurn(0.0, 1.0, "speaker_a", 0.8)
        self.assertEqual(turn.as_record(),
                         {"start": 0.0, "end": 1.0,
                          "speaker_label": "speaker_a", "confidence": 0.8})


class DefaultRunnerTests(unittest.TestCase):
    def test_default_runner_uses_manifest_paths_and_is_lazy(self):
        source = Path(diarization.__file__).read_text(encoding="utf-8")
        head = source.split("def default_runner")[0]
        self.assertNotIn("import sherpa_onnx", head)
        self.assertIn("sherpa_onnx", source.split("def default_runner")[1])

    def test_models_available_reports_false_when_missing(self):
        with mock.patch("scripts.model_assets.ModelAssetManager") as manager:
            manager.return_value.capability_usable.return_value = False
            self.assertFalse(diarization.models_available())
            manager.return_value.capability_usable.assert_called_with("diarization")

    def test_models_available_reports_true_when_present(self):
        with mock.patch("scripts.model_assets.ModelAssetManager") as manager:
            manager.return_value.capability_usable.return_value = True
            self.assertTrue(diarization.models_available())


if __name__ == "__main__":
    unittest.main()
