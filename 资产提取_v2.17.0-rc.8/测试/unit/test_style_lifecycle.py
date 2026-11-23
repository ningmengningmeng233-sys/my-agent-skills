#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单04-Task2：风格生命周期与安全删除准备（规格 10）。

只允许 active→deprecated→disabled→removable；被项目引用的风格不得物理删除；
停用风格不进入新项目列表但旧项目可按固定版本读取。
"""
import json
import sys
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scripts.engine.style_registry import (  # noqa: E402
    LIFECYCLE,
    LifecycleError,
    scan_style_usage,
    set_style_status,
)

MANAGE = REPO_ROOT / "scripts" / "manage_styles.py"
STYLE_NAME = "仙侠国风电影CG"


def _project(tmp_path, name="项目A", style="H7", pack_hash="a" * 64):
    lib = tmp_path / name
    (lib / ".process").mkdir(parents=True)
    (lib / ".start-state.json").write_text(json.dumps(
        {"style": style, "genre": "玄幻", "input_source": "原文.txt"}, ensure_ascii=False),
        encoding="utf-8")
    (lib / "视觉资产").mkdir(parents=True)
    (lib / "视觉资产" / "计划.toml").write_text(
        f'schema_version = 1\nstyle_slug = "仙侠国风电影CG"\n'
        f'style_version = "1.0.0"\nstyle_hash = "{pack_hash}"\n', encoding="utf-8")
    return lib


def _cli(*args):
    return run_py([str(MANAGE), *[str(a) for a in args]], cwd=str(REPO_ROOT))


# --- 生命周期 -------------------------------------------------------------------

def test_lifecycle_order_is_enforced():
    assert LIFECYCLE == ("active", "deprecated", "disabled", "removable")


def test_registry_records_start_active():
    from scripts.engine.style_registry import resolve_style_record
    assert resolve_style_record(STYLE_NAME).status == "active"


def test_illegal_lifecycle_jump_rejected(tmp_path):
    registry = tmp_path / "registry.toml"
    registry.write_text(
        'schema_version = 1\n\n[[styles]]\nid = "X1"\nname = "测试风格"\n'
        'aliases = []\nstatus = "active"\nvalidation_level = "legacy_unverified"\n'
        'kind = "style"\npack_slug = ""\npack_version = ""\npack_hash = ""\n'
        "shareable = true\n", encoding="utf-8")
    with pytest.raises(LifecycleError) as ctx:
        set_style_status(registry, "测试风格", "removable")
    assert ctx.value.code == "ILLEGAL_LIFECYCLE_JUMP"


def test_deprecate_then_disable_writes_registry(tmp_path):
    registry = tmp_path / "registry.toml"
    registry.write_text(
        'schema_version = 1\n\n[[styles]]\nid = "X1"\nname = "测试风格"\n'
        'aliases = []\nstatus = "active"\nvalidation_level = "legacy_unverified"\n'
        'kind = "style"\npack_slug = ""\npack_version = ""\npack_hash = ""\n'
        "shareable = true\n", encoding="utf-8")
    set_style_status(registry, "测试风格", "deprecated")
    assert 'status = "deprecated"' in registry.read_text(encoding="utf-8")
    set_style_status(registry, "测试风格", "disabled")
    assert 'status = "disabled"' in registry.read_text(encoding="utf-8")


def test_disabled_style_not_in_new_project_list(tmp_path):
    from scripts.engine.style_registry import StyleRecord, StyleRegistry, list_user_styles

    reg = StyleRegistry(records=(
        StyleRecord(id="X1", name="可用", status="active"),
        StyleRecord(id="X2", name="已停用", status="disabled"),
    ))
    names = {r.name for r in list_user_styles(include_legacy=True, registry=reg)}
    assert names == {"可用"}


def test_deprecated_style_still_readable_by_old_project(tmp_path):
    """停用不等于删档：旧项目按固定版本仍可解析。"""
    from scripts.engine.style_registry import StyleRecord, StyleRegistry, resolve_style_record

    reg = StyleRegistry(records=(
        StyleRecord(id="X1", name="弃用风格", status="deprecated", pack_slug="s",
                    pack_version="1.0.0", pack_hash="a" * 64),
    ))
    record = resolve_style_record("弃用风格", registry=reg)
    assert record is not None and record.pack_version == "1.0.0"


# --- 引用扫描 -------------------------------------------------------------------

def test_scan_finds_project_references(tmp_path):
    """老库的启动状态记旧编号、新视觉计划记新中文名——两类引用都要扫得到。"""
    _project(tmp_path)
    legacy_hits = scan_style_usage(tmp_path, "H7", "")
    assert {u["source"] for u in legacy_hits} == {"启动状态"}
    for usage in legacy_hits:
        assert usage["project"].startswith("项目A")
        assert usage["last_used"]

    new_hits = scan_style_usage(tmp_path, "仙侠国风电影CG", "")
    assert {u["source"] for u in new_hits} == {"视觉计划"}
    assert new_hits[0]["pack_hash"] == "a" * 64


def test_scan_matches_pack_hash(tmp_path):
    _project(tmp_path, pack_hash="b" * 64)
    assert scan_style_usage(tmp_path, "仙侠国风电影CG", "b" * 64)
    assert scan_style_usage(tmp_path, "仙侠国风电影CG", "c" * 64) == []


def test_scan_covers_delivery_manifests(tmp_path):
    lib = _project(tmp_path)
    (lib / "交付清单.json").write_text(json.dumps(
        {"status": "DELIVERED", "style": "H7"}, ensure_ascii=False), encoding="utf-8")
    usages = scan_style_usage(tmp_path, "H7", "")
    assert any(u["source"] == "交付清单" for u in usages)


# --- remove 安全 -----------------------------------------------------------------

def test_referenced_style_cannot_be_removed(tmp_path):
    _project(tmp_path)
    r = _cli("remove", "--style", STYLE_NAME, "--root", tmp_path)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "引用" in out and "停用" in out


def test_remove_requires_explicit_flag_and_zero_references(tmp_path):
    empty = tmp_path / "无项目"
    empty.mkdir()
    r = _cli("remove", "--style", STYLE_NAME, "--root", empty)
    assert r.returncode != 0
    assert "--force-unreferenced" in (r.stdout + r.stderr)


def test_remove_produces_plan_not_deletion(tmp_path):
    """零引用且已 removable 时，remove 只产出清单，不删任何文件。"""
    import re

    empty = tmp_path / "无项目"
    empty.mkdir()
    registry = tmp_path / "registry.toml"
    registry.write_text("\n".join([
        "schema_version = 1", "", "[[styles]]", 'id = "X1"', 'name = "待移除风格"',
        "aliases = []", 'status = "removable"', 'validation_level = "legacy_unverified"',
        'kind = "style"', 'pack_slug = ""', 'pack_version = ""', 'pack_hash = ""',
        "shareable = true", "",
    ]), encoding="utf-8")
    before = registry.read_bytes()
    r = _cli("remove", "--style", "待移除风格", "--root", empty,
             "--registry", registry, "--force-unreferenced", "--confirm")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "待删除清单" in r.stdout
    plan = empty / "风格待删除清单.md"
    assert plan.is_file()
    assert "待移除风格" in plan.read_text(encoding="utf-8")
    # 只产出清单：登记表与项目文件都不动
    assert registry.read_bytes() == before
    assert not any(empty.rglob("*.toml"))
    assert re.search(r"不执行物理删除", r.stdout)


def test_removable_requires_disabled_first(tmp_path):
    """active 直接 remove 必须被拒（生命周期单向）。"""
    r = _cli("remove", "--style", STYLE_NAME, "--root", tmp_path,
             "--force-unreferenced", "--confirm")
    assert "removable" in (r.stdout + r.stderr) or r.returncode != 0


# --- CLI -------------------------------------------------------------------------

def test_cli_deprecate_and_scan(tmp_path):
    r = _cli("scan-usage", "--style", STYLE_NAME, "--root", tmp_path, "--json")
    assert r.returncode == 0
    assert json.loads(r.stdout) == []

    r2 = _cli("deprecate", "--style", "并不存在的风格XYZ", "--root", tmp_path)
    assert r2.returncode != 0
    assert "登记表" in (r2.stdout + r2.stderr)


def test_cli_list_marks_lifecycle():
    r = _cli("list", "--json", "--all")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert all("生命周期" in row for row in data)
    assert any(row["生命周期"] == "active" for row in data)


def test_no_glob_or_path_concatenation_deletion():
    """remove 不得用通配符或路径拼接直接删文件。"""
    src = (REPO_ROOT / "scripts" / "manage_styles.py").read_text(encoding="utf-8")
    for forbidden in ("shutil.rmtree(", "unlink()", "glob("):
        assert forbidden not in src, forbidden
