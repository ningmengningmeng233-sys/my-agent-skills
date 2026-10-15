# -*- coding: utf-8 -*-
"""CP-03: assemble 必须真正调用 jy 导出并核验草稿产物文件存在，才可记 EXPORTED。

反例（修前）：只要 jy_wrapper 可 import 就谎报"已调用剪映生成草稿"、
记 EXPORTED / SUCCEEDED，全程没调用导出、没校验产物。
"""
import json
import sys
import types
from pathlib import Path

import renderplan.stages as st
from renderplan.plan import build_plan, load_json


def _plan(storyboard_dict, dubbing_path, asset_ledger_path):
    return build_plan(storyboard_dict, dubbing=load_json(dubbing_path),
                      asset_ledger=load_json(asset_ledger_path))


def _place_clips(out_dir, plan):
    clips = Path(out_dir) / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    for s in plan["shots"]:
        (clips / f"{s['shot_id']}.mp4").write_bytes(
            b"STUB-CLIP::" + s["shot_id"].encode("utf-8"))
    return clips


def _assemble_subtitle_export(out_dir):
    return json.loads((Path(out_dir) / "assemble.json").read_text(encoding="utf-8"))


def test_stub_jy_without_export_entry_is_never_exported(
        monkeypatch, tmp_path, storyboard_dict, dubbing_path, asset_ledger_path):
    """桩 jy 模块无导出入口（只可 import）→ 未实际调用，不得记 EXPORTED/SUCCEEDED。"""
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)
    _place_clips(tmp_path, plan)
    monkeypatch.setattr(st, "_jianying_module", lambda: types.SimpleNamespace())
    res = st.run_assemble_stage(plan, tmp_path)
    assert res["jianying"] != "EXPORTED", f"未调用导出却记 EXPORTED：{res!r}"
    assert res["status"] != "SUCCEEDED", f"未核验产物却记 SUCCEEDED：{res!r}"
    assert res["jianying"] == "NOT_RUN"
    assert _assemble_subtitle_export(tmp_path)["jianying_export"] == "未验证"


def test_stub_jy_export_without_artifact_is_never_exported(
        monkeypatch, tmp_path, storyboard_dict, dubbing_path, asset_ledger_path):
    """桩 jy 导出入口被调用但未产出草稿文件 → 不得记 EXPORTED/SUCCEEDED。"""
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)
    _place_clips(tmp_path, plan)
    calls = []

    def fake_export_draft(payload, draft_dir):
        calls.append((payload, draft_dir))

    stub = types.ModuleType("jy_wrapper")
    stub.export_draft = fake_export_draft
    monkeypatch.setattr(st, "_jianying_module", lambda: stub)
    res = st.run_assemble_stage(plan, tmp_path)
    assert calls, "应真正调用 jy_wrapper 的实际导出方法"
    assert res["jianying"] != "EXPORTED", f"无产物却记 EXPORTED：{res!r}"
    assert res["status"] != "SUCCEEDED", f"无产物却记 SUCCEEDED：{res!r}"
    assert res["jianying"] == "NOT_RUN"
    assert res["status"] == "PARTIAL"  # 有素材、无验证导出 → PARTIAL
    assert _assemble_subtitle_export(tmp_path)["jianying_export"] == "未验证"


def test_stub_jy_export_failure_is_never_exported(
        monkeypatch, tmp_path, storyboard_dict, dubbing_path, asset_ledger_path):
    """桩 jy 导出抛错 → 不得崩、不得记 EXPORTED/SUCCEEDED。"""
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)
    _place_clips(tmp_path, plan)

    def boom(payload, draft_dir):
        raise RuntimeError("受控导出失败")

    stub = types.ModuleType("jy_wrapper")
    stub.export_draft = boom
    monkeypatch.setattr(st, "_jianying_module", lambda: stub)
    res = st.run_assemble_stage(plan, tmp_path)
    assert res["jianying"] == "NOT_RUN"
    assert res["status"] == "PARTIAL"
    assert _assemble_subtitle_export(tmp_path)["jianying_export"] == "未验证"


def test_stub_jy_export_with_verified_artifact_is_exported(
        monkeypatch, tmp_path, storyboard_dict, dubbing_path, asset_ledger_path):
    """桩 jy 导出真实落盘草稿文件 → 核验通过后才可记 EXPORTED/SUCCEEDED（正向守卫）。"""
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)
    _place_clips(tmp_path, plan)

    def fake_export_draft(payload, draft_dir):
        assert payload["timeline"], "导出应收到非空时间线"
        d = Path(draft_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "draft_content.json").write_text(
            json.dumps({"shots": len(payload["timeline"])}, ensure_ascii=False),
            encoding="utf-8")
        return str(d / "draft_content.json")

    stub = types.ModuleType("jy_wrapper")
    stub.export_draft = fake_export_draft
    monkeypatch.setattr(st, "_jianying_module", lambda: stub)
    res = st.run_assemble_stage(plan, tmp_path)
    assert res["jianying"] == "EXPORTED"
    assert res["status"] == "SUCCEEDED"
    assert _assemble_subtitle_export(tmp_path)["jianying_export"] == "已导出"


def test_real_import_path_still_falls_back_without_jianying(
        monkeypatch, tmp_path, storyboard_dict, dubbing_path, asset_ledger_path):
    """回归守卫：真实 import 路径下无 jy 环境 → 沿用 NOT_RUN 降级（不被新逻辑误伤）。"""
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)
    _place_clips(tmp_path, plan)
    monkeypatch.delenv("JY_SKILL_ROOT", raising=False)
    assert "jy_wrapper" not in sys.modules, "基线环境不应预装 jy_wrapper"
    res = st.run_assemble_stage(plan, tmp_path)
    assert res["jianying"] == "NOT_RUN"
    assert res["status"] == "PARTIAL"
