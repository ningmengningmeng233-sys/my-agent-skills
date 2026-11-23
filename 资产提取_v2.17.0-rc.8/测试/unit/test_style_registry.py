#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单02-Task1：语义风格登记与兼容别名（规格 5）。

用户只看到可理解的中文风格名；A1/H7 等内部编号仅用于兼容与诊断。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.style_registry import (  # noqa: E402
    OMNI_VERIFIED,
    LEGACY_UNVERIFIED,
    STYLE_ALIAS_CONFLICT,
    StyleAliasConflict,
    StyleRecord,
    StyleRegistry,
    list_all_styles,
    list_user_styles,
    load_registry,
    resolve_style_record,
)

REGISTRY_PATH = REPO_ROOT / "references" / "styles" / "registry.toml"
STYLE_LIB = REPO_ROOT / "references" / "画风库.md"


def test_h7_and_aliases_resolve_to_semantic_name():
    for value in ("H7", "仙侠CG", "luminous-xianxia", "仙侠国风电影CG"):
        record = resolve_style_record(value)
        assert record is not None, value
        assert record.name == "仙侠国风电影CG", value


def test_registry_file_exists_and_loads():
    assert REGISTRY_PATH.is_file(), f"缺少登记表 {REGISTRY_PATH}"
    reg = load_registry()
    assert reg.records


def test_registry_covers_every_style_card():
    """登记表必须覆盖画风库全部卡片，防止治理面漏项。"""
    import re

    ids = re.findall(r"^### ([A-H]\d+)\.", STYLE_LIB.read_text(encoding="utf-8"), re.M)
    registered = {r.id for r in list_all_styles()}
    missing = sorted(set(ids) - registered)
    assert not missing, f"登记表缺少卡片: {missing}"


def test_user_style_list_hides_internal_ids():
    for record in list_user_styles():
        assert record.name, record.id
        for field in (record.name,):
            assert field.strip() != record.id
        # 面向用户的列表不得把内部编号当显示名
        assert record.name != record.id


def test_every_user_style_has_chinese_name():
    for record in list_user_styles():
        assert any("\u4e00" <= ch <= "\u9fff" for ch in record.name), record.name


def test_legacy_styles_are_marked_unverified():
    reg = load_registry()
    legacy = [r for r in reg.records if r.id != "H7" and r.kind == "style"]
    assert legacy
    for record in legacy:
        assert record.validation_level == LEGACY_UNVERIFIED, record.id


def test_only_xianxia_card_is_omni_verified():
    verified = [r for r in list_all_styles() if r.validation_level == OMNI_VERIFIED]
    assert [r.id for r in verified] == ["H7"]


def test_omni_style_records_pack_metadata():
    record = resolve_style_record("仙侠国风电影CG")
    assert record.pack_slug
    assert record.pack_version
    assert record.shareable is False, "含私人参考母版的风格默认不可分享"


def test_disabled_style_not_in_default_list():
    synthetic = StyleRegistry(records=(
        StyleRecord(id="X1", name="可用风格", aliases=(), status="active",
                    validation_level=LEGACY_UNVERIFIED, pack_slug="", pack_version="",
                    pack_hash="", shareable=True, kind="style"),
        StyleRecord(id="X2", name="已停用风格", aliases=(), status="disabled",
                    validation_level=LEGACY_UNVERIFIED, pack_slug="", pack_version="",
                    pack_hash="", shareable=True, kind="style"),
        StyleRecord(id="X3", name="已弃用风格", aliases=(), status="deprecated",
                    validation_level=LEGACY_UNVERIFIED, pack_slug="", pack_version="",
                    pack_hash="", shareable=True, kind="style"),
    ))
    default = {r.id for r in list_user_styles(registry=synthetic)}
    with_legacy = {r.id for r in list_user_styles(include_legacy=True, registry=synthetic)}
    assert "X2" not in default and "X2" not in with_legacy
    assert "X3" not in default and "X3" in with_legacy
    assert "X1" in default


def test_reference_tables_are_not_offered_as_styles():
    """F/G 系是速查表而非可选风格，不得出现在用户风格列表里。"""
    user_ids = {r.id for r in list_user_styles(include_legacy=True)}
    assert not any(i.startswith(("F", "G")) for i in user_ids)
    assert any(i.startswith(("A", "H")) for i in user_ids)


def test_alias_conflict_raises_instead_of_first_hit():
    synthetic = StyleRegistry(records=(
        StyleRecord(id="X1", name="甲风格", aliases=("同名",), status="active",
                    validation_level=LEGACY_UNVERIFIED, pack_slug="", pack_version="",
                    pack_hash="", shareable=True, kind="style"),
        StyleRecord(id="X2", name="乙风格", aliases=("同名",), status="active",
                    validation_level=LEGACY_UNVERIFIED, pack_slug="", pack_version="",
                    pack_hash="", shareable=True, kind="style"),
    ))
    with pytest.raises(StyleAliasConflict) as ctx:
        resolve_style_record("同名", registry=synthetic)
    assert ctx.value.code == STYLE_ALIAS_CONFLICT
    assert "同名" in str(ctx.value)


def test_unknown_value_returns_none():
    assert resolve_style_record("这个画风不存在") is None
    assert resolve_style_record("") is None


def test_resolution_is_case_and_space_insensitive():
    for value in ("h7", " H7 ", "LUMINOUS-XIANXIA"):
        record = resolve_style_record(value)
        assert record is not None and record.id == "H7", value


def test_records_are_immutable():
    record = resolve_style_record("H7")
    with pytest.raises(Exception):
        record.name = "改名"
