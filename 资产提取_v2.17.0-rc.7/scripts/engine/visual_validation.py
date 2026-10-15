#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉计划的纯校验（T07 从 visual_schema 机械搬移，不改判定规则）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .visual_types import (ASPECT_BY_KIND, MAX_AUTO_ROUNDS, PRODUCED_STATUSES,
                           ReferenceRole, SHA256_RE, TASK_ID_RE, VisualPlan, VisualTaskKind,
                           VisualTaskStatus, is_relative_safe)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    task_id: str = ""

    def __str__(self) -> str:
        return f"[{self.code}] {self.task_id + ': ' if self.task_id else ''}{self.message}"


# --- 校验（规格 6/7/8）：未知资产、依赖环、自依赖、超过三轮、未通过任务当参考 ------

def _find_cycle(graph: dict[str, set[str]]) -> list[str]:
    """返回首个环上的节点序列（空列表表示无环）。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    stack: list[str] = []

    def visit(node: str) -> list[str]:
        color[node] = GRAY
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in color:
                continue
            if color[nxt] == GRAY:
                return stack[stack.index(nxt):] + [nxt]
            if color[nxt] == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return []

    for node in sorted(graph):
        if color[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return []


def validate_visual_plan(plan: VisualPlan, asset_index: dict | None) -> list[ValidationIssue]:
    """校验视觉计划；返回问题清单（空列表 = 通过）。"""
    issues: list[ValidationIssue] = []
    assets = {}
    if isinstance(asset_index, dict):
        assets = {a.get("asset_id"): a for a in asset_index.get("assets", [])
                  if isinstance(a, dict)}

    if not plan.style_slug or not plan.style_version or not plan.style_hash:
        issues.append(ValidationIssue("STYLE_UNPINNED",
                                      "视觉计划必须固定风格包 slug/版本/内容哈希（规格 2.9）"))

    seen_ids: set[str] = set()
    for task in plan.tasks:
        tid = task.task_id
        if not TASK_ID_RE.match(tid or ""):
            issues.append(ValidationIssue("TASK_ID_FORMAT",
                                          f"任务 ID 必须为 <资产ID>-<任务种类>-vNN，当前 {tid!r}", tid))
        if tid in seen_ids:
            issues.append(ValidationIssue("DUPLICATE_TASK_ID", "任务 ID 重复", tid))
        seen_ids.add(tid)

        if task.kind not in VisualTaskKind.valid_values():
            issues.append(ValidationIssue("ILLEGAL_TASK_KIND",
                                          f"非法任务种类 {task.kind!r}", tid))
        if task.status not in VisualTaskStatus.valid_values():
            issues.append(ValidationIssue("ILLEGAL_STATUS", f"非法状态 {task.status!r}", tid))

        if assets and task.asset_id not in assets:
            issues.append(ValidationIssue("UNKNOWN_ASSET",
                                          f"资产 {task.asset_id!r} 不在资产索引中", tid))

        expected_aspect = ASPECT_BY_KIND.get(task.kind)
        if expected_aspect and task.aspect_ratio != expected_aspect:
            issues.append(ValidationIssue(
                "ASPECT_RATIO_MISMATCH",
                f"{task.kind} 画幅必须为 {expected_aspect}，当前 {task.aspect_ratio}", tid))

        if task.style_slug != plan.style_slug or task.style_version != plan.style_version \
                or task.style_hash != plan.style_hash:
            issues.append(ValidationIssue("STYLE_PIN_MISMATCH",
                                          "任务风格包版本/哈希必须与计划一致（母版更新不自动改变项目）", tid))

        if not str(task.fact_source or "").strip():
            issues.append(ValidationIssue("FACT_SOURCE_MISSING",
                                          "任务必须记录事实来源（资产索引与摘句），不得凭视觉模型推断事实", tid))

        if task.max_rounds > MAX_AUTO_ROUNDS:
            issues.append(ValidationIssue("MAX_ROUNDS_OVERFLOW",
                                          f"单任务最多自动返工 {MAX_AUTO_ROUNDS} 轮，当前上限 {task.max_rounds}", tid))
        if task.round_count > min(task.max_rounds, MAX_AUTO_ROUNDS):
            issues.append(ValidationIssue("ROUND_LIMIT_EXCEEDED",
                                          f"已产生 {task.round_count} 轮，超过 {MAX_AUTO_ROUNDS} 轮上限"
                                          "（应进入需要人工决定）", tid))

        for binding in task.references:
            if binding.role not in ReferenceRole.valid_values():
                issues.append(ValidationIssue("ILLEGAL_REFERENCE_ROLE",
                                              f"参考图职责非法 {binding.role!r}", tid))
            if not binding.path:
                issues.append(ValidationIssue("REFERENCE_PATH_EMPTY", "参考图路径为空", tid))
            elif not is_relative_safe(binding.path):
                code = "ABSOLUTE_PATH" if re.match(r"^[A-Za-z]:", binding.path) \
                    or binding.path.startswith("/") else "PATH_OUTSIDE_LIB"
                issues.append(ValidationIssue(code,
                                              f"参考图路径必须为资产库内相对路径，当前 {binding.path!r}", tid))

        for name in ("composition_path", "blocking_path"):
            value = str(getattr(task, name) or "")
            if not value:
                continue
            if not is_relative_safe(value):
                code = "ABSOLUTE_PATH" if re.match(r"^[A-Za-z]:", value) or value.startswith("/") \
                    else "PATH_OUTSIDE_LIB"
                label = "构图卡" if name == "composition_path" else "场景站位图"
                issues.append(ValidationIssue(
                    code, f"{label}路径必须为资产库内相对路径，当前 {value!r}", tid))

        for name in ("composition_sha256", "blocking_sha256"):
            value = str(getattr(task, name) or "").strip().lower()
            if value and not SHA256_RE.match(value):
                issues.append(ValidationIssue(
                    "HASH_LENGTH",
                    f"{name} 必须是 64 位十六进制 SHA-256，当前 {value!r}", tid))

        if task.depends_on and tid in task.depends_on:
            issues.append(ValidationIssue("SELF_DEPENDENCY", "任务不得依赖自身", tid))

    # 依赖端点与"未通过任务不得作为参考"
    by_id = {t.task_id: t for t in plan.tasks}
    for task in plan.tasks:
        for dep in task.depends_on:
            if dep == task.task_id:
                continue
            target = by_id.get(dep)
            if target is None:
                issues.append(ValidationIssue("UNKNOWN_DEPENDENCY",
                                              f"依赖的 {dep!r} 不在计划中", task.task_id))
                continue
            if not target.is_accepted and task.status in PRODUCED_STATUSES:
                issues.append(ValidationIssue(
                    "REFERENCE_NOT_ACCEPTED",
                    f"依赖的 {dep!r} 状态为 {target.status}，而本任务已开工"
                    f"（{task.status}）——未通过图片不得作为下游参考", task.task_id))

    graph = {t.task_id: {d for d in t.depends_on if d in by_id and d != t.task_id}
             for t in plan.tasks}
    cycle = _find_cycle(graph)
    if cycle:
        issues.append(ValidationIssue("DEPENDENCY_CYCLE", "依赖环：" + " → ".join(cycle),
                                      cycle[0]))
    return issues
