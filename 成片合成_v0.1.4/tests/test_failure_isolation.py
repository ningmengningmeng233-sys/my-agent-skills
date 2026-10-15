"""行为 3：失败隔离——3 镜中第 2 镜失败 → 2 成功 + failed_items.json 仅列第 2 镜；
--retry-failed 只重跑失败镜头，不动成功镜头（用桩调用次数断言）。"""
import json
from pathlib import Path

from renderplan.plan import build_plan, load_json
from renderplan.stages import run_video_stage


class _ControlledVideoAdapter:
    def __init__(self, fail_ids=()):
        self.calls = []
        self.fail_ids = set(fail_ids)

    def generate(self, shot):
        self.calls.append(shot["shot_id"])
        if shot["shot_id"] in self.fail_ids:
            raise RuntimeError("模拟视频渲染失败")
        p = Path(shot["_clip_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"stub-clip")
        return str(p)


def _plan(storyboard_dict, dubbing_path, asset_ledger_path):
    return build_plan(storyboard_dict, dubbing=load_json(dubbing_path),
                      asset_ledger=load_json(asset_ledger_path))


def test_failure_isolation_and_retry_only_failed(storyboard_dict, dubbing_path, asset_ledger_path, tmp_path):
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)

    first = _ControlledVideoAdapter(fail_ids={"E01-01-f2"})
    res = run_video_stage(plan, tmp_path, first)
    assert first.calls == ["E01-01-f1", "E01-01-f2", "E01-01-f3"]
    assert res["succeeded"] == 2
    assert res["failed"] == 1
    assert sorted((tmp_path / "clips").glob("*.mp4")) == [
        tmp_path / "clips" / "E01-01-f1.mp4", tmp_path / "clips" / "E01-01-f3.mp4"]

    failed = json.loads((tmp_path / "failed_items.json").read_text(encoding="utf-8"))
    assert [f["shot_id"] for f in failed] == ["E01-01-f2"]

    # retry-failed：只重跑第 2 镜，成功镜头的桩一次都不再被调用
    retry = _ControlledVideoAdapter(fail_ids=())
    res2 = run_video_stage(plan, tmp_path, retry, retry_failed=True)
    assert retry.calls == ["E01-01-f2"]
    assert res2["succeeded"] == 1
    assert len(list((tmp_path / "clips").glob("*.mp4"))) == 3
    assert json.loads((tmp_path / "failed_items.json").read_text(encoding="utf-8")) == []


def test_failed_items_records_reason(tmp_path, storyboard_dict, dubbing_path, asset_ledger_path):
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)
    run_video_stage(plan, tmp_path, _ControlledVideoAdapter(fail_ids={"E01-01-f2"}))
    failed = json.loads((tmp_path / "failed_items.json").read_text(encoding="utf-8"))
    assert failed[0]["error"]  # 记录失败原因
    assert failed[0]["stage"] == "video"
