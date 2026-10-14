# -*- coding: utf-8 -*-
"""T1 代码评审（2026-09-22，判「修后合入」）三条 Must 的回归用例。

Must#1 `--assemble-only` 缺集静默交付（exit 0 假成功）
Must#2 台账跨运行残留 → 下一轮首轮假 escalate；`--round 0` / `--cap 0` 无校验
Must#3 坏台账把「部分交付 exit 3」降级成「整批硬失败 exit 1」
另附 Should：预算与真异常并存时真异常必须优先（不得吞成"充值重跑即可"）。
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dramacraft.breaker import Problem, append_round  # noqa: E402
from dramacraft.gateway import BudgetExhausted  # noqa: E402
from dramacraft.outline_lint import compute_outline_hash  # noqa: E402
from conftest import run_py

OUTLINE = os.path.join(ROOT, "fixtures", "sample_outline.json")


def _s(*specs):
    return frozenset(Problem(k, w, d) for k, w, d in specs)


def _gate_report(path, *, failed=(("台词密度", "EP03-S2 台词超长"),), gates_present=True):
    gates = [{"id": "g1", "label": "台词密度", "ok": True, "detail": ""}]
    for label, detail in failed:
        gates.append({"id": "gX", "label": label, "ok": False, "detail": detail})
    if not gates_present:
        gates = []
    path.write_text(json.dumps({"gates": gates, "hits": [], "warnings": []}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def _run_breaker(*args):
    return run_py(["-m", "dramacraft.breaker", *map(str, args)], cwd=ROOT)


def _run_batch(args):
    return run_py([os.path.join(ROOT, "run_batch.py"), *args], cwd=ROOT)


# ---------------------------------------------------------------- Must#2 台账残留 / 参数边界

def test_stale_journal_round_does_not_fake_escalate(tmp_path):
    """上一轮运行留下的 round=0 记录，不得让本轮的 round 1 变成"连续两轮不降"。"""
    work = tmp_path / "w"
    journal = work / "state" / "healing" / "ep003.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({
        "ep": 3,
        "rounds": [{"round": 0, "verdict": "continue",
                    "problems": [["g1", "a", "x"]], "pids": ["deadbeef0000"], "ts": "2026-09-21T00:00:00"}],
    }, ensure_ascii=False), encoding="utf-8")          # 旧版/上一轮留下的残留
    rep = _gate_report(tmp_path / "gate.json")
    out = tmp_path / "brk.json"
    cp = _run_breaker("--workdir", work, "--ep", 3, "--round", 1, "--gate-report", rep, "--json", out)
    assert cp.returncode == 0 and cp.stdout.strip() == "continue", cp.stdout + cp.stderr


def test_new_attempt_purges_previous_rounds(tmp_path):
    """本轮从 round 1 重新开始 = 新一次尝试：旧轮次残留必须清掉，不得外泄成"历史"。"""
    work = tmp_path / "w"
    rep = _gate_report(tmp_path / "gate.json")
    append_round(work, ep=3, round_no=1, problems=_s(("g1", "a", "x")), verdict="continue")
    append_round(work, ep=3, round_no=2, problems=_s(("g1", "a", "x")), verdict="abort")
    out = tmp_path / "brk.json"
    cp = _run_breaker("--workdir", work, "--ep", 3, "--round", 1, "--gate-report", rep, "--json", out)
    assert cp.stdout.strip() == "continue"
    doc = json.loads((work / "state" / "healing" / "ep003.json").read_text("utf-8"))
    assert [r["round"] for r in doc["rounds"]] == [1]


@pytest.mark.parametrize("bad", [["--round", "0"], ["--cap", "0"]])
def test_round_and_cap_must_be_positive(tmp_path, bad):
    rep = _gate_report(tmp_path / "gate.json")
    cp = _run_breaker("--workdir", tmp_path / "w", "--ep", 1, "--round", 1, "--gate-report", rep,
                      "--json", tmp_path / "b.json", *(bad if bad[0] != "--round" else []),
                      *(["--round", "0"] if bad[0] == "--round" else bad))
    assert cp.returncode == 1, cp.stdout + cp.stderr


def test_empty_gate_set_is_hard_error(tmp_path):
    """Should#3：门报告是合法 JSON 但门集合为空 → 不许当"没有违规"放行。"""
    rep = _gate_report(tmp_path / "gate.json", gates_present=False)
    out = tmp_path / "brk.json"
    cp = _run_breaker("--workdir", tmp_path / "w", "--ep", 1, "--round", 1, "--gate-report", rep,
                      "--json", out)
    assert cp.returncode == 1 and not out.exists()


