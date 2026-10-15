#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目设置契约（阶段 1 工单 1.1）。

「这次是否直接生成图片 / 同时生成几张 / 自动还是先看构图卡 / 是否开启两个审核子代理」
四项选择持久化在 `.process/项目设置.toml`，继续同一项目时自动沿用，只有用户主动修改
设置时才重新询问。

口径：
- 缺文件 → 兼容默认值（`data_only`、并发 3、`auto`、子代理关闭），**不创建视觉目录**；
- 越界/类型错误/未知模式 → `PROJECT_SETTINGS_INVALID` 显式失败，绝不悄悄回默认值；
- 写入用「同目录临时文件 + `os.replace`」原子替换，同一组值重写字节稳定（统一 LF、UTF-8）；
- 核心资料线用 `safe_visual_mode` 安全读取：任何异常都退回 `data_only`，
  资料签发不因设置损坏而中断（设计 §2.1：核心反推不依赖视觉模块）。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 回退
    import tomli as tomllib  # type: ignore

SETTINGS_REL = (".process", "项目设置.toml")
SCHEMA_VERSION = 1
DEFAULT_MAX_PARALLEL = 3
MIN_PARALLEL = 1
MAX_PARALLEL = 4
DEFAULT_COMPOSITION_MODE = "auto"
COMPOSITION_MODES = ("auto", "preview")

SETTINGS_INVALID = "PROJECT_SETTINGS_INVALID"
# 模式拦截原因码（命令层用稳定码拒绝，而不是靠中文措辞）
MODE_BLOCKED_DATA_ONLY = "VISUAL_MODE_DATA_ONLY"
MODE_BLOCKED_PROMPT_ONLY = "VISUAL_MODE_PROMPT_ONLY"


