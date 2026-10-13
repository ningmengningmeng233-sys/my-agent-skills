"""fill_summaries.py 补摘要、统计与重渲染测试。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.fill_summaries import main, run_fill, stats_json
from scripts.transcript_db import TranscriptDB


class FillSummariesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.output_root = root / "output"
        self.db = TranscriptDB(root / ".db" / "t.db")
        self.addCleanup(self.db.close)
        for i, aweme in enumerate(("7000000001", "7000000002")):
            self.db.upsert_fetching(aweme_id=aweme, original_input="",
                                    canonical_url="", metadata={"title": f"T{i}"})
            self.db.mark_transcribed(
                aweme, transcript_text=f"原稿{i}", segments_json="[]",
                transcript_source="faster-whisper", whisper_model="base",
                language="zh")
            txt = root / f"old{i}.txt"
            txt.write_text("x", encoding="utf-8")
            self.db.set_transcript_file(aweme, str(txt))

    def test_stats_output(self):
        payload = stats_json(self.db)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["pending_summary"], 2)
        self.assertEqual(payload["summarized"], 0)

    def test_dry_run_calls_no_api(self):
        summary_fn = mock.Mock()
        out = run_fill(self.db, summary_fn=summary_fn,
                       renderer=mock.Mock(), output_root=self.output_root,
                       dry_run=True)
        summary_fn.assert_not_called()
        self.assertEqual(out["pending_found"], 2)
        self.assertEqual(out["summarized"], 0)
        # 数据库状态未变
        self.assertEqual(self.db.stats()["summarized"], 0)

    def test_success_updates_db_and_re_renders(self):
        def summary_fn(title, text):
            return ("summarized", "核心观点\n主要论点\n关键结论")

        rendered = []

        def renderer(record, root):
            rendered.append(record["aweme_id"])
            path = Path(root) / f"{record['aweme_id']}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("rendered", encoding="utf-8")
            return str(path)

        out = run_fill(self.db, summary_fn=summary_fn, renderer=renderer,
                       output_root=self.output_root)
        self.assertEqual(out["summarized"], 2)
        self.assertEqual(out["still_pending"], 0)
        self.assertEqual(sorted(rendered), ["7000000001", "7000000002"])
        for aweme in ("7000000001", "7000000002"):
            record = self.db.get_by_aweme_id(aweme)
            self.assertEqual(record["summary_status"], "summarized")
            self.assertTrue(Path(record["transcript_file"]).is_file())

    def test_failure_keeps_pending_and_continues(self):
        seen = []

        def summary_fn(title, text):
            seen.append(text)
            if len(seen) == 1:
                return ("pending", "")
            return ("summarized", "摘要")

        out = run_fill(self.db, summary_fn=summary_fn,
                       renderer=lambda r, root: str(Path(root) / "x.txt"),
                       output_root=self.output_root)
        self.assertEqual(out["still_pending"], 1)
        self.assertEqual(out["summarized"], 1)
        self.assertEqual(len(seen), 2)  # 第一条失败后继续第二条
        statuses = {r["aweme_id"]: r["summary_status"]
                    for r in self.db.pending_summaries()}
        self.assertIn("7000000001", statuses)
        self.assertNotIn("7000000002", statuses)

    def test_summary_fn_exception_treated_as_pending(self):
        def boom(title, text):
            raise RuntimeError("网络炸了")

        out = run_fill(self.db, summary_fn=boom,
                       renderer=mock.Mock(), output_root=self.output_root)
        self.assertEqual(out["still_pending"], 2)
        self.assertEqual(out["summarized"], 0)

    def test_single_aweme_backfill_only_targets_one(self):
        # 继承 B站版长处：generate_summary --bvid 单条补摘要 → 抖音对应 --aweme-id
        out = run_fill(self.db, summary_fn=lambda t, s: ("summarized", "摘要"),
                       renderer=lambda r, root: str(Path(root) /
                                                    f"{r['aweme_id']}.txt"),
                       output_root=self.output_root,
                       only_aweme_id="7000000002")
        self.assertEqual(out["pending_found"], 1)
        self.assertEqual(out["summarized"], 1)
        self.assertEqual(self.db.get_by_aweme_id("7000000001")["summary_status"],
                         "pending")
        self.assertEqual(self.db.get_by_aweme_id("7000000002")["summary_status"],
                         "summarized")

    def test_single_aweme_not_pending_is_noop(self):
        self.db.mark_summarized("7000000001", "已有")
        out = run_fill(self.db, summary_fn=mock.Mock(),
                       renderer=mock.Mock(), output_root=self.output_root,
                       only_aweme_id="7000000001")
        self.assertEqual(out["pending_found"], 0)

    def test_cli_stats_flag(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["--stats", "--db", str(self.db.db_path)])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["pending_summary"], 2)

    def test_cli_dry_run_flag(self):
        stdout = io.StringIO()
        with mock.patch("scripts.fill_summaries.generate_or_pending") as api, \
             redirect_stdout(stdout):
            code = main(["--dry-run", "--db", str(self.db.db_path)])
        api.assert_not_called()
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["pending_found"], 2)


if __name__ == "__main__":
    unittest.main()
