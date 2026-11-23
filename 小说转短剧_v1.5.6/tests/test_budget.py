import json
import math
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

import run_batch
from dramacraft.gateway import LLMGateway, BudgetExhausted

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG = {"nodes": [
    {"name": "N1", "proto": "openai", "base_url": "https://a.com/v1", "model": "m", "api_key": "k"},
    {"name": "N2", "proto": "openai", "base_url": "https://b.com/v1", "model": "m", "api_key": "k"},
]}


def _ok_response():
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"choices": [{"message": {"content": "文本"}, "finish_reason": "stop"}]}
    return r


def test_gateway_counts_every_http_attempt_including_failed_failover():
    with patch("requests.post", side_effect=[Exception("429"), _ok_response()]):
        gw = LLMGateway(config=CONFIG, max_calls=5)
        gw.call_llm("hi")
        assert gw.call_count == 2  # 失败尝试也烧额度，必须计数


def test_gateway_raises_budget_exhausted_at_cap():
    with patch("requests.post", return_value=_ok_response()):
        gw = LLMGateway(config=CONFIG, max_calls=2)
        gw.call_llm("a")
        gw.call_llm("b")
        with pytest.raises(BudgetExhausted):
            gw.call_llm("c")


def test_default_fuse_formula():
    assert run_batch._default_budget(pending=0) is None          # 无待跑集：不装闸
    assert run_batch._default_budget(pending=3) == math.ceil(3 * 4 * 1.1) + 10
    assert run_batch._default_budget(pending=1) == math.ceil(1 * 4 * 1.1) + 10


def test_run_batch_stops_clean_on_budget_exhausted(tmp_path, monkeypatch, capsys):
    class FakeGateway:
        last = None

        def __init__(self, config_path=None, config=None, max_calls=None):
            self.max_calls = max_calls
            self.call_count = 0
            FakeGateway.last = self

        def call_llm(self, prompt, system_prompt=""):
            self.call_count += 1
            if self.call_count >= (self.max_calls or 10 ** 9):
                raise BudgetExhausted(f"预算上限 {self.max_calls} 次已触达")
            return "{}"  # 故意给坏 JSON：本用例只验熔断路径，不验生成

    monkeypatch.setattr(run_batch, "LLMGateway", FakeGateway)
    monkeypatch.setattr(sys, "argv", [
        "run_batch.py",
        "--outline", os.path.join("fixtures", "sample_outline.json"),
        "--target-seconds", "90",
        "--max-calls", "1",
        "--workdir", str(tmp_path / "ws"),
    ])
    with pytest.raises(SystemExit) as ex:
        run_batch.main()
    assert ex.value.code == 3

    from dramacraft.ledger import load_events
    kinds = [e["event"] for e in load_events(tmp_path / "ws" / "state" / "ledger.jsonl")]
    assert "budget_stop" in kinds and "run_finish" in kinds
    out = capsys.readouterr().out
    assert "断点续跑" in out and "不重复计费" in out
