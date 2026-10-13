# -*- coding: utf-8 -*-
"""T1 统一熔断接线：self_healing 回炉循环 + run_batch 收束语义。

v1.5.0 T1 熔断统一（工单 R4）：
- 连续两轮阻塞项不缩小 → `escalate` **早停**（不再烧轮次等人拍板）
- 触回炉硬顶 → `abort`（即使每轮都在缩小，也只是缩小得不够快）
- 两者都 raise `HealingBlocked`：该集**未交付**，CLI 侧整批不中断
- 非零退出但条目不可解析 ≠ 通过（假成功防线）
"""
import json
import os
import sys

from unittest.mock import MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from dramacraft.ingestor import _read_file_safe  # noqa: E402
from dramacraft.ledger import load_events  # noqa: E402
from dramacraft.self_healing import HealingBlocked, generate_episode_with_healing  # noqa: E402

OUTLINE = os.path.join(ROOT, "fixtures", "sample_outline.json")


def _good_ep(n=1):
    sample = json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_script.json")))
    return sample["episodes"][n - 1]


def _bad_ep(n=1):
    return {
        "ep": n, "targetSeconds": 90, "hook": "开场", "hookBeat": [1, 1], "cliff": "结尾",
        "beatsClaimed": ["爽点"],
        "scenes": [{"sceneId": "S01", "characters": ["C01"],
                    "flow": [{"type": "action", "action": "主角若有所思地坐下。"}]}],
    }


def _outline():
    return json.loads(_read_file_safe(OUTLINE))


def _gen(gateway, tmp_path=None, **kw):
    return generate_episode_with_healing(
        ep_meta={"ep": 1, "targetSeconds": 90}, gateway=gateway, outline_data=_outline(),
        workdir=str(tmp_path) if tmp_path else None, **kw)


# ---------- 早停 / 硬顶 / 交付 ----------

def test_escalate_stops_early_without_burning_cap_rounds():
    """同一份硬伤每轮照原样回来（阻塞项不缩小）→ 第 2 轮即升级，不烧第 3 轮额度。"""
    gw = MagicMock()
    gw.call_llm.return_value = json.dumps(_bad_ep(1), ensure_ascii=False)
    with pytest.raises(HealingBlocked) as ei:
        _gen(gw, max_retries=3)
    assert ei.value.verdict == "escalate" and ei.value.delivered is False
    assert ei.value.rounds == 2 and gw.call_llm.call_count == 2
    assert ei.value.problems, "升级必须随附阻塞项清单（供人拍板）"


def test_abort_at_cap_when_shrinking_but_never_clean(monkeypatch):
    """每轮都在缩小（3→2→1）但触硬顶 → abort；硬顶就是硬顶，不无限续。"""
    rounds = [("g1|a|x", "g2|b|y", "g3|c|z"), ("g1|a|x", "g2|b|y"), ("g1|a|x",)]
    seen = {"n": 0}

    def fake_gate(script_data, outline_data=None, art_data=None, hits_collector=None):
        items = list(rounds[min(seen["n"], len(rounds) - 1)])
        seen["n"] += 1
        return False, "", items

    monkeypatch.setattr("dramacraft.self_healing.run_quality_gate", fake_gate)
    gw = MagicMock()
    gw.call_llm.return_value = json.dumps(_good_ep(1), ensure_ascii=False)
    with pytest.raises(HealingBlocked) as ei:
        _gen(gw, max_retries=3)
    assert ei.value.verdict == "abort" and ei.value.rounds == 3
    assert gw.call_llm.call_count == 3


def test_shrink_then_clean_delivers(monkeypatch):
    """阻塞项逐轮缩小并清零 → 正常交付（熔断不得误伤收敛中的回炉）。"""
    rounds = [(("g1|a|x"), ("g2|b|y")), (("g1|a|x"),), ()]
    seen = {"n": 0}

    def fake_gate(script_data, outline_data=None, art_data=None, hits_collector=None):
        items = list(rounds[min(seen["n"], len(rounds) - 1)])
        seen["n"] += 1
        return (not items), "", items

    monkeypatch.setattr("dramacraft.self_healing.run_quality_gate", fake_gate)
    gw = MagicMock()
    gw.call_llm.return_value = json.dumps(_good_ep(1), ensure_ascii=False)
    result = _gen(gw, max_retries=3)
    assert result.get("ep") == 1 and gw.call_llm.call_count == 3


