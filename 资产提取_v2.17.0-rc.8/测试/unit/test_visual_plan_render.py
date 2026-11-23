#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单02-Task7：视觉计划阅读稿与可选状态接线（规格 9）。

TOML 是唯一机器事实源；Markdown 只由 TOML 渲染——改 Markdown 不得改变状态，
改 TOML 后重渲染必须覆盖阅读稿。
"""
import json
import sys
from pathlib import Path
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scripts.engine.pipeline_state import collect_pipeline_state  # noqa: E402
from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_planner import plan_visual_tasks  # noqa: E402
from scripts.engine.visual_schema import (  # noqa: E402
    ReferenceBinding,
    VisualTask,
    load_visual_plan,
    write_visual_plan,
)
from scripts.render_visual_plan import (  # noqa: E402
    READING_COPY_NAME,
    render_visual_plan,
    write_reading_copy,
)

STYLE = resolve_style_record("仙侠国风电影CG")


def _index():
    return {
        "schema_version": 1, "source_fingerprint": "sha256:demo",
        "assets": [
            {"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘", "priority": "P0",
             "fact_level": "剧本事实",
             "evidence": [{"segment": "段01.md", "line": "00001", "quote": "林姑娘走进茶楼"}],
             "relations": [], "dependencies": [], "status": "CONFIRMED"},
        ],
    }


def _plan(**kw):
    plan = plan_visual_tasks(_index(), STYLE, **kw)
    return plan


def _lib_with_plan(tmp_path, **kw):
    lib = tmp_path / "资产库"
    plan = _plan(**kw)
    write_visual_plan(plan, lib / "视觉资产" / "计划.toml")
    return lib, plan


def test_render_uses_chinese_status_labels(tmp_path):
    plan = _plan()
    text = render_visual_plan(plan)
    assert "待生成" in text
    assert "`PLANNED`" not in text
    assert "PLANNED" not in text


def test_render_shows_style_pin(tmp_path):
    text = render_visual_plan(_plan())
    assert "仙侠国风电影CG" in text
    assert "1.0.0" in text
    assert STYLE.pack_hash[:12] in text


def test_render_lists_dependencies_in_chinese():
    text = render_visual_plan(_plan())
    assert "依赖" in text
    assert "正脸" in text
    assert "CH_林姑娘-正脸-v01" in text


def test_render_lists_reference_roles():
    task = VisualTask(
        task_id="CH_林姑娘-正脸-v01", asset_id="CH_林姑娘", kind="正脸", status="PLANNED",
        aspect_ratio="1:1", style_slug=STYLE.pack_slug, style_version=STYLE.pack_version,
        style_hash=STYLE.pack_hash,
        references=(ReferenceBinding(path="_参考/face.png", role="identity", note="身份参考"),),
        fact_source=".process/资产索引.json#CH_林姑娘｜摘句: 林姑娘走进茶楼",
    )
    text = render_visual_plan(_plan().with_task(task))
    assert "身份" in text
    assert "_参考/face.png" in text


def test_render_has_next_step_command():
    text = render_visual_plan(_plan())
    assert "create_visual_plan.py" in text or "visual_production.py" in text


def test_render_header_forbids_manual_edit():
    text = render_visual_plan(_plan())
    assert "自动生成" in text
    assert "不要手工修改" in text or "禁止手工修改" in text


def test_write_reading_copy_writes_file(tmp_path):
    lib, plan = _lib_with_plan(tmp_path)
    path = write_reading_copy(plan, lib)
    assert path.name == READING_COPY_NAME
    assert path.is_file()
    assert "仙侠国风电影CG" in path.read_text(encoding="utf-8")


def test_editing_markdown_does_not_change_state(tmp_path):
    lib, plan = _lib_with_plan(tmp_path)
    write_reading_copy(plan, lib)
    before = collect_pipeline_state(lib)["visual_delivery"]
    copy = lib / "视觉资产" / READING_COPY_NAME
    copy.write_text("我手工把这里改成全部已通过\n", encoding="utf-8")
    after = collect_pipeline_state(lib)["visual_delivery"]
    assert before == after
    assert after["state"] == "PLANNED"


def test_rerender_overwrites_manual_edits(tmp_path):
    lib, plan = _lib_with_plan(tmp_path)
    write_reading_copy(plan, lib)
    copy = lib / "视觉资产" / READING_COPY_NAME
    copy.write_text("手工垃圾内容\n", encoding="utf-8")
    write_reading_copy(plan, lib)
    text = copy.read_text(encoding="utf-8")
    assert "手工垃圾内容" not in text
    assert "仙侠国风电影CG" in text


def test_toml_change_is_reflected_after_rerender(tmp_path):
    lib, plan = _lib_with_plan(tmp_path)
    write_reading_copy(plan, lib)
    task = plan.by_id("CH_林姑娘-正脸-v01")
    write_visual_plan(plan.with_task(VisualTask(**{**task.__dict__, "status": "ACCEPTED"})),
                      lib / "视觉资产" / "计划.toml")
    reloaded = load_visual_plan(lib / "视觉资产" / "计划.toml")
    write_reading_copy(reloaded, lib)
    assert "已通过" in (lib / "视觉资产" / READING_COPY_NAME).read_text(encoding="utf-8")


# --- 状态接线 -------------------------------------------------------------------

def test_visual_delivery_not_enabled_without_plan(delivered_lib):
    state = collect_pipeline_state(delivered_lib)["visual_delivery"]
    assert state["state"] == "NOT_ENABLED"
    assert state["reason_code"] == "VISUAL_OPTION_NOT_ENABLED"


def test_visual_delivery_planned_with_plan(tmp_path):
    lib, _plan0 = _lib_with_plan(tmp_path)
    state = collect_pipeline_state(lib)["visual_delivery"]
    assert state["state"] == "PLANNED"
    assert state["counts"]["PLANNED"] == len(_plan0.tasks)


def test_visual_delivery_partial_when_some_accepted(tmp_path):
    lib, plan = _lib_with_plan(tmp_path)
    from conftest import accept_task_with_artifact

    accept_task_with_artifact(lib, "CH_林姑娘-正脸-v01", status="ACCEPTED")
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    _ = plan
    state = collect_pipeline_state(lib)["visual_delivery"]
    assert state["state"] == "PARTIAL"
    assert state["counts"].get("ACCEPTED") == 1


def test_visual_delivery_needs_manifest_to_deliver(tmp_path):
    lib, plan = _lib_with_plan(tmp_path)
    from conftest import accept_all_with_artifacts

    accept_all_with_artifacts(lib, status="ACCEPTED")
    # 全部通过但缺视觉交付清单 → 仍不是"视觉资产已交付"
    assert collect_pipeline_state(lib)["visual_delivery"]["state"] == "PARTIAL"
    (lib / "视觉资产" / "视觉交付清单.json").write_text(
        json.dumps({"status": "DELIVERED"}, ensure_ascii=False), encoding="utf-8")
    state = collect_pipeline_state(lib)["visual_delivery"]
    assert state["state"] == "DELIVERED"
    assert state["reason_code"] == "VISUAL_DELIVERY_VALID"


def test_core_delivery_ignores_visual_state(tmp_path):
    """视觉计划存在与否，都不得改变资料交付线。"""
    lib, _plan0 = _lib_with_plan(tmp_path)
    state = collect_pipeline_state(lib)
    assert state["data_delivery"]["state"] == state["state"]
    assert state["visual_delivery"]["state"] != "NOT_ENABLED"


# --- CLI ------------------------------------------------------------------------

def _cli(lib, *extra):
    return run_py(
            [str(REPO_ROOT / "scripts" / "maintenance.py"), "visual-plan", "--lib", str(lib), "--style", "仙侠国风电影CG", *extra],
    )


def test_visual_plan_cli_writes_both_files(tmp_path):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(_index(), ensure_ascii=False), encoding="utf-8")
    r = _cli(lib)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (lib / "视觉资产" / "计划.toml").is_file()
    assert (lib / "视觉资产" / READING_COPY_NAME).is_file()
    assert "仙侠国风电影CG" in r.stdout


def test_status_json_exposes_visual_stats_without_losing_legacy_fields(delivered_lib):
    lib, plan = delivered_lib, _plan()
    write_visual_plan(plan, lib / "视觉资产" / "计划.toml")
    r = run_py(
            [str(REPO_ROOT / "scripts" / "maintenance.py"), "status", "--lib", str(lib), "--json"],
    )
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    # 旧字段一字不变
    for key in ("pipeline_state", "reason_code", "evidence_path", "next_command",
                "segments", "registry", "final", "desc", "base", "deriv", "sched_rows",
                "done", "next_step"):
        assert key in payload, key
    assert payload["pipeline_state"] == "DELIVERED"
    assert payload["visual_delivery"]["state"] == "PLANNED"
    assert payload["visual_stats"]["PLANNED"] == len(plan.tasks)
