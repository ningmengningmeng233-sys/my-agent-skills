#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""风格包清单校验（工单 P0-2 / P1-2）。

风格包自带 `manifest.toml`（逐文件 SHA-256 / 字节数 / 类别 / 分享许可）。
本模块是**校验的唯一实现**：同步脚本、发布构建、运行期编译都复用同一份逻辑，
避免「同步时说一致、运行时说缺图」这类双实现漂移。

放在引擎层的原因：视觉编译（`visual_request_chain`）必须在**不依赖同步脚本**的前提下
自证权威包完整——否则技能目录结构一变，运行期就会静默退化成「无参考图生成」。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

MANIFEST_FILE = "manifest.toml"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
SOURCE_PREFIX = "references/sources/"
# 运行必需参考图的前缀（router 实际引用的主体/材质图都在此下）
RUNTIME_IMAGE_PREFIX = "references/"
EXCLUDED_NAMES = (".env", ".env.local", ".env.example")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_path_for_pack(pack_dir: Path) -> Path:
    """命令清单位置：风格包内容根的**上一级**（随版本目录一起分发）。"""
    return Path(pack_dir).parent / MANIFEST_FILE


def read_pack_manifest(pack_dir: Path) -> dict:
    """读取风格包清单；缺失或不可解析返回空 dict（由调用方决定是否报错）。"""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 回退
        import tomli as tomllib

    path = manifest_path_for_pack(pack_dir)
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def iter_pack_files(pack_dir: Path):
    """按确定顺序遍历包内文件；跳过符号链接、密钥文件与同步临时目录。"""
    root = Path(pack_dir)
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name in EXCLUDED_NAMES:
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".tmp") or ".tmp" in Path(rel).parts[0]:
            continue
        yield rel, path


def inspect_pack(pack_dir: Path, manifest: dict | None = None,
                 require_images: bool = False) -> tuple[list[str], list[str]]:
    """按清单校验包内容，返回 (硬问题, 提示信息)。

    硬问题：必需文件缺失、哈希漂移、清单外多出文件、require_images 时缺图。
    提示信息：参考图未随包分发（精简安装合法，分享包需声明不完整）。
    """
    root = Path(pack_dir)
    if manifest is None:
        manifest = read_pack_manifest(root)
    issues: list[str] = []
    infos: list[str] = []
    expected = {str(f.get("path", "")): f for f in manifest.get("files", []) if isinstance(f, dict)}
    on_disk = {rel for rel, _ in iter_pack_files(root)}
    missing_images: list[str] = []
    for rel, entry in sorted(expected.items()):
        path = root / rel
        if not path.is_file():
            if bool(entry.get("required")) or require_images:
                issues.append(f"清单文件缺失：{rel}")
            else:
                missing_images.append(rel)
            continue
        if sha256_file(path) != entry.get("sha256"):
            issues.append(f"内容漂移（哈希与清单不符）：{rel}")
    for rel in sorted(on_disk - set(expected)):
        issues.append(f"清单外多出文件：{rel}")
    if missing_images:
        infos.append(f"参考图未随包分发：{len(missing_images)} 张"
                     f"（{'、'.join(missing_images[:3])}…；精简安装合法，分享包需声明不完整）")
    return issues, infos


def verify_pack(pack_dir: Path, require_images: bool = False) -> list[str]:
    """校验内置包与清单一致；问题清单为空即通过。

    `require_images=True` 表示**运行必需**参考图必须齐备。判定分两类：

    - **运行必需图**（`references/` 下，router 实际引用的 20 张）：缺一张即问题；
    - **来源图**（`references/sources/`）：蒸馏溯源的授权证据，清单里
      `share_allowed=false`。分享包按策略排除它们属**设计如此**，不是「风格包不完整」——
      否则分享包永远无法通过校验，与 `VISUAL_STYLE_INCOMPLETE_FOR_SHARE` 声明矛盾。
    """
    root = Path(pack_dir)
    manifest = read_pack_manifest(root)
    if not manifest:
        return [f"缺少或无法解析 {MANIFEST_FILE}：{root}"]
    issues, _infos = inspect_pack(root, manifest, require_images=require_images)
    if not require_images:
        return issues
    return [item for item in issues if not _is_source_entry(item)]


def runtime_image_issues(pack_dir: Path) -> list[str]:
    """**运行必需**参考图（router 实际引用的主体/材质图）缺失/漂移清单。"""
    root = Path(pack_dir)
    manifest = read_pack_manifest(root)
    if not manifest:
        return [f"缺少或无法解析 {MANIFEST_FILE}：{root}"]
    issues: list[str] = []
    for entry in manifest.get("files", []):
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("path") or "")
        if not rel.startswith(RUNTIME_IMAGE_PREFIX):
            continue
        if rel.startswith(SOURCE_PREFIX):
            continue
        if Path(rel).suffix.lower() not in IMAGE_SUFFIXES:
            continue
        path = root / rel
        if not path.is_file():
            issues.append(f"运行必需参考图缺失：{rel}")
        elif sha256_file(path) != entry.get("sha256"):
            issues.append(f"运行必需参考图漂移：{rel}")
    return issues


def pack_is_share_form(pack_dir: Path) -> bool:
    """该包是否为**分享形态**（来源图被策略排除，但运行必需图齐备）。"""
    sources = Path(pack_dir) / SOURCE_PREFIX
    if not sources.is_dir():
        return True
    return not any(p.is_file() for p in sources.rglob("*"))


def _is_source_entry(issue: str) -> bool:
    return SOURCE_PREFIX in issue or "sources/" in issue


def pack_infos(pack_dir: Path, require_images: bool = False) -> list[str]:
    """非阻塞提示（参考图缺失情况等），供校验命令如实报告而不误判失败。"""
    root = Path(pack_dir)
    manifest = read_pack_manifest(root)
    if not manifest:
        return []
    _issues, infos = inspect_pack(root, manifest, require_images)
    return infos


def pack_content_hash(pack_dir: Path) -> str:
    """风格包**当前真实内容**的聚合哈希（逐文件路径 + SHA-256 再哈希）。

    用途：把「任务所依赖的风格源」钉成可比较的指纹。权威包任一文件被改动、
    增删，指纹都会变化，从而使旧计划失效（防止风格漂移后仍按旧规则出图）。
    不依赖 manifest，因此清单被改也不能掩盖内容变化。
    """
    root = Path(pack_dir)
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    for rel, path in iter_pack_files(root):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
