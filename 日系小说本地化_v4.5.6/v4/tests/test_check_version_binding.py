# -*- coding: utf-8 -*-
"""Batch 3 TDD：检查报告与不可变版本强绑定（checks/vNNN.json），approve 核验绑定。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest



def _ruleset_sha256() -> str:
    from v4.core import fingerprints

    return fingerprints.rules_fingerprint()


def _chapter_with_version(tmp_path: Path, *, text: str = "第一章\n\n正文。") -> Path:
    """构造一个带 v001 版本文件的章节目录（不含检查绑定）。"""
    from v4.core.version_store import save_version

    chapter_dir = tmp_path / "chapters" / "ch-0001"
    chapter_dir.mkdir(parents=True, exist_ok=True)
    save_version(chapter_dir, text, kind="draft")
    (chapter_dir / "output.md").write_text(text, encoding="utf-8")
    return chapter_dir


def test_approve_rejects_version_without_binding(tmp_path: Path):
    """旧版本没有 checks/vNNN.json 绑定报告时，approve 必须被拒绝。"""
    from v4.core.version_store import approve_version

    chapter_dir = _chapter_with_version(tmp_path)
    with pytest.raises(ValueError, match="检查报告|绑定"):
        approve_version(chapter_dir, "v001")


def test_approve_rejects_when_output_hash_mismatch(tmp_path: Path):
    """检查报告与版本正文哈希不一致（版本被篡改）时，approve 必须被拒绝。"""
    from v4.core.version_store import approve_version

    chapter_dir = _chapter_with_version(tmp_path)
    checks_dir = chapter_dir / "checks"
    checks_dir.mkdir(parents=True, exist_ok=True)
    binding = {
        "schema_version": "v4-checks-1",
        "chapter_id": "ch-0001",
        "version": "v001",
        "output_sha256": "0" * 64,
        "prompt_run_id": None,
        "ruleset_sha256": "b" * 64,
    }
    (checks_dir / "v001.json").write_text(
        json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="哈希不一致"):
        approve_version(chapter_dir, "v001")


def test_approve_accepts_bound_version(tmp_path: Path):
    """绑定报告与版本正文哈希一致时，approve 成功并写入批准指针。"""
    from v4.core.version_store import approve_version

    chapter_dir = _chapter_with_version(tmp_path)
    text = (chapter_dir / "versions" / "v001.md").read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    checks_dir = chapter_dir / "checks"
    checks_dir.mkdir(parents=True, exist_ok=True)
    binding = {
        "schema_version": "v4-checks-1",
        "chapter_id": "ch-0001",
        "version": "v001",
        "output_sha256": digest,
        "prompt_run_id": "pr-abc123def456",
        "ruleset_sha256": "b" * 64,
    }
    (checks_dir / "v001.json").write_text(
        json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    record = approve_version(chapter_dir, "v001")
    assert record["approved_version"] == 1
    assert record["approved_sha256"] == digest


def test_record_checks_writes_versioned_binding(tmp_path: Path):
    """record_checks 必须额外写 checks/vNNN.json，强绑定 version/output_sha256/
    prompt_run_id/ruleset_sha256；checks.json 派生视图保留。"""
    from v4.core import checks, rewrite
    from v4.core.manifest import ManifestStore
    from v4.tests._helpers import _build_workspace, _make_chapter

    ws = _build_workspace(tmp_path, [])
    chapter = _make_chapter(
        ws, chapter_id="ch-0001", ordinal=1, title="第一章 相遇",
        state="ready_for_rewrite",
    )
    from v4.core.jsonio import write_json_atomic, read_json

    manifest = read_json(ws / "manifest.json")
    manifest["chapters"] = [chapter]
    write_json_atomic(ws / "manifest.json", manifest)

    ctx = rewrite.begin_rewrite(ws, "ch-0001")
    text = rewrite.rewrite_text(ctx)
    rewrite.commit_output(ws, "ch-0001", text)

    # 伪造一个 prompt manifest，绑定应拾取最新 prompt_run_id
    prompts_dir = ws / "chapters" / "ch-0001" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "pr-fedcba987654.json").write_text(
        json.dumps({"prompt_run_id": "pr-fedcba987654"}, ensure_ascii=False), encoding="utf-8"
    )

    manifest = ManifestStore(ws).load()
    recorded = manifest["chapters"][0]["fingerprints"]
    input_hashes = {
        key: recorded[key]
        for key in ("source", "events", "project_decisions", "chapter_decisions", "rules")
    }
    doc = checks.run_checks(
        chapter_id="ch-0001",
        source_text=ctx.source_text,
        output_text=text,
        events_graph=ctx.events,
        project_decisions=ctx.project_decisions,
        chapter_decisions=ctx.chapter_decisions,
        input_hashes=input_hashes,
    )
    checks.record_checks(ws, "ch-0001", doc)

    binding_path = ws / "chapters" / "ch-0001" / "checks" / "v001.json"
    assert binding_path.is_file(), "检查绑定报告 checks/v001.json 必须落盘"
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    assert binding["version"] == "v001"
    assert binding["output_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert binding["prompt_run_id"] == "pr-fedcba987654"
    assert binding["ruleset_sha256"] == _ruleset_sha256()
    # checks.json 派生视图仍存在
    assert (ws / "chapters" / "ch-0001" / "checks.json").is_file()
