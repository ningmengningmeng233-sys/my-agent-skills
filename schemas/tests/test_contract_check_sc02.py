# -*- coding: utf-8 -*-
"""SC-02 回归：契约门不再被技能包目录改名/升版烂掉。

覆盖 contract_check.py 的三处修复（动态发现、包内 schema 回退、H3 权威别名）
与两处数据同步（twin_of 配对名、twin-kernel 登记簿）。除最后一项整树验收外
全部 hermetic（tmp_path 自建根），不读真实技能包、不碰网络。
"""
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

SCHEMAS_DIR = Path(__file__).resolve().parent.parent
CABINET_ROOT = SCHEMAS_DIR.parent
sys.path.insert(0, str(SCHEMAS_DIR))

import contract_check as cc  # noqa: E402

EXPECTED_CONTRACT_IDS = {
    "dramacraft-v150-h3-spec-mirror",
    "dramacraft-script",
    "dramacraft-storyboard",
    "asset-script-input",
    "asset-ledger-producer",
    "h3-shot-table",
    "knowledge-index-twin-kernel",
    "adult-index-twin-kernel",
}

# SC-02 之前烂掉的旧版本目录签名：发现结果里一个都不许再出现。
STALE_DIR_MARKERS = ("v1.5.3", "rc.5", "v7.4.4", "v5.4.4", "v4.8.3")

MIN_SCHEMA = {
    "x-contract": {"version": "1.0.0"},
    "type": "object",
    "properties": {"source": {"type": "string"}},
    "required": ["source"],
}


def _skill_md(version, *blocks):
    doc = '---\nversion: "%s"\n---\n# test skill\n' % version
    for b in blocks:
        doc += "<!-- pipeline-contract:begin\n%s\npipeline-contract:end -->\n" % b
    return doc


def test_discover_finds_existing_contract_files():
    found = cc.discover_skill_files(CABINET_ROOT)
    assert found, "动态发现一个技能都找不到，门会回退到静态清单"
    for rel in found:
        assert (CABINET_ROOT / rel).is_file(), f"发现结果不存在：{rel}（SC-02 之前全是这种过期硬编码）"
        assert not rel.startswith("."), rel
    assert len(found) == len(set(found)), f"发现结果重复：{found}"
    assert found == sorted(found), "发现结果必须排序稳定，否则报告顺序抖动"


def test_discover_covers_all_contract_ids_without_stale_dirs():
    found = cc.discover_skill_files(CABINET_ROOT)
    for rel in found:
        for marker in STALE_DIR_MARKERS:
            assert marker not in rel, f"发现结果含过期目录签名 {marker}：{rel}"
    ids = set()
    for rel in found:
        text = (CABINET_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        for m in cc.BLOCK_RE.finditer(text):
            ids.add(yaml.safe_load(m.group(1))["id"])
    assert ids == EXPECTED_CONTRACT_IDS, f"契约覆盖缺口：{EXPECTED_CONTRACT_IDS ^ ids}"


def test_schema_package_relative_fallback(tmp_path):
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "s_root.schema.json").write_text(
        json.dumps(MIN_SCHEMA), encoding="utf-8")
    vendor = tmp_path / "skillA" / "vendor" / "schemas"
    vendor.mkdir(parents=True)
    (vendor / "a.schema.json").write_text(json.dumps(MIN_SCHEMA), encoding="utf-8")
    (tmp_path / "skillA" / "SKILL.md").write_text(_skill_md(
        "1.0.0",
        'id: a-pkg\nnamespace: doc-a\nrole: producer\n'
        'schema: vendor/schemas/a.schema.json\nversion: "1.0.0"\nskill_version: "1.0.0"\n'
        'fields: [source]',
        'id: a-root\nnamespace: doc-b\nrole: producer\n'
        'schema: schemas/s_root.schema.json\nversion: "1.0.0"\nskill_version: "1.0.0"\n'
        'fields: [source]',
        'id: a-missing\nnamespace: doc-c\nrole: producer\n'
        'schema: schemas/nope.schema.json\nversion: "1.0.0"\nskill_version: "1.0.0"\n'
        'fields: [source]',
    ), encoding="utf-8")
    code, drifts, _ = cc.run_check(tmp_path, skill_files=["skillA/SKILL.md"],
                                   h3_scan_set=[], exceptions=[], today="2026-10-14")
    missing = [d for d in drifts if d["code"] == "SCHEMA_NOT_FOUND"]
    assert [d["contract"] for d in missing] == ["a-missing"], (
        f"包内相对/柜根相对应解析成功、唯缺件报红，实测：{[(d['code'], d['contract']) for d in drifts]}")
    assert code == 1, "有真实漂移必须 exit 1（且不是 exit 2 工具错误）"


