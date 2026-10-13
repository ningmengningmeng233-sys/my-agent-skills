#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemini 生图适配器（规格 2.13 / 3.1，工单03-Task2）。

- 配置只读 `GEMINI_API_KEY` / `GEMINI_IMAGE_MODEL` / `GEMINI_QC_MODEL` 等专用变量，
  **绝不回退读取 `OPENAI_API_KEY`**，任何错误信息与元数据都不含密钥值。
- Google SDK **只在本模块内延迟导入**：无视觉依赖时核心仍可完整启动。
- 只发送当前任务的提示词与选中参考图；响应必须恰好一张合法图片，否则显式拒绝。
- 网络暂态与 429 只做**传输级**指数退避；内容级拒绝立即上抛，由质量状态机决定返工。
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .image_executor import (
    EXECUTOR_NOT_CONFIGURED,
    GENERATION_REJECTED,
    INVALID_IMAGE_RESPONSE,
    RATE_LIMITED,
    GenerationError,
    GenerationRequest,
    GenerationResult,
)

# 生成模型：经官方模型目录确认的图片生成模型（可由 GEMINI_IMAGE_MODEL 覆盖）
DEFAULT_IMAGE_MODEL = "gemini-3-pro-image"
# 质检模型：需要「图像输入 + 文本输出」的多模态模型。
# 旧默认 `gemini-3-pro` 并不存在于官方模型目录，质检会在真实调用时直接失败。
DEFAULT_QC_MODEL = "gemini-3.1-pro-preview"
DEFAULT_IMAGE_SIZE = "2K"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 2.0

ALLOWED_MIME = ("image/png", "image/jpeg", "image/webp")
TRANSIENT_STATUS = (429, 500, 502, 503, 504)
TRANSIENT_NAME_HINTS = ("timeout", "timedout", "connection", "unavailable",
                        "serviceunavailable", "internalserver", "deadline")

# 参考图相关的稳定原因码（执行前拦截，绝不发请求）
REFERENCE_UNRESOLVED = "REFERENCE_UNRESOLVED"
REFERENCE_MIME_INVALID = "REFERENCE_MIME_INVALID"

# 这些环境变量名只用于读取配置，绝不写入任何产物
API_KEY_ENV = "GEMINI_API_KEY"
MODEL_ENV = "GEMINI_IMAGE_MODEL"
QC_MODEL_ENV = "GEMINI_QC_MODEL"
IMAGE_SIZE_ENV = "GEMINI_IMAGE_SIZE"
TIMEOUT_ENV = "GEMINI_TIMEOUT_SECONDS"


@dataclass(frozen=True)
class GeminiImageConfig:
    """Gemini 生图与质检配置（不可变；密钥只驻留内存，never logged）。"""

    api_key: str
    model: str = DEFAULT_IMAGE_MODEL
    qc_model: str = DEFAULT_QC_MODEL
    image_size: str = DEFAULT_IMAGE_SIZE
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS

    def __repr__(self) -> str:  # 防止密钥经 repr 泄漏到日志
        return (f"GeminiImageConfig(api_key=<set:{bool(self.api_key)}>, model={self.model!r}, "
                f"qc_model={self.qc_model!r}, image_size={self.image_size!r}, "
                f"timeout_seconds={self.timeout_seconds})")

    __str__ = __repr__


