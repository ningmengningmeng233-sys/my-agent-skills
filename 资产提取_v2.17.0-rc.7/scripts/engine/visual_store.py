#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉产物仓库（规格 8，工单03-Task4）。

- 目录契约：`视觉资产/任务/<task_id>/attempt-NN/{image.png,request.json,response.json,qc.toml}`。
- 文件名**固定**，绝不来自模型返回文本或用户原始名称；任务 ID 先过安全校验再落盘。
- 拒绝越界路径、伪装扩展名、空文件、超限文件与哈希不符；原子写入不留半截产物。
- 响应元数据写入前清除密钥与认证头；同一 response ID / 同一图片哈希重导入不产生新轮次。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from .image_executor import GenerationResult
from .visual_schema import AttemptRecord, ValidationIssue, VisualTask, normalize_rel_path

TASKS_SUBDIR = ("视觉资产", "任务")
ATTEMPT_PREFIX = "attempt-"
IMAGE_NAME = "image.png"
REQUEST_NAME = "request.json"
RESPONSE_NAME = "response.json"
QC_NAME = "qc.toml"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MIN_IMAGE_BYTES = 32
SAFE_TASK_ID_RE = re.compile(r"^[^/\\:*?\"<>|]+$")
DECODABLE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
SECRET_KEY_HINTS = ("api_key", "apikey", "authorization", "auth", "token", "secret",
                    "x-goog", "bearer", "cookie", "password")

QC_PLACEHOLDER = """# 质检占位（由 image_qc 写回；未质检前保持 PENDING）
decision = "PENDING"
checker = ""
checked_at = ""
"""


