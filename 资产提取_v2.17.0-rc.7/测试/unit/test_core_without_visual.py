#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单01-Task1：核心资产反推交付与可选视觉生产彻底解耦。

规格 3.1：核心流程不得导入 Gemini SDK、不得要求 GEMINI_API_KEY、不得加载图像，
也不得因为视觉任务未完成而拒绝资料签发。
规格 9：资产资料交付与视觉资产交付是两条独立状态线；视觉未启用时显示"未启用"，
不得显示失败。
"""
import json
import sys
from pathlib import Path
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.pipeline_state import collect_pipeline_state

# 核心反推链路模块：任一静态依赖 Gemini SDK 即违反规格 3.1 与 12（非目标）
CORE_MODULES = (
    "scripts/engine/pipeline_state.py",
    "scripts/engine/asset_schema.py",
    "scripts/engine/continuity.py",
    "scripts/engine/index_db.py",
    "scripts/maintenance.py",
    "scripts/split_segments.py",
    "scripts/extract_segments.py",
    "scripts/aggregate.py",
    "scripts/generate_prompts.py",
    "scripts/scan_registry.py",
)

# 迁移/离线检查脚本同样必须可在无视觉依赖环境下运行
OFFLINE_MODULES = (
    "scripts/check_schedule.py",
    "scripts/check_final.py",
    "scripts/check_asset_index.py",
    "scripts/check_style_lib.py",
)

GEMINI_SDK_MARKERS = ("google.genai", "google.generativeai", "from google import genai")


def test_core_delivery_does_not_require_visual_or_gemini(delivered_lib, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    state = collect_pipeline_state(delivered_lib)
    assert state["data_delivery"]["state"] == "DELIVERED"
    assert state["visual_delivery"]["state"] == "NOT_ENABLED"


def test_visual_delivery_is_not_a_failure_when_unenabled(delivered_lib):
    """视觉未启用是合法状态，不是失败：reason_code 必须可区分于任何错误码。"""
    state = collect_pipeline_state(delivered_lib)
    assert state["visual_delivery"]["reason_code"] == "VISUAL_OPTION_NOT_ENABLED"
    assert state["visual_delivery"]["state"] not in ("FAILED", "BLOCKED")


def test_visual_delivery_tracks_plan_presence(delivered_lib):
    """创建视觉计划后视觉线转为"待生成"，且资料交付判定不被牵动。"""
    plan = delivered_lib / "视觉资产" / "计划.toml"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text('schema_version = 1\n', encoding="utf-8")
    state = collect_pipeline_state(delivered_lib)
    assert state["visual_delivery"]["state"] == "PLANNED"
    assert state["visual_delivery"]["reason_code"] == "VISUAL_PLAN_CREATED"
    assert state["data_delivery"]["state"] == "DELIVERED"


def test_legacy_top_level_fields_preserved(delivered_lib):
    """旧调用者读 state/reason_code/next_command，必须一字不差地保留。"""
    from scripts.engine.pipeline_state import derive_pipeline_state

    legacy = derive_pipeline_state(delivered_lib)
    state = collect_pipeline_state(delivered_lib)
    for key in ("state", "reason_code", "next_command", "evidence_path"):
        assert state[key] == legacy[key], key


def test_data_delivery_mirrors_pipeline_state(delivered_lib):
    state = collect_pipeline_state(delivered_lib)
    assert state["data_delivery"]["state"] == state["state"]
    assert state["data_delivery"]["reason_code"] == state["reason_code"]


def test_missing_llm_config_still_delivers(delivered_lib):
    """没有 OpenAI 兼容配置也不得拒绝资料签发（规格 3.1 的等价推论）。"""
    state = collect_pipeline_state(delivered_lib, has_config=False)
    assert state["data_delivery"]["state"] == "DELIVERED"


def test_collect_pipeline_state_accepts_verify_results(delivered_lib):
    """verify_results/style 透传：status --json 与 verify 共用同一推导。"""
    state = collect_pipeline_state(delivered_lib, verify_results=[("check_final", 0)], style="H7")
    assert state["data_delivery"]["state"] == "DELIVERED"
    assert state["visual_delivery"]["state"] == "NOT_ENABLED"


def test_core_modules_have_no_gemini_sdk_import():
    """核心链路源码不得出现 Gemini SDK 导入（静态扫描，含迁移与离线检查脚本）。"""
    offenders = []
    for rel in CORE_MODULES + OFFLINE_MODULES:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if any(m in code for m in GEMINI_SDK_MARKERS):
                offenders.append(f"{rel}:{i}")
    assert offenders == [], f"核心链路出现 Gemini SDK 导入: {offenders}"


def test_fresh_interpreter_imports_core_without_google_sdk():
    """干净解释器导入核心状态机后，sys.modules 不得出现 google SDK。"""
    script = (
        "import sys, json;"
        "sys.path.insert(0, r'%s');"
        "import scripts.engine.pipeline_state as m;"
        "print(json.dumps(sorted(k for k in sys.modules if k.startswith('google'))))"
        % str(REPO_ROOT)
    )
    proc = run_py(["-c", script])
    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])
    assert loaded == [], f"核心导入拉起 Google SDK: {loaded}"


def test_status_json_exposes_both_delivery_lines(delivered_lib):
    """status --json 同时给出两条状态线，且旧字段原样保留。"""
    proc = run_py(
            [str(REPO_ROOT / "scripts" / "maintenance.py"), "status", "--lib", str(delivered_lib), "--json"],
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["pipeline_state"] == "DELIVERED"
    assert payload["data_delivery"]["state"] == "DELIVERED"
    assert payload["visual_delivery"]["state"] == "NOT_ENABLED"
    assert payload["next_command"]
