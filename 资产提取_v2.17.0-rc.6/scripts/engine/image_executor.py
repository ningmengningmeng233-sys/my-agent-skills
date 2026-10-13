#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎无关的图像执行器协议（规格 2.13 / 3.2，工单03-Task1）。

- `ImageExecutor.generate(request) -> GenerationResult` 是唯一执行入口；
  Gemini 适配器（工单03-Task2）与手工导出器都实现同一协议。
- 本模块**不导入任何供应商 SDK**，核心与协议层对 Gemini 零依赖。
- 手工模式输出任务包并返回 `AWAITING_MANUAL_RESULT`，绝不伪造图片、不冒充已生成。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .visual_schema import ReferenceBinding, normalize_rel_path

# 稳定异常码（工单03-Task1 Step3）
EXECUTOR_NOT_CONFIGURED = "EXECUTOR_NOT_CONFIGURED"
GENERATION_REJECTED = "GENERATION_REJECTED"
RATE_LIMITED = "RATE_LIMITED"
INVALID_IMAGE_RESPONSE = "INVALID_IMAGE_RESPONSE"
REFERENCE_LIMIT_EXCEEDED = "REFERENCE_LIMIT_EXCEEDED"

STATUS_OK = "OK"
STATUS_AWAITING_MANUAL_RESULT = "AWAITING_MANUAL_RESULT"
STATUS_REJECTED = "REJECTED"

DEFAULT_REFERENCE_LIMIT = 8
SAFE_TASK_ID_RE = re.compile(r"^[^/\\:]+$")
ROLE_CN = {
    "identity": "身份",
    "structure": "结构",
    "style": "风格",
    "material": "材质",
    "lighting": "光影",
    "space": "空间",
}


