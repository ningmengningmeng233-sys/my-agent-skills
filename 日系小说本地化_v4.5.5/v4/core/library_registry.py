# -*- coding: utf-8 -*-
"""索引库登记：管理多个用户索引库，供 lib 子命令与自动发现使用。

登记文件默认：<skill>/卡工厂/index-libraries.json
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .audit import now_iso
from .jsonio import read_json, write_json_atomic
from .paths import PROJECT_ROOT

DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "卡工厂" / "index-libraries.json"


class LibraryRegistryError(RuntimeError):
    pass


def load_registry(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_REGISTRY_PATH
    if not p.is_file():
        return {"schema_version": "v4-index-libraries-1", "libraries": []}
    return read_json(p)


def save_registry(doc: dict[str, Any], path: Path | str | None = None) -> None:
    p = Path(path) if path else DEFAULT_REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(p, doc)


def list_libraries(path: Path | str | None = None) -> list[dict[str, Any]]:
    doc = load_registry(path)
    return list(doc.get("libraries", []))


def add_library(
    lib_path: Path | str,
    *,
    name: str | None = None,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """登记一个索引库；已存在则更新时间戳。"""
    lib_path = Path(lib_path).expanduser().resolve()
    doc = load_registry(path)
    libs = doc.setdefault("libraries", [])
    existing = next((item for item in libs if Path(item["path"]).expanduser().resolve() == lib_path), None)
    now = now_iso()
    if existing is not None:
        existing["last_refreshed_at"] = now
        if name:
            existing["name"] = name
        save_registry(doc, path)
        return existing
    if name and any(item.get("name") == name for item in libs):
        raise LibraryRegistryError(f"索引库名称已存在: {name}")
    record = {
        "name": name or lib_path.name or str(lib_path),
        "path": str(lib_path),
        "added_at": now,
        "last_refreshed_at": now,
    }
    libs.append(record)
    save_registry(doc, path)
    return record


def remove_library(name_or_path: str, *, path: Path | str | None = None) -> bool:
    doc = load_registry(path)
    libs = doc.get("libraries", [])
    remaining = []
    removed = False
    for item in libs:
        if item.get("name") == name_or_path or Path(item["path"]).expanduser().resolve() == Path(name_or_path).expanduser().resolve():
            removed = True
            continue
        remaining.append(item)
    if removed:
        doc["libraries"] = remaining
        save_registry(doc, path)
    return removed


def primary_library(path: Path | str | None = None) -> dict[str, Any] | None:
    libs = list_libraries(path)
    return libs[0] if libs else None


def refresh_all(path: Path | str | None = None) -> list[dict[str, Any]]:
    """对全部已登记库执行 card_index.write_card_index。"""
    from . import card_index

    libs = list_libraries(path)
    refreshed: list[dict[str, Any]] = []
    for item in libs:
        lib_path = Path(item["path"])
        try:
            card_index.write_card_index(lib_path)
            item["last_refreshed_at"] = now_iso()
            refreshed.append(item)
        except Exception as exc:  # noqa: BLE001 - 单个库失败不阻断其他库
            item["last_error"] = str(exc)
    if libs:
        doc = load_registry(path)
        doc["libraries"] = libs
        save_registry(doc, path)
    return refreshed
