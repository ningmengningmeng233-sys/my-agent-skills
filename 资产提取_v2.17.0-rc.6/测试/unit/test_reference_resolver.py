#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 P0-1：参考图解析与校验（新增 reference_resolver）。

红测目标（修复前必须失败）：
- 解析器必须在**生成前**逐文件校验存在性、非空、SHA-256、MIME；
- 缺失/越界/空字节/哈希不符/扩展名与魔数冲突一律以稳定原因码拒绝，绝不降级为空图；
- 清单只写相对路径，绝不泄露机器绝对路径。
"""
import base64
import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.reference_resolver import (  # noqa: E402
    ORIGIN_LIBRARY,
    ORIGIN_STYLEPACK,
    ReferenceResolveError,
    resolve_references,
    sniff_image_mime,
)
from scripts.engine.visual_schema import ReferenceBinding  # noqa: E402

# 真实 4x4 PNG（Pillow verify() 通过）
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")
# 真实 1x1 JPEG（含正确 SOI/EOI 魔数）
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4n"
    "ICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/E"
    "ABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")


def _write(root: Path, rel: str, data: bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def roots(tmp_path):
    """(风格包根, 资产库根)，各放一张真实图片。"""
    pack = tmp_path / "风格包"
    lib = tmp_path / "资产库"
    _write(pack, "references/face.png", PNG)
    _write(lib, "视觉资产/任务/CH_甲-正脸-v01/attempt-01/image.png", PNG)
    return pack, lib


# --- MIME 嗅探 -------------------------------------------------------------------

def test_sniff_png_and_jpeg():
    assert sniff_image_mime(PNG) == "image/png"
    assert sniff_image_mime(JPEG) == "image/jpeg"


def test_sniff_webp():
    webp = b"RIFF" + (0).to_bytes(4, "little") + b"WEBPVP8 "
    assert sniff_image_mime(webp) == "image/webp"


def test_sniff_unknown_returns_empty():
    assert sniff_image_mime(b"not an image at all") == ""
    assert sniff_image_mime(b"") == ""


# --- 正常解析 --------------------------------------------------------------------

def test_resolve_stylepack_reference(roots):
    pack, lib = roots
    got = resolve_references(
        [ReferenceBinding(path="references/face.png", role="style")],
        stylepack_root=pack, library_root=lib)
    assert len(got) == 1
    ref = got[0]
    assert ref.origin == ORIGIN_STYLEPACK
    assert ref.relative_path == "references/face.png"
    assert ref.role == "style"
    assert ref.sha256 == _sha(PNG)
    assert ref.mime_type == "image/png"
    assert ref.bytes == len(PNG)
    assert ref.resolved_path.is_file()


def test_resolve_library_reference(roots):
    pack, lib = roots
    rel = "视觉资产/任务/CH_甲-正脸-v01/attempt-01/image.png"
    got = resolve_references([ReferenceBinding(path=rel, role="identity")],
                             stylepack_root=pack, library_root=lib)
    assert got[0].origin == ORIGIN_LIBRARY
    assert got[0].relative_path == rel


def test_manifest_never_leaks_absolute_path(roots):
    pack, lib = roots
    got = resolve_references([ReferenceBinding(path="references/face.png", role="style")],
                             stylepack_root=pack, library_root=lib)
    manifest = got[0].to_manifest()
    dumped = repr(manifest)
    assert str(pack) not in dumped
    assert str(lib) not in dumped
    assert ":" not in manifest["path"].replace("/", ""), "清单路径必须是相对路径"
    assert manifest["sha256"] == _sha(PNG)
    assert manifest["mime_type"] == "image/png"
    assert manifest["bytes"] == len(PNG)


def test_resolved_relative_path_uses_forward_slashes(roots):
    pack, lib = roots
    got = resolve_references(
        [ReferenceBinding(path="references\\face.png", role="style")],
        stylepack_root=pack, library_root=lib)
    assert "\\" not in got[0].relative_path


# --- 失败码：生成前必须拒绝 --------------------------------------------------------

def test_missing_file_rejected(roots):
    pack, lib = roots
    with pytest.raises(ReferenceResolveError) as ctx:
        resolve_references([ReferenceBinding(path="references/不存在.png", role="style")],
                           stylepack_root=pack, library_root=lib)
    assert ctx.value.code == "REFERENCE_MISSING"


def test_zero_byte_rejected(roots):
    pack, lib = roots
    _write(pack, "references/空图.png", b"")
    with pytest.raises(ReferenceResolveError) as ctx:
        resolve_references([ReferenceBinding(path="references/空图.png", role="style")],
                           stylepack_root=pack, library_root=lib)
    assert ctx.value.code == "REFERENCE_EMPTY"


def test_hash_mismatch_rejected(roots):
    pack, lib = roots
    declared = ReferenceBinding(path="references/face.png", role="style", sha256="0" * 64)
    with pytest.raises(ReferenceResolveError) as ctx:
        resolve_references([declared], stylepack_root=pack, library_root=lib)
    assert ctx.value.code == "REFERENCE_HASH_MISMATCH"


def test_path_escape_rejected(roots, tmp_path):
    pack, lib = roots
    outside = tmp_path / "外部.png"
    outside.write_bytes(PNG)
    with pytest.raises(ReferenceResolveError) as ctx:
        resolve_references([ReferenceBinding(path="../外部.png", role="style")],
                           stylepack_root=pack, library_root=lib)
    assert ctx.value.code == "REFERENCE_PATH_UNSAFE"


def test_absolute_path_rejected(roots):
    pack, lib = roots
    with pytest.raises(ReferenceResolveError) as ctx:
        resolve_references([ReferenceBinding(path=str(pack / "references" / "face.png"),
                                             role="style")],
                           stylepack_root=pack, library_root=lib)
    assert ctx.value.code == "REFERENCE_PATH_UNSAFE"


def test_non_image_rejected(roots):
    pack, lib = roots
    _write(pack, "references/文本.png", b"this is plain text, not an image")
    with pytest.raises(ReferenceResolveError) as ctx:
        resolve_references([ReferenceBinding(path="references/文本.png", role="style")],
                           stylepack_root=pack, library_root=lib)
    assert ctx.value.code == "REFERENCE_NOT_IMAGE"


def test_extension_conflicts_with_magic_rejected(roots):
    """扩展名与魔数冲突（PNG 字节却叫 .jpg）必须拒绝。"""
    pack, lib = roots
    _write(pack, "references/伪装.jpg", PNG)
    with pytest.raises(ReferenceResolveError) as ctx:
        resolve_references([ReferenceBinding(path="references/伪装.jpg", role="style")],
                           stylepack_root=pack, library_root=lib)
    assert ctx.value.code == "REFERENCE_MIME_MISMATCH"


def test_unresolvable_in_either_root_rejected(roots):
    """既不在风格包根也不在资产库根的相对路径必须显式失败，不得静默跳过。"""
    pack, lib = roots
    with pytest.raises(ReferenceResolveError) as ctx:
        resolve_references([ReferenceBinding(path="随便/某处.png", role="style")],
                           stylepack_root=pack, library_root=lib)
    assert ctx.value.code == "REFERENCE_MISSING"


def test_missing_stylepack_root_rejected(lib_root_only):
    lib = lib_root_only
    with pytest.raises(ReferenceResolveError) as ctx:
        resolve_references([ReferenceBinding(path="references/face.png", role="style")],
                           stylepack_root=None, library_root=lib)
    assert ctx.value.code == "STYLEPACK_UNAVAILABLE"


@pytest.fixture
def lib_root_only(tmp_path):
    lib = tmp_path / "资产库"
    lib.mkdir()
    return lib


# --- 顺序与去重 ------------------------------------------------------------------

def test_order_preserved(tmp_path):
    pack = tmp_path / "p"
    lib = tmp_path / "l"
    _write(pack, "references/a.png", PNG)
    _write(pack, "references/b.png", PNG)
    _write(lib, "c.png", PNG)
    got = resolve_references(
        [ReferenceBinding(path="references/a.png", role="identity"),
         ReferenceBinding(path="c.png", role="structure"),
         ReferenceBinding(path="references/b.png", role="style")],
        stylepack_root=pack, library_root=lib)
    assert [r.relative_path for r in got] == ["references/a.png", "c.png", "references/b.png"]
    assert [r.role for r in got] == ["identity", "structure", "style"]


def test_duplicate_role_same_path_kept_once(tmp_path):
    pack = tmp_path / "p"
    lib = tmp_path / "l"
    _write(pack, "references/a.png", PNG)
    got = resolve_references(
        [ReferenceBinding(path="references/a.png", role="style"),
         ReferenceBinding(path="references/a.png", role="style")],
        stylepack_root=pack, library_root=lib)
    assert len(got) == 1


def test_same_path_different_role_kept(tmp_path):
    """同图不同职责是合法用法（如既作结构又作材质），不得误去重。"""
    pack = tmp_path / "p"
    lib = tmp_path / "l"
    _write(pack, "references/a.png", PNG)
    got = resolve_references(
        [ReferenceBinding(path="references/a.png", role="structure"),
         ReferenceBinding(path="references/a.png", role="material")],
        stylepack_root=pack, library_root=lib)
    assert len(got) == 2


def test_empty_bindings_ok(roots):
    pack, lib = roots
    assert resolve_references([], stylepack_root=pack, library_root=lib) == ()
