#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词中间表示（工单 6，设计方案 §7）。

把「资产事实 + 项目美术总则」确定性组装成固定九分区的 IR，供编译器渲染；模型只在
上游补足「事实无法决定的审美候选与歧义裁决」，**格式化、合并、负面词、基础/变体继承**
全部由代码完成。分区顺序固定，两次构建逐字节一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .art_constitution import resolve_with_sources

SECTION_ORDER: tuple[str, ...] = (
    "身份与主体", "原文事实", "造型与材质", "构图与视角", "背景",
    "光照", "风格", "禁止项", "证据元数据",
)

# 独占槽位：同一时刻只能有一个确定值，按 RULE_PRECEDENCE 由上游独占、下游不得改写。
EXCLUSIVE_SECTIONS: tuple[str, ...] = (
    "身份与主体", "原文事实", "构图与视角", "背景", "光照", "证据元数据",
)
# 复合槽位：允许多层共同贡献（美术总则给材质/光照基调、主风格包给画风卡、硬规则追加禁止项），
# 因此不做槽位覆盖；层间相斥仍由冲突门报错，不静默吞掉。
COMPOSITE_SECTIONS: tuple[str, ...] = ("造型与材质", "风格", "禁止项")

# 基础（资产展示）提示词不得出现的叙事元素——动作/站位/镜头关系只属于叙事概念图。
BASE_FORBIDDEN_HINTS: tuple[str, ...] = (
    "站位", "镜头", "机位", "景别", "对峙", "动作路线", "视线", "行走路线",
)

# 破损类设定必须有原文证据，否则视为「无证据破损」。
DAMAGE_WORDS: tuple[str, ...] = (
    "破损", "破旧", "褴褛", "破烂", "残破", "污渍", "污秽", "赤足", "赤脚",
    "血迹", "血污", "湿透", "补丁",
)

# Omni 人物视图规格：核心四张、重要配角两张起、普通/群演一张；无侧面图；每视图独立任务。
OMNI_PERSON_VIEWS: Mapping[str, tuple[str, ...]] = {
    "P0": ("正脸特写", "后脑与发型", "全身正面", "全身背面"),
    "P1": ("正脸特写", "全身正面"),
    "P2": ("正脸特写",),
    "P3": ("正脸特写",),
}

PURE_WHITE_TYPES = ("人物", "服装")
_UNSET = "未说明"


def omni_person_views(priority: str) -> tuple[str, ...]:
    return OMNI_PERSON_VIEWS.get((priority or "").strip().upper(), ("正脸特写",))


def _views_for(asset_type: str, priority: str, variant: str | None) -> tuple[str, ...]:
    if asset_type == "人物":
        return omni_person_views(priority)
    if asset_type == "服装":
        return ("服装独立正面", "服装独立背面")
    if asset_type == "场景":
        return ("场景母版",)
    if asset_type == "道具":
        return ("道具",)
    return (_UNSET,)


def _clean(value: object) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class PromptIR:
    """确定性提示词中间表示。`sections` 只允许 SECTION_ORDER 中的键。"""

    asset_id: str
    name: str
    asset_type: str
    is_narrative: bool
    variant: str | None
    sections: Mapping[str, str]
    views: tuple[str, ...]
    rule_sources: Mapping[str, str] = field(default_factory=dict)
    rulings: tuple[str, ...] = ()

    def ordered_sections(self) -> list[tuple[str, str]]:
        return [(key, self.sections.get(key, _UNSET)) for key in SECTION_ORDER]


def _evidence_text(facts: Sequence | None, asset: Mapping) -> str:
    """原文事实：facts 为 None 时回落到 asset 的 evidence，显式空列表表示无事实。"""
    items = asset.get("evidence", []) if facts is None else list(facts or [])
    quotes: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            q = _clean(item.get("quote"))
        else:
            q = _clean(item)
        if q and q not in quotes:
            quotes.append(q)
    return "\n".join(quotes) if quotes else _UNSET


def _view_spec(asset_type: str, priority: str) -> str:
    """Omni 人物/服装视图规格：每视图独立成图，无侧面。"""
    if asset_type == "人物":
        return "视图：" + "、".join(omni_person_views(priority)) + "；固定平视，非侧面"
    if asset_type == "服装":
        return "视图：服装独立正面、服装独立背面；平视，非侧面"
    return ""


