# -*- coding: utf-8 -*-
"""内容寻址缓存（v2.17.0 工单 8）。

- **只复用完整成功结果**：失败、部分输出、审核不通过、结构版本不兼容一律不命中。
- **原子写**：payload 与索引都走 `tmp + os.replace`，任何时刻读到的都是完整文件。
- **单写者语义**：写路径持内核级独占锁（Windows `msvcrt.locking` / POSIX `flock`），
  进程崩溃时锁随句柄自动释放，不留删不掉的锁文件。
- `inspect` / `invalidate` / `prune`；`prune` **默认 dry-run**，不自动删除用户产物。

柠檬裁决：本模块自带一份 12 行的加锁原语，而不是从 `visual_scheduler` 复用——
那边为了取一个文件锁要拖进 `image_executor`/`review_contract`/`visual_schema` 整条运行期依赖链，
让一个缓存原语依赖整个视觉运行期是更大的耦合。两处共用同一内核原语（msvcrt/flock），语义一致。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Mapping

SCHEMA_VERSION = 1
PROCESS_DIRNAME = ".process"
CACHE_DIRNAME = "内容缓存"
INDEX_NAME = "index.json"
PAYLOAD_NAME = "payload.bin"
LOCK_NAME = "内容缓存.lock"

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_PARTIAL = "partial"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_SUCCESS, STATUS_FAILED, STATUS_PARTIAL, STATUS_REJECTED)
_REUSABLE = (STATUS_SUCCESS,)
_PRUNABLE = (STATUS_FAILED, STATUS_PARTIAL, STATUS_REJECTED)

_FORBIDDEN_IN_KEY = re.compile(r"[/\\]|\.\.|\x00")
_THREAD_LOCK = threading.Lock()


class CacheError(RuntimeError):
    """缓存层显式失败（非法键、索引损坏等），不得退化成「没缓存过」。"""


def safe_key(key: str) -> str:
    """校验逻辑键：非空、无路径分隔、无 `..`（防目录穿越）。逻辑键本身可含 `:` 等任意字符。"""
    text = str(key or "").strip()
    if not text or _FORBIDDEN_IN_KEY.search(text):
        raise CacheError(f"非法缓存键：{key!r}")
    return text


def _dir_name(key: str) -> str:
    """逻辑键 → 文件系统安全的短目录名（保留可读前缀 + 哈希防碰撞）。

    逻辑键可能含 `:`、`?` 等 Windows 非法文件名字符，因此不能直接拿键当目录名；
    但键与目录必须一一对应，所以尾部附键的摘要，不做有损替换。
    """
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    prefix = re.sub(r"[^0-9A-Za-z_\-]+", "-", key)[:24].strip("-") or "entry"
    return f"{prefix}-{digest}"


@dataclass(frozen=True)
class CacheEntry:
    key: str
    fingerprint: str
    schema_version: int
    status: str
    size: int
    rel_path: str
    reused: bool = False


@contextmanager
def _kernel_lock(path: Path) -> Iterator[None]:
    """跨进程独占锁；同进程内再用线程锁串行，避免同线程重入死锁。"""
    with _THREAD_LOCK:
        handle = os.open(str(path), os.O_CREAT | os.O_RDWR)
        try:
            os.lseek(handle, 0, os.SEEK_SET)
            if os.name == "nt":  # pragma: no cover - 平台分支
                import msvcrt

                msvcrt.locking(handle, msvcrt.LK_LOCK, 1)
            else:  # pragma: no cover - 平台分支
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            try:
                os.lseek(handle, 0, os.SEEK_SET)
                if os.name == "nt":  # pragma: no cover - 平台分支
                    import msvcrt

                    msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - 平台分支
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                os.close(handle)


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


class ContentCache:
    """以 `<lib>/.process/内容缓存/` 为根的键 → 指纹 → 载荷缓存。"""

    def __init__(self, lib, *, schema_version: int = SCHEMA_VERSION) -> None:
        self.root = Path(lib) / PROCESS_DIRNAME / CACHE_DIRNAME
        self.root.mkdir(parents=True, exist_ok=True)
        self.schema_version = int(schema_version)
        self.index_path = self.root / INDEX_NAME
        self.lock_path = self.root / LOCK_NAME

    # --- 索引读写 -------------------------------------------------------------

    def _read_index(self) -> dict:
        if not self.index_path.is_file():
            return {}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CacheError(f"{INDEX_NAME} 不可读：{exc}") from exc
        entries = data.get("entries") if isinstance(data, dict) else None
        return entries if isinstance(entries, dict) else {}

    def _write_index(self, entries: Mapping[str, dict]) -> None:
        payload = {"schema_version": self.schema_version, "entries": dict(entries)}
        _atomic_write(self.index_path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    # --- 对外接口 -------------------------------------------------------------

    def put(self, key: str, fingerprint: str, payload: bytes, *,
            status: str = STATUS_SUCCESS) -> CacheEntry:
        """写入缓存。同键同指纹同状态的**完整成功**结果直接复用，不重复落盘。"""
        name = safe_key(key)
        if status not in STATUSES:
            raise CacheError(f"未知缓存状态：{status!r}")
        rel = f"{_dir_name(name)}/{PAYLOAD_NAME}"
        body = bytes(payload or b"")

        with _kernel_lock(self.lock_path):
            entries = self._read_index()
            old = entries.get(name) or {}
            stored = self.root / str(old.get("rel_path", rel))
            same_meta = (old.get("fingerprint") == fingerprint
                         and old.get("status") == status
                         and int(old.get("schema_version", -1)) == self.schema_version
                         and stored.is_file())
            if same_meta:
                existing = stored.read_bytes()
                if existing == body:
                    return CacheEntry(name, fingerprint, self.schema_version, status,
                                      len(existing), str(old.get("rel_path", rel)), reused=True)
                # 指纹相同但载荷不同 = 指纹函数没覆盖某个输入维度。
                # 静默沿用旧内容会把上游缺陷藏起来，必须显式失败。
                raise CacheError(
                    f"缓存键 {name!r} 指纹碰撞：指纹 {fingerprint} 相同但载荷不同"
                    f"（已存 {len(existing)} 字节，新写入 {len(body)} 字节）——请检查指纹组成是否漏了输入维度")
            target_dir = self.root / _dir_name(name)
            target_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.root / rel, body)
            entries[name] = {
                "fingerprint": fingerprint,
                "schema_version": self.schema_version,
                "status": status,
                "rel_path": rel,
            }
            self._write_index(entries)
        return CacheEntry(name, fingerprint, self.schema_version, status, len(body), rel)

    def get(self, key: str, fingerprint: str) -> bytes | None:
        """仅当「存在 + 指纹一致 + 完整成功 + 结构版本一致」同时满足才命中。"""
        try:
            name = safe_key(key)
        except CacheError:
            return None
        entries = self._read_index()
        entry = entries.get(name)
        if not isinstance(entry, Mapping):
            return None
        if entry.get("status") not in _REUSABLE:
            return None
        if entry.get("fingerprint") != fingerprint:
            return None
        if int(entry.get("schema_version", -1)) != self.schema_version:
            return None
        path = self.root / str(entry.get("rel_path", f"{_dir_name(name)}/{PAYLOAD_NAME}"))
        if not path.is_file():
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def inspect(self) -> List[CacheEntry]:
        """列出全部缓存条目（含失败/部分/不通过），供人工判断而不是自动清理。"""
        out: List[CacheEntry] = []
        for name, entry in sorted(self._read_index().items()):
            if not isinstance(entry, Mapping):
                continue
            rel = str(entry.get("rel_path", f"{_dir_name(name)}/{PAYLOAD_NAME}"))
            path = self.root / rel
            out.append(CacheEntry(
                key=name,
                fingerprint=str(entry.get("fingerprint", "")),
                schema_version=int(entry.get("schema_version", 0)),
                status=str(entry.get("status", "")),
                size=path.stat().st_size if path.is_file() else 0,
                rel_path=rel,
            ))
        return out

    def invalidate(self, key: str) -> bool:
        """移除单个键（索引 + 载荷）。返回是否确实移除过。"""
        try:
            name = safe_key(key)
        except CacheError:
            return False
        with _kernel_lock(self.lock_path):
            entries = self._read_index()
            if name not in entries:
                return False
            entries.pop(name)
            self._write_index(entries)
            self._remove_key_dir(name)
        return True

    def prune(self, *, apply: bool = False) -> List[str]:
        """列出（默认）或删除失败/部分/审核不通过的条目。

        **默认 dry-run**：不显式传 `apply=True` 就绝不删任何东西；只动缓存目录自身，不碰用户产物。
        """
        entries = self._read_index()
        doomed = sorted(name for name, entry in entries.items()
                        if isinstance(entry, Mapping) and entry.get("status") in _PRUNABLE)
        if not apply or not doomed:
            return doomed
        with _kernel_lock(self.lock_path):
            fresh = self._read_index()
            for name in doomed:
                if fresh.pop(name, None) is not None:
                    self._remove_key_dir(name)
            self._write_index(fresh)
        return doomed

    def _remove_key_dir(self, name: str) -> None:
        """只删缓存根目录下的该键子目录，路径必须落在缓存根内。"""
        target = (self.root / _dir_name(name)).resolve()
        root = self.root.resolve()
        if root != target and root in target.parents:
            for path in sorted(target.rglob("*"), reverse=True):
                try:
                    path.unlink() if path.is_file() else path.rmdir()
                except OSError:
                    pass
            try:
                target.rmdir()
            except OSError:
                pass