def load_gemini_config(env: Mapping[str, str] | None = None) -> GeminiImageConfig:
    """从环境读取 Gemini 配置；缺密钥即显式失败（不打印任何密钥值）。

    `env=None`（生产路径）时先按统一顺序加载包外 `.env`：
    `ASSET_ENV_PATH` → 技能目录 `.env` → `~/.asset-extract/.env`。
    **进程环境优先级最高**（显式传入的变量不被 .env 覆盖）。
    """
    if env is None:
        import os

        from .common import load_env

        _ = load_env()  # 已存在的进程环境变量优先，load_dotenv 默认不覆盖
        source = dict(os.environ)
    else:
        source = dict(env)
    api_key = str(source.get(API_KEY_ENV) or "").strip()
    if not api_key:
        raise GenerationError(
            EXECUTOR_NOT_CONFIGURED,
            f"未配置 {API_KEY_ENV}：Gemini 自动生图不可用。请设置环境变量 {API_KEY_ENV}"
            "（或显式指定 ASSET_ENV_PATH 指向的包外 .env），"
            "或改用手工导出模式（visual_production.py export 子命令导出提示词任务包）后回传生成结果。"
            f"（本适配器只读取 {API_KEY_ENV}，不读取其他供应商密钥；"
            f"离线模式 {API_KEY_ENV} 缺失时在任何网络调用之前本地拒绝）")

    def _int(name: str, default: int) -> int:
        raw = str(source.get(name) or "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    return GeminiImageConfig(
        api_key=api_key,
        model=str(source.get(MODEL_ENV) or "").strip() or DEFAULT_IMAGE_MODEL,
        qc_model=str(source.get(QC_MODEL_ENV) or "").strip() or DEFAULT_QC_MODEL,
        image_size=str(source.get(IMAGE_SIZE_ENV) or "").strip() or DEFAULT_IMAGE_SIZE,
        timeout_seconds=_int(TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS),
    )


def _load_sdk_types():
    """延迟导入 Google SDK 的 types（只有真正调用生成时才发生）。"""
    from google.genai import types  # pragma: no cover - 单元测试注入替身

    return types


def _is_transient(exc: BaseException) -> bool:
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        if int(code) in TRANSIENT_STATUS:
            return True
    except (TypeError, ValueError):
        pass
    name = type(exc).__name__.lower()
    return any(hint in name for hint in TRANSIENT_NAME_HINTS)


def _decode_image(data: Any) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        try:
            return base64.b64decode(data, validate=False)
        except (ValueError, TypeError) as e:
            raise GenerationError(INVALID_IMAGE_RESPONSE, f"图片 base64 解码失败：{e}") from e
    raise GenerationError(INVALID_IMAGE_RESPONSE, f"未知的图片数据类型: {type(data)!r}")


