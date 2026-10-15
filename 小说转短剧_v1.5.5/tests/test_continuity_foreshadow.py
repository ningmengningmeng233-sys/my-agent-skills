# -*- coding: utf-8 -*-
"""T1（v1.4.0 创作纵深）：伏笔账本事件流 + F1 大纲闸。

伏笔账本 = append-only 事件流（复用 Ledger 单行+锁），当前状态一律由 replay 重放派生；
F1 是纯函数大纲闸，与 O1-O6 同点同时机拒跑。
"""
import json
import os
import subprocess
import threading

import pytest

from dramacraft.continuity_lint import (
    emit_event,
    foreshadow_log_path,
    lint_registration,
    register_from_outline,
    replay,
)
from dramacraft.ledger import load_events
from dramacraft.premise_to_outline import convert_premises_to_outline
from conftest import run_py

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _outline(eps_spec, title="测试剧"):
    """eps_spec: [(ep号, [伏笔dict...]), ...]"""
    return {
        "title": title,
        "characters": [{"id": "C01", "name": "林秋", "desire": "查明真相",
                        "obstacle": "对手阻挠", "voiceStyle": "沉着有力"}],
        "episodes": [
            {"ep": n, "targetSeconds": 90, "hook": f"第{n}集钩子", "cliff": f"第{n}集卡点",
             "beatsClaimed": [], "foreshadows": fs}
            for n, fs in eps_spec
        ],
        "beats": [],
    }


# ---------- 账本路径与事件写入 ----------

def test_log_path_lives_under_state(tmp_path):
    assert foreshadow_log_path(tmp_path) == os.path.join(str(tmp_path), "state", "foreshadow_log.jsonl")


def test_emit_event_appends_single_json_line(tmp_path):
    emit_event(tmp_path, "plant", ep=1, id="F001", text="玉佩夜里发烫",
               payoffPlannedEp=3, registeredBy="outline")
    events = load_events(foreshadow_log_path(tmp_path))
    assert len(events) == 1
    assert events[0]["event"] == "plant" and events[0]["ep"] == 1
    assert events[0]["id"] == "F001" and events[0]["payoffPlannedEp"] == 3
    assert "ts" in events[0]


def test_emit_event_never_overwrites_history(tmp_path):
    emit_event(tmp_path, "plant", ep=1, id="F001", text="a", payoffPlannedEp=2)
    emit_event(tmp_path, "plant", ep=1, id="F001", text="a", payoffPlannedEp=2)
    assert len(load_events(foreshadow_log_path(tmp_path))) == 2


def test_concurrent_emit_loses_no_line(tmp_path):
    """双 worker 并发登记零丢账（append-only + 单行原子）。"""
    errors = []

    def worker(tag):
        try:
            for i in range(25):
                emit_event(tmp_path, "plant", ep=1, id=f"{tag}{i:03d}", text=f"{tag}伏笔{i}",
                           payoffPlannedEp=2, registeredBy="outline")
        except Exception as e:  # pragma: no cover - 失败即测试失败
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    events = load_events(foreshadow_log_path(tmp_path))
    assert len(events) == 50, f"并发丢账：期望 50 行，实得 {len(events)}"
    ids = {e["id"] for e in events}
    assert len(ids) == 50 and "A000" in ids and "B024" in ids


# ---------- replay 重放派生 ----------

def test_replay_plant_derives_open(tmp_path):
    emit_event(tmp_path, "plant", ep=1, id="F001", text="玉佩夜里发烫",
               payoffPlannedEp=3, registeredBy="outline")
    states = replay(tmp_path, {1, 2, 3})
    assert states["F001"]["status"] == "open"
    assert states["F001"]["text"] == "玉佩夜里发烫"
    assert states["F001"]["plantedInEp"] == 1
    assert states["F001"]["payoffPlannedEp"] == 3


def test_replay_pay_derives_paid(tmp_path):
    emit_event(tmp_path, "plant", ep=1, id="F001", text="玉佩夜里发烫", payoffPlannedEp=3)
    emit_event(tmp_path, "pay", ep=3, id="F001", evidence="第3集第2场：玉佩特写+母亲认领")
    st = replay(tmp_path, {1, 2, 3})["F001"]
    assert st["status"] == "paid"
    assert st["paidInEp"] == 3
    assert "第3集第2场" in st["evidence"]