def test_nonzero_exit_without_parsable_items_is_not_a_pass(monkeypatch):
    """假成功防线：门进程非零退出但条目不可解析 → 绝不静默当通过。"""
    monkeypatch.setattr("dramacraft.self_healing.run_quality_gate",
                        lambda *a, **k: (False, "报告文本不可解析", []))
    gw = MagicMock()
    gw.call_llm.return_value = json.dumps(_good_ep(1), ensure_ascii=False)
    with pytest.raises(HealingBlocked) as ei:
        _gen(gw, max_retries=1)
    assert ei.value.verdict == "abort" and gw.call_llm.call_count == 1


def test_blocked_run_leaves_journal_ledger_rows(tmp_path):
    """台账（跨进程真值）：每轮阻塞集与裁决都落 state/healing/epNNN.json。"""
    gw = MagicMock()
    gw.call_llm.return_value = json.dumps(_bad_ep(1), ensure_ascii=False)
    with pytest.raises(HealingBlocked):
        _gen(gw, tmp_path, max_retries=3)
    journal = tmp_path / "state" / "healing" / "ep001.json"
    doc = json.loads(journal.read_text("utf-8"))
    assert [r["round"] for r in doc["rounds"]] == [1, 2]
    assert [r["verdict"] for r in doc["rounds"]] == ["continue", "escalate"]
    assert all(r["pids"] for r in doc["rounds"]), "每轮都要留指纹，供工作流侧同读"


# ---------- run_batch：人不在场 → 未交付，但整批继续 ----------

def test_run_batch_marks_blocked_episode_and_continues(tmp_path, monkeypatch):
    import run_batch

    outline_path = os.path.join(str(tmp_path), "outline.json")
    with open(outline_path, "w", encoding="utf-8") as fp:
        json.dump(_outline(), fp, ensure_ascii=False)

    good = {1: _good_ep(1), 3: _good_ep(3)}

    class _GW:
        def __init__(self, *a, **kw):
            self.nodes = [type("N", (), {"name": "fake-node"})()]
            self.call_count = 0
            self.max_calls = None

        def call_llm(self, prompt=None, system_prompt=None, ep=None):
            self.call_count += 1
            return json.dumps(good.get(ep) or _bad_ep(ep), ensure_ascii=False)

    monkeypatch.setattr(run_batch, "LLMGateway", _GW)

    workdir = os.path.join(str(tmp_path), "ws")
    monkeypatch.setattr(sys, "argv", [
        "run_batch.py", "--outline", outline_path, "--target-seconds", "90",
        "--workdir", workdir, "--concurrency", "1",
    ])
    with pytest.raises(SystemExit) as ei:
        run_batch.main()
    assert ei.value.code == 2, "有集未交付 → 非零退出（不是静默成功）"

    ep_dir = os.path.join(workdir, "state", "episodes")
    assert os.path.exists(os.path.join(ep_dir, "ep001.json"))
    assert not os.path.exists(os.path.join(ep_dir, "ep002.json")), "受阻集绝不落盘当成品"
    assert os.path.exists(os.path.join(ep_dir, "ep003.json")), "阻塞集之后的集照跑（产线继续）"

    events = load_events(os.path.join(workdir, "state", "ledger.jsonl"))
    undelivered = [e for e in events if e.get("event") == "episode_undelivered"]
    assert [e["ep"] for e in undelivered] == [2]
    assert undelivered[0]["verdict"] == "escalate"
    assert [e.get("status") for e in events if e.get("event") == "run_finish"] == ["partial"]
    assert (tmp_path / "ws" / "state" / "healing" / "ep002.json").exists()
