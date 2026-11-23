#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AE-07（TICKET-46）：gemini_image 异常捕获边界。

回归门：KeyboardInterrupt / SystemExit 是系统级中断，必须向上抛出，
绝不能被吞掉，更不能误记为内容级拒绝（GENERATION_REJECTED / QC_UNTRUSTWORTHY）。
全程 mock client，零真实网络。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from conftest import FAKE_PNG  # noqa: E402
from scripts.engine.gemini_image import (  # noqa: E402
    GeminiImageConfig,
    GeminiImageExecutor,
    GeminiQcChecker,
)
from scripts.engine.image_executor import (  # noqa: E402
    GENERATION_REJECTED,
    GenerationError,
    GenerationRequest,
)
from scripts.engine.visual_schema import (  # noqa: E402
    AttemptRecord,
    QcDecision,
    VisualTask,
)


class _FakeTypes:
    class GenerateContentConfig:
        def __init__(self, **kw):
            self.kwargs = kw

    class ImageConfig:
        def __init__(self, **kw):
            self.kwargs = kw

    class Part:
        @staticmethod
        def from_bytes(data, mime_type):
            return ("part", data, mime_type)


class _RaisingClient:
    """generate_content 固定抛指定异常的假 SDK client。"""

    def __init__(self, exc):
        self._exc = exc
        self.models = self

    def generate_content(self, **kwargs):
        raise self._exc


def _config(**kw):
    base = dict(api_key="test-key", model="img-model", qc_model="qc-model",
                image_size="2K", timeout_seconds=30)
    base.update(kw)
    return GeminiImageConfig(**base)


def _request():
    return GenerationRequest(task_id="CH_林姑娘-正脸-v01", prompt="正脸特写")


def _qc_task():
    return VisualTask(task_id="CH_林姑娘-正脸-v01", asset_id="CH_林姑娘", kind="正脸",
                      status="GENERATED", aspect_ratio="1:1", style_slug="s",
                      style_version="1.0.0", style_hash="a" * 64,
                      fact_source=".process/资产索引.json#CH_林姑娘")


def _qc_attempt():
    return AttemptRecord(round=1, image="视觉资产/任务/x/attempt-01/image.png",
                         image_sha256="c" * 64)


def test_keyboard_interrupt_propagates_from_generate():
    ex = GeminiImageExecutor(client=_RaisingClient(KeyboardInterrupt()),
                             config=_config(), types_module=_FakeTypes)
    with pytest.raises(KeyboardInterrupt):
        ex.generate(_request())


def test_system_exit_propagates_from_generate():
    ex = GeminiImageExecutor(client=_RaisingClient(SystemExit(3)),
                             config=_config(), types_module=_FakeTypes)
    with pytest.raises(SystemExit):
        ex.generate(_request())


def test_business_exception_still_maps_to_content_rejected():
    """对照组：普通业务异常仍映射为内容级拒绝（行为不回归）。"""
    ex = GeminiImageExecutor(client=_RaisingClient(ValueError("content policy")),
                             config=_config(max_retries=3), types_module=_FakeTypes)
    with pytest.raises(GenerationError) as ctx:
        ex.generate(_request())
    assert ctx.value.code == GENERATION_REJECTED


def test_keyboard_interrupt_propagates_from_qc_check(tmp_path):
    image = tmp_path / "视觉资产/任务/x/attempt-01/image.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(FAKE_PNG)
    checker = GeminiQcChecker(client=_RaisingClient(KeyboardInterrupt()),
                             config=_config(), types_module=_FakeTypes)
    with pytest.raises(KeyboardInterrupt):
        checker.check(_qc_task(), _qc_attempt(), tmp_path)


def test_system_exit_propagates_from_qc_check(tmp_path):
    image = tmp_path / "视觉资产/任务/x/attempt-01/image.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(FAKE_PNG)
    checker = GeminiQcChecker(client=_RaisingClient(SystemExit(3)),
                             config=_config(), types_module=_FakeTypes)
    with pytest.raises(SystemExit):
        checker.check(_qc_task(), _qc_attempt(), tmp_path)


def test_qc_business_exception_still_untrustworthy(tmp_path):
    """对照组：普通质检异常仍判不可信（行为不回归）。"""
    image = tmp_path / "视觉资产/任务/x/attempt-01/image.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(FAKE_PNG)
    checker = GeminiQcChecker(client=_RaisingClient(RuntimeError("boom")),
                             config=_config(), types_module=_FakeTypes)
    result = checker.check(_qc_task(), _qc_attempt(), tmp_path)
    assert result.decision == QcDecision.UNTRUSTWORTHY.value
