#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 1 工单 1.1：项目设置契约（可选生图与三种视觉模式）。

设置文件固定 `.process/项目设置.toml`，是「这次是否生图 / 并发几路 / 自动还是先看构图卡 /
是否开启两个审核子代理」的唯一持久化事实源。口径：

- 缺文件 → 兼容默认值（`data_only`、并发 3、`auto`、子代理关闭），且**不创建视觉目录**；
- 三种模式往返；并发只接受 1–4 的整数（布尔与字符串一律拒绝）；
- 损坏或越界显式 `PROJECT_SETTINGS_INVALID`，绝不悄悄回默认值；
- 同值重写字节稳定；写入原子替换，不留半截文件或临时文件。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.project_settings import (  # noqa: E402
    DEFAULT_MAX_PARALLEL,
    SETTINGS_INVALID,
    SETTINGS_REL,
    ProjectSettings,
    ProjectSettingsError,
    VisualMode,
    load_project_settings,
    safe_visual_mode,
    write_project_settings,
)


def _lib(tmp_path, name="资产库"):
    lib = tmp_path / name
    (lib / ".process").mkdir(parents=True, exist_ok=True)
    return lib


# --- 1. 缺文件：兼容默认值，且不创建视觉目录 ---------------------------------------

def test_missing_settings_returns_compatible_defaults(tmp_path):
    lib = _lib(tmp_path)
    settings = load_project_settings(lib)
    assert settings.schema_version == 1
    assert settings.visual_mode == VisualMode.DATA_ONLY.value
    assert settings.max_parallel == DEFAULT_MAX_PARALLEL == 3
    assert settings.composition_mode == "auto"
    assert settings.review_subagents is False


def test_missing_settings_does_not_create_visual_dirs(tmp_path):
    lib = _lib(tmp_path)
    load_project_settings(lib)
    assert not (lib / "视觉资产").exists(), "只读设置不得创建视觉目录"
    assert not (lib / SETTINGS_REL[0] / SETTINGS_REL[1]).exists(), "只读设置不得顺手写文件"


def test_settings_file_is_under_process_dir():
    assert SETTINGS_REL == (".process", "项目设置.toml")


# --- 2. 三种模式往返 ---------------------------------------------------------------

@pytest.mark.parametrize("mode", ["data_only", "prompt_only", "direct_generate"])
def test_three_modes_roundtrip(tmp_path, mode):
    lib = _lib(tmp_path)
    write_project_settings(lib, ProjectSettings(visual_mode=mode, max_parallel=3))
    loaded = load_project_settings(lib)
    assert loaded.visual_mode == mode
    assert loaded.schema_version == 1


def test_write_returns_settings_path(tmp_path):
    lib = _lib(tmp_path)
    path = write_project_settings(lib, ProjectSettings(visual_mode="prompt_only"))
    assert path == lib / SETTINGS_REL[0] / SETTINGS_REL[1]
    assert path.is_file()


def test_visual_mode_enum_values_are_stable():
    assert VisualMode.DATA_ONLY.value == "data_only"
    assert VisualMode.PROMPT_ONLY.value == "prompt_only"
    assert VisualMode.DIRECT_GENERATE.value == "direct_generate"


# --- 3. 中文路径字节稳定 / 同值重写字节不变 -----------------------------------------

def test_chinese_path_is_byte_stable(tmp_path):
    lib = _lib(tmp_path, name="资产反推_修仙记-第01集")
    settings = ProjectSettings(visual_mode="direct_generate", max_parallel=4,
                               composition_mode="preview", review_subagents=True)
    first = write_project_settings(lib, settings).read_bytes()
    second = write_project_settings(lib, settings).read_bytes()
    assert first == second, "同值重写必须字节稳定"
    assert first.decode("utf-8").count("\r\n") == 0, "设置文件统一 LF"


def test_same_values_rewrite_keeps_bytes(tmp_path):
    lib = _lib(tmp_path)
    write_project_settings(lib, ProjectSettings(visual_mode="prompt_only", max_parallel=2))
    before = (lib / SETTINGS_REL[0] / SETTINGS_REL[1]).read_bytes()
    write_project_settings(lib, ProjectSettings(visual_mode="prompt_only", max_parallel=2))
    after = (lib / SETTINGS_REL[0] / SETTINGS_REL[1]).read_bytes()
    assert before == after


def test_write_leaves_no_temp_files(tmp_path):
    lib = _lib(tmp_path)
    write_project_settings(lib, ProjectSettings(visual_mode="prompt_only"))
    leftovers = [p.name for p in (lib / ".process").iterdir()
                 if p.name not in {"项目设置.toml"}]
    assert leftovers == [], f"原子写入不得留下临时文件：{leftovers}"


def test_write_does_not_create_visual_dirs(tmp_path):
    lib = _lib(tmp_path)
    write_project_settings(lib, ProjectSettings(visual_mode="direct_generate"))
    assert not (lib / "视觉资产").exists(), "写设置不等于启用视觉（计划仍由 visual-plan 创建）"


# --- 4. 并发与枚举取值校验 ---------------------------------------------------------

@pytest.mark.parametrize("value", [0, 5, -1, "3", True, 2.0, None])
def test_parallel_out_of_range_or_wrong_type_rejected(tmp_path, value):
    lib = _lib(tmp_path)
    with pytest.raises(ProjectSettingsError) as ctx:
        write_project_settings(lib, ProjectSettings(max_parallel=value))
    assert ctx.value.code == SETTINGS_INVALID


