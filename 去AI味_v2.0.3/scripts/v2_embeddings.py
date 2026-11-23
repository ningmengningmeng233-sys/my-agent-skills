#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_embeddings.py - BGE 向量与长文窗口（v2.0.0 alpha.2 P07）。

职责边界（设计方案 3/4 节、用户要求「不重写 Embedding/ANN」）：

  1. **只做接线**。推理全部走上游 ``FlagEmbedding.FlagModel.encode``，模型目标钉死为
     ``BAAI/bge-small-zh-v1.5``（MIT，可商用）并钉 revision；本模块不算向量、
     不训模型、不写第二套编码器。
  2. **禁隐式下载**。只允许从钉定的本地快照加载（``local_files_only``），快照缺失、
     revision 不符或权重字节不符一律直接拒——不能让用户以为在用钉住的那份模型。
  3. **不静默截断**。上游 ``encode`` 会按 ``max_length`` 截掉超长输入；本模块先把超长
     文本切成**完整覆盖**的窗口（窗口并集必须铺满整段），并保留父锚点。取不到
     覆盖或单字符都放不进预算时直接拒，不「截一半当整段已检查」。
  4. **窗口是编码单位，不是授权单位**。候选锚点始终是父锚点（可逐字回读原处）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

import v2_contracts as vc

PINNED_MODEL_ID = "BAAI/bge-small-zh-v1.5"
PINNED_MODEL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
PINNED_MODEL_LICENSE = "MIT"
PINNED_WEIGHTS_FILE = "model.safetensors"
PINNED_WEIGHTS_BYTES = 95827648
PINNED_WEIGHTS_SHA256 = "354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026"
PINNED_ALLOW_PATTERNS = ("*.json", "*.txt", "model.safetensors", "README.md")


class EmbeddingProvider:
    """编码提供方基类：真实推理与桩测试共用同一形状，但身份必须自报。"""

    model_id = ""
    model_revision = ""
    max_tokens = 0
    dimension = 0
    is_real_model = False

    def encode(self, texts: tuple[str, ...]) -> np.ndarray:
        raise NotImplementedError

    def token_count(self, text: str) -> int:
        """长度尺子；桩按字符保守计，真提供方用真 tokenizer。"""
        return len(text)

    def identity(self) -> dict:
        return {"model_id": self.model_id, "model_revision": self.model_revision,
                "max_tokens": self.max_tokens, "dimension": self.dimension,
                "is_real_model": self.is_real_model}


@dataclass(frozen=True)
class WindowUnit:
    """一个编码窗口：记住父锚点与原处坐标，候选永远挂在父锚点上。"""

    parent_anchor: vc.SourceAnchor
    candidate_id: str
    start: int
    end: int
    excerpt: str


def coverage_windows(text: str, *, measure: Callable[[str], int], budget: int,
                     overlap: int = 0) -> tuple:
    """把超长文本切成完整覆盖的窗口，返回 ``(start, end)`` 序列。

    ``measure`` 是长度尺子（真提供方传 tokenizer）。窗口必须满足：每个都不超预算、
    彼此连着铺满整段、按顺序排列。任何一段放不下（如单字符已超预算）直接拒，
    不返回「少了尾巴」的结果冒充已检查。
    """
    if budget < 1:
        raise ValueError(f"budget 必须 ≥1: {budget}")
    if overlap < 0:
        raise ValueError(f"overlap 不得为负: {overlap}")
    length = len(text)
    if length == 0:
        return ()
    out: list[tuple[int, int]] = []
    cursor = 0
    while cursor < length:
        span = min(length - cursor, budget)
        while span > 1 and measure(text[cursor:cursor + span]) > budget:
            span //= 2
        if measure(text[cursor:cursor + span]) > budget:
            raise ValueError(
                f"窗口预算放不下单个单元（budget={budget}，起点={cursor}）——"
                "不得截断后宣称整段已检查")
        end = cursor + span
        out.append((cursor, end))
        if end >= length:
            break
        nxt = end - overlap
        cursor = nxt if nxt > cursor else cursor + 1
    return tuple(out)


