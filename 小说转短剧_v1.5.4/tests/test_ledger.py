import json
import os
import subprocess

from dramacraft.ledger import Ledger, load_events
from conftest import run_py

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_emit_appends_valid_jsonl(tmp_path):
    lg = Ledger(tmp_path / "state" / "ledger.jsonl")
    lg.emit("run_start", episodes_total=3, cached=0)
    lg.emit("episode_cached", ep=1, path="state/episodes/ep001.json")

    events = load_events(lg.path)
    assert len(events) == 2
    assert events[0]["event"] == "run_start" and events[0]["episodes_total"] == 3
    assert events[1]["event"] == "episode_cached" and events[1]["ep"] == 1
    assert all("ts" in e for e in events)


def test_append_across_sessions_never_overwrites(tmp_path):
    p = tmp_path / "ledger.jsonl"
    Ledger(p).emit("run_start", episodes_total=1)
    Ledger(p).emit("run_start", episodes_total=1)  # 第二轮重跑续记
    assert len(load_events(p)) == 2


def _seed_episodes(episodes_dir):
    episodes_dir.mkdir(parents=True)
    sample = json.load(open(os.path.join(ROOT, "fixtures", "sample_script.json"), encoding="utf-8"))
    outline = json.load(open(os.path.join(ROOT, "fixtures", "sample_outline.json"), encoding="utf-8"))
    from dramacraft.outline_lint import compute_outline_hash
    h = compute_outline_hash(outline)
    for ep in sample["episodes"]:
        ep_c = dict(ep)
        ep_c["_meta"] = {"engineVersion": "1.2.0", "schemaVersion": 2, "outlineHash": h}
        with open(episodes_dir / f"ep{ep['ep']:03d}.json", "w", encoding="utf-8") as fp:
            json.dump(ep_c, fp, ensure_ascii=False)


def test_run_batch_writes_ledger_lifecycle(tmp_path):
    workdir = tmp_path / "ws"
    _seed_episodes(workdir / "state" / "episodes")
    res = run_py(
        ["run_batch.py",
         "--outline", os.path.join("fixtures", "sample_outline.json"),
         "--target-seconds", "90",
         "--workdir", str(workdir), "--assemble-only"],
        cwd=ROOT
    )
    assert res.returncode == 0, res.stdout + res.stderr

    events = load_events(workdir / "state" / "ledger.jsonl")
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_start"
    start = events[0]
    assert start["episodes_total"] == 3 and start["cached"] == 3 and start["target_seconds"] == 90
    last = events[-1]
    assert last["event"] == "run_finish" and last["status"] == "passed"
