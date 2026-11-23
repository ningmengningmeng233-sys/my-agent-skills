"""校正输出的 JSON 截取测试（新 API 会把 JSON 包在围栏或说明文字里）。"""

from __future__ import annotations

import json
import unittest

from scripts.correction import (_extract_json, _first_json_object,
                                CorrectionRejected)

VALID = {"segments": [{"id": "seg-1", "text": "校正后",
                       "evidence": "ocr", "confidence": 0.9}]}


class FirstJsonObjectTests(unittest.TestCase):
    def test_truncates_balanced_object(self):
        text = '前言 {"a": {"b": 1}} 后记'
        self.assertEqual(_first_json_object(text), '{"a": {"b": 1}}')

    def test_ignores_braces_inside_strings(self):
        text = '{"text": "含 } 花括号"}'
        self.assertEqual(_first_json_object(text), text)

    def test_handles_escaped_quotes(self):
        text = '{"text": "引号 \\" 与 { 花括号"}'
        self.assertEqual(_first_json_object(text), text)

    def test_returns_empty_when_unbalanced(self):
        self.assertEqual(_first_json_object('{"a": 1'), "")
        self.assertEqual(_first_json_object("没有对象"), "")


class ExtractJsonTests(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(_extract_json(json.dumps(VALID))["segments"], VALID["segments"])

    def test_fenced_json(self):
        raw = "```json\n" + json.dumps(VALID) + "\n```"
        self.assertEqual(_extract_json(raw)["segments"], VALID["segments"])

    def test_json_after_prose_preamble(self):
        """模型先写一段说明再给 JSON（真实运行里就是这样失败的）。"""
        raw = "我先说明一下修改思路：\n" + json.dumps(VALID) + "\n以上。"
        self.assertEqual(_extract_json(raw)["segments"], VALID["segments"])

    def test_json_inside_fence_with_preamble(self):
        raw = "分析如下：\n```json\n" + json.dumps(VALID) + "\n```\n完成。"
        self.assertEqual(_extract_json(raw)["segments"], VALID["segments"])

    def test_rejection_reports_excerpt(self):
        try:
            _extract_json("这不是 JSON，只是在描述")
        except CorrectionRejected as exc:
            self.assertIn("输出不是 JSON", str(exc))
            self.assertIn("这不是 JSON", str(exc))
        else:
            self.fail("应当拒绝非 JSON 输出")

    def test_missing_segments_is_rejected(self):
        with self.assertRaises(CorrectionRejected):
            _extract_json('{"other": 1}')

    def test_tool_call_payload_is_rejected(self):
        with self.assertRaises(CorrectionRejected):
            _extract_json('{"segments": [], "tool_calls": [{"id": "x"}]}')

    def test_empty_output_is_rejected(self):
        with self.assertRaises(CorrectionRejected):
            _extract_json("")


if __name__ == "__main__":
    unittest.main()