@pytest.mark.parametrize("value", [1, 2, 3, 4])
def test_parallel_accepts_one_to_four(tmp_path, value):
    lib = _lib(tmp_path)
    write_project_settings(lib, ProjectSettings(max_parallel=value))
    assert load_project_settings(lib).max_parallel == value


def test_unknown_visual_mode_rejected(tmp_path):
    lib = _lib(tmp_path)
    with pytest.raises(ProjectSettingsError) as ctx:
        write_project_settings(lib, ProjectSettings(visual_mode="auto_generate"))
    assert ctx.value.code == SETTINGS_INVALID


def test_unknown_composition_mode_rejected(tmp_path):
    lib = _lib(tmp_path)
    with pytest.raises(ProjectSettingsError) as ctx:
        write_project_settings(lib, ProjectSettings(composition_mode="sometimes"))
    assert ctx.value.code == SETTINGS_INVALID


def test_review_subagents_must_be_bool(tmp_path):
    lib = _lib(tmp_path)
    with pytest.raises(ProjectSettingsError) as ctx:
        write_project_settings(lib, ProjectSettings(review_subagents="yes"))
    assert ctx.value.code == SETTINGS_INVALID


# --- 5. 损坏设置：显式报错，不得悄悄回默认 ------------------------------------------

def test_corrupt_toml_raises_settings_invalid(tmp_path):
    lib = _lib(tmp_path)
    (lib / SETTINGS_REL[0] / SETTINGS_REL[1]).write_text("这不是 TOML [[[ ", encoding="utf-8")
    with pytest.raises(ProjectSettingsError) as ctx:
        load_project_settings(lib)
    assert ctx.value.code == SETTINGS_INVALID


def test_corrupt_values_raise_settings_invalid(tmp_path):
    lib = _lib(tmp_path)
    (lib / SETTINGS_REL[0] / SETTINGS_REL[1]).write_text(
        'schema_version = 1\nvisual_mode = "whatever"\n', encoding="utf-8")
    with pytest.raises(ProjectSettingsError) as ctx:
        load_project_settings(lib)
    assert ctx.value.code == SETTINGS_INVALID


def test_out_of_range_value_in_file_is_invalid(tmp_path):
    lib = _lib(tmp_path)
    (lib / SETTINGS_REL[0] / SETTINGS_REL[1]).write_text(
        'schema_version = 1\nvisual_mode = "data_only"\nmax_parallel = 9\n', encoding="utf-8")
    with pytest.raises(ProjectSettingsError) as ctx:
        load_project_settings(lib)
    assert ctx.value.code == SETTINGS_INVALID


def test_corrupt_settings_report_did_not_silently_default(tmp_path):
    lib = _lib(tmp_path)
    (lib / SETTINGS_REL[0] / SETTINGS_REL[1]).write_text("坏设置", encoding="utf-8")
    try:
        load_project_settings(lib)
    except ProjectSettingsError as e:
        assert "PROJECT_SETTINGS_INVALID" in str(e)
    else:  # pragma: no cover - 走到这里说明退回了默认值
        pytest.fail("损坏设置必须显式失败，不得悄悄回默认值")


# --- 6. 核心流程的安全读取（资料线不因设置损坏而中断） -------------------------------

def test_safe_visual_mode_defaults_to_data_only_when_missing(tmp_path):
    lib = _lib(tmp_path)
    assert safe_visual_mode(lib) == VisualMode.DATA_ONLY.value


def test_safe_visual_mode_reads_written_value(tmp_path):
    lib = _lib(tmp_path)
    write_project_settings(lib, ProjectSettings(visual_mode="prompt_only"))
    assert safe_visual_mode(lib) == "prompt_only"


def test_safe_visual_mode_never_raises_on_corrupt_settings(tmp_path):
    lib = _lib(tmp_path)
    (lib / SETTINGS_REL[0] / SETTINGS_REL[1]).write_text("坏设置", encoding="utf-8")
    assert safe_visual_mode(lib) == VisualMode.DATA_ONLY.value, (
        "核心资料线不得因设置损坏而中断；安全读取退回 data_only")


def test_safe_visual_mode_on_missing_lib_dir(tmp_path):
    assert safe_visual_mode(tmp_path / "不存在") == VisualMode.DATA_ONLY.value


# --- 7. 与双交付状态线的接线（status 可见、核心线不受影响） -------------------------

def test_visual_delivery_state_reports_mode(tmp_path):
    from scripts.engine.pipeline_state import visual_delivery_state

    lib = _lib(tmp_path)
    assert visual_delivery_state(lib)["mode"] == "data_only", "缺设置时默认 data_only"
    write_project_settings(lib, ProjectSettings(visual_mode="prompt_only"))
    state = visual_delivery_state(lib)
    assert state["mode"] == "prompt_only"
    assert state["state"] == "NOT_ENABLED", "模式不改变「未创建计划」这一事实"


def test_visual_delivery_state_survives_corrupt_settings(tmp_path):
    from scripts.engine.pipeline_state import visual_delivery_state

    lib = _lib(tmp_path)
    (lib / SETTINGS_REL[0] / SETTINGS_REL[1]).write_text("坏设置", encoding="utf-8")
    assert visual_delivery_state(lib)["mode"] == "data_only"
