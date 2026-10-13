# -*- coding: utf-8 -*-
"""唯一类型化资产总账（工单 2）。

在旧四表与 .process/资产索引.json 之上建立机器层总账：五类记录（人物/场景/道具/服装/后期元素），
每条带 ledger_status / narrative_importance / production_mode / required_outputs，
所有旧 Markdown 表可由总账渲染，旧 ID 与 stable_key 原样保留、绝不重编号。

后期元素独立成交付记录，production_mode=post_only，永不进入生图计划。
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, TypedDict

SCHEMA_VERSION = 1

TYPE_KEY = {"人物": "person", "场景": "scene", "道具": "prop", "服装": "costume"}
# 反向映射只在**这一处**派生：总账存英文键，展示与判据侧要中文类型
KEY_TO_TYPE = {v: k for k, v in TYPE_KEY.items()}
KEY_TO_LEGACY = {
    "person": ("人物资产提取表", "CH"),
    "scene": ("场景资产提取表", "SC"),
    "prop": ("道具资产提取表", "PR"),
    "costume": ("服装妆造资产提取表", "CO"),
}
PRIORITY_TO_IMPORTANCE = {
    "P0": "main", "P1": "supporting", "P2": "minor", "P3": "minor",
}


class AssetLedgerEntry(TypedDict, total=False):
    asset_id: str
    stable_key: str
    asset_type: str
    canonical_name: str
    aliases: List[str]
    evidence_ids: List[str]
    fact_level: str
    ledger_status: str
    narrative_importance: str
    production_mode: str
    required_outputs: List[str]
    relations: List[dict]
    source_refs: List[dict]
    prompt_ids: List[str]
    visual_task_ids: List[str]
    # 旧四表展示字段（工单 2 L139b）：四表由总账渲染时逐字还原，不丢列
    positions: str
    importance_level: str
    remark: str
    alias_display: str
    bind_display: str
    display_order: int


def _evidence_ids(asset: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for ev in asset.get("evidence", []) or []:
        quote = (ev.get("quote") or "").strip()
        line = str(ev.get("line") or "")
        if quote:
            out.append(hashlib.sha1(f"{line}|{quote}".encode("utf-8")).hexdigest()[:12])
    return out


def build_ledger(index: Dict[str, Any],
                 post_elements: Optional[List[Dict[str, str]]] = None) -> List[AssetLedgerEntry]:
    """由资产索引 + 后期元素事实构建总账；未知类型跳过但保留四类与后期元素。"""
    ledger: List[AssetLedgerEntry] = []
    for asset in index.get("assets", []):
        key = TYPE_KEY.get((asset.get("type") or "").strip())
        if key is None:
            continue
        fact_level = asset.get("fact_level", "")
        status = asset.get("status", "CONFIRMED")
        formal = status != "PENDING" and fact_level != "待定冲突"
        relations = list(asset.get("relations", []) or [])
        if key == "costume":
            # 普通单套服装绑定人物（随人物呈现，不独立要求基础提示词）；无绑定的关键服装独立生产
            bound = any(r.get("kind") == "WEARS" for r in relations)
            if bound:
                required, mode = ["bound_image"], "bound"
            else:
                required, mode = (["base_prompt", "image_task"] if formal else ["base_prompt"]), "image"
        elif key in ("person", "scene", "prop"):
            required = ["base_prompt", "image_task"] if formal else ["base_prompt"]
            mode = "image"
        else:  # pragma: no cover
            required, mode = [], "none"
        entry: AssetLedgerEntry = {
            "asset_id": asset.get("asset_id", ""),
            "stable_key": asset.get("stable_key", ""),
            "asset_type": key,
            "canonical_name": asset.get("name", ""),
            "aliases": list(asset.get("aliases", []) or []),
            "evidence_ids": _evidence_ids(asset),
            "fact_level": fact_level,
            "ledger_status": "formal" if formal else "pending",
            "narrative_importance": PRIORITY_TO_IMPORTANCE.get(asset.get("priority", "P2"), "minor"),
            "production_mode": mode,
            "required_outputs": required,
            "relations": relations,
            "source_refs": list(asset.get("source_refs", []) or []),
            "prompt_ids": [],
            "visual_task_ids": [],
            "positions": asset.get("positions", ""),
            "importance_level": asset.get("importance_level", ""),
            "remark": asset.get("remark", ""),
            "alias_display": asset.get("alias_display", ""),
            "bind_display": asset.get("bind_display", ""),
            "display_order": asset.get("display_order", 0),
        }
        if not entry["source_refs"]:
            if asset.get("is_manual"):
                entry["source_refs"] = [{
                    "source_type": "manual",
                    "source_id": "MANUAL",
                    "quote": "手工创建资产",
                }]
            else:
                entry["source_refs"] = [{
                    "source_type": "novel",
                    "source_id": asset.get("stable_key", "ORIGIN"),
                    "quote": asset.get("positions", "") or asset.get("name", ""),
                }]
        ledger.append(entry)

    for pe in post_elements or []:
        name = (pe.get("name") or pe.get("quote") or "").strip()
        if not name:
            continue
        digest = hashlib.sha1(f"post|{name}".encode("utf-8")).hexdigest()[:8]
        ledger.append({
            "asset_id": f"POST_{digest}",
            "stable_key": f"POST:{digest}",
            "asset_type": "post_element",
            "canonical_name": name,
            "aliases": [],
            "evidence_ids": _evidence_ids({"evidence": [{"quote": pe.get("quote", name), "line": ""}]}),
            "fact_level": "剧本事实",
            "ledger_status": "post",
            "narrative_importance": "minor",
            "production_mode": "post_only",
            "required_outputs": ["post_delivery"],
            "relations": [],
            "source_refs": [{
                "source_type": "manual",
                "source_id": "MANUAL",
                "quote": pe.get("quote", name),
            }],
            "prompt_ids": [],
            "visual_task_ids": [],
        })
    return ledger


LEGACY_HEADERS: Dict[str, str] = {
    "person": "| 资产ID | 人物姓名 | 别名/称呼 | 出现位置 | 重要等级 | 备注 |\n|---|---|---|---|---|---|",
    "scene": "| 资产ID | 场景名称 | 出现位置 | 重要等级 | 备注 |\n|---|---|---|---|---|",
    "prop": "| 资产ID | 道具名称 | 出现位置 | 重要等级 | 备注 |\n|---|---|---|---|---|",
    "costume": "| 资产ID | 造型名称 | 绑定人物 | 出现位置 | 重要等级 | 备注 |\n|---|---|---|---|---|---|",
}


def _legacy_row(key: str, entry: AssetLedgerEntry) -> str:
    """按列渲染单行。列口径与旧渲染路径逐字一致，避免「从总账渲染」丢列或偷偷改口径。"""
    aid = entry.get("asset_id", "")
    name = entry.get("canonical_name", "")
    pos = entry.get("positions", "")
    level = entry.get("importance_level", "")
    remark = entry.get("remark", "")
    if key == "person":
        return f"| {aid} | {name} | {entry.get('alias_display', '')} | {pos} | {level} | {remark} |"
    if key == "costume":
        return f"| {aid} | {name} | {entry.get('bind_display', '')} | {pos} | {level} | {remark} |"
    return f"| {aid} | {name} | {pos} | {level} | {remark} |"


def render_legacy_tables(ledger: List[AssetLedgerEntry], only: Optional[str] = None) -> str:
    """把总账渲染回旧四表 Markdown（表头与列结构与 v2.16 一致）。

    `only` 给出类型键（person/scene/prop/costume）时只渲染该表，供 aggregate.py 逐表取用；
    缺省渲染全部四表。**行集完全来自总账**，调用方不再各自维护名单。
    """
    groups: Dict[str, List[AssetLedgerEntry]] = {}
    for e in ledger:
        key = e.get("asset_type", "")
        if key in KEY_TO_LEGACY:
            groups.setdefault(key, []).append(e)
    keys = [only] if only in KEY_TO_LEGACY else ["person", "scene", "prop", "costume"]
    blocks: List[str] = []
    for key in keys:
        rows = [f"## {KEY_TO_LEGACY[key][0]}", LEGACY_HEADERS[key]]
        for e in sorted(groups.get(key, []),
                        key=lambda a: (a.get("display_order", 0), a.get("asset_id", ""))):
            rows.append(_legacy_row(key, e))
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks) + "\n"


def write_ledger(lib, ledger: List[AssetLedgerEntry]) -> str:
    """原子落盘 .process/asset-ledger.json（UTF-8 + schema_version + 稳定顺序）。"""
    proc = os.path.join(str(lib), ".process")
    os.makedirs(proc, exist_ok=True)
    path = os.path.join(proc, "asset-ledger.json")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "entries": sorted(ledger, key=lambda e: (e["asset_type"], e["asset_id"])),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def load_ledger(lib) -> List[AssetLedgerEntry]:
    path = os.path.join(str(lib), ".process", "asset-ledger.json")
    if not os.path.isfile(path):
        return []
    data = json.loads(open(path, encoding="utf-8").read())
    return data.get("entries", [])
