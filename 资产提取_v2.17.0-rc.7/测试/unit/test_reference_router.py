#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单02-Task6：参考图与材质动态路由（规格 7）。

参考图必须声明职责，路由器按资产类型与实际材质选择**最小集合**；
职责不得越权；超出引擎上限时按 身份→结构→主风格→关键材质 裁剪，并记录被省略的参考。
"""
import pytest
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.reference_router import (  # noqa: E402
    ROLE_PRIORITY,
    StylePack,
    load_omni_stylepack,
    route_references,
    route_references_detailed,
)
from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_schema import VisualTask  # noqa: E402

STYLE = resolve_style_record("仙侠国风电影CG")


def _pack():
    return load_omni_stylepack(STYLE)


def _task(kind="正脸", asset_id="CH_林姑娘", depends_on=(), materials=(),
          task_id=None, notes=""):
    return VisualTask(
        task_id=task_id or f"{asset_id}-{kind}-v01", asset_id=asset_id, kind=kind,
        status="PLANNED", aspect_ratio="1:1", style_slug=STYLE.pack_slug,
        style_version=STYLE.pack_version, style_hash=STYLE.pack_hash,
        depends_on=tuple(depends_on), materials=tuple(materials),
        fact_source=".process/资产索引.json#x｜摘句: y", notes=notes,
    )


def test_pack_loader_reads_master_routes():
    pack = _pack()
    assert isinstance(pack, StylePack)
    assert {"face", "full_body", "environment", "object"} <= set(pack.subject_routes)
    assert len(pack.material_routes) == 16, sorted(pack.material_routes)
    assert "skin" in pack.material_routes and "hair" in pack.material_routes
    assert pack.root.is_dir()


@pytest.mark.requires_private_stylepack
def test_face_task_takes_face_skin_hair_only():
    task = _task("正脸", materials=("皮肤", "头发"))
    refs = route_references(task, _pack(), {}, limit=8)
    roles = [r.role for r in refs]
    assert roles.count("identity") == 0
    names = {Path(r.path).stem for r in refs}
    assert "face" in names
    assert "skin" in names and "hair" in names
    assert "full_body" not in names and "environment" not in names


def test_face_task_uses_accepted_face_as_identity():
    task = _task("正脸", depends_on=("CH_林姑娘-正脸-v01",))
    refs = route_references(task, _pack(), {"CH_林姑娘-正脸-v01": Path("视觉资产/正脸.png")},
                            limit=8)
    identity = [r for r in refs if r.role == "identity"]
    assert len(identity) == 1
    assert identity[0].path == "视觉资产/正脸.png"


@pytest.mark.requires_private_stylepack
def test_back_head_does_not_inherit_front_composition():
    task = _task("后脑", materials=("头发",))
    refs = route_references(task, _pack(), {}, limit=8)
    names = {Path(r.path).stem for r in refs}
    assert "face" not in names, "后脑不得引用正脸图（会继承正面构图）"
    assert "hair" in names


@pytest.mark.requires_private_stylepack
def test_costume_task_takes_full_body_and_real_materials():
    task = _task("服装独立正面", materials=("布料", "皮革"))
    refs = route_references(task, _pack(), {}, limit=8)
    names = {Path(r.path).stem for r in refs}
    assert "full_body" in names
    assert "fabric" in names and "leather" in names
    assert "face" not in names
    roles = [r.role for r in refs]
    assert "style" in roles and "material" in roles


@pytest.mark.requires_private_stylepack
def test_scene_task_takes_environment_and_present_materials():
    task = _task("场景母版", asset_id="SC_茶楼", materials=("石材", "木材"))
    refs = route_references(task, _pack(), {}, limit=8)
    names = {Path(r.path).stem for r in refs}
    assert "environment" in names
    assert "stone" in names and "wood" in names
    assert "face" not in names and "full_body" not in names


@pytest.mark.requires_private_stylepack
def test_prop_task_takes_object_and_matching_material():
    task = _task("道具", asset_id="PR_琴", materials=("木材",))
    refs = route_references(task, _pack(), {}, limit=8)
    names = {Path(r.path).stem for r in refs}
    assert "object" in names
    assert "wood" in names
    assert "skin" not in names and "hair" not in names


@pytest.mark.requires_private_stylepack
def test_unregistered_material_is_not_invented():
    task = _task("道具", asset_id="PR_琴", materials=("不存在的材质",))
    refs = route_references(task, _pack(), {}, limit=8)
    names = {Path(r.path).stem for r in refs}
    assert "object" in names
    assert not any("不存在" in r.path for r in refs)
    assert len(names) == 1


@pytest.mark.requires_private_stylepack
def test_limit_trims_by_priority_and_records_omissions():
    task = _task("人物全身正面", materials=("皮肤", "头发", "布料", "金属"),
                 depends_on=("CH_林姑娘-服装独立背面-v01",))
    accepted = {"CH_林姑娘-服装独立背面-v01": Path("视觉资产/服装背.png")}
    result = route_references_detailed(task, _pack(), accepted, limit=3)
    assert len(result.bindings) == 3
    assert result.bindings[0].role == "identity" or result.bindings[0].role == "structure"
    ranks = [ROLE_PRIORITY[r.role] for r in result.bindings]
    assert ranks == sorted(ranks), ranks
    assert result.omitted, "被裁剪的参考必须记录"
    for item in result.omitted:
        assert item["path"] and item["reason"]


def test_limit_one_keeps_identity_only():
    task = _task("人物全身正面", depends_on=("CH_林姑娘-服装独立背面-v01",),
                 materials=("皮肤",))
    accepted = {"CH_林姑娘-服装独立背面-v01": Path("视觉资产/服装背.png")}
    refs = route_references(task, _pack(), accepted, limit=1)
    assert len(refs) == 1
    assert refs[0].role in ("identity", "structure")


def test_routing_is_deterministic():
    task = _task("人物全身正面", materials=("皮肤", "头发", "布料", "金属"))
    first = route_references(task, _pack(), {}, limit=8)
    second = route_references(task, _pack(), {}, limit=8)
    assert [r.path for r in first] == [r.path for r in second]


def test_routing_does_not_depend_on_directory_order():
    """同一任务在同一包上重复路由，且不因已加载顺序变化（无全局可变状态）。"""
    task_a = _task("道具", asset_id="PR_琴", materials=("木材",))
    task_b = _task("场景母版", asset_id="SC_茶楼", materials=("石材",))
    pack = _pack()
    a1 = [r.path for r in route_references(task_a, pack, {}, limit=8)]
    _ = route_references(task_b, pack, {}, limit=8)
    a2 = [r.path for r in route_references(task_a, pack, {}, limit=8)]
    assert a1 == a2


def test_all_roles_are_legal():
    for kind in ("正脸", "后脑", "服装独立正面", "服装独立背面",
                 "人物全身正面", "人物全身背面", "场景母版", "道具"):
        task = _task(kind, materials=("布料",), depends_on=("CH_林姑娘-正脸-v01",))
        refs = route_references(task, _pack(), {"CH_林姑娘-正脸-v01": Path("a.png")}, limit=8)
        for r in refs:
            assert r.role in ROLE_PRIORITY, r.role


def test_reference_paths_are_pack_relative_or_accepted_image():
    task = _task("正脸", materials=("皮肤",))
    refs = route_references(task, _pack(), {}, limit=8)
    for r in refs:
        assert not Path(r.path).is_absolute(), r.path
        assert "\\" not in r.path, r.path


@pytest.mark.requires_private_stylepack
def test_style_reference_never_overrides_target_costume():
    """风格参考只带 style 职责，备注显式声明不得复制其服装（规格 7）。"""
    task = _task("服装独立正面", materials=("布料",))
    refs = route_references(task, _pack(), {}, limit=8)
    style_refs = [r for r in refs if r.role == "style"]
    assert style_refs
    for r in style_refs:
        assert "服装" in r.note or "不得复制" in r.note


def test_costume_structure_note_forbids_head_attachments():
    """服装背面以"服装正面"为结构参考，备注必须明确不得继承头部/面部。"""
    upstream = "CH_林姑娘-服装独立正面-v01"
    task = _task("服装独立背面", materials=("布料",), depends_on=(upstream,))
    refs = route_references(task, _pack(), {upstream: Path("视觉资产/服装正.png")}, limit=8)
    structure = [r for r in refs if r.role == "structure"]
    assert structure
    assert any("头" in r.note or "面部" in r.note for r in structure)


def test_zero_limit_returns_nothing():
    task = _task("正脸", materials=("皮肤",))
    assert route_references(task, _pack(), {}, limit=0) == []


def test_shipped_references_exist_on_disk():
    """被路由到的参考图必须真实存在于内置包（不允许指向缺失文件）。"""
    for kind, asset_id, materials in (
        ("正脸", "CH_林姑娘", ("皮肤", "头发")),
        ("后脑", "CH_林姑娘", ("头发",)),
        ("服装独立背面", "CH_林姑娘", ("布料",)),
        ("场景母版", "SC_茶楼", ("石材",)),
        ("道具", "PR_琴", ("木材",)),
    ):
        task = _task(kind, asset_id=asset_id, materials=materials)
        for ref in route_references(task, _pack(), {}, limit=8):
            assert (_pack().root / ref.path).is_file(), f"{kind}: {ref.path} 不存在于内置包"


def test_missing_shipped_image_is_reported_not_silently_dropped():
    """参考图缺失时不得静默降级：要么路由到存在文件，要么显式记入 omitted。"""
    task = _task("正脸", materials=("皮肤", "头发"))
    result = route_references_detailed(task, _pack(), {}, limit=8)
    for item in result.omitted:
        assert item["reason"]
    assert all((_pack().root / r.path).is_file() for r in result.bindings)
