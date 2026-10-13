#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""段级备注结构化标签解析（ADR-0084 工单C：原文明确空间事实）。

从 aggregate.py 抽出为独立引擎模块，供聚合与测试复用：
- 保持旧三张表和列数不变
- 只记录原文明确内容；未知不填，不推断房间互通
- v2.15.0：子类标签归一到中文枚举；视觉需求绑定资产（规格 2.1，不新建第五类资产）
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from engine.asset_schema import subtype_with_raw


def parse_metadata_tags_from_remark(remark: str, asset_type: str = "") -> Dict[str, Any]:
    """从段级提取表格的备注列解析结构化扩展标记 [标签:值]。

    asset_type 给出时（调用方知道该行属于哪类资产），[子类:…] 会归一到中文枚举；
    归入「其他」的值同时保留 subtype_raw，避免丢掉原文用词。
    """
    if not remark:
        return {}
    res: Dict[str, Any] = {}
    m_sub = re.search(r"\[(?:子类|subtype)[:=]([^\]]+)\]", remark)
    if m_sub:
        raw = m_sub.group(1).strip()
        if asset_type:
            rec = subtype_with_raw(asset_type, raw)
            res["subtype"] = rec["subtype"]
            if rec["subtype_raw"]:
                res["subtype_raw"] = rec["subtype_raw"]
        else:
            res["subtype"] = raw
    m_fn = re.search(r"\[(?:功能|story_function)[:=]([^\]]+)\]", remark)
    if m_fn:
        res["story_function"] = m_fn.group(1).strip()
    m_st = re.search(r"\[(?:状态|state)[:=]([^\]]+)\]", remark)
    if m_st:
        res["state"] = m_st.group(1).strip()
    rel_matches = re.findall(r"\[(?:关系|relation)[:=]([A-Za-z_]+)[=:]([^\]]+)\]", remark)
    if rel_matches:
        relations = []
        for kind, target in rel_matches:
            relations.append({"kind": kind.strip(), "target": target.strip()})
        res["relations"] = relations
    # 视觉需求（规格 2.1）：只解析种类与取值，绑定资产与证据由调用方按本行补全
    vis_matches = re.findall(r"\[(?:视觉需求|视觉|visual)\s*[:=]\s*([^=\]]+)[=:]([^\]]+)\]", remark)
    if vis_matches:
        needs: List[Dict[str, str]] = []
        for kind, value in vis_matches:
            k, v = kind.strip(), value.strip()
            if k and v:
                needs.append({"kind": k, "value": v})
        if needs:
            res["visual_needs"] = needs
    # 空间标签（只记录原文明确内容；未知不填，不推断）
    for key, field in (("空间结构", "space_structure"), ("固定锚点", "anchor"),
                       ("入口出口", "entry_exit"), ("光源", "lighting"), ("材质", "material"),
                       ("主要物件", "objects"), ("朝向", "orientation"), ("时间", "time")):
        m = re.search(rf"\[(?:{key})[:=]([^\]]+)\]", remark)
        if m and m.group(1).strip():
            res[field] = m.group(1).strip()
    m_conn = re.search(r"\[(?:连接点|connects_to)[:=]([^\]]+)\]", remark)
    if m_conn:
        targets: List[str] = [t.strip() for t in re.split(r"[、/，,]", m_conn.group(1)) if t.strip()]
        if targets:
            res["connects"] = targets
    return res
