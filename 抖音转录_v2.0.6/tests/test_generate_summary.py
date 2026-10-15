"""generate_summary.py 摘要客户端测试（全部 mock 网络）。"""

import json
import os
import unittest
from unittest import mock

from scripts import generate_summary


def _api_response(content: str) -> mock.Mock:
    response = mock.Mock(status_code=200)
    response.json.return_value = {
        "choices": [{"message": {"content": content}}]}
    response.raise_for_status = mock.Mock()
    return response


class ConfigTests(unittest.TestCase):
    def test_load_summary_config_reads_env(self):
        env = {"OPENAI_API_KEY": "sk-test",
               "SUMMARY_API_URL": "https://example.com/v1/chat/completions",
               "SUMMARY_API_MODEL": "some-model"}
        with mock.patch.dict(os.environ, env, clear=False):
            config = generate_summary.load_summary_config()
        self.assertEqual(config["api_key"], "sk-test")
        self.assertEqual(config["model"], "some-model")
        self.assertEqual(config["url"], "https://example.com/v1/chat/completions")

    def test_missing_key_yields_pending(self):
        env = {"OPENAI_API_KEY": "", "SUMMARY_API_URL": "", "SUMMARY_API_MODEL": ""}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(generate_summary, "load_dotenv"):
            status, summary = generate_summary.generate_or_pending("标题", "原稿")
        self.assertEqual((status, summary), ("pending", ""))


class BuildMessagesTests(unittest.TestCase):
    def test_messages_require_three_part_chinese_summary(self):
        messages = generate_summary.build_messages("标题", "口播原稿内容")
        system = messages[0]["content"]
        self.assertIn("中文", system)
        self.assertIn("核心观点", system)
        self.assertIn("主要论点", system)
        self.assertIn("关键结论", system)
        self.assertIn("标题", messages[1]["content"])
        self.assertIn("口播原稿内容", messages[1]["content"])

    def test_transcript_truncated_to_30000_chars(self):
        long_text = "字" * 40000
        messages = generate_summary.build_messages("t", long_text)
        self.assertLessEqual(len(messages[1]["content"]), 30000 + 100)
        self.assertNotIn("字" * 40000, messages[1]["content"])


class ApiCallTests(unittest.TestCase):
    def setUp(self):
        self.config = {"api_key": "sk-test", "model": "m",
                       "url": "https://api.example.test/chat/completions"}

    def test_request_payload_and_timeout(self):
        messages = [{"role": "system", "content": "s"},
                    {"role": "user", "content": "u"}]
        with mock.patch.object(generate_summary.requests, "post",
                               return_value=_api_response(" 摘要正文 ")) as post:
            text = generate_summary.call_api(messages, self.config)
        self.assertEqual(text, "摘要正文")
        kwargs = post.call_args.kwargs
        self.assertEqual(post.call_args.args[0],
                         "https://api.example.test/chat/completions")
        self.assertEqual(kwargs["json"]["model"], "m")
        self.assertEqual(kwargs["json"]["messages"], messages)
        self.assertIn("Bearer sk-test", kwargs["headers"]["Authorization"])
        self.assertEqual(kwargs["timeout"], 60)

    def test_payload_caps_summary_length(self):
        # 继承 B站版长处：max_tokens 限制摘要长度与成本
        with mock.patch.object(generate_summary.requests, "post",
                               return_value=_api_response("x")) as post:
            generate_summary.call_api([{"role": "user", "content": "u"}],
                                      self.config)
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 1024)

    def test_success_flow(self):
        env = {"OPENAI_API_KEY": "sk-test"}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(generate_summary, "load_dotenv"), \
             mock.patch.object(generate_summary.requests, "post",
                               return_value=_api_response("结构化摘要")):
            status, summary = generate_summary.generate_or_pending("标题", "原稿")
        self.assertEqual(status, "summarized")
        self.assertEqual(summary, "结构化摘要")

    def test_http_error_returns_pending(self):
        with mock.patch.object(generate_summary.requests, "post",
                               side_effect=generate_summary.requests.RequestException):
            status, summary = generate_summary.call_api_safe(
                [{"role": "user", "content": "x"}], self.config)
        self.assertEqual((status, summary), ("pending", ""))

    def test_empty_choices_returns_pending(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"choices": []}
        response.raise_for_status = mock.Mock()
        with mock.patch.object(generate_summary.requests, "post",
                               return_value=response):
            status, summary = generate_summary.call_api_safe(
                [{"role": "user", "content": "x"}], self.config)
        self.assertEqual((status, summary), ("pending", ""))

    def test_blank_content_returns_pending(self):
        with mock.patch.object(generate_summary.requests, "post",
                               return_value=_api_response("   ")):
            status, summary = generate_summary.call_api_safe(
                [{"role": "user", "content": "x"}], self.config)
        self.assertEqual((status, summary), ("pending", ""))


if __name__ == "__main__":
    unittest.main()
