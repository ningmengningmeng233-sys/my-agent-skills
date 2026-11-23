# -*- coding: utf-8 -*-
"""实体消歧与状态/部件/别名关系裁决（工单 3 / scripts/engine/entity_resolution.py）。

五种明确裁决：
- ALIAS_OF：候选名精确命中某同类型资产的规范名或别名，且无跨类型冲突 → 高置信合并（唯一允许自动合并）。
- STATE_OF：候选声明为某母资产的状态（如「断臂公子」）→ 指向母资产，写进其变体需求，绝不造自环、不吞并。
- PART_OF：候选声明为某资产的部件 → 指向母资产，不吞并。
- INDEPENDENT：新实体，或仅名字包含但非同一对象 → 独立，绝不因字符串包含而合并。
- AMBIGUOUS：同名命中多个实体或跨类型冲突 → 待人工裁决，不得静默复用 stable_key。

模型/人工裁决必须携带引用的事实 ID（evidence_fact_ids）与理由，供上层落审计。
"""
from __future__ import annotations

from typing import Any, Dict, List, TypedDict

ALIAS_OF = "ALIAS_OF"
STATE_OF = "STATE_OF"
PART_OF = "PART_OF"
INDEPENDENT = "INDEPENDENT"
AMBIGUOUS = "AMBIGUOUS"

# 只有达到该置信度才允许自动合并；当前唯一达标的是精确同类型别名。
AUTO_MERGE_THRESHOLD = 0.9

_CONF_ALIAS = 0.95
_CONF_LINK = 0.60
_CONF_AMBIGUOUS = 0.40


class Resolution(TypedDict):
    decision: str
    target_id: str
    confidence: float
    reason: str
    auto_merge: bool
    evidence_fact_ids: List[str]


def _matches(name: str, item: Dict[str, Any]) -> bool:
    if not name:
        return False
    if name == (item.get("name") or "").strip():
        return True
    return name in [a.strip() for a in item.get("aliases", []) or []]


def _find_link(field_value: str, existing: List[Dict[str, Any]], same_type: bool,
               cand_type: str) -> List[Dict[str, Any]]:
    """按声明字段（state_of / part_of）在既有资产里找规范名或别名命中者。"""
    hits = []
    for item in existing:
        if not _matches(field_value, item):
            continue
        if same_type and (item.get("type") or "").strip() != cand_type:
            continue
        hits.append(item)
    return hits


def resolve_entity(candidate: Dict[str, Any], existing: List[Dict[str, Any]]) -> Resolution:
    """对单个候选实体给出消歧裁决；无法确定一律 AMBIGUOUS，绝不静默任选或吞并。"""
    name = (candidate.get("name") or "").strip()
    cand_type = (candidate.get("type") or "").strip()
    facts = list(candidate.get("fact_ids", []) or [])

    def _res(decision: str, target: str, conf: float, reason: str) -> Resolution:
        return {
            "decision": decision,
            "target_id": target,
            "confidence": conf,
            "reason": reason,
            "auto_merge": decision == ALIAS_OF and conf >= AUTO_MERGE_THRESHOLD,
            "evidence_fact_ids": facts,
        }

    # 1. 显式声明的状态：必须能在同类型中找到唯一母资产
    state_of = (candidate.get("state_of") or "").strip()
    if state_of:
        hits = _find_link(state_of, existing, same_type=True, cand_type=cand_type)
        if len(hits) == 1:
            return _res(STATE_OF, hits[0]["asset_id"], _CONF_LINK,
                        f"声明为母资产『{state_of}』的状态，写入其变体需求而非新建自环")
        return _res(AMBIGUOUS, "", _CONF_AMBIGUOUS,
                    f"状态母资产『{state_of}』命中 {len(hits)} 个（非唯一），需人工裁决")

    # 2. 显式声明的部件：允许跨类型指向唯一母资产
    part_of = (candidate.get("part_of") or "").strip()
    if part_of:
        hits = _find_link(part_of, existing, same_type=False, cand_type=cand_type)
        if len(hits) == 1:
            return _res(PART_OF, hits[0]["asset_id"], _CONF_LINK,
                        f"声明为『{part_of}』的部件，作为独立子件挂接母资产")
        return _res(AMBIGUOUS, "", _CONF_AMBIGUOUS,
                    f"部件母资产『{part_of}』命中 {len(hits)} 个（非唯一），需人工裁决")

    # 3. 精确名/别名匹配
    hits = [item for item in existing if _matches(name, item)]
    if len(hits) > 1:
        return _res(AMBIGUOUS, "", _CONF_AMBIGUOUS,
                    f"名称『{name}』命中 {len(hits)} 个实体，禁止自动合并")
    if len(hits) == 1:
        hit = hits[0]
        if (hit.get("type") or "").strip() != cand_type:
            return _res(AMBIGUOUS, "", _CONF_AMBIGUOUS,
                        f"名称『{name}』跨类型冲突（候选 {cand_type} 对 既有 {hit.get('type')}），需人工裁决")
        return _res(ALIAS_OF, hit["asset_id"], _CONF_ALIAS,
                    f"名称『{name}』精确命中同类型资产，作为别名合并")

    # 4. 无精确命中：即便名字被包含（如「小林」含「林」）也判独立，绝不按字符串包含合并
    return _res(INDEPENDENT, "", 0.0, "无精确名/别名命中，判为独立实体（禁止按字符串包含自动合并）")


def assign_stable_keys(candidates: List[Dict[str, Any]], prefix: str,
                       start_seq: int = 1, line_width: int = 5) -> List[str]:
    """为新实体分配互不相同的 stable_key（TYPE:LINE:SEQ）；歧义/合并候选不进此处。

    这里只保证「不重复分配同一键」的下限不变式；真正的持久化复用仍由 StableKeyAllocator 负责。
    """
    keys: List[str] = []
    seen = set()
    seq = start_seq
    for _ in candidates:
        while True:
            key = f"{prefix}:{seq:0{line_width}d}:1"
            seq += 1
            if key not in seen:
                seen.add(key)
                keys.append(key)
                break
    return keys