def test_replay_abandon_keeps_reason_and_decider(tmp_path):
    emit_event(tmp_path, "plant", ep=1, id="F001", text="玉佩夜里发烫", payoffPlannedEp=3)
    emit_event(tmp_path, "abandon", ep=3, id="F001",
               abandonReason="情节线砍掉，用户确认放弃", decidedBy="user")
    st = replay(tmp_path, {1, 2, 3})["F001"]
    assert st["status"] == "abandoned"
    assert st["abandonReason"] == "情节线砍掉，用户确认放弃"
    assert st["decidedBy"] == "user"


def test_replay_drops_pay_outside_valid_eps(tmp_path):
    """redo 场景：认领集缓存被删 → 该 pay 作废 → 伏笔自然回 open（重放即回滚）。"""
    emit_event(tmp_path, "plant", ep=1, id="F001", text="玉佩夜里发烫", payoffPlannedEp=3)
    emit_event(tmp_path, "pay", ep=3, id="F001", evidence="第3集第2场：认领")
    assert replay(tmp_path, {1, 2, 3})["F001"]["status"] == "paid"
    assert replay(tmp_path, {1, 2})["F001"]["status"] == "open"


def test_replay_without_valid_eps_honors_no_pay(tmp_path):
    """保守缺省：未确认的有效缓存集 → 认领一律不采信（宁多报不漏账）。"""
    emit_event(tmp_path, "plant", ep=1, id="F001", text="玉佩夜里发烫", payoffPlannedEp=3)
    emit_event(tmp_path, "pay", ep=3, id="F001", evidence="第3集第2场：认领")
    assert replay(tmp_path)["F001"]["status"] == "open"


def test_replay_on_empty_workdir_returns_empty(tmp_path):
    assert replay(tmp_path, set()) == {}


# ---------- F1 登记校验（纯函数） ----------

def test_f1_accepts_valid_registration(tmp_path):
    outline = _outline([(1, [{"text": "玉佩夜里发烫", "payoffInEp": 3}]),
                        (3, [])])
    assert lint_registration(outline) == []


def test_f1_accepts_single_episode_self_payoff():
    """单元剧：埋设与回收同集合法。"""
    outline = _outline([(1, [{"text": "旧照片背面的字", "payoffInEp": 1}])])
    assert lint_registration(outline) == []


def test_f1_rejects_empty_text():
    outline = _outline([(1, [{"text": "   ", "payoffInEp": 3}]), (3, [])])
    bad = lint_registration(outline)
    assert any("[F1]" in v and "text" in v for v in bad)


def test_f1_rejects_payoff_before_plant():
    outline = _outline([(1, []), (3, [{"text": "玉佩夜里发烫", "payoffInEp": 1}])])
    bad = lint_registration(outline)
    assert any("[F1]" in v and "早于" in v for v in bad)


def test_f1_rejects_payoff_skipping_episode_number():
    """集数可不连续，但回收集必须真实存在于大纲集号中（跳号即孤儿）。"""
    outline = _outline([(1, [{"text": "玉佩夜里发烫", "payoffInEp": 3}]), (2, []), (5, [])])
    bad = lint_registration(outline)
    assert any("[F1]" in v and "不在大纲集号" in v for v in bad)


def test_f1_rejects_payoff_beyond_all_episodes():
    outline = _outline([(1, [{"text": "玉佩夜里发烫", "payoffInEp": 99}]), (2, []), (3, [])])
    bad = lint_registration(outline)
    assert any("[F1]" in v and "不在大纲集号" in v for v in bad)


def test_f1_rejects_duplicate_explicit_id():
    outline = _outline([(1, [{"id": "F001", "text": "玉佩夜里发烫", "payoffInEp": 3}]),
                        (3, [{"id": "F001", "text": "旧照片背面的字", "payoffInEp": 3}])])
    bad = lint_registration(outline)
    assert any("[F1]" in v and "重复" in v for v in bad)


def test_f1_rejects_malformed_foreshadows_field():
    outline = _outline([(1, [])])
    outline["episodes"][0]["foreshadows"] = "玉佩夜里发烫"
    bad = lint_registration(outline)
    assert any("[F1]" in v and "列表" in v for v in bad)


def test_f1_reports_auto_assigned_ids_in_violation():
    outline = _outline([(1, [{"text": "", "payoffInEp": 3}]), (3, [])])
    bad = lint_registration(outline)
    assert any("F001" in v for v in bad)


# ---------- 大纲入册 ----------

