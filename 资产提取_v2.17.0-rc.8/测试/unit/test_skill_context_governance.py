#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前置工单：技能体积与真实上下文治理的长期守卫（配套 文档/柠檬/交接/技能上下文审计.md）。

与既有 `test_skill_router_size.py`（阶段 8 主入口瘦身）互补，本文件钉死**审计量化标准**，
防止工单 0–14 的后续改动悄悄把主入口撑回过大、引入断链、无条件全量加载或历史外泄：

- 启动只加载 `SKILL.md`，且不超过 25KB 硬上限；
- 主入口引用的所有 references/docs/根级 `.md` 路由目标必须存在（无断链）；
- 不得出现「先阅读整个 references」式无条件全量加载指令；
- 主入口不得内联完整 CHANGELOG / ADR / 外部技能全文；
- 主入口版本号单一权威；
- 审计脚本 `scripts/audit_skill_context.py` 的输出与磁盘 `SKILL.md` 字节一致（防漂移），
  且没有任何单个启动加载文本 > 50KB。
"""
import re
import sys
from pathlib import Path
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import audit_skill_context as audit  # noqa: E402

SKILL = REPO_ROOT / "SKILL.md"
HARD_LIMIT = 25 * 1024


def _entry_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _payload() -> dict:
    return audit.run_audit()


def test_boot_loads_only_the_entry_file():
    payload = _payload()
    assert payload["decision"]["boot_load_files"] == ["SKILL.md"]


def test_entry_within_hard_limit():
    size = len(_entry_text().encode("utf-8"))
    assert size <= HARD_LIMIT, f"主入口 {size} 字节超过 25KB 硬上限，需按 C 节拆分"


def test_no_broken_relative_links_in_entry():
    broken = [i["target"] for i in audit.extract_relative_links(_entry_text()) if not i["exists"]]
    assert not broken, f"主入口存在断链路由目标：{broken}"


def test_no_unconditional_full_load_instruction():
    hits = audit.unconditional_read_instructions(_entry_text())
    assert not hits, f"主入口出现无条件全量加载指令：{hits}"


def test_entry_has_no_inlined_changelog_adr_or_external_skill():
    text = _entry_text()
    body = text.split("---", 2)[-1]
    assert "## 更新日志" not in body and "## Changelog" not in body.lower(), \
        "主入口不得内联完整更新日志"
    assert "[ADR-" not in body, "主入口不得内联 ADR 决策正文"
    assert not re.search(r"^---\nname:", body, re.M), "主入口不得内联外部技能 frontmatter"
    assert audit.external_skill_copy()["marker_hits"] == [] or all(
        i["file"].startswith("references/") for i in audit.external_skill_copy()["marker_hits"]
    ), "外部技能正文不得被复制进主入口"


def test_entry_declares_version_once():
    markers = _payload()["entry"]["version_markers"]
    assert len(markers) == 1, f"主入口版本号不唯一：{markers}"


def test_no_boot_text_exceeds_50kb():
    boot = _payload()["decision"]["text_over_50kb"]["boot_loaded"]
    assert boot == [], f"启动加载了超过 50KB 的单个文本：{boot}"


def test_audit_entry_bytes_match_disk(tmp_path):
    out = tmp_path / "audit.json"
    rc = run_py(
            [str(REPO_ROOT / "scripts" / "audit_skill_context.py"), "--out", str(out)],
            cwd=str(REPO_ROOT),
    )
    assert rc.returncode == 0, rc.stdout + rc.stderr
    import json
    data = json.loads(out.read_text(encoding="utf-8"))
    disk = len(_entry_text().encode("utf-8"))
    assert data["entry"]["bytes_total"] == disk, "审计脚本字节数与磁盘主入口不一致（漂移）"


def test_classification_is_recomputable_from_entry():
    classification = _payload()["entry"]["classification"]
    lines = _entry_text().splitlines()
    assert classification["total_lines"] == len(lines), "分类总行数与主入口行数不一致"
    assert set(classification["counts"]).issubset(set(audit.CLASSIFICATION_METHODS) | {"blank", "prose"})
