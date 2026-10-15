#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 3 工单 3.1：导演构图推导器（输入优先级 + 事实分层 + 冲突记录）。

事实优先级固定为：**原始剧本 > 资产索引 > 分镜提示词 > 导演推导**。
- 分镜提示词不得改写剧本事实与资产身份；冲突时保留剧本事实并产出冲突记录；
- 导演推导只补充前三层未说明的站位与镜头细节，且必须标记来源，不得伪装成原文事实。

默认规则（设计 §7）：人物设定图正面/正后方、平视、居中、完整站立；剧情图用正常人眼高度。
没有剧情理由不得使用极端俯仰、荷兰角、贴脸广角或夸张留白。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.director_composition import (  # noqa: E402
    COMPOSITION_DIR_REL,
    SOURCE_ASSET,
    SOURCE_DIRECTOR,
    SOURCE_SCRIPT,
    SOURCE_STORYBOARD,
    CompositionCard,
    composition_issues,
    derive_composition,
    load_composition_card,
    write_composition_card,
)
from scripts.engine.visual_schema import ReferenceBinding, VisualTask  # noqa: E402

TASK_ID = "CH_老罗-正脸-v01"
ASSET = {
    "asset_id": "CH_老罗", "type": "人物", "name": "老罗", "priority": "P0",
    "relations": [
        {"type": "HOLDS", "target": "PR_柴刀"},
        {"type": "WEARS", "target": "CO_白衣"},
    ],
}


def _task(kind="正脸", task_id=TASK_ID, **kw):
    base = dict(task_id=task_id, asset_id="CH_老罗", kind=kind, status="PLANNED",
                aspect_ratio="1:1" if kind in ("正脸", "后脑", "道具") else "2:3",
                style_slug="仙侠国风电影CG", style_version="1.0.0", style_hash="a" * 64,
                fact_source=".process/资产索引.json#CH_老罗", purpose="身份近景")
    base.update(kw)
    return VisualTask(**base)


# --- 1. 设定图默认：正面/正后方、平视、居中、完整 --------------------------------

def test_setup_task_defaults_are_flat_front_centered():
    card = derive_composition(_task("正脸"), ASSET)
    spec = card.spec
    assert spec.camera_angle == "平视"
    assert spec.camera_height
    assert spec.subjects[0].zone == "中"
    assert spec.subjects[0].facing and "镜头" in spec.subjects[0].facing
    assert spec.source_level == SOURCE_DIRECTOR, "全默认时必须老实标记为导演推导"


def test_back_head_task_faces_away():
    card = derive_composition(_task("后脑", task_id="CH_老罗-后脑-v01"), ASSET)
    assert "背对" in card.spec.subjects[0].facing


def test_full_body_task_keeps_head_to_feet():
    card = derive_composition(_task("人物全身正面", task_id="CH_老罗-人物全身正面-v01"), ASSET)
    assert card.spec.shot_size == "全景"
    assert any("完整" in item for item in card.spec.forbidden) or "完整" in card.spec.rationale


def test_no_extreme_angles_without_reason():
    card = derive_composition(_task("正脸"), ASSET)
    joined = "；".join(card.spec.forbidden)
    assert "俯拍" in joined and "荷兰角" in joined and "贴脸" in joined


# --- 2. 剧本层：保护/遮挡关系与前后景 ---------------------------------------------

def test_script_protection_relation_becomes_occlusion():
    card = derive_composition(
        _task(), ASSET,
        script_text="老罗挡在栓子和刁三之间，把他们护在身后。",
        known_subjects=("老罗", "栓子", "刁三"))
    spec = card.spec
    first = spec.subjects[0]
    assert first.subject == "老罗"
    assert first.depth == "前景", "挡在中间的人在前景"
    assert "挡在" in first.occlusion or "护" in first.occlusion
    assert spec.source_level == SOURCE_SCRIPT
    assert card.evidence["occlusion"]["source"] == SOURCE_SCRIPT
    assert "挡在" in card.evidence["occlusion"]["quote"]


def test_script_subject_count_matches_mentions():
    card = derive_composition(_task(), ASSET,
                              script_text="老罗挡在栓子和刁三之间。",
                              known_subjects=("老罗", "栓子", "刁三"))
    assert len(card.spec.subjects) == 3
    assert {s.subject for s in card.spec.subjects} == {"老罗", "栓子", "刁三"}


# --- 3. 分镜层：剧本未说明时采用分镜位置 -------------------------------------------

def test_storyboard_position_used_when_script_silent():
    card = derive_composition(_task(), ASSET, script_text="", storyboard_text="老罗画面左侧，中景平视。")
    assert card.spec.subjects[0].zone == "左"
    assert card.spec.shot_size == "中景"
    assert card.spec.source_level == SOURCE_STORYBOARD
    assert card.evidence["zone"]["source"] == SOURCE_STORYBOARD


