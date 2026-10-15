#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 2 工单 2.2：审核请求与结果契约（可移植的主模型审核协议）。

必须拒绝：错任务、旧图、旧提示词、旧参考集、旧画风、旧构图卡、缺逐项清单、未知判定；
重复导入同一结果幂等；任何密钥样式字段一律不得写出。
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
    REVIEW_CHECKLIST_INCOMPLETE,
    REVIEW_COMPOSITION_STALE,
    REVIEW_DECISION_UNKNOWN,
    REVIEW_IMAGE_MISMATCH,
    REVIEW_IMAGE_MISSING,
    REVIEW_PATH_UNSAFE,
    REVIEW_PROMPT_STALE,
    REVIEW_REFERENCE_STALE,
    REVIEW_SECRET_FIELD,
    REVIEW_STYLE_STALE,
    REVIEW_TASK_MISMATCH,
    RESULT_NAME,
    ReviewContractError,
    build_review_request,
    load_review_result,
    request_path,
    result_path,
    validate_review_binding,
    write_review_request,
)
from scripts.engine.visual_schema import (  # noqa: E402
    AttemptRecord,
    ReferenceBinding,
    ReviewBinding,
    VisualPlan,
    VisualTask,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")
TASK_ID = "CH_林姑娘-正脸-v01"
STYLE_HASH = "a" * 64
COMPOSITION_REL = "视觉资产/构图卡/CH_林姑娘-正脸-v01.toml"


def _lib(tmp_path):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    return lib


def _task(lib, **kw):
    rel = f"视觉资产/任务/{TASK_ID}/attempt-01/image.png"
    target = lib / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        target.write_bytes(PNG)
    composition = lib / COMPOSITION_REL
    composition.parent.mkdir(parents=True, exist_ok=True)
    composition.write_text('shot_size = "特写"\n', encoding="utf-8", newline="\n")
    base = dict(
        task_id=TASK_ID, asset_id="CH_林姑娘", kind="正脸", status="WAITING_REVIEW",
        aspect_ratio="1:1", style_slug="仙侠国风电影CG", style_version="1.0.0",
        style_hash=STYLE_HASH, prompt="【任务目标】正脸身份近景",
        negative_prompt="禁止多视图", fact_source=".process/资产索引.json#CH_林姑娘",
        purpose="正脸身份近景", script_source="段01.md#00012",
        asset_index_key="CHR:00003:1", storyboard_source="分镜-EP01-014",
        composition_path=COMPOSITION_REL,
        composition_sha256=hashlib.sha256(composition.read_bytes()).hexdigest(),
        references=(ReferenceBinding(path="视觉资产/参考/face.png", role="identity",
                                     sha256="f" * 64),),
        attempts=(AttemptRecord(round=1, image=rel,
                                image_sha256=hashlib.sha256(PNG).hexdigest(),
                                model="gemini-3-pro-image", provider="gemini"),),
    )
    base.update(kw)
    return VisualTask(**base)


def _plan(task):
    return VisualPlan(schema_version=1, style_name="仙侠国风电影CG", style_slug="仙侠国风电影CG",
                      style_version="1.0.0", style_hash=STYLE_HASH, tasks=(task,))


def _result_payload(request, **kw):
    payload = {
        "schema_version": 1,
        "task_id": request.task_id,
        "decision": "ACCEPTED",
        "binding": request.binding.as_dict(),
        "checks": [{"item": item, "verdict": "pass", "evidence": "目视通过"}
                   for item in CHECKLIST_ITEMS],
        "issues": [],
        "reviewer": "当前主模型",
        "reviewed_at": "2026-09-18T00:00:00Z",
    }
    payload.update(kw)
    return payload


def _write_result(lib, payload):
    path = result_path(lib, TASK_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path


# --- 1. 请求：六项哈希现场计算、路径相对、含任务事实 ---------------------------------

def test_build_request_carries_six_hashes_and_facts(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib)
    request = build_review_request(lib, _plan(task), task)
    binding = request.binding.as_dict()
    assert binding["task_id"] == TASK_ID
    assert binding["image_sha256"] == hashlib.sha256(PNG).hexdigest()
    assert binding["prompt_sha256"] and binding["reference_set_sha256"]
    assert binding["style_sha256"] == STYLE_HASH
    assert binding["composition_sha256"]
    assert request.facts["purpose"] == "正脸身份近景"
    assert request.facts["script_source"] == "段01.md#00012"
    assert request.image == f"视觉资产/任务/{TASK_ID}/attempt-01/image.png"
    assert request.checklist_version == 1


def test_build_request_ignores_stale_declared_composition_hash(tmp_path):
    """构图卡文件存在时以**现算**哈希为准：计划里登记的旧哈希不得冒充绑定。"""
    lib = _lib(tmp_path)
    task = _task(lib, composition_sha256="0" * 64)
    request = build_review_request(lib, _plan(task), task)
    assert request.binding.composition_sha256 != "0" * 64


def test_build_request_without_artifact_fails(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib, attempts=())
    with pytest.raises(ReviewContractError) as ctx:
        build_review_request(lib, _plan(task), task)
    assert ctx.value.code == REVIEW_IMAGE_MISSING


def test_build_request_with_missing_file_fails(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib)
    (lib / task.latest_attempt.image).unlink()
    with pytest.raises(ReviewContractError) as ctx:
        build_review_request(lib, _plan(task), task)
    assert ctx.value.code == REVIEW_IMAGE_MISSING


def test_build_request_rejects_absolute_paths(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib, composition_path="C:/别的库/构图卡.toml")
    with pytest.raises(ReviewContractError) as ctx:
        build_review_request(lib, _plan(task), task)
    assert ctx.value.code == REVIEW_PATH_UNSAFE


# --- 2. 写出请求：固定路径、原子、字节稳定 ------------------------------------------

def test_write_request_is_atomic_and_deterministic(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib)
    request = build_review_request(lib, _plan(task), task)
    first = write_review_request(lib, request)
    assert first == request_path(lib, TASK_ID)
    before = first.read_bytes()
    write_review_request(lib, request)
    assert first.read_bytes() == before, "同一请求两次写出必须字节稳定"
    assert list(first.parent.glob("*.tmp")) == []
    assert first.name == "review-request.json"


def test_write_request_rejects_secret_like_facts(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib, prompt="用这个 AIzaSyDdummyKeyValueForTest0000000000 生成")
    with pytest.raises(ReviewContractError) as ctx:
        build_review_request(lib, _plan(task), task)
    assert ctx.value.code == REVIEW_SECRET_FIELD


# --- 3. 校验绑定：六项逐一比对 -----------------------------------------------------

def test_validate_binding_accepts_matching_result(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib)
    request = build_review_request(lib, _plan(task), task)
    write_review_request(lib, request)
    result = load_review_result(_write_result(lib, _result_payload(request)))
    validate_review_binding(request, result)
    assert result.is_accepted


def test_validate_binding_rejects_other_task(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib)
    request = build_review_request(lib, _plan(task), task)
    payload = _result_payload(request, task_id="CH_林姑娘-后脑-v01")
    payload["binding"]["task_id"] = "CH_林姑娘-后脑-v01"
    result = load_review_result(_write_result(lib, payload))
    with pytest.raises(ReviewContractError) as ctx:
        validate_review_binding(request, result)
    assert ctx.value.code == REVIEW_TASK_MISMATCH


@pytest.mark.parametrize("key,code", [
    ("image_sha256", REVIEW_IMAGE_MISMATCH),
    ("prompt_sha256", REVIEW_PROMPT_STALE),
    ("reference_set_sha256", REVIEW_REFERENCE_STALE),
    ("style_sha256", REVIEW_STYLE_STALE),
    ("composition_sha256", REVIEW_COMPOSITION_STALE),
])
def test_validate_binding_rejects_each_stale_hash(tmp_path, key, code):
    lib = _lib(tmp_path)
    task = _task(lib)
    request = build_review_request(lib, _plan(task), task)
    payload = _result_payload(request)
    payload["binding"][key] = "9" * 64
    result = load_review_result(_write_result(lib, payload))
    with pytest.raises(ReviewContractError) as ctx:
        validate_review_binding(request, result)
    assert ctx.value.code == code


# --- 4. 结果契约：判定、逐项清单、幂等 ---------------------------------------------

def test_unknown_decision_rejected(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib)
    request = build_review_request(lib, _plan(task), task)
    path = _write_result(lib, _result_payload(request, decision="LOOKS_FINE"))
    with pytest.raises(ReviewContractError) as ctx:
        load_review_result(path)
    assert ctx.value.code == REVIEW_DECISION_UNKNOWN


def test_pass_with_notes_is_a_legal_decision(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib)
    request = build_review_request(lib, _plan(task), task)
    result = load_review_result(_write_result(lib, _result_payload(request,
                                                                   decision="PASS_WITH_NOTES")))
    assert result.is_accepted, "通过但有备注允许发布"


def test_incomplete_checklist_rejected(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib)
    request = build_review_request(lib, _plan(task), task)
    payload = _result_payload(request)
    payload["checks"] = payload["checks"][:-1]
    with pytest.raises(ReviewContractError) as ctx:
        load_review_result(_write_result(lib, payload))
    assert ctx.value.code == REVIEW_CHECKLIST_INCOMPLETE


def test_missing_checklist_rejected(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib)
    request = build_review_request(lib, _plan(task), task)
    payload = _result_payload(request)
    payload.pop("checks")
    with pytest.raises(ReviewContractError) as ctx:
        load_review_result(_write_result(lib, payload))
    assert ctx.value.code == REVIEW_CHECKLIST_INCOMPLETE


def test_unknown_verdict_rejected(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib)
    request = build_review_request(lib, _plan(task), task)
    payload = _result_payload(request)
    payload["checks"][0]["verdict"] = "maybe"
    with pytest.raises(ReviewContractError) as ctx:
        load_review_result(_write_result(lib, payload))
    assert ctx.value.code == REVIEW_CHECKLIST_INCOMPLETE


def test_reimporting_same_result_is_idempotent(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib)
    request = build_review_request(lib, _plan(task), task)
    path = _write_result(lib, _result_payload(request))
    first = load_review_result(path)
    second = load_review_result(path)
    assert first == second
    validate_review_binding(request, first)
    validate_review_binding(request, second)


def test_result_with_secret_field_rejected(tmp_path):
    lib = _lib(tmp_path)
    task = _task(lib)
    request = build_review_request(lib, _plan(task), task)
    payload = _result_payload(request)
    payload["api_key"] = "not-a-real-key"
    with pytest.raises(ReviewContractError) as ctx:
        load_review_result(_write_result(lib, payload))
    assert ctx.value.code == REVIEW_SECRET_FIELD


def test_result_path_contract(tmp_path):
    lib = _lib(tmp_path)
    assert result_path(lib, TASK_ID) == lib / "视觉资产" / "审核请求" / TASK_ID / RESULT_NAME


def test_binding_from_plan_hashes_differ_when_reference_changes(tmp_path):
    lib = _lib(tmp_path)
    first = build_review_request(lib, _plan(_task(lib)), _task(lib))
    second_task = _task(lib, references=(ReferenceBinding(path="视觉资产/参考/other.png",
                                                          role="identity", sha256="e" * 64),))
    second = build_review_request(lib, _plan(second_task), second_task)
    assert first.binding.reference_set_sha256 != second.binding.reference_set_sha256


def test_review_binding_type_is_shared_with_schema():
    assert ReviewBinding(task_id="t", image_sha256="a" * 64).as_dict()["task_id"] == "t"
