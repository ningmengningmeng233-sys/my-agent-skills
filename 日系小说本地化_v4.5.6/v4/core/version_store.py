"""章节不可变输出版本存储。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .jsonio import write_json_atomic, write_text_atomic


def _version_dir(chapter_dir: Path) -> Path:
    path = chapter_dir / "versions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_version(chapter_dir: Path | str, text: str, *, kind: str = "draft") -> dict[str, Any]:
    chapter_dir = Path(chapter_dir)
    current_path = chapter_dir / "current.json"
    try:
        current = json.loads(current_path.read_text(encoding="utf-8")) if current_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        current = {}
    number = int(current.get("latest_version", 0)) + 1
    version_id = f"v{number:03d}"
    output_path = _version_dir(chapter_dir) / f"{version_id}.md"
    write_text_atomic(output_path, text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    payload = {
        "latest_version": number,
        "latest_sha256": digest,
        "latest_kind": kind,
        "approved_version": current.get("approved_version"),
        "approved_sha256": current.get("approved_sha256"),
    }
    write_json_atomic(current_path, payload)
    return {"version": version_id, "number": number, "path": str(output_path), "sha256": digest, "kind": kind}


def approve_version(chapter_dir: Path | str, version: str) -> dict[str, Any]:
    """批准一个版本；必须存在与该版本强绑定的检查报告 checks/vNNN.json。

    核验两项（fail-closed）：
    1. 绑定报告存在且其 ``version`` 字段与待批版本一致；
    2. 报告 ``output_sha256`` 与版本正文实测哈希一致（版本文件批准前不得被改动）。
    任一不满足即抛 ``ValueError``，批准指针保持不变。
    """
    chapter_dir = Path(chapter_dir)
    current_path = chapter_dir / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    number = int(str(version).lstrip("v"))
    version_id = f"v{number:03d}"
    source = _version_dir(chapter_dir) / f"{version_id}.md"
    if not source.is_file():
        raise FileNotFoundError(source)
    text = source.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    binding_path = chapter_dir / "checks" / f"{version_id}.json"
    if not binding_path.is_file():
        raise ValueError(
            f"{version_id} 没有绑定的检查报告（checks/{version_id}.json 缺失），"
            "请先对最新版本运行 novel check 再批准"
        )
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    if binding.get("version") != version_id:
        raise ValueError(
            f"检查报告绑定版本 {binding.get('version')!r} 与待批版本 {version_id!r} 不一致，拒绝批准"
        )
    if binding.get("output_sha256") != digest:
        raise ValueError(
            f"检查报告 output_sha256 与 {version_id} 正文哈希不一致，"
            "版本可能在检查后被改动，拒绝批准"
        )

    current["approved_version"] = number
    current["approved_sha256"] = digest
    write_json_atomic(current_path, current)
    return current


def clear_approval(chapter_dir: Path | str) -> dict[str, Any]:
    """回滚批准：清除指针但绝不删除任何物理历史版本文件（Task 5.2）。"""
    chapter_dir = Path(chapter_dir)
    current_path = chapter_dir / "current.json"
    current = (
        json.loads(current_path.read_text(encoding="utf-8"))
        if current_path.is_file()
        else {}
    )
    current["approved_version"] = None
    current["approved_sha256"] = None
    write_json_atomic(current_path, current)
    return current
