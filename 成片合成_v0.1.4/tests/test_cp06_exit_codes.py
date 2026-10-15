# -*- coding: utf-8 -*-
"""CP-06: renderplan run 按阶段状态返回 0/1/2，失败不再静默 exit 0。"""
import json
from pathlib import Path

from renderplan import cli as CLI
from renderplan.cli import main
from renderplan.adapters import StubTtsAdapter


class _FailingVideoAdapter:
    def __init__(self, fail_ids):
        self.fail_ids = set(fail_ids)

    def generate(self, shot):
        if shot["shot_id"] in self.fail_ids:
            raise RuntimeError("受控失败 " + shot["shot_id"])
        p = Path(shot["_clip_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"stub-clip")
        return str(p)


def _plan_file(tmp_path, storyboard_path, dubbing_path, asset_ledger_path):
    pf = tmp_path / "render_plan.json"
    assert main(["plan", "--storyboard", str(storyboard_path),
                 "--dubbing", str(dubbing_path),
                 "--asset-ledger", str(asset_ledger_path),
                 "--out", str(pf)]) == 0
    return pf


def test_run_video_failed_returns_2_and_keeps_details(
        monkeypatch, tmp_path, storyboard_path, dubbing_path, asset_ledger_path):
    """video 全失败 → exit 2，且 failed_items.json 保留失败详情。"""
    pf = _plan_file(tmp_path, storyboard_path, dubbing_path, asset_ledger_path)
    run = tmp_path / "run"
    monkeypatch.setattr(CLI, "_adapters",
                        lambda: (_FailingVideoAdapter(
                            {"E01-01-f1", "E01-01-f2", "E01-01-f3"}),
                            StubTtsAdapter()))
    rc = main(["run", "--plan", str(pf), "--stage", "video", "--out", str(run)])
    assert rc == 2
    failed = json.loads((run / "failed_items.json").read_text(encoding="utf-8"))
    assert len(failed) == 3 and all(f["error"] for f in failed)


def test_run_video_partial_returns_1(
        monkeypatch, tmp_path, storyboard_path, dubbing_path, asset_ledger_path):
    """video 部分失败 → exit 1（混合失败同样非零）。"""
    pf = _plan_file(tmp_path, storyboard_path, dubbing_path, asset_ledger_path)
    run = tmp_path / "run"
    monkeypatch.setattr(CLI, "_adapters",
                        lambda: (_FailingVideoAdapter({"E01-01-f2"}),
                                 StubTtsAdapter()))
    rc = main(["run", "--plan", str(pf), "--stage", "video", "--out", str(run)])
    assert rc == 1
    failed = json.loads((run / "failed_items.json").read_text(encoding="utf-8"))
    assert [f["shot_id"] for f in failed] == ["E01-01-f2"]


def test_run_assemble_not_run_returns_1(
        tmp_path, storyboard_path, dubbing_path, asset_ledger_path):
    """assemble 无素材 → NOT_RUN → exit 1。"""
    pf = _plan_file(tmp_path, storyboard_path, dubbing_path, asset_ledger_path)
    rc = main(["run", "--plan", str(pf), "--stage", "assemble",
               "--out", str(tmp_path / "run")])
    assert rc == 1


def test_run_lipsync_skipped_returns_1(tmp_path, storyboard_path, dubbing_path):
    """lip-sync 默认 off → SKIPPED → exit 1。"""
    pf = tmp_path / "render_plan.json"
    assert main(["plan", "--storyboard", str(storyboard_path),
                 "--dubbing", str(dubbing_path), "--out", str(pf)]) == 0
    rc = main(["run", "--plan", str(pf), "--stage", "lip-sync",
               "--out", str(tmp_path / "run")])
    assert rc == 1


def test_run_video_succeeded_returns_0(
        tmp_path, storyboard_path, dubbing_path, asset_ledger_path):
    """video 全绿 → exit 0（不误伤成功路径）。"""
    pf = _plan_file(tmp_path, storyboard_path, dubbing_path, asset_ledger_path)
    rc = main(["run", "--plan", str(pf), "--stage", "video",
               "--out", str(tmp_path / "run")])
    assert rc == 0
