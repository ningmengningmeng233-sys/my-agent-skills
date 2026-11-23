#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 4 工单 4.1：生成器注册表与 Codex 转接 Gemini。

- 三个注册名：`gemini_api` / `codex_gemini_gateway` / `manual_export`，共用同一
  `GenerationRequest` / `GenerationResult` 契约（只换传输适配器）；
- 未配置引擎给**可操作**错误（指出 manual_export 这条路），不静默降级；
- 超时、429、5xx 标为 `TRANSPORT_ERROR`（不计画面次数）；安全拦截标 `POLICY_BLOCK`；
- 请求载荷只有任务事实与已校验参考（相对路径 + 哈希），绝无密钥。
"""
import base64
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.generator_registry import (  # noqa: E402
    GENERATOR_NAMES,
    GENERATOR_NOT_CONFIGURED,
    GENERATOR_UNKNOWN,
    POLICY_BLOCK,
    TRANSPORT_ERROR,
    CodexGeminiGatewayExecutor,
    GatewayResponse,
    GeneratorError,
    classify_response,
    generator_status,
    get_generator,
)
from scripts.engine.image_executor import GenerationRequest, GenerationResult  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")


def _request():
    return GenerationRequest(task_id="CH_林姑娘-正脸-v01", prompt="【任务目标】正脸",
                             negative_prompt="禁止多视图", references=(),
                             aspect_ratio="1:1", image_size="2K", output_mime="image/png",
                             style_name="仙侠国风电影CG", style_version="1.0.0")


# --- 1. 注册表 ---------------------------------------------------------------------

def test_registry_exposes_three_generator_names():
    assert GENERATOR_NAMES == ("gemini_api", "codex_gemini_gateway", "manual_export")


def test_unknown_generator_is_rejected():
    with pytest.raises(GeneratorError) as ctx:
        get_generator("stable_diffusion")
    assert ctx.value.code == GENERATOR_UNKNOWN
    with pytest.raises(GeneratorError):
        generator_status("stable_diffusion")


def test_manual_export_is_always_available(tmp_path):
    executor = get_generator("manual_export", lib=tmp_path)
    assert hasattr(executor, "generate"), "手工导出执行器必须实现统一契约"
    assert generator_status("manual_export")["configured"] is True


def test_gemini_api_reports_unconfigured_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    status = generator_status("gemini_api")
    assert status["configured"] is False
    assert "manual_export" in status["detail"], "错误信息必须给出可操作出路"
    with pytest.raises(GeneratorError) as ctx:
        get_generator("gemini_api")
    assert ctx.value.code == GENERATOR_NOT_CONFIGURED


def test_gateway_reports_unconfigured_without_base_url(monkeypatch):
    monkeypatch.delenv("CODEX_GEMINI_BASE_URL", raising=False)
    assert generator_status("codex_gemini_gateway")["configured"] is False
    with pytest.raises(GeneratorError) as ctx:
        get_generator("codex_gemini_gateway")
    assert ctx.value.code == GENERATOR_NOT_CONFIGURED


def test_gateway_status_never_leaks_key(monkeypatch):
    monkeypatch.setenv("CODEX_GEMINI_BASE_URL", "https://example.invalid/api")
    monkeypatch.setenv("CODEX_GEMINI_API_KEY", "sk-" + "x" * 40)
    status = generator_status("codex_gemini_gateway")
    assert status["configured"] is True
    assert "sk-" not in json.dumps(status, ensure_ascii=False)


# --- 2. 网关：同一契约、可注入传输 -------------------------------------------------

def test_gateway_returns_generation_result():
    def transport(payload):
        return GatewayResponse(status_code=200, image_bytes=PNG, mime_type="image/png",
                               response_id="r-1", payload={"ok": True})

    executor = CodexGeminiGatewayExecutor("https://gw.invalid/api", transport=transport)
    result = executor.generate(_request())
    assert isinstance(result, GenerationResult)
    assert result.image_bytes == PNG
    assert result.response_id == "r-1"
    assert result.mime_type == "image/png"


def test_gateway_payload_has_no_secrets_and_relative_paths():
    captured = {}

    def transport(payload):
        captured.update(payload)
        return GatewayResponse(status_code=200, image_bytes=PNG, response_id="r")

    executor = CodexGeminiGatewayExecutor("https://gw.invalid/api", api_key="super-secret",
                                          transport=transport)
    executor.generate(_request())
    blob = json.dumps(captured, ensure_ascii=False)
    assert "super-secret" not in blob
    assert "Authorization" not in blob
    assert captured["task_id"] == "CH_林姑娘-正脸-v01"
    assert "\\" not in json.dumps(captured.get("references", []))


# --- 3. 传输错误与安全拦截分类 -----------------------------------------------------

@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_transport_statuses_are_retryable_errors(status):
    problem = classify_response(GatewayResponse(status_code=status))
    assert problem is not None and problem.code == TRANSPORT_ERROR


def test_timeout_is_transport_error():
    def transport(payload):
        raise TimeoutError("read timeout")

    executor = CodexGeminiGatewayExecutor("https://gw.invalid/api", transport=transport)
    with pytest.raises(GeneratorError) as ctx:
        executor.generate(_request())
    assert ctx.value.code == TRANSPORT_ERROR
    assert "不计画面次数" in str(ctx.value) or "传输" in str(ctx.value)


def test_policy_block_is_classified():
    def transport(payload):
        return GatewayResponse(status_code=400, payload={"error": "blocked by safety policy"})

    executor = CodexGeminiGatewayExecutor("https://gw.invalid/api", transport=transport)
    with pytest.raises(GeneratorError) as ctx:
        executor.generate(_request())
    assert ctx.value.code == POLICY_BLOCK


def test_empty_image_is_bad_response():
    problem = classify_response(GatewayResponse(status_code=200, image_bytes=b""))
    assert problem is not None and problem.code not in (TRANSPORT_ERROR, POLICY_BLOCK)


# --- 4. 审核与生成分离 -------------------------------------------------------------

def test_generator_modules_do_not_reference_review_paths():
    """生成器只生成：不得引用审核器/质检器（Gemini 不终审）。"""
    for name in ("generator_registry.py", "gemini_image.py"):
        src = (REPO_ROOT / "scripts" / "engine" / name).read_text(encoding="utf-8")
        assert "review_contract" not in src, f"{name} 不得参与审核"
        assert "validate_review_binding" not in src


def test_gateway_executor_declares_name():
    executor = CodexGeminiGatewayExecutor("https://gw.invalid/api",
                                          transport=lambda payload: GatewayResponse())
    assert executor.name == "codex_gemini_gateway"
