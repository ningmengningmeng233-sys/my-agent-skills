"""校正接口调用层（_call）与配置映射的测试：不发真实请求。"""

from __future__ import annotations

import unittest
from unittest import mock

from scripts import correction
from scripts.correction import CorrectionRejected


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def chat_payload(content, **extra):
    message = {"role": "assistant", "content": content}
    message.update(extra)
    return {"choices": [{"message": message}]}


class CallLayerTests(unittest.TestCase):
    def setUp(self):
        self.config = {"api_key": "sk-test-not-real", "model": "claude-sonnet-4-5",
                       "url": "http://127.0.0.1:8045/v1/chat/completions"}

    def call(self, payload, status=200):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            captured["headers"] = kwargs.get("headers")
            captured["timeout"] = kwargs.get("timeout")
            return FakeResponse(payload, status)

        with mock.patch.object(correction.requests, "post", fake_post):
            text = correction._call([{"role": "user", "content": "x"}], self.config)
        return text, captured

    def test_returns_message_content(self):
        text, _ = self.call(chat_payload('{"segments": []}'))
        self.assertEqual(text, '{"segments": []}')

    def test_request_payload_and_auth_header(self):
        _, captured = self.call(chat_payload("ok"))
        self.assertEqual(captured["url"], self.config["url"])
        self.assertEqual(captured["json"]["model"], "claude-sonnet-4-5")
        self.assertEqual(captured["json"]["temperature"], 0.0)
        self.assertIn("max_tokens", captured["json"])
        self.assertEqual(captured["headers"]["Authorization"],
                         "Bearer sk-test-not-real")
        self.assertGreater(captured["timeout"], 0)

    def test_missing_content_returns_empty_string(self):
        text, _ = self.call({"choices": [{"message": {}}]})
        self.assertEqual(text, "")

    def test_no_choices_returns_empty_string(self):
        text, _ = self.call({})
        self.assertEqual(text, "")

    def test_tool_calls_are_rejected(self):
        with self.assertRaises(CorrectionRejected):
            self.call(chat_payload("", tool_calls=[{"id": "x"}]))

    def test_http_error_propagates(self):
        with self.assertRaises(RuntimeError):
            self.call(chat_payload("x"), status=500)


class ConfigMappingTests(unittest.TestCase):
    def test_env_config_is_mapped_to_correction_keys(self):
        env = {"api_key": "k", "api_url": "http://h/v1/chat/completions",
               "api_model": "m"}
        with mock.patch.object(correction, "get_env_config", lambda: env):
            config = correction.load_summary_config()
        self.assertEqual(config, {"api_key": "k", "url": env["api_url"],
                                  "model": "m"})

    def test_missing_url_and_model_fall_back_to_defaults(self):
        env = {"api_key": "k", "api_url": "  ", "api_model": ""}
        with mock.patch.object(correction, "get_env_config", lambda: env):
            config = correction.load_summary_config()
        self.assertEqual(config["url"], correction.DEFAULT_URL)
        self.assertEqual(config["model"], correction.DEFAULT_MODEL)


if __name__ == "__main__":
    unittest.main()
