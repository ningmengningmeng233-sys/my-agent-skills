#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单03-Task3：最小发送请求编译器（规格 2.14 / 7）。

只发送当前任务的最小事实与选中参考图：不含其他角色、整章原文、绝对本机路径。
Gemini 与 Codex 表达可调整顺序，但事实集合必须完全一致。
"""
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.image_request_builder import (  # noqa: E402
    PROMPT_BLOCKS,
    build_generation_request,
    build_send_manifest,
    extract_private_facts,
)
from scripts.engine.reference_router import load_omni_stylepack  # noqa: E402
from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_schema import ReferenceBinding, VisualTask  # noqa: E402

STYLE = resolve_style_record("仙侠国风电影CG")
CH_QUOTE = "林姑娘走进茶楼，掌柜在柜台后拨算盘"
CHAPTER_TEXT = "整章原文" * 200
OTHER_ASSET = "CH_神秘客"
OTHER_QUOTE = "神秘客自阴影中现身"


def _index():
    return {
        "schema_version": 1, "source_fingerprint": "sha256:demo",
        "assets": [
            {"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘", "priority": "P1",
             "fact_level": "剧本事实", "notes": CHAPTER_TEXT,
             "evidence": [{"segment": "段01.md", "chapter": "第1章", "line": "00001",
                           "quote": CH_QUOTE}]},
            {"asset_id": OTHER_ASSET, "type": "人物", "name": "神秘客", "priority": "P2",
             "fact_level": "剧本事实",
             "evidence": [{"segment": "段07.md", "line": "00042", "quote": OTHER_QUOTE}]},
        ],
    }


def _asset(asset_id="CH_林姑娘"):
    return next(a for a in _index()["assets"] if a["asset_id"] == asset_id)


def _task(kind="正脸", **kw):
    base = dict(
        task_id="CH_林姑娘-正脸-v01", asset_id="CH_林姑娘", kind=kind, status="PLANNED",
        aspect_ratio="1:1", style_slug=STYLE.pack_slug, style_version=STYLE.pack_version,
        style_hash=STYLE.pack_hash, engine="gemini", model="gemini-3-pro-image",
        materials=("皮肤", "头发"), fact_source=".process/资产索引.json#CH_林姑娘",
        notes="林姑娘：清秀骨相，高束发",
    )
    base.update(kw)
    return VisualTask(**base)


REFS = (ReferenceBinding(path="references/face.png", role="identity", sha256="a" * 64,
                         note="身份参考"),
        ReferenceBinding(path="references/materials/skin.png", role="material",
                         sha256="b" * 64, note="皮肤材质参考"))


def _request(profile="gemini", task=None):
    return build_generation_request(task or _task(), _asset(), load_omni_stylepack(STYLE),
                                    list(REFS), profile)


# --- 最小发送（隐私） --------------------------------------------------------------

def test_only_current_asset_facts_are_sent():
    req = _request()
    assert CH_QUOTE in req.prompt
    assert OTHER_QUOTE not in req.prompt
    assert "神秘客" not in req.prompt
    assert OTHER_ASSET not in req.prompt


def test_full_chapter_text_is_not_sent():
    req = _request()
    assert CHAPTER_TEXT not in req.prompt
    assert "整章原文整章原文整章原文" not in req.prompt
    assert len(req.prompt) < 6000, len(req.prompt)


def test_no_absolute_machine_paths():
    req = _request()
    text = req.prompt + req.negative_prompt + str(build_send_manifest(req))
    assert "C:\\" not in text and "D:\\" not in text
    assert "/Users/" not in text and "/home/" not in text
    for binding in req.references:
        assert not Path(binding.path).is_absolute()


def test_extract_private_facts_returns_bounded_quote():
    facts = extract_private_facts(_task(), _asset())
    assert facts["name"] == "林姑娘"
    assert facts["quote"] == CH_QUOTE
    assert CHAPTER_TEXT not in str(facts)


def test_references_match_router_output():
    req = _request()
    assert [r.path for r in req.references] == [r.path for r in REFS]
    assert [r.role for r in req.references] == ["identity", "material"]


# --- 提示词分块 -----------------------------------------------------------------

def test_prompt_blocks_are_fixed_and_ordered():
    req = _request()
    positions = [req.prompt.index(f"【{block}】") for block in PROMPT_BLOCKS]
    assert positions == sorted(positions), req.prompt[:400]
    assert PROMPT_BLOCKS[0] == "任务目标"
    assert PROMPT_BLOCKS[-1] == "禁止项"


def test_prompt_blocks_are_chinese_and_style_comes_from_authority_pack():
    """提示词块名保持中文；风格块正文来自权威 Omni 包（不再来自旧画风库卡片）。"""
    req = _request()
    for block in PROMPT_BLOCKS:
        assert re.search(rf"^【{block}】$", req.prompt, re.M), block
    assert "纯三维" in req.prompt or "pure 3D" in req.prompt  # 权威包生成公式的介质基线
    assert "[BASE_STYLE]" in req.prompt, "权威包生成公式必须进入风格块"
    assert "【风格锁定】" not in req.prompt, "旧画风库卡片的锁定块不得再出现"
    # 机器字段允许英文
    assert req.output_mime == "image/png"
    assert req.style_version == "1.0.0"


def test_negative_prompt_collected_separately():
    req = _request()
    assert req.negative_prompt.strip()
    assert "无" in req.negative_prompt


def test_reference_roles_declared_in_prompt():
    req = _request()
    assert "references/face.png" in req.prompt
    assert "身份" in req.prompt
    assert "材质" in req.prompt


def test_materials_only_declared_ones():
    req = _request(task=_task(materials=("头发",)))
    assert "头发" in req.prompt
    assert "皮肤" not in req.prompt.split("【禁止项】")[0].split("【材质】")[-1]


# --- 跨引擎一致性 ----------------------------------------------------------------

def test_profiles_differ_only_in_order():
    gem = _request("gemini")
    cod = _request("codex")
    assert gem.prompt != cod.prompt, "两个 profile 允许调整表达顺序"
    assert gem.negative_prompt == cod.negative_prompt
    assert gem.aspect_ratio == cod.aspect_ratio == "1:1"
    assert gem.style_version == cod.style_version
    for block in PROMPT_BLOCKS:
        assert f"【{block}】" in gem.prompt and f"【{block}】" in cod.prompt


def test_profiles_share_identical_fact_set():
    from scripts.engine.image_request_builder import prompt_fact_set

    gem = prompt_fact_set(_request("gemini").prompt)
    cod = prompt_fact_set(_request("codex").prompt)
    assert gem == cod


def test_unknown_profile_rejected():
    with pytest.raises(ValueError) as ctx:
        _request("midjourney")
    assert "profile" in str(ctx.value) or "PROFILE" in str(ctx.value)


# --- 发送清单 -------------------------------------------------------------------

def test_send_manifest_records_relative_paths_roles_hashes():
    manifest = build_send_manifest(_request())
    assert manifest["task_id"] == "CH_林姑娘-正脸-v01"
    assert manifest["aspect_ratio"] == "1:1"
    refs = manifest["references"]
    assert refs and all(r["role"] for r in refs)
    assert refs[0]["sha256"] == "a" * 64
    assert all(not Path(r["path"]).is_absolute() for r in refs)


def test_send_manifest_has_no_secrets_or_prompts():
    manifest = build_send_manifest(_request())
    text = str(manifest)
    for key in ("api_key", "API_KEY", "Authorization", "Bearer"):
        assert key not in text


def test_send_manifest_counts_prompt_size():
    manifest = build_send_manifest(_request())
    assert manifest["prompt_chars"] > 0
    assert manifest["prompt_chars"] < 6000
    assert manifest["reference_count"] == len(REFS)


# --- 工单 5：美术总则冲突门与纯白背景固定 --------------------------------------

def test_person_base_asset_locks_pure_white_background():
    """人物基础资产编译后必须含纯白 #FFFFFF 固定背景，风格包底纹不得覆盖。"""
    req = _request(task=_task(kind="正脸"))
    assert "纯白" in req.prompt and "#FFFFFF" in req.prompt
    assert "素灰背景" not in req.prompt and "摄影棚背景" not in req.prompt


def test_scene_asset_is_not_forced_pure_white():
    """场景资产不锁纯白：环境背景不被强行剥离。"""
    from scripts.engine.art_constitution import requires_pure_white

    assert not requires_pure_white("场景母版")


def test_conflicting_request_is_blocked_before_generation(monkeypatch):
    """编译阶段命中美术冲突时，必须以 ART_CONFLICT 阻断，不得放行生图。"""
    from scripts.engine import image_request_builder as irb
    from scripts.engine.image_request_builder import RequestBuildError
    from scripts.engine.art_constitution import ArtConflictError, Conflict

    def _boom(_prompt):
        raise ArtConflictError([Conflict("写实/卡通", "写实", "卡通", "风格", "构图")])

    monkeypatch.setattr(irb, "check_prompt_conflicts", _boom)
    with pytest.raises(RequestBuildError) as ctx:
        _request()
    assert ctx.value.code == "ART_CONFLICT"
