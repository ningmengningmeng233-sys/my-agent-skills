#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单03-Task1：引擎无关执行器协议与手工导出（规格 2.13 / 3.2）。

手工模式必须输出任务包并返回 pending 结果，**绝不伪造图片或冒充已生成**。
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.image_executor import (  # noqa: E402
    EXECUTOR_NOT_CONFIGURED,
    GENERATION_REJECTED,
    INVALID_IMAGE_RESPONSE,
    RATE_LIMITED,
    REFERENCE_LIMIT_EXCEEDED,
    GenerationError,
    GenerationRequest,
    GenerationResult,
    ImageExecutor,
    ManualExportExecutor,
    check_reference_limit,
)
from scripts.engine.visual_schema import ReferenceBinding  # noqa: E402


def _request(references=(), **kw):
    base = dict(
        task_id="CH_林姑娘-正脸-v01",
        prompt="正脸特写，均匀柔光",
        negative_prompt="无文字、无多格",
        references=tuple(references),
        aspect_ratio="1:1",
        image_size="2K",
        output_mime="image/png",
    )
    base.update(kw)
    return GenerationRequest(**base)


def test_request_and_result_shapes():
    req = _request()
    assert req.task_id and req.prompt and req.aspect_ratio == "1:1"
    assert req.output_mime == "image/png"
    result = GenerationResult(provider="manual", model="", image_bytes=b"",
                              mime_type="", response_id="", status="AWAITING_MANUAL_RESULT")
    assert result.image_bytes == b""
    assert not result.succeeded


def test_error_codes_are_stable():
    assert EXECUTOR_NOT_CONFIGURED == "EXECUTOR_NOT_CONFIGURED"
    assert GENERATION_REJECTED == "GENERATION_REJECTED"
    assert RATE_LIMITED == "RATE_LIMITED"
    assert INVALID_IMAGE_RESPONSE == "INVALID_IMAGE_RESPONSE"
    assert REFERENCE_LIMIT_EXCEEDED == "REFERENCE_LIMIT_EXCEEDED"
    err = GenerationError(GENERATION_REJECTED, "内容被拒")
    assert err.code == GENERATION_REJECTED


def test_protocol_is_runtime_checkable():
    class _Fake:
        def generate(self, request):
            return GenerationResult(provider="fake", model="m", image_bytes=b"x",
                                    mime_type="image/png", response_id="r", status="OK")

    assert isinstance(_Fake(), ImageExecutor)


def test_manual_export_writes_task_package(tmp_path):
    executor = ManualExportExecutor(tmp_path)
    result = executor.generate(_request())
    out = tmp_path / "CH_林姑娘-正脸-v01"
    for name in ("请求.json", "提示词.md", "参考图清单.md"):
        assert (out / name).is_file(), name
    assert result.status == "AWAITING_MANUAL_RESULT"
    assert result.image_bytes == b""
    # 绝不创建伪图片，也不产生任何图片文件
    images = [p for p in out.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    assert images == []


def test_manual_export_request_json_is_minimal_and_secret_free(tmp_path):
    executor = ManualExportExecutor(tmp_path)
    executor.generate(_request(references=(
        ReferenceBinding(path="references/face.png", role="identity", sha256="a" * 64),)))
    data = json.loads((tmp_path / "CH_林姑娘-正脸-v01" / "请求.json").read_text(encoding="utf-8"))
    assert data["task_id"] == "CH_林姑娘-正脸-v01"
    assert data["aspect_ratio"] == "1:1"
    assert data["references"][0]["role"] == "identity"
    text = json.dumps(data, ensure_ascii=False)
    for secret in ("GEMINI_API_KEY", "OPENAI_API_KEY", "Authorization", "api_key"):
        assert secret not in text


def test_manual_export_prompt_markdown_is_chinese_and_human_editable(tmp_path):
    executor = ManualExportExecutor(tmp_path)
    executor.generate(_request())
    text = (tmp_path / "CH_林姑娘-正脸-v01" / "提示词.md").read_text(encoding="utf-8")
    assert "提示词" in text
    assert "正脸特写" in text
    assert "无文字" in text


def test_manual_export_reference_list_declares_roles(tmp_path):
    executor = ManualExportExecutor(tmp_path)
    executor.generate(_request(references=(
        ReferenceBinding(path="references/face.png", role="identity", note="身份参考"),
        ReferenceBinding(path="references/materials/skin.png", role="material", note="皮肤材质"),
    )))
    text = (tmp_path / "CH_林姑娘-正脸-v01" / "参考图清单.md").read_text(encoding="utf-8")
    assert "references/face.png" in text
    assert "身份" in text
    assert "材质" in text


def test_manual_export_is_deterministic(tmp_path):
    ManualExportExecutor(tmp_path / "a").generate(_request())
    ManualExportExecutor(tmp_path / "b").generate(_request())
    for name in ("请求.json", "提示词.md", "参考图清单.md"):
        assert (tmp_path / "a" / "CH_林姑娘-正脸-v01" / name).read_bytes() == \
               (tmp_path / "b" / "CH_林姑娘-正脸-v01" / name).read_bytes(), name


def test_manual_export_rejects_absolute_reference(tmp_path):
    executor = ManualExportExecutor(tmp_path)
    with pytest.raises(GenerationError) as ctx:
        executor.generate(_request(references=(
            ReferenceBinding(path="C:/素材/face.png", role="identity"),)))
    assert ctx.value.code == GENERATION_REJECTED


def test_reference_limit_check():
    refs = tuple(ReferenceBinding(path=f"r{i}.png", role="material") for i in range(4))
    check_reference_limit(_request(references=refs), limit=4)
    with pytest.raises(GenerationError) as ctx:
        check_reference_limit(_request(references=refs), limit=3)
    assert ctx.value.code == REFERENCE_LIMIT_EXCEEDED


def test_manual_export_honours_reference_limit(tmp_path):
    refs = tuple(ReferenceBinding(path=f"r{i}.png", role="material") for i in range(5))
    executor = ManualExportExecutor(tmp_path, reference_limit=3)
    with pytest.raises(GenerationError) as ctx:
        executor.generate(_request(references=refs))
    assert ctx.value.code == REFERENCE_LIMIT_EXCEEDED


def test_no_gemini_import_in_executor_protocol():
    source = (REPO_ROOT / "scripts" / "engine" / "image_executor.py").read_text(encoding="utf-8")
    assert "google" not in source
    assert "genai" not in source


def test_manual_export_path_stays_inside_root(tmp_path):
    executor = ManualExportExecutor(tmp_path)
    with pytest.raises(GenerationError) as ctx:
        executor.generate(_request(task_id="../越界"))
    assert ctx.value.code == GENERATION_REJECTED
    assert not (tmp_path.parent / "越界").exists()
