"""三种摘要模式：正常 / 低质量受限 / 画面文字；失败一律 pending。"""

import unittest
from unittest import mock

from scripts.generate_summary import (MODE_NORMAL, MODE_RESTRICTED,
                                      MODE_VISUAL, build_messages,
                                      generate_or_pending, summary_source_text)

TRANSCRIPT = "端午粽子不分南北，粽叶飘香。"
VISUAL_TEXT = "画面文字：参数 2026-06-19 1.2 GB"
CONFIG = {"api_key": "sk-test", "url": "http://127.0.0.1:9/v1", "model": "m"}


class ModeTests(unittest.TestCase):
    def _call(self, **kwargs):
        with mock.patch("scripts.generate_summary.requests.post",
                        side_effect=OSError("boom")):
            return generate_or_pending("标题", TRANSCRIPT, CONFIG, **kwargs)

    def test_normal_prompt_keeps_three_headings(self):
        messages = build_messages("标题", TRANSCRIPT, mode=MODE_NORMAL)
        system = messages[0]["content"]
        for heading in ("核心观点", "主要论点", "关键结论"):
            self.assertIn(heading, system)

    def test_restricted_mode_marks_low_quality_and_forbids_certainty(self):
        messages = build_messages("标题", TRANSCRIPT, mode=MODE_RESTRICTED)
        system = messages[0]["content"]
        self.assertIn("低质量", system)
        self.assertIn("不得确定", system)
        for heading in ("核心观点", "主要论点", "关键结论"):
            self.assertIn(heading, system)
        self.assertIn("受限", messages[-1]["content"])

    def test_restricted_mode_prefixes_the_output(self):
        content = "核心观点：粽子花样多。\n主要论点：不分南北。\n关键结论：分量不足。"
        with mock.patch("scripts.generate_summary.requests.post") as post:
            post.return_value = mock.Mock(
                raise_for_status=mock.Mock(),
                json=mock.Mock(return_value={"choices": [
                    {"message": {"content": content}}]}))
            status, summary = generate_or_pending("标题", TRANSCRIPT, CONFIG,
                                                  mode=MODE_RESTRICTED)
        self.assertEqual(status, "summarized")
        self.assertIn("受限摘要", summary)
        self.assertIn("粽子花样多", summary)

    def test_visual_mode_speaks_of_screen_text_not_speech(self):
        messages = build_messages("标题", VISUAL_TEXT, mode=MODE_VISUAL)
        system = messages[0]["content"]
        self.assertIn("画面", system)
        self.assertNotIn("逐字稿", messages[-1]["content"])
        self.assertIn("画面文字", messages[-1]["content"])

    def test_visual_mode_output_is_labelled(self):
        content = "核心观点：画面显示参数信息。\n主要论点：日期与体积。\n关键结论：无口播。"
        with mock.patch("scripts.generate_summary.requests.post") as post:
            post.return_value = mock.Mock(
                raise_for_status=mock.Mock(),
                json=mock.Mock(return_value={"choices": [
                    {"message": {"content": content}}]}))
            status, summary = generate_or_pending("标题", VISUAL_TEXT, CONFIG,
                                                  mode=MODE_VISUAL)
        self.assertEqual(status, "summarized")
        self.assertIn("画面文字", summary)

    def test_unknown_mode_falls_back_to_normal(self):
        messages = build_messages("标题", TRANSCRIPT, mode="made-up")
        self.assertNotIn("低质量", messages[0]["content"])

    def test_all_modes_return_pending_on_failure(self):
        for mode in (MODE_NORMAL, MODE_RESTRICTED, MODE_VISUAL):
            with self.subTest(mode=mode):
                status, summary = self._call(mode=mode)
                self.assertEqual(status, "pending")
                self.assertEqual(summary, "")

    def test_missing_key_returns_pending_for_every_mode(self):
        for mode in (MODE_NORMAL, MODE_RESTRICTED, MODE_VISUAL):
            with self.subTest(mode=mode):
                status, summary = generate_or_pending(
                    "标题", TRANSCRIPT, {**CONFIG, "api_key": ""}, mode=mode)
                self.assertEqual((status, summary), ("pending", ""))


class SourceTextTests(unittest.TestCase):
    def test_normal_source_prefers_corrected_text(self):
        self.assertEqual(summary_source_text(TRANSCRIPT, "校正版", MODE_NORMAL),
                         "校正版")

    def test_normal_source_falls_back_to_raw(self):
        self.assertEqual(summary_source_text(TRANSCRIPT, "", MODE_NORMAL),
                         TRANSCRIPT)

    def test_visual_mode_uses_visual_text_only(self):
        self.assertEqual(summary_source_text(TRANSCRIPT, "校正版", MODE_VISUAL,
                                             visual_text=VISUAL_TEXT),
                         VISUAL_TEXT)

    def test_visual_mode_without_visual_text_is_empty(self):
        self.assertEqual(summary_source_text("", "", MODE_VISUAL), "")

    def test_transcript_is_truncated(self):
        long_text = "字" * 40_000
        self.assertLessEqual(len(summary_source_text(long_text, "", MODE_NORMAL)),
                             30_000)


class MetadataTests(unittest.TestCase):
    def test_summary_meta_records_mode_and_model(self):
        from scripts.generate_summary import summary_meta

        meta = summary_meta(CONFIG, MODE_RESTRICTED, pipeline_version="v2")
        self.assertEqual(meta["model"], "m")
        self.assertEqual(meta["mode"], MODE_RESTRICTED)
        self.assertTrue(meta["prompt_version"])
        self.assertEqual(meta["pipeline_version"], "v2")


if __name__ == "__main__":
    unittest.main()
