# -*- coding: utf-8 -*-
import concurrent.futures
import json
import os
import threading
import pytest

from dramacraft.ledger import Ledger, load_events
from dramacraft.gateway import LLMGateway, BudgetExhausted, LLMNode


def test_ledger_thread_safety(tmp_path):
    ledger_path = tmp_path / "state" / "ledger.jsonl"
    ledger = Ledger(str(ledger_path))

    num_threads = 10
    events_per_thread = 20
    total_expected = num_threads * events_per_thread

    def worker(t_id):
        for i in range(events_per_thread):
            ledger.emit("test_event", ep=t_id, seq=i, payload=f"data-{t_id}-{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, tid) for tid in range(num_threads)]
        concurrent.futures.wait(futures)

    events = load_events(str(ledger_path))
    assert len(events) == total_expected, f"Expected {total_expected} events, got {len(events)}"

    # 验证每一行都是完整有效的 JSON
    with open(str(ledger_path), "r", encoding="utf-8") as fp:
        lines = [line.strip() for line in fp if line.strip()]
    assert len(lines) == total_expected
    for line in lines:
        obj = json.loads(line)
        assert obj["event"] == "test_event"


def test_gateway_call_count_thread_safety():
    config = {
        "nodes": [
            {"name": "NodeA", "proto": "openai", "base_url": "https://a.com", "model": "m", "api_key": "k"}
        ]
    }
    cap = 50
    gw = LLMGateway(config=config, max_calls=cap)

    num_threads = 10
    calls_per_thread = 10
    # 总共试图调用 100 次，上限 50 次，必定触发熔断

    success_charges = 0
    exhausted_count = 0
    lock = threading.Lock()

    def worker():
        nonlocal success_charges, exhausted_count
        for _ in range(calls_per_thread):
            try:
                gw._charge()
                with lock:
                    success_charges += 1
            except BudgetExhausted:
                with lock:
                    exhausted_count += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker) for _ in range(num_threads)]
        concurrent.futures.wait(futures)

    # 验证计费严格等于 cap，绝不超支
    assert gw.call_count == cap
    assert success_charges == cap
    assert gw.is_exhausted is True
    assert exhausted_count > 0


def test_safe_write_json_atomic(tmp_path):
    from run_batch import safe_write_json

    out_file = tmp_path / "atomic_test.json"
    data = {"ep": 1, "title": "测试", "payload": [i for i in range(100)]}

    # 并发 10 个线程同时写同一个文件，验证文件无损坏
    def worker(i):
        d = dict(data)
        d["worker_id"] = i
        safe_write_json(str(out_file), d)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker, i) for i in range(20)]
        concurrent.futures.wait(futures)

    assert out_file.exists()
    # 读出必须是完整合法的 JSON
    res = json.loads(out_file.read_text(encoding="utf-8"))
    assert res["ep"] == 1
    assert "worker_id" in res


def test_run_batch_concurrency_execution(tmp_path, monkeypatch):
    import sys
    import run_batch

    with open("fixtures/sample_script.json", "r", encoding="utf-8") as fp:
        sample_script = json.load(fp)
    ep_map = {ep["ep"]: ep for ep in sample_script["episodes"]}

    class MockConcurrencyGateway:
        def __init__(self, *args, **kwargs):
            self.call_count = 0
            self.max_calls = kwargs.get("max_calls")
            self.nodes = [LLMNode("TestNode", "openai", "http://test", "model", "key")]

        def call_llm(self, prompt, system_prompt="", ep=None):
            self.call_count += 1
            # 根据 prompt 或 ep 返回对应集的满分 JSON
            target_ep = ep or 1
            ep_data = ep_map.get(target_ep, ep_map[1])
            return json.dumps(ep_data, ensure_ascii=False)

    monkeypatch.setattr(run_batch, "LLMGateway", MockConcurrencyGateway)

    ws = tmp_path / "ws"
    monkeypatch.setattr(sys, "argv", [
        "run_batch.py",
        "--outline", "fixtures/sample_outline.json",
        "--target-seconds", "90",
        "--concurrency", "3",
        "--workdir", str(ws)
    ])

    run_batch.main()

    # 验证全部 3 集缓存均已成功落盘，且具备 v1.2.1 注入的 _meta
    ep_dir = ws / "state" / "episodes"
    for i in (1, 2, 3):
        ep_file = ep_dir / f"ep{i:03d}.json"
        assert ep_file.exists()
        d = json.loads(ep_file.read_text(encoding="utf-8"))
        assert d["_meta"]["engineVersion"] == "1.2.1"
        assert d["_meta"]["schemaVersion"] == 2

    # 验证生产小结存在
    log_file = ws / "output" / "production_log.md"
    assert log_file.exists()


def test_run_batch_concurrency_budget_stop(tmp_path, monkeypatch, capsys):
    import sys
    import run_batch

    class MockBudgetGateway:
        def __init__(self, *args, **kwargs):
            self.call_count = 0
            self.max_calls = kwargs.get("max_calls", 1)
            self.nodes = [LLMNode("TestNode", "openai", "http://test", "model", "key")]
            self._lock = threading.Lock()
            self.is_exhausted = False

        def call_llm(self, prompt, system_prompt="", ep=None):
            with self._lock:
                if self.is_exhausted or self.call_count >= self.max_calls:
                    self.is_exhausted = True
                    raise BudgetExhausted("并发熔断测试触发")
                self.call_count += 1
            return "{}"

    monkeypatch.setattr(run_batch, "LLMGateway", MockBudgetGateway)

    ws = tmp_path / "ws"
    monkeypatch.setattr(sys, "argv", [
        "run_batch.py",
        "--outline", "fixtures/sample_outline.json",
        "--target-seconds", "90",
        "--concurrency", "3",
        "--max-calls", "1",
        "--workdir", str(ws)
    ])

    with pytest.raises(SystemExit) as ex:
        run_batch.main()
    assert ex.value.code == 3

    out = capsys.readouterr().out
    assert "断点续跑" in out and "不重复计费" in out


