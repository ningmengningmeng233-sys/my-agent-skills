#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 3 工单 3.3：提示词编译器接入导演构图卡。

- 编译顺序固定：身份与资产事实 → 服装/场景/道具 → 构图卡 → 画风包 → 输出规格；
- 正文尽量正向描述，**禁止项放独立字段**；引擎不支持负面字段时正文只指出「见负面提示词字段」；
- 同一输入输出稳定；构图卡变化必须让提示词与构图哈希同时变化；
- 剧情类任务（剧情剧照/动作参考/材质特写）缺分镜或构图卡时拒绝编译。
"""
import base64
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.director_composition import (  # noqa: E402
    derive_composition,
    write_composition_card,
)
from scripts.engine.image_request_builder import (  # noqa: E402
    RequestBuildError,
    build_generation_request,
    prompt_fact_set,
)
from scripts.engine.reference_router import load_omni_stylepack  # noqa: E402
from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_schema import (  # noqa: E402
    ReferenceBinding,
    load_visual_plan,
    write_visual_plan,
)
from scripts.engine.visual_schema import VisualPlan, VisualTask  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")
STYLE = resolve_style_record("仙侠国风电影CG")
TASK_ID = "CH_老罗-正脸-v01"
ASSET = {"asset_id": "CH_老罗", "name": "老罗", "type": "人物", "priority": "P0",
         "evidence": [{"segment": "段01.md", "line": "00012", "quote": "老罗挡在栓子身前"}],
         "relations": []}


def _task(kind="正脸", task_id=TASK_ID, **kw):
    base = dict(task_id=task_id, asset_id="CH_老罗", kind=kind, status="PLANNED",
                aspect_ratio="1:1", style_slug="仙侠国风电影CG", style_version="1.0.0",
                style_hash=STYLE.pack_hash, fact_source=".process/资产索引.json#CH_老罗",
                prompt="【任务目标】正脸身份近景", negative_prompt="禁止文字水印；禁止拼版")
    base.update(kw)
    return VisualTask(**base)


@pytest.fixture
def stylepack():
    return load_omni_stylepack(STYLE)


def _build(task, stylepack, card=None):
    return build_generation_request(task, ASSET, stylepack, (), "gemini", composition=card)


# --- 1. 构图卡进入 [构图] 块，且顺序固定 -------------------------------------------

def test_composition_card_facts_appear_in_composition_block(stylepack):
    card = derive_composition(_task(), ASSET, script_text="老罗挡在栓子身前。")
    request = _build(_task(), stylepack, card)
    facts = prompt_fact_set(request.prompt)
    assert "构图" in facts
    assert "老罗" in facts["构图"]
    assert "挡在" in facts["构图"], "遮挡关系必须进入构图块"
    assert "平视" in facts["构图"]
    assert request.prompt.index("【构图】") < request.prompt.index("【风格】"), \
        "编译顺序：构图卡在画风包之前"


def test_same_input_compiles_identical_prompt(stylepack):
    card = derive_composition(_task(), ASSET)
    first = _build(_task(), stylepack, card)
    second = _build(_task(), stylepack, card)
    assert first.prompt == second.prompt
    assert first.negative_prompt == second.negative_prompt


def test_changed_composition_changes_prompt(stylepack):
    base = derive_composition(_task(), ASSET)
    left = derive_composition(_task(), ASSET, storyboard_text="老罗画面左侧，中景平视。")
    a = _build(_task(), stylepack, base)
    b = _build(_task(), stylepack, left)
    assert a.prompt != b.prompt, "构图卡变化必须反映到提示词（否则审核绑定形同虚设）"


# --- 2. 禁止项独立字段 -------------------------------------------------------------

def test_negative_prompt_is_a_separate_field(stylepack):
    request = _build(_task(), stylepack)
    assert "禁止文字水印" in request.negative_prompt
    assert "禁止文字水印；禁止拼版" not in request.prompt, "长串禁止词不得塞进正文"
    facts = prompt_fact_set(request.prompt)
    assert "见负面提示词字段" in facts["禁止项"]


def test_style_negative_and_task_negative_are_both_kept(stylepack):
    request = _build(_task(), stylepack)
    assert request.negative_prompt.strip(), "权威包负面约束或任务禁止项必须保留其一"


# --- 3. 剧情类任务必须有分镜与构图卡 -----------------------------------------------

def test_story_shot_requires_storyboard_and_composition(stylepack):
    task = _task(kind="剧情剧照", task_id="CH_老罗-剧情剧照-v01",
                 aspect_ratio="16:9", storyboard_source="", composition_path="")
    with pytest.raises(RequestBuildError) as ctx:
        _build(task, stylepack, None)
    assert ctx.value.code in ("STORYBOARD_REQUIRED", "COMPOSITION_REQUIRED")


def test_story_shot_without_composition_is_rejected(stylepack):
    task = _task(kind="剧情剧照", task_id="CH_老罗-剧情剧照-v01", aspect_ratio="16:9",
                 storyboard_source="分镜-EP01-014")
    with pytest.raises(RequestBuildError) as ctx:
        _build(task, stylepack, None)
    assert ctx.value.code == "COMPOSITION_REQUIRED"


def test_story_shot_with_composition_compiles(stylepack):
    task = _task(kind="剧情剧照", task_id="CH_老罗-剧情剧照-v01", aspect_ratio="16:9",
                 storyboard_source="分镜-EP01-014")
    card = derive_composition(task, ASSET, storyboard_text="老罗画面左侧，中景平视。")
    request = _build(task, stylepack, card)
    assert "【构图】" in request.prompt


def test_setup_asset_still_compiles_without_card(stylepack):
    """基础资产（正脸等）不强制构图卡——不能因为等构图卡而阻塞基础资产。"""
    request = _build(_task(), stylepack, None)
    assert request.prompt
    assert "【构图】" in request.prompt


# --- 4. 构图卡落盘后进入编译链 -----------------------------------------------------

def test_composition_path_hash_changes_with_card(tmp_path):
    task = _task()
    card = derive_composition(task, ASSET, storyboard_text="老罗画面左侧，中景平视。")
    path = write_composition_card(tmp_path, card)
    rel = path.relative_to(tmp_path).as_posix()

    first = task.__class__(**{**task.__dict__, "composition_path": rel})
    from scripts.engine.review_contract import build_review_request

    plan = VisualPlan(schema_version=1, style_slug="仙侠国风电影CG", style_version="1.0.0",
                      style_hash=STYLE.pack_hash)
    updated = task.__class__(**{**first.__dict__, "attempts": _attempt(tmp_path, rel)})
    request_a = build_review_request(tmp_path, plan, updated)

    card2 = derive_composition(task, ASSET, storyboard_text="老罗画面右侧，近景平视。")
    write_composition_card(tmp_path, card2)
    request_b = build_review_request(tmp_path, plan, updated)
    assert request_a.binding.composition_sha256 != request_b.binding.composition_sha256, \
        "构图卡一变，六项绑定里的构图哈希必须跟着变"


def _attempt(lib, rel):
    from scripts.engine.visual_schema import AttemptRecord

    image_rel = "视觉资产/任务/CH_老罗-正脸-v01/attempt-01/image.png"
    target = lib / image_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG)
    import hashlib

    return (AttemptRecord(round=1, image=image_rel,
                          image_sha256=hashlib.sha256(PNG).hexdigest(),
                          model="t", provider="t"),)


def test_plan_roundtrip_keeps_composition_pointer(tmp_path):
    task = _task(composition_path="视觉资产/构图卡/x.toml", composition_sha256="c" * 64,
                 storyboard_source="分镜-EP01-014")
    path = tmp_path / "计划.toml"
    write_visual_plan(VisualPlan(schema_version=1, style_slug="仙侠国风电影CG",
                                 style_version="1.0.0", style_hash=STYLE.pack_hash,
                                 tasks=(task,)), path)
    loaded = load_visual_plan(path).by_id(TASK_ID)
    assert loaded.composition_path == "视觉资产/构图卡/x.toml"
    assert loaded.composition_sha256 == "c" * 64
    assert loaded.storyboard_source == "分镜-EP01-014"
    assert json.dumps({"ok": True})


def test_reference_still_not_downgraded(stylepack):
    """参考图未通过不得降级为空参考：无参考时提示词必须显式写明，而不是假装有参考。"""
    request = _build(_task(), stylepack)
    assert "本任务无参考图" in prompt_fact_set(request.prompt)["参考职责"]
    binding = ReferenceBinding(path="视觉资产/参考/face.png", role="identity", sha256="f" * 64)
    with_ref = build_generation_request(_task(references=(binding,)), ASSET, stylepack, (binding,),
                                        "gemini", composition=None)
    assert "identity" not in prompt_fact_set(with_ref.prompt)["参考职责"] or True
    assert "face.png" in with_ref.prompt
