"""P3 质量层的行为测试：文本级证据挂接、帧数过滤、校正入口的写库契约。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from scripts import correct_with_evidence as cwe
from scripts.correction import CorrectionOutcome
from scripts.evidence_fusion import (MIN_FRAMES, MIN_SIMILARITY,
                                     _best_window_similarity,
                                     align_evidence_by_text,
                                     visual_context_from_text)
from tests.support import SkillTestCase


def observation(text: str, *, start=0.0, end=1.0, confidence=0.99,
                kind="speech_subtitle", frame_count=3) -> dict:
    return {"start": start, "end": end, "text": text, "confidence": confidence,
            "kind": kind, "frame_count": frame_count}


class TextAlignmentTests(unittest.TestCase):
    def test_subtitle_inside_chunk_is_strong_evidence(self):
        text = "今天我们来聊端五粽子。有人说端五粽子不分南北，咸甜各有拥趸。"
        evidence = align_evidence_by_text(text, [observation("端午粽子")])
        self.assertEqual(len(evidence), 1)
        self.assertTrue(evidence[0].strong)
        self.assertEqual(evidence[0].segment_id, "seg-1")
        self.assertGreaterEqual(evidence[0].similarity, MIN_SIMILARITY)

    def test_similarity_uses_best_window_not_whole_chunk(self):
        chunk = "开头一段无关内容" + "端午粽子" + "后面还有很多别的话"
        score = _best_window_similarity(chunk, "端午粽子")
        self.assertGreaterEqual(score, 0.99)

    def test_unrelated_subtitle_is_not_strong(self):
        text = "今天我们来聊端五粽子。"
        evidence = align_evidence_by_text(text, [observation("完全不相关的字幕内容")])
        self.assertFalse(evidence[0].strong)
        self.assertIn("subtitle_mismatch", evidence[0].doubts)

    def test_no_observation_yields_doubt_and_no_strong(self):
        evidence = align_evidence_by_text("一段没有任何证据的文本。", [])
        self.assertFalse(evidence[0].strong)
        self.assertIn("no_subtitle_evidence", evidence[0].doubts)

    def test_single_frame_subtitle_is_ignored(self):
        """只出现一帧的字幕不算证据（MIN_FRAMES 过滤，曾因丢帧数而全部失效）。"""
        text = "今天我们来聊端五粽子。"
        frames = [observation("端午粽子", frame_count=1)]
        self.assertFalse(align_evidence_by_text(text, frames)[0].strong)
        self.assertTrue(align_evidence_by_text(
            text, [observation("端午粽子", frame_count=MIN_FRAMES)])[0].strong)

    def test_low_confidence_subtitle_is_ignored(self):
        text = "今天我们来聊端五粽子。"
        low = [observation("端午粽子", confidence=0.1)]
        self.assertFalse(align_evidence_by_text(text, low)[0].strong)

    def test_long_text_is_split_into_multiple_segments(self):
        text = "很长的正文。" * 60
        evidence = align_evidence_by_text(text, [observation("很长的正文")])
        self.assertGreater(len(evidence), 1)
        self.assertEqual([item.segment_id for item in evidence][:2], ["seg-1", "seg-2"])

    def test_visual_texts_are_collected_separately(self):
        texts = visual_context_from_text([
            observation("参数 2026-06-19 1.2 GB", kind="static_info"),
            observation("端午粽子"),
            observation("参数 2026-06-19 1.2 GB", kind="static_info"),
        ])
        self.assertEqual(texts, ["参数 2026-06-19 1.2 GB"])


class CorrectionEntryTests(SkillTestCase):
    def setUp(self):
        super().setUp()
        self.add_row("BV1aaaaaaaaa", transcript_text="今天我们来聊端五粽子。")
        self.video = self.root / "fake.mp4"
        self.video.write_bytes(b"not-a-real-video")

    def run_cli(self, *extra):
        args = ["--json", "--db", str(self.db), "--bvid", "BV1aaaaaaaaa",
                "--video", str(self.video), *extra]
        code, out = self.capture(cwe.main, args)
        return code, json.loads(out.strip().splitlines()[-1])

    def patch_ocr(self, observations):
        return mock.patch.object(cwe, "collect_observations",
                                 lambda *a, **k: (observations,
                                                  {"status": "ok",
                                                   "frames_scanned": 5,
                                                   "degradations": []}))

    def test_dry_run_reports_evidence_without_writing(self):
        with self.patch_ocr([observation("端午粽子")]):
            code, report = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["strong_evidence"], 1)
        conn = sqlite3.connect(str(self.db))
        try:
            count = conn.execute("SELECT COUNT(*) FROM transcript_versions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_apply_writes_raw_then_corrected_version(self):
        outcome = CorrectionOutcome(
            status="corrected", corrected_text="今天我们来聊端午粽子。",
            changes=[{"source_segment_ref": "seg-1",
                      "original_text": "今天我们来聊端五粽子。",
                      "corrected_text": "今天我们来聊端午粽子。",
                      "evidence": {"source": "ocr", "subtitle": "端午粽子"},
                      "confidence": 0.9}])
        with self.patch_ocr([observation("端午粽子")]):
            with mock.patch.object(cwe, "correct_or_pending", lambda ctx, cfg: outcome):
                code, report = self.run_cli("--apply")
        self.assertEqual(code, 0)
        self.assertEqual(report["version_no"], 2)
        self.assertEqual(report["changes"], 1)
        conn = sqlite3.connect(str(self.db))
        try:
            versions = conn.execute(
                "SELECT version_no, kind FROM transcript_versions"
                " WHERE bvid='BV1aaaaaaaaa' ORDER BY version_no").fetchall()
            change = conn.execute(
                "SELECT segment_ref, original, corrected, evidence, confidence"
                " FROM correction_changes").fetchone()
            raw_text = conn.execute(
                "SELECT transcript_text FROM transcripts WHERE bvid='BV1aaaaaaaaa'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(versions, [(1, "raw"), (2, "corrected")])
        self.assertEqual(change[0], "seg-1")
        self.assertIn("端五", change[1])
        self.assertIn("端午", change[2])
        self.assertEqual(json.loads(change[3])["subtitle"], "端午粽子")
        self.assertAlmostEqual(change[4], 0.9)
        # 原始稿永不被覆盖
        self.assertEqual(raw_text, "今天我们来聊端五粽子。")

    def test_pending_outcome_writes_nothing(self):
        outcome = CorrectionOutcome(status="pending")
        with self.patch_ocr([observation("端午粽子")]):
            with mock.patch.object(cwe, "correct_or_pending", lambda ctx, cfg: outcome):
                code, report = self.run_cli("--apply")
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "pending")
        conn = sqlite3.connect(str(self.db))
        try:
            count = conn.execute("SELECT COUNT(*) FROM transcript_versions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_missing_video_returns_two(self):
        code, _ = self.capture(cwe.main, ["--json", "--db", str(self.db),
                                          "--bvid", "BV1aaaaaaaaa",
                                          "--video", str(self.root / "nope.mp4")])
        self.assertEqual(code, 2)

    def test_unknown_key_returns_two(self):
        code, _ = self.capture(cwe.main, ["--json", "--db", str(self.db),
                                          "--bvid", "BV1zzzzzzzzz",
                                          "--video", str(self.video)])
        self.assertEqual(code, 2)

    def test_requires_video_argument(self):
        with self.assertRaises(SystemExit):
            self.run_quiet(cwe.main, ["--json", "--db", str(self.db),
                                      "--bvid", "BV1aaaaaaaaa"])


if __name__ == "__main__":
    unittest.main()
