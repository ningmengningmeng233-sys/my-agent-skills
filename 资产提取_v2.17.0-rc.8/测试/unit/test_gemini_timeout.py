#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TICKET-24 AE-06：配置的 timeout_seconds 必须真正传递给网络请求。

GeminiImageConfig.timeout_seconds（默认 180s，可由 GEMINI_TIMEOUT_SECONDS
覆盖）在修复前从未传入任何请求调用：生图（generate）与质检（check）共用
的两处 client.models.generate_content 调用都不带超时，网络异常时会无限
挂起。本测试钉死：两处调用的请求配置中都必须携带配置的超时值。

超时经 SDK 的 HTTP 适配层（GenerateContentConfig.http_options.timeout，
单位毫秒）传递；此处只用 mock 断言透传值，不发真实请求。
"""
import base64
import hashlib
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.gemini_image import (  # noqa: E402
    GeminiImageConfig,
    GeminiImageExecutor,
    GeminiQcChecker,
)
from scripts.engine.image_executor import GenerationRequest  # noqa: E402

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AL+2gAAAABJRU5ErkJggg=="
)


class _FakePart:
    def __init__(self, data=None, mime="image/png", text=None):
        self.inline_data = None if data is None else _FakeInline(data, mime)
        self.text = text


class _FakeInline:
    def __init__(self, data, mime):
        self.data = data
        self.mime_type = mime


class _FakeCandidate:
    def __init__(self, parts):
        self.content = type("C", (), {"parts": parts})()


class _FakeResponse:
    def __init__(self, parts, response_id="resp-1"):
        self.candidates = [_FakeCandidate(parts)]
        self.response_id = response_id
        self.usage_metadata = {}


class _Recorder:
    """记录调用参数的假 SDK client（零真实请求）。"""

    def __init__(self, parts):
        self.parts = parts
        self.calls = []
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self.parts)


class _FakeTypes:
    class HttpOptions:
        def __init__(self, **kw):
            self.kwargs = kw

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


def _timeout_of(call):
    """从一次记录调用中取出透传的超时毫秒数；未透传则返回 None。"""
    config = call.get("config")
    if config is None:
        return None
    http_options = getattr(config, "kwargs", {}).get("http_options")
    if http_options is None:
        return None
    if isinstance(http_options, dict):
        return http_options.get("timeout")
    return getattr(http_options, "kwargs", {}).get("timeout")


def _resolved_pair(tmp_dir: Path):
    from scripts.engine.reference_resolver import ResolvedReference

    out = []
    for rel, role in (("references/face.png", "identity"),
                      ("references/materials/skin.png", "material")):
        p = tmp_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(PNG_BYTES)
        out.append(ResolvedReference(
            origin="stylepack", relative_path=rel, role=role,
            sha256=hashlib.sha256(PNG_BYTES).hexdigest(), mime_type="image/png",
            bytes=len(PNG_BYTES), resolved_path=p))
    return tuple(out)


def _request(tmp_dir: Path):
    from scripts.engine.visual_schema import ReferenceBinding

    resolved = _resolved_pair(tmp_dir)
    return GenerationRequest(
        task_id="CH_林姑娘-正脸-v01", prompt="正脸特写", negative_prompt="无文字",
        aspect_ratio="1:1", image_size="2K", output_mime="image/png",
        references=tuple(ReferenceBinding(path=r.relative_path, role=r.role,
                                          sha256=r.sha256)
                         for r in resolved),
        resolved=resolved)


def test_generate_passes_configured_timeout():
    """生图请求必须携带配置的超时（非默认值可证明透传链路）。"""
    rec = _Recorder([_FakePart(PNG_BYTES)])
    cfg = GeminiImageConfig(api_key="test-key", timeout_seconds=30)
    ex = GeminiImageExecutor(client=rec, config=cfg, types_module=_FakeTypes)
    ex.generate(_request(Path(tempfile.mkdtemp())))
    assert _timeout_of(rec.calls[0]) == 30 * 1000


def test_qc_check_passes_configured_timeout(tmp_path):
    """质检请求必须携带配置的超时（与生图共用同一配置）。"""
    from scripts.engine.image_qc import rubric_for_kind
    from scripts.engine.visual_schema import AttemptRecord, VisualTask

    task = VisualTask(task_id="CH_林姑娘-服装独立背面-v01", asset_id="CH_林姑娘",
                      kind="服装独立背面", status="GENERATED", aspect_ratio="2:3",
                      style_slug="s", style_version="1.0.0", style_hash="a" * 64,
                      fact_source="f#x")
    attempt = AttemptRecord(round=1, image="视觉资产/任务/x/attempt-01/image.png",
                            image_sha256="c" * 64)
    image_path = tmp_path / attempt.image
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(PNG_BYTES)
    qc_text = ('{"decision": "ACCEPTED", "issues": [], '
               '"checker": "gemini:qc", "checked_at": "2026-01-01T00:00:00Z", '
               '"task_id": "CH_林姑娘-服装独立背面-v01", '
               '"input_hashes": ["' + "c" * 64 + '"]}')
    rec = _Recorder([_FakePart(text=qc_text)])
    cfg = GeminiImageConfig(api_key="test-key", timeout_seconds=45)
    checker = GeminiQcChecker(client=rec, config=cfg, types_module=_FakeTypes)
    _ = rubric_for_kind(task.kind)  # 真实 rubric 必须可加载，否则用例前提不成立
    checker.check(task, attempt, tmp_path)
    assert rec.calls, "质检必须发起一次网络请求"
    assert _timeout_of(rec.calls[0]) == 45 * 1000