class StoreError(RuntimeError):
    """产物仓库失败：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def attempt_dir(task_id: str, round_no: int) -> str:
    """产物目录（相对资产库，正斜杠）。"""
    return "/".join((*TASKS_SUBDIR, task_id, f"{ATTEMPT_PREFIX}{int(round_no):02d}"))


def _validate_task_id(task_id: str) -> str:
    text = str(task_id or "").strip()
    if not text or not SAFE_TASK_ID_RE.match(text) or ".." in text:
        raise StoreError("INVALID_TASK_ID",
                         f"任务 ID 非法（不得含路径分隔符/通配符/越界段）：{task_id!r}")
    return text


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_image_bytes(data: bytes) -> None:
    if not data:
        raise StoreError("EMPTY_IMAGE", "图片为空字节（拒绝把空文件当生成结果）")
    if len(data) > MAX_IMAGE_BYTES:
        raise StoreError("IMAGE_TOO_LARGE",
                         f"图片 {len(data)} 字节超过上限 {MAX_IMAGE_BYTES} 字节")
    if len(data) < MIN_IMAGE_BYTES:
        raise StoreError("INVALID_IMAGE", f"图片仅 {len(data)} 字节，不可能有效")
    if not _decodable(data):
        raise StoreError("INVALID_IMAGE", "图片无法解码（伪装扩展名或损坏文件一律拒绝）")


def _decodable(data: bytes) -> bool:
    """用 Pillow 真实解码校验（不靠扩展名）。Pillow 不可用时退化为魔数校验。"""
    try:
        import io

        from PIL import Image
    except ImportError:  # 精简安装：退化为魔数识别，仍拒绝非图片
        return (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
                or data[:4] == b"RIFF")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        return True
    except Exception:
        return False


def _sanitize(value: Any) -> Any:
    """递归清除含密钥/认证头气息的字段（键名含 api_key/authorization/token… 即丢弃）。"""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).lower()
            if any(hint in name for hint in SECRET_KEY_HINTS):
                continue
            out[str(key)] = _sanitize(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _next_round(task: VisualTask) -> int:
    return len(task.attempts) + 1


def _find_existing(task: VisualTask, *, response_id: str = "", image_sha: str = "") -> AttemptRecord | None:
    for attempt in task.attempts:
        if response_id and attempt.response_id and attempt.response_id == response_id:
            return attempt
        if image_sha and attempt.image_sha256 and attempt.image_sha256 == image_sha:
            return attempt
    return None


def _write_attempt(lib: Path, task_id: str, round_no: int, image_bytes: bytes,
                   request_manifest: Mapping[str, Any] | None,
                   response_meta: Mapping[str, Any] | None) -> AttemptRecord:
    """原子写入一个轮次目录（先临时目录，再整体替换）。"""
    lib = Path(lib)
    rel = attempt_dir(task_id, round_no)
    target = lib / rel
    tmp = target.with_name(f"{ATTEMPT_PREFIX}{int(round_no):02d}.tmp{os.getpid()}")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        (tmp / IMAGE_NAME).write_bytes(image_bytes)
        (tmp / REQUEST_NAME).write_text(
            json.dumps(_sanitize(request_manifest or {}), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        (tmp / RESPONSE_NAME).write_text(
            json.dumps(_sanitize(response_meta or {}), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        (tmp / QC_NAME).write_text(QC_PLACEHOLDER, encoding="utf-8", newline="\n")
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, target)
    except Exception:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    return AttemptRecord(
        round=int(round_no),
        image=f"{rel}/{IMAGE_NAME}",
        request=f"{rel}/{REQUEST_NAME}",
        response=f"{rel}/{RESPONSE_NAME}",
        qc=f"{rel}/{QC_NAME}",
        image_sha256=_sha256(image_bytes),
        provider=str((response_meta or {}).get("provider") or ""),
        model=str((response_meta or {}).get("model") or ""),
        response_id=str((response_meta or {}).get("response_id") or ""),
    )


def save_attempt(lib: Path, task: VisualTask, result: GenerationResult,
                 request_manifest: Mapping[str, Any] | None = None) -> AttemptRecord:
    """保存一次生成结果（原子落盘）；同一 response ID 重导入返回既有轮次。"""
    task_id = _validate_task_id(task.task_id)
    existing = _find_existing(task, response_id=str(result.response_id or ""))
    if existing is not None:
        return existing
    _check_image_bytes(result.image_bytes)
    meta = {
        "provider": result.provider,
        "model": result.model,
        "mime_type": result.mime_type,
        "response_id": result.response_id,
        "usage": dict(result.usage or {}),
        "status": result.status,
        "detail": result.detail,
        "raw_metadata": dict(result.raw_metadata or {}),
    }
    return _write_attempt(lib, task_id, _next_round(task), result.image_bytes,
                          request_manifest, meta)


def import_manual_result(lib: Path, task_id: str, image: Path,
                         existing: Sequence[AttemptRecord] = (),
                         request_manifest: Mapping[str, Any] | None = None) -> AttemptRecord:
    """导入手工生成结果：固定文件名落盘；同一图片哈希重导入不新增轮次。"""
    safe_id = _validate_task_id(task_id)
    source = Path(image)
    if not source.is_file():
        raise StoreError("IMAGE_NOT_FOUND", f"导入图片不存在：{source}")
    if source.suffix.lower() not in DECODABLE_SUFFIXES:
        raise StoreError("INVALID_IMAGE", f"只接受 {list(DECODABLE_SUFFIXES)} 图片：{source.name}")
    size = source.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise StoreError("IMAGE_TOO_LARGE", f"图片 {size} 字节超过上限 {MAX_IMAGE_BYTES} 字节")
    data = source.read_bytes()
    _check_image_bytes(data)
    digest = _sha256(data)

    records = list(existing)
    if not records:
        records = _load_existing_attempts(Path(lib), safe_id)
    hit = _find_existing(_StubTask(safe_id, tuple(records)), image_sha=digest)
    if hit is not None:
        return hit
    return _write_attempt(lib, safe_id, len(records) + 1, data,
                          request_manifest or {"task_id": safe_id, "source": "manual_import"},
                          {"provider": "manual", "model": "", "mime_type": "",
                           "response_id": "", "status": "MANUAL_RESULT_IMPORTED"})


class _StubTask:
    """仅供 _find_existing 复用的最小任务壳（避免伪造 VisualTask）。"""

    def __init__(self, task_id: str, attempts: Sequence[AttemptRecord]):
        self.task_id = task_id
        self.attempts = tuple(attempts)


def _load_existing_attempts(lib: Path, task_id: str) -> list[AttemptRecord]:
    base = lib / TASKS_SUBDIR[0] / TASKS_SUBDIR[1] / task_id
    if not base.is_dir():
        return []
    records: list[AttemptRecord] = []
    for folder in sorted(base.glob(f"{ATTEMPT_PREFIX}*")):
        if folder.suffix or not folder.is_dir():
            continue
        m = re.search(rf"{ATTEMPT_PREFIX}(\d+)$", folder.name)
        if not m:
            continue
        rel = f"{'/'.join(TASKS_SUBDIR)}/{task_id}/{folder.name}"
        image = folder / IMAGE_NAME
        digest = _sha256(image.read_bytes()) if image.is_file() else ""
        records.append(AttemptRecord(
            round=int(m.group(1)), image=f"{rel}/{IMAGE_NAME}", request=f"{rel}/{REQUEST_NAME}",
            response=f"{rel}/{RESPONSE_NAME}", qc=f"{rel}/{QC_NAME}", image_sha256=digest))
    return records


def verify_attempt(record: AttemptRecord, lib: Path) -> list[ValidationIssue]:
    """校验一次轮次的完整性：文件齐全、图片哈希一致且可解码。"""
    lib = Path(lib)
    issues: list[ValidationIssue] = []
    image = lib / normalize_rel_path(record.image)
    if not image.is_file():
        issues.append(ValidationIssue("IMAGE_MISSING", f"产物图片缺失：{record.image}"))
        return issues
    data = image.read_bytes()
    digest = _sha256(data)
    if record.image_sha256 and digest != record.image_sha256:
        issues.append(ValidationIssue("IMAGE_HASH_MISMATCH",
                                      f"图片哈希与记录不符：{record.image}"))
    if not _decodable(data):
        issues.append(ValidationIssue("IMAGE_UNDECODABLE", f"图片无法解码：{record.image}"))
    for rel in (record.request, record.response, record.qc):
        if rel and not (lib / normalize_rel_path(rel)).is_file():
            issues.append(ValidationIssue("ATTEMPT_FILE_MISSING", f"轮次文件缺失：{rel}"))
    return issues
