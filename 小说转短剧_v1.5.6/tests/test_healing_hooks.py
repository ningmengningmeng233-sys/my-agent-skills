# -*- coding: utf-8 -*-
"""T3（v1.4.0 创作纵深）：自愈循环挂接。

三件事：
1. context_block 初稿与每一轮回炉**同源**（同时根治 v1.3.0「原文上下文只在初稿、回炉即蒸发」缺陷）；
2. T 门挂点：每轮 12 门通过后进程内直调命名检查，违规则并入 failed_items 继续回炉；
3. F2 认领对账：落盘后合成 pay 事件，认领不合规则 ⚠ 但不进回炉、不额外触发重生成。
"""
import json
import os
import subprocess

from unittest.mock import MagicMock

import pytest

from dramacraft.continuity_lint import (
    emit_event,
    foreshadow_open_overview,
    foreshadow_todo_for_ep,
    register_paid_claims,
    replay,
)
from dramacraft.self_healing import (
    build_context_block,
    build_healing_prompt,
    generate_episode_with_healing,
)
from dramacraft.ingestor import _read_file_safe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _good_ep(n=1):
    sample = json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_script.json")))
    return sample["episodes"][n - 1]


def _bad_ep():
    return {
        "ep": 1, "targetSeconds": 90, "hook": "开场", "hookBeat": [1, 1], "cliff": "结尾",
        "beatsClaimed": ["爽点"],
        "scenes": [{"sceneId": "S01", "characters": ["C01"],
                    "flow": [{"type": "action", "action": "主角若有所思地坐下。"}]}],
    }


# ---------- context_block ----------

def test_build_context_block_empty_returns_empty_string():
    assert build_context_block() == ""
    assert build_context_block(None, None, None) == ""


def test_build_context_block_splits_three_sections():
    todo = [{"id": "F001", "text": "碎瓷片上的血迹", "plantedInEp": 1, "payoffPlannedEp": 3}]
    ctx = build_context_block("第一章：北境长城之上，战神归来。", todo, ["F002「旧照片」计划第 5 集回收"])
    assert "【原著对应章节参考】" in ctx and "北境长城之上" in ctx
    assert "【本集应回收】" in ctx and "F001" in ctx and "碎瓷片上的血迹" in ctx
    assert "【全剧未回收概览】" in ctx and "F002" in ctx


def test_build_context_block_omits_empty_sections():
    ctx = build_context_block(None, [{"id": "F001", "text": "t", "plantedInEp": 1,
                                      "payoffPlannedEp": 2}], None)
    assert "【本集应回收】" in ctx
    assert "【原著对应章节参考】" not in ctx
    assert "【全剧未回收概览】" not in ctx


# ---------- ① context_block 同源（v1.3.0 缺陷回归） ----------

def test_context_block_present_in_every_healing_round():
    """初稿与回炉轮均携带原著上下文与待回收清单——回炉不再丢失上下文。"""
    gateway = MagicMock()
    gateway.call_llm.side_effect = [
        json.dumps(_bad_ep(), ensure_ascii=False),
        json.dumps(_good_ep(1), ensure_ascii=False),
    ]
    todo = [{"id": "F001", "text": "碎瓷片上的血迹", "plantedInEp": 1, "payoffPlannedEp": 3}]
    ctx = build_context_block("第一章：北境长城之上，战神归来。", todo, None)

    generate_episode_with_healing(
        ep_meta={"ep": 1, "targetSeconds": 90},
        gateway=gateway,
        outline_data=json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_outline.json"))),
        max_retries=2,
        context_block=ctx,
    )

    assert gateway.call_llm.call_count == 2
    for call in gateway.call_llm.call_args_list:
        prompt = call[1].get("prompt") or call[0][0]
        assert "【原著对应章节参考】" in prompt, "初稿/回炉轮必须同源携带原著上下文"
        assert "北境长城之上" in prompt
        assert "【本集应回收】" in prompt and "F001" in prompt