def test_register_from_outline_emits_plants_with_auto_ids(tmp_path):
    outline = _outline([(1, [{"text": "玉佩夜里发烫", "payoffInEp": 3},
                             {"text": "旧照片背面的字", "payoffInEp": 3}]),
                        (3, [{"text": "烟灰缸里的钥匙", "payoffInEp": 3}])])
    assert register_from_outline(tmp_path, outline) == 3
    states = replay(tmp_path, {1, 2, 3})
    assert sorted(states.keys()) == ["F001", "F002", "F003"]
    assert states["F001"]["plantedInEp"] == 1
    assert states["F003"]["plantedInEp"] == 3
    assert states["F001"]["payoffPlannedEp"] == 3
    assert all(s["status"] == "open" for s in states.values())


def test_register_from_outline_is_idempotent(tmp_path):
    outline = _outline([(1, [{"text": "玉佩夜里发烫", "payoffInEp": 3}]), (3, [])])
    assert register_from_outline(tmp_path, outline) == 1
    assert register_from_outline(tmp_path, outline) == 0
    assert len(load_events(foreshadow_log_path(tmp_path))) == 1


def test_register_from_outline_skips_invalid_entries(tmp_path):
    outline = _outline([(1, [{"text": "", "payoffInEp": 3},
                             {"text": "玉佩夜里发烫", "payoffInEp": 3}]), (3, [])])
    assert register_from_outline(tmp_path, outline) == 1


def test_register_preserves_declared_id_and_source(tmp_path):
    outline = _outline([(1, [{"id": "F007", "text": "玉佩夜里发烫", "payoffInEp": 3,
                              "registeredBy": "topic-meeting"}]), (3, [])])
    assert register_from_outline(tmp_path, outline) == 1
    st = replay(tmp_path, {1})["F007"]
    assert st["registeredBy"] == "topic-meeting"


# ---------- 兼容回归 ----------

def test_sample_outline_without_foreshadows_is_clean():
    """旧大纲零新字段照跑：无伏笔契约字段 → F1 零违规。"""
    outline = json.load(open(os.path.join(ROOT, "fixtures", "sample_outline.json"), encoding="utf-8"))
    assert lint_registration(outline) == []


def test_register_on_old_outline_registers_nothing(tmp_path):
    outline = json.load(open(os.path.join(ROOT, "fixtures", "sample_outline.json"), encoding="utf-8"))
    assert register_from_outline(tmp_path, outline) == 0
    assert load_events(foreshadow_log_path(tmp_path)) == []


# ---------- F1 串联进大纲闸（_enforce_outline_gate / --validate-only） ----------

def _write_outline(tmp_path, outline):
    p = os.path.join(str(tmp_path), "outline.json")
    with open(p, "w", encoding="utf-8") as fp:
        json.dump(outline, fp, ensure_ascii=False)
    return p


def _seed_cached_episodes(tmp_path, outline, name="ws"):
    """按 outline 哈希播种合法缓存，供 --assemble-only 端到端跑批。"""
    from dramacraft.outline_lint import compute_outline_hash
    workdir = os.path.join(str(tmp_path), name)
    eps_dir = os.path.join(workdir, "state", "episodes")
    os.makedirs(eps_dir, exist_ok=True)
    sample = json.load(open(os.path.join(ROOT, "fixtures", "sample_script.json"), encoding="utf-8"))
    h = compute_outline_hash(outline)
    for ep in sample["episodes"]:
        c = dict(ep)
        c["_meta"] = {"engineVersion": "1.4.0", "schemaVersion": 2, "outlineHash": h}
        with open(os.path.join(eps_dir, f"ep{ep['ep']:03d}.json"), "w", encoding="utf-8") as fp:
            json.dump(c, fp, ensure_ascii=False)
    return workdir


