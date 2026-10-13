"""行为 1：plan 命令——逐镜工单产出 + 非法分镜拒绝（exit 2、纯中文、无 traceback）。"""
import json
from pathlib import Path

from conftest import run_py
from renderplan.plan import build_plan, load_json, StoryboardError


def test_build_plan_shot_fields(storyboard_dict, dubbing_path, asset_ledger_path):
    dub = load_json(dubbing_path)
    ledger = load_json(asset_ledger_path)
    plan = build_plan(storyboard_dict, dubbing=dub, asset_ledger=ledger)
    shots = plan["shots"]
    assert len(shots) == 3
    ids = [s["shot_id"] for s in shots]
    assert ids == ["E01-01-f1", "E01-01-f2", "E01-01-f3"]
    s1 = shots[0]
    # 契约字段：镜号 / 提示词引用 / 素材引用 / 时长 / 台词语
    assert s1["prompt_ref"] == {"segment_id": "E01-01", "field": "h3Prompt"}
    assert s1["duration"] == 3
    assert s1["dialogue"] is True
    assert s1["speaker"] == "C01"
    # 素材引用含人物 C 编号且映射到台账 asset_id
    assert "C01" in s1["asset_refs"]["characters"]
    assert s1["asset_refs"]["ledger_asset_ids"]


def test_dialogue_flag_and_planned_seconds_from_dubbing(storyboard_dict, dubbing_path):
    dub = load_json(dubbing_path)
    plan = build_plan(storyboard_dict, dubbing=dub)
    by_id = {s["shot_id"]: s for s in plan["shots"]}
    assert by_id["E01-01-f2"]["dialogue_planned_seconds"] == 3.5
    assert by_id["E01-01-f3"]["speaker"] == "VO"


def test_reject_nonconforming_storyboard(bad_storyboard_path, tmp_path, capsys):
    out = tmp_path / "render_plan.json"
    rc = run_py(
        ["-m", "renderplan", "plan",
         "--storyboard", str(bad_storyboard_path), "--out", str(out)],
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert rc.returncode == 2
    assert "Traceback" not in rc.stderr
    assert "分镜" in rc.stderr or "不合" in rc.stderr  # 纯中文错误
    assert not out.exists()


def test_reject_inproc_raises_plain_message():
    bad = {"source": "x", "episodes": [{"ep": 1, "segments": [{"id": "bad", "sceneIndex": 1, "cuts": []}]}]}
    try:
        build_plan(bad)
        assert False, "应抛 StoryboardError"
    except StoryboardError as e:
        msg = str(e)
        assert msg and "Traceback" not in msg  # 纯中文消息，无 traceback
        assert "段号" in msg or "cuts" in msg


def test_plan_cli_writes_file(storyboard_path, dubbing_path, asset_ledger_path, tmp_path):
    out = tmp_path / "render_plan.json"
    rc = run_py(
        ["-m", "renderplan", "plan",
         "--storyboard", str(storyboard_path),
         "--dubbing", str(dubbing_path),
         "--asset-ledger", str(asset_ledger_path),
         "--out", str(out)],
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert rc.returncode == 0, rc.stderr
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert len(plan["shots"]) == 3