def test_source_context_only_call_still_injects_legacy_header():
    """旧调用方（只传 source_context）行为保持：首轮与回炉轮都带原著段。"""
    gateway = MagicMock()
    gateway.call_llm.side_effect = [
        json.dumps(_bad_ep(), ensure_ascii=False),
        json.dumps(_good_ep(1), ensure_ascii=False),
    ]
    generate_episode_with_healing(
        ep_meta={"ep": 1, "targetSeconds": 90}, gateway=gateway,
        outline_data=json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_outline.json"))),
        max_retries=2, source_context="第一章：北境长城之上，战神归来。",
    )
    for call in gateway.call_llm.call_args_list:
        assert "【原著对应章节参考】" in (call[1].get("prompt") or call[0][0])


# ---------- ② T 门进回炉 ----------

def test_t_gate_violation_returns_to_healing_loop():
    gateway = MagicMock()
    gateway.call_llm.side_effect = [
        json.dumps(_good_ep(1), ensure_ascii=False),
        json.dumps(_good_ep(1), ensure_ascii=False),
    ]
    seen = []

    def t_gate(ep_data):
        seen.append(ep_data.get("ep"))
        return ["[T1] 第 1 集 场 S01 拍 1：出现「林辰」，标准写法「林晨」——按术语锁定表替换"] \
            if len(seen) == 1 else []

    result = generate_episode_with_healing(
        ep_meta={"ep": 1, "targetSeconds": 90}, gateway=gateway,
        outline_data=json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_outline.json"))),
        max_retries=3, t_gate_fn=t_gate,
    )

    assert result is not None
    assert gateway.call_llm.call_count == 2, "T 违规必须继续回炉，不得提前收工"
    assert len(seen) == 2, "每轮 12 门通过后都要跑 T 门"
    second_prompt = gateway.call_llm.call_args_list[1][1].get("prompt")
    assert "林辰" in second_prompt and "术语锁定表" in second_prompt


def test_t_gate_clean_returns_first_round():
    gateway = MagicMock()
    gateway.call_llm.return_value = json.dumps(_good_ep(1), ensure_ascii=False)
    result = generate_episode_with_healing(
        ep_meta={"ep": 1, "targetSeconds": 90}, gateway=gateway,
        outline_data=json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_outline.json"))),
        max_retries=3, t_gate_fn=lambda d: [],
    )
    assert result is not None and gateway.call_llm.call_count == 1


# ---------- ⑤ 向后兼容 ----------

def test_build_healing_prompt_two_arg_call_still_works():
    prompt = build_healing_prompt({"ep": 1, "scenes": []}, ["[违规] 某条"])
    assert "修改指令" in prompt and "某条" in prompt


def test_build_healing_prompt_appends_context_block():
    prompt = build_healing_prompt({"ep": 1, "scenes": []}, ["x"], context_block="【本集应回收】\n- F001")
    assert "【本集应回收】" in prompt and "F001" in prompt


# ---------- 待回收清单派生 ----------

def test_foreshadow_todo_for_ep_only_due_and_open():
    states = {
        "F001": {"id": "F001", "status": "open", "text": "到期未收", "plantedInEp": 1,
                 "payoffPlannedEp": 2},
        "F002": {"id": "F002", "status": "open", "text": "还没到期", "plantedInEp": 1,
                 "payoffPlannedEp": 3},
        "F003": {"id": "F003", "status": "paid", "text": "已回收", "payoffPlannedEp": 2},
    }
    todo = foreshadow_todo_for_ep(states, 2)
    assert [t["id"] for t in todo] == ["F001"]
    assert todo[0]["text"] == "到期未收"


def test_foreshadow_open_overview_names_each_open_item():
    states = {"F002": {"id": "F002", "status": "open", "text": "旧照片背面的字",
                       "payoffPlannedEp": 3}}
    lines = foreshadow_open_overview(states)
    assert len(lines) == 1 and "F002" in lines[0] and "第 3 集" in lines[0]


# ---------- ③④ F2 认领对账 ----------

def _planted(workdir):
    emit_event(workdir, "plant", ep=1, id="F001", text="碎瓷片上的血迹",
               payoffPlannedEp=3, registeredBy="outline")


