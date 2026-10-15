#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成器注册表与 Codex 转接 Gemini 传输适配器（阶段 4 工单 4.1）。

三个注册名对应三条**同一契约**（`GenerationRequest` / `GenerationResult`）的路径：

- `gemini_api`：本地直连 Gemini（需要密钥）；只生成，**不终审**；
- `codex_gemini_gateway`：Codex 转接 Gemini（只换传输适配器，请求/结果契约完全一致）；
- `manual_export`：无网络导出任务包（永远可用，不需要密钥）。

传输失败（超时、429、5xx）标为 `TRANSPORT_ERROR`，**不计入画面返工次数**；
安全拦截标为 `POLICY_BLOCK`。请求/结果里绝不带密钥与绝对路径。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .image_executor import GenerationError, GenerationRequest, GenerationResult, executor_is_configured

GENERATOR_NAMES = ("gemini_api", "codex_gemini_gateway", "manual_export")

GENERATOR_UNKNOWN = "GENERATOR_UNKNOWN"
GENERATOR_NOT_CONFIGURED = "GENERATOR_NOT_CONFIGURED"
TRANSPORT_ERROR = "TRANSPORT_ERROR"
POLICY_BLOCK = "POLICY_BLOCK"
GATEWAY_BAD_RESPONSE = "GATEWAY_BAD_RESPONSE"

# 可重试的传输状态码（不消耗画面次数）
RETRYABLE_STATUS = (408, 425, 429, 500, 502, 503, 504)
POLICY_KEYWORDS = ("blocked", "safety", "policy", "prohibited", "content_filter")


