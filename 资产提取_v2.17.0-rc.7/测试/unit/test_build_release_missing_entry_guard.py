# -*- coding: utf-8 -*-
"""发布暂存的「缺失即中止」守卫（补充工单 C01）。

缺陷现场：`plan_staging` 只对 5 个手写名字报错，其余白名单条目**不存在就静默 continue**。
`run_pipeline.bat` 正是这样从 cand4 分享包里无声消失的——白名单点名了它，源树里没有它，
装包一路绿灯，收件人拿不到 Windows 一键入口，而 MUST_SHIP 守卫只在看仓库根时才响。
本文件钉两条：①根级具名文件缺失必须中止并点名；②目录类条目仍按可选处理（不因某代源没有
可选目录就炸），以免把「可选下游模块」的老口径改坏。
"""
from __future__ import annotations

import pathlib

import build_release as br
import pytest

ROOT_FILES = ["SKILL.md", "README.md", "QUICKSTART.md", "FAQ.md", "CONTEXT.md",
              "INSTALL.md",
              "pyproject.toml", "requirements.txt", ".env.example", ".flake8",
              "run_pipeline.sh", "run_pipeline.bat", "llm_nodes.json.example", "LICENSE"]


def fake_source(tmp_path: pathlib.Path, omit=()) -> pathlib.Path:
    src = tmp_path / "skill"
    src.mkdir(parents=True)
    for name in ROOT_FILES:
        if name in omit:
            continue
        p = src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\nname: 素材提取器\nmetadata:\n  version: \"2.17.0-rc.5\"\n---\n# x\n"
                     if name == "SKILL.md" else "占位\n", encoding="utf-8")
    for d in ("scripts", "测试", "docs", "references", "vendor", "templates", "服饰设计"):
        if d in omit:
            continue
        (src / d).mkdir(parents=True, exist_ok=True)
        (src / d / "a.py").write_text("x = 1\n", encoding="utf-8")
    return src


def test_private_profile_finds_all_present_entries(tmp_path):
    files, _excluded = br.plan_staging(fake_source(tmp_path), profile="private")
    names = {p.name for p in files}
    assert "run_pipeline.bat" in names and "SKILL.md" in names


def test_missing_root_entry_aborts_instead_of_silently_skipping(tmp_path):
    src = fake_source(tmp_path, omit=("run_pipeline.bat",))
    with pytest.raises(SystemExit) as e:
        br.plan_staging(src, profile="private")
    assert "白名单条目缺失" in str(e.value) and "run_pipeline.bat" in str(e.value)


def test_missing_directory_entry_stays_optional(tmp_path):
    """可选下游目录缺失不该中止（老口径：视觉包本身是可选下游模块）。"""
    src = fake_source(tmp_path, omit=("服饰设计",))
    files, _ = br.plan_staging(src, profile="private")
    assert files
