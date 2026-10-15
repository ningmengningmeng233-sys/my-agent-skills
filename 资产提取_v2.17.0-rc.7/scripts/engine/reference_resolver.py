#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参考图解析与逐文件校验（工单 P0-1）。

定位：
- 路由器（`reference_router`）只决定**选哪些**参考、各担什么职责；
  本模块决定**这些参考能不能用**：逐文件校验存在性、非空、SHA-256、真实 MIME。
- 解析根有两个：**风格包根**（风格包内相对路径）与**资产库根**（资产产物相对路径）。
  绝不依赖 shell 当前工作目录；路径越界、盘符、`..` 一律以稳定码拒绝。
- 校验全部通过才返回可发送对象；任何一项失败都抛 `ReferenceResolveError`，
  **绝不降级为空图或跳过继续生成**（规格：未通过校验不得降级为空图继续）。
- 解析对象携带 `resolved_path` 供本次调用读取字节（只驻留内存），
  对外清单只写相对路径与哈希，绝不泄露机器绝对路径。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .visual_schema import ReferenceBinding, is_relative_safe, normalize_rel_path

ORIGIN_STYLEPACK = "stylepack"
ORIGIN_LIBRARY = "library"

# 稳定原因码（调用方按码分派，不靠文案）
REFERENCE_MISSING = "REFERENCE_MISSING"
REFERENCE_EMPTY = "REFERENCE_EMPTY"
REFERENCE_HASH_MISMATCH = "REFERENCE_HASH_MISMATCH"
REFERENCE_PATH_UNSAFE = "REFERENCE_PATH_UNSAFE"
REFERENCE_NOT_IMAGE = "REFERENCE_NOT_IMAGE"
REFERENCE_MIME_MISMATCH = "REFERENCE_MIME_MISMATCH"
STYLEPACK_UNAVAILABLE = "STYLEPACK_UNAVAILABLE"

# 允许的图片类型（与 Gemini 适配器的 ALLOWED_MIME 同口径）
ALLOWED_MIME = ("image/png", "image/jpeg", "image/webp")

# 扩展名 → 期望 MIME（扩展名与魔数冲突时拒绝）
EXTENSION_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class ReferenceResolveError(ValueError):
    """参考图不可用：code 为稳定原因码，detail 只描述相对路径（不含绝对路径与密钥）。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def sniff_image_mime(data: bytes) -> str:
    """按**真实字节**识别图片类型（不信任扩展名）；无法识别返回空串。"""
    if not data:
        return ""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ResolvedReference:
    """一张通过校验、可发送的参考图。"""

    origin: str
    relative_path: str
    role: str
    sha256: str
    mime_type: str
    bytes: int
    note: str = ""
    # 仅本次调用内存可见；绝不写入任何清单或产物
    resolved_path: Path = field(default_factory=lambda: Path(""), repr=False, compare=False)

    def read_bytes(self) -> bytes:
        """读取真实图片字节（缺失时以稳定码失败，绝不返回空字节）。"""
        try:
            data = self.resolved_path.read_bytes()
        except OSError as e:
            raise ReferenceResolveError(
                REFERENCE_MISSING, f"{self.relative_path} 无法读取：{type(e).__name__}") from e
        if not data:
            raise ReferenceResolveError(REFERENCE_EMPTY, f"{self.relative_path} 为空文件")
        return data

    def to_manifest(self) -> dict:
        """可审查的清单条目：只有相对路径、职责、哈希、MIME、字节数与来源。"""
        return {
            "path": self.relative_path,
            "role": self.role,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "bytes": self.bytes,
            "origin": self.origin,
            "note": self.note,
        }

    def as_binding(self) -> ReferenceBinding:
        """回写计划用的绑定（带哈希，仍只保存相对路径）。"""
        return ReferenceBinding(path=self.relative_path, role=self.role,
                                sha256=self.sha256, note=self.note)


def _candidate_paths(rel: str, stylepack_root: Path | None,
                     library_root: Path | None) -> list[tuple[str, Path]]:
    """按 风格包根 → 资产库根 的顺序给出候选绝对路径（不判存在）。"""
    out: list[tuple[str, Path]] = []
    if stylepack_root is not None:
        out.append((ORIGIN_STYLEPACK, Path(stylepack_root) / rel))
    if library_root is not None:
        out.append((ORIGIN_LIBRARY, Path(library_root) / rel))
    return out


def resolve_references(bindings: Iterable[ReferenceBinding],
                       stylepack_root: Path | None,
                       library_root: Path | None) -> tuple[ResolvedReference, ...]:
    """逐张解析并校验参考图；任一项不通过即抛稳定码失败（不做静默降级）。

    解析顺序：先试风格包根（风格包内相对路径），再试资产库根（资产产物相对路径）。
    同一 (相对路径, 职责) 只保留一次；同图不同职责是合法用法，不去重。
    """
    pack = Path(stylepack_root) if stylepack_root else None
    lib = Path(library_root) if library_root else None
    if lib is None and pack is None:
        raise ReferenceResolveError(STYLEPACK_UNAVAILABLE, "缺少风格包根与资产库根，无法解析参考图")

    results: list[ResolvedReference] = []
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        rel = normalize_rel_path(binding.path)
        role = str(binding.role or "")
        if not is_relative_safe(rel):
            raise ReferenceResolveError(
                REFERENCE_PATH_UNSAFE,
                f"参考图必须是包内/库内相对路径（不得含盘符、开头斜杠或 ..）：{rel!r}")
        key = (rel, role)
        if key in seen:
            continue
        seen.add(key)

        found: tuple[str, Path] | None = None
        for origin, path in _candidate_paths(rel, pack, lib):
            if path.is_file():
                found = (origin, path)
                break
        if found is None:
            if pack is None:
                raise ReferenceResolveError(
                    STYLEPACK_UNAVAILABLE,
                    f"缺少权威风格包根，无法解析风格包参考 {rel}（不得降级为空图继续）")
            raise ReferenceResolveError(REFERENCE_MISSING, f"参考图不存在：{rel}")

        origin, path = found
        try:
            size = path.stat().st_size
        except OSError as e:
            raise ReferenceResolveError(
                REFERENCE_MISSING, f"{rel} 无法访问：{type(e).__name__}") from e
        if size <= 0:
            raise ReferenceResolveError(REFERENCE_EMPTY, f"参考图为零字节：{rel}")

        with open(path, "rb") as fh:
            head = fh.read(16)
        mime = sniff_image_mime(head)
        if not mime:
            raise ReferenceResolveError(
                REFERENCE_NOT_IMAGE, f"参考图不是可识别的图片（支持 PNG/JPEG/WebP）：{rel}")
        ext = path.suffix.lower()
        expected = EXTENSION_MIME.get(ext)
        if expected is not None and expected != mime:
            raise ReferenceResolveError(
                REFERENCE_MIME_MISMATCH,
                f"扩展名 {ext} 与真实格式 {mime} 冲突：{rel}（拒绝按错误 MIME 发送）")

        actual = _sha256_file(path)
        declared = str(binding.sha256 or "").strip()
        if declared and declared.lower() != actual.lower():
            raise ReferenceResolveError(
                REFERENCE_HASH_MISMATCH,
                f"参考图哈希不符（内容已变化）：{rel}（声明 {declared[:12]}… 实际 {actual[:12]}…）")

        results.append(ResolvedReference(
            origin=origin, relative_path=rel, role=role, sha256=actual,
            mime_type=mime, bytes=size, note=str(binding.note or ""),
            resolved_path=path))
    return tuple(results)
