#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 13 红测：CLI、状态机、签发门与兼容层。

红测目标（实现前必须失败）：
- `build-ledger` / `compile-prompts` / `run-visual` 三条命令契约不存在；
- 发布状态机不反映「图片计划 / 生成 / 审核 / 返工」各阶段；
- 事件日志里的**最新失败**不会立即撤销实时 DELIVERED；
- 历史交付清单没有快照归档，且没有守卫阻止用历史快照冒充当前有效；
- `verify` 未检查数量守恒、美术总则指纹、依赖图完整、必需提示词、视觉审核与包内文件。

契约边界：视觉审核只在「产品结果含生图」（direct_generate）时参与签发；
data_only / prompt_only 下资料线完全不读视觉门（规格 9 两线独立）。
"""
import json
import sys
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "engine"))

MAINTENANCE = REPO_ROOT / "scripts" / "maintenance.py"
STYLE = "仙侠国风电影CG"


@pytest.fixture(scope="module")
def checks():
    """签发门扩展检查模块（惰性导入：模块不存在时必须让本文件其余用例仍可收集）。"""
    from scripts.engine import delivery_checks as module

    return module


def _cli(*args, expect=0):
    import os

    r = run_py(
            [str(MAINTENANCE), *[str(a) for a in args]],
            cwd=str(REPO_ROOT),
            env={**os.environ, "ASSET_WORKERS": "4"},
    )
    if expect is not None:
        assert r.returncode == expect, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    return r


def _write(p: Path, content: str = "内容"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")


def _index(asset_id="CH_张三", name="张三"):
    return {
        "schema_version": 1, "source_fingerprint": "sha256:demo",
        "assets": [{"asset_id": asset_id, "stable_key": "CHR:00001:1", "type": "人物",
                    "name": name, "priority": "P0", "fact_level": "剧本事实",
                    "evidence": [{"segment": "段01.md", "line": "00001", "quote": f"{name}进茶楼"}],
                    "relations": [], "dependencies": [], "status": "CONFIRMED"}],
    }


def _ledger_lib(root: Path, *, prompt=True):
    """带资产索引、反推总表与基础提示词的最小库（总账可由索引重建）。"""
    lib = root / "资产库"
    _write(lib / ".process" / "资产索引.json", json.dumps(_index(), ensure_ascii=False))
    _write(lib / "反推_最终.md", "| CH_张三 | 张三 |\n")
    if prompt:
        _write(lib / "提示词" / "基础" / "人物提示词.md", "## 001. 张三\n\n正文\n")
    return lib


# --- 1. 命令契约：三条新命令存在且有中文帮助 ---------------------------------------

@pytest.mark.parametrize("cmd", ["build-ledger", "compile-prompts", "run-visual"])
def test_new_commands_are_exposed(cmd):
    r = _cli(cmd, "--help")
    assert "--lib" in r.stdout


def test_build_ledger_writes_ledger_and_single_json(tmp_path):
    lib = _ledger_lib(tmp_path)
    r = _cli("build-ledger", "--lib", lib, "--json")
    payload = json.loads(r.stdout)          # stdout 必须是唯一一个合法 JSON
    ledger = lib / ".process" / "asset-ledger.json"
    assert ledger.is_file()
    assert payload["entries"] >= 1
    assert payload["conservation"]["counts"]["formal"] >= 1


def test_build_ledger_without_index_is_loud_and_writes_nothing(tmp_path):
    lib = tmp_path / "资产库"
    lib.mkdir()
    r = _cli("build-ledger", "--lib", lib, expect=None)
    assert r.returncode != 0
    assert "资产索引" in (r.stdout + r.stderr)
    assert not (lib / ".process" / "asset-ledger.json").is_file()


def test_compile_prompts_shadow_never_touches_official_prompts(tmp_path):
    lib = _ledger_lib(tmp_path)
    _cli("build-ledger", "--lib", lib)
    official = lib / "提示词" / "基础" / "人物提示词.md"
    before = official.read_bytes()

    r = _cli("compile-prompts", "--lib", lib, "--shadow", "--json")
    payload = json.loads(r.stdout)
    assert official.read_bytes() == before, "shadow 模式不得改写正式提示词"
    assert (lib / ".process" / "shadow-提示词差异.md").is_file()
    assert payload["mode"] == "shadow"


def test_compile_prompts_requires_ledger(tmp_path):
    lib = _ledger_lib(tmp_path)
    r = _cli("compile-prompts", "--lib", lib, "--shadow", expect=None)
    assert r.returncode != 0
    assert "总账" in (r.stdout + r.stderr)


# --- 2. 三种产品结果由显式命令/配置选择 --------------------------------------------

def test_run_visual_refuses_when_product_excludes_generation(tmp_path):
    lib = _ledger_lib(tmp_path)
    _cli("configure-visual", "--lib", lib, "--mode", "data-only")
    r = _cli("run-visual", "--lib", lib, expect=None)
    assert r.returncode != 0
    assert "VISUAL_MODE_DATA_ONLY" in (r.stdout + r.stderr)


def test_run_visual_accepts_batch_size_but_not_view_specs(tmp_path):
    lib = _ledger_lib(tmp_path)
    _cli("configure-visual", "--lib", lib, "--mode", "direct-generate")
    # 单批数量可调；越界拒绝（复用既有 1–4 并发契约）
    r = _cli("run-visual", "--lib", lib, "--batch", "9", expect=None)
    assert r.returncode != 0
    assert "1–4" in (r.stdout + r.stderr) or "1-4" in (r.stdout + r.stderr)
    # Omni 已定义的视图规格不得成为可询问/可覆盖参数
    for forbidden in ("--views", "--view-spec", "--angles"):
        assert forbidden not in _cli("run-visual", "--help").stdout


# --- 3. 状态机反映图片计划 / 生成 / 审核 / 返工 -------------------------------------

def _visual_lib(tmp_path):
    """带视觉计划的最小库（复用交付门既有夹具口径）。"""
    from scripts.engine.style_registry import resolve_style_record
    from scripts.engine.visual_planner import plan_visual_tasks
    from scripts.engine.visual_schema import write_visual_plan

    lib = _ledger_lib(tmp_path)
    record = resolve_style_record(STYLE)
    write_visual_plan(plan_visual_tasks(_index(), record), lib / "视觉资产" / "计划.toml")
    return lib


@pytest.mark.parametrize("stage", ["PLANNED", "GENERATING", "REVIEWING", "REWORK", "SIGNED"])
def test_visual_line_reports_stage(stage):
    from scripts.engine.pipeline_state import VISUAL_STAGES

    assert stage in VISUAL_STAGES


def test_visual_stage_is_planned_before_any_output(tmp_path):
    from scripts.engine.pipeline_state import visual_delivery_state

    lib = _visual_lib(tmp_path)
    state = visual_delivery_state(lib)
    assert state["stage"] == "PLANNED"


def test_visual_stage_is_rework_when_a_task_needs_revision(tmp_path):
    from conftest import accept_all_with_artifacts
    from scripts.engine.pipeline_state import visual_delivery_state

    lib = _visual_lib(tmp_path)
    accept_all_with_artifacts(lib)
    plan_path = lib / "视觉资产" / "计划.toml"
    from scripts.engine.visual_schema import VisualTask, load_visual_plan, write_visual_plan

    plan = load_visual_plan(plan_path)
    target = plan.tasks[0]
    write_visual_plan(plan.with_task(VisualTask(**{**target.__dict__, "status": "NEEDS_REVISION"})),
                      plan_path)
    assert visual_delivery_state(lib)["stage"] == "REWORK"


# --- 4. 最新失败立即撤销实时 DELIVERED ---------------------------------------------

def _delivered_visual_lib(tmp_path):
    """全部任务走发布器通过并签发视觉交付清单 → 视觉线 DELIVERED。"""
    from check_visual_delivery import build_visual_delivery
    from conftest import publish_all_with_reviews
    from scripts.engine.project_settings import (
        ProjectSettings,
        VisualMode,
        write_project_settings,
    )

    lib = _visual_lib(tmp_path)
    write_project_settings(lib, ProjectSettings(visual_mode=VisualMode.DIRECT_GENERATE.value))
    publish_all_with_reviews(lib)
    result = build_visual_delivery(lib)
    assert result["status"] == "DELIVERED", result["blocking"]
    return lib


def test_delivered_visual_line_reports_signed(tmp_path):
    from scripts.engine.pipeline_state import visual_delivery_state

    lib = _delivered_visual_lib(tmp_path)
    state = visual_delivery_state(lib)
    assert state["state"] == "DELIVERED"
    assert state["stage"] == "SIGNED"


def test_latest_failure_event_revokes_delivered_immediately(tmp_path):
    """写前日志里的最新失败必须先于计划生效：不得因计划仍写「已通过」而保持 DELIVERED。"""
    from scripts.engine.pipeline_state import visual_delivery_state
    from scripts.engine.visual_events import TRANSPORT_FAILED, VisualEvent, append_events
    from scripts.engine.visual_schema import load_visual_plan

    lib = _delivered_visual_lib(tmp_path)
    task_id = load_visual_plan(lib / "视觉资产" / "计划.toml").tasks[0].task_id
    append_events(lib, [VisualEvent.create(TRANSPORT_FAILED, task_id,
                                           payload={"error": "connection reset"})])

    state = visual_delivery_state(lib)
    assert state["state"] != "DELIVERED", "最新失败未撤销实时 DELIVERED"
    assert state["stage"] == "REWORK"
    assert task_id in state["reason"] or "失败" in state["reason"]


# --- 5. 历史交付清单只作快照 -------------------------------------------------------

def test_history_snapshot_is_not_current_authority(tmp_path):
    from scripts.engine.pipeline_state import derive_pipeline_state

    import conftest

    lib = conftest.make_delivered_lib(tmp_path)
    assert derive_pipeline_state(lib)["state"] == "DELIVERED"
    # 把清单移进历史目录：历史快照不得被当成当前有效
    history = lib / "交付清单历史"
    history.mkdir(parents=True, exist_ok=True)
    (lib / "交付清单.json").replace(history / "2026-09-19.json")
    state = derive_pipeline_state(lib)
    assert state["state"] != "DELIVERED"
    assert "MANIFEST" in state["reason_code"] or "AWAITING" in state["reason_code"]


def test_new_signing_archives_previous_manifest(tmp_path):
    from scripts.engine.pipeline_state import archive_delivery_manifest

    import conftest

    lib = conftest.make_delivered_lib(tmp_path)
    archived = archive_delivery_manifest(lib)
    assert archived, "历史清单必须归档为快照"
    assert Path(archived).is_file()
    assert (lib / "交付清单.json").is_file(), "归档不得删除当前清单"


# --- 6. verify 扩展检查 -----------------------------------------------------------

def test_extended_checks_are_part_of_signing_contract(checks):
    from scripts.engine.pipeline_state import REQUIRED_DELIVERY_CHECKS

    for name in checks.EXTENDED_DELIVERY_CHECKS:
        assert name in REQUIRED_DELIVERY_CHECKS


def test_check_asset_coverage_flags_silently_dropped_asset(checks, tmp_path):
    lib = _ledger_lib(tmp_path, prompt=False)      # 有正式资产但没有任何提示词
    _cli("build-ledger", "--lib", lib)
    rc, detail = checks.check_asset_coverage(lib)
    assert rc != 0
    assert "CH_张三" in detail


def test_check_required_prompts_flags_missing_section(checks, tmp_path):
    lib = _ledger_lib(tmp_path)
    _cli("build-ledger", "--lib", lib)
    assert checks.check_required_prompts(lib)[0] == 0
    _write(lib / "提示词" / "基础" / "人物提示词.md", "## 001. 别人\n\n正文\n")
    rc, detail = checks.check_required_prompts(lib)
    assert rc != 0
    assert "张三" in detail


def test_check_art_constitution_rejects_fingerprint_drift(checks, tmp_path):
    from scripts.engine.art_constitution import (
        ArtConstitution,
        save_constitution,
    )

    lib = _ledger_lib(tmp_path)
    assert checks.check_art_constitution(lib)[0] == 0, "无总则=未设定，不得判失败"
    save_constitution(lib, ArtConstitution(scene_aesthetic="写实电影感"))
    assert checks.check_art_constitution(lib)[0] == 0
    path = lib / ".process" / "项目美术总则.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["scene_aesthetic"] = "被手改的内容"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    rc, detail = checks.check_art_constitution(lib)
    assert rc != 0
    assert "指纹" in detail


def test_check_artifact_graph_requires_graph_once_ledger_exists(checks, tmp_path):
    lib = _ledger_lib(tmp_path)
    assert checks.check_artifact_graph(lib)[0] == 0, "无总账时依赖图不适用"
    _cli("build-ledger", "--lib", lib)
    assert checks.check_artifact_graph(lib)[0] == 0
    (lib / ".process" / "artifact-graph.json").unlink()
    rc, detail = checks.check_artifact_graph(lib)
    assert rc != 0
    assert "依赖图" in detail


def test_check_package_files_flags_missing_core_file(checks, tmp_path):
    lib = _ledger_lib(tmp_path)
    assert checks.check_package_files(lib)[0] == 0
    (lib / "反推_最终.md").unlink()
    rc, detail = checks.check_package_files(lib)
    assert rc != 0
    assert "反推_最终.md" in detail


def test_visual_review_not_applicable_without_generation(checks, tmp_path):
    from scripts.engine.project_settings import (
        ProjectSettings,
        VisualMode,
        write_project_settings,
    )

    lib = _visual_lib(tmp_path)
    write_project_settings(lib, ProjectSettings(visual_mode=VisualMode.DATA_ONLY.value))
    rc, detail = checks.check_visual_review(lib)
    assert rc == 0, detail
    assert "不适用" in detail


def test_visual_review_blocks_when_generation_chosen_but_unreviewed(checks, tmp_path):
    from scripts.engine.project_settings import (
        ProjectSettings,
        VisualMode,
        write_project_settings,
    )

    lib = _visual_lib(tmp_path)
    write_project_settings(lib, ProjectSettings(visual_mode=VisualMode.DIRECT_GENERATE.value))
    rc, detail = checks.check_visual_review(lib)
    assert rc != 0
    assert "图片" in detail or "视觉" in detail


def test_visual_review_passes_after_publish(checks, tmp_path):
    lib = _delivered_visual_lib(tmp_path)
    rc, detail = checks.check_visual_review(lib)
    assert rc == 0, detail


# --- 7. verify 接线与撤销：正例到 DELIVERED，删证据立即脱离 -------------------------
#
# 真实「跑完全链到 DELIVERED」的证据在 `测试/e2e_flow.py --snapshot`（工单 13 的终验命令）：
# 那里有真实的产品格式提示词。本文件用 conftest.make_delivered_lib 这一被交付门用例
# 共用的夹具，专注钉住「扩展检查确实进了 verify」与「删一项证据立即脱离 DELIVERED」。

def _verified_lib(tmp_path):
    """已签发交付清单的库（含真实提示词节；夹具与视觉交付门用例同源）。"""
    import conftest

    return conftest.make_delivered_lib(tmp_path)


def test_delivered_lib_reports_delivered(tmp_path):
    from scripts.engine.pipeline_state import derive_pipeline_state

    lib = _verified_lib(tmp_path)
    assert derive_pipeline_state(lib)["state"] == "DELIVERED"
    payload = json.loads(_cli("status", "--lib", lib, "--json").stdout)
    assert payload["data_delivery"]["state"] == "DELIVERED"
    assert payload["compiler_stages"]           # 编译链阶段随 status 一并报告


def test_verify_runs_every_extended_check(checks, tmp_path):
    """verify 必须真的调用扩展检查：输出里逐项出现，且缺项时非零退出。"""
    lib = _verified_lib(tmp_path)
    (lib / "提示词" / "基础" / "人物提示词.md").unlink()
    r = _cli("verify", "--lib", lib, expect=None)
    assert r.returncode != 0
    for name in checks.EXTENDED_DELIVERY_CHECKS:
        assert name in r.stdout, f"verify 未执行 {name}"
    assert "check_required_prompts" in r.stdout
    assert "张三" in r.stdout


def test_deleting_required_prompt_leaves_delivered(tmp_path):
    lib = _verified_lib(tmp_path)
    (lib / "提示词" / "基础" / "人物提示词.md").unlink()

    from scripts.engine.pipeline_state import derive_pipeline_state

    assert derive_pipeline_state(lib)["state"] != "DELIVERED"


def test_deleting_source_evidence_leaves_delivered(tmp_path):
    lib = _verified_lib(tmp_path)
    (lib / "原文段" / "小说-作者" / "段01.md").unlink()

    from scripts.engine.pipeline_state import derive_pipeline_state

    assert derive_pipeline_state(lib)["state"] != "DELIVERED"


def test_breaking_visual_review_leaves_delivered_visual_line(tmp_path):
    from scripts.engine.pipeline_state import visual_delivery_state

    lib = _delivered_visual_lib(tmp_path)
    assert visual_delivery_state(lib)["state"] == "DELIVERED"
    (lib / "视觉资产" / "视觉交付清单.json").unlink()
    assert visual_delivery_state(lib)["state"] != "DELIVERED"


def test_stale_manifest_does_not_block_resigning(checks, tmp_path):
    """追更后旧清单必然过期：包内文件检查不得因此判死，否则永远无法重新签发。

    两件事必须分开：`status` 说「旧签发已失效」，`verify` 说「现在能不能重签」。
    """
    from scripts.engine.pipeline_state import derive_pipeline_state

    lib = _verified_lib(tmp_path)
    (lib / "提示词" / "基础" / "人物提示词.md").write_text(
        "## 001. 张三\n\n改过的正文\n", encoding="utf-8", newline="\n")

    rc, detail = checks.check_package_files(lib)
    assert rc == 0, detail
    assert "取代" in detail
    assert derive_pipeline_state(lib)["state"] != "DELIVERED", "旧签发必须判失效"


# --- 8. 兼容层：旧命令退出码不变，弃用提示只在文档 ---------------------------------

@pytest.mark.parametrize("args", [
    ("status", "--lib", "{lib}"),
    ("verify-visual", "--lib", "{lib}"),
    ("visual-doctor",),
])
def test_legacy_commands_keep_their_exit_codes(args, tmp_path):
    lib = _visual_lib(tmp_path)
    r = _cli(*[a.format(lib=lib) for a in args], expect=None)
    # 旧命令必须仍可调用且行为可解释（不得因新内核接线而崩溃/改码）
    assert r.returncode in (0, 1, 3), r.stdout + r.stderr
    assert "Traceback" not in (r.stdout + r.stderr)


def test_deprecation_notes_live_in_docs_not_stdout(tmp_path):
    lib = _ledger_lib(tmp_path)
    r = _cli("status", "--lib", lib)
    assert "弃用" not in r.stdout, "弃用提示不得混进命令输出，应写在文档里"
    changelog = (REPO_ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "兼容" in changelog and "build-ledger" in changelog
