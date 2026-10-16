#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 P0-3：Gemini 载荷 / 模型 / 配置（含 visual-doctor）。

红测目标（修复前必须失败）：
- 参考图缺失时旧实现发送**空字节**图片并把 MIME 固定为 PNG；
- 质检默认模型的代码在官方模型目录中不存在；
- 视觉 CLI 检测不到 `ASSET_ENV_PATH` 指向的包外 `.env`；
- 缺少 `visual-doctor` 命令。
"""
import base64
import json
import os
import sys
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.gemini_image import (  # noqa: E402
    DEFAULT_IMAGE_MODEL,
    DEFAULT_QC_MODEL,
    REFERENCE_MIME_INVALID,
    REFERENCE_UNRESOLVED,
    GeminiImageConfig,
    GeminiImageExecutor,
    load_gemini_config,
)
from scripts.engine.image_executor import GenerationError, GenerationRequest  # noqa: E402
from scripts.engine.reference_resolver import ResolvedReference  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4n"
    "ICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/E"
    "ABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")


class _FakePart:
    @staticmethod
    def from_bytes(data, mime_type):
        return ("part", data, mime_type)


class _FakeTypes:
    Part = _FakePart

    class ImageConfig:
        def __init__(self, **kw):
            self.kwargs = kw

    class GenerateContentConfig:
        def __init__(self, **kw):
            self.kwargs = kw


class _Recorder:
    """假 SDK：记录载荷，绝不发真实请求。"""

    def __init__(self):
        self.calls = []
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("本测试不应真正调用生成")


def _resolved(path="references/face.png", role="style", data=PNG, mime="image/png",
              origin="stylepack"):
    return ResolvedReference(origin=origin, relative_path=path, role=role,
                             sha256="a" * 64, mime_type=mime, bytes=len(data),
                             resolved_path=Path("unused"), note="")


def _request(resolved=(), references=None):
    from scripts.engine.visual_schema import ReferenceBinding

    if references is None:
        references = tuple(
            ReferenceBinding(path=r.relative_path, role=r.role) for r in resolved)
    return GenerationRequest(task_id="CH_甲-正脸-v01", prompt="正脸", negative_prompt="无文字",
                             references=references, aspect_ratio="1:1", image_size="2K",
                             output_mime="image/png", style_name="仙侠国风电影CG",
                             style_version="1.0.0", reference_roles=(), extra={},
                             resolved=tuple(resolved))


def _executor(recorder=None):
    return GeminiImageExecutor(client=recorder or _Recorder(),
                               config=GeminiImageConfig(api_key="k"), types_module=_FakeTypes)


# --- 载荷：真实字节与真实 MIME ----------------------------------------------------

def test_payload_sends_real_reference_bytes_not_empty(monkeypatch):
    """捕获载荷：参考图片字节必须非空，且等于源文件真实字节。"""
    ref = _resolved()
    monkeypatch.setattr(type(ref), "read_bytes", lambda self: PNG, raising=False)
    ex = _executor()
    contents = ex._sdk_contents(_request((ref,)), _FakeTypes)
    parts = [c for c in contents if isinstance(c, tuple)]
    assert len(parts) == 1
    _tag, data, _mime = parts[0]
    assert data, "参考图不得发送空字节"
    assert data == PNG


def test_payload_uses_real_mime_for_jpeg(monkeypatch):
    """JPEG 参考图必须以 image/jpeg 发送，不得硬编码 PNG。"""
    ref = _resolved(path="references/face.jpg", data=JPEG, mime="image/jpeg")
    monkeypatch.setattr(type(ref), "read_bytes", lambda self: JPEG, raising=False)
    ex = _executor()
    contents = ex._sdk_contents(_request((ref,)), _FakeTypes)
    mimes = [c[2] for c in contents if isinstance(c, tuple)]
    assert mimes == ["image/jpeg"], mimes


def test_payload_rejects_unresolved_references():
    """带参考图但没有解析结果时，必须拒绝发送（旧实现会发空字节占位）。"""
    from scripts.engine.visual_schema import ReferenceBinding

    req = _request(resolved=(), references=(
        ReferenceBinding(path="references/face.png", role="style"),))
    with pytest.raises(GenerationError) as ctx:
        _executor()._sdk_contents(req, _FakeTypes)
    assert ctx.value.code == REFERENCE_UNRESOLVED


def test_payload_rejects_bad_mime():
    ref = _resolved(mime="image/gif")
    ex = _executor()
    with pytest.raises(GenerationError) as ctx:
        ex._sdk_contents(_request((ref,)), _FakeTypes)
    assert ctx.value.code == REFERENCE_MIME_INVALID


def test_payload_rejects_zero_byte_reference(monkeypatch):
    ref = _resolved(data=b"", mime="image/png")
    monkeypatch.setattr(type(ref), "read_bytes",
                        lambda self: (_ for _ in ()).throw(
                            GenerationError("REFERENCE_EMPTY", "空文件")), raising=False)
    with pytest.raises(GenerationError):
        _executor()._sdk_contents(_request((ref,)), _FakeTypes)


def test_payload_labels_precede_each_image(monkeypatch):
    """文本顺序必须是「提示词 + 职责标签 + 图片」，逐张对应。"""
    a = _resolved(path="references/face.png", role="identity")
    b = _resolved(path="references/materials/skin.png", role="material")
    monkeypatch.setattr(type(a), "read_bytes", lambda self: PNG, raising=False)
    ex = _executor()
    contents = ex._sdk_contents(_request((a, b)), _FakeTypes)
    assert isinstance(contents[0], str) and "正脸" in contents[0]
    assert "身份" in contents[1] and "references/face.png" in contents[1]
    assert isinstance(contents[2], tuple)
    assert "材质" in contents[3] and "materials/skin.png" in contents[3]
    assert isinstance(contents[4], tuple)


# --- 模型默认值 -------------------------------------------------------------------

def test_image_model_default_is_valid():
    assert DEFAULT_IMAGE_MODEL == "gemini-3-pro-image"


def test_qc_model_default_is_not_the_broken_one():
    assert DEFAULT_QC_MODEL != "gemini-3-pro", "旧默认模型在官方目录中不存在"
    assert DEFAULT_QC_MODEL == "gemini-3.1-pro-preview"


def test_models_overridable_by_env():
    cfg = load_gemini_config({"GEMINI_API_KEY": "k", "GEMINI_IMAGE_MODEL": "x",
                              "GEMINI_QC_MODEL": "y"})
    assert cfg.model == "x" and cfg.qc_model == "y"


# --- .env 优先级 ------------------------------------------------------------------

def test_env_file_supplies_key(tmp_path, monkeypatch):
    env_file = tmp_path / "包外.env"
    env_file.write_text("GEMINI_API_KEY=from-env-file\n", encoding="utf-8")
    for name in ("GEMINI_API_KEY", "ASSET_ENV_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASSET_ENV_PATH", str(env_file))
    cfg = load_gemini_config()
    assert cfg.api_key == "from-env-file"


def test_process_env_beats_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / "包外.env"
    env_file.write_text("GEMINI_API_KEY=from-env-file\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_ENV_PATH", str(env_file))
    monkeypatch.setenv("GEMINI_API_KEY", "from-process")
    assert load_gemini_config().api_key == "from-process"


def test_missing_env_file_still_fails_loudly(tmp_path, monkeypatch):
    for name in ("GEMINI_API_KEY", "ASSET_ENV_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASSET_ENV_PATH", str(tmp_path / "不存在.env"))
    with pytest.raises(GenerationError) as ctx:
        load_gemini_config()
    assert ctx.value.code == "EXECUTOR_NOT_CONFIGURED"


def test_repr_never_leaks_key():
    cfg = GeminiImageConfig(api_key="super-secret-value")
    assert "super-secret-value" not in repr(cfg)
    assert "super-secret-value" not in str(cfg)


# --- 视觉 CLI：包外 .env 必须生效 --------------------------------------------------

def _lib(tmp_path):
    index = {
        "schema_version": 1, "source_fingerprint": "sha256:demo",
        "assets": [{"asset_id": "CH_甲", "type": "人物", "name": "甲", "priority": "P1",
                    "fact_level": "剧本事实",
                    "evidence": [{"segment": "s.md", "line": "00001", "quote": "甲走进茶楼"}],
                    "relations": [], "dependencies": [], "status": "CONFIRMED"}],
    }
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")
    from scripts.engine.style_registry import resolve_style_record
    from scripts.engine.visual_planner import plan_visual_tasks
    from scripts.engine.visual_schema import write_visual_plan

    write_visual_plan(plan_visual_tasks(index, resolve_style_record("仙侠国风电影CG")),
                      lib / "视觉资产" / "计划.toml")
    return lib


def _cli(*args, env=None):
    base = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
    if env:
        base.update(env)
    return run_py(
            [str(REPO_ROOT / "scripts" / "visual_production.py"), *[str(a) for a in args]],
            cwd=str(REPO_ROOT),
            env=base,
    )


def test_visual_cli_detects_external_env(tmp_path):
    """包外 .env 里的密钥必须被视觉 CLI 检测到（旧实现只读 os.environ）。"""
    lib = _lib(tmp_path)
    env_file = tmp_path / "外.env"
    env_file.write_text("GEMINI_API_KEY=cli-env-file-key\n", encoding="utf-8")
    r = _cli("run", "--lib", lib, "--task", "CH_甲-正脸-v01", env={"ASSET_ENV_PATH": str(env_file)})
    out = r.stdout + r.stderr
    assert "未配置 GEMINI_API_KEY" not in out, "CLI 必须从 ASSET_ENV_PATH 的 .env 读到密钥"
    assert "cli-env-file-key" not in out, "日志绝不能回显密钥值"


def test_visual_cli_reports_missing_config(tmp_path):
    lib = _lib(tmp_path)
    r = _cli("run", "--lib", lib, "--task", "CH_甲-正脸-v01",
             env={"ASSET_ENV_PATH": str(tmp_path / "无.env")})
    assert r.returncode != 0
    assert "未配置" in (r.stdout + r.stderr)


# --- visual-doctor ---------------------------------------------------------------

DOCTOR_TIMEOUT = 300


@pytest.mark.requires_private_stylepack
def test_visual_doctor_exists_and_runs_offline(tmp_path):
    """doctor 无密钥时必须如实报告未配置，且不发起收费调用。"""
    lib = _lib(tmp_path)
    r = run_py(
            [str(REPO_ROOT / "scripts" / "maintenance.py"), "visual-doctor", "--lib", str(lib), "--json"],
            cwd=str(REPO_ROOT),
            env={k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "ASSET_ENV_PATH")},
    )
    assert r.returncode in (0, 3), r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["authoritative_pack"]["present"] is True
    assert payload["authoritative_pack"]["integrity_ok"] is True
    assert payload["api_key"] == "未配置"
    assert payload["generation"]["model"] == DEFAULT_IMAGE_MODEL
    assert payload["qc"]["model"] == DEFAULT_QC_MODEL


def test_visual_doctor_never_echoes_key(tmp_path):
    lib = _lib(tmp_path)
    secret = "doctor-secret-value"
    r = run_py(
            [str(REPO_ROOT / "scripts" / "maintenance.py"), "visual-doctor", "--lib", str(lib), "--json"],
            cwd=str(REPO_ROOT),
            env={**os.environ, "GEMINI_API_KEY": secret},
    )
    assert secret not in (r.stdout + r.stderr)
    payload = json.loads(r.stdout)
    assert payload["api_key"] == "已配置"


def test_visual_doctor_detects_pack_damage(tmp_path, monkeypatch):
    """权威包缺图时 doctor 必须报出完整性失败。"""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "engine"))
    import engine.visual_request_chain as doctor_chain

    lib = _lib(tmp_path)
    monkeypatch.setattr(doctor_chain, "stylepack_integrity_issues",
                        lambda root, require_images=True: ["清单文件缺失：references/face.png"])
    from scripts.visual_doctor import run_doctor

    report = run_doctor(lib)
    assert report["authoritative_pack"]["integrity_ok"] is False
    assert report["authoritative_pack"]["issues"]
    assert report["ok"] is False
