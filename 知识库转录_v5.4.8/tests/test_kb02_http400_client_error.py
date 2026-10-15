"""KB-02 受控 RED/GREEN：HTTP 400 是客户端输入错误，不得禁用 API 节点。

原缺陷：
- classify_http_failure 把所有 HTTP 400（含输入超长、Bad Request 等业务错误）
  粗暴归类为 quota，ApiPool 永久禁用该节点；一个超长段就能击穿整个节点池。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from api_pool import (AllNodesExhaustedError, ApiPool, Node,  # noqa: E402
                      classify_http_failure)


class _FakeResponse:
    """最小 HTTP 响应替身：只实现 ApiPool 用到的接口。"""

    def __init__(self, status, text="", sse=""):
        self.status_code = status
        self.text = text
        self._sse = sse
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=8192):
        data = self._sse.encode("utf-8")
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]


def _node(name="node-0"):
    return Node(name=name, proto="openai",
                base_url="http://127.0.0.1:9/v1", model="mock-model",
                api_key="fake-key")


def _success_sse():
    return ('data: {"choices": [{"delta": {"content": "hello"}}]}\n\n'
            'data: {"choices": [{"finish_reason": "stop"}]}\n\n'
            'data: [DONE]\n\n')


def test_400_is_client_input_error_not_quota():
    assert classify_http_failure(
        400, '{"error": "Bad Request: input text too long"}') == \
        "client_input_error"
    assert classify_http_failure(400, "Bad Request") == "client_input_error"


def test_400_with_degrade_keyword_still_degrades():
    assert classify_http_failure(
        400, "unrecognized request argument: stream_options") == "degrade"


def test_quota_signals_still_exhaust():
    assert classify_http_failure(429, "insufficient_quota") == "quota"
    assert classify_http_failure(401, "Incorrect API key provided") == "quota"
    assert classify_http_failure(
        402, '{"error": "insufficient_quota: billing overdue"}') == "quota"
    assert classify_http_failure(403, "quota exceeded for model") == "quota"


def test_payment_auth_failures_still_exhaust_node():
    """402/403 保持耗尽语义：鉴权/付费失败的节点永不可用（KB-02 只动 400）。"""
    assert classify_http_failure(402, "payment required") == "quota"
    assert classify_http_failure(403, "forbidden: region blocked") == "quota"


def test_400_raises_to_caller_and_keeps_node_healthy(monkeypatch):
    pool = ApiPool([_node()])
    monkeypatch.setattr(
        "api_pool.requests.post",
        lambda *a, **k: _FakeResponse(400, "Bad Request: input too long"))
    with pytest.raises(RuntimeError, match="HTTP 400"):
        pool.call("sys", "user")
    node = pool.nodes[0]
    assert node.exhausted is False
    assert node.is_healthy is True
    assert pool.active_count() == 1


def test_node_still_serves_after_400(monkeypatch):
    """同一节点在 400 之后仍可服务后续正常请求（未被跳过）。"""
    pool = ApiPool([_node()])
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(400, "Bad Request: input too long")
        return _FakeResponse(200, "", _success_sse())

    monkeypatch.setattr("api_pool.requests.post", fake_post)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        pool.call("sys", "bad-input")
    content, _, served = pool.call("sys", "good-input")
    assert content == "hello"
    assert served == "node-0"
    assert pool.nodes[0].served == 1


def test_single_bad_request_cannot_kill_whole_pool(monkeypatch):
    """一个坏请求不得击穿整个池：多节点 400 后池仍全员健康。"""
    pool = ApiPool([_node("node-0"), _node("node-1")])
    monkeypatch.setattr(
        "api_pool.requests.post",
        lambda *a, **k: _FakeResponse(400, "Bad Request: input too long"))
    with pytest.raises(RuntimeError, match="HTTP 400"):
        pool.call("sys", "bad-input")
    assert pool.active_count() == 2
    assert all(n.is_healthy for n in pool.nodes)
    # 不是“全部节点耗尽”：坏的是输入，不是节点。
    with pytest.raises(RuntimeError, match="HTTP 400"):
        try:
            pool.call("sys", "bad-input")
        except AllNodesExhaustedError:
            pytest.fail("400 业务错误被误判为节点池耗尽")
