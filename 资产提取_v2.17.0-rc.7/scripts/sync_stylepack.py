#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Omni 私人完整包同步（规格 5/8，工单02-Task2）。

契约：
- 母版是唯一上游；同步先生成**内容清单与哈希**，校验通过后才原子替换目标目录。
- 不跟随符号链接、不复制 `.env` 等密钥文件，不写入任何机器绝对路径（清单只记 basename）。
- `build_style_manifest(source)` 是纯内容函数：只产出文件相对路径/SHA-256/字节数/类别/
  分享许可与聚合哈希，可在任意机器之间逐字节比较。

用法：
    python scripts/sync_stylepack.py --source <母版目录> --style 仙侠国风电影CG --version 1.0.0
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
from engine.style_registry import pack_root  # noqa: E402
from engine.style_registry import version_dir_for as _version_dir_for  # noqa: E402

MANIFEST_FILE = "manifest.toml"
SCHEMA_VERSION = 1

# 同步必须存在的核心文件（缺一即拒绝同步，绝不用半个包冒充完整包）
CORE_RELATIVE_FILES = (
    "generation_formula.md",
    "negative_prompt.md",
    "references/shared_style_invariants.md",
    "references/router.md",
    "references/face_base_style.md",
    "references/full_body_base_style.md",
    "references/environment_base_style.md",
    "references/object_base_style.md",
)

# 绝不进入内置包的文件（密钥与本地配置）
EXCLUDED_NAMES = (".env", ".env.local", ".env.example")

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
SOURCE_PREFIX = "references/sources/"

# 类别：先精确匹配，再按前缀/后缀兜底
CATEGORY_BY_PATH = {
    "SKILL.md": "风格说明",
    "generation_formula.md": "生成公式",
    "negative_prompt.md": "负面约束",
    "references/shared_style_invariants.md": "共享不变量",
    "references/router.md": "参考路由器",
    "references/iteration_notes.md": "迭代记录",
    "references/face_base_style.md": "人物脸部",
    "references/face.png": "人物脸部",
    "references/full_body_base_style.md": "人物全身",
    "references/full_body.png": "人物全身",
    "references/environment_base_style.md": "环境",
    "references/environment.png": "环境",
    "references/object_base_style.md": "物件",
    "references/object.png": "物件",
}


