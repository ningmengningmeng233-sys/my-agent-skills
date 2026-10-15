#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""私有风格包安装与校验（工单 P1-2）。

背景：权威参考图体积大，`.gitignore` 排除了它们，因此 **Git 检出不是完整视觉安装**。
本命令从**本地私有 zip** 装回完整包，并逐文件校验存在性/大小/哈希：

    python scripts/install_stylepack.py --from <私有包.zip> --style <中文名> --version <版本>
    python scripts/install_stylepack.py --verify --pack <内容根> [--json]

纪律：
- **绝不自动从未知网络下载**（只接受本地文件路径）；
- 安装到临时目录 → 全部校验通过 → 原子替换目标目录；失败不留半个包；
- 缺 manifest、缺文件、哈希不符一律以稳定码拒绝，绝不用半包冒充完整包。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))

from engine.style_registry import STYLES_DIR, resolve_style_record  # noqa: E402
from engine.stylepack_verify import (  # noqa: E402
    MANIFEST_FILE,
    iter_pack_files,
    pack_content_hash,
    verify_pack,
)

STYLEPACK_MANIFEST_MISSING = "STYLEPACK_MANIFEST_MISSING"
STYLEPACK_INCOMPLETE = "STYLEPACK_INCOMPLETE"
STYLEPACK_HASH_MISMATCH = "STYLEPACK_HASH_MISMATCH"
STYLEPACK_LAYOUT_UNKNOWN = "STYLEPACK_LAYOUT_UNKNOWN"
STYLEPACK_SOURCE_UNSAFE = "STYLEPACK_SOURCE_UNSAFE"

# 安装包里允许出现的最大解压体积（防止 zip 炸弹）
MAX_UNPACKED_BYTES = 512 * 1024 * 1024