def test_register_paid_claims_emits_pay_event_for_valid_claim(tmp_path):
    _planted(tmp_path)
    states = replay(tmp_path, {1, 2, 3})
    ep = {"ep": 3, "foreshadowsPaid": [
        {"id": "F001", "evidence": "第3集第1场：瓷片特写+林晨认领"}]}
    warns = register_paid_claims(tmp_path, ep, 3, states)
    assert warns == []
    st = replay(tmp_path, {1, 2, 3})["F001"]
    assert st["status"] == "paid" and st["paidInEp"] == 3
    assert "第3集第1场" in st["evidence"]


def test_register_paid_claims_unknown_id_warns_without_pay(tmp_path):
    _planted(tmp_path)
    states = replay(tmp_path, {1, 2, 3})
    ep = {"ep": 3, "foreshadowsPaid": [{"id": "F999", "evidence": "第3集第1场"}]}
    warns = register_paid_claims(tmp_path, ep, 3, states)
    assert any("F999" in w and "账本" in w for w in warns)
    assert replay(tmp_path, {1, 2, 3})["F001"]["status"] == "open"


def test_register_paid_claims_without_evidence_warns_without_pay(tmp_path):
    _planted(tmp_path)
    states = replay(tmp_path, {1, 2, 3})
    warns = register_paid_claims(tmp_path, {"ep": 3, "foreshadowsPaid": ["F001"]}, 3, states)
    assert any("F001" in w and "证据" in w for w in warns)
    assert replay(tmp_path, {1, 2, 3})["F001"]["status"] == "open"


def test_register_paid_claims_early_claim_warns_without_pay(tmp_path):
    emit_event(tmp_path, "plant", ep=2, id="F002", text="旧照片背面的字",
               payoffPlannedEp=3, registeredBy="outline")
    states = replay(tmp_path, {1, 2, 3})
    ep = {"ep": 1, "foreshadowsPaid": [{"id": "F002", "evidence": "第1集第1场"}]}
    warns = register_paid_claims(tmp_path, ep, 1, states)
    assert any("F002" in w and "早于" in w for w in warns)
    assert replay(tmp_path, {1, 2, 3})["F002"]["status"] == "open"


def test_register_paid_claims_late_claim_warns_but_honors(tmp_path):
    _planted(tmp_path)
    states = replay(tmp_path, {1, 2, 3})
    ep = {"ep": 3, "foreshadowsPaid": [{"id": "F001", "evidence": "第3集第1场"}]}
    warns = register_paid_claims(tmp_path, ep, 3, states)
    assert any("F001" in w and "超" in w for w in warns) is False  # 3 集即计划集，不算超期
    states2 = replay(tmp_path, {1, 2, 3})
    ep_late = {"ep": 5, "foreshadowsPaid": [{"id": "F001", "evidence": "第5集第1场"}]}
    warns2 = register_paid_claims(tmp_path, ep_late, 5, states2)
    assert any("F001" in w for w in warns2)


def test_register_paid_claims_reports_overdue_open(tmp_path):
    _planted(tmp_path)
    states = replay(tmp_path, {1, 2, 3})
    warns = register_paid_claims(tmp_path, {"ep": 3}, 3, states)
    assert any("F001" in w and "超期未填" in w for w in warns)


def test_register_paid_claims_on_episode_without_claims_is_silent(tmp_path):
    _planted(tmp_path)
    states = replay(tmp_path, {1})   # 第 1 集，计划回收在第 3 集，尚未到期
    assert register_paid_claims(tmp_path, {"ep": 1}, 1, states) == []


def test_register_paid_claims_survives_malformed_entry(tmp_path):
    _planted(tmp_path)
    states = replay(tmp_path, {1, 2, 3})
    ep = {"ep": 3, "foreshadowsPaid": [None, 42, "F001"]}
    warns = register_paid_claims(tmp_path, ep, 3, states)
    assert any("F001" in w and "证据" in w for w in warns)


# ---------- 三并发端到端冒烟（假网关，真 worker 路径） ----------

class _FakeGateway:
    def __init__(self, config_path=None, max_calls=None):
        self.nodes = [type("N", (), {"name": "fake-node"})()]
        self.call_count = 0
        self.max_calls = max_calls
        self.current_ep = None

    def call_llm(self, prompt=None, system_prompt=None, ep=None):
        self.call_count += 1
        return json.dumps(_good_ep(ep or 1), ensure_ascii=False)


