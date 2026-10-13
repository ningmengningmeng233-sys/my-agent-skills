"""摘要截断兜底的测试：识别残缺输出、重试一次、不合格宁可留占位符。"""

from __future__ import annotations

import unittest
from unittest import mock

from scripts import generate_summary as gs


class FakeResponse:
    def __init__(self, content, finish="stop"):
        self._payload = {"choices": [{"message": {"role": "assistant",
                                                  "content": content},
                                      "finish_reason": finish}]}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def payload(content, finish="stop", count=0):
    """构造一个可被 mock 的 post：按序返回给定响应。"""
    responses = list(zip(content, finish)) if isinstance(content, list) else \
        [(content, finish)]

    def fake_post(url, **kwargs):
        index = min(len(fake_post.calls), len(responses) - 1)
        fake_post.calls.append(url)
        text, reason = responses[index]
        return FakeResponse(text, reason)

    fake_post.calls = []
    return fake_post


LONG_TRANSCRIPT = "正文" * 300


class LooksTruncatedTests(unittest.TestCase):
    def test_empty_content_is_truncated(self):
        self.assertTrue(gs._looks_truncated("", "stop", len(LONG_TRANSCRIPT)))

    def test_non_stop_finish_reason_is_truncated(self):
        self.assertTrue(gs._looks_truncated("完整的一段摘要" * 10, "length",
                                            len(LONG_TRANSCRIPT)))

    def test_short_summary_for_long_transcript_is_suspicious(self):
        """实测故障：62 字的半句摘要曾被当正常摘要写库。"""
        self.assertTrue(gs._looks_truncated("### 1) 核心观点\n一段以", "stop",
                                            len(LONG_TRANSCRIPT)))

    def test_short_summary_for_short_transcript_is_fine(self):
        """短视频本就产出短摘要，不能误判。"""
        self.assertFalse(gs._looks_truncated("很短但完整的摘要。", "stop", 120))

    def test_normal_summary_passes(self):
        good = "## 视频摘要\n\n### 核心观点\n" + "内容" * 80
        self.assertFalse(gs._looks_truncated(good, "stop", len(LONG_TRANSCRIPT)))

    def test_missing_finish_reason_falls_back_to_length_check(self):
        self.assertFalse(gs._looks_truncated("正常的摘要" * 30, "",
                                             len(LONG_TRANSCRIPT)))


class CallLlmApiTests(unittest.TestCase):
    def call(self, fake_post, transcript=LONG_TRANSCRIPT):
        with mock.patch.object(gs.requests, "post", fake_post):
            return gs.call_llm_api("标题", transcript, "k", "http://x/v1/c", "m")

    def test_good_response_returns_immediately(self):
        good = "## 摘要\n" + "内容" * 80
        fake = payload(good)
        self.assertEqual(self.call(fake), good)
        self.assertEqual(len(fake.calls), 1)

    def test_truncated_then_good_retries_once(self):
        good = "## 摘要\n" + "内容" * 80
        fake = payload(["半句", good], ["length", "stop"])
        self.assertEqual(self.call(fake), good)
        self.assertEqual(len(fake.calls), 2, "应恰好重试一次")

    def test_persistently_truncated_raises_and_stores_nothing(self):
        fake = payload(["半句", "还是半句"], ["length", "length"])
        with self.assertRaises(ValueError) as ctx:
            self.call(fake)
        self.assertIn("截断", str(ctx.exception))
        self.assertEqual(len(fake.calls), 2)


if __name__ == "__main__":
    unittest.main()
