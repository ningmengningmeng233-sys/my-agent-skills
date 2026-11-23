"""行为 2：三个 stage 各自可独立运行且幂等。"""
import json
from pathlib import Path

from renderplan.plan import build_plan, load_json
from renderplan.stages import run_video_stage, run_tts_stage, run_assemble_stage


class _CountingVideoAdapter:
    def __init__(self):
        self.calls = []

    def generate(self, shot):
        self.calls.append(shot["shot_id"])
        p = Path(shot["_clip_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"stub-clip")
        return str(p)


class _DurationTtsAdapter:
    """按每个 shot 计划秒数 + 固定 delta 产出音频，可制造超限。"""
    def __init__(self, delta):
        self.delta = delta
        self.calls = []

    def synthesize(self, shot):
        self.calls.append(shot["shot_id"])
        planned = shot.get("dialogue_planned_seconds") or shot["duration"]
        p = Path(shot["_audio_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"stub-audio")
        return {"path": str(p), "duration_seconds": round(planned + self.delta, 3)}


def _plan(storyboard_dict, dubbing_path, asset_ledger_path):
    return build_plan(
        storyboard_dict,
        dubbing=load_json(dubbing_path),
        asset_ledger=load_json(asset_ledger_path),
    )


def test_video_stage_produces_clips_and_is_idempotent(storyboard_dict, dubbing_path, asset_ledger_path, tmp_path):
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)
    a1 = _CountingVideoAdapter()
    run_video_stage(plan, tmp_path, a1)
    clips = list((tmp_path / "clips").glob("*.mp4"))
    assert len(clips) == 3
    assert sorted(a1.calls) == ["E01-01-f1", "E01-01-f2", "E01-01-f3"]

    # 幂等：再跑一次不重复调用已完成镜头
    a2 = _CountingVideoAdapter()
    run_video_stage(plan, tmp_path, a2)
    assert a2.calls == []


def test_tts_stage_warns_on_duration_drift(storyboard_dict, dubbing_path, asset_ledger_path, tmp_path):
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)
    ad = _DurationTtsAdapter(delta=0.9)  # 超过默认阈值 0.35
    res = run_tts_stage(plan, tmp_path, ad, threshold=0.35)
    assert res["warnings"], "应产生时长漂移告警"
    assert any("画外音" in w["degradation"] and "字幕" in w["degradation"] for w in res["warnings"])
    out = json.loads((tmp_path / "tts_result.json").read_text(encoding="utf-8"))
    assert out["warnings"]


def test_tts_stage_within_threshold_no_warning(storyboard_dict, dubbing_path, asset_ledger_path, tmp_path):
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)
    ad = _DurationTtsAdapter(delta=0.05)
    res = run_tts_stage(plan, tmp_path, ad, threshold=0.35)
    assert res["warnings"] == []


def test_assemble_stage_falls_back_without_jianying(storyboard_dict, dubbing_path, asset_ledger_path, tmp_path):
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)
    res = run_assemble_stage(plan, tmp_path)
    assert res["jianying"] == "NOT_RUN"
    # 措辞改了（要带下一步），这里只钉语义：未验证 + 点名缺哪个运行环境
    assert "剪映导出未验证" in res["note"] and "jianying-editor" in res["note"]
    draft_dir = tmp_path / "draft"
    assert draft_dir.is_dir()
    assert (draft_dir / "draft_desc.json").exists()
    a = json.loads((tmp_path / "assemble.json").read_text(encoding="utf-8"))
    assert a["jianying_export"] == "未验证"
    assert len(a["tracks"]["video"]) == 3
