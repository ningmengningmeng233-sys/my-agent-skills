#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""场景空间连续性母版构建与校验（ADR-0084 / scripts/engine/continuity.py）

职责：
1. 从资产索引 + 章节摘要构建 <资产库>/连续性/场景母版.md（文本连续性卡）
2. 每场景一节：资产ID、证据、空间结构、固定锚点、入口出口、连接点、材质光源、未知项
3. 原文未写的内容一律填“未说明”，严禁虚构
4. 校验 CONNECTS_TO 双向一致性与母版锚点漂移
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

try:
    from engine.asset_schema import RELATION_MATRIX
    from engine.scene_master import UNKNOWN
except ImportError:  # 以顶层模块方式加载时
    from asset_schema import RELATION_MATRIX
    from scene_master import UNKNOWN

MASTER_FILENAME = "场景母版.md"
MASTER_DIRNAME = "连续性"


def _scene_assets(index: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [a for a in index.get("assets", []) if a.get("type") == "场景"]


def _fact_text(facts: List[Dict[str, Any]]) -> str:
    """空间事实连同证据落母版：只写有证据的结构/锚点，未写保留未说明。"""
    parts = []
    for f in facts[:3]:
        v = (f.get("value", "") or "").strip()
        if not v:
            continue
        parts.append(f"{v}（{f.get('chapter', '')}{f.get('segment', '')}行{f.get('line', '')}）")
    return "；".join(parts) or UNKNOWN


def build_scene_master(index: Dict[str, Any], summaries: List[Dict[str, str]] | None = None,
                       constitution: Any = None) -> str:
    """构建场景母版 Markdown（纯文本连续性卡，不做图片/分镜）。

    空间槽位与必要视图由 `scene_master` 推导；场景审美由美术总则 + 场景功能 + 叙事氛围编译。
    """
    from .scene_master import (IMMUTABLE_SLOT, compile_scene_aesthetic, immutable_text,
                               required_scene_views, space_slot_values)

    summaries = summaries or []
    scenes = sorted(_scene_assets(index), key=lambda a: (a.get("stable_key", ""), a.get("asset_id", "")))
    lines = ["# 场景空间母版（ADR-0084 文本连续性卡）", ""]
    lines.append("> 每场景一节，记录已确认的空间结构与固定锚点。原文未说明项一律填“未说明”，严禁虚构。")
    lines.append("")
    id_set = {a.get("asset_id", "") for a in index.get("assets", [])}
    for sc in scenes:
        asset_id = sc.get("asset_id", "")
        name = sc.get("name", asset_id)
        subtype = sc.get("subtype", "") or UNKNOWN
        story_fn = sc.get("story_function", "") or UNKNOWN
        evidence = sc.get("evidence", []) or []
        ev_text = "；".join(
            f"{e.get('chapter', '')}{e.get('segment', '')}行{e.get('line', '')}「{(e.get('quote', '') or '')[:24]}」"
            for e in evidence[:3]
        ) or UNKNOWN
        # 连接点：仅保留索引中存在的端点
        connects = sorted({
            r.get("target", "") for r in (sc.get("relations", []) or [])
            if r.get("kind") == "CONNECTS_TO" and r.get("target") in id_set and r.get("target") != asset_id
        })
        lines.append(f"## {asset_id} {name}")
        lines.append("| 字段 | 内容 |")
        lines.append("|---|---|")
        lines.append(f"| 资产ID | {asset_id} |")
        lines.append(f"| stable_key | {sc.get('stable_key', UNKNOWN)} |")
        lines.append(f"| 子类 | {subtype} |")
        lines.append(f"| 故事功能 | {story_fn} |")
        lines.append(f"| 事实等级 | {sc.get('fact_level', UNKNOWN)} |")
        lines.append(f"| 证据 | {ev_text} |")
        space = sc.get("space_facts", {}) or {}
        lines.append(f"| 空间结构 | {_fact_text(space.get('space_structure', []))} |")
        lines.append(f"| 固定锚点 | {_fact_text(space.get('anchor', []))} |")
        lines.append(f"| 入口出口 | {_fact_text(space.get('entry_exit', []))} |")
        lines.append(f"| 连接点 | {'、'.join(connects) if connects else UNKNOWN} |")
        lighting = _fact_text(space.get('lighting', []))
        material = _fact_text(space.get('material', []))
        both = "；".join([x for x in (lighting, material) if x != UNKNOWN]) or UNKNOWN
        lines.append(f"| 材质与光源 | {both} |")
        # 工单 7：主要物件/朝向/时间/禁改项/必要视图/场景审美（纯追加，不改动上方既有行）
        slots = space_slot_values(sc)
        for label in ("主要物件", "朝向", "时间"):
            lines.append(f"| {label} | {slots[label]} |")
        aesthetic, removed = compile_scene_aesthetic(constitution, sc)
        lines.append(f"| {IMMUTABLE_SLOT} | {immutable_text(sc)} |")
        lines.append(f"| 必要视图 | {'、'.join(required_scene_views(sc))} |")
        lines.append(f"| 场景审美 | {aesthetic} |")
        if removed:
            lines.append(f"| 被剔除的空泛词 | {'、'.join(removed)} |")
        lines.append("| 未知项 | 未在证据中出现的空间细节一律视为未知，不虚构 |")
        lines.append("")
    if not scenes:
        lines.append("> 暂无场景资产。")
        lines.append("")
    return "\n".join(lines)


def write_scene_master(lib: Path, index: Dict[str, Any], summaries: List[Dict[str, str]] | None = None,
                       constitution: Any = None) -> Path:
    """落盘 <资产库>/连续性/场景母版.md（安全合并：保留人工字段，不静默覆盖）。"""
    out_dir = lib / MASTER_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / MASTER_FILENAME
    fresh = build_scene_master(index, summaries, constitution)
    if not path.is_file():
        path.write_text(fresh, encoding="utf-8")
        return path
    try:
        existing = parse_master_sections(path.read_text(encoding="utf-8"))
    except OSError:
        path.write_text(fresh, encoding="utf-8")
        return path
    fresh_sections = parse_master_sections(fresh)
    merged_lines = ["# 场景空间母版（ADR-0084 文本连续性卡）", "",
                    "> 每场景一节，记录已确认的空间结构与固定锚点。原文未说明项一律填“未说明”，严禁虚构。", ""]
    conflicts: List[str] = []
    for sec_id, fields in fresh_sections.items():
        old = existing.get(sec_id, {})
        for key in ("空间结构", "固定锚点", "入口出口", "材质与光源", "主要物件", "朝向", "时间",
                    "禁改项", "场景审美"):
            new_val = (fields.get(key, "") or "").strip()
            old_val = (old.get(key, "") or "").strip()
            # 人工补充（非未说明）优先保留；新证据冲突时报 NEEDS_REVIEW 由人工裁决
            if old_val and old_val != UNKNOWN and new_val != UNKNOWN and old_val != new_val:
                conflicts.append(f"{sec_id}: {key} 人工值与新证据冲突（保留人工值，需复核）")
                fields[key] = old_val
            elif old_val and old_val != UNKNOWN and new_val == UNKNOWN:
                fields[key] = old_val
        merged_lines.append(f"## {sec_id} {fields.get('__name__', '')}".rstrip())
        merged_lines.append("| 字段 | 内容 |")
        merged_lines.append("|---|---|")
        for key in ("资产ID", "stable_key", "子类", "故事功能", "事实等级", "证据",
                    "空间结构", "固定锚点", "入口出口", "连接点", "材质与光源",
                    "主要物件", "朝向", "时间", "禁改项", "必要视图", "场景审美", "被剔除的空泛词",
                    "未知项"):
            if key in fields:
                merged_lines.append(f"| {key} | {fields[key]} |")
        merged_lines.append("")
    # 新场景追加（fresh 已含全量，此处保持顺序）；已删除场景不复活
    path.write_text("\n".join(merged_lines), encoding="utf-8")
    if conflicts:
        conflict_path = out_dir / "母版冲突待审.md"
        conflict_path.write_text("# 母版冲突待审（NEEDS_REVIEW，保留人工值）\n\n" + "\n".join(f"- {c}" for c in conflicts) + "\n",
                                 encoding="utf-8")
    return path


def check_connects_consistency(index: Dict[str, Any]) -> List[str]:
    """校验 CONNECTS_TO：目标存在 + 目标为场景 + 双向对称。返回错误列表。"""
    errors: List[str] = []
    by_id = {a.get("asset_id", ""): a for a in index.get("assets", []) if isinstance(a, dict)}
    for a in index.get("assets", []):
        for r in (a.get("relations", []) or []):
            if r.get("kind") != "CONNECTS_TO":
                continue
            src, target = a.get("asset_id", ""), r.get("target", "")
            tgt = by_id.get(target)
            if tgt is None:
                errors.append(f"{src}: CONNECTS_TO 目标不存在 '{target}'")
                continue
            if tgt.get("type") != "场景":
                errors.append(f"{src}: CONNECTS_TO 目标必须为场景，'{target}' 为{tgt.get('type')}")
                continue
            back = [x for x in (tgt.get("relations", []) or [])
                    if isinstance(x, dict) and x.get("kind") == "CONNECTS_TO" and x.get("target") == src]
            if not back:
                errors.append(f"{src}: CONNECTS_TO 单向（{target} 缺反向边）")
    return errors


def check_relation_types(index: Dict[str, Any]) -> List[str]:
    """全关系类型矩阵校验：源类型/目标类型/端点存在。"""
    errors: List[str] = []
    by_id = {a.get("asset_id", ""): a for a in index.get("assets", []) if isinstance(a, dict)}
    for a in index.get("assets", []):
        src_type = a.get("type", "")
        for r in (a.get("relations", []) or []):
            if not isinstance(r, dict):
                continue
            kind, target = r.get("kind", ""), r.get("target", "")
            # R4b：STATE_OF 不再跳过类型校验；自环显式报废弃（旧索引须重跑聚合）
            if kind == "STATE_OF" and target == a.get("asset_id"):
                errors.append(f"{a.get('asset_id')}: STATE_OF 自环已废弃"
                              f"（无独立变体节点时用 variant_needs 表达，请重跑聚合）")
            rule = RELATION_MATRIX.get(kind)
            if not rule:
                errors.append(f"{a.get('asset_id')}: 非法关系种类 '{kind}'")
                continue
            if src_type != rule["source"]:
                errors.append(f"{a.get('asset_id')}: {kind} 源必须为{rule['source']}，当前为{src_type}")
            tgt = by_id.get(target)
            if tgt is None:
                errors.append(f"{a.get('asset_id')}: {kind} 目标不存在 '{target}'")
            elif tgt.get("type") != rule["target"]:
                errors.append(f"{a.get('asset_id')}: {kind} 目标必须为{rule['target']}，'{target}' 为{tgt.get('type')}")
    unresolved = index.get("unresolved_relations", []) or []
    for u in unresolved:
        if isinstance(u, dict):
            errors.append(f"{u.get('asset_id')}: 未解析关系 {u.get('kind')}→{u.get('raw')}（{u.get('reason')}）")
    return errors


def parse_master_sections(text: str) -> Dict[str, Dict[str, str]]:
    """解析场景母版各节 → {资产ID: {字段: 内容}}。"""
    sections: Dict[str, Dict[str, str]] = {}
    cur: str | None = None
    for line in text.splitlines():
        m = re.match(r"^##\s+(SC_\S+)", line.strip())
        if m:
            cur = m.group(1).strip()
            sections[cur] = {}
            continue
        if cur and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[0] not in ("字段", "---", "---", "") and not set(cells[0]) <= {"-", " "}:
                if cells[0].startswith("---"):
                    continue
                sections[cur][cells[0]] = cells[1]
    return sections


def check_master_drift(index: Dict[str, Any], master_text: str) -> List[str]:
    """母版漂移检查：连接点 + 已确认固定锚点/入口出口必须与索引一致。

    机械可验证：连接点集合、锚点/入口出口的已确认值（非未说明）被改写即报错；
    无法可靠判断的空间语义保留人工审查，不写“全自动通过”。
    """
    errors: List[str] = []
    sections = parse_master_sections(master_text)
    id_set = {a.get("asset_id", "") for a in index.get("assets", [])}
    for a in index.get("assets", []):
        if a.get("type") != "场景":
            continue
        asset_id = a.get("asset_id", "")
        expected = sorted({
            r.get("target", "") for r in (a.get("relations", []) or [])
            if r.get("kind") == "CONNECTS_TO" and r.get("target") in id_set
        })
        sec = sections.get(asset_id)
        if sec is None:
            errors.append(f"{asset_id}: 场景母版缺失对应小节")
            continue
        raw = (sec.get("连接点", "") or "").strip()
        actual = [] if raw in ("", UNKNOWN) else [p.strip() for p in re.split(r"[、/，,]", raw) if p.strip()]
        if sorted(actual) != expected:
            errors.append(f"{asset_id}: 母版连接点漂移（索引 {expected}，母版 {actual}）")
        # 已确认锚点/入口出口：索引有空间事实但母版被改写/清空即报错
        space = a.get("space_facts", {}) or {}
        for field, mkey in (("anchor", "固定锚点"), ("entry_exit", "入口出口"),
                            ("space_structure", "空间结构"), ("lighting", "材质与光源")):
            facts = space.get(field, []) if isinstance(space.get(field, []), list) else []
            confirmed = sorted({(f.get("value", "") or "").strip() for f in facts if (f.get("value", "") or "").strip()})
            if not confirmed:
                continue
            mraw = (sec.get(mkey, "") or "").strip()
            if mraw in ("", UNKNOWN):
                errors.append(f"{asset_id}: 母版{mkey}缺失已确认值（索引 {confirmed}）")
            elif not any(c in mraw for c in confirmed):
                errors.append(f"{asset_id}: 母版{mkey}被改写（索引 {confirmed}，母版 {mraw[:40]}）")
    return errors


def check_deriv_anchors(master_text: str, deriv_text: str, asset_id: str) -> List[str]:
    """派生正文锚点检查：母版已确认锚点必须在派生提示词中保留（机械子集）。

    未知字段不要求 LLM 虚构；删掉确认锚点或写反方向即报错。
    """
    errors: List[str] = []
    sections = parse_master_sections(master_text)
    sec = sections.get(asset_id)
    if not sec:
        return [f"{asset_id}: 母版缺失，无法验证派生锚点"]
    for mkey in ("固定锚点", "入口出口", "空间结构"):
        raw = (sec.get(mkey, "") or "").strip()
        if not raw or raw == UNKNOWN:
            continue
        anchors = [p.strip() for p in re.split(r"[；;]", raw) if p.strip()]
        for anchor in anchors:
            core = re.sub(r"（[^）]*）$", "", anchor).strip()
            if core and core not in deriv_text:
                errors.append(f"{asset_id}: 派生正文缺母版{mkey}「{core[:20]}」")
    return errors
