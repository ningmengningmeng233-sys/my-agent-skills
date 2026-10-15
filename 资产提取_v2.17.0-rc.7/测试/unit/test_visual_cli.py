#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单03-Task7：视觉 CLI、自动拼版与视觉签发（规格 8 / 9）。

无密钥时 `run` 必须非零而 `export` 必须成功；导入必须绑定现有 task_id；
拼版只接受 ACCEPTED 图且不调用生成模型；视觉签发独立于资料签发。
"""
import base64
import json
import sys
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.compose_reference_board import (  # noqa: E402
    BOARD_SUBDIR,
    BoardError,
    compose_person_board,
)
from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_planner import plan_visual_tasks  # noqa: E402
from scripts.engine.visual_schema import (  # noqa: E402
    AttemptRecord,
    VisualTask,
    load_visual_plan,
    write_visual_plan,
)
from dataclasses import replace  # noqa: E402
from scripts.check_visual_delivery import (  # noqa: E402
    DELIVERY_MANIFEST_NAME,
    build_visual_delivery,
)

STYLE = resolve_style_record("仙侠国风电影CG")
# 真实最小的 4x4 PNG（经 Pillow verify() 通过）
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")
VISUAL = REPO_ROOT / "scripts" / "visual_production.py"
MAINTENANCE = REPO_ROOT / "scripts" / "maintenance.py"
BOARD_CLI = REPO_ROOT / "scripts" / "compose_reference_board.py"
DELIVERY_CLI = REPO_ROOT / "scripts" / "check_visual_delivery.py"


def _index():
    return {
        "schema_version": 1, "source_fingerprint": "sha256:demo",
        "assets": [
            {"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘", "priority": "P1",
             "fact_level": "剧本事实",
             "evidence": [{"segment": "段01.md", "line": "00001", "quote": "林姑娘走进茶楼"}],
             "relations": [], "dependencies": [], "status": "CONFIRMED"},
        ],
    }


def _lib(tmp_path, with_plan=True):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(_index(), ensure_ascii=False), encoding="utf-8")
    if with_plan:
        plan = plan_visual_tasks(_index(), STYLE)
        write_visual_plan(plan, lib / "视觉资产" / "计划.toml")
    return lib


def _cli(script, *args, env=None):
    import os

    base = {**os.environ, "ASSET_WORKERS": "4"}
    base.pop("GEMINI_API_KEY", None) if env is None else None
    if env:
        base.update(env)
    return run_py([str(script), *[str(a) for a in args]], cwd=str(REPO_ROOT), env=base)


# --- CLI 守卫 -------------------------------------------------------------------

def test_run_without_key_fails_loudly(tmp_path):
    lib = _lib(tmp_path)
    r = _cli(VISUAL, "run", "--lib", lib, "--task", "CH_林姑娘-正脸-v01")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "GEMINI_API_KEY" in out
    assert "export" in out, "无密钥时必须指出可改用手工导出"


def test_export_without_key_succeeds(tmp_path):
    lib = _lib(tmp_path)
    r = _cli(VISUAL, "export", "--lib", lib, "--task", "CH_林姑娘-正脸-v01")
    assert r.returncode == 0, r.stdout + r.stderr
    pkg = lib / "视觉资产" / "任务包" / "CH_林姑娘-正脸-v01"
    for name in ("请求.json", "提示词.md", "参考图清单.md"):
        assert (pkg / name).is_file(), name


def test_export_marks_task_awaiting_manual(tmp_path):
    lib = _lib(tmp_path)
    _cli(VISUAL, "export", "--lib", lib, "--task", "CH_林姑娘-正脸-v01")
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    assert plan.by_id("CH_林姑娘-正脸-v01").status == "AWAITING_MANUAL_RESULT"


def test_import_requires_existing_task(tmp_path):
    lib = _lib(tmp_path)
    img = tmp_path / "结果.png"
    img.write_bytes(PNG)
    bad = _cli(VISUAL, "import", "--lib", lib, "--task", "CH_不存在-正脸-v01", "--image", img)
    assert bad.returncode != 0
    ok = _cli(VISUAL, "import", "--lib", lib, "--task", "CH_林姑娘-正脸-v01", "--image", img)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert (lib / "视觉资产" / "任务" / "CH_林姑娘-正脸-v01" / "attempt-01" / "image.png").is_file()


def test_import_binds_attempt_to_task(tmp_path):
    lib = _lib(tmp_path)
    img = tmp_path / "结果.png"
    img.write_bytes(PNG)
    _cli(VISUAL, "import", "--lib", lib, "--task", "CH_林姑娘-正脸-v01", "--image", img)
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    task = plan.by_id("CH_林姑娘-正脸-v01")
    assert task.round_count == 1
    assert task.attempts[0].image.startswith("视觉资产/任务/CH_林姑娘-正脸-v01/attempt-01")


def test_upstream_blocked_task_cannot_run(tmp_path):
    """只生成依赖已通过的任务：上游未通过时下游必须拒绝。"""
    lib = _lib(tmp_path)
    r = _cli(VISUAL, "run", "--lib", lib, "--task", "CH_林姑娘-后脑-v01",
             env={"GEMINI_API_KEY": "fake-key-for-guard-only"})
    assert r.returncode != 0
    assert "依赖" in (r.stdout + r.stderr)


def test_check_lists_task_status(tmp_path):
    lib = _lib(tmp_path)
    r = _cli(VISUAL, "check", "--lib", lib, "--task", "CH_林姑娘-正脸-v01")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "待生成" in r.stdout


# --- 拼版 ------------------------------------------------------------------------

def _accepted_images(lib):
    base = lib / "visual"
    base.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(3):
        p = base / f"{i}.png"
        p.write_bytes(PNG)
        out.append(p)
    return out


def test_board_requires_accepted_images(tmp_path):
    with pytest.raises(BoardError) as ctx:
        compose_person_board([], tmp_path / "板.png")
    assert ctx.value.code == "NO_ACCEPTED_IMAGES"


def test_board_composes_from_images_only(tmp_path):
    images = _accepted_images(tmp_path)
    out = compose_person_board(images, tmp_path / "board" / "人物板.png")
    assert out.is_file()
    assert out.stat().st_size > 0


def test_board_is_deterministic(tmp_path):
    images = _accepted_images(tmp_path)
    a = compose_person_board(images, tmp_path / "a.png")
    b = compose_person_board(images, tmp_path / "b.png")
    assert a.read_bytes() == b.read_bytes()


def test_board_cli_writes_manifest_with_hashes(tmp_path):
    lib = _lib(tmp_path)
    img = tmp_path / "结果.png"
    img.write_bytes(PNG)
    task_id = "CH_林姑娘-正脸-v01"
    _cli(VISUAL, "import", "--lib", lib, "--task", task_id, "--image", img)
    _accept_task(lib, task_id)
    r = _cli(BOARD_CLI, "--lib", lib, "--task", task_id)
    assert r.returncode == 0, r.stdout + r.stderr
    board = lib / BOARD_SUBDIR[0] / BOARD_SUBDIR[1] / f"{task_id}.png"
    assert board.is_file()
    meta = board.with_suffix(".toml")
    assert meta.is_file()
    text = meta.read_text(encoding="utf-8")
    assert "input_hashes" in text


def test_board_rejects_unaccepted_task(tmp_path):
    lib = _lib(tmp_path)
    r = _cli(BOARD_CLI, "--lib", lib, "--task", "CH_林姑娘-正脸-v01")
    assert r.returncode != 0
    assert "通过" in (r.stdout + r.stderr)


def test_stale_board_is_invalidated(tmp_path):
    """任一输入过期（哈希变化）则旧拼版失效。"""
    from scripts.compose_reference_board import board_is_fresh

    lib = _lib(tmp_path)
    img = tmp_path / "结果.png"
    img.write_bytes(PNG)
    task_id = "CH_林姑娘-正脸-v01"
    _cli(VISUAL, "import", "--lib", lib, "--task", task_id, "--image", img)
    _accept_task(lib, task_id)
    _cli(BOARD_CLI, "--lib", lib, "--task", task_id)
    assert board_is_fresh(lib, task_id)
    (lib / "视觉资产" / "任务" / task_id / "attempt-01" / "image.png").write_bytes(PNG + b"x")
    assert not board_is_fresh(lib, task_id)


# --- 视觉签发 --------------------------------------------------------------------

def _accept_task(lib, task_id):
    """置为已通过并落真实产物（交付门会核验产物存在与哈希一致）。

    `image=None`：复用调用方刚导入的图片字节，避免覆盖真实产物导致拼版失效。
    """
    from conftest import accept_task_with_artifact

    accept_task_with_artifact(lib, task_id, image=None)


def _accept_all(lib):
    """走发布器把全部任务置为正式版本（交付门会交叉验证正式版本清单）。"""
    from conftest import publish_all_with_reviews

    publish_all_with_reviews(lib)


def test_delivery_blocked_when_tasks_pending(tmp_path):
    lib = _lib(tmp_path)
    result = build_visual_delivery(lib)
    assert result["status"] != "DELIVERED"
    assert result["blocking"]


def test_delivery_signed_when_all_required_accepted(tmp_path):
    lib = _lib(tmp_path)
    _accept_all(lib)
    result = build_visual_delivery(lib)
    assert result["status"] == "DELIVERED", result["blocking"]
    assert (lib / "视觉资产" / DELIVERY_MANIFEST_NAME).is_file()


def test_optional_failure_does_not_block_but_is_listed(tmp_path):
    lib = _lib(tmp_path)
    _accept_all(lib)
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    first = plan.tasks[0]
    broken = VisualTask(**{**first.__dict__, "status": "NEEDS_HUMAN_DECISION",
                           "kind": "拼版", "task_id": f"{first.asset_id}-拼版-v01",
                           "aspect_ratio": "16:9"})
    write_visual_plan(type(plan)(**{**plan.__dict__,
                                    "tasks": plan.tasks + (broken,)}),
                      lib / "视觉资产" / "计划.toml")
    result = build_visual_delivery(lib)
    assert result["status"] == "DELIVERED"
    assert any("拼版" in item for item in result["incomplete"])


def test_cli_exit_code_reflects_delivery(tmp_path):
    lib = _lib(tmp_path)
    blocked = _cli(DELIVERY_CLI, "--lib", lib)
    assert blocked.returncode != 0
    _accept_all(lib)
    signed = _cli(DELIVERY_CLI, "--lib", lib)
    assert signed.returncode == 0, signed.stdout + signed.stderr


def test_maintenance_verify_visual_isolated_from_verify(tmp_path):
    """资料 verify 不调用视觉门；verify-visual 独立执行。"""
    lib = _lib(tmp_path)
    r = _cli(REPO_ROOT / "scripts" / "maintenance.py", "verify-visual", "--lib", lib)
    assert r.returncode != 0
    assert "视觉" in (r.stdout + r.stderr)


def test_status_json_shows_both_lines_with_visual_stats(tmp_path):
    lib = _lib(tmp_path)
    r = _cli(REPO_ROOT / "scripts" / "maintenance.py", "status", "--lib", lib, "--json")
    payload = json.loads(r.stdout)
    assert "data_delivery" in payload and "visual_delivery" in payload
    assert payload["visual_delivery"]["state"] == "PLANNED"
    assert payload["visual_stats"]


def test_core_verify_never_requires_visual_module(tmp_path):
    """核心 verify 不得**直接依赖**视觉门（资料线独立）。

    工单 13 起视觉审核成为签发契约里的一项独立检查：它由
    `engine.delivery_checks.check_visual_review` 承担，且只在产品结果含生图
    （direct_generate）时参与；data_only / prompt_only 判「不适用」并放行。
    因此这里钉住的是「do_verify 本体不碰任何视觉入口」这一更强也更精确的不变量，
    行为面由 test_asset_compiler_cli.py 的 test_visual_review_* 与
    test_verify_runs_every_extended_check 覆盖。
    """
    import re

    src = (REPO_ROOT / "scripts" / "maintenance.py").read_text(encoding="utf-8")
    parts = re.split(r"^def do_verify\(", src, flags=re.M)
    assert len(parts) == 2, "未找到 do_verify 定义"
    verify_body = parts[1].split("\ndef ", 1)[0]
    for forbidden in ("check_visual_delivery", "build_visual_delivery", "visual_scheduler",
                      "visual_production", "load_visual_plan", "visual_delivery_state"):
        assert forbidden not in verify_body, f"do_verify 不得直接依赖 {forbidden}"


# --- 工单 0.2 红测：正式路径不得内联 Gemini 终审 -----------------------------------

def test_cmd_run_never_uses_gemini_qc_checker(tmp_path):
    """Gemini（含 Codex 转接）只生成，不终审：正式入口不得导入或实例化 GeminiQcChecker。

    允许该类存在以兼容旧兼容路径，但必须不可达且标记为测试专用。
    """
    src = (REPO_ROOT / "scripts" / "visual_production.py").read_text(encoding="utf-8")
    assert "GeminiQcChecker" not in src, "视觉 CLI 不得引用 GeminiQcChecker"
    pipeline = (REPO_ROOT / "scripts" / "engine" / "visual_pipeline.py").read_text(encoding="utf-8")
    assert "GeminiQcChecker" not in pipeline, "流水线不得引用 GeminiQcChecker"


# --- 阶段 5 工单 5.2：批量入口、暂停与机器可读状态 --------------------------------

def _plan_with_no_runnable_tasks(tmp_path):
    """把计划内全部任务置为「可作为参考」：没有可执行任务，故无需密钥即可跑批量入口。"""
    lib = _lib(tmp_path)
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    done = tuple(
        VisualTask(**{**task.__dict__, "status": "READY_AS_REFERENCE", "attempts": (
            AttemptRecord(round=1, image="视觉资产/任务/占位/image.png",
                          image_sha256="a" * 64),)})
        for task in plan.tasks)
    write_visual_plan(replace(plan, tasks=done), lib / "视觉资产" / "计划.toml")
    return lib


def test_status_json_reports_machine_readable_state(tmp_path):
    lib = _lib(tmp_path)
    proc = _cli(VISUAL, "status", "--lib", lib, "--json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    assert payload["revision"] == plan.revision
    assert payload["total"] == len(plan.tasks)
    assert payload["counts"] == plan.counts_by_status()
    assert sum(payload["counts_label"].values()) == len(plan.tasks), "中文标签必须覆盖全部任务"
    assert payload["paused"] is False
    # 依赖顺序：只有链首「正脸」可执行，其余在等上游
    assert payload["ready"] == [plan.by_id("CH_林姑娘-正脸-v01").task_id], payload["ready"]
    assert len(payload["waiting_upstream"]) == len(plan.tasks) - 1
    assert payload["waiting_review"] == [] and payload["published"] == []


def test_status_without_json_prints_chinese(tmp_path):
    lib = _lib(tmp_path)
    proc = _cli(VISUAL, "status", "--lib", lib)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "待生成" in proc.stdout, "人话状态必须是中文"
    assert "计划" in proc.stdout


def test_run_ready_without_key_is_loud_and_leaves_no_trace(tmp_path):
    lib = _lib(tmp_path)
    plan_path = lib / "视觉资产" / "计划.toml"
    before = plan_path.read_bytes()
    proc = _cli(VISUAL, "run-ready", "--lib", lib)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, "无密钥必须明确失败，不得假装成功"
    assert "GEMINI_API_KEY" in out
    assert "export" in out, "必须给出手工/只提示词的替代路径"
    assert plan_path.read_bytes() == before, "拒绝时不得改动计划"
    assert not (lib / ".process" / "视觉事件.jsonl").exists(), "拒绝时不得派发、不得留事件"


def test_run_ready_parallel_flag_rejects_out_of_range(tmp_path):
    lib = _lib(tmp_path)
    proc = _cli(VISUAL, "run-ready", "--lib", lib, "--parallel", "9")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "1" in out and "4" in out, "必须说明可选范围 1–4"


def test_run_ready_with_nothing_ready_exits_zero_without_key(tmp_path):
    lib = _plan_with_no_runnable_tasks(tmp_path)
    proc = _cli(VISUAL, "run-ready", "--lib", lib)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "没有可执行任务" in out
    assert not (lib / ".process" / "视觉事件.jsonl").exists()


def test_pause_blocks_dispatch_until_cleared(tmp_path):
    lib = _lib(tmp_path)
    assert _cli(VISUAL, "pause", "--lib", lib).returncode == 0
    marker = lib / ".process" / "视觉暂停.json"
    assert marker.is_file(), "暂停必须落盘（保留已落盘状态）"
    for command in ("run-ready", "resume"):
        out = _cli(VISUAL, command, "--lib", lib)
        text = out.stdout + out.stderr
        assert out.returncode != 0, f"{command} 在暂停时必须拒绝派发"
        assert "VISUAL_PAUSED" in text
    assert _cli(VISUAL, "pause", "--lib", lib, "--clear").returncode == 0
    assert not marker.exists()
    after = _cli(VISUAL, "run-ready", "--lib", lib)
    assert "VISUAL_PAUSED" not in (after.stdout + after.stderr)


def test_resume_without_plan_fails_clearly(tmp_path):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    out = _cli(VISUAL, "resume", "--lib", lib)
    assert out.returncode != 0
    assert "计划" in (out.stdout + out.stderr)


def test_resume_with_nothing_ready_exits_zero_without_key(tmp_path):
    lib = _plan_with_no_runnable_tasks(tmp_path)
    out = _cli(VISUAL, "resume", "--lib", lib)
    assert out.returncode == 0, out.stdout + out.stderr
    assert not (lib / ".process" / "视觉事件.jsonl").exists()


def test_status_reports_effective_concurrency_from_project_settings(tmp_path):
    """并发数必须来自项目设置（命令行未给 --parallel 时），默认 3。"""
    lib = _lib(tmp_path)
    default = json.loads(_cli(VISUAL, "status", "--lib", lib, "--json").stdout)
    assert default["max_parallel"] == 3
    assert _cli(MAINTENANCE, "configure-visual", "--lib", lib, "--mode", "direct-generate",
                "--parallel", "2").returncode == 0
    after = json.loads(_cli(VISUAL, "status", "--lib", lib, "--json").stdout)
    assert after["max_parallel"] == 2, "项目设置的并发必须被读到"
    assert "直接生成" in _cli(VISUAL, "status", "--lib", lib).stdout


def test_corrupt_release_manifest_fails_loudly_in_all_entry_points(tmp_path):
    """正式版本清单损坏：status / run-ready / resume 都必须显式报错，不得静默停住下游。"""
    lib = _lib(tmp_path)
    (lib / "视觉资产").mkdir(parents=True, exist_ok=True)
    (lib / "视觉资产" / "正式版本.json").write_text("{ 半截 JSON", encoding="utf-8")
    for command in ("status", "status --json", "run-ready", "resume"):
        argv = command.split()
        out = _cli(VISUAL, *argv, "--lib", lib)
        text = out.stdout + out.stderr
        assert out.returncode != 0, f"{command} 必须非零退出：{text}"
        assert "RELEASE_MANIFEST_UNREADABLE" in text, f"{command} 必须给出稳定原因码：{text}"
