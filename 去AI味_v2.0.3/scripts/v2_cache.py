#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_cache.py - 可重建的向量缓存（v2.0.0 alpha.2 P07）。

合同（设计方案 9 节）：

  1. 缓存是**既有 job 的可重建附属数据**，不是授权证据：整目录删掉后同一批请求
     还能重算出来，运行身份与授权判定都不引用它。
  2. 缓存键把身份全带上——文本哈希、上下文哈希、规则版本、Prompt 版本、**模型实际
     revision**、裁判纪元（judge_epoch）与项目偏好版本。换裁判不能复用旧判定，
     换模型不能复用旧向量。
  3. 键不同、文件缺失、内容不合形状都算「没有可用缓存」并分开计数，不得静默当成
     命中，更不得拿半截向量当判定证据。
  4. 写盘沿用 ``novel_job._atomic_write``（同目录临时文件 + os.replace + sort_keys），
     本模块不另写第二套原子文件算法；同内容重复写字节不变。
  5. 向量以 base64 原始字节存放并记录 dtype 与 shape，还原后**逐位相同**，
     不做有损转换。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import numpy as np

import novel_job

CACHE_DIR_NAME = "v2_vector_cache"
CACHE_KIND = "v2-vector-cache"
CACHE_SHAPE_VERSION = 1
_IDENTITY_FIELDS = ("text_hash", "context_hash", "rule_version", "prompt_version",
                    "model_revision", "judge_epoch", "preference_version")
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def cache_key(*, text_hash: str, context_hash: str, rule_version: str,
              prompt_version: str, model_revision: str, judge_epoch: int,
              preference_version: str) -> str:
    """把七项身份压成 64 位十六进制键；任何一项变化都必须得到不同键。

    字段值本身不校验语义（那是各生产方的合同），但**不许缺席或空串**——缺席会
    让不同身份塌成同一个键，缓存就会把旧判定冒充成新判定。
    """
    payload: dict = {}
    for name, value in (("text_hash", text_hash), ("context_hash", context_hash),
                        ("rule_version", rule_version), ("prompt_version", prompt_version),
                        ("model_revision", model_revision),
                        ("preference_version", preference_version)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"缓存身份 {name} 必须是非空字符串，收到 {value!r}")
        payload[name] = value.strip()
    if isinstance(judge_epoch, bool) or not isinstance(judge_epoch, int):
        raise ValueError(f"judge_epoch 必须是整数，收到 {judge_epoch!r}")
    if judge_epoch < 0:
        raise ValueError(f"judge_epoch 不得为负: {judge_epoch}")
    payload["judge_epoch"] = judge_epoch
    stable = json.dumps({"kind": CACHE_KIND, "shape": CACHE_SHAPE_VERSION,
                         "identity": payload}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


class VectorCache:
    """按缓存键存取单位向量；缺失或损坏一律按 MISS 处理并分开计数。"""

    def __init__(self, job_dir: Path | str) -> None:
        self.job_dir = Path(job_dir)
        self.root = self.job_dir / CACHE_DIR_NAME
        self._hits = 0
        self._misses = 0
        self._corrupt = 0

    def _path(self, key: str) -> Path:
        if not _KEY_RE.match(key or ""):
            raise ValueError(f"缓存键必须由 cache_key 生成（64 位十六进制）: {key!r}")
        return self.root / f"{key}.json"

    def get(self, key: str) -> np.ndarray | None:
        path = self._path(key)
        payload = novel_job._read_json(path)
        if payload is None:
            self._misses += 1
            return None
        vector = _vector_from_payload(payload)
        if vector is None:
            self._corrupt += 1
            return None
        self._hits += 1
        return vector

    def put(self, key: str, vector: np.ndarray, *, identity: dict | None = None) -> None:
        """写入向量；``identity`` 是该键的七项身份原文（P13 换裁判要用）。

        键是哈希，事后从键认不出纪元——「旧判定已随换裁判失效」这句话就没法机械
        核对。所以这里要求写入方把身份原文一并登记，并**当场验证键确实由这份身份
        推出**（不符即拒，防止登记一个指鹿指马的纪元）。没登记身份的旧写入件由
        ``discard_stale`` 计为 undetermined，不算「当前纪元的可用缓存」。
        """
        if not isinstance(vector, np.ndarray) or vector.ndim != 1 or vector.size == 0:
            raise ValueError(f"缓存向量必须是一维非空数组，收到 {type(vector).__name__}")
        recorded = None
        if identity is not None:
            if not isinstance(identity, dict):
                raise ValueError(f"identity 必须是映射，收到 {type(identity).__name__}")
            if cache_key(**identity) != key:
                raise ValueError("identity 与缓存键不一致：拒绝登记指鹿指马的纪元")
            recorded = dict(identity)
        self.root.mkdir(parents=True, exist_ok=True)
        array = np.ascontiguousarray(vector)
        novel_job._atomic_write(self._path(key), {
            "kind": CACHE_KIND,
            "shape_version": CACHE_SHAPE_VERSION,
            "key": key,
            "dtype": str(array.dtype),
            "shape": list(array.shape),
            "data_b64": base64.b64encode(array.tobytes()).decode("ascii"),
            "identity": recorded,
        })

    def stats(self) -> dict:
        return {"entries": len(list(self.root.glob("*.json"))) if self.root.is_dir() else 0,
                "hits": self._hits, "misses": self._misses, "corrupt": self._corrupt}


def discard_stale(job_dir: Path | str, *, keep_epoch: int) -> dict:
    """删掉不属于 ``keep_epoch`` 的缓存条目，并分开计数三种结局（P13 换裁判）。

    - 键是哈希，事后从键认不出纪元，所以判定只看写入时登记的 ``identity``；
    - **没登记纪元的条目按「不可归因」处理并删除**：换裁判后留着指不清来源的
      旧条目，等于让旧判定冒充新判定；缓存本来就是可重建附属数据（合同 1），
      删错了最多重算，留下才咬人。这类删除单独计 ``undetermined``，不并入
      「旧纪元已清理」，也不静默当成清理干净。
    - 只动本 job 缓存目录里形状合规的 ``<64 位十六进制>.json``，其余一律不碰。
    """
    if isinstance(keep_epoch, bool) or not isinstance(keep_epoch, int):
        raise ValueError(f"keep_epoch 必须是整数，收到 {keep_epoch!r}")
    return _discard_by_identity_field(job_dir, "judge_epoch", keep_epoch)


def discard_stale_preferences(job_dir: Path | str, *,
                              keep_preference_version: str) -> dict:
    """删掉不属于 ``keep_preference_version`` 的条目（P14 项目偏好变更）。

    与 :func:`discard_stale` 同一判据、同一计数口径，只是比的字段换成偏好版本——
    两维各清各的，不把「换裁判」与「改偏好」塞进同一个函数互相顶包。
    """
    if not isinstance(keep_preference_version, str) or not keep_preference_version.strip():
        raise ValueError("keep_preference_version 必须是非空字符串，收到 "
                         f"{keep_preference_version!r}")
    return _discard_by_identity_field(job_dir, "preference_version",
                                      keep_preference_version)


def _discard_by_identity_field(job_dir: Path | str, field: str, keep_value) -> dict:
    root = Path(job_dir) / CACHE_DIR_NAME
    verdict = {"deleted": 0, "kept": 0, "undetermined": 0}
    if not root.is_dir():
        return verdict
    for path in sorted(root.glob("*.json")):
        if not _KEY_RE.fullmatch(path.stem):
            continue
        payload = novel_job._read_json(path)
        identity = payload.get("identity") if isinstance(payload, dict) else None
        value = identity.get(field) if isinstance(identity, dict) else None
        if value == keep_value:
            verdict["kept"] += 1
            continue
        if value is None:
            verdict["undetermined"] += 1
        try:
            path.unlink()
        except OSError as exc:
            raise ValueError(f"缓存条目删除失败，不谎报已清理：{path.name}（{exc}）") from exc
        verdict["deleted"] += 1
    return verdict


def _vector_from_payload(payload) -> np.ndarray | None:
    """不合形状/损坏一律返回 None——绝不返回半截向量冒充命中。"""
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != CACHE_KIND:
        return None
    try:
        data = base64.b64decode(payload["data_b64"], validate=True)
        dtype = np.dtype(payload["dtype"])
        shape = tuple(payload["shape"])
    except (KeyError, TypeError, ValueError):
        return None
    if not shape or shape[0] * dtype.itemsize != len(data):
        return None
    return np.frombuffer(data, dtype=dtype).reshape(shape).copy()
