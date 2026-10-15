#!/usr/bin/env python3
"""Unit tests for engine/llm.py streaming client and error handling."""
import json
import os
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

import llm


class TestCallLlm(unittest.TestCase):
    def setUp(self) -> None:
        self.sleep_patcher = patch("llm.time.sleep")
        self.mock_sleep = self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

        self.key_patcher = patch.object(llm, "API_KEY", "fake-key")
        self.key_patcher.start()
        self.addCleanup(self.key_patcher.stop)

    def _stream_body(self, content: str, usage: dict | None = None, finish_reason: str | None = "stop") -> bytes:
        payload: dict[str, Any] = {
            "id": "test",
            "choices": [{"delta": {"content": content}, "finish_reason": finish_reason}],
        }
        if usage:
            payload["usage"] = usage
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    @patch("llm.requests.post")
    def test_auth_failure_names_the_key_config(self, mock_post: MagicMock) -> None:
        """空 key 时必须给出点名 OPENAI_API_KEY 的可执行报错——**且不许真联网**。

        原来这条叫 `test_missing_api_key_raises`，没有任何打桩，是**真的往
        `api.openai.com` 发请求**、靠服务端回 401 才让断言成立的。于是：
        网络能到 api.openai.com 的机器上它绿，网络路径给出 SSL EOF / 连接被拒的
        机器上它红——同一份代码在仓库里总门 16/16、在桌面工作树里 15/16 就是这么来的。
        单测不该有这种机器依赖性。

        已知遗留（未在此修，属行为变更需单独一单）：`llm.py` 并**没有**"发送前检查
        key 是否为空"的前置守卫，空 key 会照常发出 `Authorization: Bearer `。
        本测试因此打桩成 401 来验"报错文案是否指到配置项"，而不是验"发送前被拦住"。
        """
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "Unauthorized"
        mock_post.return_value = resp
        with patch.object(llm, "API_KEY", ""):
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                llm.call_llm("sys", "user", retries=1)

    def test_no_live_network_in_unit_tests(self) -> None:
        """守卫：`call_llm` 相关的错误路径测试不得触网。

        做法是把 SUMMARY_API_URL 指到一个必然拒绝连接的本地端口，再确认打桩生效——
        若哪天有人删掉 `@patch("llm.requests.post")`，这里会因真实连接异常而红，
        而不是像之前那样"看机器脸色"。
        """
        with patch.object(llm, "API_URL", "http://127.0.0.1:9/v1/chat/completions"), \
                patch("llm.requests.post") as mock_post:
            resp = MagicMock()
            resp.status_code = 200
            resp.iter_content.return_value = [self._stream_body("ok")]
            mock_post.return_value = resp
            content, _usage = llm.call_llm("sys", "user", retries=1)
        self.assertEqual(content, "ok")
        self.assertEqual(mock_post.call_count, 1, "打桩未生效，说明该路径可能真发了请求")

    @patch("llm.requests.post")
    def test_successful_stream(self, mock_post: MagicMock) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [self._stream_body("hello", {"prompt_tokens": 1})]
        mock_post.return_value = resp

        content, usage = llm.call_llm("sys", "user", retries=1)
        self.assertEqual(content, "hello")
        self.assertEqual(usage["prompt_tokens"], 1)
        # 工单 1：模型调用可观测性字段（绝不含密钥/授权头）
        for key in ("input_chars", "output_chars", "effective_landed_chars",
                    "elapsed_ms", "cache_hit", "attempts"):
            self.assertIn(key, usage)
        self.assertEqual(usage["output_chars"], len("hello"))
        self.assertEqual(usage["effective_landed_chars"], len("hello"))
        self.assertFalse(usage["cache_hit"])
        self.assertGreaterEqual(usage["input_chars"], len("sys") + len("user"))
        self.assertNotIn("api_key", usage)
        self.assertNotIn("authorization", {k.lower() for k in usage})
        self.assertIn("stream_options", mock_post.call_args.kwargs["json"])

    @patch("llm.requests.post")
    def test_usage_records_reasoning_tokens_and_effective_ratio(self, mock_post: MagicMock) -> None:
        """工单 9：记录 reasoning_tokens 与有效占比（有效落盘字符 / completion_tokens）。"""
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [self._stream_body("hello", {
            "prompt_tokens": 1, "completion_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 2},
        })]
        mock_post.return_value = resp

        _, usage = llm.call_llm("sys", "user", retries=1)
        self.assertEqual(usage["reasoning_tokens"], 2)
        self.assertEqual(usage["completion_tokens"], 4)
        self.assertAlmostEqual(usage["effective_ratio"], len("hello") / 4)

    @patch("llm.requests.post")
    def test_absent_usage_fields_are_unknown_not_guessed(self, mock_post: MagicMock) -> None:
        """端点不返回用量字段时记 unknown，绝不猜。"""
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [self._stream_body("hello")]   # 无 usage 块
        mock_post.return_value = resp

        _, usage = llm.call_llm("sys", "user", retries=1)
        for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "effective_ratio"):
            self.assertEqual(usage[key], "unknown", key)

    @patch("llm.requests.post")
    def test_429_retries_then_fails(self, mock_post: MagicMock) -> None:
        resp = MagicMock()
        resp.status_code = 429
        mock_post.return_value = resp

        with self.assertRaisesRegex(RuntimeError, "429"):
            llm.call_llm("sys", "user", retries=2)
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(self.mock_sleep.call_count, 2)

    @patch("llm.requests.post")
    def test_empty_content_raises(self, mock_post: MagicMock) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [self._stream_body("")]
        mock_post.return_value = resp

        with self.assertRaisesRegex(ValueError, "为空"):
            llm.call_llm("sys", "user", retries=1)

    @patch("llm.requests.post")
    def test_min_length_enforced(self, mock_post: MagicMock) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [self._stream_body("hi")]
        mock_post.return_value = resp

        with self.assertRaisesRegex(ValueError, "输出过短"):
            llm.call_llm("sys", "user", min_len=10, retries=1)

    @patch("llm.requests.post")
    def test_finish_reason_length_raises(self, mock_post: MagicMock) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [self._stream_body("truncated", finish_reason="length")]
        mock_post.return_value = resp

        with self.assertRaisesRegex(ValueError, "截断"):
            llm.call_llm("sys", "user", retries=1)

    @patch("llm.requests.post")
    def test_finish_reason_content_filter_raises(self, mock_post: MagicMock) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [self._stream_body("ok", finish_reason="content_filter")]
        mock_post.return_value = resp

        with self.assertRaisesRegex(ValueError, "异常结束"):
            llm.call_llm("sys", "user", retries=1)

    @patch("llm.requests.post")
    def test_malformed_sse_lines_ignored(self, mock_post: MagicMock) -> None:
        resp = MagicMock()
        resp.status_code = 200
        body = b"event: ping\n\ninvalid json\n\n" + self._stream_body("valid")
        resp.iter_content.return_value = [body]
        mock_post.return_value = resp

        content, _ = llm.call_llm("sys", "user", retries=1)
        self.assertEqual(content, "valid")

    @patch("llm.requests.post")
    def test_http_400_downgrades_stream_options(self, mock_post: MagicMock) -> None:
        resp_bad = MagicMock()
        resp_bad.status_code = 400
        resp_bad.raise_for_status.side_effect = llm.requests.HTTPError(response=resp_bad)

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.iter_content.return_value = [self._stream_body("ok")]

        mock_post.side_effect = [resp_bad, resp_ok]

        content, _ = llm.call_llm("sys", "user", retries=2)
        self.assertEqual(content, "ok")
        self.assertEqual(mock_post.call_count, 2)
        second_call = mock_post.call_args_list[1].kwargs["json"]
        self.assertNotIn("stream_options", second_call)

    @patch("llm.requests.post")
    def test_401_auth_failure_raises_without_retry(self, mock_post: MagicMock) -> None:
        resp = MagicMock()
        resp.status_code = 401
        mock_post.return_value = resp

        with self.assertRaisesRegex(llm.LLMAuthError, "认证失败"):
            llm.call_llm("sys", "user", retries=3)
        self.assertEqual(mock_post.call_count, 1)

    @patch("llm.requests.post")
    def test_403_auth_failure_raises_without_retry(self, mock_post: MagicMock) -> None:
        resp = MagicMock()
        resp.status_code = 403
        mock_post.return_value = resp

        with self.assertRaisesRegex(llm.LLMAuthError, "认证失败"):
            llm.call_llm("sys", "user", retries=3)
        self.assertEqual(mock_post.call_count, 1)

    @patch("llm.requests.post")
    def test_non_400_http_error_retries_then_fails(self, mock_post: MagicMock) -> None:
        resp = MagicMock()
        resp.status_code = 500
        resp.raise_for_status.side_effect = llm.requests.HTTPError(response=resp)
        mock_post.return_value = resp

        with self.assertRaises(llm.requests.HTTPError):
            llm.call_llm("sys", "user", retries=2)
        self.assertEqual(mock_post.call_count, 2)

    @patch("llm.requests.post")
    def test_network_error_retries_then_fails(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = llm.requests.ConnectionError("boom")

        with self.assertRaises(llm.requests.ConnectionError):
            llm.call_llm("sys", "user", retries=2)
        self.assertEqual(mock_post.call_count, 2)


class TestThinkingPassthrough(unittest.TestCase):
    """推理模型端点上根治空流/截断：可配置的 thinking 透传，**默认关闭**。

    推理模型会先把 max_tokens 预算花在 reasoning_content 上，正文因预算耗尽返回空串
    且 finish_reason=length——实测表现就是链路里的「流式响应为空」与「输出截断」。
    实测 `{"type": "disabled"}` 对 DeepSeek 系生效（推理 token 归零、正文正常）。
    """

    def setUp(self) -> None:
        self.sleep_patcher = patch("llm.time.sleep")
        self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)
        self.key_patcher = patch.object(llm, "API_KEY", "fake-key")
        self.key_patcher.start()
        self.addCleanup(self.key_patcher.stop)

    @staticmethod
    def _body() -> bytes:
        payload: dict[str, Any] = {
            "id": "t",
            "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    def _call(self, mock_post: MagicMock) -> dict:
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [self._body()]
        mock_post.return_value = resp
        # 强制走单端点路径（本机无池配置，但显式钉住更稳）
        with patch.object(llm, "_get_pool", return_value=None):
            llm.call_llm("sys", "user", retries=1)
        return mock_post.call_args.kwargs["json"]

    @patch("llm.requests.post")
    def test_switch_on_injects_thinking_disabled(self, mock_post: MagicMock) -> None:
        with patch.dict(os.environ, {"ASSET_THINKING_DISABLED": "1"}):
            sent = self._call(mock_post)
        self.assertEqual(sent.get("thinking"), {"type": "disabled"})

    @patch("llm.requests.post")
    def test_switch_off_keeps_payload_unchanged(self, mock_post: MagicMock) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ASSET_THINKING_DISABLED", None)
            sent = self._call(mock_post)
        self.assertNotIn("thinking", sent)

    @patch("llm.requests.post")
    def test_falsy_values_mean_off(self, mock_post: MagicMock) -> None:
        for value in ("0", "false", "no", "off", ""):
            with patch.dict(os.environ, {"ASSET_THINKING_DISABLED": value}):
                sent = self._call(mock_post)
            self.assertNotIn("thinking", sent, f"{value!r} 应视为关闭")


if __name__ == "__main__":
    unittest.main()