class GeminiImageExecutor:
    """Gemini 适配器：只消费已编译的请求对象，不自行改写提示词或反复收费。"""

    provider = "gemini"

    def __init__(self, client: Any = None, config: GeminiImageConfig | None = None,
                 types_module: Any = None):
        self._client = client
        self._config = config
        self._types = types_module
        self._sleep = time.sleep

    # --- 内部 -----------------------------------------------------------------

    @property
    def config(self) -> GeminiImageConfig:
        if self._config is None:
            self._config = load_gemini_config()
        return self._config

    def _types_module(self) -> Any:
        if self._types is None:
            self._types = _load_sdk_types()
        return self._types

    def _client_or_create(self) -> Any:
        if self._client is not None:
            return self._client
        import os

        from .offline import ensure_online

        # 离线开关在任何 socket 之前拦截：未显式配置 base_url 时目标是
        # 服务商默认端点（真实计费），离线模式一律本地拒绝。
        ensure_online("Gemini 直连生图",
                      url=str(os.environ.get("GOOGLE_GEMINI_BASE_URL") or "").strip() or None)
        from google import genai  # pragma: no cover - 真实调用路径

        self._client = genai.Client(api_key=self.config.api_key)
        return self._client

    def _prompt_text(self, request: GenerationRequest) -> str:
        """只发送当前任务的最小事实：正向提示词 + 负面提示词（严禁整部原稿）。"""
        text_parts = [request.prompt.strip()]
        if request.negative_prompt.strip():
            text_parts.append("负面提示词：" + request.negative_prompt.strip())
        return "\n\n".join(text_parts)

    def _sdk_contents(self, request: GenerationRequest, types_module: Any) -> list:
        """真实调用载荷：提示词 → 每张参考图前先声明其职责与相对路径 → 图片本体。

        参考图**只**取已解析并校验过的对象（`request.resolved`），并使用其真实字节与
        真实 MIME：缺文件、零字节、哈希不符在解析阶段就已失败，这里绝不发送
        `Part.from_bytes(data=b"")` 这种空占位——那会让模型"以为"有参考却看不到内容。
        """
        from engine.visual_schema import ReferenceRole as _Role

        role_cn = {_Role.IDENTITY.value: "身份", _Role.STRUCTURE.value: "结构",
                   _Role.STYLE.value: "风格", _Role.MATERIAL.value: "材质",
                   _Role.LIGHTING.value: "光影", _Role.SPACE.value: "空间"}
        resolved = tuple(getattr(request, "resolved", ()) or ())
        if request.references and not resolved:
            raise GenerationError(
                REFERENCE_UNRESOLVED,
                f"参考图未经过解析校验（{len(request.references)} 张）——拒绝发送，"
                "以免把不存在的图片当空图发出。请改用编译链（visual_request_chain）构建请求。")

        out: list = [self._prompt_text(request)]
        for ref in resolved:
            label = f"参考图（{role_cn.get(ref.role, ref.role)}）：{ref.relative_path}"
            if ref.note:
                label += f"｜{ref.note}"
            out.append(label)
            # 先校验 MIME 声明，再读真实字节（顺序固定，错误原因码可预期）
            mime = str(ref.mime_type or "").strip().lower()
            if mime not in ALLOWED_MIME:
                raise GenerationError(
                    REFERENCE_MIME_INVALID,
                    f"参考图 {ref.relative_path} 的 MIME 非法：{mime or '缺少 MIME'}")
            data = ref.read_bytes()          # 空/缺失在此以稳定码失败
            out.append(types_module.Part.from_bytes(data=data, mime_type=mime))
        return out

    def _config_for(self, request: GenerationRequest, types_module: Any) -> Any:
        image_config = types_module.ImageConfig(
            aspect_ratio=request.aspect_ratio,
            image_size=request.image_size or self.config.image_size,
        )
        return types_module.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=image_config,
            temperature=1.0,
        )

    def _extract(self, response: Any) -> tuple[bytes, str, str]:
        """从响应中取唯一的最终图片；空图/多图/错误 MIME 一律显式拒绝。"""
        candidates = getattr(response, "candidates", None) or []
        images: list[tuple[bytes, str]] = []
        if candidates:
            parts = getattr(getattr(candidates[0], "content", None), "parts", None) or []
            for part in parts:
                inline = getattr(part, "inline_data", None)
                if inline is None:
                    continue
                mime = str(getattr(inline, "mime_type", "") or "").strip().lower()
                if not mime.startswith("image/"):
                    raise GenerationError(INVALID_IMAGE_RESPONSE,
                                          f"响应含非图片数据：{mime or '缺少 MIME'}")
                if mime not in ALLOWED_MIME:
                    raise GenerationError(INVALID_IMAGE_RESPONSE, f"不支持的图片类型：{mime}")
                images.append((_decode_image(getattr(inline, "data", None)), mime))
        if not images:
            raise GenerationError(INVALID_IMAGE_RESPONSE, "响应未包含任何图片")
        if len(images) > 1:
            raise GenerationError(INVALID_IMAGE_RESPONSE,
                                  f"响应包含 {len(images)} 张图片，无法判断最终图"
                                  "（每项资产必须单独生成一张）")
        data, mime = images[0]
        if not data:
            raise GenerationError(INVALID_IMAGE_RESPONSE, "响应图片为空字节")
        return data, mime, str(getattr(response, "response_id", "") or "")

    # --- 协议实现 -------------------------------------------------------------

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """按请求生成一张图片；传输级暂态重试，内容级拒绝立即上抛。"""
        cfg = self.config
        client = self._client_or_create()
        types_module = self._types_module()
        contents = self._sdk_contents(request, types_module)
        call_config = self._config_for(request, types_module)

        attempts = max(1, int(cfg.max_retries))
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                response = client.models.generate_content(
                    model=cfg.model, contents=contents, config=call_config)
            except GenerationError:
                raise
            except BaseException as exc:  # noqa: BLE001 - 按暂态/内容级分流
                if not _is_transient(exc):
                    raise GenerationError(
                        GENERATION_REJECTED,
                        f"生成被拒绝（内容级，不做自动重试以免重复收费）："
                        f"{type(exc).__name__}: {exc}") from exc
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                self._sleep(cfg.backoff_seconds * (2 ** attempt))
                continue
            data, mime, response_id = self._extract(response)
            return GenerationResult(
                provider=self.provider, model=cfg.model, image_bytes=data, mime_type=mime,
                response_id=response_id,
                usage=dict(getattr(response, "usage_metadata", {}) or {}),
                raw_metadata={"attempts": attempt + 1},
                status="OK", detail="",
            )
        raise GenerationError(
            RATE_LIMITED,
            f"传输级失败重试 {attempts} 次后仍未成功（{type(last_error).__name__}: {last_error}）") \
            from last_error


