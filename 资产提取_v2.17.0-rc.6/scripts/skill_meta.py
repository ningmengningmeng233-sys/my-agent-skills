#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SKILL.md 版本口径的单一实现：技能版本、正文版本标记、契约声明块。

存在的原因：`<!-- pipeline-contract:begin … end -->` 机器声明块里合法地写着
`version: "1.0.0"`（schema 版本）与 `skill_version`，任何在整份文件上跑
`^version:` 或版本号统计的治理脚本都会抓错对象。这里把两类口径彻底分开，
让版本一致性测试、审计脚本与发布器共用同一份解析逻辑。
"""
from __future__ import annotations

import re

CONTRACT_RE = re.compile(
    r"^[ \t]*<!--\s*pipeline-contract:begin\s*(.*?)\s*pipeline-contract:end\s*-->[ \t]*\n?",
    re.S | re.M,
)
VERSION_LINE_RE = re.compile(r"^[ \t]*version:[ \t]*(.+?)[ \t]*$", re.M)
SEMVER_RE = re.compile(r"v?\d+\.\d+\.\d+(?:[-.]?rc\.?\d*)?", re.I)


def frontmatter(text: str) -> str:
    """返回 YAML frontmatter 正文（不含 `---` 分隔行）；无 frontmatter 时返回空串。"""
    if not text.startswith("---"):
        return ""
    parts = text.split("---", 2)
    return parts[1] if len(parts) == 3 else ""


def body(text: str) -> str:
    """返回去掉 frontmatter 后的主入口正文。"""
    if not text.startswith("---"):
        return text
    return text.split("---", 2)[-1]


def strip_contract_blocks(text: str) -> str:
    return CONTRACT_RE.sub("", text)


def contract_blocks(text: str) -> list[dict[str, str]]:
    """解析声明块的顶层标量字段（只取 `key: value`，嵌套列表原样忽略）。"""
    blocks: list[dict[str, str]] = []
    for raw in CONTRACT_RE.finditer(text):
        fields: dict[str, str] = {}
        for line in raw.group(1).splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
            if m:
                fields[m.group(1)] = m.group(2).strip().strip('"').strip("'")
        blocks.append(fields)
    return blocks


def skill_version(text: str) -> str:
    """技能版本 = frontmatter 里唯一的 `version` 声明（顶层或 metadata 下）。"""
    found = [m.group(1).strip().strip('"').strip("'")
             for m in VERSION_LINE_RE.finditer(frontmatter(text))]
    if not found:
        raise ValueError("SKILL.md frontmatter 缺 version 字段")
    unique = set(found)
    if len(unique) > 1:
        raise ValueError(f"frontmatter 出现多个不一致的 version：{sorted(unique)}")
    return found[0]


def prose_versions(text: str) -> list[str]:
    """正文里出现的版本号字面量（去重排序），契约声明块不计入。"""
    scanned = strip_contract_blocks(body(text))
    return sorted({m.group(0) for m in SEMVER_RE.finditer(scanned)})


def strip_version_prefix(marker: str) -> str:
    return re.sub(r"^[vV]", "", marker)
