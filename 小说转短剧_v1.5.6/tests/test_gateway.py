import pytest
from unittest.mock import patch, MagicMock

from dramacraft.gateway import LLMGateway, LLMNode


def test_gateway_load_nodes_from_dict():
    config = {
        "nodes": [
            {
                "name": "Node-1",
                "proto": "openai",
                "base_url": "https://api.test1.com/v1",
                "model": "model-1",
                "api_key": "key-1"
            },
            {
                "name": "Node-2",
                "proto": "anthropic",
                "base_url": "https://api.test2.com",
                "model": "model-2",
                "api_key": "key-2"
            }
        ]
    }
    gw = LLMGateway(config=config)
    assert len(gw.nodes) == 2
    assert gw.nodes[0].name == "Node-1"
    assert gw.nodes[1].proto == "anthropic"


@patch("requests.post")
def test_gateway_failover_on_http_error(mock_post):
    # 模拟第一个节点返回 429 错误，第二个节点正常返回
    resp_fail = MagicMock()
    resp_fail.raise_for_status.side_effect = Exception("HTTP 429 Too Many Requests")

    resp_success = MagicMock()
    resp_success.raise_for_status.return_value = None
    resp_success.json.return_value = {
        "choices": [
            {
                "message": {"content": "这是节点2的有效回复内容"},
                "finish_reason": "stop"
            }
        ]
    }

    mock_post.side_effect = [resp_fail, resp_success]

    config = {
        "nodes": [
            {"name": "FailNode", "proto": "openai", "base_url": "https://fail.com", "model": "m1", "api_key": "k1"},
            {"name": "SuccessNode", "proto": "openai", "base_url": "https://ok.com", "model": "m2", "api_key": "k2"}
        ]
    }
    gw = LLMGateway(config=config)
    reply = gw.call_llm(prompt="你好")
    assert reply == "这是节点2的有效回复内容"
    assert mock_post.call_count == 2


@patch("requests.post")
def test_gateway_retry_on_truncated(mock_post):
    # 模拟第一次返回被截断（finish_reason不是stop），第二次正常返回
    resp_trunc = MagicMock()
    resp_trunc.raise_for_status.return_value = None
    resp_trunc.json.return_value = {
        "choices": [
            {
                "message": {"content": "半句话没说完"},
                "finish_reason": "length"
            }
        ]
    }

    resp_full = MagicMock()
    resp_full.raise_for_status.return_value = None
    resp_full.json.return_value = {
        "choices": [
            {
                "message": {"content": "这是完整无截断的文本"},
                "finish_reason": "stop"
            }
        ]
    }

    mock_post.side_effect = [resp_trunc, resp_full]

    config = {
        "nodes": [
            {"name": "TruncNode", "proto": "openai", "base_url": "https://test.com", "model": "m1", "api_key": "k1"}
        ]
    }
    gw = LLMGateway(config=config)
    reply = gw.call_llm(prompt="生成长剧本")
    assert reply == "这是完整无截断的文本"
    assert mock_post.call_count == 2
