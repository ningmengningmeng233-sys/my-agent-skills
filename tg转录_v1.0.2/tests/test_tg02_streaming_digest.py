# -*- coding: utf-8 -*-
"""TG-02 回归：附件与大文件哈希计算必须分块流式迭代，严禁整包读入（GB 级视频 OOM）。

受控 RED：以下断言在修复前必须失败，修复后通过。全 mock/stub，不碰真实 tdl/网络。
策略：对 >1MB 文件禁用 Path.read_bytes（抛 AssertionError），并用 2.5MB 合成文件
做分块间谍（单次 update ≤ 1MB、摘要与整包一致、内存峰值恒定）。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import media
import sharing

BIG = 1 << 20
PAYLOAD = 2 * BIG + 512 * 1024  # 2.5MB：大于分块，远小于 OOM


@pytest.fixture()
def big_file(tmp_path):
    target = tmp_path / "big-video.bin"
    block = bytes(range(256)) * 4096  # 1MB 确定性内容
    with open(target, "wb") as handle:
        handle.write(block)
        handle.write(block)
        handle.write(bytes(range(256)) * 2048)
    assert target.stat().st_size == PAYLOAD
    return target


def _whole_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


@pytest.fixture()
def no_whole_read_of_large_files(monkeypatch):
    """大文件整包 read_bytes 即判红： product 代码命中即抛，非 OSError 不会被吞。"""
    real = Path.read_bytes

    def _guard(self):
        try:
            size = self.stat().st_size
        except OSError:
            return real(self)
        if size > BIG:
            raise AssertionError("大文件严禁整包 read_bytes：%s (%d bytes)"
                                 % (self.name, size))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", _guard)


@pytest.fixture()
def hash_spy(monkeypatch):
    """记录每次摘要输入字节数；返回 (sizes, 真实 sha256)。"""
    real_sha256 = hashlib.sha256
    sizes = []

    class _Spy:
        def __init__(self, data=b""):
            self._hash = real_sha256()
            if data:
                sizes.append(len(data))
                self._hash.update(data)

        def update(self, chunk):
            sizes.append(len(chunk))
            self._hash.update(chunk)

        def hexdigest(self):
            return self._hash.hexdigest()

    monkeypatch.setattr(hashlib, "sha256", lambda data=b"": _Spy(data))
    return sizes, real_sha256


def test_media_digest_of_streams_in_chunks(big_file, hash_spy):
    """包内成熟分块实现（media._digest_of）：多次小块更新，摘要与整包一致。"""
    sizes, _real = hash_spy
    digest = media._digest_of(big_file)
    assert len(sizes) > 1, "2.5MB 文件必须多次 update，不能一次读入"
    assert max(sizes) <= BIG
    assert sum(sizes) == PAYLOAD
    expected = hashlib.sha256()
    with open(big_file, "rb") as handle:
        for chunk in iter(lambda: handle.read(BIG), b""):
            expected.update(chunk)
    assert digest == expected.hexdigest()


def test_write_download_index_streams_source(big_file, tmp_path,
                                           no_whole_read_of_large_files):
    """索引源文件（可达 GB 级的导出 JSON）不得整包读入；摘要/体积与落盘一致。"""
    selection = {
        "attachments": [{"message_id": 7, "downloadable": True, "category": "video",
                         "expected_size": None}],
        "messages": [{"id": 7, "media": {"_": "messageMediaDocument"}}],
        "unknown_ids": [],
        "ambiguous_flag_ids": [],
        "not_downloadable_ids": [],
        "beyond_upper_bound_ids": [],
    }
    report = media.write_download_index(
        selection, tmp_path / "idx", identity="peer:channel:1", chat_id=1,
        source_path=str(big_file), fingerprint="fp")
    source = report["payload"]["source"]
    assert source["size"] == PAYLOAD
    assert source["sha256"] == _whole_digest(big_file)


def test_accepted_export_streams_instead_of_whole_read(big_file, tmp_path,
                                                     monkeypatch,
                                                     no_whole_read_of_large_files):
    """导出验收的摘要核对必须流式；state.validate_export 桩掉以聚焦 media 自身。"""
    digest = _whole_digest(big_file)
    manifest = {"segments": {"s1": {"sha256": digest, "size": PAYLOAD, "count": 1,
                                   "identity": "peer:channel:1", "chat_id": 1,
                                   "lower": 1, "upper": 10}}}
    monkeypatch.setattr(media.state, "load_manifest", lambda root: manifest)
    stub = {"messages": [], "path": str(big_file), "name": big_file.name,
            "sha256": digest, "size": PAYLOAD, "count": 1}
    monkeypatch.setattr(media.state, "validate_export", lambda *a, **k: stub)
    task = {"identity": "peer:channel:1", "chat_id": 1, "plan_lower": 1,
            "snapshot_upper_bound": 10}
    report = media.accepted_export(str(big_file), tmp_path, task)
    assert report["sha256"] == digest


def test_sharing_copy_media_streams_attachments(big_file, tmp_path,
                                              no_whole_read_of_large_files):
    """生成分享副本时的附件落盘校验：大附件必须流式比对，不得整包读入。"""
    items = {0: {"rel": "media/big-video.bin", "source": str(big_file),
                 "name": "big-video.bin"}}
    copied, not_landed = sharing._copy_media(tmp_path / "share", items)
    assert copied == ["big-video.bin"], not_landed
    assert not_landed == []
    landed = tmp_path / "share" / "media" / "big-video.bin"
    assert _whole_digest(landed) == _whole_digest(big_file)


def test_sharing_digest_streams_in_chunks(big_file, hash_spy):
    """分享侧文件摘要与 media 同口径：1MB 分块流式迭代。"""
    sizes, _real = hash_spy
    digest = sharing._digest(big_file)
    assert len(sizes) > 1
    assert max(sizes) <= BIG
    assert sum(sizes) == PAYLOAD
    expected = hashlib.sha256()
    with open(big_file, "rb") as handle:
        for chunk in iter(lambda: handle.read(BIG), b""):
            expected.update(chunk)
    assert digest == expected.hexdigest()
