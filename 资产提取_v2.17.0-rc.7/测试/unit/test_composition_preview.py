#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 3 工单 3.4：构图预览模式（先看构图卡再生成）。

- `compose`：为计划内的任务生成/刷新导演构图卡，并把路径与哈希写回计划；
- `approve-composition`：宿主或用户批准某张构图卡（记录哈希，改卡即失效）；
- `composition_mode=preview` 时未批准的构图卡**不得进入生成/导出**（`COMPOSITION_NOT_APPROVED`）；
- `composition_mode=auto`（默认）不要求批准，行为与旧版一致。
"""
import pytest
import json
import sys
from pathlib import Path
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.project_settings import ProjectSettings, write_project_settings  # noqa: E402
from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_planner import plan_visual_tasks  # noqa: E402
from scripts.engine.visual_schema import load_visual_plan, write_visual_plan  # noqa: E402

VISUAL = REPO_ROOT / "scripts" / "visual_production.py"
STYLE = resolve_style_record("仙侠国风电影CG")
TASK_ID = "CH_林姑娘-正脸-v01"


def _index():
    return {
        "schema_version": 1, "source_fingerprint": "sha256:demo",
        "assets": [{"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘", "priority": "P1",
                    "fact_level": "剧本事实",
                    "evidence": [{"segment": "段01.md", "line": "00001", "quote": "林姑娘走进茶楼"}],
                    "relations": [], "dependencies": [], "status": "CONFIRMED"}],
    }


def _lib(tmp_path, mode=None):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(_index(), ensure_ascii=False), encoding="utf-8")
    write_visual_plan(plan_visual_tasks(_index(), STYLE), lib / "视觉资产" / "计划.toml")
    if mode:
        write_project_settings(lib, ProjectSettings(visual_mode="prompt_only",
                                                    composition_mode=mode))
    return lib


def _cli(script, *args, env=None):
    import os

    base = {**os.environ, "ASSET_WORKERS": "4"}
    base.pop("GEMINI_API_KEY", None)
    if env:
        base.update(env)
    return run_py([str(script), *[str(a) for a in args]], cwd=str(REPO_ROOT), env=base)


# --- 1. compose：生成构图卡并写回计划 ----------------------------------------------

def test_compose_writes_cards_for_plan_tasks(tmp_path):
    lib = _lib(tmp_path)
    r = _cli(VISUAL, "compose", "--lib", lib)
    assert r.returncode == 0, r.stdout + r.stderr
    card = lib / "视觉资产" / "构图卡" / f"{TASK_ID}.toml"
    assert card.is_file(), r.stdout
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    task = plan.by_id(TASK_ID)
    assert task.composition_path.endswith(f"构图卡/{TASK_ID}.toml")
    assert len(task.composition_sha256) == 64


def test_compose_is_idempotent(tmp_path):
    lib = _lib(tmp_path)
    _cli(VISUAL, "compose", "--lib", lib)
    card = lib / "视觉资产" / "构图卡" / f"{TASK_ID}.toml"
    before = card.read_bytes()
    _cli(VISUAL, "compose", "--lib", lib)
    assert card.read_bytes() == before, "同输入重跑不得改写构图卡"


def test_compose_single_task(tmp_path):
    lib = _lib(tmp_path)
    r = _cli(VISUAL, "compose", "--lib", lib, "--task", TASK_ID)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (lib / "视觉资产" / "构图卡" / f"{TASK_ID}.toml").is_file()


# --- 2. preview：未批准不得生成/导出 -----------------------------------------------

def test_preview_blocks_export_until_approved(tmp_path):
    lib = _lib(tmp_path, mode="preview")
    _cli(VISUAL, "compose", "--lib", lib)
    r = _cli(VISUAL, "export", "--lib", lib, "--task", TASK_ID)
    assert r.returncode != 0
    assert "COMPOSITION_NOT_APPROVED" in (r.stdout + r.stderr)


@pytest.mark.requires_private_stylepack
def test_approve_then_export_succeeds(tmp_path):
    lib = _lib(tmp_path, mode="preview")
    _cli(VISUAL, "compose", "--lib", lib)
    ok = _cli(VISUAL, "approve-composition", "--lib", lib, "--task", TASK_ID)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    r = _cli(VISUAL, "export", "--lib", lib, "--task", TASK_ID)
    assert r.returncode == 0, r.stdout + r.stderr


def test_changed_card_invalidates_approval(tmp_path):
    lib = _lib(tmp_path, mode="preview")
    _cli(VISUAL, "compose", "--lib", lib)
    _cli(VISUAL, "approve-composition", "--lib", lib, "--task", TASK_ID)
    card = lib / "视觉资产" / "构图卡" / f"{TASK_ID}.toml"
    card.write_text(card.read_text(encoding="utf-8") + "# 宿主手改\n", encoding="utf-8")
    r = _cli(VISUAL, "export", "--lib", lib, "--task", TASK_ID)
    assert r.returncode != 0
    assert "COMPOSITION_NOT_APPROVED" in (r.stdout + r.stderr)


@pytest.mark.requires_private_stylepack
def test_auto_mode_does_not_require_approval(tmp_path):
    lib = _lib(tmp_path, mode="auto")
    _cli(VISUAL, "compose", "--lib", lib)
    r = _cli(VISUAL, "export", "--lib", lib, "--task", TASK_ID)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.requires_private_stylepack
def test_no_settings_means_auto(tmp_path):
    lib = _lib(tmp_path)
    _cli(VISUAL, "compose", "--lib", lib)
    r = _cli(VISUAL, "export", "--lib", lib, "--task", TASK_ID)
    assert r.returncode == 0, r.stdout + r.stderr


def test_approve_requires_existing_card(tmp_path):
    lib = _lib(tmp_path, mode="preview")
    r = _cli(VISUAL, "approve-composition", "--lib", lib, "--task", TASK_ID)
    assert r.returncode != 0
    assert "COMPOSITION_CARD_MISSING" in (r.stdout + r.stderr)
