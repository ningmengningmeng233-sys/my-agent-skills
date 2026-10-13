# -*- coding: utf-8 -*-
"""工单 0 夹具守卫：脱敏回归夹具的结构完整性、出处可追性与脱敏性。

不跑流水线（保持快门绿）：期望资产数量一律从 expected_assets.json 机器读取，
绝不硬编码在断言里。夹具不完整或含真实专名/密钥/绝对路径时本测试失败。
"""
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "测试" / "fixtures" / "asset_compiler_regression"
SOURCE = FIXTURE_DIR / "source.txt"
EXPECTED = FIXTURE_DIR / "expected_assets.json"

# 各类型正式资产的最低期望（v2.17 目标真值的下限）
FORMAL_FLOORS = {"person": 7, "scene": 6, "prop": 10, "costume": 3}
# 后期元素：非正式，但必须出现的最小条数
POST_ELEMENT_FLOOR = 2


def _load_expected():
    with EXPECTED.open(encoding="utf-8") as f:
        return json.load(f)


def _source_text():
    return SOURCE.read_text(encoding="utf-8")


def test_fixture_files_present():
    assert SOURCE.is_file(), "缺少脱敏源文夹具 source.txt"
    assert EXPECTED.is_file(), "缺少期望资产真值 expected_assets.json"


def test_expected_carries_schema_version():
    data = _load_expected()
    assert data.get("schema_version") == 1, "所有新 JSON 必须带 schema_version"
    assert isinstance(data.get("assets"), list) and data["assets"], "assets 不能为空"


def test_formal_asset_counts_meet_floors():
    data = _load_expected()
    formal_by_type: dict[str, int] = {}
    for a in data["assets"]:
        if a.get("is_formal"):
            formal_by_type[a["asset_type"]] = formal_by_type.get(a["asset_type"], 0) + 1
    post = sum(1 for a in data["assets"] if a["asset_type"] == "post_element")
    for typ, floor in FORMAL_FLOORS.items():
        assert formal_by_type.get(typ, 0) >= floor, (
            f"{typ} 正式资产 {formal_by_type.get(typ, 0)} < 下限 {floor}"
        )
    assert post >= POST_ELEMENT_FLOOR, f"后期元素 {post} < 下限 {POST_ELEMENT_FLOOR}"


def test_every_evidence_quote_is_traceable_in_source():
    """每条期望资产的证据摘句必须真实出现在脱敏源文中（出处不可伪造）。"""
    text = _source_text()
    missing = [
        a["canonical_name"]
        for a in _load_expected()["assets"]
        if a.get("evidence_quote") and a["evidence_quote"] not in text
    ]
    assert not missing, f"以下资产摘句在源文中找不到，出处校验失败: {missing}"


def test_required_hard_cases_are_represented():
    """夹具必须覆盖：单次出现关键道具、同名歧义、人物状态、群演、制作说明、后期文字。"""
    assets = _load_expected()["assets"]
    props = {a["canonical_name"] for a in assets if a["asset_type"] == "prop"}
    assert "残破木门" in props, "缺少单次出现但推动剧情的关键道具"
    kinds = {a["asset_type"] for a in assets}
    assert "state" in kinds, "缺少人物状态"
    assert "production_note" in kinds, "缺少制作说明"
    assert "post_element" in kinds, "缺少后期元素"
    # 群演：至少一条 crowd 且 is_formal False
    assert any(a.get("narrative_importance") == "crowd" and not a["is_formal"] for a in assets)
    # 同名歧义：两条 person 记录分别引用"阿锦"，且被显式区分为不同实体
    persons = [a for a in assets if a["asset_type"] == "person"]
    ajin_persons = [
        p for p in persons
        if "阿锦" in p["canonical_name"] or any("阿锦" in al for al in p.get("aliases", []))
    ]
    assert len(ajin_persons) >= 2, "缺少同名歧义的两条独立记录"


def test_production_note_and_post_elements_are_not_formal_props():
    """制作说明与后期文字绝不得记为正式资产，也不得进道具类。"""
    for a in _load_expected()["assets"]:
        if a["asset_type"] in {"post_element", "production_note"}:
            assert a["is_formal"] is False, f"{a['canonical_name']} 不得为正式资产"
            assert a["asset_type"] != "prop", "后期元素/制作说明不得计为道具"


def test_fixture_is_deidentified():
    """禁止真实项目专名、密钥形态、绝对用户路径进入夹具。"""
    blob = _source_text() + _load_expected_raw_text()
    forbidden_literals = ["DBIO", "历险记"]
    for lit in forbidden_literals:
        assert lit not in blob, f"夹具含真实项目专名: {lit}"
    # 密钥/令牌形态
    secret_patterns = [
        r"sk-[A-Za-z0-9]{16,}",
        r"api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9]{12,}",
        r"https?://[^\s\"']*chat/completions",
        r"Bearer\s+[A-Za-z0-9._\-]{12,}",
    ]
    for pat in secret_patterns:
        assert not re.search(pat, blob, re.I), f"夹具命中疑似密钥/端点形态: {pat}"
    # 绝对用户路径
    path_patterns = [r"[A-Za-z]:\\\\Users\\\\", r"/home/[a-z0-9_]+/", r"/Users/[a-z0-9_]+/"]
    for pat in path_patterns:
        assert not re.search(pat, blob, re.I), f"夹具命中绝对用户路径: {pat}"


def _load_expected_raw_text():
    return EXPECTED.read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