# ---------------------------------------------------------------- Must#3 坏台账不致命

def test_corrupt_journal_is_quarantined_not_fatal(tmp_path):
    """坏台账是派生件损坏：留证据、当空台账继续，绝不把整批拖成硬失败。"""
    work = tmp_path / "w"
    journal = work / "state" / "healing" / "ep001.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{ 这不是合法 JSON", encoding="utf-8")
    rep = _gate_report(tmp_path / "gate.json")
    out = tmp_path / "brk.json"
    cp = _run_breaker("--workdir", work, "--ep", 1, "--round", 1, "--gate-report", rep, "--json", out)
    assert cp.returncode == 0 and cp.stdout.strip() == "continue", cp.stdout + cp.stderr
    assert list(journal.parent.glob("ep001.json.broken*")), "坏台账要留证，不许静默删"
    assert json.loads(out.read_text("utf-8"))["verdict"] == "continue"


# ---------------------------------------------------------------- Must#1 assemble-only 缺集

def _seed_cached_episode(ep_dir, ep_num, outline_hash, target_seconds=90):
    """播一集**能过终验**的缓存：正片内容直接取 sample_script 的第 N 集，只注入 _meta。"""
    ep_dir.mkdir(parents=True, exist_ok=True)
    sample = json.loads(open(os.path.join(ROOT, "fixtures", "sample_script.json"),
                             encoding="utf-8").read())
    payload = json.loads(json.dumps(next(e for e in sample["episodes"] if e.get("ep") == ep_num)))
    payload["_meta"] = {"engineVersion": "1.2.1", "schemaVersion": 2, "outlineHash": outline_hash,
                        "gatesPassedAt": "2026-09-22T00:00:00+08:00"}
    path = ep_dir / f"ep{ep_num:03d}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_assemble_only_with_missing_episode_refuses_silent_success(tmp_path):
    outline = json.loads(open(OUTLINE, encoding="utf-8").read())
    outline_hash = compute_outline_hash(outline)
    ws = tmp_path / "ws"
    ep_dir = ws / "state" / "episodes"
    _seed_cached_episode(ep_dir, 1, outline_hash)
    _seed_cached_episode(ep_dir, 3, outline_hash)          # 缺第 2 集

    res = _run_batch(["--outline", OUTLINE, "--target-seconds", "90",
                      "--workdir", str(ws), "--assemble-only"])
    out = res.stdout + res.stderr
    assert res.returncode == 3, out
    assert "第 2 集" in out, out
    assert "成功" not in out, "缺集还报成功就是假成功"
    assert not (ws / "output" / "script.json").exists(), "缺集不得出交付物"


def test_assemble_only_with_all_episodes_present_still_passes(tmp_path):
    outline = json.loads(open(OUTLINE, encoding="utf-8").read())
    outline_hash = compute_outline_hash(outline)
    ws = tmp_path / "ws"
    ep_dir = ws / "state" / "episodes"
    for ep_n in (1, 2, 3):
        _seed_cached_episode(ep_dir, ep_n, outline_hash)

    res = _run_batch(["--outline", OUTLINE, "--target-seconds", "90",
                      "--workdir", str(ws), "--assemble-only"])
    assert res.returncode == 0, res.stdout + res.stderr
    assert (ws / "output" / "script.json").exists()


# ---------------------------------------------------------------- Should#1 真异常优先于预算收束

