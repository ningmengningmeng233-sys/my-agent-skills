import pytest


def test_budget_stops_run(fake_endpoint):
    from engine import llm_pool
    fake_endpoint.script([{"text": "ok"} for _ in range(5)])
    pool = llm_pool.CallPool([llm_pool.Node(name="A", proto="openai",
                                            base_url=fake_endpoint.url.rsplit("/v1/", 1)[0] + "/v1",
                                            model="m", api_key="k")],
                             global_limit=1, max_calls=2)
    pool.call("s", "u")
    pool.call("s", "u")
    with pytest.raises(llm_pool.BudgetExceeded) as e:
        pool.call("s", "u")
    assert e.value.code == "BUDGET_EXCEEDED" and "2" in str(e.value)


def test_budget_disabled_by_default(fake_endpoint):
    from engine import llm_pool
    fake_endpoint.script([{"text": "ok"} for _ in range(3)])
    pool = llm_pool.CallPool([llm_pool.Node(name="A", proto="openai",
                                            base_url=fake_endpoint.url.rsplit("/v1/", 1)[0] + "/v1",
                                            model="m", api_key="k")], global_limit=1)
    for _ in range(3):
        pool.call("s", "u")          # 不抛异常
