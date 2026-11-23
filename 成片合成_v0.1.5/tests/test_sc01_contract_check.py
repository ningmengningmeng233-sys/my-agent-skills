# -*- coding: utf-8 -*-
"""SC-01:run 侧契约校验统一退出码 2 + 错误带定位；断点状态加载前验合法性。

受控 RED→GREEN：破损工单/脏断点必须明确报错（点名文件与字段/镜头位置）
并 exit 2（与 plan 侧 StoryboardError 同一口径），不得抛裸 traceback；
合法工单（含手写无 contract 标记）保持可跑。
"""
import json
from pathlib import Path

import pytest

from renderplan import contract as C
from renderplan.cli import main


def _write(path, obj):
    p = Path(path)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def _plan_file(tmp_path, storyboard_path, dubbing_path, asset_ledger_path):
    pf = tmp_path / "render_plan.json"
    assert main(["plan", "--storyboard", str(storyboard_path),
                 "--dubbing", str(dubbing_path),
                 "--asset-ledger", str(asset_ledger_path),
                 "--out", str(pf)]) == 0
    return pf


def test_run_broken_plan_missing_shots_exit_2_with_location(tmp_path, capsys):
    bad = _write(tmp_path / "bad_plan.json", {"contract": "storyboard", "source": "x"})
    rc = main(["run", "--plan", str(bad), "--stage", "video", "--out", str(tmp_path / "run")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "bad_plan.json" in err, f"报错必须点名工单文件：{err}"
    assert "shots" in err, f"报错必须点名缺失字段：{err}"


def test_run_storyboard_as_plan_hints_wrong_file(tmp_path, storyboard_path, capsys):
    rc = main(["run", "--plan", str(storyboard_path),
               "--stage", "video", "--out", str(tmp_path / "run")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "storyboard" in err.lower() or "分镜" in err, f"传错文件类型必须被点出：{err}"


def test_run_plan_shot_missing_id_exit_2_with_shot_location(tmp_path, capsys):
    bad = _write(tmp_path / "noid.json",
                 {"contract": "storyboard",
                  "shots": [{"shot_id": "E01-01-f1", "duration": 3.0},
                            {"duration": 2.0}]})
    rc = main(["run", "--plan", str(bad), "--stage", "video", "--out", str(tmp_path / "run")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "第2个镜头" in err, f"报错必须定位到镜头序号：{err}"
    assert "shot_id" in err


def test_run_plan_wrong_namespace_exit_2(tmp_path, capsys):
    bad = _write(tmp_path / "ns.json",
                 {"contract": "shooting-script",
                  "shots": [{"shot_id": "E01-01-f1", "duration": 3.0}]})
    rc = main(["run", "--plan", str(bad), "--stage", "video", "--out", str(tmp_path / "run")])
    assert rc == 2
    assert "storyboard" in capsys.readouterr().err


def test_run_corrupt_failed_items_exit_2_with_path(
        tmp_path, storyboard_path, dubbing_path, asset_ledger_path, capsys):
    pf = _plan_file(tmp_path, storyboard_path, dubbing_path, asset_ledger_path)
    run = tmp_path / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "failed_items.json").write_text("{corrupt json", encoding="utf-8")
    rc = main(["run", "--plan", str(pf), "--stage", "video", "--out", str(run)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "failed_items.json" in err, f"报错必须点名断点文件：{err}"


@pytest.mark.parametrize("payload", [{"a": 1}, [123], [{"stage": "video"}]])
def test_run_misshaped_failed_items_exit_2(
        tmp_path, storyboard_path, dubbing_path, asset_ledger_path, payload, capsys):
    pf = _plan_file(tmp_path, storyboard_path, dubbing_path, asset_ledger_path)
    run = tmp_path / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "failed_items.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    rc = main(["run", "--plan", str(pf), "--stage", "video", "--out", str(run)])
    assert rc == 2
    assert "failed_items.json" in capsys.readouterr().err


def test_run_handwritten_plan_without_contract_still_works(tmp_path):
    """无 contract 标记的手写工单：向后兼容，照常跑（只认结构不认标记）。"""
    ok = _write(tmp_path / "hand.json",
                {"shots": [{"shot_id": "E01-01-f1", "duration": 3.0}]})
    rc = main(["run", "--plan", str(ok), "--stage", "video", "--out", str(tmp_path / "run")])
    assert rc == 0
    assert list((tmp_path / "run" / "clips").glob("*.mp4"))


def test_validate_plan_unit():
    assert C.validate_plan({"shots": [{"shot_id": "a", "duration": 1}]}) == []
    assert C.validate_plan([]) != []
    assert any("shots" in p for p in C.validate_plan({"contract": "storyboard"}))
    with pytest.raises(C.ContractError):
        C.check_plan({"shots": []})


def test_load_failed_items_unit(tmp_path):
    assert C.load_failed_items(tmp_path / "nope.json") == {}
    good = _write(tmp_path / "f.json",
                  [{"shot_id": "E01-01-f1", "stage": "video", "error": "x"}])
    assert C.load_failed_items(good) == {"E01-01-f1": "x"}
    with pytest.raises(C.ContractError):
        C.load_failed_items(_write(tmp_path / "g.json", {"a": 1}))
