def test_second_run_makes_zero_calls(tmp_path, fake_endpoint, monkeypatch):
    """同一输入连跑两次，第二次不产生任何模型调用。"""
    monkeypatch.setenv("SUMMARY_API_URL", fake_endpoint.url)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("SUMMARY_API_MODEL", "m")
    monkeypatch.setenv("ASSET_LIB", str(tmp_path))
    fake_endpoint.script([{"text": "第一次"}])
    from engine import llm_pool
    pool = llm_pool.from_env(lib=tmp_path)                 # 首次：真调用
    pool.call("s", "u", step="描述补全", cache_key="资产:老罗:描述")
    before = len(fake_endpoint.received())
    pool2 = llm_pool.from_env(lib=tmp_path)                # 二次：同 cache_key
    text, usage = pool2.call("s", "u", step="描述补全", cache_key="资产:老罗:描述")
    assert len(fake_endpoint.received()) == before          # 没有新请求
    assert usage.get("cache_hit") is True
    assert text == "第一次"


def test_force_reruns_and_leaves_trace(tmp_path, fake_endpoint, monkeypatch):
    monkeypatch.setenv("SUMMARY_API_URL", fake_endpoint.url)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("SUMMARY_API_MODEL", "m")
    fake_endpoint.script([{"text": "A"}, {"text": "B"}])
    from engine import llm_pool
    p1 = llm_pool.from_env(lib=tmp_path)
    p1.call("s", "u", step="x", cache_key="k1")
    p2 = llm_pool.from_env(lib=tmp_path, force=True)
    text, _ = p2.call("s", "u", step="x", cache_key="k1")
    assert text == "B"
    events = (tmp_path / ".process" / "调用事件.jsonl").read_text(encoding="utf-8")
    assert "force" in events                                 # 留痕