def test_validate_only_path_rejects_bad_foreshadow_registration(tmp_path):
    """F1 与 O 门同点同时机拒跑：回收集跳号 → exit 2 且点名 [F1]。"""
    outline = json.load(open(os.path.join(ROOT, "fixtures", "sample_outline.json"), encoding="utf-8"))
    outline["episodes"][0]["foreshadows"] = [{"text": "碎瓷片上的血迹", "payoffInEp": 7}]
    outline_path = _write_outline(tmp_path, outline)
    res = run_py(
        ["run_batch.py", "--outline", outline_path, "--target-seconds", "90",
         "--validate-only", os.path.join("fixtures", "sample_script.json")],
        cwd=ROOT
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "[F1]" in res.stdout, res.stdout


def test_main_path_registers_outline_foreshadows_and_terminal_gate_blocks(tmp_path):
    """主路径：大纲伏笔加载即入册；未回收则**终验硬闸拦在交付前**（全绿不豁免）。"""
    outline = json.load(open(os.path.join(ROOT, "fixtures", "sample_outline.json"), encoding="utf-8"))
    outline["episodes"][0]["foreshadows"] = [{"text": "碎瓷片上的血迹", "payoffInEp": 3}]
    outline["episodes"][1]["foreshadows"] = [{"id": "F009", "text": "九龙金帅印", "payoffInEp": 3}]
    outline_path = _write_outline(tmp_path, outline)
    workdir = _seed_cached_episodes(tmp_path, outline)
    res = run_py(
        ["run_batch.py", "--outline", outline_path, "--target-seconds", "90",
         "--workdir", workdir, "--assemble-only"],
        cwd=ROOT
    )
    states = replay(workdir, {1, 2, 3})
    assert sorted(states.keys()) == ["F001", "F009"]
    assert states["F001"]["plantedInEp"] == 1 and states["F009"]["plantedInEp"] == 2
    assert all(s["status"] == "open" for s in states.values())
    # 埋了不收 → 12 门全绿也不许交付
    assert res.returncode == 1, res.stdout + res.stderr
    assert "未回收伏笔" in res.stdout
    assert not os.path.exists(os.path.join(workdir, "output", "script.md"))


def test_main_path_on_old_outline_writes_no_foreshadow_event(tmp_path):
    """兼容回归：旧大纲（零 foreshadows 字段）走完整主路径，账本零 plant、退出码不变。"""
    outline_path = os.path.join(ROOT, "fixtures", "sample_outline.json")
    outline = json.load(open(outline_path, encoding="utf-8"))
    workdir = _seed_cached_episodes(tmp_path, outline)
    res = run_py(
        ["run_batch.py", "--outline", outline_path, "--target-seconds", "90",
         "--workdir", workdir, "--assemble-only"],
        cwd=ROOT
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert replay(workdir, {1, 2, 3}) == {}


# ---------- 选题会纪要伏笔透传（premise_to_outline） ----------

def _premises_with_foreshadows():
    def mk(ep, title, foreshadows=None):
        p = {
            "ep": ep, "title": title, "cards_used": [], "verdict": "pass",
            "four": {
                "desire": {"summary": "夺回遗物"},
                "obstacle": {"summary": "对手阻拦"},
                "twist": {"summary": "亮出底牌"},
                "cost": {"summary": "付出代价"},
            },
            "cast": [{"name": f"角色{ep}", "role": "主角", "voiceStyle": "冷静"}],
            "hook": f"第{ep}集钩子", "cliff": f"第{ep}集卡点", "target_seconds": 90,
        }
        if foreshadows is not None:
            p["foreshadows"] = foreshadows
        return p

    return [mk(1, "银针断生死", [{"text": "银针上的暗记", "payoffInEp": 2}]),
            mk(2, "假面名媛")]


def test_premise_foreshadows_pass_through_to_outline():
    """选题会纪要的伏笔字段随四件套一起转译进大纲 episodes[i].foreshadows。"""
    outline = convert_premises_to_outline(_premises_with_foreshadows(), title="都市奇案录")
    ep1_fs = outline["episodes"][0]["foreshadows"]
    assert ep1_fs[0]["text"] == "银针上的暗记"
    assert ep1_fs[0]["payoffInEp"] == 2
    assert ep1_fs[0]["registeredBy"] == "topic-meeting"
    assert "foreshadows" not in outline["episodes"][1]


def test_premise_foreshadows_outline_passes_f1_and_registers(tmp_path):
    outline = convert_premises_to_outline(_premises_with_foreshadows(), title="都市奇案录")
    assert lint_registration(outline) == []
    assert register_from_outline(tmp_path, outline) == 1
    st = replay(tmp_path, {1, 2})["F001"]
    assert st["status"] == "open"
    assert st["plantedInEp"] == 1
    assert st["payoffPlannedEp"] == 2
    assert st["registeredBy"] == "topic-meeting"


def test_premises_without_foreshadows_add_no_field():
    """契约只增不改：纪要没有伏笔字段时，转译后的大纲不新增 foreshadows 键。"""
    outline = convert_premises_to_outline(_premises_with_foreshadows(), title="都市奇案录")
    assert "foreshadows" not in outline["episodes"][1]
    assert lint_registration(outline) == []


# ---------- 评审修复回归（2026-09-22 独立代码评审 Block/Warning） ----------

def test_pay_invalidated_when_episode_regenerated(tmp_path):
    """B1 回归：redo 后**重新生成**同集，旧 pay 必须失效（绑定集内容指纹，不只绑集号）。"""
    from dramacraft.continuity_lint import ep_content_hash
    emit_event(tmp_path, "plant", ep=1, id="F001", text="碎瓷片上的血迹", payoffPlannedEp=3)
    old_ep = {"ep": 3, "scenes": [{"sceneId": "S01", "flow": [
        {"action": "瓷片特写", "line": "这是我娘的。"}]}],
        "foreshadowsPaid": [{"id": "F001", "evidence": "第3集第1场：认领"}]}
    from dramacraft.continuity_lint import register_paid_claims
    register_paid_claims(tmp_path, old_ep, 3, replay(tmp_path, {1, 2, 3}))
    assert replay(tmp_path, {3}, {3: ep_content_hash(old_ep)})["F001"]["status"] == "paid"

    # 同集重生成：新稿没有认领声明 → 内容指纹变了 → 旧 pay 不得复活
    new_ep = {"ep": 3, "scenes": [{"sceneId": "S01", "flow": [
        {"action": "顾问转身离开", "line": "改天再说。"}]}]}
    assert ep_content_hash(new_ep) != ep_content_hash(old_ep)
    assert replay(tmp_path, {3}, {3: ep_content_hash(new_ep)})["F001"]["status"] == "open"


def test_pay_honored_when_episode_content_unchanged(tmp_path):
    from dramacraft.continuity_lint import ep_content_hash, register_paid_claims
    emit_event(tmp_path, "plant", ep=1, id="F001", text="碎瓷片上的血迹", payoffPlannedEp=3)
    ep = {"ep": 3, "scenes": [{"action": "x"}],
          "foreshadowsPaid": [{"id": "F001", "evidence": "第3集第1场：认领"}]}
    register_paid_claims(tmp_path, ep, 3, replay(tmp_path, {1, 2, 3}))
    assert replay(tmp_path, {3}, {3: ep_content_hash(ep)})["F001"]["status"] == "paid"


def test_load_ep_hashes_reads_workdir(tmp_path):
    from dramacraft.continuity_lint import ep_content_hash, load_ep_hashes
    eps_dir = os.path.join(str(tmp_path), "state", "episodes")
    os.makedirs(eps_dir)
    ep = {"ep": 2, "scenes": [], "_meta": {"engineVersion": "x"}}
    with open(os.path.join(eps_dir, "ep002.json"), "w", encoding="utf-8") as fp:
        json.dump(ep, fp, ensure_ascii=False)
    assert load_ep_hashes(tmp_path) == {2: ep_content_hash(ep)}


def test_register_survives_foreshadow_inserted_at_front(tmp_path):
    """B2 回归：在队首插入新伏笔导致自动编号漂移时，新伏笔不得静默丢失。"""
    base = _outline([(1, [{"text": "碎瓷片上的血迹", "payoffInEp": 3}]), (3, [])])
    assert register_from_outline(tmp_path, base) == 1
    assert replay(tmp_path, {1})["F001"]["text"] == "碎瓷片上的血迹"

    # 用户在前面插了一条新伏笔：自动编号重排，新文本与旧 F001 撞号
    inserted = _outline([(1, [{"text": "墙上的旧照片", "payoffInEp": 3},
                              {"text": "碎瓷片上的血迹", "payoffInEp": 3}]), (3, [])])
    assert register_from_outline(tmp_path, inserted) == 1, "插入的新伏笔必须入册，不得静默丢弃"
    states = replay(tmp_path, {1})
    texts = sorted(s.get("text") for s in states.values())
    assert texts == ["墙上的旧照片", "碎瓷片上的血迹"], f"账本内容错乱：{states}"


def test_register_is_idempotent_under_reorder(tmp_path):
    """纯重排（内容不变）不得重复入册。"""
    o1 = _outline([(1, [{"text": "甲", "payoffInEp": 3}, {"text": "乙", "payoffInEp": 3}]), (3, [])])
    o2 = _outline([(1, [{"text": "乙", "payoffInEp": 3}, {"text": "甲", "payoffInEp": 3}]), (3, [])])
    assert register_from_outline(tmp_path, o1) == 2
    assert register_from_outline(tmp_path, o2) == 0
    assert len(replay(tmp_path, {1})) == 2
