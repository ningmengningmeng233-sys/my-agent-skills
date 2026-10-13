#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 2 工单 2.3：正式版本发布器。

只有发布器能产生正式资产：直接复制图片、手改计划状态、伪造 Markdown 都不行。
发布必须满足「判定通过 + 六项绑定仍匹配 + 图片可读合规 + 上游已发布」；
替换当前版本时旧版进 history（只增不改），直接下游转 NEEDS_REVIEW。
"""
import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.review_contract import (  # noqa: E402
    CHECKLIST_ITEMS,
    build_review_request,
    load_review_result,
    result_path,
)
from scripts.engine.visual_publisher import (  # noqa: E402
    PUBLISH_BINDING_STALE,
    PUBLISH_DECISION_REQUIRED,
    PUBLISH_HASH_STALE,
    PUBLISH_IMAGE_MISSING,
    PUBLISH_MIME_UNSUPPORTED,
    PUBLISH_RELEASE_MISSING,
    PUBLISH_UPSTREAM_INVALID,
    PublishError,
    RELEASE_MANIFEST_NAME,
    current_release,
    history_of,
    load_release_manifest,
    propagate_release_change,
    publish_task,
    validate_release,
)
from scripts.engine.visual_schema import (  # noqa: E402
    AttemptRecord,
    ReferenceBinding,
    VisualPlan,
    VisualTask,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")
TASK_ID = "CH_林姑娘-正脸-v01"
BACK_ID = "CH_林姑娘-后脑-v01"
STYLE_HASH = "a" * 64


def _task(task_id=TASK_ID, kind="正脸", depends_on=(), status="WAITING_REVIEW",
          aspect="1:1", prompt="【任务目标】正脸身份近景", **kw):
    base = dict(
        task_id=task_id, asset_id="CH_林姑娘", kind=kind, status=status,
        aspect_ratio=aspect, style_slug="仙侠国风电影CG", style_version="1.0.0",
        style_hash=STYLE_HASH, prompt=prompt, negative_prompt="禁止多视图",
        fact_source=".process/资产索引.json#CH_林姑娘", purpose="身份近景",
        script_source="段01.md#00012", depends_on=depends_on,
        references=(ReferenceBinding(path="视觉资产/参考/face.png", role="identity",
                                     sha256="f" * 64),),
    )
    base.update(kw)
    return VisualTask(**base)


def _write_image(lib, task_id, round_no, data=PNG):
    rel = f"视觉资产/任务/{task_id}/attempt-{round_no:02d}/image.png"
    target = lib / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return rel


def _with_attempt(task, rel, data=PNG, round_no=1):
    return VisualTask(**{**task.__dict__, "attempts": (
        AttemptRecord(round=round_no, image=rel, image_sha256=hashlib.sha256(data).hexdigest(),
                      model="gemini-3-pro-image", provider="gemini"),)})


def _review(lib, plan, task, decision="ACCEPTED", reviewer="当前主模型"):
    request = build_review_request(lib, plan, task)
    payload = {
        "schema_version": 1, "task_id": request.task_id, "decision": decision,
        "binding": request.binding.as_dict(),
        "checks": [{"item": item, "verdict": "pass", "evidence": "目视通过"}
                   for item in CHECKLIST_ITEMS],
        "issues": [], "reviewer": reviewer, "reviewed_at": "2026-09-18T00:00:00Z",
    }
    path = result_path(lib, task.task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    return request, load_review_result(path)


def _prepared(tmp_path, task=None, decision="ACCEPTED"):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    task = task or _task()
    rel = _write_image(lib, task.task_id, 1)
    task = _with_attempt(task, rel)
    plan = VisualPlan(schema_version=1, style_name="仙侠国风电影CG", style_slug="仙侠国风电影CG",
                      style_version="1.0.0", style_hash=STYLE_HASH, tasks=(task,))
    request, result = _review(lib, plan, task, decision=decision)
    return lib, plan, task, request, result


# --- 1. 正常发布 -------------------------------------------------------------------

def test_publish_writes_current_release_record(tmp_path):
    lib, plan, task, request, result = _prepared(tmp_path)
    updated, record = publish_task(lib, plan, task, request, result,
                                   published_at="2026-09-18T01:00:00Z")
    assert record.version == "attempt-01"
    assert record.decision == "ACCEPTED"
    assert record.mime == "image/png"
    assert (record.width, record.height) == (4, 4)
    assert record.binding["prompt_sha256"] == request.binding.prompt_sha256
    manifest = load_release_manifest(lib)
    assert manifest["tasks"][TASK_ID]["current"]["image_sha256"] == \
        hashlib.sha256(PNG).hexdigest()
    assert (lib / "视觉资产" / RELEASE_MANIFEST_NAME).is_file()
    assert current_release(lib, TASK_ID).version == "attempt-01"
    assert updated.by_id(TASK_ID).status == "READY_AS_REFERENCE"
    assert updated.by_id(TASK_ID).current_version == "attempt-01"


def test_publish_accepts_pass_with_notes(tmp_path):
    lib, plan, task, request, result = _prepared(tmp_path, decision="PASS_WITH_NOTES")
    _, record = publish_task(lib, plan, task, request, result)
    assert record.decision == "PASS_WITH_NOTES", "通过但有备注允许发布并记录在案"


def test_manifest_is_deterministic_json(tmp_path):
    lib, plan, task, request, result = _prepared(tmp_path)
    publish_task(lib, plan, task, request, result, published_at="t")
    first = (lib / "视觉资产" / RELEASE_MANIFEST_NAME).read_bytes()
    publish_task(lib, plan, task, request, result, published_at="t")
    assert (lib / "视觉资产" / RELEASE_MANIFEST_NAME).read_bytes() == first


# --- 2. 发布前置条件 ---------------------------------------------------------------

def test_needs_revision_cannot_publish(tmp_path):
    lib, plan, task, request, result = _prepared(tmp_path, decision="NEEDS_REVISION")
    with pytest.raises(PublishError) as ctx:
        publish_task(lib, plan, task, request, result)
    assert ctx.value.code == PUBLISH_DECISION_REQUIRED
    assert not (lib / "视觉资产" / RELEASE_MANIFEST_NAME).exists()


def test_needs_human_decision_cannot_publish(tmp_path):
    lib, plan, task, request, result = _prepared(tmp_path, decision="NEEDS_HUMAN_DECISION")
    with pytest.raises(PublishError) as ctx:
        publish_task(lib, plan, task, request, result)
    assert ctx.value.code == PUBLISH_DECISION_REQUIRED


def test_stale_binding_cannot_publish(tmp_path):
    """审核后改了提示词：六项绑定对不上，不得发布。"""
    lib, plan, task, request, result = _prepared(tmp_path)
    changed = VisualTask(**{**task.__dict__, "prompt": "审核后偷改的提示词"})
    with pytest.raises(PublishError) as ctx:
        publish_task(lib, plan, changed, request, result)
    assert ctx.value.code == PUBLISH_BINDING_STALE


def test_swapped_image_cannot_publish(tmp_path):
    """审核后换图：文件哈希与请求绑定不符，不得发布。"""
    lib, plan, task, request, result = _prepared(tmp_path)
    (lib / task.latest_attempt.image).write_bytes(PNG + b"\x00")
    with pytest.raises(PublishError) as ctx:
        publish_task(lib, plan, task, request, result)
    assert ctx.value.code == PUBLISH_HASH_STALE


def test_garbage_image_cannot_publish(tmp_path):
    lib, plan, task, request, result = _prepared(tmp_path)
    (lib / task.latest_attempt.image).write_bytes(b"definitely-not-an-image" * 3)
    with pytest.raises(PublishError) as ctx:
        publish_task(lib, plan, task, request, result)
    assert ctx.value.code == PUBLISH_MIME_UNSUPPORTED


def test_missing_image_cannot_publish(tmp_path):
    lib, plan, task, request, result = _prepared(tmp_path)
    (lib / task.latest_attempt.image).unlink()
    with pytest.raises(PublishError) as ctx:
        publish_task(lib, plan, task, request, result)
    assert ctx.value.code == PUBLISH_IMAGE_MISSING


def test_unreleased_upstream_blocks_publish(tmp_path):
    """上游没有正式版本时，下游不得发布（不得拿未发布的图当参考）。"""
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    face = _with_attempt(_task(), _write_image(lib, TASK_ID, 1))
    back = _with_attempt(_task(task_id=BACK_ID, kind="后脑", depends_on=(TASK_ID,)),
                         _write_image(lib, BACK_ID, 1))
    plan = VisualPlan(schema_version=1, style_slug="仙侠国风电影CG", style_version="1.0.0",
                      style_hash=STYLE_HASH, tasks=(face, back))
    request, result = _review(lib, plan, back)
    with pytest.raises(PublishError) as ctx:
        publish_task(lib, plan, back, request, result)
    assert ctx.value.code == PUBLISH_UPSTREAM_INVALID


def test_published_upstream_allows_downstream(tmp_path):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    face = _with_attempt(_task(), _write_image(lib, TASK_ID, 1))
    back = _with_attempt(_task(task_id=BACK_ID, kind="后脑", depends_on=(TASK_ID,)),
                         _write_image(lib, BACK_ID, 1))
    plan = VisualPlan(schema_version=1, style_slug="仙侠国风电影CG", style_version="1.0.0",
                      style_hash=STYLE_HASH, tasks=(face, back))
    req_face, res_face = _review(lib, plan, face)
    plan, _ = publish_task(lib, plan, face, req_face, res_face)
    req_back, res_back = _review(lib, plan, back)
    _, record = publish_task(lib, plan, back, req_back, res_back)
    assert record.version == "attempt-01"


# --- 3. 替换当前版本：旧版进 history，下游转 NEEDS_REVIEW ---------------------------

def test_replacing_current_keeps_history(tmp_path):
    lib, plan, task, request, result = _prepared(tmp_path)
    plan, first = publish_task(lib, plan, task, request, result, published_at="t1")

    rel2 = _write_image(lib, TASK_ID, 2, data=PNG + b"\x00")
    task2 = _with_attempt(VisualTask(**{**task.__dict__, "current_version": "attempt-01"}),
                          rel2, data=PNG + b"\x00", round_no=2)
    plan = plan.with_task(task2)
    request2, result2 = _review(lib, plan, task2)
    plan, second = publish_task(lib, plan, task2, request2, result2, published_at="t2")

    assert second.version == "attempt-02"
    assert current_release(lib, TASK_ID).version == "attempt-02"
    history = history_of(lib, TASK_ID)
    assert [h.version for h in history] == ["attempt-01"], "旧版必须进入历史"
    assert history[0].image_sha256 == hashlib.sha256(PNG).hexdigest()
    assert history[0].published_at == "t1"


def test_downstream_needs_review_after_replacement(tmp_path):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    face = _with_attempt(_task(), _write_image(lib, TASK_ID, 1))
    back = _with_attempt(_task(task_id=BACK_ID, kind="后脑", depends_on=(TASK_ID,)),
                         _write_image(lib, BACK_ID, 1))
    plan = VisualPlan(schema_version=1, style_slug="仙侠国风电影CG", style_version="1.0.0",
                      style_hash=STYLE_HASH, tasks=(face, back))
    req_face, res_face = _review(lib, plan, face)
    plan, _ = publish_task(lib, plan, face, req_face, res_face)
    req_back, res_back = _review(lib, plan, back)
    plan, _ = publish_task(lib, plan, back, req_back, res_back)
    assert plan.by_id(BACK_ID).is_accepted

    updated = propagate_release_change(plan, TASK_ID)
    assert updated.by_id(BACK_ID).status == "NEEDS_REVIEW", "上游换版后下游要重新审核"
    assert updated.by_id(BACK_ID).attempts, "旧图保留（只取消参考资格）"


# --- 4. 交叉验证（交付门用） -------------------------------------------------------

def test_validate_release_flags_handwritten_status(tmp_path):
    """只改计划状态、没走发布器 → 交付门交叉验证必须报缺发布记录。"""
    lib, plan, task, request, result = _prepared(tmp_path)
    handwritten = VisualTask(**{**task.__dict__, "status": "READY_AS_REFERENCE"})
    plan = VisualPlan(**{**plan.__dict__, "tasks": (handwritten,)})
    problems = validate_release(lib, plan)
    assert [(tid, code) for tid, code, _ in problems] == [(TASK_ID, PUBLISH_RELEASE_MISSING)]


def test_validate_release_flags_missing_and_drifted_images(tmp_path):
    lib, plan, task, request, result = _prepared(tmp_path)
    plan, _ = publish_task(lib, plan, task, request, result)
    assert validate_release(lib, plan) == []

    (lib / task.latest_attempt.image).write_bytes(PNG + b"drift")
    problems = validate_release(lib, plan)
    assert [code for _, code, _ in problems] == [PUBLISH_HASH_STALE]

    (lib / task.latest_attempt.image).unlink()
    problems = validate_release(lib, plan)
    assert [code for _, code, _ in problems] == [PUBLISH_IMAGE_MISSING]


def test_validate_release_ignores_unpublished_pending_tasks(tmp_path):
    lib, plan, task, request, result = _prepared(tmp_path)
    assert validate_release(lib, plan) == [], "未通过的任务不参与正式版本交叉验证"
