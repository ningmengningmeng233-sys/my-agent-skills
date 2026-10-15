"""LLM 调用封装：DeepSeek/OpenAI 兼容接口，支持 dry-run、超时重试、审计日志。"""

import json
import os
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .audit import append_audit
from .hashing import sha256_bytes

_DEFAULT_TIMEOUT = 120.0
_DEFAULT_MAX_RETRIES = 2
_DEFAULT_MAX_TOKENS = 8192
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    """LLM 调用失败（网络、配置或 API 错误）。"""

    def __init__(self, message: str, *, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """LLM 调用配置。"""

    provider: str
    api_key: str
    base_url: str
    model: str
    timeout: float
    enabled: bool
    dry_run: bool
    max_retries: int
    max_tokens: int = _DEFAULT_MAX_TOKENS


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _provider_env(provider: str, suffix: str) -> str:
    return f"{provider.upper()}_{suffix}"


def load_config(provider: str | None = None) -> LLMConfig:
    """从环境变量加载 LLM 配置。

    支持的变量（以 DeepSeek 为例）：
        NOVEL_LLM_PROVIDER=deepseek
        NOVEL_LLM_ENABLED=1
        NOVEL_LLM_DRY_RUN=0
        DEEPSEEK_API_KEY=...
        DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
        DEEPSEEK_MODEL=deepseek-chat
        DEEPSEEK_TIMEOUT=120
        DEEPSEEK_MAX_RETRIES=2
    """
    provider = (provider or _env("NOVEL_LLM_PROVIDER", "deepseek")).lower()
    enabled = _env("NOVEL_LLM_ENABLED", "1") not in {"0", "false", "False", "no"}
    dry_run = _env("NOVEL_LLM_DRY_RUN", "0") not in {"", "0", "false", "False", "no"}

    base_url = _env(_provider_env(provider, "BASE_URL"), "https://api.deepseek.com/v1")
    model = _env(_provider_env(provider, "MODEL"), "deepseek-chat")
    timeout = float(_env(_provider_env(provider, "TIMEOUT"), str(_DEFAULT_TIMEOUT)))
    max_retries = int(_env(_provider_env(provider, "MAX_RETRIES"), str(_DEFAULT_MAX_RETRIES)))
    max_tokens = int(_env(_provider_env(provider, "MAX_TOKENS"), str(_DEFAULT_MAX_TOKENS)))

    return LLMConfig(
        provider=provider,
        api_key=_env(_provider_env(provider, "API_KEY")),
        base_url=base_url.rstrip("/"),
        model=model,
        timeout=timeout,
        enabled=enabled,
        dry_run=dry_run,
        max_retries=max_retries,
        max_tokens=max_tokens,
    )


def _dry_run_response(messages: Sequence[dict[str, str]]) -> str:
    """测试模式下返回固定占位文本，避免真实 API 调用。"""
    content = messages[-1].get("content", "") if messages else ""
    return f"[DRY-RUN] {content[:80]}"


def _build_request(config: LLMConfig, messages: Sequence[dict[str, str]]) -> dict[str, Any]:
    return {
        "model": config.model,
        "messages": list(messages),
        "temperature": 0.7,
        "max_tokens": config.max_tokens,
    }


def _parse_response_with_usage(payload: dict[str, Any]) -> tuple[str, dict[str, int]]:
    """解析响应，返回 (正文, usage)。

    ``usage`` 缺省或字段非法时退化为空字典：审计只做核账，绝不因供应商
    未返回用量而中断改写。
    """
    try:
        choice = payload["choices"][0]
        finish_reason = choice.get("finish_reason")
        if finish_reason in {"length", "content_filter"}:
            raise LLMError(f"LLM 输出未完整结束: finish_reason={finish_reason}")
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM 返回空内容")
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"LLM 响应格式异常: {payload}") from exc

    usage: dict[str, int] = {}
    raw_usage = payload.get("usage")
    if isinstance(raw_usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = raw_usage.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if value < 0:
                continue
            usage[key] = value
    return content, usage


def _parse_response(payload: dict[str, Any]) -> str:
    """向后兼容入口：只返回正文。"""
    return _parse_response_with_usage(payload)[0]


def _chat_completions_url(config: LLMConfig) -> str:
    return f"{config.base_url}/chat/completions"


def call_llm(
    messages: Sequence[dict[str, str]],
    *,
    config: LLMConfig | None = None,
    audit_path: Path | None = None,
    chapter_id: str = "",
) -> str:
    """调用 LLM 返回文本内容。

    参数:
        messages: OpenAI 风格消息列表。
        config: 配置；默认从环境变量加载。
        audit_path: 审计日志路径；提供时写入调用记录。
        chapter_id: 用于审计的章节 ID。

    返回:
        LLM 返回的文本字符串。
    """
    config = config or load_config()
    messages = list(messages)

    if config.dry_run:
        text = _dry_run_response(messages)
        if audit_path:
            _append_llm_audit(
                audit_path, chapter_id, config, messages, text, dry_run=True
            )
        return text

    if not config.enabled:
        raise LLMError("LLM 已禁用（NOVEL_LLM_ENABLED=0）且未开启 dry-run")

    if not config.api_key:
        raise LLMError(f"缺少 API key: {_provider_env(config.provider, 'API_KEY')}")

    request_body = _build_request(config, messages)
    url = _chat_completions_url(config)
    text, usage, attempts = _request_with_retry(config, url, request_body)

    if audit_path:
        _append_llm_audit(
            audit_path, chapter_id, config, messages, text,
            dry_run=False, usage=usage, attempts=attempts,
        )

    return text


def _request_with_retry(
    config: LLMConfig, url: str, request_body: dict[str, Any]
) -> tuple[str, dict[str, int], int]:
    """带重试的请求；返回 (正文, usage, 实际尝试次数)。

    尝试次数用于清单第 9 节的“重试次数”核账：首次即成功为 1。
    """
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    last_error: Exception | None = None
    attempts = 0

    for attempt in range(config.max_retries + 1):
        attempts = attempt + 1
        try:
            with httpx.Client(timeout=config.timeout) as client:
                response = client.post(url, headers=headers, json=request_body)
                response.raise_for_status()
                text, usage = _parse_response_with_usage(response.json())
                return text, usage, attempts
        except httpx.HTTPStatusError as exc:
            last_error = exc
            status = exc.response.status_code
            if status not in _RETRYABLE_STATUS:
                break
            if attempt < config.max_retries:
                retry_after = exc.response.headers.get("Retry-After", "")
                try:
                    delay = max(0.0, min(float(retry_after), 30.0))
                except ValueError:
                    delay = min(2.0**attempt, 8.0) + random.random() * 0.2
                time.sleep(delay)
        except httpx.RequestError as exc:
            last_error = exc
            if attempt < config.max_retries:
                time.sleep(min(2.0**attempt, 8.0) + random.random() * 0.2)

    raise LLMError(
        f"LLM 请求失败（已重试 {config.max_retries} 次）: {url}",
        cause=last_error,
    )


def _append_llm_audit(
    audit_path: Path,
    chapter_id: str,
    config: LLMConfig,
    messages: Sequence[dict[str, str]],
    response_text: str,
    *,
    dry_run: bool,
    usage: dict[str, int] | None = None,
    attempts: int = 1,
) -> None:
    request_hash = sha256_bytes(
        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    response_hash = sha256_bytes(response_text.encode("utf-8"))
    metrics = dict(usage or {})
    append_audit(
        audit_path,
        "llm_call",
        chapter_id=chapter_id,
        provider=config.provider,
        model=config.model,
        request_hash=request_hash,
        response_hash=response_hash,
        dry_run=dry_run,
        attempts=attempts,
        prompt_tokens=metrics.get("prompt_tokens"),
        completion_tokens=metrics.get("completion_tokens"),
        total_tokens=metrics.get("total_tokens"),
    )
