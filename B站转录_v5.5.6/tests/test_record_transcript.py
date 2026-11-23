"""record_transcript.py 的行为测试：追加版本、保留旧版本、分段与质量分落库。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from scripts import record_transcript as rt
from tests.support import SkillTestCase


def segments_payload(n: int = 3, duration: float = 30.0) -> dict:
    step = duration / n
    return {
        "model": "small", "device": "cuda", "compute_type": "float16",
        "language": "zh", "language_probability": 0.99, "duration": duration,
        "segments": [
            {"start": i * step, "end": (i + 1) * step, "text": f"第{i + 1}段",
             "avg_logprob": -0.2, "no_speech_prob": 0.01}
            for i in range(n)
        ],
    }


class RecordTranscriptTests(SkillTestCase):
    def setUp(self):
        super().setUp()
        self.meta = self.root / "meta.json"
        self.meta.write_text(json.dumps({
            "bvid": "BV1aaaaaaaaa", "page": 1, "cid": "", "part_title": "",
            "url": "https://www.bilibili.com/video/BV1aaaaaaaaa/",
            "title": "示例标题", "author": "示例作者", "duration": "30秒",
            "upload_date": "2026-01-02",
            "transcript_source": "faster-whisper small 模型（GPU加速）",
        }, ensure_ascii=False), encoding="utf-8")
        self.text = self.root / "text.txt"
        self.text.write_text("这是第一段\n这是第二段\n这是第三段\n", encoding="utf-8")
        self.segments = self.root / "audio.segments.json"
        self.segments.write_text(json.dumps(segments_payload(), ensure_ascii=False),
                                 encoding="utf-8")

    def run_rec(self, *extra, text_file=None):
        args = ["--json", "--db", str(self.db), "--bvid", "BV1aaaaaaaaa",
                "--page", "1", "--meta", str(self.meta),
                "--text-file", str(text_file or self.text), *extra]
        code, out = self.capture(rt.main, args)
        return code, json.loads(out.strip().splitlines()[-1])

    def versions(self):
        conn = sqlite3.connect(str(self.db))
        try:
            return conn.execute(
                "SELECT version_no, kind, is_current, length(transcript_text),"
                " quality_score FROM transcript_versions WHERE bvid='BV1aaaaaaaaa'"
                " ORDER BY version_no").fetchall()
        finally:
            conn.close()

    def test_creates_row_and_first_version(self):
        code, report = self.run_rec()
        self.assertEqual(code, 0)
        self.assertEqual(report["version_no"], 1)
        self.assertEqual(self.versions(), [(1, "raw", 1, len("这是第一段\n这是第二段\n这是第三段"), report["quality_score"])])

    def test_second_run_appends_version_and_flips_current(self):
        self.run_rec()
        self.run_rec()
        rows = self.versions()
        self.assertEqual([r[0] for r in rows], [1, 2])
        self.assertEqual([r[2] for r in rows], [0, 1], "只有最新版本是 current")

    def test_old_versions_are_preserved_not_overwritten(self):
        self.run_rec()
        first = self.versions()[0][3]
        self.text.write_text("换了一段完全不同的正文内容", encoding="utf-8")
        self.run_rec()
        rows = self.versions()
        self.assertEqual(rows[0][3], first, "旧版本正文必须原样保留")
        self.assertNotEqual(rows[1][3], first)

    def test_row_text_matches_current_version(self):
        self.run_rec(text_file=self.text)
        conn = sqlite3.connect(str(self.db))
        try:
            row_text = conn.execute(
                "SELECT transcript_text FROM transcripts WHERE bvid='BV1aaaaaaaaa'"
            ).fetchone()[0]
            version_text = conn.execute(
                "SELECT transcript_text FROM transcript_versions"
                " WHERE bvid='BV1aaaaaaaaa' AND is_current=1").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(row_text, version_text)

    def test_segments_and_metrics_are_stored(self):
        _, report = self.run_rec("--segments", str(self.segments))
        self.assertEqual(report["segments"], 3)
        self.assertIsNotNone(report["quality_score"])
        conn = sqlite3.connect(str(self.db))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM transcript_segments WHERE bvid='BV1aaaaaaaaa'"
            ).fetchone()[0]
            metrics = json.loads(conn.execute(
                "SELECT metrics_json FROM transcript_versions"
                " WHERE bvid='BV1aaaaaaaaa'").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(count, 3)
        self.assertEqual(metrics["language"], "zh")
        self.assertEqual(metrics["segment_count"], 3)
        self.assertIn("speech_ratio", metrics)

    def test_without_segments_no_quality_score(self):
        _, report = self.run_rec()
        self.assertEqual(report["segments"], 0)
        self.assertIsNone(report["quality_score"])
        self.assertEqual(report["quality_status"], "no_segments")

    def test_missing_sidecar_is_tolerated(self):
        code, report = self.run_rec("--segments", str(self.root / "nope.json"))
        self.assertEqual(code, 0)
        self.assertEqual(report["segments"], 0)

    def test_empty_text_is_rejected(self):
        empty = self.root / "empty.txt"
        empty.write_text("   \n", encoding="utf-8")
        code, _ = self.capture(rt.main, ["--json", "--db", str(self.db),
                                         "--bvid", "BV1aaaaaaaaa", "--page", "1",
                                         "--meta", str(self.meta),
                                         "--text-file", str(empty)])
        self.assertEqual(code, 2)
        self.assertEqual(self.versions(), [])

    def test_missing_meta_returns_two(self):
        code, _ = self.capture(rt.main, ["--json", "--db", str(self.db),
                                         "--bvid", "BV1aaaaaaaaa", "--page", "1",
                                         "--meta", str(self.root / "nope.json"),
                                         "--text-file", str(self.text)])
        self.assertEqual(code, 2)

    def test_rerun_replaces_segments_of_same_version_only(self):
        self.run_rec("--segments", str(self.segments))
        self.run_rec("--segments", str(self.segments))
        conn = sqlite3.connect(str(self.db))
        try:
            per_version = conn.execute(
                "SELECT version_no, COUNT(*) FROM transcript_segments"
                " WHERE bvid='BV1aaaaaaaaa' GROUP BY version_no ORDER BY version_no"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(per_version, [(1, 3), (2, 3)])


if __name__ == "__main__":
    unittest.main()