def test_three_way_concurrent_run_plants_and_pays(tmp_path, monkeypatch):
    """3 集并发跑批：第 1 集埋伏笔、第 3 集认领 → 账本 1 plant + 1 pay，四件套齐出。"""
    import run_batch

    outline = json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_outline.json")))
    outline["episodes"][0]["foreshadows"] = [{"text": "碎瓷片上的血迹", "payoffInEp": 3}]
    outline_path = os.path.join(str(tmp_path), "outline.json")
    with open(outline_path, "w", encoding="utf-8") as fp:
        json.dump(outline, fp, ensure_ascii=False)

    ep3 = _good_ep(3)
    ep3["foreshadowsPaid"] = [{"id": "F001", "evidence": "第3集第1场：瓷片特写+林晨认领"}]
    good = {1: _good_ep(1), 2: _good_ep(2), 3: ep3}

    class _GW(_FakeGateway):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._lock = __import__("threading").Lock()

        def call_llm(self, prompt=None, system_prompt=None, ep=None):
            with self._lock:
                self.call_count += 1
            return json.dumps(good[ep], ensure_ascii=False)

    monkeypatch.setattr(run_batch, "LLMGateway", _GW)

    workdir = os.path.join(str(tmp_path), "ws")
    monkeypatch.setattr("sys.argv", [
        "run_batch.py", "--outline", outline_path, "--target-seconds", "90",
        "--workdir", workdir, "--concurrency", "3",
    ])
    run_batch.main()   # 失败会 sys.exit → 抛 SystemExit，测试随之失败

    states = replay(workdir, {1, 2, 3})
    assert states["F001"]["status"] == "paid"
    assert states["F001"]["plantedInEp"] == 1 and states["F001"]["paidInEp"] == 3
    events = [e["event"] for e in
              __import__("dramacraft.ledger", fromlist=["load_events"]).load_events(
                  os.path.join(workdir, "state", "foreshadow_log.jsonl"))]
    assert events.count("plant") == 1 and events.count("pay") == 1

    for name in ("script.md", "script_breakdown.csv", "script.json", "script-report.html"):
        assert os.path.exists(os.path.join(workdir, "output", name)), name


# ---------- 评审修复回归（2026-09-22 独立代码评审 Warning） ----------

def test_register_paid_claims_accepts_bare_string_claim(tmp_path):
    """W1 回归：foreshadowsPaid 写成裸字符串时不得静默吞掉——应归一后给出可操作的证据提示。"""
    _planted(tmp_path)
    states = replay(tmp_path, {1, 2, 3})
    warns = register_paid_claims(tmp_path, {"ep": 3, "foreshadowsPaid": "F001"}, 3, states)
    assert any("F001" in w and "证据" in w for w in warns), warns


def test_register_paid_claims_warns_on_malformed_container(tmp_path):
    _planted(tmp_path)
    states = replay(tmp_path, {1, 2, 3})
    warns = register_paid_claims(tmp_path, {"ep": 3, "foreshadowsPaid": {"id": "F001"}}, 3, states)
    assert any("F001" in w for w in warns), warns


def test_register_paid_claims_rejects_duplicate_claim_across_episodes(tmp_path):
    """W2 回归：同一伏笔跨集重复认领 —— 保留首次证据，不重复记账。"""
    _planted(tmp_path)
    states = replay(tmp_path, {1, 2, 3})
    first = {"ep": 3, "foreshadowsPaid": [{"id": "F001", "evidence": "第3集第1场：瓷片特写"}]}
    assert register_paid_claims(tmp_path, first, 3, states) == []
    states2 = replay(tmp_path, {1, 2, 3})
    second = {"ep": 4, "foreshadowsPaid": [{"id": "F001", "evidence": "第4集第2场：又说了一遍"}]}
    warns = register_paid_claims(tmp_path, second, 4, states2)
    assert any("F001" in w and "重复" in w for w in warns), warns
    st = replay(tmp_path, {1, 2, 3, 4})["F001"]
    assert st["paidInEp"] == 3 and "第3集第1场" in st["evidence"], "首次证据必须保留"
