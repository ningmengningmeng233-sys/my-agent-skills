#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 6 工单 6.1 / 6.5：主模型审核的可移植往返与机器命令。

完整闭环（全程离线、零真实 API）：
    生成（不含终审）→ 等待审核 → review-export → 主模型读图 → review-import → publish → 交付门
"""
import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.review_contract import (  # noqa: E402
    CHECKLIST_ITEMS,
    REVIEW_PROMPT_STALE,
    ReviewContractError,
    result_path,
)
from scripts.engine.review_provider import (  # noqa: E402
    FileReviewProvider,
    import_review_decision,
)
from scripts.engine.visual_schema import (  # noqa: E402
    AttemptRecord,
    VisualPlan,
    VisualTask,
    load_visual_plan,
    write_visual_plan,
)

VISUAL = REPO_ROOT / "scripts" / "visual_production.py"
TASK_ID = "CH_林姑娘-正脸-v01"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")


def _lib(tmp_path):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    rel = f"视觉资产/任务/{TASK_ID}/attempt-01/image.png"
    target = lib / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG)
    task = VisualTask(task_id=TASK_ID, asset_id="CH_林姑娘", kind="正脸", status="WAITING_REVIEW",
                      aspect_ratio="1:1", style_slug="仙侠国风电影CG", style_version="1.0.0",
                      style_hash="a" * 64, prompt="【任务目标】正脸身份近景",
                      negative_prompt="禁止多视图", fact_source=".process/资产索引.json#CH_林姑娘",
                      attempts=(AttemptRecord(round=1, image=rel,
                                              image_sha256=hashlib.sha256(PNG).hexdigest(),
                                              model="t", provider="t"),))
    write_visual_plan(VisualPlan(schema_version=1, style_name="仙侠国风电影CG",
                                 style_slug="仙侠国风电影CG", style_version="1.0.0",
                                 style_hash="a" * 64, tasks=(task,)),
                      lib / "视觉资产" / "计划.toml")
    return lib


def _result_payload(request, decision="ACCEPTED"):
    return {
        "schema_version": 1, "task_id": request.task_id, "decision": decision,
        "binding": request.binding.as_dict(),
        "checks": [{"item": item, "verdict": "pass", "evidence": "目视通过"}
                   for item in CHECKLIST_ITEMS],
        "issues": [], "reviewer": "当前主模型", "reviewed_at": "2026-09-18T00:00:00Z",
    }


def _cli(*args):
    import os

    base = {**os.environ, "ASSET_WORKERS": "4"}
    base.pop("GEMINI_API_KEY", None)
    return run_py([str(VISUAL), *[str(a) for a in args]], cwd=str(REPO_ROOT), env=base)


# --- 1. Provider：导出、收集、拒收 --------------------------------------------------

def test_prepare_writes_request(tmp_path):
    lib = _lib(tmp_path)
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    path = FileReviewProvider(lib, plan).prepare(plan.by_id(TASK_ID))
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["binding"]["task_id"] == TASK_ID
    assert len(payload["binding"]) == 6


def test_collect_returns_none_without_result(tmp_path):
    lib = _lib(tmp_path)
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    assert FileReviewProvider(lib, plan).collect(plan.by_id(TASK_ID)) is None


def test_collect_validates_binding(tmp_path):
    lib = _lib(tmp_path)
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    provider = FileReviewProvider(lib, plan)
    request_path = provider.prepare(plan.by_id(TASK_ID))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    out = result_path(lib, TASK_ID)
    stub = type("R", (), {"task_id": TASK_ID, "binding": _binding(request)})
    out.write_text(json.dumps(_result_payload(stub), ensure_ascii=False),
                   encoding="utf-8", newline="\n")
    result = provider.collect(plan.by_id(TASK_ID))
    assert result is not None and result.is_accepted


def _binding(request_payload):
    from scripts.engine.visual_schema import ReviewBinding

    data = request_payload["binding"]
    return ReviewBinding(**data)


def test_collect_rejects_stale_result(tmp_path):
    lib = _lib(tmp_path)
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    provider = FileReviewProvider(lib, plan)
    request_path = provider.prepare(plan.by_id(TASK_ID))
    request_payload = json.loads(request_path.read_text(encoding="utf-8"))
    out = result_path(lib, TASK_ID)
    out.write_text(json.dumps(_result_payload(
        type("R", (), {"task_id": TASK_ID, "binding": _binding(request_payload)})()),
        ensure_ascii=False), encoding="utf-8", newline="\n")
    # 审核后又改了提示词 → 绑定失效
    from dataclasses import replace

    changed = replace(plan.by_id(TASK_ID), prompt="审核后偷改的提示词")
    with pytest.raises(ReviewContractError) as ctx:
        provider.collect(changed)
    assert ctx.value.code == REVIEW_PROMPT_STALE


def test_import_decision_maps_three_outcomes():
    task = VisualTask(task_id=TASK_ID, prompt="x")
    plan = VisualPlan(schema_version=1, tasks=(task,))
    for decision, expected in (("ACCEPTED", "ACCEPTED"), ("PASS_WITH_NOTES", "ACCEPTED"),
                               ("NEEDS_REVISION", "NEEDS_REVISION"),
                               ("NEEDS_HUMAN_DECISION", "NEEDS_HUMAN_DECISION")):
        result = type("R", (), {"decision": decision})()
        assert import_review_decision(plan, task, result).status == expected


# --- 2. CLI 闭环（离线） -----------------------------------------------------------

def test_cli_review_export_and_import_and_publish(tmp_path):
    lib = _lib(tmp_path)
    exported = _cli("review-export", "--lib", lib, "--task", TASK_ID)
    assert exported.returncode == 0, exported.stdout + exported.stderr
    request_path = lib / "视觉资产" / "审核请求" / TASK_ID / "review-request.json"
    assert request_path.is_file()

    request_payload = json.loads(request_path.read_text(encoding="utf-8"))
    result_file = tmp_path / "主模型结果.json"
    result_file.write_text(json.dumps(
        _result_payload(type("R", (), {"task_id": TASK_ID, "binding": _binding(request_payload)})()),
        ensure_ascii=False), encoding="utf-8", newline="\n")

    imported = _cli("review-import", "--lib", lib, "--task", TASK_ID, "--result", result_file)
    assert imported.returncode == 0, imported.stdout + imported.stderr
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    assert plan.by_id(TASK_ID).status == "ACCEPTED", "导入后仍未发布"

    published = _cli("publish", "--lib", lib, "--task", TASK_ID)
    assert published.returncode == 0, published.stdout + published.stderr
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    assert plan.by_id(TASK_ID).status == "READY_AS_REFERENCE"
    assert plan.by_id(TASK_ID).current_version == "attempt-01"
    assert (lib / "视觉资产" / "正式版本.json").is_file()


def test_cli_review_import_rejects_stale_result(tmp_path):
    lib = _lib(tmp_path)
    _cli("review-export", "--lib", lib, "--task", TASK_ID)
    request_payload = json.loads(
        (lib / "视觉资产" / "审核请求" / TASK_ID / "review-request.json").read_text(encoding="utf-8"))
    payload = _result_payload(
        type("R", (), {"task_id": TASK_ID, "binding": _binding(request_payload)})())
    payload["binding"]["prompt_sha256"] = "0" * 64
    result_file = tmp_path / "旧结果.json"
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    r = _cli("review-import", "--lib", lib, "--task", TASK_ID, "--result", result_file)
    assert r.returncode != 0
    assert REVIEW_PROMPT_STALE in (r.stdout + r.stderr)
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    assert plan.by_id(TASK_ID).status == "WAITING_REVIEW", "拒绝写回状态"


def test_cli_publish_without_result_fails(tmp_path):
    lib = _lib(tmp_path)
    r = _cli("publish", "--lib", lib, "--task", TASK_ID)
    assert r.returncode != 0
    assert "审核" in (r.stdout + r.stderr)


def test_cli_review_needs_revision_does_not_publish(tmp_path):
    lib = _lib(tmp_path)
    _cli("review-export", "--lib", lib, "--task", TASK_ID)
    request_payload = json.loads(
        (lib / "视觉资产" / "审核请求" / TASK_ID / "review-request.json").read_text(encoding="utf-8"))
    payload = _result_payload(
        type("R", (), {"task_id": TASK_ID, "binding": _binding(request_payload)})(),
        decision="NEEDS_REVISION")
    result_file = tmp_path / "返工.json"
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    r = _cli("review-import", "--lib", lib, "--task", TASK_ID, "--result", result_file)
    assert r.returncode == 0, r.stdout + r.stderr
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    assert plan.by_id(TASK_ID).status == "NEEDS_REVISION"
    assert not (lib / "视觉资产" / "正式版本.json").exists()
