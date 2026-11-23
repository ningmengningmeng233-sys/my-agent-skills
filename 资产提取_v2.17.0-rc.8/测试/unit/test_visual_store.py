#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单03-Task4：产物仓库与任务绑定（规格 8）。

目录契约：`视觉资产/任务/<task_id>/attempt-NN/{image.png,request.json,response.json,qc.toml}`。
文件名固定，绝不来自模型返回文本或用户原始名称；越界/伪装/空文件/超限/哈希不符一律拒绝。
"""
import base64
import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.image_executor import GenerationResult  # noqa: E402
from scripts.engine.visual_schema import VisualTask  # noqa: E402
from scripts.engine.visual_store import (  # noqa: E402
    MAX_IMAGE_BYTES,
    StoreError,
    attempt_dir,
    import_manual_result,
    save_attempt,
    verify_attempt,
)

# 真实最小的 4x4 PNG（经 Pillow verify() 通过；手写的 1x1 假 PNG 会被校验拒绝）
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9UaAAAAABJRU5ErkJggg=="
)
TASK_ID = "CH_林姑娘-正脸-v01"


def _task(**kw):
    base = dict(task_id=TASK_ID, asset_id="CH_林姑娘", kind="正脸", status="PLANNED",
                aspect_ratio="1:1", style_slug="仙侠国风电影CG", style_version="1.0.0",
                style_hash="a" * 64, fact_source=".process/资产索引.json#CH_林姑娘")
    base.update(kw)
    return VisualTask(**base)


def _result(**kw):
    base = dict(provider="gemini", model="img-model", image_bytes=PNG_BYTES,
                mime_type="image/png", response_id="resp-1",
                usage={"prompt_token_count": 10}, raw_metadata={"attempts": 1}, status="OK")
    base.update(kw)
    return GenerationResult(**base)


def _manifest():
    return {"task_id": TASK_ID, "aspect_ratio": "1:1",
            "references": [{"path": "references/face.png", "role": "identity",
                            "sha256": "b" * 64}]}


def test_directory_contract(tmp_path):
    record = save_attempt(tmp_path, _task(), _result(), _manifest())
    base = tmp_path / "视觉资产" / "任务" / TASK_ID / "attempt-01"
    assert base.is_dir()
    for name in ("image.png", "request.json", "response.json", "qc.toml"):
        assert (base / name).is_file(), name
    assert record.round == 1
    assert record.image == f"视觉资产/任务/{TASK_ID}/attempt-01/image.png"
    assert record.image_sha256 == hashlib.sha256(PNG_BYTES).hexdigest()


def test_file_names_never_come_from_model_text(tmp_path):
    """即使模型响应里带攻击性文件名，落盘名也必须固定。"""
    record = save_attempt(tmp_path, _task(), _result(
        raw_metadata={"filename": "../../evil.png", "path": "C:/Windows/x.png"}),
        _manifest())
    assert record.image.endswith("attempt-01/image.png")
    assert not (tmp_path.parent / "evil.png").exists()
    assert list(tmp_path.rglob("evil.png")) == []


def test_response_metadata_is_sanitized(tmp_path):
    result = _result(raw_metadata={"api_key": "secret-key", "Authorization": "Bearer x",
                                   "headers": {"x-goog-api-key": "secret-key"},
                                   "attempts": 1})
    save_attempt(tmp_path, _task(), result, _manifest())
    text = (tmp_path / "视觉资产" / "任务" / TASK_ID / "attempt-01" / "response.json").read_text(
        encoding="utf-8")
    assert "secret-key" not in text
    assert "Bearer" not in text
    assert "api_key" not in text
    assert "x-goog-api-key" not in text


def test_request_manifest_written_verbatim(tmp_path):
    import json

    save_attempt(tmp_path, _task(), _result(), _manifest())
    data = json.loads((tmp_path / "视觉资产" / "任务" / TASK_ID / "attempt-01" / "request.json")
                      .read_text(encoding="utf-8"))
    assert data["task_id"] == TASK_ID
    assert data["references"][0]["role"] == "identity"
    assert data["references"][0]["sha256"] == "b" * 64


def test_qc_placeholder_written(tmp_path):
    save_attempt(tmp_path, _task(), _result(), _manifest())
    text = (tmp_path / "视觉资产" / "任务" / TASK_ID / "attempt-01" / "qc.toml").read_text(
        encoding="utf-8")
    assert "decision" in text


def test_second_attempt_increments_round(tmp_path):
    save_attempt(tmp_path, _task(), _result(response_id="resp-1"), _manifest())
    record = save_attempt(tmp_path, _task(attempts=(_rec(tmp_path),)),
                          _result(response_id="resp-2"), _manifest())
    assert record.round == 2
    assert record.image.endswith("attempt-02/image.png")


def _rec(tmp_path):
    from scripts.engine.visual_schema import AttemptRecord

    return AttemptRecord(round=1, image=f"视觉资产/任务/{TASK_ID}/attempt-01/image.png",
                         image_sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
                         response_id="resp-1")


def test_same_response_id_is_idempotent(tmp_path):
    first = save_attempt(tmp_path, _task(), _result(response_id="resp-1"), _manifest())
    again = save_attempt(tmp_path, _task(attempts=(_rec(tmp_path),)),
                         _result(response_id="resp-1"), _manifest())
    assert again.round == first.round
    assert not (tmp_path / "视觉资产" / "任务" / TASK_ID / "attempt-02").exists()


def test_empty_image_rejected(tmp_path):
    with pytest.raises(StoreError) as ctx:
        save_attempt(tmp_path, _task(), _result(image_bytes=b""), _manifest())
    assert ctx.value.code == "EMPTY_IMAGE"


def test_non_image_bytes_rejected(tmp_path):
    with pytest.raises(StoreError) as ctx:
        save_attempt(tmp_path, _task(), _result(image_bytes=b"not an image"), _manifest())
    assert ctx.value.code == "INVALID_IMAGE"


def test_fake_extension_rejected(tmp_path):
    fake = tmp_path / "伪装.png"
    fake.write_bytes(b"#!/bin/sh\necho hi\n")
    with pytest.raises(StoreError) as ctx:
        import_manual_result(tmp_path, TASK_ID, fake)
    assert ctx.value.code == "INVALID_IMAGE"


def test_oversized_image_rejected(tmp_path):
    big = tmp_path / "大图.png"
    big.write_bytes(PNG_BYTES + b"\x00" * (MAX_IMAGE_BYTES + 1))
    with pytest.raises(StoreError) as ctx:
        import_manual_result(tmp_path, TASK_ID, big)
    assert ctx.value.code == "IMAGE_TOO_LARGE"


def test_path_traversal_task_id_rejected(tmp_path):
    with pytest.raises(StoreError) as ctx:
        save_attempt(tmp_path, _task(task_id="../../越界"), _result(), _manifest())
    assert ctx.value.code == "INVALID_TASK_ID"
    assert list(tmp_path.rglob("越界")) == []


def test_missing_image_rejected(tmp_path):
    with pytest.raises(StoreError) as ctx:
        import_manual_result(tmp_path, TASK_ID, tmp_path / "不存在.png")
    assert ctx.value.code == "IMAGE_NOT_FOUND"


def test_manual_import_uses_fixed_file_name(tmp_path):
    src = tmp_path / "用户给的怪名字 带空格.png"
    src.write_bytes(PNG_BYTES)
    record = import_manual_result(tmp_path, TASK_ID, src)
    assert record.image.endswith("attempt-01/image.png")
    assert (tmp_path / "视觉资产" / "任务" / TASK_ID / "attempt-01" / "image.png").is_file()


def test_manual_import_same_image_is_idempotent(tmp_path):
    src = tmp_path / "a.png"
    src.write_bytes(PNG_BYTES)
    first = import_manual_result(tmp_path, TASK_ID, src)
    again = import_manual_result(tmp_path, TASK_ID, src, existing=(first,))
    assert again.round == first.round
    assert len(list((tmp_path / "视觉资产" / "任务" / TASK_ID).glob("attempt-*"))) == 1


def test_verify_attempt_detects_tampering(tmp_path):
    record = save_attempt(tmp_path, _task(), _result(), _manifest())
    assert verify_attempt(record, tmp_path) == []
    (tmp_path / record.image).write_bytes(PNG_BYTES + b"x")
    issues = verify_attempt(record, tmp_path)
    assert issues and issues[0].code == "IMAGE_HASH_MISMATCH"


def test_verify_attempt_detects_missing_file(tmp_path):
    record = save_attempt(tmp_path, _task(), _result(), _manifest())
    (tmp_path / record.image).unlink()
    issues = verify_attempt(record, tmp_path)
    assert any(i.code == "IMAGE_MISSING" for i in issues)


def test_attempt_dir_helper():
    assert attempt_dir("CH_x-正脸-v01", 2).endswith("视觉资产/任务/CH_x-正脸-v01/attempt-02")
    assert "\\" not in attempt_dir("CH_x-正脸-v01", 2)


def test_directory_replaced_atomically(tmp_path):
    record = save_attempt(tmp_path, _task(), _result(), _manifest())
    base = tmp_path / record.image
    assert base.parent.is_dir()
    assert list(base.parent.parent.glob("*.tmp*")) == []


def test_no_vendor_sdk_needed_for_store():
    source = (REPO_ROOT / "scripts" / "engine" / "visual_store.py").read_text(encoding="utf-8")
    assert "google" not in source
    assert "genai" not in source