def test_runtime_error_wins_over_budget_stop(tmp_path, monkeypatch):
    """预算耗尽与真异常并存时：真异常必须优先浮出（否则用户被误导成"充值重跑即可"）。"""
    import run_batch

    outline_path = tmp_path / "outline.json"
    outline_path.write_text(open(OUTLINE, encoding="utf-8").read(), encoding="utf-8")

    class _GW:
        def __init__(self, *a, **kw):
            self.nodes = [type("N", (), {"name": "fake-node"})()]
            self.call_count = 0
            self.max_calls = None

        def call_llm(self, prompt=None, system_prompt=None, ep=None):
            self.call_count += 1
            if ep == 1:
                raise RuntimeError("线上崩了（真异常）")
            raise BudgetExhausted("预算用完了")

    monkeypatch.setattr(run_batch, "LLMGateway", _GW)
    monkeypatch.setattr(sys, "argv", [
        "run_batch.py", "--outline", str(outline_path), "--target-seconds", "90",
        "--workdir", str(tmp_path / "ws"), "--concurrency", "2",
    ])
    with pytest.raises(SystemExit) as ei:
        run_batch.main()
    assert ei.value.code == 1, "真异常必须按 1 退出，不能被预算收束吞成 3"


# ---------------------------------------------------------------- 单集验证的唯一入口（T7 冒烟实测逼出来的）

def _run_self_healing(*args):
    return run_py(["-m", "dramacraft.self_healing", *map(str, args)], cwd=ROOT)


def test_single_ep_cli_wraps_bare_episode_and_passes(tmp_path):
    """裸单集件直验会被 validate.mjs 判成 0 集（4 门必红）；走引擎入口必须全绿。"""
    sample = json.loads(open(os.path.join(ROOT, "fixtures", "sample_script.json"), encoding="utf-8").read())
    bare = tmp_path / "ep001.json"
    bare.write_text(json.dumps(next(e for e in sample["episodes"] if e["ep"] == 1), ensure_ascii=False),
                    encoding="utf-8")
    out = tmp_path / "rep.json"
    cp = _run_self_healing("--ep", bare, "--outline", OUTLINE, "--json", out)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert json.loads(out.read_text("utf-8"))["passed"] is True


def test_single_ep_cli_reports_violations_and_missing_input(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"ep": 1, "targetSeconds": 90, "scenes": [
        {"sceneId": "S01", "characters": ["C01"], "flow": [
            {"type": "action", "action": "他若有所思地站着。"}]}]}, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "rep.json"
    cp = _run_self_healing("--ep", bad, "--outline", OUTLINE, "--json", out)
    assert cp.returncode == 2 and json.loads(out.read_text("utf-8"))["hard"]
    missing = tmp_path / "nope.json"
    cp2 = _run_self_healing("--ep", tmp_path / "nope.json", "--json", tmp_path / "r2.json")
    assert cp2.returncode == 1 and not (tmp_path / "r2.json").exists()


def test_single_ep_cli_report_feeds_breaker(tmp_path):
    """单集报告必须带**真 gates**：空 gates 喂熔断器会被拒判（装包实验实跑逼出来的同源缺口）。

    形状口径：报告里 `gates` 为 validate.mjs 原始 12 门；`hard` 仍按「✗ [违规] 标签 + 详情条目」列，
    两条来源在熔断器里会被 set 去重，不会重复计数。
    """
    sample = json.loads(open(os.path.join(ROOT, "fixtures", "sample_script.json"), encoding="utf-8").read())
    bare = tmp_path / "ep001.json"
    bare.write_text(json.dumps(next(e for e in sample["episodes"] if e["ep"] == 1), ensure_ascii=False),
                    encoding="utf-8")
    out = tmp_path / "rep.json"
    cp = _run_self_healing("--ep", bare, "--outline", OUTLINE, "--json", out)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    report = json.loads(out.read_text("utf-8"))
    assert len(report["gates"]) == 12, f"单集报告必须带满 12 门，实得 {len(report['gates'])}"
    brk = run_py(["-m", "dramacraft.breaker", "--workdir", str(tmp_path / "ws"),
                  "--ep", "1", "--round", "1", "--cap", "3", "--gate-report", str(out),
                  "--json", str(tmp_path / "brk.json")], cwd=ROOT)
    assert brk.returncode == 0, brk.stdout + brk.stderr
    assert json.loads((tmp_path / "brk.json").read_text("utf-8"))["verdict"] == "pass"
