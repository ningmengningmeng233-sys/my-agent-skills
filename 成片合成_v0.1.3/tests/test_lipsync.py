"""行为 4：lip-sync stage 存在，默认 off，接管分镜技能让渡的口型决策；
off 时对白镜头走画外音+字幕降级，并在计划里记录原因。"""
import json

from renderplan.plan import build_plan, load_json
from renderplan.stages import run_lipsync_stage
from renderplan.cli import main


def test_lipsync_default_off_degrades_dialogue_shots(storyboard_dict, dubbing_path):
    plan = build_plan(storyboard_dict, dubbing=load_json(dubbing_path))
    res = run_lipsync_stage(plan, mode="off")
    assert res["mode"] == "off"
    by_id = {s["shot_id"]: s for s in plan["shots"]}
    # C02 是画内对白 → 降级；VO 本就是画外音，无需降级
    f2 = by_id["E01-01-f2"]
    assert f2["lip_sync"]["mode"] == "off"
    assert f2["lip_sync"]["degradation"] == "画外音 + 字幕"
    assert "口型" in f2["lip_sync"]["reason"] or "画外音" in f2["lip_sync"]["reason"]
    assert by_id["E01-01-f3"]["lip_sync"]["degradation"] is None  # VO 画外音不降级


def test_lipsync_owner_declared(storyboard_dict, dubbing_path):
    plan = build_plan(storyboard_dict, dubbing=load_json(dubbing_path))
    assert plan["lip_sync"]["owner"] == "成片合成"
    assert plan["lip_sync"]["default_mode"] == "off"


def test_lipsync_on_marks_enabled(storyboard_dict, dubbing_path, tmp_path):
    plan = build_plan(storyboard_dict, dubbing=load_json(dubbing_path))
    run_lipsync_stage(plan, mode="on", out_dir=tmp_path)
    f2 = next(s for s in plan["shots"] if s["shot_id"] == "E01-01-f2")
    assert f2["lip_sync"]["mode"] == "on"
    assert f2["lip_sync"]["degradation"] is None
    assert (tmp_path / "lip_sync.json").exists()


def test_lipsync_on_is_not_reported_green(storyboard_dict, dubbing_path, tmp_path):
    """on 模式没有接引擎：报 NOT_RUN、成功数为 0，绝不因「开了开关」就算绿。"""
    plan = build_plan(storyboard_dict, dubbing=load_json(dubbing_path))
    lp = tmp_path / ".lemon" / "run-ledger.jsonl"
    res = run_lipsync_stage(plan, mode="on", out_dir=tmp_path, ledger_path=lp)
    rec = json.loads(lp.read_text(encoding="utf-8").splitlines()[-1])
    assert res["status"] == "NOT_RUN" == rec["status"]
    assert rec["succeeded"] == 0


def test_cli_run_lipsync_stage(tmp_path, storyboard_path, dubbing_path):
    plan_file = tmp_path / "render_plan.json"
    main(["plan", "--storyboard", str(storyboard_path),
          "--dubbing", str(dubbing_path), "--out", str(plan_file)])
    rc = main(["run", "--plan", str(plan_file), "--stage", "lip-sync",
               "--out", str(tmp_path / "run")])
    assert rc == 0
    out = json.loads((tmp_path / "run" / "lip_sync.json").read_text(encoding="utf-8"))
    assert out["mode"] == "off"
