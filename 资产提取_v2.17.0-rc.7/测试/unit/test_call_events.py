import json


def test_events_written_in_order(tmp_path, fake_endpoint, monkeypatch):
    monkeypatch.setenv("ASSET_LIB", str(tmp_path))
    from engine import llm_pool
    fake_endpoint.script([{"status": 429, "body": '{"error":{"message":"insufficient_quota"}}'},
                          {"text": "ok"}])
    node_a = llm_pool.Node(name="A", proto="openai", base_url=fake_endpoint.url.rsplit("/v1/", 1)[0] + "/v1",
                           model="m", api_key="sk-SECRET-VALUE")
    node_b = llm_pool.Node(name="B", proto="openai", base_url=node_a.base_url,
                           model="m", api_key="sk-SECRET-VALUE")
    pool = llm_pool.CallPool([node_a, node_b], global_limit=1, lib=tmp_path)
    pool.call("s", "u")
    lines = [json.loads(x) for x in (tmp_path / ".process" / "调用事件.jsonl").read_text(encoding="utf-8").splitlines()]
    events = [x["event"] for x in lines]
    assert "pick" in events and "quota" in events and "ok" in events
    assert all("sk-SECRET-VALUE" not in json.dumps(x, ensure_ascii=False) for x in lines)
