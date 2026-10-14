# -*- coding: utf-8 -*-
"""DR-06 回归：批处理退出码解耦。

- exit 2 仅用于参数/大纲格式硬性预检失败（输入违规拒跑）；
- 预检通过、但生产/回炉阶段存在部分集未收敛时，返回 exit 3（部分未完成，可续跑）。

受控 RED：前两条断言在修复前必须失败（返回 2），修复后通过。
全 mock/stub，不调真实模型/网络。
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from conftest import run_py  # noqa: E402
from dramacraft.self_healing import HealingBlocked  # noqa: E402
from dramacraft.ledger import load_events  # noqa: E402
from dramacraft.outline_lint import compute_outline_hash  # noqa: E402

OUTLINE = os.path.join(ROOT, "fixtures", "sample_outline.json")


def _sample_ep(ep_num):
    sample = json.loads(open(os.path.join(ROOT, "fixtures", "sample_script.json"),
                             encoding="utf-8").read())
    return json.loads(json.dumps(next(e for e in sample["episodes"]
                                      if e.get("ep") == ep_num)))


def test_partial_unconverged_returns_exit_3_not_2(tmp_path, monkeypatch, capsys):
    """回炉未收敛（abort 熔断）：exit 3 + 未交付集点名 + 其余集照常缓存。"""
    import run_batch

    outline_path = tmp_path / "outline.json"
    outline_path.write_text(open(OUTLINE, encoding="utf-8").read(), encoding="utf-8")

    class _GW:
        def __init__(self, *a, **kw):
            self.nodes = [type("N", (), {"name": "fake-node"})()]
            self.call_count = 0
            self.max_calls = None

    def _fake_gen(*, ep_meta, gateway, **kw):
        if ep_meta.get("ep") == 2:
            raise HealingBlocked(ep=2, verdict="abort",
                                 detail="达到回炉硬顶（合成故障）", rounds=3)
        return _sample_ep(ep_meta["ep"])

    monkeypatch.setattr(run_batch, "LLMGateway", _GW)
    monkeypatch.setattr(run_batch, "generate_episode_with_healing", _fake_gen)
    monkeypatch.setattr(sys, "argv", [
        "run_batch.py", "--outline", str(outline_path), "--target-seconds", "90",
        "--workdir", str(tmp_path / "ws"), "--concurrency", "1",
    ])
    with pytest.raises(SystemExit) as ei:
        run_batch.main()
    assert ei.value.code == 3, "部分集未收敛必须 exit 3，不得再与预检违规共用 exit 2"
    out = capsys.readouterr().out
    assert "第 2 集" in out, "未交付集必须点名"

    ep_dir = tmp_path / "ws" / "state" / "episodes"
    assert (ep_dir / "ep001.json").exists() and (ep_dir / "ep003.json").exists()
    assert not (ep_dir / "ep002.json").exists(), "受阻集绝不落盘当成品"
    events = load_events(str(tmp_path / "ws" / "state" / "ledger.jsonl"))
    assert [e.get("status") for e in events
            if e.get("event") == "run_finish"] == ["partial"]


def test_missing_episodes_returns_exit_3(tmp_path):
    """缺集防空：大纲规划 3 集、库里只有 2 集 → exit 3 且不出交付物。"""
    outline = json.loads(open(OUTLINE, encoding="utf-8").read())
    ws = tmp_path / "ws"
    ep_dir = ws / "state" / "episodes"
    ep_dir.mkdir(parents=True, exist_ok=True)
    for ep_n in (1, 3):
        payload = _sample_ep(ep_n)
        payload["_meta"] = {"engineVersion": "1.2.1", "schemaVersion": 2,
                            "outlineHash": compute_outline_hash(outline),
                            "gatesPassedAt": "2026-09-22T00:00:00+08:00"}
        (ep_dir / f"ep{ep_n:03d}.json").write_text(json.dumps(payload, ensure_ascii=False),
                                                  encoding="utf-8")
    res = run_py([os.path.join(ROOT, "run_batch.py"), "--outline", OUTLINE,
                  "--target-seconds", "90", "--workdir", str(ws), "--assemble-only"],
                 cwd=ROOT)
    assert res.returncode == 3, res.stdout + res.stderr
    assert "第 2 集" in res.stdout + res.stderr
    assert not (ws / "output" / "script.json").exists(), "缺集不得出交付物"


def test_precheck_violation_stays_exit_2(tmp_path):
    """预检半边：大纲 O 门违规仍 exit 2（输入违规拒跑）。"""
    outline = json.loads(open(OUTLINE, encoding="utf-8").read())
    outline["characters"][0]["desire"] = ""
    p = tmp_path / "dirty_outline.json"
    p.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    res = run_py([os.path.join(ROOT, "run_batch.py"), "--outline", str(p),
                  "--target-seconds", "90", "--workdir", str(tmp_path / "ws"),
                  "--assemble-only"], cwd=ROOT)
    assert res.returncode == 2, res.stdout + res.stderr
