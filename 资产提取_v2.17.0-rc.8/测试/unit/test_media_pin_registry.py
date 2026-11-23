# -*- coding: utf-8 -*-
"""媒体钉单常驻复核：包内每张图都必须逐件登记且摘要相符，缺登记或摘要漂移即红。

这条是**反向尺**：分母取自盘上媒体件，而不是登记清单本身——
只按 MEDIA_PIN.json 自查会造出「登记了几张就只验几张」的假绿，
新增图不登记就永远不会被发现（与 10-02 dramacraft 的现场同源）。
"""
from __future__ import annotations

import hashlib
import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
PIN = REPO / "scripts" / "package_governance" / "MEDIA_PIN.json"
MEDIA_SUFFIX = {".webp", ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mov"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules", ".lemon"}


def on_disk_media() -> set[str]:
    out = set()
    for p in REPO.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in MEDIA_SUFFIX:
            continue
        if SKIP_DIRS & set(p.relative_to(REPO).parts[:-1]):
            continue
        out.add(p.relative_to(REPO).as_posix())
    return out


def test_media_pin_file_exists_and_is_declared():
    assert PIN.is_file(), f"缺少媒体钉单 {PIN}"
    data = json.loads(PIN.read_text(encoding="utf-8"))
    assert data.get("schema_version") == "media-pin-1", data.get("schema_version")
    assert str(data.get("note", "")).strip(), "钉单必须写明这批媒体凭什么进包"
    assert data.get("human_review") in ("pending", "done"), data.get("human_review")
    assert isinstance(data.get("media"), dict) and data["media"], "media 不能为空"


def test_every_media_file_on_disk_is_pinned():
    """反向完整性：盘上有图未钉 → 红（不许靠「登记了几张验几张」蒙过）。"""
    pins = json.loads(PIN.read_text(encoding="utf-8"))["media"]
    unpinned = sorted(on_disk_media() - set(pins))
    assert not unpinned, f"这些媒体件没有登记进 MEDIA_PIN.json：{unpinned}"


def test_pinned_media_exist_with_matching_digest():
    pins = json.loads(PIN.read_text(encoding="utf-8"))["media"]
    missing, mismatch = [], []
    for rel, meta in sorted(pins.items()):
        fp = REPO / rel
        if not fp.is_file():
            missing.append(rel)
            continue
        actual = hashlib.sha256(fp.read_bytes()).hexdigest()
        if actual != meta.get("sha256"):
            mismatch.append(rel)
        if fp.stat().st_size != meta.get("bytes"):
            mismatch.append(f"{rel}（字节数不符）")
    assert not missing, f"钉单里的媒体在盘上找不到：{missing}"
    assert not mismatch, f"钉单摘要与盘上不符（图被改过但没重跑登记）：{mismatch}"


def test_pin_and_disk_counts_agree():
    pins = json.loads(PIN.read_text(encoding="utf-8"))["media"]
    disk = on_disk_media()
    assert set(pins) == disk, (f"钉单 {len(pins)} 件与盘上 {len(disk)} 件不一致；"
                               f"多 {sorted(set(pins) - disk)[:3]} 缺 {sorted(disk - set(pins))[:3]}")