def segment_windows(record: vc.CandidateRecord, *, text: str | None = None,
                    measure: Callable[[str], int], budget: int,
                    overlap: int = 0) -> tuple[WindowUnit, ...]:
    """按窗口切开一个候选的可比较文本，窗口带父锚点。"""
    anchor = record.source_anchor
    body = anchor.excerpt if text is None else text
    if text is None and not body:
        raise ValueError(f"候选 {record.candidate_id} 的锚点没有可比较文本，且未提供 text")
    units: list[WindowUnit] = []
    for index, (start, end) in enumerate(
            coverage_windows(body, measure=measure, budget=budget, overlap=overlap)):
        units.append(WindowUnit(parent_anchor=anchor, candidate_id=record.candidate_id,
                                start=start, end=end, excerpt=body[start:end]))
    if not units:
        raise ValueError(f"候选 {record.candidate_id} 的可比较文本为空，无法保证覆盖")
    return tuple(units)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class BGEProvider(EmbeddingProvider):
    """钉定 bge-small-zh-v1.5 的真实推理提供方；只读本地快照，绝不联网拉模型。"""

    is_real_model = True

    def __init__(self, *, cache_dir: str, model_id: str = PINNED_MODEL_ID,
                 model_revision: str = PINNED_MODEL_REVISION,
                 device: str = "cpu") -> None:
        if model_id != PINNED_MODEL_ID:
            raise ValueError(
                f"模型必须是 {PINNED_MODEL_ID}（收到 {model_id}）；换模型属于产品决策，"
                "不由调用方随手指定")
        if model_revision != PINNED_MODEL_REVISION:
            raise ValueError(
                f"revision 必须钉在 {PINNED_MODEL_REVISION}（收到 {model_revision}），"
                "否则报告里的模型身份不可复现")
        try:
            from huggingface_hub import snapshot_download
            from huggingface_hub.errors import HfHubHTTPError, LocalEntryNotFoundError
        except ModuleNotFoundError as exc:
            from runtime_contract import install_hint
            raise ModuleNotFoundError(f"{exc}；{install_hint('similarity')}", name=exc.name) from exc
        try:
            snapshot = snapshot_download(repo_id=model_id, revision=model_revision,
                                         cache_dir=cache_dir, local_files_only=True,
                                         allow_patterns=list(PINNED_ALLOW_PATTERNS))
        except (LocalEntryNotFoundError, HfHubHTTPError, OSError, ValueError) as exc:
            raise ValueError(
                f"缺钉定快照：{model_id}@{model_revision} 不在 {cache_dir} 内（{exc}）。"
                "本模块禁止隐式下载，请先按授权显式取模型") from exc
        weights = Path(snapshot) / PINNED_WEIGHTS_FILE
        if not weights.is_file():
            raise ValueError(f"快照缺权重文件 {PINNED_WEIGHTS_FILE}: {weights}")
        if weights.stat().st_size != PINNED_WEIGHTS_BYTES:
            raise ValueError(
                f"权重字节不符钉定值: {weights.stat().st_size} != {PINNED_WEIGHTS_BYTES}")
        if _sha256(weights) != PINNED_WEIGHTS_SHA256:
            raise ValueError(f"权重 SHA256 不符钉定 revision: {weights}")

        try:
            from FlagEmbedding import FlagModel
        except ModuleNotFoundError as exc:
            from runtime_contract import install_hint
            raise ModuleNotFoundError(f"{exc}；{install_hint('similarity')}", name=exc.name) from exc
        config = json.loads((Path(snapshot) / "config.json").read_text(encoding="utf-8"))
        self._model = FlagModel(str(snapshot), cache_dir=cache_dir, devices=[device],
                                use_fp16=False, normalize_embeddings=True,
                                convert_to_numpy=True)
        self.model_id = model_id
        self.model_revision = model_revision
        self.snapshot_dir = str(snapshot)
        self.dimension = int(config["hidden_size"])
        self.max_tokens = int(self._model.passage_max_length)

    def token_count(self, text: str) -> int:
        encoded = self._model.tokenizer(text)
        return len(encoded["input_ids"])

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        batch = list(texts)
        if not batch:
            raise ValueError("encode 不接受空批次")
        over = [i for i, item in enumerate(batch) if self.token_count(item) > self.max_tokens]
        if over:
            raise ValueError(
                f"这些输入超过模型预算 {self.max_tokens} tokens，必须先切窗口: {over}"
                "（不得依赖上游截断再宣称全段已检查）")
        vectors = self._model.encode(batch, convert_to_numpy=True)
        out = np.asarray(vectors, dtype=np.float32)
        if out.ndim != 2 or out.shape[1] != self.dimension:
            raise ValueError(f"向量形状异常: {out.shape}，期望 (n, {self.dimension})")
        return out
