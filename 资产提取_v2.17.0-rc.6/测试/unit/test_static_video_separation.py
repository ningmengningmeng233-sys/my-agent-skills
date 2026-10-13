#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单02-Task3：静态图片规则与视频规则彻底分离（规格 2.10 / ADR-0085）。

- 仙侠国风电影CG 的静态上下文不得出现旧外壳的冲突表述，必须含母版语义。
- E1 的运镜/时长/节奏约束必须迁出到 references/video_styles/，静态解析器不读取该目录。
"""
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "engine"))

from engine.style import (  # noqa: E402
    STATIC_VIDEO_LEAK,
    StaticStyleError,
    VIDEO_STYLE_DIR,
    build_static_style_context,
    find_video_leaks,
    parse_style_card,
    render_lock_sentences,
)
from engine.style_registry import resolve_style_record  # noqa: E402

VIDEO_CONFIG = REPO_ROOT / "references" / "video_styles" / "电影感写实视频.toml"

# 旧 H7 外壳中与母版冲突的表述：一个都不许留在静态上下文里
CONFLICT_PHRASES = (
    "斗罗", "玄机", "瓷感", "瓷亮", "柔滑瓷", "高饱和", "三格", "三宫格",
    "每4秒", "每4s", "运镜", "镜面皮肤", "强烈金属高光", "强制华丽",
)
# 母版语义：必须出现在静态上下文里
MASTER_PHRASES = ("干燥哑光至柔缎", "中间调", "成束", "珍珠", "水墨", "蓝灰")
SUBJECT_TYPES = ("人物", "场景", "道具", "服装")


def _card():
    return parse_style_card("H7")


def test_h7_card_name_is_semantic():
    card = _card()
    assert card["name"] == "仙侠国风电影CG"


def test_static_card_layers_have_no_conflict_phrases():
    card = _card()
    text = str(card["zh"]) + "".join(str(x) for x in card.get("layers") or []) + str(card["negative"])
    hits = [p for p in CONFLICT_PHRASES if p in text]
    assert hits == [], f"静态外壳仍含旧冲突表述: {hits}"


def test_static_card_keeps_master_semantics():
    card = _card()
    text = str(card["zh"]) + "".join(str(x) for x in card.get("layers") or [])
    missing = [p for p in MASTER_PHRASES if p not in text]
    assert missing == [], f"静态外壳缺少母版语义: {missing}"


def test_static_context_is_clean_for_every_subject_type():
    record = resolve_style_record("仙侠国风电影CG")
    for subject in SUBJECT_TYPES:
        context = build_static_style_context(record, subject, ["皮肤", "头发", "布料"])
        assert context.strip(), subject
        assert find_video_leaks(context) == [], subject
        hits = [p for p in CONFLICT_PHRASES if p in context]
        assert hits == [], f"{subject} 上下文含冲突表述: {hits}"


def test_static_context_contains_master_semantics():
    record = resolve_style_record("仙侠国风电影CG")
    context = build_static_style_context(record, "人物", ["皮肤", "头发"])
    for phrase in MASTER_PHRASES:
        assert phrase in context, phrase


def test_static_context_only_includes_declared_materials():
    """材质定性块只写调用方声明的材质；禁用项属独立段落，不参与材质注入判定。"""
    record = resolve_style_record("仙侠国风电影CG")
    context = build_static_style_context(record, "道具", ["金属"])
    assert "【材质定性（仅画面中真实存在的材质）】" in context
    material_block = context.split("【材质定性（仅画面中真实存在的材质）】", 1)[1]
    material_block = material_block.split("【禁用项】", 1)[0]
    assert "金属" in material_block
    assert "植被" not in material_block, "未声明的材质不得注入"
    assert "皮肤" not in material_block, "道具上下文不得注入皮肤材质"


def test_static_context_requires_record():
    with pytest.raises(StaticStyleError) as ctx:
        build_static_style_context(None, "人物", [])
    assert ctx.value.code == "STYLE_RECORD_REQUIRED"


def test_video_leak_guard_is_armed():
    """守卫必须真的会拦：把视频词塞进静态文本即报 STATIC_VIDEO_LEAK。"""
    assert find_video_leaks("镜头每4秒推近一次") != []
    assert find_video_leaks("静态构图基线，无时间维度") == []

    import engine.style as style_mod

    original = style_mod.render_lock_sentences
    style_mod.render_lock_sentences = lambda card: ["1. 建模层：本卡规定每 4 秒运镜一次"]
    try:
        with pytest.raises(StaticStyleError) as ctx:
            build_static_style_context(resolve_style_record("仙侠国风电影CG"), "人物", [])
        assert ctx.value.code == STATIC_VIDEO_LEAK
    finally:
        style_mod.render_lock_sentences = original


def test_video_config_exists_and_is_complete():
    from engine.common import toml_module

    assert VIDEO_CONFIG.is_file(), f"缺少视频配置 {VIDEO_CONFIG}"
    data = toml_module().loads(VIDEO_CONFIG.read_text(encoding="utf-8"))
    assert data["purpose"] == "video"
    assert len(data["hooks"]) == data["hook_count"] == 12
    assert len(data["camera_language"]) == data["camera_language_count"] == 15
    assert len(data["lighting"]) == data["lighting_count"] == 10
    assert len(data["color_grade"]) == data["color_grade_count"] == 5
    assert data["rhythm"]["min_moves_per_4s"] >= 1
    assert any("每 4 秒" in item for item in data["negative"]["items"])


def test_video_config_declares_static_isolation():
    data = VIDEO_CONFIG.read_text(encoding="utf-8")
    assert "不读取" in data or "不得注入" in data


def test_e1_card_has_no_video_rules():
    card = parse_style_card("E1")
    text = str(card["zh"]) + "".join(str(x) for x in card.get("layers") or []) + str(card["negative"])
    assert find_video_leaks(text) == [], text[-200:]
    assert "钩子" not in text
    assert "镜头语言" not in text
    assert "调色 5 种" not in text


def test_e1_card_keeps_static_baseline():
    card = parse_style_card("E1")
    text = str(card["zh"])
    assert "方向光四十五度" in text
    assert len(card.get("layers") or []) == 4
    assert str(card["negative"]).strip()


def test_static_resolver_never_reads_video_dir(monkeypatch):
    """静态解析路径触碰 references/video_styles/ 即失败（规格 2.10）。"""
    real_is_file = Path.is_file
    real_read_text = Path.read_text

    def guard(path: Path):
        try:
            path.resolve().relative_to(VIDEO_STYLE_DIR.resolve())
        except ValueError:
            return
        raise AssertionError(f"静态解析器不得读取视频目录: {path}")

    def is_file(self):
        guard(self)
        return real_is_file(self)

    def read_text(self, *a, **kw):
        guard(self)
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "read_text", read_text)
    for sid in ("H7", "E1", "A1", "A2"):
        card = parse_style_card(sid)
        assert card is not None
        render_lock_sentences(card)
    build_static_style_context(resolve_style_record("仙侠国风电影CG"), "人物", ["皮肤"])


def test_style_module_has_no_video_dir_reader():
    source = (REPO_ROOT / "scripts" / "engine" / "style.py").read_text(encoding="utf-8")
    # 允许常量声明与守卫文案，禁止任何读取调用
    assert "VIDEO_STYLE_DIR" in source
    for pattern in (r"VIDEO_STYLE_DIR\s*\.\s*glob", r"VIDEO_STYLE_DIR\s*/", r"read_text\(.*VIDEO_STYLE_DIR"):
        assert re.search(pattern, source) is None, pattern


def test_genre_mapping_uses_chinese_name():
    from engine.style import load_style_map, resolve_style

    mapping = load_style_map()
    assert mapping.get("玄幻") == "仙侠国风电影CG"
    assert mapping.get("仙侠") == "仙侠国风电影CG"
    card, mode = resolve_style("玄幻")
    assert mode == "card"
    assert card["id"] == "H7"
    assert card["name"] == "仙侠国风电影CG"


def test_legacy_h7_input_still_resolves():
    from engine.style import resolve_style

    for value in ("H7", "仙侠CG", "斗罗风"):
        card, mode = resolve_style("玄幻", value)
        assert mode == "card", value
        assert card["id"] == "H7", value
        assert card["name"] == "仙侠国风电影CG", value
