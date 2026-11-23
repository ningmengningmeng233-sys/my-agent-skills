# -*- coding: utf-8 -*-
"""CP-05: assemble 必须读取 out_dir/lip_sync.json 落盘降级台账中的 degraded 镜头，
将其字幕轨置为画外音字幕——降级决策须跨独立 CLI 进程如实传递。

反例（修前）：降级只改内存 plan 对象的 degradation_note；独立进程 assemble
重读工单文件，永远看不到降级，degraded 镜头被误记为对白字幕。
"""
import json
from pathlib import Path

import renderplan.stages as st
from renderplan.plan import build_plan, load_json


def _fresh_plan(storyboard_dict, dubbing_path, asset_ledger_path):
    """模拟独立进程：每次重建工单，不带任何内存降级标记。"""
    plan = build_plan(storyboard_dict, dubbing=load_json(dubbing_path),
                      asset_ledger=load_json(asset_ledger_path))
    return json.loads(json.dumps(plan, ensure_ascii=False))


def _subtitle_kinds(out_dir):
    asm = json.loads((Path(out_dir) / "assemble.json").read_text(encoding="utf-8"))
    return {t["shot_id"]: t["kind"] for t in asm["tracks"]["subtitle"]}


def test_assemble_honors_lipsync_degraded_ledger_across_processes(
        tmp_path, storyboard_dict, dubbing_path, asset_ledger_path):
    """跨进程：lip-sync 落盘 degraded 台账后，另起全新工单跑 assemble，
    degraded 镜头必须输出画外音字幕而非对白字幕。"""
    first = _fresh_plan(storyboard_dict, dubbing_path, asset_ledger_path)
    lip = st.run_lipsync_stage(first, mode="off", out_dir=tmp_path)
    assert lip["degraded"], "夹具应至少降级一个画内对白镜头"
    assert (tmp_path / "lip_sync.json").exists()

    # 独立进程：工单重读，不带内存 degradation_note
    second = _fresh_plan(storyboard_dict, dubbing_path, asset_ledger_path)
    assert not any(s.get("degradation_note") for s in second["shots"])
    st.run_assemble_stage(second, tmp_path)

    kinds = _subtitle_kinds(tmp_path)
    for shot_id in lip["degraded"]:
        assert kinds[shot_id] == "画外音字幕", \
            f"degraded 镜头 {shot_id} 应为画外音字幕，实为 {kinds[shot_id]!r}：{kinds!r}"


def test_assemble_keeps_dialogue_subtitles_without_ledger(
        tmp_path, storyboard_dict, dubbing_path, asset_ledger_path):
    """无 lip_sync.json 时保持原行为：对白镜头记对白字幕（回归守卫）。"""
    plan = _fresh_plan(storyboard_dict, dubbing_path, asset_ledger_path)
    st.run_assemble_stage(plan, tmp_path)
    kinds = _subtitle_kinds(tmp_path)
    assert kinds, "应有字幕轨"
    assert set(kinds.values()) == {"对白字幕"}, f"无降级台账时应全为对白字幕：{kinds!r}"


def test_assemble_tolerates_corrupt_lipsync_ledger(
        tmp_path, storyboard_dict, dubbing_path, asset_ledger_path):
    """lip_sync.json 损坏/格式异常 → 不崩，按无降级处理（对白字幕）。"""
    plan = _fresh_plan(storyboard_dict, dubbing_path, asset_ledger_path)
    (tmp_path / "lip_sync.json").write_text("{broken json", encoding="utf-8")
    st.run_assemble_stage(plan, tmp_path)
    kinds = _subtitle_kinds(tmp_path)
    assert set(kinds.values()) == {"对白字幕"}, f"损坏台账不得崩、不得误降级：{kinds!r}"
