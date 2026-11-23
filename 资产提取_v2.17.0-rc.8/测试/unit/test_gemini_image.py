#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单03-Task2：Gemini 配置与适配器（规格 2.13 / 3.1）。

密钥只从包外环境或指定 .env 读取；不得回退读取 OPENAI_API_KEY；
SDK 只在本模块内延迟导入，保证无视觉依赖时核心仍可启动。
"""
import base64
import hashlib
import sys
import tempfile
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.gemini_image import (  # noqa: E402
    DEFAULT_IMAGE_MODEL,
    DEFAULT_QC_MODEL,
    GeminiImageConfig,
    GeminiImageExecutor,
    load_gemini_config,
)
from scripts.engine.image_executor import (  # noqa: E402
    EXECUTOR_NOT_CONFIGURED,
    INVALID_IMAGE_RESPONSE,
    GenerationError,
    GenerationRequest,
)

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AL+2gAAAABJRU5ErkJggg=="
)


# --- 配置 ------------------------------------------------------------------------

def test_config_reads_gemini_env_only():
    cfg = load_gemini_config({"GEMINI_API_KEY": "k", "GEMINI_IMAGE_MODEL": "img",
                              "GEMINI_QC_MODEL": "qc", "GEMINI_IMAGE_SIZE": "4K"})
    assert cfg.api_key == "k"
    assert cfg.model == "img"
    assert cfg.qc_model == "qc"
    assert cfg.image_size == "4K"


def test_config_defaults():
    cfg = load_gemini_config({"GEMINI_API_KEY": "k"})
    assert cfg.model == DEFAULT_IMAGE_MODEL
    assert cfg.qc_model == DEFAULT_QC_MODEL
    assert cfg.timeout_seconds > 0


def test_config_never_falls_back_to_openai_key():
    with pytest.raises(GenerationError) as ctx:
        load_gemini_config({"OPENAI_API_KEY": "占位-不参与鉴权"})
    assert ctx.value.code == EXECUTOR_NOT_CONFIGURED
    assert "OPENAI_API_KEY" not in str(ctx.value)


def test_config_error_never_leaks_key_value():
    with pytest.raises(GenerationError) as ctx:
        load_gemini_config({})
    assert "k" not in ctx.value.detail.replace("KEY", "")


def test_config_is_frozen():
    cfg = load_gemini_config({"GEMINI_API_KEY": "k"})
    with pytest.raises(Exception):
        cfg.api_key = "other"  # type: ignore[misc]


# --- SDK mock：请求组装 -----------------------------------------------------------

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
        self.usage_metadata = {"prompt_token_count": 10, "candidates_token_count": 20}


class _Recorder:
    """记录调用参数的假 SDK client（单元测试零真实请求）。"""

    def __init__(self, parts):
        self.parts = parts
        self.calls = []
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.parts, Exception):
            raise self.parts
        return _FakeResponse(self.parts)


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


def _config(**kw):
    base = dict(api_key="test-key", model="img-model", qc_model="qc-model",
                image_size="2K", timeout_seconds=30)
    base.update(kw)
    return GeminiImageConfig(**base)


def _resolved_pair(tmp_dir: Path):
    """两张真实存在的参考图（含正确 MIME），供载荷测试使用。

    执行器只发送**已解析校验**的参考：单元测试必须走同一契约，
    否则会掩盖「发送空字节占位图」这类缺陷。
    """
    from scripts.engine.reference_resolver import ResolvedReference

    out = []
    for rel, data in (("references/face.png", PNG_BYTES),
                      ("references/materials/skin.png", PNG_BYTES)):
        p = tmp_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        out.append(ResolvedReference(
            origin="stylepack", relative_path=rel, role="identity" if "face" in rel else "material",
            sha256=hashlib.sha256(data).hexdigest(), mime_type="image/png",
            bytes=len(data), resolved_path=p))
    return tuple(out)


def _request(tmp_path: Path | None = None, **kw):
    from scripts.engine.visual_schema import ReferenceBinding

    resolved = _resolved_pair(tmp_path or Path(tempfile.mkdtemp()))
    base = dict(task_id="CH_林姑娘-正脸-v01", prompt="正脸特写", negative_prompt="无文字",
                aspect_ratio="1:1", image_size="2K", output_mime="image/png",
                references=tuple(ReferenceBinding(path=r.relative_path, role=r.role,
                                                  sha256=r.sha256)
                                 for r in resolved),
                resolved=resolved)
    base.update(kw)
    return GenerationRequest(**base)


def _executor(parts, **kw):
    return GeminiImageExecutor(client=_Recorder(parts), config=_config(**kw),
                               types_module=_FakeTypes)


# --- SDK mock：调用与解析 ---------------------------------------------------------

def test_generate_sends_text_and_reference_images_in_order():
    """文本在最前；每张参考图前先声明职责与相对路径；图片按声明顺序紧随其后。"""
    rec = _Recorder([_FakePart(PNG_BYTES)])
    ex = GeminiImageExecutor(client=rec, config=_config(), types_module=_FakeTypes)
    ex.generate(_request())
    contents = rec.calls[0]["contents"]
    assert isinstance(contents, list)
    assert "正脸特写" in contents[0]
    labels = [c for c in contents if isinstance(c, str)]
    assert len(labels) == 3, labels
    assert "身份" in labels[1] and "references/face.png" in labels[1]
    assert "材质" in labels[2] and "references/materials/skin.png" in labels[2]
    assert sum(1 for c in contents if not isinstance(c, str)) == 2


def test_generate_passes_aspect_ratio_and_size():
    rec = _Recorder([_FakePart(PNG_BYTES)])
    ex = GeminiImageExecutor(client=rec, config=_config(), types_module=_FakeTypes)
    ex.generate(_request(aspect_ratio="2:3", image_size="4K"))
    cfg = rec.calls[0]["config"]
    assert cfg.kwargs["image_config"].kwargs == {"aspect_ratio": "2:3", "image_size": "4K"}


def test_generate_decodes_base64_image():
    rec = _Recorder([_FakePart(base64.b64encode(PNG_BYTES).decode("ascii"))])
    ex = GeminiImageExecutor(client=rec, config=_config(), types_module=_FakeTypes)
    result = ex.generate(_request())
    assert result.image_bytes == PNG_BYTES
    assert result.mime_type == "image/png"
    assert result.provider == "gemini"
    assert result.model == "img-model"
    assert result.response_id == "resp-1"
    assert result.succeeded


def test_generate_accepts_raw_bytes():
    rec = _Recorder([_FakePart(PNG_BYTES)])
    result = GeminiImageExecutor(client=rec, config=_config(),
                                 types_module=_FakeTypes).generate(_request())
    assert result.image_bytes == PNG_BYTES


def test_empty_response_rejected():
    rec = _Recorder([])
    with pytest.raises(GenerationError) as ctx:
        GeminiImageExecutor(client=rec, config=_config(),
                            types_module=_FakeTypes).generate(_request())
    assert ctx.value.code == INVALID_IMAGE_RESPONSE


def test_multiple_images_rejected():
    rec = _Recorder([_FakePart(PNG_BYTES), _FakePart(PNG_BYTES)])
    with pytest.raises(GenerationError) as ctx:
        GeminiImageExecutor(client=rec, config=_config(),
                            types_module=_FakeTypes).generate(_request())
    assert ctx.value.code == INVALID_IMAGE_RESPONSE


def test_wrong_mime_rejected():
    rec = _Recorder([_FakePart(PNG_BYTES, mime="text/plain")])
    with pytest.raises(GenerationError) as ctx:
        GeminiImageExecutor(client=rec, config=_config(),
                            types_module=_FakeTypes).generate(_request())
    assert ctx.value.code == INVALID_IMAGE_RESPONSE


def test_empty_image_bytes_rejected():
    rec = _Recorder([_FakePart(b"")])
    with pytest.raises(GenerationError) as ctx:
        GeminiImageExecutor(client=rec, config=_config(),
                            types_module=_FakeTypes).generate(_request())
    assert ctx.value.code == INVALID_IMAGE_RESPONSE


def test_transient_errors_are_retried():
    calls = {"n": 0}

    class _Flaky(_Recorder):
        def generate_content(self, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("temporary")
            return _FakeResponse([_FakePart(PNG_BYTES)])

    ex = GeminiImageExecutor(client=_Flaky(None), config=_config(max_retries=3),
                             types_module=_FakeTypes)
    result = ex.generate(_request())
    assert result.succeeded
    assert calls["n"] == 3


def test_content_rejection_is_not_retried():
    """内容级拒绝不得在适配器内部偷偷重复收费（返工由质量状态机决定）。"""
    calls = {"n": 0}

    class _Rejected(_Recorder):
        def generate_content(self, **kwargs):
            calls["n"] += 1
            raise ValueError("content policy rejected")

    ex = GeminiImageExecutor(client=_Rejected(None), config=_config(max_retries=3),
                             types_module=_FakeTypes)
    with pytest.raises(GenerationError):
        ex.generate(_request())
    assert calls["n"] == 1


def test_transient_error_exhausts_to_rate_limited():
    from scripts.engine.image_executor import RATE_LIMITED

    class _Limited(_Recorder):
        def generate_content(self, **kwargs):
            raise _HttpError(429, "rate limited")

    ex = GeminiImageExecutor(client=_Limited(None), config=_config(max_retries=2),
                             types_module=_FakeTypes)
    with pytest.raises(GenerationError) as ctx:
        ex.generate(_request())
    assert ctx.value.code in (RATE_LIMITED,)


class _HttpError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def test_sdk_is_not_imported_at_module_import_time():
    """协议与适配器模块导入时不得拉起 Google SDK（核心可离线启动）。"""

    script = (
        "import sys, json;"
        "sys.path.insert(0, r'%s'); sys.path.insert(0, r'%s');"
        "import scripts.engine.image_executor, scripts.engine.gemini_image;"
        "print(json.dumps(sorted(k for k in sys.modules if k.startswith('google'))))"
        % (str(REPO_ROOT), str(REPO_ROOT / "scripts" / "engine"))
    )
    proc = run_py(["-c", script])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("[]")


def test_env_example_declares_gemini_keys():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("GEMINI_API_KEY", "GEMINI_IMAGE_MODEL", "GEMINI_QC_MODEL"):
        assert key in text, key


def test_requirements_declares_google_genai():
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproj = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for text in (req, pyproj):
        assert "google-genai" in text
    # 生图依赖不得进核心必装（用 optional 分组声明）
    assert "google-genai" in pyproj


def test_no_secret_in_metadata():
    rec = _Recorder([_FakePart(PNG_BYTES)])
    result = GeminiImageExecutor(client=rec, config=_config(),
                                 types_module=_FakeTypes).generate(_request())
    text = str(result.raw_metadata)
    assert "test-key" not in text
