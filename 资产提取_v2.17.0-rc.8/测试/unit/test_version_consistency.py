#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F2.1：发布元数据一致性——SKILL/README/pyproject 版本语义相同，无占位来源。"""
import re
import unittest
from pathlib import Path

import skill_meta

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _norm(v: str) -> tuple[str, int | None]:
    """归一化版本：'2.14.0-rc.1'/'2.14.0rc.1'/'2.14.0rc1' → ('2.14.0', 1)；'2.14.0' → ('2.14.0', None)。"""
    v = (v or "").strip().strip('"').strip("'")
    m = re.match(r"^(\d+\.\d+\.\d+)(?:-?rc\.?(\d+))?$", v)
    if not m:
        raise ValueError(f"无法解析版本号: {v!r}")
    return m.group(1), (int(m.group(2)) if m.group(2) else None)


def _skill_version() -> str:
    text = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
    return skill_meta.skill_version(text)


def _readme_version() -> str:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"版本\s*([0-9][0-9a-zA-Z.\-]*)", text)
    if not m:
        raise ValueError("README.md 缺「版本 X」标识")
    return m.group(1)


def _pyproject_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise ValueError("pyproject.toml 缺 version 字段")
    return m.group(1)


class TestVersionConsistency(unittest.TestCase):
    def test_versions_semantically_equal(self):
        candidates = {
            "SKILL.md": _skill_version(),
            "README.md": _readme_version(),
            "pyproject.toml": _pyproject_version(),
        }
        normalized = {k: _norm(v) for k, v in candidates.items()}
        self.assertEqual(len(set(normalized.values())), 1,
                         f"版本语义不一致: {candidates}")

    def test_skill_declares_exactly_one_version_and_it_matches(self):
        """主入口只保留一处版本声明（阶段 8.1 瘦身）：正文版本行必须与 frontmatter 一致。"""
        text = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
        found = {skill_meta.strip_version_prefix(v) for v in skill_meta.prose_versions(text)}
        self.assertEqual(len(found), 1, f"主入口出现多个版本号：{sorted(found)}")
        self.assertEqual(_norm(found.pop()), _norm(_skill_version()),
                         "主入口正文版本与 frontmatter 不一致")

    def test_no_placeholder_project_url(self):
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("yourname", text, "pyproject.toml 仍含占位仓库地址")


if __name__ == "__main__":
    unittest.main()
