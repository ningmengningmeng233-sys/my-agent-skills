# -*- coding: utf-8 -*-
"""W04 剧本输入适配器与资产来源双向追踪单元测试。"""
from pathlib import Path
import pytest

TEST_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TEST_DIR.parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
import sys
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from engine.script_adapter import (
    adapt_script_to_assets,
    script_to_markdown,
    extract_script_entities,
)
from engine.asset_ledger import build_ledger


@pytest.fixture
def sample_script():
    return {
        "source": "水浒传",
        "version": "1.0.0",
        "episodes": [
            {
                "ep": 1,
                "targetSeconds": 120,
                "hook": "大雪纷飞",
                "cliff": "长枪挑落酒葫芦",
                "scenes": [
                    {
                        "sceneId": "S01",
                        "location": "草料场山神庙",
                        "lighting": "风雪黄昏",
                        "characters": ["C01", "C02"],
                        "props": ["P01"],
                        "flow": [
                            {"type": "action", "action": "林冲推开山神庙大门，抖落斗笠积雪。"},
                            {"type": "dialogue", "speaker": "C01", "line": "好大一场风雪！"},
                            {"type": "action", "action": "陆谦手持解腕尖刀从暗处逼近。"},
                            {"type": "dialogue", "speaker": "C02", "line": "林教头，认得我么？"},
                        ],
                    }
                ],
            }
        ],
    }


def test_script_to_markdown_preserves_scenes_and_beats(sample_script):
    """测试将 script.json 导出为保持结构与场次标题的规范文本。"""
    md = script_to_markdown(sample_script)
    assert "第1集 第1场" in md or "第1场" in md
    assert "草料场山神庙" in md
    assert "林冲推开山神庙大门" in md
    assert "好大一场风雪" in md


def test_extract_script_entities(sample_script):
    """测试结构化提取人物、场景、道具及其 source_refs。"""
    entities = extract_script_entities(sample_script)
    chars = entities["characters"]
    scenes = entities["scenes"]
    props = entities["props"]

    assert "C01" in chars
    assert "C02" in chars
    assert "S01" in scenes
    assert "P01" in props

    # 验证 source_refs 详细定位
    s01_refs = scenes["S01"]["source_refs"]
    assert len(s01_refs) >= 1
    assert s01_refs[0]["source_id"] == "S01"
    assert s01_refs[0]["source_type"] == "script"
    assert s01_refs[0]["scene_id"] == "S01"
    assert s01_refs[0]["episode"] == 1

    c01_refs = chars["C01"]["source_refs"]
    assert c01_refs[0]["source_id"] == "C01"
    assert c01_refs[0]["source_type"] == "script"


def test_adapt_script_to_assets_and_ledger(sample_script):
    """测试完整流程：从 script.json 产生资产索引并构建带 source_refs 的总账，双向可追溯。"""
    index = adapt_script_to_assets(sample_script)
    assert "assets" in index
    assets = index["assets"]

    # 必须包含三类资产
    types = {a["type"] for a in assets}
    assert "人物" in types
    assert "场景" in types
    assert "道具" in types

    # 构建总账
    ledger = build_ledger(index)
    assert len(ledger) >= 3

    # 验证原 ID → 资产 ID → 来源位置 双向追踪
    c01_entry = next(e for e in ledger if any(r.get("source_id") == "C01" for r in e.get("source_refs", [])))
    assert c01_entry["asset_type"] == "person"
    assert c01_entry["asset_id"].startswith("CH_")
    ref = c01_entry["source_refs"][0]
    assert ref["source_id"] == "C01"
    assert ref["scene_id"] == "S01"
    assert ref["episode"] == 1


def _source_text(script):
    """script.json 里真实存在的文本集合（场景地、台词、动作），逐字判定的唯一依据。"""
    texts = [str(script.get("source", ""))]
    for ep in script.get("episodes", []):
        for key in ("hook", "cliff"):
            if ep.get(key):
                texts.append(str(ep[key]))
        for scene in ep.get("scenes", []):
            for key in ("location", "lighting"):
                if scene.get(key):
                    texts.append(str(scene[key]))
            for beat in scene.get("flow", []):
                for key in ("action", "line"):
                    if beat.get(key):
                        texts.append(str(beat[key]))
    return chr(10).join(texts)


def test_appearance_refs_do_not_fake_a_verbatim_quote(sample_script):
    """出场/道具是结构化事实，不是原文摘录：不得把「第N集SM场出场」写成 quote。"""
    entities = extract_script_entities(sample_script)
    import re
    fake = re.compile(r"^\d*第?\d*集?S\d+场(出场|道具)")
    bad = []
    for group in entities.values():
        for ent in group.values():
            for ref in ent.get("source_refs", []):
                q = str(ref.get("quote", ""))
                if fake.match(q):
                    bad.append((ent["id"], q))
    assert not bad, f"派生说明被当成逐字引用: {bad}"


def test_appearance_refs_carry_an_explicit_note(sample_script):
    """派生说明改放 appearance_note，坐标（集/场/编号）仍完整可回溯。"""
    entities = extract_script_entities(sample_script)
    notes = []
    for cid, ent in entities["characters"].items():
        for ref in ent.get("source_refs", []):
            if "quote" not in ref:
                notes.append((ref.get("appearance_note"), ref.get("episode"),
                              ref.get("scene_id"), ref.get("source_id")))
    assert ("第1集S01场出场", 1, "S01", "C01") in notes or any(n and n[3] for n in notes), notes
    assert all(n[0] for n in notes), f"派生说明缺字段: {notes}"


def test_every_quote_is_verbatim_source_text(sample_script):
    """凡有 quote 就必须能在 script.json 真实文本里逐字找到（截断也算，因为它是前缀）。"""
    source = _source_text(sample_script)
    entities = extract_script_entities(sample_script)
    offenders = []
    for group in entities.values():
        for ent in group.values():
            for ref in ent.get("source_refs", []):
                q = str(ref.get("quote", ""))
                if q and q not in source:
                    offenders.append((ent["id"], q))
    assert not offenders, f"quote 不是原文: {offenders}"


def test_dialogue_quote_keeps_the_whole_line(sample_script):
    """台词引用存整句，不再截到 40 字，避免下游按引用复核时被截断文本误导。"""
    entities = extract_script_entities(sample_script)
    lines = [ref.get("quote") for ref in entities["characters"]["C01"]["source_refs"]
             if ref.get("beat_index")]
    assert "好大一场风雪！" in lines, lines


def test_entity_ids_and_group_shape_stay_compatible(sample_script):
    """旧格式兼容：分组键、实体 ID 与 adapt/ledger 链路不因分字段而变。"""
    entities = extract_script_entities(sample_script)
    assert set(entities) == {"characters", "scenes", "props"}
    assert set(entities["characters"]) == {"C01", "C02"}
    assert set(entities["scenes"]) == {"S01"}
    assert set(entities["props"]) == {"P01"}
    adapted = adapt_script_to_assets(sample_script)
    ledger = build_ledger(adapted)
    assert ledger, "总账仍要能建出来"