class StylepackInstallError(RuntimeError):
    """安装失败：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """安全解压：拒绝绝对路径、`..` 越界与符号链接条目。"""
    total = 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise StylepackInstallError(STYLEPACK_SOURCE_UNSAFE, f"包内路径越界：{name}")
        if info.is_dir():
            continue
        total += int(info.file_size)
        if total > MAX_UNPACKED_BYTES:
            raise StylepackInstallError(
                STYLEPACK_SOURCE_UNSAFE, f"解压体积超过上限 {MAX_UNPACKED_BYTES} 字节")
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def _find_layout(root: Path) -> tuple[Path, Path]:
    """在解压结果里定位 (版本目录, 内容根=版本目录/private)。

    支持两种布局：包根直接是版本目录，或包内多一层套壳目录。
    """
    candidates: list[Path] = []
    if (root / MANIFEST_FILE).is_file():
        candidates.append(root)
    for child in sorted(root.rglob(MANIFEST_FILE)):
        candidates.append(child.parent)
    for version_dir in candidates:
        content = version_dir / "private"
        if content.is_dir():
            return version_dir, content
    raise StylepackInstallError(
        STYLEPACK_MANIFEST_MISSING,
        f"包内找不到 {MANIFEST_FILE}（私有完整包必须自带清单）")


def _check_layout_complete(content_root: Path) -> None:
    """安装前逐文件校验：缺文件 / 哈希不符即拒绝。"""
    issues = verify_pack(content_root, require_images=True)
    if not issues:
        return
    missing = [i for i in issues if "缺失" in i]
    if missing:
        raise StylepackInstallError(STYLEPACK_INCOMPLETE, "；".join(missing[:3]))
    raise StylepackInstallError(STYLEPACK_HASH_MISMATCH, "；".join(issues[:3]))


def install_stylepack(zip_path: Path | str, target_styles_dir: Path | str,
                      style_name: str, version: str) -> dict:
    """从本地 zip 安装私有风格包到 `<target>/<style>/<version>/`（原子替换）。"""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise StylepackInstallError(STYLEPACK_SOURCE_UNSAFE, f"安装包不存在：{zip_path.name}")
    target = Path(target_styles_dir) / style_name / version
    # 暂存目录与目标同盘（保证 replace 是原子的）；只建到 styles 根，
    # 风格目录留到校验通过后再建，安装失败时目标侧不留任何痕迹。
    styles_root = Path(target_styles_dir)
    styles_root.mkdir(parents=True, exist_ok=True)
    stage_parent = Path(tempfile.mkdtemp(prefix=".stylepack-", dir=str(styles_root)))
    try:
        unpacked = stage_parent / "unpacked"
        unpacked.mkdir()
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad:
                raise StylepackInstallError(STYLEPACK_INCOMPLETE, f"压缩包损坏：{bad}")
            _safe_extract(zf, unpacked)
        version_dir, content_root = _find_layout(unpacked)
        _check_layout_complete(content_root)

        staged = stage_parent / "staged"
        shutil.copytree(version_dir, staged)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        staged.replace(target)
    except BaseException:
        shutil.rmtree(stage_parent, ignore_errors=True)
        raise
    shutil.rmtree(stage_parent, ignore_errors=True)

    content = target / "private"
    return {
        "installed_to": str(target),
        "style": style_name,
        "version": version,
        "installed": sum(1 for _ in iter_pack_files(content)),
        "content_sha256": pack_content_hash(content),
        "integrity_ok": verify_pack(content, require_images=True) == [],
    }


def verify_stylepack(pack_dir: Path | str, require_images: bool = True) -> dict:
    """校验已安装风格包；返回结构化报告。"""
    root = Path(pack_dir)
    issues = verify_pack(root, require_images=require_images)
    files = list(iter_pack_files(root)) if root.is_dir() else []
    images = [rel for rel, _ in files
              if Path(rel).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    return {
        "pack": str(root),
        "present": root.is_dir(),
        "file_count": len(files),
        "image_count": len(images),
        "integrity_ok": not issues,
        "issues": issues,
        "content_sha256": pack_content_hash(root) if root.is_dir() else "",
    }


def _resolve_pack_dir(pack: str | None, style: str | None, version: str | None) -> Path:
    """定位内容根：显式 --pack 优先，否则按风格名+版本从登记表解析。"""
    if pack:
        return Path(pack)
    if not style:
        raise StylepackInstallError(
            STYLEPACK_LAYOUT_UNKNOWN, "需要 --pack，或同时给出 --style 与 --version")
    record = resolve_style_record(style)
    if record is None:
        raise StylepackInstallError(STYLEPACK_LAYOUT_UNKNOWN, f"风格 {style!r} 不在登记表中")
    from engine.style_registry import pack_root

    return Path(pack_root(record.pack_slug, version or record.pack_version))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="私有风格包安装与校验（不发网络请求）")
    parser.add_argument("--from", dest="source", help="本地私有风格包 zip（不接受 URL）")
    parser.add_argument("--style", help="风格中文正式名（登记表解析）")
    parser.add_argument("--version", help="风格包版本（默认取登记版本）")
    parser.add_argument("--to", help="目标 styles 目录（默认技能内 references/styles）")
    parser.add_argument("--verify", action="store_true", help="只校验，不安装")
    parser.add_argument("--pack", help="要校验的内容根（默认由风格名解析）")
    parser.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    args = parser.parse_args(argv)

    try:
        if args.source and args.source.lower().startswith(("http://", "https://")):
            raise StylepackInstallError(
                STYLEPACK_SOURCE_UNSAFE, "只接受本地 zip 路径，不自动从网络下载")
        if args.verify:
            pack_dir = _resolve_pack_dir(args.pack, args.style, args.version)
            report = verify_stylepack(pack_dir)
            _emit(report, args.json)
            return 0 if report["integrity_ok"] else 1
        if not args.source:
            raise StylepackInstallError(
                STYLEPACK_LAYOUT_UNKNOWN, "请给出 --from <私有包.zip>，或用 --verify 校验")
        record = resolve_style_record(args.style) if args.style else None
        style_name = record.pack_slug if record else args.style
        version = args.version or (record.pack_version if record else "")
        if not style_name or not version:
            raise StylepackInstallError(
                STYLEPACK_LAYOUT_UNKNOWN, "安装需要 --style（登记名或中文名）与 --version")
        result = install_stylepack(args.source, args.to or STYLES_DIR, style_name, version)
        _emit(result, args.json)
        return 0
    except StylepackInstallError as e:
        payload = {"ok": False, "code": e.code, "detail": e.detail}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"❌ 风格包安装/校验失败（{e.code}）：{e.detail}")
        return 1


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if "integrity_ok" in payload and "installed" not in payload:
        print(f"🧩 风格包校验：{'通过' if payload['integrity_ok'] else '失败'}")
        print(f"   内容根：{payload['pack']}")
        print(f"   文件 {payload['file_count']} 个（其中参考图 {payload['image_count']} 张）")
        for issue in payload["issues"]:
            print(f"   ❌ {issue}")
        if payload["content_sha256"]:
            print(f"   内容哈希：{payload['content_sha256']}")
        return
    print(f"✅ 风格包已安装：{payload['installed_to']}")
    print(f"   文件 {payload['installed']} 个｜内容哈希 {payload['content_sha256']}")


if __name__ == "__main__":
    sys.exit(main())