class SyncError(ValueError):
    """同步失败：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def version_dir_for(style_name: str, version: str) -> Path:
    """版本目录（委托登记层，路径规则单一来源）。"""
    return _version_dir_for(style_name, version)


def pack_dir_for(style_name: str, version: str) -> Path:
    """内置包内容根：references/styles/<中文正式名>/<版本>/private/。"""
    return pack_root(style_name, version)


def manifest_path_for(style_name: str, version: str) -> Path:
    return version_dir_for(style_name, version) / MANIFEST_FILE


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def category_for(rel: str) -> str:
    """文件类别（清单可读性 + 分享许可判定）。"""
    if rel in CATEGORY_BY_PATH:
        return CATEGORY_BY_PATH[rel]
    if rel.startswith(SOURCE_PREFIX):
        return "来源图"
    if rel.startswith("references/materials/"):
        return "材料参考图" if Path(rel).suffix.lower() in IMAGE_SUFFIXES else "材质说明"
    if rel in CORE_RELATIVE_FILES:
        return "核心风格"
    return "其他"


def is_share_allowed(rel: str) -> bool:
    """来源图未经授权检查不得进入分享包（规格 2.8）；其余素材可分享。"""
    return not rel.startswith(SOURCE_PREFIX)


def is_required(rel: str) -> bool:
    """风格定义层（Markdown）必须随包分发；参考图体积大，允许精简安装缺失。"""
    return Path(rel).suffix.lower() not in IMAGE_SUFFIXES


def _iter_source_files(source: Path):
    """按确定顺序遍历源目录；跳过符号链接、密钥文件与同步临时目录。"""
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        if path.name in EXCLUDED_NAMES:
            continue
        rel = path.relative_to(source).as_posix()
        if rel.startswith(".tmp") or ".tmp" in Path(rel).parts[0]:
            continue
        yield rel, path


def build_style_manifest(source: Path) -> dict:
    """纯内容清单：文件相对路径 / SHA-256 / 字节数 / 类别 / 分享许可 + 聚合哈希。

    不含任何机器绝对路径，因此可在不同机器之间逐字节比较。
    """
    source = Path(source)
    files = []
    for rel, path in _iter_source_files(source):
        files.append({
            "path": rel,
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
            "category": category_for(rel),
            "share_allowed": is_share_allowed(rel),
            "required": is_required(rel),
        })
    files.sort(key=lambda f: f["path"])
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": SCHEMA_VERSION,
        "file_count": len(files),
        "total_bytes": sum(f["bytes"] for f in files),
        "content_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "files": files,
    }


def _toml_str(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_manifest_toml(manifest: dict, style: str, version: str) -> str:
    """确定性 TOML 渲染：同一内容必然产生同一字节序列。"""
    lines = [
        "# 风格包清单（工单02-Task2）：每个文件的相对路径 / SHA-256 / 字节数 / 类别 / 分享许可",
        "# 由 scripts/sync_stylepack.py 从母版生成；修改内置包会使校验失败。",
        f"schema_version = {int(manifest['schema_version'])}",
        f"style = {_toml_str(style)}",
        f"version = {_toml_str(version)}",
        f"file_count = {int(manifest['file_count'])}",
        f"total_bytes = {int(manifest['total_bytes'])}",
        f"content_sha256 = {_toml_str(manifest['content_sha256'])}",
        "",
    ]
    for entry in manifest["files"]:
        lines.append("[[files]]")
        lines.append(f"path = {_toml_str(entry['path'])}")
        lines.append(f"sha256 = {_toml_str(entry['sha256'])}")
        lines.append(f"bytes = {int(entry['bytes'])}")
        lines.append(f"category = {_toml_str(entry['category'])}")
        lines.append(f"share_allowed = {'true' if entry['share_allowed'] else 'false'}")
        lines.append(f"required = {'true' if entry['required'] else 'false'}")
        lines.append("")
    return "\n".join(lines)


def sync_stylepack(source: Path, destination: Path, version: str,
                   style_name: str | None = None) -> Path:
    """同步母版到内置包：先临时目录，校验后原子替换；返回 manifest 路径。"""
    source = Path(source)
    destination = Path(destination)
    if not source.is_dir():
        raise SyncError("STYLEPACK_SOURCE_MISSING", f"母版目录不存在：{source}")
    missing = [rel for rel in CORE_RELATIVE_FILES if not (source / rel).is_file()]
    if missing:
        raise SyncError("STYLEPACK_SOURCE_INCOMPLETE",
                        f"母版缺少核心文件：{'、'.join(missing)}（拒绝用半个包冒充完整包）")

    manifest = build_style_manifest(source)
    style = style_name or destination.parent.parent.name
    tmp = destination.with_name(destination.name + f".tmp{os.getpid()}")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    try:
        for entry in manifest["files"]:
            src_file = source / entry["path"]
            dst_file = tmp / entry["path"]
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_file, dst_file)
        # 落盘后现算哈希，不以源端缓存值代替（规格：同步后再次从目标文件计算）
        issues, _infos = inspect_pack(tmp, manifest, require_images=False)
        if issues:
            raise SyncError("STYLEPACK_VERIFY_FAILED", "；".join(issues[:3]))
        backup = None
        if destination.exists():
            backup = destination.with_name(destination.name + f".old{os.getpid()}")
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(destination, backup)
        os.replace(tmp, destination)
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        manifest_path = destination.parent / MANIFEST_FILE
        manifest_path.write_text(render_manifest_toml(manifest, style, version),
                                 encoding="utf-8", newline="\n")
    except Exception:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    return manifest_path


def _read_manifest(pack_dir: Path) -> dict:
    from engine.stylepack_verify import read_pack_manifest

    return read_pack_manifest(pack_dir)


def inspect_pack(pack_dir: Path, manifest: dict | None = None,
                 require_images: bool = False) -> tuple[list[str], list[str]]:
    """按清单校验目录内容，返回 (硬问题, 提示信息)。

    校验逻辑只有一份实现（`engine.stylepack_verify`），避免同步与运行期双实现漂移。
    """
    from engine.stylepack_verify import inspect_pack as _inspect

    return _inspect(pack_dir, manifest, require_images)


def verify_pack(pack_dir: Path, require_images: bool = False) -> list[str]:
    """校验内置包与清单一致；问题清单为空即通过。"""
    from engine.stylepack_verify import verify_pack as _verify

    return _verify(pack_dir, require_images)


def pack_infos(pack_dir: Path, require_images: bool = False) -> list[str]:
    """非阻塞提示（参考图缺失情况等），供校验命令如实报告而不误判失败。"""
    from engine.stylepack_verify import pack_infos as _infos

    return _infos(pack_dir, require_images)


def main() -> int:
    parser = argparse.ArgumentParser(description="从视觉母版同步 Omni 风格私人完整包")
    parser.add_argument("--source", required=True, help="权威母版目录（只读，不会被修改）")
    parser.add_argument("--style", required=True, help="风格中文正式名（如 仙侠国风电影CG）")
    parser.add_argument("--version", required=True, help="包版本（如 1.0.0）")
    parser.add_argument("--dry-run", action="store_true", help="只生成清单与哈希，不写盘")
    args = parser.parse_args()

    source = Path(args.source)
    destination = pack_dir_for(args.style, args.version)
    try:
        if args.dry_run:
            manifest = build_style_manifest(source)
            print(f"ℹ️ 干跑：{manifest['file_count']} 个文件 / {manifest['total_bytes']} 字节"
                  f"｜content_sha256={manifest['content_sha256'][:16]}…")
            return 0
        manifest_path = sync_stylepack(source, destination, args.version, style_name=args.style)
    except SyncError as e:
        print(f"❌ 同步失败（{e.code}）：{e.detail}")
        return 1
    issues = verify_pack(destination, require_images=False)
    manifest = _read_manifest(destination)
    print(f"✅ 同步完成：{args.style}@{args.version}")
    print(f"   内置包: {destination}")
    print(f"   清单: {manifest_path}")
    print(f"   文件: {manifest.get('file_count')} 个 / {manifest.get('total_bytes')} 字节"
          f"｜content_sha256={str(manifest.get('content_sha256'))[:16]}…")
    for issue in issues:
        print(f"   ⚠️ {issue}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
