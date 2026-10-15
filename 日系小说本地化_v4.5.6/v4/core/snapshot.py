"""输入归一化只读快照。

- 单文件：复制为 ``source/original.<ext>``，记录完整 SHA-256 与检测编码；
- 章节文件夹：按文件名自然排序复制到 ``source/files/<file-id>.<ext>``，
  清单记录相对路径、顺序、编码与完整 SHA-256；
- 工作区后续检查一律以快照为准，绝不直接读取用户可能继续变化的源文件
  （v4 spec §2.1 / §5.1 / §12.3）。

只依赖标准库与本包 ``hashing``。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hashing import sha256_bytes

VALID_EXTENSIONS = {".txt", ".md", ".markdown", ".text"}
SOURCE_STATUS = ("pending", "ready")
_IGNORED_NAMES = {".v4.lock", ".ds_store", "thumbs.db", "desktop.ini", "~$"}
_NATURAL_SPLIT = re.compile(r"(\d+)")
_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_LE_BOM = b"\xff\xfe"
_UTF16_BE_BOM = b"\xfe\xff"
_GB18030 = "gb18030"


class SnapshotError(ValueError):
    """快照输入不可用（缺失、空目录、无可读文件等），应进入阻塞状态。"""


@dataclass
class SnapshotResult:
    """快照产物：``source`` 为可直接写入 manifest 的 source 对象；
    ``texts`` 为按顺序排列的已解码文本，供章节切分使用。"""

    source: dict[str, Any]
    texts: list[dict[str, Any]]


def decode_bytes(data: bytes) -> tuple[str, str]:
    """返回 (解码文本, 编码标签)。

    优先级：BOM（utf-8-sig / utf-16）→ utf-8 严格 → gb18030 严格 →
    utf-8 replace 兜底。解码失败只影响文本内容，快照字节始终原样保留。
    """
    if data.startswith(_UTF8_BOM):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith(_UTF16_LE_BOM):
        return data.decode("utf-16"), "utf-16"
    if data.startswith(_UTF16_BE_BOM):
        return data.decode("utf-16-be"), "utf-16-be"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode(_GB18030), _GB18030
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "unknown-replacement"


def natural_sort_key(value: str) -> list[Any]:
    """文件名自然排序键：chapter2 < chapter10，f10 < f2。"""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in _NATURAL_SPLIT.split(value)
    ]


def _write_bytes_atomic(data: bytes, dest: Path) -> None:
    """按已被哈希的字节原样写入快照，保证快照与清单哈希一致（C-015/C-016）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, dest)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _combined_sha(files: list[dict[str, Any]]) -> str:
    """文件夹快照的确定性聚合哈希：按顺序对 (ordinal, source_path, sha256) 求值。"""
    digest = hashlib.sha256()
    for entry in files:
        digest.update(
            f"{entry['ordinal']}:{entry['source_path']}:{entry['sha256']}\n".encode(
                "utf-8"
            )
        )
    return digest.hexdigest()


def _collect_files(directory: Path) -> list[Path]:
    files: list[Path] = []
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        if path.name in _IGNORED_NAMES or path.name.startswith("."):
            continue
        if path.suffix.lower() not in VALID_EXTENSIONS:
            continue
        files.append(path)
    files.sort(key=lambda p: natural_sort_key(p.relative_to(directory).as_posix()))
    return files


def _snapshot_file(workspace: Path, source: Path) -> SnapshotResult:
    data = source.read_bytes()
    text, encoding = decode_bytes(data)
    sha = sha256_bytes(data)
    ext = source.suffix.lower() or ".txt"
    snapshot_path = f"source/original{ext}"
    _write_bytes_atomic(data, workspace / snapshot_path)
    files = [
        {
            "ordinal": 1,
            "source_path": source.name,
            "snapshot_path": snapshot_path,
            "sha256": sha,
        }
    ]
    source_obj = {
        "status": "pending",
        "path": str(source),
        "kind": "file",
        "sha256": sha,
        "snapshot_path": snapshot_path,
        "snapshot_sha256": sha,
        "encoding": encoding,
        "boundary_status": "awaiting_confirmation",
        "files": files,
    }
    texts = [
        {
            "ordinal": 1,
            "source_path": source.name,
            "snapshot_path": snapshot_path,
            "text": text,
            "encoding": encoding,
            "sha256": sha,
        }
    ]
    return SnapshotResult(source=source_obj, texts=texts)


def _snapshot_folder(workspace: Path, source: Path) -> SnapshotResult:
    files = _collect_files(source)
    if not files:
        raise SnapshotError(f"文件夹内没有可读的小说稿文件: {source}")
    snapshot_dir = workspace / "source" / "files"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    file_entries: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    encodings: set[str] = set()
    for index, path in enumerate(files, start=1):
        rel = path.relative_to(source).as_posix()
        data = path.read_bytes()
        text, encoding = decode_bytes(data)
        encodings.add(encoding)
        sha = sha256_bytes(data)
        ext = path.suffix.lower() or ".txt"
        snapshot_path = f"source/files/f{index:03d}{ext}"
        _write_bytes_atomic(data, workspace / snapshot_path)
        file_entries.append(
            {
                "ordinal": index,
                "source_path": rel,
                "snapshot_path": snapshot_path,
                "sha256": sha,
            }
        )
        texts.append(
            {
                "ordinal": index,
                "source_path": rel,
                "snapshot_path": snapshot_path,
                "text": text,
                "encoding": encoding,
                "sha256": sha,
            }
        )
    combined = _combined_sha(file_entries)
    source_obj = {
        "status": "pending",
        "path": str(source),
        "kind": "folder",
        "sha256": combined,
        "snapshot_path": "source",
        "snapshot_sha256": combined,
        "encoding": sorted(encodings)[0] if len(encodings) == 1 else "mixed",
        "boundary_status": "awaiting_confirmation",
        "files": file_entries,
    }
    return SnapshotResult(source=source_obj, texts=texts)


def snapshot_source(workspace: Path | str, source: Path | str) -> SnapshotResult:
    """把用户源路径归一化为只读快照，返回 manifest.source 结构与顺序文本。

    - 单文件：``source/original.<ext>``
    - 文件夹：``source/files/<file-id>.<ext>`` + 顺序清单

    source 缺失 / 文件夹为空 / 无可读文件时抛出 :class:`SnapshotError`。
    """
    workspace = Path(workspace)
    source = Path(source)
    if not source.exists():
        raise SnapshotError(f"源路径不存在: {source}")
    if source.is_file():
        return _snapshot_file(workspace, source)
    if source.is_dir():
        return _snapshot_folder(workspace, source)
    raise SnapshotError(f"源路径既不是文件也不是目录: {source}")


def with_boundary_status(source: dict[str, Any], boundary_status: str) -> dict[str, Any]:
    """按章节切分结果更新 manifest.source 的边界状态。

    ``confirmed`` → ``status: ready``；``awaiting_confirmation``/``blocked`` → ``pending``。
    返回新的 dict，不改动传入对象。
    """
    if boundary_status not in {"confirmed", "awaiting_confirmation", "blocked"}:
        raise ValueError(f"非法 boundary_status: {boundary_status!r}")
    updated = dict(source)
    updated["boundary_status"] = boundary_status
    updated["status"] = "ready" if boundary_status == "confirmed" else "pending"
    return updated