def _h3_tmp_root(tmp_path, derived_from, source="copy A"):
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "s.schema.json").write_text(
        json.dumps({"x-contract": {"version": "1.0.0"}, "type": "object"}), encoding="utf-8")
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "h3.md").write_text("# authority skeleton", encoding="utf-8")
    (tmp_path / "skillB").mkdir()
    (tmp_path / "skillB" / "SKILL.md").write_text(_skill_md(
        "1.0.0",
        'id: h-t\nnamespace: doc-x\n'
        'schema: schemas/s.schema.json\nversion: "1.0.0"\nskill_version: "1.0.0"\n'
        'h3_spec_source: "%s"\nh3_spec_role: derived\nderived_from: "%s"'
        % (source, derived_from),
    ), encoding="utf-8")
    return [{"path": "auth/h3.md", "name": "test authority",
             "h3_spec_role": "authority", "aliases": ["upstream-logical:h3.md"]}]


def test_h3_authority_alias_exact_match(tmp_path):
    scan = _h3_tmp_root(tmp_path, "upstream-logical:h3.md")
    _code, drifts, _ = cc.run_check(tmp_path, skill_files=["skillB/SKILL.md"],
                                    h3_scan_set=scan, exceptions=[], today="2026-10-14")
    h3 = [d for d in drifts if d["code"].startswith("H3_")]
    assert h3 == [], f"登记别名应精确命中权威，实测：{h3}"


def test_h3_unknown_derivation_still_drifts(tmp_path):
    scan = _h3_tmp_root(tmp_path, "something-else")
    _code, drifts, _ = cc.run_check(tmp_path, skill_files=["skillB/SKILL.md"],
                                    h3_scan_set=scan, exceptions=[], today="2026-10-14")
    h3 = [d["code"] for d in drifts if d["code"].startswith("H3_")]
    assert h3 == ["H3_DERIVATION_UNDECLARED"], (
        f"别名机制不许模糊匹配，未登记指向必须报红，实测：{drifts}")


def test_twin_registry_names_resolve_to_real_dirs():
    doc = json.loads((SCHEMAS_DIR / "twin-kernel.json").read_text(encoding="utf-8"))
    assert doc["pairs"], "登记簿没有配对，twin 门实际空转"
    for pair in doc["pairs"]:
        for side in ("trunk", "customization"):
            d = CABINET_ROOT / pair[side] / pair.get(f"{side}_scripts", "scripts")
            assert d.is_dir(), f"登记簿{side}目录不存在：{pair[side]}（改名后未同步即烂门）"
        for ref in pair["declared_by"]:
            f = ref.split("#")[0]
            assert (CABINET_ROOT / f).is_file(), f"declared_by 悬空：{ref}"


def _official_authority_path():
    return Path(os.path.expandvars(os.path.expanduser(cc.OFFICIAL_H3_SKELETON)))


@pytest.mark.skipif(not _official_authority_path().is_file(),
                    reason="H3 唯一权威骨架只在作者机技能库在位时才可做整树验收")
def test_real_tree_gate_is_clean():
    code, drifts, summary = cc.run_check(CABINET_ROOT)
    assert code == 0, f"整树门禁 exit {code}：{drifts}"
    assert summary["contracts"] == len(EXPECTED_CONTRACT_IDS)
