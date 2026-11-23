# -*- coding: utf-8 -*-
"""依赖感知的工件依赖图（v2.17.0 工单 8）。

把「源事实 → 总账项 → 提示词 → 图片任务 → 审核结果」以及共享的「项目美术总则」建成有向图。
边语义固定为 **src → dst 表示 dst 依赖 src**：src 变化则 dst 及其下游全部失效。

节点指纹由**规范化输入 + 规则版本 + 模板版本 + 模型配置摘要**组成，且**绝不包含密钥**
（键名或取值命中密钥形态一律剔除，剔除后指纹不随密钥变化）。

失效传播返回「自身 + 下游传递闭包」，用迭代 + 访问集实现，环（审核返工回指任务）不会死循环。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

SCHEMA_VERSION = 1
GRAPH_FILENAME = "artifact-graph.json"
PROCESS_DIRNAME = ".process"

KIND_SOURCE_FACT = "source_fact"
KIND_LEDGER_ITEM = "ledger_item"
KIND_ART_CONSTITUTION = "art_constitution"
KIND_PROMPT = "prompt"
KIND_IMAGE_TASK = "image_task"
KIND_REVIEW = "review"
NODE_KINDS: Tuple[str, ...] = (KIND_SOURCE_FACT, KIND_LEDGER_ITEM, KIND_ART_CONSTITUTION,
                               KIND_PROMPT, KIND_IMAGE_TASK, KIND_REVIEW)

# 疑似密钥：**按词元**匹配，避免 "author" 被 "auth" 误伤。
SECRET_KEY_TOKENS = frozenset({
    "key", "apikey", "authorization", "auth", "token", "secret", "password",
    "passwd", "bearer", "cookie", "credential", "credentials", "signature",
})
# 允许出现的非密钥键（图结构自身用得到）
_KEY_ALLOWLIST = frozenset({"stable_key", "keys", "key_rule", "cache_key", "prompt_key"})


def node_id(kind: str, key: str) -> str:
    """节点标识：`kind:key`，天然按类型分命名空间，避免跨类型撞名。"""
    return f"{kind}:{key}"


def _tokens(name: Any) -> list:
    low = re.sub(r"[^a-z0-9]+", " ", str(name or "").strip().lower())
    return [t for t in low.split() if t]


def _is_secret_key(name: Any) -> bool:
    raw = str(name or "").strip().lower()
    if raw in _KEY_ALLOWLIST:
        return False
    tokens = _tokens(raw)
    if not tokens:
        return False
    if "_".join(tokens) in _KEY_ALLOWLIST:
        return False
    return any(t in SECRET_KEY_TOKENS for t in tokens)


def _strip_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _strip_secrets(v) for k, v in value.items() if not _is_secret_key(k)}
    if isinstance(value, (list, tuple)):
        return [_strip_secrets(v) for v in value]
    return value


def canonical_payload(payload: Mapping[str, Any] | None) -> str:
    """规范化 JSON：先剔除疑似密钥字段，再键排序、UTF-8 原文（不转义）。"""
    cleaned = _strip_secrets(dict(payload or {}))
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True, default=str)


def node_fingerprint(payload: Mapping[str, Any] | None, *, rule_version: str = "",
                     template_version: str = "", model_summary: str = "") -> str:
    """节点指纹 = sha256(规范化输入 + 规则版本 + 模板版本 + 模型配置摘要)。"""
    body = "\n".join([
        canonical_payload(payload),
        f"rule:{rule_version}",
        f"template:{template_version}",
        f"model:{model_summary}",
    ])
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class GraphNode:
    kind: str
    key: str
    fingerprint: str

    @property
    def id(self) -> str:
        return node_id(self.kind, self.key)


class ArtifactGraph:
    """工件依赖图；`edges[src] = {dst, ...}` 表示 dst 依赖 src。"""

    def __init__(self) -> None:
        self._nodes: Dict[str, GraphNode] = {}
        self._edges: Dict[str, set] = {}

    # --- 构建 -----------------------------------------------------------------

    def set_node(self, kind: str, key: str, fingerprint: str) -> GraphNode:
        if kind not in NODE_KINDS:
            raise ValueError(f"未知节点类型：{kind}（合法值 {NODE_KINDS}）")
        node = GraphNode(kind=kind, key=key, fingerprint=fingerprint)
        self._nodes[node.id] = node
        return node

    def add_edge(self, src_kind: str, src_key: str, dst_kind: str, dst_key: str) -> None:
        """登记「dst 依赖 src」；两端不存在时按未知指纹补建，避免悬空边静默丢失。"""
        src, dst = node_id(src_kind, src_key), node_id(dst_kind, dst_key)
        for kind, key, nid in ((src_kind, src_key, src), (dst_kind, dst_key, dst)):
            if nid not in self._nodes:
                self.set_node(kind, key, "")
        self._edges.setdefault(src, set()).add(dst)

    # --- 查询 -----------------------------------------------------------------

    def fingerprint(self, kind: str, key: str) -> str:
        node = self._nodes.get(node_id(kind, key))
        return node.fingerprint if node else ""

    def has_node(self, kind: str, key: str) -> bool:
        return node_id(kind, key) in self._nodes

    def nodes(self) -> Tuple[GraphNode, ...]:
        return tuple(self._nodes[k] for k in sorted(self._nodes))

    def nodes_of_kind(self, kind: str) -> Tuple[str, ...]:
        prefix = f"{kind}:"
        return tuple(sorted(n for n in self._nodes if n.startswith(prefix)))

    def dependencies(self, kind: str, key: str) -> set:
        """该节点依赖的上游（入边源）。"""
        dst = node_id(kind, key)
        return {src for src, outs in self._edges.items() if dst in outs}

    def dependents(self, kind: str, key: str) -> set:
        """直接下游（出边目标）。"""
        return set(self._edges.get(node_id(kind, key), set()))

    def invalidated_by(self, kind: str, key: str) -> set:
        """自身 + 下游传递闭包；环安全（用访问集而不是递归深度）。"""
        start = node_id(kind, key)
        if start not in self._nodes:
            return set()
        seen = {start}
        queue: List[str] = [start]
        while queue:
            current = queue.pop()
            for nxt in self._edges.get(current, set()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return seen

    def invalidated_tasks(self, kind: str, key: str) -> set:
        prefix = f"{KIND_IMAGE_TASK}:"
        return {n for n in self.invalidated_by(kind, key) if n.startswith(prefix)}

    # --- 落盘 -----------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "nodes": [{"kind": n.kind, "key": n.key, "fingerprint": n.fingerprint}
                      for n in self.nodes()],
            "edges": [{"src": src, "dst": dst}
                      for src in sorted(self._edges) for dst in sorted(self._edges[src])],
        }

    def save(self, lib) -> str:
        """原子落盘 `<lib>/.process/artifact-graph.json`。"""
        proc = Path(lib) / PROCESS_DIRNAME
        proc.mkdir(parents=True, exist_ok=True)
        path = proc / GRAPH_FILENAME
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return str(path)

    @classmethod
    def load(cls, lib) -> "ArtifactGraph":
        """载入依赖图。文件不存在返回空图；存在但损坏必须显式失败，不得退化成「没有依赖」。"""
        path = Path(lib) / PROCESS_DIRNAME / GRAPH_FILENAME
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{GRAPH_FILENAME} 不可读：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{GRAPH_FILENAME} 顶层必须是对象")
        graph = cls()
        for raw in data.get("nodes") or []:
            if isinstance(raw, Mapping) and raw.get("kind") in NODE_KINDS:
                graph.set_node(raw.get("kind", ""), str(raw.get("key", "")),
                               str(raw.get("fingerprint", "")))
        for raw in data.get("edges") or []:
            if not isinstance(raw, Mapping):
                continue
            src = _parse_node_id(str(raw.get("src", "")))
            dst = _parse_node_id(str(raw.get("dst", "")))
            if src and dst:
                graph.add_edge(src[0], src[1], dst[0], dst[1])
        return graph


def _parse_node_id(text: str) -> Tuple[str, str] | None:
    kind, _, key = (text or "").partition(":")
    if kind in NODE_KINDS and key:
        return kind, key
    return None


def build_library_graph(lib, *, prompt_fingerprints: Mapping[str, str] | None = None
                        ) -> ArtifactGraph:
    """按「源事实 → 总账项 → 提示词 → 图片任务 → 审核结果」建**库级**依赖图（工单 13）。

    只登记**存在的事实**：没有提示词就不建提示词节点，避免用空节点冒充「已编译」。
    `prompt_fingerprints`（资产 ID → 编译输入指纹）由编译器提供；缺省用总账项自身摘要，
    这样图在没跑编译器时依然自洽，而不是拿空指纹伪装已编译。
    视觉计划与审核结果一律惰性读取：核心库没有视觉模块时图仍然可建。
    """
    from .common import file_sha256

    lib = Path(lib)
    graph = ArtifactGraph()

    facts = lib / PROCESS_DIRNAME / "source-facts.jsonl"
    facts_key = "source-facts"
    graph.set_node(KIND_SOURCE_FACT, facts_key, file_sha256(facts) or "")

    constitution = lib / PROCESS_DIRNAME / "项目美术总则.json"
    if constitution.is_file():
        graph.set_node(KIND_ART_CONSTITUTION, "项目美术总则", file_sha256(constitution) or "")

    try:
        from .asset_ledger import load_ledger
        ledger = load_ledger(lib)
    except (ImportError, ValueError):
        ledger = []

    prompt_fingerprints = dict(prompt_fingerprints or {})
    for entry in ledger:
        asset_id = str(entry.get("asset_id") or "").strip()
        if not asset_id:
            continue
        graph.set_node(KIND_LEDGER_ITEM, asset_id, node_fingerprint({
            "asset_id": asset_id,
            "asset_type": entry.get("asset_type", ""),
            "canonical_name": entry.get("canonical_name", ""),
            "ledger_status": entry.get("ledger_status", ""),
            "production_mode": entry.get("production_mode", ""),
            "evidence_ids": list(entry.get("evidence_ids") or []),
        }))
        graph.add_edge(KIND_SOURCE_FACT, facts_key, KIND_LEDGER_ITEM, asset_id)
        if constitution.is_file():
            graph.add_edge(KIND_ART_CONSTITUTION, "项目美术总则", KIND_LEDGER_ITEM, asset_id)
        if "base_prompt" in (entry.get("required_outputs") or []):
            graph.set_node(KIND_PROMPT, asset_id, prompt_fingerprints.get(asset_id, ""))
            graph.add_edge(KIND_LEDGER_ITEM, asset_id, KIND_PROMPT, asset_id)

    plan_path = lib.joinpath("视觉资产", "计划.toml")
    if plan_path.is_file():
        try:
            from .visual_schema import load_visual_plan

            plan = load_visual_plan(plan_path)
        except (ImportError, OSError, ValueError):
            plan = None
        if plan is not None:
            for task in plan.tasks:
                graph.set_node(KIND_IMAGE_TASK, task.task_id, node_fingerprint({
                    "task_id": task.task_id, "kind": task.kind, "status": task.status,
                    "asset_id": task.asset_id,
                }, template_version=task.style_version, model_summary=task.model))
                asset_id = str(task.asset_id or "").strip()
                if asset_id and graph.has_node(KIND_PROMPT, asset_id):
                    graph.add_edge(KIND_PROMPT, asset_id, KIND_IMAGE_TASK, task.task_id)
                elif asset_id:
                    graph.add_edge(KIND_LEDGER_ITEM, asset_id, KIND_IMAGE_TASK, task.task_id)
                review = lib / "视觉资产" / "审核请求" / task.task_id / "review-result.json"
                if task.status in ("ACCEPTED", "READY_AS_REFERENCE") and review.is_file():
                    graph.set_node(KIND_REVIEW, task.task_id, file_sha256(review) or "")
                    graph.add_edge(KIND_IMAGE_TASK, task.task_id, KIND_REVIEW, task.task_id)
    return graph