class GeneratorError(RuntimeError):
    """生成器不可用/响应异常：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


@dataclass(frozen=True)
class GatewayResponse:
    """网关返回的规范化结果（测试注入替身时也用这个类型）。"""

    status_code: int = 200
    image_bytes: bytes = b""
    mime_type: str = "image/png"
    response_id: str = ""
    payload: dict = field(default_factory=dict)


def classify_response(response: GatewayResponse) -> GenerationError | None:
    """把网关响应分成「可重试传输错误 / 安全拦截 / 正常」。"""
    code = int(response.status_code or 0)
    if code in RETRYABLE_STATUS or code >= 500:
        return GeneratorError(TRANSPORT_ERROR, f"HTTP {code}（传输错误，不计画面次数）")
    if code != 200:
        blob = json.dumps(response.payload or {}, ensure_ascii=False).lower()
        if any(word in blob for word in POLICY_KEYWORDS):
            return GeneratorError(POLICY_BLOCK, "模型安全拦截：不绕过限制，改用忠实的非刺激表达")
        return GeneratorError(TRANSPORT_ERROR, f"HTTP {code}")
    if not response.image_bytes:
        return GeneratorError(GATEWAY_BAD_RESPONSE, "网关未返回图片字节")
    return None


class CodexGeminiGatewayExecutor:
    """Codex 转接 Gemini：传输可注入（测试零网络），契约与直连完全一致。"""

    name = "codex_gemini_gateway"

    def __init__(self, base_url: str, *, api_key: str = "",
                 transport: Callable[[dict], GatewayResponse] | None = None,
                 model: str = "gemini-3-pro-image") -> None:
        self.base_url = str(base_url or "").strip()
        self._api_key = str(api_key or "")
        self.model = model
        self._transport = transport or self._http_post

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def build_payload(self, request: GenerationRequest) -> dict:
        """只发当前任务事实与已校验参考（相对路径 + 哈希），不含密钥与绝对路径。"""
        return {
            "model": self.model,
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "aspect_ratio": request.aspect_ratio,
            "image_size": request.image_size,
            "output_mime": request.output_mime,
            "task_id": request.task_id,
            "references": [
                {"role": role, "sha256": getattr(ref, "sha256", ""),
                 "path": getattr(ref, "relative_path", getattr(ref, "path", "")),
                 "mime_type": getattr(ref, "mime_type", "")}
                for ref, role in zip(request.references, request.reference_roles)
            ],
        }

    def generate(self, request: GenerationRequest) -> GenerationResult:
        if not self.configured:
            raise GeneratorError(GENERATOR_NOT_CONFIGURED,
                                 "未配置网关地址（CODEX_GEMINI_BASE_URL）："
                                 "可用 manual_export 导出手工任务包")
        payload = self.build_payload(request)
        try:
            response = self._transport(payload)
        except TimeoutError as e:
            raise GeneratorError(TRANSPORT_ERROR, f"网关超时（不计画面次数）：{e}") from e
        except OSError as e:
            raise GeneratorError(TRANSPORT_ERROR, f"网关连接失败：{type(e).__name__}") from e
        problem = classify_response(response)
        if problem is not None:
            raise problem
        return GenerationResult(
            provider=self.name, model=self.model, image_bytes=response.image_bytes,
            mime_type=response.mime_type or "image/png",
            response_id=response.response_id, status="OK")

    def _http_post(self, payload: dict) -> GatewayResponse:  # pragma: no cover - 真实网络路径
        import urllib.error
        import urllib.request

        from .offline import ensure_online

        # 传输层守门：离线模式在任何 socket 之前抛 OfflineRequested，不进重试。
        ensure_online("Codex 转接网关外呼", url=self.base_url)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base_url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        if self._api_key:
            # 认证头只在本进程内构造，绝不写入请求清单、日志或产物
            req.add_header("Authorization", "Bearer " + self._api_key)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return _response_from_payload(resp.status, body)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except (ValueError, OSError):
                body = {}
            return _response_from_payload(e.code, body if isinstance(body, dict) else {})


def _response_from_payload(status_code: int, body: Mapping[str, Any]) -> GatewayResponse:
    import base64

    raw = str(body.get("image_base64") or "")
    image = base64.b64decode(raw) if raw else b""
    return GatewayResponse(status_code=status_code, image_bytes=image,
                           mime_type=str(body.get("mime_type") or "image/png"),
                           response_id=str(body.get("response_id") or ""),
                           payload=dict(body))


def generator_status(name: str) -> dict:
    """某一生成器是否可用（只报「已配置/未配置」，绝不回显任何密钥值）。"""
    key = str(name or "").strip()
    if key not in GENERATOR_NAMES:
        raise GeneratorError(GENERATOR_UNKNOWN, f"未知引擎 {name!r}（只允许 {list(GENERATOR_NAMES)}）")
    if key == "manual_export":
        return {"name": key, "configured": True, "detail": "无需密钥，随时可用"}
    if key == "gemini_api":
        configured = bool(executor_is_configured())
        return {"name": key, "configured": configured,
                "detail": "GEMINI_API_KEY 已配置" if configured else
                          "GEMINI_API_KEY 未配置：可改用 manual_export 或 codex_gemini_gateway"}
    import os

    base = str(os.environ.get("CODEX_GEMINI_BASE_URL") or "").strip()
    return {"name": key, "configured": bool(base),
            "detail": "网关地址已配置" if base else "未配置 CODEX_GEMINI_BASE_URL"}


def get_generator(name: str, *, config: Any = None, lib: Path | None = None):
    """按注册名返回生成器实例；不可用即稳定码失败（不静默降级成别的引擎）。"""
    key = str(name or "").strip()
    if key not in GENERATOR_NAMES:
        raise GeneratorError(GENERATOR_UNKNOWN, f"未知引擎 {name!r}（只允许 {list(GENERATOR_NAMES)}）")
    if key == "manual_export":
        from .image_executor import ManualExportExecutor

        root = Path(lib) if lib is not None else Path(".")
        return ManualExportExecutor(root / "视觉资产" / "任务包")
    if key == "gemini_api":
        if not executor_is_configured():
            raise GeneratorError(GENERATOR_NOT_CONFIGURED,
                                 "GEMINI_API_KEY 未配置：改用 manual_export 导出任务包")
        from .gemini_image import GeminiImageExecutor

        return GeminiImageExecutor(config=config)
    import os

    base = str(os.environ.get("CODEX_GEMINI_BASE_URL") or "").strip()
    if not base:
        raise GeneratorError(GENERATOR_NOT_CONFIGURED,
                             "未配置 CODEX_GEMINI_BASE_URL：改用 manual_export 导出任务包")
    return CodexGeminiGatewayExecutor(base, api_key=str(os.environ.get("CODEX_GEMINI_API_KEY") or ""))
