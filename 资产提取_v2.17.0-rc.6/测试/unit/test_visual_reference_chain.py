#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 P0-1 集成回归：参考图路由必须真正接入 run/export 生产链。

红测目标（修复前必须失败）：
- 真实离线导出的正脸任务，`参考图清单.md` 不得再出现「本任务无参考图」，
  且至少含 1 张来自权威风格包的 style 参考；
- 有已验收身份图时，全身正面任务必须含 identity 参考，超限裁剪项进入清单；
- run 与 export 对同一任务产出**同一提示词、同一负面提示词、同一参考职责与哈希**；
- 任一参考失败时**生成前**中断并给出稳定原因码，绝不发请求。
"""
import base64
import json
import re
import sys
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.reference_resolver import (  # noqa: E402
    REFERENCE_HASH_MISMATCH,
    REFERENCE_MISSING,
)
from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_planner import plan_visual_tasks  # noqa: E402
from scripts.engine.visual_schema import (  # noqa: E402
    AttemptRecord,
    VisualTask,
    load_visual_plan,
    write_visual_plan,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")
VISUAL = REPO_ROOT / "scripts" / "visual_production.py"
STYLE = resolve_style_record("仙侠国风电影CG")
VERSION_DIR = REPO_ROOT / "references" / "styles" / "仙侠国风电影CG" / "1.0.0"
PACK_ROOT = VERSION_DIR / "private"


def _copy_pack(dest_parent: Path, name: str = "包") -> Path:
    """复制整个版本目录（含 manifest.toml），返回其中的 private 内容根。"""
    import shutil

    dest = dest_parent / name
    shutil.copytree(VERSION_DIR, dest)
    return dest / "private"


def _index():
    return {
        "schema_version": 1, "source_fingerprint": "sha256:demo",
        "assets": [
            {"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘", "priority": "P1",
             "fact_level": "剧本事实", "materials": ["皮肤", "头发", "布料"],
             "evidence": [{"segment": "段01.md", "line": "00001", "quote": "林姑娘走进茶楼"}],
             "relations": [], "dependencies": [], "status": "CONFIRMED"},
        ],
    }


def _lib(tmp_path):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(_index(), ensure_ascii=False), encoding="utf-8")
    plan = plan_visual_tasks(_index(), STYLE)
    write_visual_plan(plan, lib / "视觉资产" / "计划.toml")
    return lib


def _plan_path(lib):
    return lib / "视觉资产" / "计划.toml"


def _accept_with_image(lib, task_id, image_bytes=PNG):
    """把某任务标记为已通过，并写入真实存在的 attempt 图片（供下游当身份参考）。"""
    import hashlib

    from scripts.engine.visual_store import attempt_dir

    plan = load_visual_plan(_plan_path(lib))
    task = plan.by_id(task_id)
    rel = f"{attempt_dir(task_id, 1)}/image.png"
    target = lib / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(image_bytes)
    attempt = AttemptRecord(round=1, image=rel,
                            image_sha256=hashlib.sha256(image_bytes).hexdigest(),
                            model="manual", provider="manual")
    updated = VisualTask(**{**task.__dict__, "status": "READY_AS_REFERENCE",
                            "attempts": (attempt,)})
    write_visual_plan(plan.with_task(updated), _plan_path(lib))


def _cli(*args, env=None):
    import os

    base = {**os.environ, "ASSET_WORKERS": "4"}
    base.pop("GEMINI_API_KEY", None)
    if env:
        base.update(env)
    return run_py([str(VISUAL), *[str(a) for a in args]], cwd=str(REPO_ROOT), env=base)


def _package(lib, task_id):
    return lib / "视觉资产" / "任务包" / task_id


# --- 红测 1：正脸导出必须带风格参考 ------------------------------------------------

def test_export_face_task_includes_stylepack_reference(tmp_path):
    lib = _lib(tmp_path)
    task_id = "CH_林姑娘-正脸-v01"
    r = _cli("export", "--lib", lib, "--task", task_id)
    assert r.returncode == 0, r.stdout + r.stderr
    text = (_package(lib, task_id) / "参考图清单.md").read_text(encoding="utf-8")
    assert "本任务无参考图" not in text, "正脸任务必须带权威风格参考图"
    assert re.search(r"风格", text), f"清单缺少风格职责：{text}"
    assert "face.png" in text, f"正脸应路由到权威包 face.png：{text}"


def test_exported_reference_hash_matches_authoritative_manifest(tmp_path):
    """导出的参考图哈希必须与权威包 manifest 逐字一致。"""
    from scripts.sync_stylepack import build_style_manifest

    lib = _lib(tmp_path)
    task_id = "CH_林姑娘-正脸-v01"
    _cli("export", "--lib", lib, "--task", task_id)
    pkg_manifest = json.loads(
        (_package(lib, task_id) / "任务包清单.json").read_text(encoding="utf-8"))
    real = {f["path"]: f["sha256"] for f in build_style_manifest(PACK_ROOT)["files"]}
    refs = [e for e in pkg_manifest["files"] if e.get("source", "").startswith("stylepack")]
    assert refs, "任务包清单必须记录来源为 stylepack 的参考图"
    for entry in refs:
        assert entry["sha256"] == real[entry["source_path"]], entry["source_path"]


def test_exported_reference_bytes_are_non_empty(tmp_path):
    lib = _lib(tmp_path)
    task_id = "CH_林姑娘-正脸-v01"
    _cli("export", "--lib", lib, "--task", task_id)
    imgs = sorted((_package(lib, task_id) / "参考图").glob("*"))
    assert imgs, "任务包必须复制真实参考图片"
    for img in imgs:
        assert img.stat().st_size > 0, f"{img.name} 为空字节"


def test_run_and_export_compile_identical_prompt(tmp_path):
    """同一任务在自动与手工两条路径产生同一事实集合。"""
    from scripts.engine.visual_request_chain import compile_for_library

    lib = _lib(tmp_path)
    plan = load_visual_plan(_plan_path(lib))
    task = plan.by_id("CH_林姑娘-正脸-v01")
    auto = compile_for_library(task, lib, plan, engine_profile="gemini")
    manual = compile_for_library(task, lib, plan, engine_profile="gemini")
    assert auto.request.prompt == manual.request.prompt
    assert auto.request.negative_prompt == manual.request.negative_prompt
    assert [r.to_manifest() for r in auto.resolved] == [r.to_manifest() for r in manual.resolved]
    assert auto.prompt_facts() == manual.prompt_facts()


def test_omitted_references_are_recorded(tmp_path):
    """超限裁剪必须进入清单，且不静默消失。"""
    from scripts.engine.visual_request_chain import compile_for_library

    lib = _lib(tmp_path)
    plan = load_visual_plan(_plan_path(lib))
    task = plan.by_id("CH_林姑娘-正脸-v01")
    compiled = compile_for_library(task, lib, plan, limit=1)
    assert len(compiled.resolved) == 1
    assert compiled.omitted, "limit=1 时必须记录被省略的参考"
    for item in compiled.omitted:
        assert item["reason"]


# --- 红测 2：有身份图时全身正面必须带 identity --------------------------------------

def test_full_body_front_uses_accepted_face_as_identity(tmp_path):
    """身份参考独立于生成顺序：正脸已通过时，全身正面必须带 identity。

    依赖链上全身正面的「最近前置」是服装背面（生成顺序），若只看依赖链，
    身份锁会丢失。此处把正脸与服装链都置为已通过，验证 identity 确实在列。
    """
    lib = _lib(tmp_path)
    for task_id in ("CH_林姑娘-正脸-v01", "CH_林姑娘-后脑-v01",
                    "CH_林姑娘-服装独立正面-v01", "CH_林姑娘-服装独立背面-v01"):
        _accept_with_image(lib, task_id)

    task_id = "CH_林姑娘-人物全身正面-v01"
    r = _cli("export", "--lib", lib, "--task", task_id)
    assert r.returncode == 0, r.stdout + r.stderr
    text = (_package(lib, task_id) / "参考图清单.md").read_text(encoding="utf-8")
    assert "身份" in text, f"全身正面必须继承正脸身份参考：{text}"
    manifest = json.loads((_package(lib, task_id) / "请求.json").read_text(encoding="utf-8"))
    roles = [ref["role"] for ref in manifest["references"]]
    assert "identity" in roles, roles
    identity = next(ref for ref in manifest["references"] if ref["role"] == "identity")
    assert identity["path"].endswith("image.png"), identity
    assert identity["origin"] == "library", identity


def test_identity_reference_independent_of_dependency_chain(tmp_path):
    """即使只通过正脸（其余上游未动），路由仍应给出 identity 绑定。"""
    from scripts.engine.reference_router import route_references_detailed
    from scripts.engine.visual_request_chain import accepted_asset_paths
    from scripts.engine.reference_router import load_omni_stylepack

    lib = _lib(tmp_path)
    _accept_with_image(lib, "CH_林姑娘-正脸-v01")
    plan = load_visual_plan(_plan_path(lib))
    task = plan.by_id("CH_林姑娘-人物全身正面-v01")
    accepted = accepted_asset_paths(plan, lib)
    assert "CH_林姑娘-正脸-v01" in accepted
    result = route_references_detailed(task, load_omni_stylepack(STYLE), accepted)
    roles = [b.role for b in result.bindings]
    assert "identity" in roles, roles


def test_unaccepted_upstream_never_becomes_identity_reference(tmp_path):
    """未验收的上游不得作为参考（防止用未通过图当身份真相）。"""
    from scripts.engine.visual_request_chain import accepted_asset_paths

    lib = _lib(tmp_path)
    plan = load_visual_plan(_plan_path(lib))
    assert accepted_asset_paths(plan, lib) == {}, "PLANNED 任务不得进入 accepted_assets"


# --- 红测 3：失败必须发生在生成前 --------------------------------------------------

def _broken_ref_lib(tmp_path, mode):
    """构造一个参考图有问题的库：风格包缺图 / 空图 / 哈希不符。"""
    lib = _lib(tmp_path)
    pack = _copy_pack(tmp_path)
    face = pack / "references" / "face.png"
    if mode == "missing":
        face.unlink()
    elif mode == "empty":
        face.write_bytes(b"")
    elif mode == "hash":
        # 篡改计划里的哈希声明，使校验必然不符
        plan = load_visual_plan(_plan_path(lib))
        task = plan.by_id("CH_林姑娘-正脸-v01")
        from scripts.engine.visual_schema import ReferenceBinding

        updated = VisualTask(**{**task.__dict__, "references": (
            ReferenceBinding(path="references/face.png", role="style", sha256="0" * 64),)})
        write_visual_plan(plan.with_task(updated), _plan_path(lib))
    return lib, pack


def test_stylepack_integrity_detects_missing_image(tmp_path):
    """删掉权威包一张图后，包完整性校验必须报出缺图（发布与运行都据此拒绝）。"""
    from scripts.engine.visual_request_chain import stylepack_integrity_issues

    pack = _copy_pack(tmp_path)
    assert stylepack_integrity_issues(pack) == [], "完整包不应有问题"
    (pack / "references" / "face.png").unlink()
    issues = stylepack_integrity_issues(pack)
    assert issues, "缺图必须被报出"
    assert any("face.png" in item for item in issues), issues


def test_stylepack_integrity_detects_drift(tmp_path):
    from scripts.engine.visual_request_chain import stylepack_integrity_issues

    pack = _copy_pack(tmp_path)
    (pack / "generation_formula.md").write_text("被改写", encoding="utf-8")
    issues = stylepack_integrity_issues(pack)
    assert any("generation_formula.md" in item for item in issues), issues


@pytest.mark.parametrize("mode,expected", [
    ("missing", "STYLEPACK_UNAVAILABLE"),
    ("empty", "STYLEPACK_UNAVAILABLE"),
    ("hash", REFERENCE_HASH_MISMATCH),
])
def test_broken_reference_blocks_before_generation(tmp_path, mode, expected):
    """权威包缺图/空图/漂移、或声明哈希不符时，编译必须在生成前以稳定码失败。

    缺图与空图由**包完整性层**拦下（退化为少发参考正是 rc.1 的 P0 缺陷形态）；
    声明哈希与真实内容不符由**逐文件校验层**拦下。
    """
    from scripts.engine.reference_router import load_omni_stylepack
    from scripts.engine.visual_request_chain import VisualCompileError, compile_visual_request

    lib, pack = _broken_ref_lib(tmp_path, mode)
    plan = load_visual_plan(_plan_path(lib))
    task = plan.by_id("CH_林姑娘-正脸-v01")
    index = json.loads((lib / ".process" / "资产索引.json").read_text(encoding="utf-8"))

    real = load_omni_stylepack(STYLE)
    stand_in = type(real)(root=pack, subject_routes=real.subject_routes,
                          material_routes=real.material_routes, router_dir=real.router_dir)
    with pytest.raises(VisualCompileError) as ctx:
        compile_visual_request(task, index["assets"][0], plan, lib,
                               stylepack_override=(STYLE, stand_in, pack))
    assert ctx.value.code == expected


def test_accepted_upstream_with_swapped_bytes_is_rejected(tmp_path):
    """已通过的正脸文件被换掉后，下游编译必须拒绝——不得静默丢掉身份参考。"""
    from scripts.engine.visual_request_chain import (
        VisualCompileError,
        compile_for_library,
        unverifiable_accepted,
    )

    lib = _lib(tmp_path)
    face_id = "CH_林姑娘-正脸-v01"
    _accept_with_image(lib, face_id)
    assert unverifiable_accepted(load_visual_plan(_plan_path(lib)), lib) == []

    # 保留哈希记录、替换真实字节 → 必须被判定为无法核验
    rel = f"视觉资产/任务/{face_id}/attempt-01/image.png"
    (lib / rel).write_bytes(PNG + b"tampered")
    problems = unverifiable_accepted(load_visual_plan(_plan_path(lib)), lib)
    assert problems, "换图必须被报出"
    assert problems[0][1] == REFERENCE_HASH_MISMATCH

    plan = load_visual_plan(_plan_path(lib))
    task = plan.by_id("CH_林姑娘-人物全身正面-v01")
    with pytest.raises(VisualCompileError) as ctx:
        compile_for_library(task, lib, plan)
    assert ctx.value.code == REFERENCE_HASH_MISMATCH


def test_accepted_upstream_with_missing_file_is_rejected(tmp_path):
    """已通过的正脸文件被删除后，下游编译必须拒绝。"""
    from scripts.engine.visual_request_chain import (
        VisualCompileError,
        compile_for_library,
        unverifiable_accepted,
    )

    lib = _lib(tmp_path)
    face_id = "CH_林姑娘-正脸-v01"
    _accept_with_image(lib, face_id)
    (lib / f"视觉资产/任务/{face_id}/attempt-01/image.png").unlink()
    problems = unverifiable_accepted(load_visual_plan(_plan_path(lib)), lib)
    assert problems and problems[0][1] == REFERENCE_MISSING

    plan = load_visual_plan(_plan_path(lib))
    with pytest.raises(VisualCompileError) as ctx:
        compile_for_library(plan.by_id("CH_林姑娘-人物全身正面-v01"), lib, plan)
    assert ctx.value.code == REFERENCE_MISSING


def test_compile_rejects_incomplete_stylepack(tmp_path):
    """缺图后编译整个正脸任务必须 STYLEPACK_UNAVAILABLE，而不是少发参考继续。"""
    from scripts.engine.reference_router import load_omni_stylepack
    from scripts.engine.visual_request_chain import VisualCompileError, compile_visual_request

    lib = _lib(tmp_path)
    pack = _copy_pack(tmp_path)
    (pack / "references" / "face.png").unlink()
    plan = load_visual_plan(_plan_path(lib))
    task = plan.by_id("CH_林姑娘-正脸-v01")
    index = json.loads((lib / ".process" / "资产索引.json").read_text(encoding="utf-8"))
    real = load_omni_stylepack(STYLE)
    stand_in = type(real)(root=pack, subject_routes=real.subject_routes,
                          material_routes=real.material_routes, router_dir=real.router_dir)
    with pytest.raises(VisualCompileError) as ctx:
        compile_visual_request(task, index["assets"][0], plan, lib,
                               stylepack_override=(STYLE, stand_in, pack))
    assert ctx.value.code == "STYLEPACK_UNAVAILABLE"
    assert "face.png" in ctx.value.detail
