# -*- coding: utf-8 -*-
"""技能优化（skill-optimization）文档完整性与引用一致性门禁测试。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_skill_frontmatter_and_references_exist():
    skill_md = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.startswith("---"), "SKILL.md 必须以 YAML frontmatter 开头"
    front = skill_md.split("---", 2)[1]
    assert re.search(r"(?m)^name:\s*skill-optimization\s*$", front)
    assert re.search(r"(?m)^\s*version:\s*[\"']?\d+\.\d+\.\d+", front)

    # All markdown links to references/*.md must resolve
    refs = set(re.findall(r"\]\((references/[^)#\s]+)\)", skill_md))
    assert refs, "SKILL.md 未检测到 references/ 链接"
    missing = [r for r in sorted(refs) if not (ROOT / r).is_file()]
    assert not missing, f"引用缺失：{missing}"


def test_upstream_ponytail_snapshot_and_license_present():
    audit_file = ROOT / "references" / "ponytail-audit.md"
    lic_file = ROOT / "LICENSE-PONYTAIL"
    assert audit_file.is_file(), "缺少上游 ponytail-audit.md 快照"
    assert lic_file.is_file(), "缺少上游 LICENSE-PONYTAIL"
    txt = audit_file.read_text(encoding="utf-8")
    assert "name: ponytail-audit" in txt
    assert "## 1. Map first" in txt and "## 2. Look for" in txt


def test_reviewer_supports_v5_audit_categories():
    reviewer = (ROOT / "references" / "reviewer.md").read_text(encoding="utf-8")
    for token in ("delete", "reuse", "stdlib", "native", "yagni", "merge", "split", "shrink", "APPROVE", "REVIEW", "REJECT"):
        assert token in reviewer, f"reviewer.md 缺少关键分类或判定：{token}"
