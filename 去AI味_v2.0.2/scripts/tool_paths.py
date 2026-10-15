#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tool_paths.py - 发布工具链的路径解析层（v1.2 先修问题第 3 项）。

机器专属位置一律不再写死进脚本与测试。每个外部位置按三层解析：

  1. **显式参数**（调用方从命令行参数拿到的值）；
  2. **环境变量**（``LIEFLAT_*``，其中 ``LIEFLAT_INSTALL_ROOT`` 由设计方案点名）；
  3. **自动发现**：只从 ``Path.home()`` 与本仓位置推导常见 Agent 宿主目录，
     绝不拼用户名目录，也绝不「猜一个看起来像的」。

命中哪一层会写进结果的 ``source`` 字段（``explicit`` / ``env`` / ``discover`` /
``missing``），换机排查时能直接看出是谁的设置在生效，而不是靠反推。

缺失的语义按必需/可选分两套，不混用：

  - 可选依赖（官方校验器、宿主数据库、跨仓治理仓）解析不到返回 ``None``，
    调用方据此记 **SKIP**；
  - 必需路径（安装根目录）解析不到抛 :class:`MissingPathError`，
    调用方据此记 **ERROR**。

显式值或环境变量指向一个不存在的路径同样算「没解析到」，**不回落**到下一层：
静默改用别的目录等于把「配置写错了」这件事藏起来，跨机时更难查。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

SKILL_DIR_NAME = "lieflat-less-ai-tone"
GITGUARD_REPO_DIR_NAME = "柠檬治理仓"
QUICK_VALIDATE_FILE_NAME = "quick_validate.py"
HOST_DB_FILE_NAME = "db.sqlite"

# 本机可能存在的 Agent 宿主目录名（技能发现根）。只有目录名约定，与用户名无关。
AGENT_HOME_DIRS = (".codex", ".agents", ".claude", ".qoder")
# 官方 skill-creator 校验器在宿主目录下的两种已知摆法
SKILL_CREATOR_SUBPATHS = (
    ("skills", ".system", "skill-creator", "scripts"),
    ("skills", "skill-creator", "scripts"),
)
# 宿主 CLI 数据库的已知位置（ZCode）；别的宿主用 LIEFLAT_HOST_DB 指过来
HOST_DB_SUBPATHS = ((".zcode", "cli", "db"),)

HOME = Path.home()
REPO_ROOT = Path(__file__).resolve().parent.parent

QUICK_VALIDATE_ENV = "LIEFLAT_QUICK_VALIDATE"
INSTALL_ROOT_ENV = "LIEFLAT_INSTALL_ROOT"
HOST_DB_ENV = "LIEFLAT_HOST_DB"
GITGUARD_ROOT_ENV = "LIEFLAT_GITGUARD_ROOT"

SOURCE_EXPLICIT = "explicit"
SOURCE_ENV = "env"
SOURCE_DISCOVER = "discover"
SOURCE_MISSING = "missing"

# 目标名 → (环境变量名, 挂在搜索根下面的相对路径)
TARGETS = {
    "quick_validate": (QUICK_VALIDATE_ENV, QUICK_VALIDATE_FILE_NAME),
    "install_root": (INSTALL_ROOT_ENV, SKILL_DIR_NAME),
    "host_db": (HOST_DB_ENV, HOST_DB_FILE_NAME),
    "gitguard_root": (GITGUARD_ROOT_ENV, GITGUARD_REPO_DIR_NAME),
}


class MissingPathError(RuntimeError):
    """必需路径解析不到：调用方应记 ERROR，而不是拿一个猜出来的路径继续跑。"""


@dataclass(frozen=True)
class ResolvedPath:
    """一次解析的结果：路径 + 命中的来源，供调用方写进结构化报告。"""

    path: Path | None
    source: str
    env_var: str = ""
    requested: Path | None = None
    relative_name: str = ""

    def require(self) -> Path:
        """按「必需路径」取用：解析不到就抛错（ERROR）。"""
        if self.path is not None:
            return self.path
        if self.requested is not None:
            detail = f"显式值或环境变量指向的路径不存在: {self.requested}"
        elif self.env_var:
            detail = f"环境变量 {self.env_var} 未设置，自动发现也未命中"
        else:
            detail = "自动发现未命中"
        raise MissingPathError(
            f"必需路径 {self.relative_name} 解析失败（来源={self.source}；{detail}）。"
            f"请用显式参数或环境变量 {self.env_var or 'LIEFLAT_*'} 指定；"
            "自动发现只从 Path.home() 推导宿主目录，不猜用户名目录。"
        )


