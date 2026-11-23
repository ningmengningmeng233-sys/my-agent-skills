import importlib
import json
import threading

import pytest


def test_missing_config_returns_empty(tmp_path, monkeypatch):
    """没有 llm_nodes.json 时返回空列表（由调用方回落到单端点）。"""
    monkeypatch.setenv("ASSET_ENV_PATH", str(tmp_path / "none.env"))
    from engine import llm_pool
    importlib.reload(llm_pool)
    assert llm_pool.load_nodes(env={}) == []


def test_parses_nodes_in_order(tmp_path):
    cfg = tmp_path / "llm_nodes.json"
    cfg.write_text(json.dumps({"nodes": [
        {"name": "A", "proto": "openai", "base_url": "http://a/v1", "model": "m-a",
         "api_key": "k1", "max_tokens": 8000, "max_concurrency": 4},
        {"name": "B", "proto": "openai", "base_url": "http://b/v1", "model": "m-b",
         "api_key": "k2", "max_concurrency": 2},
    ]}, ensure_ascii=False), encoding="utf-8")
    from engine import llm_pool
    nodes = llm_pool.load_nodes(env={"ASSET_ENV_PATH": str(cfg.with_suffix(".env"))},
                                search=[tmp_path])
    assert [n.name for n in nodes] == ["A", "B"]          # 数组顺序即优先级
    assert nodes[0].max_concurrency == 4
    assert nodes[1].max_tokens == llm_pool.DEFAULT_MAX_TOKENS   # 缺省填空


def test_fallback_when_no_nodes(tmp_path, monkeypatch):
    """无池配置时，LLM 必须完全回落到现有单端点行为。"""
    monkeypatch.setenv("SUMMARY_API_URL", "http://127.0.0.1:1/v1/chat/completions")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("SUMMARY_API_MODEL", "test-model")
    from engine import llm, llm_pool
    importlib.reload(llm_pool)
    importlib.reload(llm)
    nodes = llm_pool.load_nodes(env={"ASSET_ENV_PATH": str(tmp_path / "none.env")}, search=[tmp_path])
    assert nodes == []
    assert llm_pool.single_node_from_env() is not None


def test_node_concurrency_cap(fake_endpoint):
    """单节点在途请求数不得超过 max_concurrency。"""
    from engine import llm_pool
    importlib.reload(llm_pool)
    fake_endpoint.script([{"sleep": 0.15, "text": "ok"} for _ in range(6)])
    node = llm_pool.Node(name="A", proto="openai", base_url=fake_endpoint.url.rsplit("/v1/", 1)[0] + "/v1",
                         model="m", api_key="k", max_concurrency=2)
    pool = llm_pool.CallPool([node], global_limit=8)
    threads = [threading.Thread(target=lambda: pool.call("s", "u")) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake_endpoint.peak() <= 2


def test_global_limit_takes_min(fake_endpoint):
    """全局上限与节点闸门取小，不放大并发。"""
    from engine import llm_pool
    importlib.reload(llm_pool)
    fake_endpoint.script([{"sleep": 0.15, "text": "ok"} for _ in range(6)])
    node = llm_pool.Node(name="A", proto="openai", base_url=fake_endpoint.url.rsplit("/v1/", 1)[0] + "/v1",
                         model="m", api_key="k", max_concurrency=8)
    pool = llm_pool.CallPool([node], global_limit=2)
    threads = [threading.Thread(target=lambda: pool.call("s", "u")) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake_endpoint.peak() <= 2


def _node(fake_endpoint, **kw):
    from engine import llm_pool
    base = fake_endpoint.url.rsplit("/v1/", 1)[0] + "/v1"
    return llm_pool.Node(name=kw.pop("name", "A"), proto="openai", base_url=base,
                         model="m", api_key="k", **kw)


def test_quota_switches_node_permanently(fake_endpoint):
    """额度耗尽 → 切下一个节点，且该节点不再被选中。"""
    from engine import llm_pool
    fake_endpoint.script([
        {"status": 429, "body": '{"error":{"message":"insufficient_quota"}}'},
        {"text": "第二个节点的回答"},
        {"text": "后续都走第二个节点"},
    ])
    pool = llm_pool.CallPool([_node(fake_endpoint, name="A"), _node(fake_endpoint, name="B")], global_limit=1)
    text, _ = pool.call("s", "u1")
    assert "第二个节点" in text
    text2, _ = pool.call("s", "u2")
    assert "第二个节点" in text2 or "后续" in text2
    assert pool.skipped_nodes() == ["A"]


def test_rate_limit_reenters_after_cooldown(fake_endpoint, monkeypatch):
    from engine import llm_pool
    monkeypatch.setattr(llm_pool, "RATE_LIMIT_COOLDOWN_SECONDS", 0.1)
    fake_endpoint.script([{"status": 429, "body": '{"error":{"message":"rate limit"}}'},
                          {"text": "冷却后重试成功"}])
    pool = llm_pool.CallPool([_node(fake_endpoint)], global_limit=1)
    text, _ = pool.call("s", "u")
    assert "冷却后" in text
    assert pool.skipped_nodes() == []          # 未永久跳过


def test_degrade_drops_stream_options(fake_endpoint):
    """端点报不支持 stream_options → 去掉该参数重试同节点。"""
    from engine import llm_pool
    fake_endpoint.script([{"status": 400, "body": '{"error":{"message":"unrecognized request argument: stream_options"}}'},
                          {"text": "降级成功"}])
    pool = llm_pool.CallPool([_node(fake_endpoint)], global_limit=1)
    text, _ = pool.call("s", "u")
    assert "降级成功" in text
    assert "stream_options" not in fake_endpoint.received()[-1]      # 第二次请求去掉了该参数
    assert "stream_options" in fake_endpoint.received()[0]


def test_all_nodes_exhausted_raises_stable_code(fake_endpoint):
    from engine import llm_pool
    fake_endpoint.script([{"status": 429, "body": '{"error":{"message":"insufficient_quota"}}'},
                          {"status": 429, "body": '{"error":{"message":"insufficient_quota"}}'}])
    pool = llm_pool.CallPool([_node(fake_endpoint, name="A"), _node(fake_endpoint, name="B")], global_limit=1)
    with pytest.raises(llm_pool.AllNodesExhausted) as e:
        pool.call("s", "u")
    assert e.value.code == "ALL_NODES_EXHAUSTED"


def test_thinking_passthrough_matches_single_endpoint(fake_endpoint, monkeypatch):
    """池路径与单端点路径的 thinking 透传口径一致：开关开则带，关则与现状一致。"""
    from engine import llm_pool
    fake_endpoint.script([{"text": "开"}, {"text": "关"}])
    pool = llm_pool.CallPool([_node(fake_endpoint)], global_limit=1)
    monkeypatch.setenv("ASSET_THINKING_DISABLED", "1")
    pool.call("s", "u")
    monkeypatch.delenv("ASSET_THINKING_DISABLED", raising=False)
    pool.call("s", "u")
    got = fake_endpoint.received()
    assert got[0].get("thinking") == {"type": "disabled"}
    assert "thinking" not in got[1]