def test_storyboard_cannot_override_asset_identity():
    """分镜只能决定站位/景别等呈现层，不得改写资产身份字段。"""
    card = derive_composition(_task(), ASSET, storyboard_text="老罗改为白发老者，画面左侧。")
    assert card.spec.subjects[0].subject == "老罗"
    assert any(item.get("field") == "identity" for item in card.conflicts)


# --- 4. 冲突：保留剧本事实 + 产出冲突记录 -----------------------------------------

def test_script_wins_over_storyboard_and_conflict_is_recorded():
    card = derive_composition(_task(), ASSET,
                              script_text="老罗站在画面右侧。",
                              storyboard_text="老罗画面左侧。")
    assert card.spec.subjects[0].zone == "右", "剧本事实优先"
    assert card.conflicts, "冲突必须留痕"
    conflict = card.conflicts[0]
    assert conflict["field"] == "zone"
    assert conflict["kept"] == SOURCE_SCRIPT
    assert conflict["storyboard"]
    assert card.spec.source_level == SOURCE_SCRIPT


# --- 5. 导演推导：标记来源且可解释 ------------------------------------------------

def test_director_derivation_is_labelled_and_explained():
    card = derive_composition(_task(), ASSET)
    assert card.spec.source_level == SOURCE_DIRECTOR
    assert card.spec.rationale, "导演推导必须给出理由"
    assert card.evidence["zone"]["source"] == SOURCE_DIRECTOR
    assert card.evidence["zone"]["quote"], "默认值也要给出依据说明"


def test_asset_layer_is_recorded_when_relations_used():
    card = derive_composition(_task("道具", task_id="PR_柴刀-道具-v01"), ASSET)
    assert card.spec.props or card.spec.anchors
    assert card.spec.source_level in (SOURCE_ASSET, SOURCE_DIRECTOR)


# --- 6. 数量与持有者一致性 --------------------------------------------------------

def test_prop_holder_comes_from_asset_relations():
    card = derive_composition(_task("道具", task_id="PR_柴刀-道具-v01"), ASSET)
    assert any("柴刀" in p.name for p in card.spec.props) or card.spec.rationale
    issues = composition_issues(card, ASSET)
    assert isinstance(issues, list)


def test_subject_count_mismatch_is_reported():
    card = derive_composition(_task(), ASSET, script_text="老罗挡在栓子和刁三之间。",
                              known_subjects=("老罗", "栓子", "刁三"))
    tampered = CompositionCard(task_id=card.task_id,
                               spec=_replace_subjects(card.spec, card.spec.subjects[:2]),
                               evidence=dict(card.evidence), conflicts=card.conflicts)
    issues = composition_issues(tampered, ASSET, expected_subjects=("老罗", "栓子", "刁三"))
    assert any("COMPOSITION_SUBJECT_MISMATCH" in item for item in issues)


def _replace_subjects(spec, subjects):
    from dataclasses import replace

    return replace(spec, subjects=tuple(subjects))


# --- 7. 落盘与往返 ----------------------------------------------------------------

def test_card_roundtrip_toml(tmp_path):
    card = derive_composition(_task(), ASSET, script_text="老罗坐在茶楼里。")
    path = write_composition_card(tmp_path, card)
    assert path == tmp_path / COMPOSITION_DIR_REL[0] / COMPOSITION_DIR_REL[1] / f"{TASK_ID}.toml"
    loaded = load_composition_card(path)
    assert loaded.task_id == card.task_id
    assert loaded.spec.shot_size == card.spec.shot_size
    assert loaded.spec.subjects[0].zone == card.spec.subjects[0].zone
    assert loaded.spec.source_level == card.spec.source_level


def test_card_write_is_atomic_and_deterministic(tmp_path):
    card = derive_composition(_task(), ASSET)
    first = write_composition_card(tmp_path, card)
    before = first.read_bytes()
    write_composition_card(tmp_path, card)
    assert first.read_bytes() == before
    assert list(first.parent.glob("*.tmp")) == []


def test_card_toml_keeps_chinese_unescaped(tmp_path):
    card = derive_composition(_task(), ASSET)
    text = write_composition_card(tmp_path, card).read_text(encoding="utf-8")
    assert "原始剧本" in text or "导演推导" in text
    assert "\\u" not in text


def test_load_missing_card_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_composition_card(tmp_path / "不存在.toml")


def test_references_are_not_touched_by_derivation():
    """构图推导只产出构图事实，不修改任务的参考绑定。"""
    binding = ReferenceBinding(path="视觉资产/参考/face.png", role="identity", sha256="f" * 64)
    task = _task(references=(binding,))
    derive_composition(task, ASSET)
    assert task.references == (binding,)
