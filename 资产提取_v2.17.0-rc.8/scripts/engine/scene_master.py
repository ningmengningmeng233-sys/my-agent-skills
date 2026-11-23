# -*- coding: utf-8 -*-
"""场景母版的空间模型与必要视图推导（v2.17.0 工单 7）。

职责边界：本模块只做**推导与判定**（纯函数）；母版文件的读取、人工字段安全合并与落盘
仍由 `continuity.py` 负责，不复制既有实现。

- 空间槽位（空间结构/固定锚点/入口出口/主要物件/朝向/时间/材质与光源）：原文未说明一律写
  「未说明」，严禁虚构；「禁改项」由固定锚点与出入口派生，不是可自由填写的字段。
- 必要视图：基础场景只生成能确立空间的视图，只有剧情证据指向关键方向时才增补，**绝不机械生成侧视图**。
- 场景审美：由美术总则 + 场景功能 + 叙事氛围共同编译；无证据的空泛堆砌词一律剔除并回报。
- 漂移检查：比较结构锚点**集合**，而不是关键词搜索。
- 叙事站位：必须可追溯到原始剧本、资产索引或分镜提示词之一；缺证据进待审，不胡编站位。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence

UNKNOWN = "未说明"

# 母版必须固定的空间槽位（工单 7 步骤 1）；值为 space_facts 的键
SPACE_SLOTS: tuple[tuple[str, str], ...] = (
    ("空间结构", "space_structure"),
    ("固定锚点", "anchor"),
    ("入口出口", "entry_exit"),
    ("主要物件", "objects"),
    ("朝向", "orientation"),
    ("时间", "time"),
    ("光源", "lighting"),
    ("材质", "material"),
)
# 固定锚点 + 入口出口 → 派生「禁改项」（下游不得改写）
IMMUTABLE_SLOT = "禁改项"
_IMMUTABLE_SOURCES = ("anchor", "entry_exit")

# 结构锚点槽位：漂移检查只比较这些槽位的**集合**
STRUCTURAL_SLOTS = ("anchor", "entry_exit", "objects")

BASE_SCENE_VIEW = "场景母版"
REVERSE_SCENE_VIEW = "反向母版"
_SIDE_VIEW_HINTS = ("侧面", "侧视", "侧视图")

# 只有证据指向反向/进出方向时才需要反向母版
_REVERSE_HINTS = ("反向", "背对", "回望", "折返", "出门", "进门", "迎面")

# 无证据时不得出现的空泛堆砌词
VAGUE_AESTHETIC_WORDS = ("宏大", "华丽", "电影感", "史诗", "震撼", "高级感", "氛围感拉满", "大气磅礴")

# 站位/视线/轴线可追溯的三个来源（与 director_composition 的 SOURCE_* 对齐）
BLOCKING_SOURCES = ("原始剧本", "资产索引", "分镜提示词")
BLOCKING_OK = "TRACEABLE"
BLOCKING_PENDING = "AWAITING_SPACE_EVIDENCE"


def _split_values(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for raw in values:
        for part in re.split(r"[、/，,；;]", raw or ""):
            item = part.strip()
            if item and item not in out:
                out.append(item)
    return out


def _fact_values(scene_asset: Mapping[str, Any], field: str) -> List[str]:
    """取某个空间事实的取值列表（按首次出现顺序去重）。"""
    facts = (scene_asset.get("space_facts") or {}).get(field) or []
    return _split_values(str(f.get("value", "")) for f in facts if isinstance(f, Mapping))


def _fact_text(scene_asset: Mapping[str, Any], field: str) -> str:
    """空间事实连同证据落母版：只写有证据的内容，未知写「未说明」。"""
    facts = (scene_asset.get("space_facts") or {}).get(field) or []
    parts: List[str] = []
    for f in facts[:3]:
        if not isinstance(f, Mapping):
            continue
        value = str(f.get("value", "") or "").strip()
        if not value:
            continue
        parts.append(f"{value}（{f.get('chapter', '')}{f.get('segment', '')}行{f.get('line', '')}）")
    return "；".join(parts) or UNKNOWN


def space_slot_values(scene_asset: Mapping[str, Any]) -> Dict[str, str]:
    """母版全部空间槽位取值；原文未说明一律「未说明」，不推断。"""
    return {label: _fact_text(scene_asset, field) for label, field in SPACE_SLOTS}


def immutable_items(scene_asset: Mapping[str, Any]) -> tuple:
    """禁改项：由固定锚点与出入口派生，下游层不得改写。"""
    items: List[str] = []
    for field in _IMMUTABLE_SOURCES:
        for value in _fact_values(scene_asset, field):
            if value not in items:
                items.append(value)
    return tuple(items)


def immutable_text(scene_asset: Mapping[str, Any]) -> str:
    items = immutable_items(scene_asset)
    return "、".join(items) if items else UNKNOWN


def evidence_text(scene_asset: Mapping[str, Any]) -> str:
    """场景全部证据摘句 + 已确认空间事实的拼接，供方向判定与审美证据校验使用（去重、保持首现顺序）。"""
    parts: List[str] = []
    for e in scene_asset.get("evidence", []) or []:
        if isinstance(e, Mapping):
            quote = str(e.get("quote", "") or "").strip()
        else:
            quote = str(e or "").strip()
        if quote and quote not in parts:
            parts.append(quote)
    for label, field in SPACE_SLOTS:
        for value in _fact_values(scene_asset, field):
            if value not in parts:
                parts.append(value)
    return "；".join(parts)


def required_scene_views(scene_asset: Mapping[str, Any]) -> tuple:
    """基础场景的必要视图：只生成能确立空间的视图，缺关键方向才增补，永不机械生成侧视图。"""
    text = evidence_text(scene_asset)
    views = [BASE_SCENE_VIEW]
    if any(h in text for h in _REVERSE_HINTS):
        views.append(REVERSE_SCENE_VIEW)
    return tuple(v for v in views if not any(s in v for s in _SIDE_VIEW_HINTS))


def unsupported_vague_words(text: str, evidence: str) -> tuple:
    """无证据支撑的空泛堆砌词（有原文出处的不算堆砌）。"""
    haystack = evidence or ""
    return tuple(w for w in VAGUE_AESTHETIC_WORDS if w in (text or "") and w not in haystack)


def compile_scene_aesthetic(constitution, scene_asset: Mapping[str, Any]) -> tuple:
    """场景审美 = 美术总则场景审美 + 场景功能 + 叙事氛围；返回（文本, 被剔除的空泛词）。

    叙事氛围只取**已确认的空间事实**（时间/光源），不倒原文进审美正文；
    美术总则缺位时不臆造审美，只保留有证据支撑的部分。
    """
    evidence = evidence_text(scene_asset)
    bits: List[str] = []
    if constitution is not None:
        for value in (getattr(constitution, "scene_aesthetic", ""), getattr(constitution, "palette", ""),
                      getattr(constitution, "lighting", "")):
            if value and value not in bits:
                bits.append(value)
    story_function = str(scene_asset.get("story_function", "") or "").strip()
    if story_function and story_function not in bits:
        bits.append(story_function)
    for field in ("time", "lighting"):
        fact = _fact_text(scene_asset, field)
        if fact != UNKNOWN and fact not in bits:
            bits.append(fact)

    removed = unsupported_vague_words("；".join(bits), evidence)
    kept = [b for b in bits if not unsupported_vague_words(b, evidence)]
    return ("；".join(kept) or UNKNOWN, removed)


def compare_anchors(old_slots: Mapping[str, str], new_slots: Mapping[str, str]) -> List[str]:
    """漂移检查：比较结构锚点**集合**（固定锚点/入口出口/主要物件），不是关键词搜索。

    返回人类可读的漂移说明；空列表表示结构自洽。槽位任一为「未说明」时跳过该槽位。
    """
    drifts: List[str] = []
    for label, field in SPACE_SLOTS:
        if field not in STRUCTURAL_SLOTS:
            continue
        old_raw, new_raw = old_slots.get(label), new_slots.get(label)
        if not old_raw or not new_raw or old_raw == UNKNOWN or new_raw == UNKNOWN:
            continue
        old_set, new_set = set(_split_values([old_raw])), set(_split_values([new_raw]))
        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)
        if added or removed:
            detail = []
            if added:
                detail.append("新增 " + "、".join(added))
            if removed:
                detail.append("丢失 " + "、".join(removed))
            drifts.append(f"{label}结构漂移：" + "；".join(detail))
    return drifts


def blocking_status(source: str) -> str:
    """叙事概念图的站位/视线/轴线来源判定：可追溯→TRACEABLE，缺证据→待审。"""
    return BLOCKING_OK if (source or "").strip() in BLOCKING_SOURCES else BLOCKING_PENDING


def scene_master_rows(scene_asset: Mapping[str, Any], constitution=None) -> Sequence[tuple]:
    """母版需要追加的内容行（槽位 + 禁改项 + 必要视图 + 场景审美）。

    供 `continuity.build_scene_master` 直接拼进 Markdown，避免两处各写一份口径。
    """
    rows: List[tuple] = [(label, value) for label, value in space_slot_values(scene_asset).items()]
    rows.append((IMMUTABLE_SLOT, immutable_text(scene_asset)))
    rows.append(("必要视图", "、".join(required_scene_views(scene_asset))))
    aesthetic, removed = compile_scene_aesthetic(constitution, scene_asset)
    rows.append(("场景审美", aesthetic))
    if removed:
        rows.append(("被剔除的空泛词", "、".join(removed)))
    return rows
