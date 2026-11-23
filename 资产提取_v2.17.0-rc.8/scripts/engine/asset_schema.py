#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资产规范化模式验证与稳定键映射（ADR-0084 / scripts/engine/asset_schema.py）

定义：
1. 规范化四级事实等级（FactLevel）：剧本事实、强推断、制作提案、待定冲突
2. 生产优先级（ProductionPriority）：P0、P1、P2、P3
3. 显式资产关系（RelationKind）：WEARS、APPEARS_IN、LOCATED_IN、CONNECTS_TO、STATE_OF
4. 资产类型（AssetType）：人物、场景、道具、服装
5. 稳定键分配器（StableKeyAllocator）：格式 TYPE:LINE:SEQ，防前置重排与漂移
6. 资产索引验证器（AssetSchemaValidator）：验证类型、ID存在性、双向关系、事实等级与摘句
"""

from __future__ import annotations

import enum
from typing import Any, Dict, List, Optional, Set


class FactLevel(str, enum.Enum):
    SCRIPT_FACT = "剧本事实"
    STRONG_INFERENCE = "强推断"
    PRODUCTION_PROPOSAL = "制作提案"
    PENDING_CONFLICT = "待定冲突"

    @classmethod
    def valid_values(cls) -> Set[str]:
        return {item.value for item in cls}


class ProductionPriority(str, enum.Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"

    @classmethod
    def valid_values(cls) -> Set[str]:
        return {item.value for item in cls}


class RelationKind(str, enum.Enum):
    WEARS = "WEARS"
    APPEARS_IN = "APPEARS_IN"
    LOCATED_IN = "LOCATED_IN"
    CONNECTS_TO = "CONNECTS_TO"
    STATE_OF = "STATE_OF"

    @classmethod
    def valid_values(cls) -> Set[str]:
        return {item.value for item in cls}


class AssetType(str, enum.Enum):
    CHARACTER = "人物"
    SCENE = "场景"
    PROP = "道具"
    COSTUME = "服装"

    @classmethod
    def valid_values(cls) -> Set[str]:
        return {item.value for item in cls}


class ValidationError(Exception):
    """资产索引模式或语义验证失败"""
    pass


class AliasConflictError(ValidationError):
    """别名同时命中多个实体，需人工裁决（禁止自动合并不同实体）"""
    pass


# ---------------------------------------------------------------------------
# 中文子类型与视觉需求（规格 2.1）：四类顶层资产不变，细化靠子类型与视觉需求表达。
# 顶层类型永远只有四个：人物、场景、道具、服装——法术、天气、特效等一律不进顶层。
# ---------------------------------------------------------------------------

ASSET_SUBTYPES: Dict[str, frozenset] = {
    "人物": frozenset({"主角", "重要配角", "普通配角", "非人角色", "群众", "其他"}),
    "场景": frozenset({"建筑", "室内空间", "自然环境", "交通节点", "其他"}),
    "道具": frozenset({"载具", "冷兵器", "日常器物", "文书", "法宝", "其他"}),
    "服装": frozenset({"常服", "礼服", "甲胄", "配饰", "其他"}),
}

# 原始标签 → 中文枚举（同义词归一）。单字别名只做精确匹配，避免「林府」误判为自然环境。
SUBTYPE_ALIASES: Dict[str, Dict[str, str]] = {
    "人物": {
        "男主": "主角", "女主": "主角", "主人公": "主角", "主角组": "主角",
        "主要角色": "重要配角", "关键角色": "重要配角", "反派": "重要配角",
        "次要角色": "普通配角", "配角": "普通配角", "辅助角色": "普通配角",
        "怪物": "非人角色", "妖兽": "非人角色", "神兽": "非人角色", "魔兽": "非人角色",
        "灵兽": "非人角色", "异兽": "非人角色", "精怪": "非人角色", "鬼怪": "非人角色",
        "妖族": "非人角色", "非人": "非人角色", "灵体": "非人角色", "傀儡": "非人角色",
        "群演": "群众", "龙套": "群众", "群众演员": "群众", "路人": "群众",
    },
    "场景": {
        "宫殿": "建筑", "楼阁": "建筑", "庙宇": "建筑", "殿堂": "建筑", "府邸": "建筑",
        "宅院": "建筑", "亭台": "建筑", "城楼": "建筑", "寺院": "建筑", "塔": "建筑",
        "房间": "室内空间", "屋内": "室内空间", "室内": "室内空间", "厅堂": "室内空间",
        "密室": "室内空间", "客栈": "室内空间", "书房": "室内空间", "大殿": "室内空间",
        "山林": "自然环境", "河流": "自然环境", "湖泊": "自然环境", "海边": "自然环境",
        "草原": "自然环境", "沙漠": "自然环境", "洞穴": "自然环境", "山谷": "自然环境",
        "森林": "自然环境", "竹林": "自然环境", "野外": "自然环境",
        "街道": "交通节点", "码头": "交通节点", "桥梁": "交通节点", "城门": "交通节点",
        "驿站": "交通节点", "路口": "交通节点", "渡口": "交通节点",
    },
    "道具": {
        "马车": "载具", "舟船": "载具", "轿子": "载具", "车辆": "载具", "船只": "载具",
        "飞行器": "载具", "轿": "载具", "车": "载具", "船": "载具",
        "长剑": "冷兵器", "短刀": "冷兵器", "长枪": "冷兵器", "兵刃": "冷兵器",
        "剑": "冷兵器", "刀": "冷兵器", "枪": "冷兵器", "弓": "冷兵器",
        "茶具": "日常器物", "灯具": "日常器物", "家具": "日常器物", "餐具": "日常器物",
        "灯": "日常器物", "桌": "日常器物", "椅": "日常器物", "杯": "日常器物", "碗": "日常器物",
        "书信": "文书", "卷轴": "文书", "地图": "文书", "账簿": "文书", "告示": "文书",
        "法器": "法宝", "灵器": "法宝", "符箓": "法宝", "丹药": "法宝", "宝物": "法宝",
    },
    "服装": {
        "便服": "常服", "日常服": "常服", "长袍": "常服", "衣裙": "常服", "常衣": "常服",
        "礼服": "礼服", "朝服": "礼服", "嫁衣": "礼服", "宫装": "礼服", "华服": "礼服",
        "盔甲": "甲胄", "铠甲": "甲胄", "战甲": "甲胄", "甲衣": "甲胄", "盔": "甲胄", "甲": "甲胄",
        "首饰": "配饰", "发饰": "配饰", "腰带": "配饰", "玉坠": "配饰", "面纱": "配饰",
    },
}

SUBTYPE_UNDIVIDED = "未细分"
SUBTYPE_OTHER = "其他"

# 视觉需求种类：法术、天候等感受性需求挂在资产/场景上，不生成第五类资产 ID
VISUAL_NEED_KINDS = frozenset({"法术", "天候", "时段", "氛围", "状态", "特效", "材质", "光线"})


def normalize_subtype(asset_type: str, raw: str) -> str:
    """把原始子类标签归一到中文枚举；未知值返回「其他」并保留原标签。

    空标签返回「未细分」（旧记录无子类型时的合法默认，规格 2.1 兼容）。
    保留原标签由 subtype_with_raw 提供，避免枚举丢失原文用词。
    """
    text = (raw or "").strip()
    if not text:
        return SUBTYPE_UNDIVIDED
    allowed = ASSET_SUBTYPES.get((asset_type or "").strip())
    if not allowed:
        return SUBTYPE_OTHER
    if text in allowed:
        return text
    aliases = SUBTYPE_ALIASES.get(asset_type, {})
    if text in aliases:
        return aliases[text]
    # 描述性子类（如「黑色长袍」）按最长别名包含匹配；单字别名只精确匹配
    for alias in sorted(aliases, key=lambda a: (-len(a), a)):
        if len(alias) >= 2 and alias in text:
            return aliases[alias]
    return SUBTYPE_OTHER


def subtype_with_raw(asset_type: str, raw: str) -> Dict[str, str]:
    """返回 {"subtype": 归一结果, "subtype_raw": 原始标签}。

    只有归入「其他」时才保留原标签——枚举命中或同义词归一都不丢信息
    （原始用词仍留在段级备注的 [子类:…] 标签里）。
    """
    normalized = normalize_subtype(asset_type, raw)
    text = (raw or "").strip()
    keep_raw = text if text and normalized == SUBTYPE_OTHER else ""
    return {"subtype": normalized, "subtype_raw": keep_raw}


def build_visual_need(asset_id: str, kind: str, value: str, quote: str = "",
                      line: str = "", fact_level: str = FactLevel.SCRIPT_FACT.value) -> Dict[str, Any]:
    """构造绑定在资产/场景上的视觉需求（规格 2.1：不新增第五类资产）。

    need_id 由内容确定性派生（同资产同需求反复解析得到同一 ID），前缀 VN_ 表明
    这不是资产 ID，禁止冒充 CH_/SC_/PR_/CO_。
    """
    import hashlib

    digest = hashlib.sha256(f"{asset_id}|{kind}|{value}".encode("utf-8")).hexdigest()[:8]
    return {
        "need_id": f"VN_{digest}",
        "asset_id": (asset_id or "").strip(),
        "kind": (kind or "").strip(),
        "value": (value or "").strip(),
        "fact_level": (fact_level or "").strip(),
        "evidence": {"quote": (quote or "").strip(), "line": str(line or "").strip()},
    }


def validate_visual_needs(needs: List[Dict[str, Any]], asset_ids: Set[str]) -> List[str]:
    """校验视觉需求：必须绑定已知资产、种类合法、剧本事实必须带原文摘句。"""
    errors: List[str] = []
    for idx, need in enumerate(needs):
        path = f"visual_needs[{idx}]"
        if not isinstance(need, dict):
            errors.append(f"{path}: 视觉需求必须为对象")
            continue
        asset_id = str(need.get("asset_id") or "").strip()
        if not asset_id:
            errors.append(f"{path}.asset_id: 视觉需求必须绑定资产或场景，不得悬空")
        elif asset_id not in asset_ids:
            errors.append(f"{path}.asset_id: 视觉需求引用的资产 '{asset_id}' 在索引中不存在"
                          "（法术/天气等需求必须挂到已有资产上，不得新建第五类资产）")
        kind = str(need.get("kind") or "").strip()
        if kind not in VISUAL_NEED_KINDS:
            errors.append(f"{path}.kind: 非法视觉需求种类 '{kind}'，只允许 {sorted(VISUAL_NEED_KINDS)}")
        if not str(need.get("value") or "").strip():
            errors.append(f"{path}.value: 视觉需求必须有具体取值")
        fact_level = str(need.get("fact_level") or "").strip()
        if fact_level not in FactLevel.valid_values():
            errors.append(f"{path}.fact_level: 非法事实等级 '{fact_level}'，"
                          f"只允许 {list(FactLevel.valid_values())}")
        elif fact_level == FactLevel.SCRIPT_FACT.value:
            evidence = need.get("evidence")
            quote = evidence.get("quote") if isinstance(evidence, dict) else ""
            if not str(quote or "").strip():
                errors.append(f"{path}.evidence: 事实等级为'剧本事实'的视觉需求必须带原文摘句(quote)")
    return errors


# R4c：关系契约单一真相（方向/类型矩阵）。validator 与 continuity 校验共用。
# STATE_OF 源类型「变体」当前四类资产契约中不存在——即：无独立变体节点时，
# 任何 STATE_OF 边都非法（自环与跨类型均报错），需求一律用 variant_needs 表达。
RELATION_MATRIX: Dict[str, Dict[str, str]] = {
    "WEARS": {"source": "服装", "target": "人物", "note": "服装穿戴于人物"},
    "APPEARS_IN": {"source": "人物", "target": "场景", "note": "人物出场于场景"},
    "LOCATED_IN": {"source": "道具", "target": "场景", "note": "道具定位于场景"},
    "CONNECTS_TO": {"source": "场景", "target": "场景", "note": "场景双向连通"},
    "STATE_OF": {"source": "变体", "target": "母资产", "note": "变体归属母资产"},
}


class ValidationResult:
    def __init__(self, is_valid: bool, errors: Optional[List[str]] = None):
        self.is_valid = is_valid
        self.errors = errors or []

    def __repr__(self) -> str:
        return f"<ValidationResult valid={self.is_valid} errors_count={len(self.errors)}>"


class AssetSchemaValidator:
    """资产索引 JSON 校验器"""

    def validate_index(self, index_data: Dict[str, Any]) -> ValidationResult:
        errors: List[str] = []

        if not isinstance(index_data, dict):
            return ValidationResult(False, ["资产索引根对象必须为 JSON Object"])

        schema_version = index_data.get("schema_version")
        if schema_version != 1:
            errors.append(f"schema_version 必须为 1，当前为: {schema_version}")

        fingerprint = index_data.get("source_fingerprint")
        if not fingerprint or not isinstance(fingerprint, str):
            errors.append("source_fingerprint 必须为非空字符串")

        assets = index_data.get("assets")
        if not isinstance(assets, list):
            errors.append("assets 必须为列表")
            return ValidationResult(False, errors)

        asset_ids: Set[str] = set()
        stable_keys: Set[str] = set()
        types_by_id: Dict[str, str] = {}

        # 第一轮扫描：基础字段、重复检查与格式校验
        for idx, item in enumerate(assets):
            path = f"assets[{idx}]"
            if not isinstance(item, dict):
                errors.append(f"{path}: 资产项必须为对象")
                continue

            asset_id = item.get("asset_id")
            if not asset_id or not isinstance(asset_id, str):
                errors.append(f"{path}.asset_id: 必须为非空字符串")
            elif asset_id in asset_ids:
                errors.append(f"{path}.asset_id: 重复的 asset_id '{asset_id}'")
            else:
                asset_ids.add(asset_id)

            stable_key = item.get("stable_key")
            if not stable_key or not isinstance(stable_key, str):
                errors.append(f"{path}.stable_key: 必须为非空字符串")
            elif stable_key in stable_keys:
                errors.append(f"{path}.stable_key: 重复的 stable_key '{stable_key}'")
            else:
                stable_keys.add(stable_key)

            asset_type = item.get("type")
            if asset_id:
                types_by_id[asset_id] = asset_type if isinstance(asset_type, str) else ""
            if asset_type not in AssetType.valid_values():
                errors.append(f"{path}.type: 非法资产类型 '{asset_type}'，只允许 {list(AssetType.valid_values())}")

            fact_level = item.get("fact_level")
            if fact_level not in FactLevel.valid_values():
                errors.append(f"{path}.fact_level: 非法事实等级 '{fact_level}'，只允许 {list(FactLevel.valid_values())}")

            priority = item.get("priority")
            if priority not in ProductionPriority.valid_values():
                errors.append(f"{path}.priority: 非法生产优先级 '{priority}'，只允许 {list(ProductionPriority.valid_values())}")

            evidence = item.get("evidence")
            if not isinstance(evidence, list):
                errors.append(f"{path}.evidence: 必须为列表")
            else:
                # 剧本事实必须包含至少一条有摘句的 evidence
                if fact_level == FactLevel.SCRIPT_FACT.value:
                    valid_quotes = [e for e in evidence if isinstance(e, dict) and e.get("quote")]
                    if not valid_quotes:
                        errors.append(f"{path}.evidence: 事实等级为'剧本事实'时必须提供至少一条非空原文摘句(quote)")

        # 第二轮扫描：依赖关系与网络端点存在性校验
        for idx, item in enumerate(assets):
            path = f"assets[{idx}]"
            asset_id = item.get("asset_id", f"item_{idx}")

            dependencies = item.get("dependencies", [])
            if isinstance(dependencies, list):
                for dep in dependencies:
                    if dep not in asset_ids:
                        errors.append(f"{path} ({asset_id}): 依赖的资产 '{dep}' 在索引中不存在")
            else:
                errors.append(f"{path}.dependencies: 必须为列表")

            relations = item.get("relations", [])
            if isinstance(relations, list):
                for r_idx, rel in enumerate(relations):
                    rel_path = f"{path}.relations[{r_idx}]"
                    if not isinstance(rel, dict):
                        errors.append(f"{rel_path}: 关系项必须为对象")
                        continue
                    kind = rel.get("kind")
                    target = rel.get("target")
                    if kind not in RelationKind.valid_values():
                        errors.append(f"{rel_path}.kind: 非法关系种类 '{kind}'")
                    if not target or target not in asset_ids:
                        errors.append(f"{rel_path}.target: 关系目标 '{target}' 在索引中不存在")
                    # R4c：方向/类型矩阵与 continuity 共用同一契约；STATE_OF 自环显式拒绝
                    if isinstance(kind, str) and kind == RelationKind.STATE_OF.value and target == asset_id:
                        errors.append(f"{rel_path}: STATE_OF 自环禁止（无独立变体节点时用 variant_needs 表达）")
                    rule = RELATION_MATRIX.get(kind) if isinstance(kind, str) else None
                    if rule and target in asset_ids:
                        src_type = item.get("type")
                        if src_type != rule["source"]:
                            errors.append(f"{rel_path}: {kind} 方向错误：源必须为{rule['source']}，当前为{src_type}")
                        tgt_type = types_by_id.get(target, "")
                        if tgt_type and tgt_type != rule["target"]:
                            errors.append(f"{rel_path}: {kind} 方向错误：目标必须为{rule['target']}，'{target}' 为{tgt_type}")
            else:
                errors.append(f"{path}.relations: 必须为列表")

        return ValidationResult(len(errors) == 0, errors)


class StableKeyAllocator:
    """稳定键分配器：持久化 stable_key ↔ 出处/别名/旧ID 映射，防止行号移动导致重编号。

    复用规则（按优先级）：
    1. asset_id 命中已有映射 → 复用；
    2. 别名命中唯一实体 → 复用；命中多个实体 → 抛 AliasConflictError，交人工裁决；
    3. 全新实体 → 按类型/行号分配新 key（行号仅影响新 key 命名，不改变已有 key）。
    """

    TYPE_PREFIX_MAP = {
        "人物": "CHR",
        "场景": "SCN",
        "道具": "PRP",
        "服装": "CST",
    }

    def __init__(self, mapping_store: Optional[Dict[str, Any]] = None):
        # 存储: { "keys": { stable_key: { "asset_id": ..., "source_key": ..., "aliases": [...] } }, "type_counters": { "CHR": 1, ... } }
        self.store = mapping_store if mapping_store is not None else {"keys": {}, "type_counters": {}}
        if "keys" not in self.store:
            self.store["keys"] = {}
        if "type_counters" not in self.store:
            self.store["type_counters"] = {}
        # 别名复用只针对外部传入的历史映射；本构建内新建实体不得因共享别名互相吞并
        self._preexisting_keys = set(self.store["keys"])

    def allocate(
        self,
        asset_type: str,
        asset_id: str,
        first_evidence: Optional[Dict[str, Any]] = None,
        aliases: Optional[List[str]] = None,
        line_no: Optional[int] = None,
    ) -> str:
        """为实体分配或复用 stable_key"""
        aliases = aliases or []
        prefix = self.TYPE_PREFIX_MAP.get(asset_type, "AST")

        # 1. asset_id 命中 → 复用
        for sk, data in self.store["keys"].items():
            if data.get("asset_id") == asset_id:
                return sk
        # 2. 别名命中唯一历史实体 → 复用；命中多个 → 抛冲突（仅查外部传入的历史键）
        hits = []
        for sk in self._preexisting_keys:
            existing_aliases = self.store["keys"][sk].get("aliases", [])
            if any(a in existing_aliases for a in aliases):
                hits.append(sk)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise AliasConflictError(
                f"别名 {aliases} 同时命中多个实体 {hits}，禁止自动合并，需人工裁决")

        # 出处计算基数
        line_num = 0
        if line_no is not None:
            line_num = int(line_no)
        elif first_evidence and "line" in first_evidence:
            try:
                line_str = str(first_evidence["line"])
                digits = "".join(filter(str.isdigit, line_str))
                line_num = int(digits) if digits else 0
            except Exception:
                line_num = 0

        line_key = f"{prefix}:{line_num:05d}"
        current_counter = self.store["type_counters"].get(line_key, 0) + 1
        self.store["type_counters"][line_key] = current_counter

        stable_key = f"{prefix}:{line_num:05d}:{current_counter}"
        self.store["keys"][stable_key] = {
            "asset_id": asset_id,
            "type": asset_type,
            "aliases": aliases,
            "first_evidence": first_evidence,
        }
        return stable_key

    def to_dict(self) -> Dict[str, Any]:
        return self.store

    def get_mapping(self) -> Dict[str, Any]:
        return self.store
