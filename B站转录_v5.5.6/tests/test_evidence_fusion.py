"""证据融合：只有稳定同步字幕能成为自动修正证据，其余只进画面文字区。"""

import unittest

from scripts.evidence_fusion import (SegmentEvidence, align_evidence,
                                     segment_ref)


def segment(index, start, end, text):
    return {"start": start, "end": end, "text": text}


def observation(start, end, text, *, kind="speech_subtitle", confidence=0.93,
                frame_count=2):
    return {"start": start, "end": end, "text": text, "kind": kind,
            "confidence": confidence, "frame_count": frame_count}


class SegmentRefTests(unittest.TestCase):
    def test_reference_is_stable_and_readable(self):
        self.assertEqual(segment_ref(0), "seg-1")
        self.assertEqual(segment_ref(11), "seg-12")


class AlignEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.segments = [
            segment(0, 0.0, 4.0, "端午粽子不分南北"),
            segment(1, 4.0, 8.0, "我还不够塞牙缝"),
        ]

    def test_stable_subtitle_is_strong_evidence(self):
        evidence = align_evidence(self.segments, [
            observation(0.0, 4.0, "端午粽子不分南北")])
        first = evidence[0]
        self.assertIsInstance(first, SegmentEvidence)
        self.assertTrue(first.strong)
        self.assertEqual(first.segment_id, "seg-1")
        self.assertAlmostEqual(first.confidence, 0.93)

    def test_single_frame_subtitle_is_not_strong(self):
        evidence = align_evidence(self.segments, [
            observation(0.0, 4.0, "端午粽子不分南北", frame_count=1)])
        self.assertFalse(evidence[0].strong)
        self.assertIn("not_repeated", evidence[0].doubts)

    def test_low_confidence_subtitle_is_not_strong(self):
        evidence = align_evidence(self.segments, [
            observation(0.0, 4.0, "端午粽子不分南北", confidence=0.55)])
        self.assertFalse(evidence[0].strong)
        self.assertIn("low_confidence_subtitle", evidence[0].doubts)

    def test_unrelated_subtitle_becomes_a_doubt_not_evidence(self):
        evidence = align_evidence(self.segments, [
            observation(0.0, 4.0, "完全无关的画面文字内容")])
        self.assertFalse(evidence[0].strong)
        self.assertIn("subtitle_mismatch", evidence[0].doubts)

    def test_non_overlapping_subtitle_is_ignored(self):
        evidence = align_evidence(self.segments, [
            observation(100.0, 104.0, "端午粽子不分南北")])
        self.assertEqual([item.subtitle_text for item in evidence], ["", ""])
        self.assertFalse(any(item.strong for item in evidence))

    def test_static_info_never_produces_evidence(self):
        evidence = align_evidence(self.segments, [
            observation(0.0, 4.0, "参数 2026-06-19 1.2 GB", kind="static_info")])
        self.assertFalse(evidence[0].strong)
        self.assertEqual(evidence[0].subtitle_text, "")
        self.assertIn("参数 2026-06-19 1.2 GB", evidence[0].visual_texts)

    def test_screenshot_text_never_produces_evidence(self):
        evidence = align_evidence(self.segments, [
            observation(0.0, 4.0, "张三：你好\n李四：在吗", kind="screenshot_text")])
        self.assertFalse(evidence[0].strong)
        self.assertEqual(evidence[0].subtitle_text, "")

    def test_title_and_unknown_text_only_enter_visual_area(self):
        evidence = align_evidence(self.segments, [
            observation(0.0, 4.0, "奶龙大侠", kind="title_emphasis"),
            observation(4.0, 8.0, "某段文字", kind="unknown_visual_text")])
        self.assertFalse(any(item.strong for item in evidence))
        self.assertIn("奶龙大侠", evidence[0].visual_texts)
        self.assertIn("某段文字", evidence[1].visual_texts)

    def test_text_and_timestamps_are_preserved(self):
        evidence = align_evidence(self.segments, [
            observation(0.0, 4.0, "端午粽子不分南北")])
        self.assertEqual(evidence[0].text, "端午粽子不分南北")
        self.assertEqual((evidence[0].start, evidence[0].end), (0.0, 4.0))

    def test_overlapping_partial_subtitle_is_matched(self):
        evidence = align_evidence(self.segments, [
            observation(2.0, 8.0, "端午粽子不分南北我还不够塞牙缝")])
        self.assertTrue(any(item.strong for item in evidence))
        self.assertGreaterEqual(evidence[0].overlap_seconds, 2.0)

    def test_empty_inputs_are_handled(self):
        self.assertEqual(align_evidence([], []), [])
        self.assertEqual(align_evidence(self.segments, []),
                         align_evidence(self.segments, []))

    def test_multiple_subtitles_join_for_one_segment(self):
        evidence = align_evidence(self.segments, [
            observation(0.0, 2.0, "端午粽子"),
            observation(2.0, 4.0, "不分南北")])
        self.assertEqual(evidence[0].subtitle_text, "端午粽子 不分南北")


class EvidenceRecordTests(unittest.TestCase):
    def test_record_is_json_ready(self):
        evidence = align_evidence([segment(0, 0.0, 4.0, "端午粽子")], [
            observation(0.0, 4.0, "端午粽子")])[0]
        record = evidence.as_record()
        self.assertEqual(record["segment_id"], "seg-1")
        self.assertTrue(record["strong"])
        self.assertIn("subtitle_text", record)
        self.assertIn("evidence_json", {"evidence_json": ""})


if __name__ == "__main__":
    unittest.main()