def describe_config(config: GeminiImageConfig) -> dict:
    """可安全写入日志/报告的配置摘要（不含密钥）。"""
    return {"model": config.model, "qc_model": config.qc_model,
            "image_size": config.image_size, "timeout_seconds": config.timeout_seconds,
            "api_key_present": bool(config.api_key)}


class GeminiQcChecker:
    """用 Gemini 做结构化质检：发送被检图片 + 检查清单，解析结构化结果。

    与生成共用配置与客户端；**使用与生成不同的提示词**（见 image_qc.build_qc_request）。
    解析不出结构化结果或缺证据时返回 `QC_UNTRUSTWORTHY`，绝不勉强判通过。
    """

    def __init__(self, client: Any = None, config: GeminiImageConfig | None = None,
                 types_module: Any = None):
        self._executor = GeminiImageExecutor(client=client, config=config,
                                             types_module=types_module)

    def check(self, task: Any, attempt: Any, lib: Any):
        import json

        from .image_qc import (QcResult, build_qc_request, parse_qc_response,
                               rubric_for_kind)
        from .visual_schema import QcDecision

        payload = build_qc_request(task, attempt, rubric_for_kind(task.kind))
        image_path = Path(lib) / payload["image"]
        types_module = self._executor._types_module()
        try:
            data = image_path.read_bytes()
        except OSError:
            return QcResult(decision=QcDecision.UNTRUSTWORTHY.value, checker="gemini",
                            task_id=task.task_id, input_hashes=(attempt.image_sha256,),
                            untrustworthy_reason=f"被检图片不可读：{payload['image']}")
        if not data:
            return QcResult(decision=QcDecision.UNTRUSTWORTHY.value, checker="gemini",
                            task_id=task.task_id, input_hashes=(attempt.image_sha256,),
                            untrustworthy_reason=f"被检图片为空字节：{payload['image']}")
        from .reference_resolver import sniff_image_mime

        mime = sniff_image_mime(data[:16]) or "image/png"
        contents = [payload["prompt"]
                    + '\n输出 JSON：{"task_id","decision","issues","checker",'
                      '"checked_at","input_hashes"}。',
                    types_module.Part.from_bytes(data=data, mime_type=mime)]
        try:
            response = self._executor._client_or_create().models.generate_content(
                model=self._executor.config.qc_model, contents=contents)
        except BaseException as exc:  # noqa: BLE001 - 质检失败即不可信
            return QcResult(decision=QcDecision.UNTRUSTWORTHY.value, checker="gemini",
                            task_id=task.task_id, input_hashes=(attempt.image_sha256,),
                            untrustworthy_reason=f"质检请求失败：{type(exc).__name__}")
        text = _response_text(response)
        structured = _extract_json(text)
        if not isinstance(structured, dict):
            return QcResult(decision=QcDecision.UNTRUSTWORTHY.value, checker="gemini",
                            task_id=task.task_id, input_hashes=(attempt.image_sha256,),
                            untrustworthy_reason="质检响应不是结构化对象")
        structured.setdefault("task_id", task.task_id)
        structured.setdefault("checker", f"gemini:{self._executor.config.qc_model}")
        structured.setdefault("input_hashes", [attempt.image_sha256])
        structured.setdefault("checked_at", _now_iso())
        _ = json
        return parse_qc_response(structured, expected={"task_id": task.task_id,
                                                       "input_hashes": [attempt.image_sha256]})


def _response_text(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return str(getattr(response, "text", "") or "")
    parts = getattr(getattr(candidates[0], "content", None), "parts", None) or []
    return chr(10).join(str(getattr(p, "text", "") or "") for p in parts)


def _extract_json(text: str) -> Any:
    import json as _json

    body = str(text or "").strip()
    newline = chr(10)
    if body.startswith("```"):
        body = body.strip("`")
        if newline in body:
            body = body.split(newline, 1)[1]
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return _json.loads(body[start:end + 1])
    except ValueError:
        return None


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