class GenerationError(RuntimeError):
    """生成失败：code 为稳定原因码（调用方按码分派，不靠文案）。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


@dataclass(frozen=True)
class GenerationRequest:
    """一次生图请求的最小事实集合（严禁携带整部原稿或完整风格包）。"""

    task_id: str
    prompt: str
    negative_prompt: str = ""
    references: tuple[ReferenceBinding, ...] = ()
    aspect_ratio: str = "1:1"
    image_size: str = "2K"
    output_mime: str = "image/png"
    style_name: str = ""
    style_version: str = ""
    reference_roles: tuple[str, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)
    # 已解析并逐文件校验过的参考图（含真实字节、MIME、SHA-256）。
    # 执行器**只**发送这些对象；缺失时以稳定码失败，绝不发送空字节占位图片。
    resolved: tuple[Any, ...] = ()

    def to_manifest(self) -> dict:
        """可审查的发送清单（只记相对路径、职责、哈希与字节数，绝不含密钥）。"""
        return {
            "task_id": self.task_id,
            "aspect_ratio": self.aspect_ratio,
            "image_size": self.image_size,
            "output_mime": self.output_mime,
            "style_name": self.style_name,
            "style_version": self.style_version,
            "references": [
                {"path": normalize_rel_path(r.path), "role": r.role, "sha256": r.sha256,
                 "note": r.note}
                for r in self.references
            ],
        }


@dataclass(frozen=True)
class GenerationResult:
    """一次生成的结果：手工模式返回空图像字节 + pending 状态，不伪造成功。"""

    provider: str = ""
    model: str = ""
    image_bytes: bytes = b""
    mime_type: str = ""
    response_id: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    raw_metadata: Mapping[str, Any] = field(default_factory=dict)
    status: str = STATUS_OK
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == STATUS_OK and bool(self.image_bytes)

    @property
    def awaiting_manual(self) -> bool:
        return self.status == STATUS_AWAITING_MANUAL_RESULT


@runtime_checkable
class ImageExecutor(Protocol):
    """执行器协议：任何引擎实现 generate 即可接入（Codex 编排不绑定供应商）。"""

    def generate(self, request: GenerationRequest) -> GenerationResult:  # pragma: no cover
        ...


def check_reference_limit(request: GenerationRequest, limit: int = DEFAULT_REFERENCE_LIMIT) -> None:
    """参考图超出引擎上限即显式失败（禁止静默截断，裁剪由路由器负责并记录）。"""
    if len(request.references) > int(limit):
        raise GenerationError(
            REFERENCE_LIMIT_EXCEEDED,
            f"参考图 {len(request.references)} 张超过引擎上限 {limit} 张"
            "（请由参考路由器按职责优先级裁剪并记录被省略项）")


def _validate_request(request: GenerationRequest) -> None:
    task_id = str(request.task_id or "")
    if not task_id or not SAFE_TASK_ID_RE.match(task_id) or ".." in task_id:
        raise GenerationError(GENERATION_REJECTED,
                              f"任务 ID 非法（不得含路径分隔符或越界段）：{task_id!r}")
    for binding in request.references:
        path = normalize_rel_path(binding.path)
        if not path:
            raise GenerationError(GENERATION_REJECTED, "参考图路径为空")
        if re.match(r"^[A-Za-z]:", path) or path.startswith("/") or ".." in path.split("/"):
            raise GenerationError(GENERATION_REJECTED,
                                  f"参考图必须是资产库内相对路径：{binding.path!r}")


class ManualExportExecutor:
    """无 API 时的手工执行器：导出**自包含**任务包，返回 pending 结果。

    产物落在 `<root>/<task_id>/`，跨机转交给 Codex/Gemini 时无需访问原资产库：

    - `请求.json`：最小发送清单（相对路径 / 职责 / 哈希 / MIME / 字节数）
    - `提示词.md`：正向与负面提示词
    - `参考图清单.md`：逐张职责与包内相对路径
    - `参考图/`：**实际选中的图片本体**（`01-风格.png`、`02-身份.webp` …）
    - `任务包清单.json`：每个文件的 SHA-256 / 字节数 / MIME / 职责 / 源逻辑标识

    导出先写临时目录，全部成功后再原子替换目标目录：失败不留半包。
    二次导出同输入字节稳定；源图变化会改变哈希并使旧包过期。
    """

    provider = "manual"
    REFERENCE_DIR = "参考图"
    PACKAGE_MANIFEST = "任务包清单.json"

    def __init__(self, root: Path, reference_limit: int = DEFAULT_REFERENCE_LIMIT):
        self.root = Path(root)
        self.reference_limit = int(reference_limit)

    def generate(self, request: GenerationRequest, resolved: Sequence[Any] = (),
                 omitted: Sequence[dict] = ()) -> GenerationResult:
        _validate_request(request)
        check_reference_limit(request, self.reference_limit)
        out = self.root / request.task_id
        parent = out.parent
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{request.task_id}-", dir=str(parent)))
        try:
            payloads = self._build_payloads(request, resolved, omitted)
            for rel, data in payloads:
                target = stage / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(data, bytes):
                    target.write_bytes(data)
                else:
                    target.write_text(data, encoding="utf-8", newline="\n")
            if out.exists():
                shutil.rmtree(out)
            os.replace(stage, out)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return GenerationResult(
            provider=self.provider, model="", image_bytes=b"", mime_type="",
            response_id="", usage={},
            raw_metadata={"package_dir": normalize_rel_path(str(out)),
                          "reference_count": len(resolved)},
            status=STATUS_AWAITING_MANUAL_RESULT,
            detail="已导出自包含任务包，等待人工生成后回传（本执行器不产生图片）",
        )

    def _build_payloads(self, request: GenerationRequest, resolved: Sequence[Any],
                        omitted: Sequence[dict]) -> list[tuple[str, Any]]:
        """包内全部文件（相对包根路径 → 文本或字节），按参考顺序命名。"""
        request_json = json.dumps(self._request_manifest(request, resolved),
                                  ensure_ascii=False, indent=2) + "\n"
        payloads: list[tuple[str, Any]] = [
            ("请求.json", request_json),
            ("提示词.md", self._render_prompt(request)),
            ("参考图清单.md", self._render_references(request, resolved)),
        ]
        files: list[dict] = []
        for index, ref in enumerate(resolved, start=1):
            role_cn = ROLE_CN.get(ref.role, ref.role)
            ext = Path(ref.relative_path).suffix.lower() or ".png"
            name = f"{index:02d}-{role_cn}{ext}"
            data = ref.read_bytes()          # 缺失/空字节在此以稳定码失败
            rel = f"{self.REFERENCE_DIR}/{name}"
            payloads.append((rel, data))
            files.append({
                "path": rel, "role": ref.role, "bytes": len(data),
                "sha256": _sha256_bytes(data), "mime_type": ref.mime_type,
                "source": ref.origin, "source_path": ref.relative_path,
                "note": ref.note,
            })
        for text_name in ("请求.json", "提示词.md", "参考图清单.md"):
            body = next(d for r, d in payloads if r == text_name)
            raw = body.encode("utf-8")
            files.append({"path": text_name, "role": "文件", "bytes": len(raw),
                          "sha256": _sha256_bytes(raw), "mime_type": "text/plain; charset=utf-8",
                          "source": "generated", "source_path": text_name, "note": ""})
        payloads.append((self.PACKAGE_MANIFEST,
                         json.dumps({
                             "task_id": request.task_id, "schema_version": 1,
                             "style_name": request.style_name,
                             "style_version": request.style_version,
                             "aspect_ratio": request.aspect_ratio,
                             "image_size": request.image_size,
                             "files": sorted(files, key=lambda item: item["path"]),
                             "omitted": [dict(item) for item in omitted],
                             "self_contained": True,
                         }, ensure_ascii=False, indent=2) + "\n"))
        return payloads

    @staticmethod
    def _request_manifest(request: GenerationRequest, resolved: Sequence[Any]) -> dict:
        """请求清单：参考图用**解析后**的真实哈希/MIME/字节数，逐张对应实际发送图片。"""
        data = request.to_manifest()
        if resolved:
            data["references"] = [
                {"path": ref.relative_path, "role": ref.role, "sha256": ref.sha256,
                 "mime_type": ref.mime_type, "bytes": ref.bytes, "origin": ref.origin,
                 "note": ref.note}
                for ref in resolved
            ]
        return data

    @staticmethod
    def _render_prompt(request: GenerationRequest) -> str:
        parts = [
            f"# 生图提示词（任务 {request.task_id}）",
            "",
            f"- 画幅：{request.aspect_ratio}｜分辨率：{request.image_size}｜输出：{request.output_mime}",
        ]
        if request.style_name:
            parts.append(f"- 风格：{request.style_name}@{request.style_version}")
        parts += [
            "",
            "## 正向提示词",
            "",
            request.prompt.strip(),
        ]
        if request.negative_prompt.strip():
            parts += ["", "## 负面提示词", "", request.negative_prompt.strip()]
        parts += ["", "> 本文件由任务包导出，可人工在任意生图工具中使用；"
                      "生成后请用 `visual_production.py import` 回传原图。"]
        return "\n".join(parts).rstrip("\n") + "\n"

    def _render_references(self, request: GenerationRequest,
                           resolved: Sequence[Any] = ()) -> str:
        lines = [f"# 参考图清单（任务 {request.task_id}）", ""]
        if not resolved and not request.references:
            lines.append("- （本任务无参考图）")
        for index, ref in enumerate(resolved, start=1):
            role_cn = ROLE_CN.get(ref.role, ref.role)
            ext = Path(ref.relative_path).suffix.lower() or ".png"
            lines.append(
                f"- {role_cn}：`{self.REFERENCE_DIR}/{index:02d}-{role_cn}{ext}`"
                f"（SHA-256 {ref.sha256[:12]}…｜{ref.mime_type}｜{ref.bytes} 字节"
                f"｜来源 {ref.origin}:{ref.relative_path}）"
                + (f"｜{ref.note}" if ref.note else ""))
        if not resolved:
            for binding in request.references:
                role_cn = ROLE_CN.get(binding.role, binding.role)
                short = (binding.sha256 or "")[:12]
                lines.append(f"- {role_cn}：`{normalize_rel_path(binding.path)}`"
                             + (f"（哈希 {short}…）" if short else "")
                             + (f"｜{binding.note}" if binding.note else ""))
        lines += ["", "> 参考图职责不得越权：材质参考不得改变身份，风格参考不得复制其服装与人物。",
                  "> `参考图/` 内为实际选中的图片本体，已随包分发；"
                  "逐文件哈希见 `任务包清单.json`。"]
        return "\n".join(lines).rstrip("\n") + "\n"


def executor_is_configured(env: Mapping[str, str] | None = None) -> bool:
    """是否配置了自动生图执行器（缺配置时改为手工导出，不得冒充自动生成）。

    `env=None`（生产路径）时先按统一顺序加载包外 `.env`
    （`ASSET_ENV_PATH` → 技能目录 `.env` → `~/.asset-extract/.env`），
    否则只放在 .env 里的密钥会被误判为「未配置」。
    **离线模式（`ASSET_OFFLINE` 置真）下隐式回退被屏蔽**，只认显式
    `ASSET_ENV_PATH` 与进程环境：未配密钥即判未配置，且在任何 socket 之前（B8）。
    """
    import os

    if env is None:
        from .common import load_env

        _ = load_env()
        source = dict(os.environ)
    else:
        source = dict(env)
    return bool(str(source.get("GEMINI_API_KEY") or "").strip())


def require_configured_executor(env: Mapping[str, str] | None = None) -> None:
    if not executor_is_configured(env):
        raise GenerationError(
            EXECUTOR_NOT_CONFIGURED,
            "未配置 GEMINI_API_KEY：自动执行不可用。请设置环境变量 GEMINI_API_KEY，"
            "或改用手工导出（visual_production.py export 子命令导出手工提示词任务包）"
            "并在生成后回传；也可显式用 ASSET_ENV_PATH 指向包外 .env 配置密钥")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
