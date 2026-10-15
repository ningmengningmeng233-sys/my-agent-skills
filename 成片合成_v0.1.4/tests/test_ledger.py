"""行为 5：每个 stage 向 .lemon/run-ledger.jsonl 追加一条运行台账记录。
台账字段以 `schemas/run-ledger.schema.json`（C8 跨技能统一台账）为准，
由 test_record_satisfies_unified_run_ledger_schema 直接吃统一内核 validate() 校验。"""
import json

from renderplan.plan import build_plan, load_json
from renderplan import ledger as L
from renderplan.stages import run_video_stage, run_tts_stage, run_assemble_stage, run_lipsync_stage
from renderplan.adapters import StubVideoAdapter, StubTtsAdapter

LEDGER_FIELDS = {"schema_version", "ts", "skill", "stage", "run_id",
                 "status", "succeeded", "failed", "skipped", "artifacts", "note"}


def test_record_has_contract_fields():
    rec = L.make_record(stage="video", status="SUCCEEDED", succeeded=2, failed=1,
                        skipped=0, artifacts=["a.mp4"], note="x", run_id="r1")
    assert LEDGER_FIELDS <= set(rec)
    assert rec["skill"] == "成片合成"
    assert rec["schema_version"] == 1


def test_every_stage_writes_one_record(storyboard_dict, dubbing_path, asset_ledger_path, tmp_path):
    plan = build_plan(storyboard_dict, dubbing=load_json(dubbing_path),
                      asset_ledger=load_json(asset_ledger_path))
    lp = tmp_path / ".lemon" / "run-ledger.jsonl"
    run_video_stage(plan, tmp_path, StubVideoAdapter(), ledger_path=lp, run_id="run-x")
    run_tts_stage(plan, tmp_path, StubTtsAdapter(), ledger_path=lp, run_id="run-x")
    run_assemble_stage(plan, tmp_path, ledger_path=lp, run_id="run-x")
    run_lipsync_stage(plan, mode="off", out_dir=tmp_path, ledger_path=lp, run_id="run-x")

    lines = [json.loads(x) for x in lp.read_text(encoding="utf-8").splitlines() if x.strip()]
    stages = [r["stage"] for r in lines]
    assert stages == ["video", "tts", "assemble", "lip-sync"]
    for r in lines:
        assert LEDGER_FIELDS <= set(r)
        assert r["run_id"] == "run-x"


def test_ledger_records_blocked_cred_status(tmp_path):
    lp = tmp_path / "run-ledger.jsonl"
    L.append(L.make_record(stage="video", status="BLOCKED-CRED", succeeded=0, failed=0,
                           skipped=0, artifacts=[], note="凭据为 REPLACE 占位，真实渲染未跑",
                           run_id="r"), ledger_path=lp)
    rec = json.loads(lp.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["status"] == "BLOCKED-CRED"


def test_skipped_is_not_reported_green(tmp_path, storyboard_dict, dubbing_path):
    plan = build_plan(storyboard_dict, dubbing=load_json(dubbing_path))
    lp = tmp_path / ".lemon" / "run-ledger.jsonl"
    # assemble 无视频素材时应记 SKIPPED/NOT_RUN，不得记 SUCCEEDED
    run_assemble_stage(plan, tmp_path, ledger_path=lp)
    rec = json.loads(lp.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["status"] in {"SKIPPED", "NOT_RUN", "PARTIAL"}
    assert rec["status"] != "SUCCEEDED"


def _unified_validator():
    """吃 schemas/ledger.py 的 validate()；找不到内核就直接失败，不许静默跳过。"""
    import importlib.util
    from renderplan.plan import UNIFIED_LEDGER_PY as kernel
    assert kernel.is_file(), f"缺统一台账内核: {kernel}"
    spec = importlib.util.spec_from_file_location("_run_ledger_kernel", kernel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.validate


def test_record_satisfies_unified_run_ledger_schema():
    """台账必须能被 C8 统一内核收下——本技能此前自称对齐、实则缺 10 个必填字段。"""
    validate = _unified_validator()
    rec = L.make_record(stage="video", status="SUCCEEDED", succeeded=2, failed=1,
                        skipped=0, artifacts=["run/clips/E01-01-f2.mp4"],
                        note="x", run_id="r1")
    assert validate(rec) == [], "统一台账拒绝：" + "；".join(validate(rec))


def test_every_stage_record_passes_unified_schema(storyboard_dict, dubbing_path,
                                                  asset_ledger_path, tmp_path):
    validate = _unified_validator()
    plan = build_plan(storyboard_dict, dubbing=load_json(dubbing_path),
                      asset_ledger=load_json(asset_ledger_path))
    lp = tmp_path / ".lemon" / "run-ledger.jsonl"
    run_video_stage(plan, tmp_path, StubVideoAdapter(), ledger_path=lp, run_id="run-y")
    run_tts_stage(plan, tmp_path, StubTtsAdapter(), ledger_path=lp, run_id="run-y")
    run_assemble_stage(plan, tmp_path, ledger_path=lp, run_id="run-y")
    run_lipsync_stage(plan, mode="off", out_dir=tmp_path, ledger_path=lp, run_id="run-y")
    lines = [json.loads(x) for x in lp.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 4
    for rec in lines:
        assert validate(rec) == [], f"{rec['stage']} 台账被统一内核拒绝"


def test_lipsync_does_not_count_degradations_as_success(tmp_path, storyboard_dict,
                                                        dubbing_path):
    """降级不是成功：off 模式下口型同步一镜都没做，succeeded 必须是 0。

    旧实现把 `succeeded=len(degraded)` 写进台账——降级越多，账上「成功」越多，
    而 SKIPPED≠绿 是整套技能的红线；这条账会让任何下游汇总把缺陷读成产能。
    """
    plan = build_plan(storyboard_dict, dubbing=load_json(dubbing_path))
    lp = tmp_path / ".lemon" / "run-ledger.jsonl"
    res = run_lipsync_stage(plan, mode="off", out_dir=tmp_path, ledger_path=lp)
    rec = json.loads(lp.read_text(encoding="utf-8").splitlines()[-1])
    assert res["degraded"], "夹具里应有画内对白镜，否则这条测不出问题"
    assert rec["succeeded"] == 0
    assert rec["status"] == "SKIPPED"
    assert f"降级 {len(res['degraded'])}" in rec["note"]


def test_default_ledger_path_does_not_double_nest_lemon(tmp_path):
    """文档命令 `--out .lemon/run` 曾把台账写到 .lemon/.lemon/run-ledger.jsonl。"""
    from renderplan.cli import _default_ledger
    got = _default_ledger(tmp_path / ".lemon" / "run")
    assert got == tmp_path / ".lemon" / "run-ledger.jsonl"
    other = _default_ledger(tmp_path / "build" / "run")
    assert other == tmp_path / "build" / ".lemon" / "run-ledger.jsonl"