def _is_set(value) -> bool:
    return value is not None and str(value).strip() != ""


def _clean(value) -> Path:
    return Path(value).expanduser().resolve()


def default_search_roots(target: str, home: Path | None = None,
                         repo_root: Path | None = None) -> tuple[Path, ...]:
    """该目标的自动发现根，全部由 home / 仓库位置推导（顺序即优先级）。"""
    home = HOME if home is None else Path(home)
    repo_root = REPO_ROOT if repo_root is None else Path(repo_root)
    if target == "quick_validate":
        return tuple(
            home / host / Path(*sub)
            for host in AGENT_HOME_DIRS for sub in SKILL_CREATOR_SUBPATHS
        )
    if target == "install_root":
        return tuple(home / host / "skills" for host in AGENT_HOME_DIRS)
    if target == "host_db":
        return tuple(home / Path(*sub) for sub in HOST_DB_SUBPATHS)
    if target == "gitguard_root":
        # 跨仓依赖：本仓同级/上两级目录，其次家目录与桌面
        return (repo_root.parent, repo_root.parent.parent, home / "Desktop", home)
    raise ValueError(f"未知路径目标: {target}（可选 {sorted(TARGETS)}）")


def resolve_optional_path(explicit=None, env_name="", search_roots=(),
                          relative_name="") -> ResolvedPath:
    """按 显式 → 环境变量 → 自动发现 三层解析一个路径，并记录命中的来源。

    解析不到时 ``path`` 为 ``None``（可选依赖的 SKIP 语义）；``source`` 仍如实记录
    是哪一层被选中却没落到有效路径上。
    """
    for value, source in ((explicit, SOURCE_EXPLICIT),
                          (os.environ.get(env_name) if env_name else None, SOURCE_ENV)):
        if not _is_set(value):
            continue
        requested = _clean(value)
        if requested.exists():
            return ResolvedPath(requested, source, env_var=env_name)
        return ResolvedPath(None, source, env_var=env_name, requested=requested)
    for root in search_roots:
        candidate = Path(root) / relative_name
        if candidate.exists():
            return ResolvedPath(candidate.resolve(), SOURCE_DISCOVER, env_var=env_name)
    return ResolvedPath(None, SOURCE_MISSING, env_var=env_name)


def resolve_target(target: str, explicit=None) -> ResolvedPath:
    """按目标名解析（带来源记录）。目标名写错直接抛错，不静默返回 None。"""
    if target not in TARGETS:
        raise ValueError(f"未知路径目标: {target}（可选 {sorted(TARGETS)}）")
    env_name, relative_name = TARGETS[target]
    res = resolve_optional_path(
        explicit, env_name, default_search_roots(target), relative_name)
    return replace(res, relative_name=relative_name)


def resolve_quick_validate(explicit=None) -> Path | None:
    """官方 skill-creator 校验器；可选依赖，解析不到返回 None（调用方记 SKIP）。"""
    return resolve_target("quick_validate", explicit).path


def resolve_install_root(explicit=None) -> Path:
    """安装版根目录；必需路径，解析不到抛 MissingPathError（调用方记 ERROR）。"""
    return resolve_target("install_root", explicit).require()


def resolve_host_db(explicit=None) -> Path | None:
    """宿主 CLI 数据库；可选依赖（只有冒烟取证会用到），解析不到返回 None。"""
    return resolve_target("host_db", explicit).path


def resolve_gitguard_root(explicit=None) -> Path | None:
    """柠檬治理仓（跨仓依赖）；解析不到返回 None，两仓交叉断言按 SKIP 处理。"""
    return resolve_target("gitguard_root", explicit).path
