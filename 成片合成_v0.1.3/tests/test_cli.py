"""行为 2/7：CLI run --stage 三档独立可跑 + 退出码；网络层不可达。"""
import os

from renderplan.cli import main
from renderplan import adapters


def _plan_file(tmp_path, storyboard_path, dubbing_path, asset_ledger_path):
    pf = tmp_path / "render_plan.json"
    assert main(["plan", "--storyboard", str(storyboard_path),
                 "--dubbing", str(dubbing_path),
                 "--asset-ledger", str(asset_ledger_path),
                 "--out", str(pf)]) == 0
    return pf


def test_cli_video_stage_default_stub(tmp_path, storyboard_path, dubbing_path, asset_ledger_path):
    pf = _plan_file(tmp_path, storyboard_path, dubbing_path, asset_ledger_path)
    run = tmp_path / "run"
    assert main(["run", "--plan", str(pf), "--stage", "video", "--out", str(run)]) == 0
    assert len(list((run / "clips").glob("*.mp4"))) == 3
    assert (run / "failed_items.json").exists()
    assert (tmp_path / ".lemon" / "run-ledger.jsonl").exists()


def test_cli_assemble_then_full_pipeline_order(tmp_path, storyboard_path, dubbing_path, asset_ledger_path):
    pf = _plan_file(tmp_path, storyboard_path, dubbing_path, asset_ledger_path)
    run = tmp_path / "run"
    for stage in ("video", "tts", "assemble"):
        assert main(["run", "--plan", str(pf), "--stage", stage, "--out", str(run)]) == 0
    assert (run / "assemble.json").exists()


def test_cli_bad_storyboard_exit_code_2(bad_storyboard_path, tmp_path):
    rc = main(["plan", "--storyboard", str(bad_storyboard_path),
               "--out", str(tmp_path / "p.json")])
    assert rc == 2


def test_http_layer_never_built_without_credentials(monkeypatch):
    # 真实 HTTP 适配器在无凭据（全 REPLACE 占位）时构造即拒绝，测试里不可达
    monkeypatch.delenv("RENDER_ENDPOINT", raising=False)
    for k in list(os.environ):
        if k.startswith("RENDER_KEY"):
            monkeypatch.delenv(k, raising=False)
    try:
        adapters.HttpVideoAdapter()
        assert False, "占位凭据下不得构造真实适配器"
    except RuntimeError as e:
        assert "凭据" in str(e) or "REPLACE" in str(e)
