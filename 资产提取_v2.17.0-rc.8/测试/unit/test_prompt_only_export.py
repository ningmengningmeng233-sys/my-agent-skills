#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 4 工单 4.2：只准备提示词（批量导出任务包，零网络）。

`prompt_only` 模式下 `export-all` 为所有**当前可执行**任务导出完整任务包：
正向提示词、独立禁止项、参考图副本与职责、画幅、构图卡、输入哈希与建议保存路径。
不得发出网络请求；状态为 `AWAITING_MANUAL_RESULT`，不冒充已生成。
"""
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
FACE = "CH_林姑娘-正脸-v01"
BACK = "CH_林姑娘-后脑-v01"


def _lib(tmp_path, mode=None):
    index = {
        "schema_version": 1, "source_fingerprint": "sha256:demo",
        "assets": [{"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘", "priority": "P1",
                    "fact_level": "剧本事实",
                    "evidence": [{"segment": "段01.md", "line": "00001", "quote": "林姑娘走进茶楼"}],
                    "relations": [], "dependencies": [], "status": "CONFIRMED"}],
    }
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")
    write_visual_plan(plan_visual_tasks(index, STYLE), lib / "视觉资产" / "计划.toml")
    if mode:
        write_project_settings(lib, ProjectSettings(visual_mode=mode))
    return lib


def _cli(*args):
    import os

    base = {**os.environ, "ASSET_WORKERS": "4"}
    base.pop("GEMINI_API_KEY", None)
    base.pop("CODEX_GEMINI_BASE_URL", None)
    return run_py([str(VISUAL), *[str(a) for a in args]], cwd=str(REPO_ROOT), env=base)


def test_export_all_exports_ready_tasks(tmp_path):
    lib = _lib(tmp_path, mode="prompt_only")
    r = _cli("export-all", "--lib", lib)
    assert r.returncode == 0, r.stdout + r.stderr
    pkg = lib / "视觉资产" / "任务包" / FACE
    for name in ("请求.json", "提示词.md", "参考图清单.md"):
        assert (pkg / name).is_file(), name


def test_export_all_skips_tasks_with_unmet_dependencies(tmp_path):
    lib = _lib(tmp_path, mode="prompt_only")
    _cli("export-all", "--lib", lib)
    assert not (lib / "视觉资产" / "任务包" / BACK).exists(), "上游未通过时不得导出下游"
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    assert plan.by_id(FACE).status == "AWAITING_MANUAL_RESULT"
    assert plan.by_id(BACK).status == "PLANNED"


def test_export_all_is_zero_network(tmp_path):
    lib = _lib(tmp_path, mode="prompt_only")
    r = _cli("export-all", "--lib", lib)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not list((lib / "视觉资产" / "任务").glob("*/attempt-*")), "不得产生任何生成轮次"
    out = r.stdout + r.stderr
    assert "已生成" not in out.replace("已生成待检查", ""), "不得冒充已生成"


def test_export_all_does_not_mark_direct_generation(tmp_path):
    lib = _lib(tmp_path)
    _cli("export-all", "--lib", lib)
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    assert plan.by_id(FACE).status == "AWAITING_MANUAL_RESULT"


def test_export_all_is_idempotent(tmp_path):
    lib = _lib(tmp_path, mode="prompt_only")
    _cli("export-all", "--lib", lib)
    pkg = lib / "视觉资产" / "任务包" / FACE / "提示词.md"
    before = pkg.read_bytes()
    second = _cli("export-all", "--lib", lib)
    assert second.returncode == 0, second.stdout + second.stderr
    assert pkg.read_bytes() == before
    assert "跳过" in second.stdout, "已导出的任务不得重复导出"