class ProjectSettingsError(ValueError):
    """项目设置无效：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


class VisualMode(str, Enum):
    """三种内部视觉模式（用户入口仍是「这次是否直接生成图片」）。"""

    DATA_ONLY = "data_only"
    PROMPT_ONLY = "prompt_only"
    DIRECT_GENERATE = "direct_generate"


# 用户可见的模式说法（唯一来源：维护入口与视觉 CLI 都读这里，避免两套口径）
MODE_LABELS: dict[str, str] = {
    "data_only": "否，只反推资产（data_only）",
    "prompt_only": "不直接生成，只准备提示词任务包（prompt_only）",
    "direct_generate": "是，直接生成图片（direct_generate）",
}


@dataclass(frozen=True)
class ProjectSettings:
    """项目级设置（不可变；写入即整体替换）。"""

    schema_version: int = SCHEMA_VERSION
    visual_mode: str = VisualMode.DATA_ONLY.value
    max_parallel: int = DEFAULT_MAX_PARALLEL
    composition_mode: str = DEFAULT_COMPOSITION_MODE
    review_subagents: bool = False


def settings_path(lib: Path) -> Path:
    return Path(lib) / SETTINGS_REL[0] / SETTINGS_REL[1]


def _check_schema_version(value: object) -> int:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ProjectSettingsError(
            SETTINGS_INVALID, f"schema_version 必须为 {SCHEMA_VERSION}，实际 {value!r}")
    return value


def _check_visual_mode(value: object) -> str:
    allowed = tuple(mode.value for mode in VisualMode)
    if not isinstance(value, str) or value not in allowed:
        raise ProjectSettingsError(
            SETTINGS_INVALID, f"visual_mode 必须是 {allowed} 之一，实际 {value!r}")
    return value


def _check_max_parallel(value: object) -> int:
    if type(value) is not int or not MIN_PARALLEL <= value <= MAX_PARALLEL:
        raise ProjectSettingsError(
            SETTINGS_INVALID,
            f"max_parallel 必须是 {MIN_PARALLEL}–{MAX_PARALLEL} 的整数，实际 {value!r}")
    return value


def _check_composition_mode(value: object) -> str:
    if not isinstance(value, str) or value not in COMPOSITION_MODES:
        raise ProjectSettingsError(
            SETTINGS_INVALID, f"composition_mode 必须是 {COMPOSITION_MODES} 之一，实际 {value!r}")
    return value


def _check_review_subagents(value: object) -> bool:
    if not isinstance(value, bool):
        raise ProjectSettingsError(
            SETTINGS_INVALID, f"review_subagents 必须是布尔值，实际 {value!r}")
    return value


def validate_settings(settings: ProjectSettings) -> ProjectSettings:
    """逐项校验并返回规范化后的设置（不合法即 `PROJECT_SETTINGS_INVALID`）。"""
    if not isinstance(settings, ProjectSettings):
        raise ProjectSettingsError(SETTINGS_INVALID, f"设置类型错误：{type(settings).__name__}")
    return ProjectSettings(
        schema_version=_check_schema_version(settings.schema_version),
        visual_mode=_check_visual_mode(settings.visual_mode),
        max_parallel=_check_max_parallel(settings.max_parallel),
        composition_mode=_check_composition_mode(settings.composition_mode),
        review_subagents=_check_review_subagents(settings.review_subagents),
    )


def _render(settings: ProjectSettings) -> str:
    """确定性文本：键序固定、LF 结尾；同值必然同字节。"""
    return "\n".join((
        f"schema_version = {settings.schema_version}",
        f'visual_mode = "{settings.visual_mode}"',
        f"max_parallel = {settings.max_parallel}",
        f'composition_mode = "{settings.composition_mode}"',
        f"review_subagents = {'true' if settings.review_subagents else 'false'}",
    )) + "\n"


def load_project_settings(lib: Path) -> ProjectSettings:
    """读取项目设置；缺文件返回兼容默认值，损坏则显式 `PROJECT_SETTINGS_INVALID`。"""
    path = settings_path(lib)
    if not path.is_file():
        return ProjectSettings()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ProjectSettingsError(SETTINGS_INVALID, f"设置文件不可解析：{path.name}（{e}）") from e
    if not isinstance(data, dict):
        raise ProjectSettingsError(SETTINGS_INVALID, f"设置文件结构错误：{path.name}")
    base = ProjectSettings()
    # 未知键容忍（便于向后兼容地新增设置项），已知键一律严格校验
    merged = ProjectSettings(
        schema_version=data.get("schema_version", base.schema_version),
        visual_mode=data.get("visual_mode", base.visual_mode),
        max_parallel=data.get("max_parallel", base.max_parallel),
        composition_mode=data.get("composition_mode", base.composition_mode),
        review_subagents=data.get("review_subagents", base.review_subagents),
    )
    return validate_settings(merged)


def write_project_settings(lib: Path, settings: ProjectSettings) -> Path:
    """校验后原子写入设置文件，返回其路径（只创建 `.process/`，不创建视觉目录）。"""
    validated = validate_settings(settings)
    path = settings_path(lib)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _render(validated).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".项目设置-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def safe_visual_mode(lib: Path) -> str:
    """核心流程的安全读取：任何异常都退回 `data_only`，绝不中断资料线。"""
    try:
        return load_project_settings(lib).visual_mode
    except (ProjectSettingsError, OSError):
        return VisualMode.DATA_ONLY.value


def explicit_visual_mode(lib: Path) -> str | None:
    """用户**显式选择**过的视觉模式；缺设置文件（尚未做过选择）时返回 None。

    与 `safe_visual_mode` 的区别：后者把「缺文件」也报成 `data_only`，用于展示与兜底；
    命令拦截只认显式选择，避免未做过选择的旧项目突然被拦。
    设置损坏时同样显式失败，不静默放过。
    """
    path = settings_path(lib)
    if not path.is_file():
        return None
    return load_project_settings(lib).visual_mode


def safe_settings_value(lib: Path, field: str, default: str = ""):
    """安全读取某一项设置值：缺文件或损坏时返回该项默认值，绝不中断调用方。"""
    fallback = getattr(ProjectSettings(), field, default)
    try:
        return getattr(load_project_settings(lib), field, fallback)
    except (ProjectSettingsError, OSError):
        return fallback
