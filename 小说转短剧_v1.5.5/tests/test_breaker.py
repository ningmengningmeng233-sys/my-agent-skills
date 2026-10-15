"""T1 统一熔断执行器（工单 2026-09-22-DramaCraft-v1.5.0五阶接通 T1；方案 §3.3）

规则（钉死，判定顺序不可变）：
1. 本轮阻塞集为空 → `pass`（先于一切熔断判定——成功优先）
2. `round_no >= cap` → `abort`（该集 partial/未交付）
3. 最近两轮均 `len(cur) >= len(prev)` 且 cur 非空 → `escalate`（随附新旧差集）
4. 其余 → `continue`

熔断算术只在本模块实现一次：CLI 路径（self_healing/run_batch）与工作流路径
（`python -m dramacraft.breaker`）都消费它，工作流侧零复刻。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from dramacraft.breaker import (  # noqa: E402
    Problem,
    append_round,
    breaker_decision,
    breaker_detail,
    from_failed_items,
    load_history,
)
from conftest import run_py


def _s(*specs):
    return frozenset(Problem(k, w, d) for k, w, d in specs)


# ---------------------------------------------------------------- 判定四律

def test_zero_blockers_pass_before_stagnation():
    # [0,0,0] 字面触发"不降"，成功优先必须直接放行（审查 A6 反例）
    assert breaker_decision([_s(), _s(), _s()], round_no=3, cap=3) == "pass"


def test_same_count_new_problems_counts_stagnant():
    old = _s(("g11", "ep3-s2-b4", "禁词X"))
    new = _s(("g11", "ep3-s5-b1", "禁词Y"))
    assert breaker_decision([old, new, new], round_no=3, cap=5) == "escalate"


def test_strict_shrink_continues():
    old = _s(("g1", "ep1", "x"), ("g2", "ep1", "y"))
    cur = _s(("g2", "ep1", "y"))
    assert breaker_decision([old, cur], round_no=2, cap=3) == "continue"


def test_cap_reached_aborts():
    cur = _s(("g1", "ep1", "x"))
    assert breaker_decision([cur, cur], round_no=3, cap=3) == "abort"


def test_escalate_carries_diff_sets():
    old = _s(("g1", "a", "x"), ("g9", "b", "y"))
    cur = _s(("g1", "a", "x"), ("g3", "c", "z"))
    d = breaker_detail(old, cur)
    assert d["unresolved"] == {("g1", "a", "x")} and d["new"] == {("g3", "c", "z")} and d["resolved"] == {("g9", "b", "y")}


def test_first_round_without_prev_is_continue():
    cur = _s(("g1", "ep1", "x"))
    assert breaker_decision([cur], round_no=1, cap=3) == "continue"
    # prev 为空 ⇒「两轮都在」必为空集：首轮全部算本轮新出
    d = breaker_detail(frozenset(), cur)
    assert d["unresolved"] == set() and d["new"] == {("g1", "ep1", "x")} and d["resolved"] == set()


def test_problem_pid_is_stable_twelve_hex():
    a = Problem("g1", "ep3-s2", "禁词X")
    b = Problem("g1", "ep3-s2", "禁词X")
    c = Problem("g1", "ep3-s2", "禁词Y")
    assert a.pid == b.pid
    assert a.pid != c.pid
    assert len(a.pid) == 12 and all(ch in "0123456789abcdef" for ch in a.pid)


def test_equal_problems_hash_and_compare():
    # frozen dataclass 必须可哈希可相等——集合去重是熔断判定的一半语义
    assert _s(("g1", "a", "x"), ("g1", "a", "x")) == _s(("g1", "a", "x"))


# ---------------------------------------------------------------- 条目解析

def test_from_failed_items_stable_and_strips_markers():
    items = ["✗ [违规] 台词密度", "↳ 详情: EP03-S2 台词超长", "- EP03-S4 缺收尾"]
    p1 = from_failed_items(items)
    p2 = from_failed_items(list(items))
    assert p1 == p2 and len(p1) == 3
    # 同样内容带不带前缀归一为同一个 Problem（跨轮稳定性）
    assert from_failed_items(["EP03-S2 台词超长"]) == from_failed_items(["↳ 详情:  EP03-S2 台词超长"])


def test_pipe_form_splits_three_fields():
    ps = from_failed_items(["g11|ep3-s2-b4|禁词X"])
    assert len(ps) == 1
    p = next(iter(ps))
    assert (p.kind, p.where, p.digest) == ("g11", "ep3-s2-b4", "禁词X")


# ---------------------------------------------------------------- 台账（journal，跨进程唯一真值）

def test_append_round_upserts_same_round(tmp_path):
    cur = _s(("g1", "a", "x"))
    append_round(tmp_path, ep=3, round_no=1, problems=cur, verdict="continue")
    other = _s(("g1", "a", "x"), ("g2", "b", "y"))
    append_round(tmp_path, ep=3, round_no=1, problems=other, verdict="escalate")
    hist = load_history(tmp_path, ep=3)
    assert len(hist) == 1 and hist[0] == other  # 同轮重写=覆盖，不追加重影


def test_load_history_is_round_ordered(tmp_path):
    # 轮次按序落账（round 1 是每次尝试的开头，故按 1→2 顺序写）
    append_round(tmp_path, ep=2, round_no=1, problems=_s(("g1", "a", "x")), verdict="continue")
    append_round(tmp_path, ep=2, round_no=2, problems=_s(("g2", "b", "y")), verdict="continue")
    hist = load_history(tmp_path, ep=2)
    assert [next(iter(h)).kind for h in hist] == ["g1", "g2"]
    assert load_history(tmp_path, ep=9) == []


# ---------------------------------------------------------------- CLI（工作流侧唯一入口）

def _gate_report(path: Path, *, failed=(), warnings=()):
    """造 validate.mjs 形状的机读报告。"""
    gates = [{"id": "g1", "label": "台词密度", "ok": True, "detail": ""}]
    for label, detail in failed:
        gates.append({"id": "gX", "label": label, "ok": False, "detail": detail})
    path.write_text(json.dumps({"gates": gates, "hits": [], "warnings": list(warnings)}, ensure_ascii=False), encoding="utf-8")
    return path


def _run_cli(*args):
    return run_py(["-m", "dramacraft.breaker", *map(str, args)], cwd=str(ROOT))


def test_cli_pass_writes_report_and_bare_word(tmp_path):
    work = tmp_path / "w"
    rep = _gate_report(tmp_path / "gate.json")
    out = tmp_path / "brk.json"
    cp = _run_cli("--workdir", work, "--ep", 1, "--round", 1, "--gate-report", rep, "--json", out)
    assert cp.returncode == 0 and cp.stdout.strip() == "pass"
    payload = json.loads(out.read_text("utf-8"))
    assert payload["verdict"] == "pass" and payload["problemCount"] == 0


def test_cli_missing_json_path_is_usage_error(tmp_path):
    cp = _run_cli("--workdir", tmp_path, "--ep", 1, "--round", 1)
    assert cp.returncode == 1  # 用法错硬失败，绝不 exit0 静默


def test_cli_missing_gate_report_is_hard_error_no_report_written(tmp_path):
    out = tmp_path / "brk.json"
    cp = _run_cli("--workdir", tmp_path / "w", "--ep", 1, "--round", 1,
                  "--gate-report", tmp_path / "nope.json", "--json", out)
    assert cp.returncode == 1
    assert not out.exists()  # 输入缺失绝不落一份"看起来通过"的报告（审查 #2 假成功）
    assert "nope.json" in cp.stderr


def test_cli_unrecognized_report_shape_is_hard_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"whatever": 1}), encoding="utf-8")
    out = tmp_path / "brk.json"
    cp = _run_cli("--workdir", tmp_path / "w", "--ep", 1, "--round", 1, "--gate-report", bad, "--json", out)
    assert cp.returncode == 1 and not out.exists()


def test_cli_escalates_after_two_flat_rounds(tmp_path):
    work = tmp_path / "w"
    rep = _gate_report(tmp_path / "gate.json", failed=[("台词密度", "EP03-S2 台词超长；EP03-S4 台词超短")])
    r1, r2 = tmp_path / "b1.json", tmp_path / "b2.json"
    cp1 = _run_cli("--workdir", work, "--ep", 3, "--round", 1, "--gate-report", rep, "--json", r1)
    cp2 = _run_cli("--workdir", work, "--ep", 3, "--round", 2, "--gate-report", rep, "--json", r2)
    assert cp1.returncode == 0 and cp1.stdout.strip() == "continue"
    assert cp2.returncode == 2 and cp2.stdout.strip() == "escalate"
    payload = json.loads(r2.read_text("utf-8"))
    assert payload["verdict"] == "escalate" and payload["new"] == [] and len(payload["unresolved"]) >= 1


def test_cli_aborts_at_cap(tmp_path):
    work = tmp_path / "w"
    rep = _gate_report(tmp_path / "gate.json", failed=[("台词密度", "EP03-S2 台词超长")])
    _run_cli("--workdir", work, "--ep", 3, "--round", 1, "--gate-report", rep, "--json", tmp_path / "b1.json")
    out = tmp_path / "b2.json"
    cp = _run_cli("--workdir", work, "--ep", 3, "--round", 2, "--gate-report", rep, "--cap", 2, "--json", out)
    assert cp.returncode == 2 and cp.stdout.strip() == "abort"
    assert json.loads(out.read_text("utf-8"))["verdict"] == "abort"


def test_cli_merges_hard_list_shape(tmp_path):
    work = tmp_path / "w"
    lint = tmp_path / "plan.json"
    lint.write_text(json.dumps({"hard": ["H1 ep3 源场S2未被任何规划场映射"], "warn": ["W1 某处告警"]}, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "brk.json"
    cp = _run_cli("--workdir", work, "--ep", 3, "--round", 1, "--gate-report", lint, "--json", out)
    payload = json.loads(out.read_text("utf-8"))
    assert cp.returncode == 0 and payload["problemCount"] == 1  # 告警不入熔断


def test_cli_empty_gate_report_path_list_is_usage_error(tmp_path):
    # 一个门报告都不给 = 无法判定 → 用法错，绝不默认 pass
    out = tmp_path / "brk.json"
    cp = _run_cli("--workdir", tmp_path / "w", "--ep", 1, "--round", 1, "--json", out)
    assert cp.returncode == 1 and not out.exists()


def test_cli_history_is_disk_backed_across_processes(tmp_path):
    # 两个独立进程往返：第 2 轮的判定必须看得见第 1 轮（工作流每轮都是新进程）
    work = tmp_path / "w"
    rep = _gate_report(tmp_path / "gate.json", failed=[("台词密度", "EP03-S2 台词超长")])
    _run_cli("--workdir", work, "--ep", 1, "--round", 1, "--gate-report", rep, "--json", tmp_path / "b1.json")
    journal = work / "state" / "healing" / "ep001.json"
    assert journal.exists()
    hist = load_history(work, ep=1)
    assert len(hist) == 1 and len(hist[0]) == 2  # 门标签行 + 详情行