def _template_composition(asset_type: str) -> str:
    if asset_type == "场景":
        return "全景母版，前景/中景/后景分层来自原文事实"
    if asset_type == "道具":
        return "单件独立展示，轮廓与材质身份清晰"
    return ""


def build_prompt_ir(asset: Mapping, facts: Sequence | None, art_constitution,
                    *, variant: str | None = None, narrative: bool = False) -> PromptIR:
    """按 `RULE_PRECEDENCE` 把资产事实与美术总则确定性组装成九分区 IR。

    柠檬裁决：独占槽位做按秩覆盖（上游先占、下游不得改写）；复合槽位（造型与材质、风格、禁止项）
    不做覆盖，因为美术总则与主风格包本就各供其维度，层间相斥交给冲突门报错而非静默消解。
    """
    name = _clean(asset.get("name"))
    asset_type = _clean(asset.get("type"))
    priority = _clean(asset.get("priority"))
    aid = _clean(asset.get("asset_id"))
    evidence = _evidence_text(facts, asset)
    c = art_constitution

    subject = f"{name}｜{asset_type}" + (f"｜变体：{variant}" if variant else "")
    meta = (f"编号：{aid}｜稳定键：{_clean(asset.get('stable_key'))}｜"
            f"事实等级：{_clean(asset.get('fact_level'))}｜优先级：{priority}")

    omni_slots: dict[str, str] = {}
    if asset_type in PURE_WHITE_TYPES:
        omni_slots["背景"] = "纯白 #FFFFFF 背景，非实景、非棚拍环境"
    if not narrative:
        spec = _view_spec(asset_type, priority)
        if spec:
            omni_slots["构图与视角"] = spec

    fact_slots: dict[str, str] = {"身份与主体": subject, "原文事实": evidence}
    if narrative:
        fact_slots["构图与视角"] = "站位与镜头关系（来自分镜/剧本）：" + (
            evidence if evidence != _UNSET else _UNSET)

    constitution_slots: dict[str, str] = {}
    if c:
        constitution_slots["光照"] = c.lighting
        constitution_slots["背景"] = c.scene_aesthetic   # 项目环境基调，人物/服装由 Omni 纯白覆盖

    template_slots: dict[str, str] = {}
    if not narrative:
        composition = _template_composition(asset_type)
        if composition:
            template_slots["构图与视角"] = composition

    resolved, owners, rulings = resolve_with_sources([
        ("Omni标准", omni_slots),
        ("原文事实", fact_slots),
        ("项目美术总则", constitution_slots),
        ("类型模板", template_slots),
        ("必要补全", {"证据元数据": meta}),
    ])

    # 复合槽位：多层共同贡献
    notes = _clean(asset.get("notes"))
    styling_bits = [
        c.materials if c else "",
        (c.costume_aesthetic if c else "") if asset_type == "服装" else "",
        notes,
    ]
    style_bits = []
    if c:
        style_bits += [c.medium, c.realism, c.palette]
    forced = _clean(asset.get("forced_style_words"))
    if forced:
        style_bits.append(forced)
    forbidden_bits = list(c.forbidden) if c and c.forbidden else []
    forbidden_bits.append("无未经原文证据的破损/污渍/赤足设定")

    sections = {key: resolved.get(key, _UNSET) for key in SECTION_ORDER}
    sections["造型与材质"] = "；".join(b for b in styling_bits if b) or _UNSET
    sections["风格"] = "；".join(b for b in style_bits if b) or _UNSET
    sections["禁止项"] = "；".join(forbidden_bits)

    rule_sources = {key: owners.get(key, "必要补全") for key in SECTION_ORDER}
    rule_sources["造型与材质"] = "项目美术总则" if c else "必要补全"
    rule_sources["风格"] = "唯一主风格包" if c else "必要补全"
    rule_sources["禁止项"] = "项目美术总则" if c else "必要补全"

    return PromptIR(
        asset_id=aid, name=name, asset_type=asset_type, is_narrative=narrative,
        variant=variant, sections=sections, views=_views_for(asset_type, priority, variant),
        rule_sources=rule_sources, rulings=tuple(rulings),
    )
