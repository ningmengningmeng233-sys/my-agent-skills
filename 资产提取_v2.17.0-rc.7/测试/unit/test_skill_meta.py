#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技能版本单一权威口径的机械守卫（S4 工单：契约声明块不得污染版本治理）。

背景：`<!-- pipeline-contract:begin … end -->` 机器声明块里合法地写着
`version: "1.0.0"`（**契约/schema 版本**）与 `skill_version`，而版本治理测试
要盯的是 **技能自身** 的版本。两者混在一起会让 `^version:` 抓到 schema 版本、
让正文版本号统计凭空多出几个数字。

因此口径固定为：
- 技能版本 = frontmatter 的 `version`（顶层或 `metadata.version`）；
- 正文版本号扫描只看**散文字符串**，契约声明块不参与；
- 契约声明块仍是 SKILL.md 的一部分，删除它必须显式改这里。
"""
from pathlib import Path

import pytest

import skill_meta  # noqa: E402  (conftest 已把 scripts/ 加入 sys.path)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKILL = REPO_ROOT / "SKILL.md"

SAMPLE = """---
name: 示例
metadata:
  version: "9.8.7-rc.6"
---

# 正文

> **版本：v9.8.7-rc.6**

<!-- pipeline-contract:begin
id: demo
role: producer
schema: schemas/demo.schema.json
version: "1.0.0"
skill_version: "9.8.7-rc.6"
pipeline-contract:end -->

结尾说明文字。
"""


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_version_reads_metadata_version_field():
    assert skill_meta.skill_version(SAMPLE) == "9.8.7-rc.6"


def test_skill_version_accepts_top_level_field():
    assert skill_meta.skill_version('---\nversion: "1.2.3"\n---\n正文\n') == "1.2.3"


def test_skill_version_missing_raises():
    with pytest.raises(ValueError):
        skill_meta.skill_version("---\nname: x\n---\n正文\n")


def test_contract_blocks_do_not_count_as_prose_versions():
    assert skill_meta.prose_versions(SAMPLE) == ["v9.8.7-rc.6"]


def test_strip_contract_blocks_keeps_surrounding_prose():
    stripped = skill_meta.strip_contract_blocks(SAMPLE)
    assert "结尾说明文字。" in stripped and "版本：v9.8.7-rc.6" in stripped
    assert "pipeline-contract" not in stripped


def test_entry_has_single_skill_version_marker():
    markers = skill_meta.prose_versions(_skill_text())
    assert len(markers) == 1, f"主入口正文版本号不唯一：{markers}"
    assert skill_meta.strip_version_prefix(markers[0]) == skill_meta.skill_version(_skill_text())


def test_entry_declares_contract_blocks_and_schema_versions():
    """契约声明块必须仍在主入口（schemas/contract_check.py 只读 SKILL.md）。"""
    blocks = skill_meta.contract_blocks(_skill_text())
    assert blocks, "主入口缺 pipeline-contract 声明块"
    assert all(b.get("version") for b in blocks), "声明块缺 schema 版本"
    assert all(b.get("skill_version") == skill_meta.skill_version(_skill_text()) for b in blocks), \
        "声明块登记的技能版本与 frontmatter 不一致"
