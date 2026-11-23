#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布物完整性守卫（缺陷 F34）。

实测根因：`build_release.WHITELIST` 是一份**手写的"什么属于这个技能"清单**，
它在 T9 往仓库根加 `run_pipeline.sh`/`.bat`、T13 加 `llm_nodes.json.example` 之前就写好了，
之后再没同步过。后果是这三件交付物**既不进发布包、也没有任何测试会响**——
`python scripts/build_release.py` 一路绿灯出包，而装包的人拿不到一键入口。
更阴的连带：包内 `测试/unit/test_run_pipeline_script.py` 用相对路径跑
`bash run_pipeline.sh`，在仓库里能过（文件在），在解压出来的包里那个文件根本不存在；
发布门是在仓库里跑的，所以永远看不到这件事。

本文件钉两条：
① 仓库根凡是"可交付外形"的文件（.sh / .bat / *.example）必须登记进 MUST_SHIP；
② MUST_SHIP 里每一项都必须真的出现在发布暂存清单里。
新增根级脚本忘了登记 → ①红；登记了但白名单没加 → ②红。
"""
from pathlib import Path

import build_release as br

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 根级交付物：随包出去的。手写清单，故意手写——①号测试负责盯着它别漏项。
MUST_SHIP = (
    "SKILL.md", "README.md", "QUICKSTART.md", "FAQ.md", "CONTEXT.md",
    "INSTALL.md",
    "pyproject.toml", "requirements.txt", ".env.example", ".flake8",
    "run_pipeline.sh", "run_pipeline.bat", "llm_nodes.json.example",
    "LICENSE",
)

# 什么外形算"交付物"：可执行入口脚本与配置样例。
DELIVERABLE_SUFFIXES = (".sh", ".bat", ".example")
# 无扩展名的交付件外形：许可正文与版本戳最容易被"没有后缀"漏过（缺陷 F-22 现场——
# frontmatter 写着 license: Proprietary，而包里没有 LICENSE，收件人无从判定能否再分发）。
DELIVERABLE_NAMES = {"LICENSE", "VERSION", "NOTICE"}


def _root_files() -> list[str]:
    return sorted(p.name for p in REPO_ROOT.iterdir()
                  if p.is_file() and not p.name.startswith("."))


def test_licence_bearing_root_files_are_registered():
    """仓库根出现 LICENSE/VERSION/NOTICE 这类无扩展名交付件时，必须进 MUST_SHIP。"""
    unregistered = [n for n in _root_files()
                    if n in DELIVERABLE_NAMES and n not in MUST_SHIP]
    assert not unregistered, f"根级许可/版本件未登记进 MUST_SHIP：{unregistered}"


def _shipped(profile: str = "private") -> set[str]:
    plan, _excluded = br.plan_staging(REPO_ROOT, profile)
    out = set()
    for item in plan:
        rel = Path(item).relative_to(REPO_ROOT).as_posix()
        out.add(rel)
    return out


def test_no_root_deliverable_is_undeclared():
    """① 仓库根的新交付物外形必须先登记，否则这里就红。"""
    undeclared = [n for n in _root_files()
                  if n.endswith(DELIVERABLE_SUFFIXES) and n not in MUST_SHIP]
    assert not undeclared, (
        f"仓库根有可交付文件没登记进 MUST_SHIP，它们会静默不随包发布：{undeclared}\n"
        f"处理：加进本文件 MUST_SHIP，并确认 build_release.WHITELIST 也有它。")


def test_all_declared_deliverables_exist_in_repo():
    """防 MUST_SHIP 自己腐坏：登记的每一项都得真的在仓库里。"""
    missing = [n for n in MUST_SHIP if not (REPO_ROOT / n).is_file()]
    assert not missing, f"MUST_SHIP 登记了仓库里不存在的文件（清单已过期）：{missing}"


def test_declared_deliverables_all_ship_in_private_profile():
    """② 每一项都必须出现在 private 发布暂存清单里。"""
    shipped = _shipped("private")
    absent = [n for n in MUST_SHIP if n not in shipped]
    assert not absent, (
        f"以下根级交付物不在 private 发布包内：{absent}\n"
        f"根因几乎总是 build_release.WHITELIST 没点名它们（F34 的老路）。")


def test_one_click_entry_actually_ships():
    """一键入口是 F34 的当事物，单独立一条：它必须随包出去。"""
    shipped = _shipped("private")
    assert "run_pipeline.sh" in shipped, "一键入口 run_pipeline.sh 没进包——用户拿不到 T9 交付的能力"
    assert "run_pipeline.bat" in shipped, "Windows 一键入口没进包"


def test_llm_nodes_example_ships():
    """节点池样例是 T13 分档能力的唯一配置入口，没它用户无法启用分档。"""
    shipped = _shipped("private")
    assert "llm_nodes.json.example" in shipped, (
        "llm_nodes.json.example 没进包：分档模型只落在代码里，装包的人无从配置 roles")
