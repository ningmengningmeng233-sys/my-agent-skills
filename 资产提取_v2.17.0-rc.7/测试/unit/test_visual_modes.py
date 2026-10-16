#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 1 工单 1.2：人话入口与非交互参数（三种视觉模式的真实行为）。

用户入口仍是容易理解的问题（「这次是否直接生成图片」），设置持久化到
`.process/项目设置.toml` 后由后续命令自动沿用：

- 显式选择 `data_only` → 不创建视觉计划，资料线照常；
- 显式选择 `prompt_only` → 可导出任务包，但自动生成命令必须拒绝（零 API 调用）；
- 显式选择 `direct_generate` → 允许进入自动生成路径（无密钥时仍明确失败）；
- 已有设置时再次运行不重复询问，也不改写设置（除非显式传参）；
- 显式命令行参数覆盖并持久化为新值。
"""
import pytest
import json
import sys
from pathlib import Path
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.project_settings import (  # noqa: E402
    ProjectSettings,
    load_project_settings,
    write_project_settings,
)

MAINTENANCE = REPO_ROOT / "scripts" / "maintenance.py"
VISUAL = REPO_ROOT / "scripts" / "visual_production.py"
STYLE = "仙侠国风电影CG"


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
    if mode:
        write_project_settings(lib, ProjectSettings(visual_mode=mode))
    return lib


def _cli(script, *args, env=None):
    import os

    base = {**os.environ, "ASSET_WORKERS": "4"}
    base.pop("GEMINI_API_KEY", None)
    if env:
        base.update(env)
    return run_py([str(script), *[str(a) for a in args]], cwd=str(REPO_ROOT), env=base)


# --- configure-visual：设置入口 ----------------------------------------------------

def test_configure_visual_writes_settings(tmp_path):
    lib = _lib(tmp_path)
    r = _cli(MAINTENANCE, "configure-visual", "--lib", lib, "--mode", "prompt-only",
             "--parallel", "2", "--composition", "preview", "--review-subagents", "on")
    assert r.returncode == 0, r.stdout + r.stderr
    settings = load_project_settings(lib)
    assert settings.visual_mode == "prompt_only"
    assert settings.max_parallel == 2
    assert settings.composition_mode == "preview"
    assert settings.review_subagents is True


def test_configure_visual_show_reports_current_mode(tmp_path):
    lib = _lib(tmp_path, mode="direct_generate")
    r = _cli(MAINTENANCE, "configure-visual", "--lib", lib, "--show", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["visual_mode"] == "direct_generate"


def test_explicit_args_override_and_persist(tmp_path):
    lib = _lib(tmp_path, mode="data_only")
    r = _cli(MAINTENANCE, "configure-visual", "--lib", lib, "--mode", "direct-generate",
             "--parallel", "4")
    assert r.returncode == 0, r.stdout + r.stderr
    settings = load_project_settings(lib)
    assert settings.visual_mode == "direct_generate"
    assert settings.max_parallel == 4
    assert settings.composition_mode == "auto", "未显式传的项保持原值/默认值"
    assert settings.review_subagents is False


def test_repeat_run_does_not_ask_again(tmp_path):
    lib = _lib(tmp_path, mode="prompt_only")
    before = (lib / ".process" / "项目设置.toml").read_bytes()
    r = _cli(MAINTENANCE, "configure-visual", "--lib", lib)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "沿用" in r.stdout or "沿用" in r.stderr, "已有设置时应说明沿用，而不是重新询问"
    assert (lib / ".process" / "项目设置.toml").read_bytes() == before


def test_configure_visual_rejects_bad_parallel(tmp_path):
    lib = _lib(tmp_path)
    r = _cli(MAINTENANCE, "configure-visual", "--lib", lib, "--mode", "data-only",
             "--parallel", "9")
    assert r.returncode != 0
    assert "PROJECT_SETTINGS_INVALID" in (r.stdout + r.stderr)


# --- data_only：不创建视觉计划 ------------------------------------------------------

def test_data_only_never_creates_visual_plan(tmp_path):
    lib = _lib(tmp_path, mode="data_only")
    r = _cli(MAINTENANCE, "visual-plan", "--lib", lib, "--style", STYLE)
    assert r.returncode != 0, "显式选择只反推时不得创建视觉计划"
    assert "VISUAL_MODE_DATA_ONLY" in (r.stdout + r.stderr), r.stdout + r.stderr
    assert not (lib / "视觉资产" / "计划.toml").is_file()


def test_plan_creation_still_works_without_explicit_settings(tmp_path):
    """未做过选择（缺设置文件）时保持向后兼容：显式 visual-plan 仍可建计划。"""
    lib = _lib(tmp_path)
    r = _cli(MAINTENANCE, "visual-plan", "--lib", lib, "--style", STYLE)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (lib / "视觉资产" / "计划.toml").is_file()


# --- prompt_only：可导任务包，但零 API 调用 -----------------------------------------

def test_prompt_only_blocks_automatic_generation(tmp_path):
    """只准备提示词模式必须在**密钥检查之前**拦截：既不生成，也不可能触网。

    断言模式原因码先于 GEMINI_API_KEY 提示出现，且不留下任何轮次目录——
    这就是「零 API 调用」的可核验形式（本用例全程无密钥、无网络）。
    """
    lib = _lib(tmp_path, mode="prompt_only")
    _cli(MAINTENANCE, "visual-plan", "--lib", lib, "--style", STYLE)
    r = _cli(VISUAL, "run", "--lib", lib, "--task", "CH_林姑娘-正脸-v01")
    assert r.returncode != 0, "只准备提示词模式下不得自动生成"
    out = r.stdout + r.stderr
    assert "VISUAL_MODE_PROMPT_ONLY" in out, out
    assert "GEMINI_API_KEY" not in out, f"应由模式拦截而不是密钥不足：{out}"
    assert not list((lib / "视觉资产" / "任务").glob("*/attempt-*")), "不得留下任何生成轮次"
    assert not (lib / "视觉资产" / "任务包" / "CH_林姑娘-正脸-v01").exists(), "run 不得顺手导包"


@pytest.mark.requires_private_stylepack
def test_prompt_only_still_exports_task_package(tmp_path):
    lib = _lib(tmp_path, mode="prompt_only")
    _cli(MAINTENANCE, "visual-plan", "--lib", lib, "--style", STYLE)
    r = _cli(VISUAL, "export", "--lib", lib, "--task", "CH_林姑娘-正脸-v01")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (lib / "视觉资产" / "任务包" / "CH_林姑娘-正脸-v01" / "提示词.md").is_file()


# --- direct_generate：设置被后续命令读取 -------------------------------------------

def test_direct_generate_reaches_generation_path(tmp_path):
    """选择直接生成后，自动命令不再被模式拦截（无密钥仍明确失败，但报错点不同）。"""
    lib = _lib(tmp_path, mode="direct_generate")
    _cli(MAINTENANCE, "visual-plan", "--lib", lib, "--style", STYLE)
    r = _cli(VISUAL, "run", "--lib", lib, "--task", "CH_林姑娘-正脸-v01")
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "GEMINI_API_KEY" in out, out
    assert "VISUAL_MODE_PROMPT_ONLY" not in out, "直接生成模式不得被只提示词模式拦截"
