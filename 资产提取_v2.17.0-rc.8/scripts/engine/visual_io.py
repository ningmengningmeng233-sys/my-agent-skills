#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉计划的 TOML 编解码（T07 从 visual_schema 机械搬移，保持确定性与原子写）。

原契约口径（自 visual_schema.py 归位，逐字未改；写入纪律归本模块）：
- 写入确定性（同一对象两次写出字节一致）且原子（临时文件 + os.replace）。
- 中文直接写 UTF-8，绝不转义为 ``\\uXXXX``；Windows 反斜杠输入归一化为 ``/``。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence

from .visual_types import (AttemptRecord, MAX_AUTO_ROUNDS, ReferenceBinding, SCHEMA_VERSION,
                           VisualPlan, VisualTask, normalize_rel_path)

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 回退
    import tomli as tomllib


# --- TOML 序列化（确定性、UTF-8 直写中文、原子替换） -------------------------------

def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"不支持的 TOML 值类型: {type(value)!r}")


def _emit_table(lines: list[str], data: dict) -> None:
    for key, value in data.items():
        if isinstance(value, (list, tuple)) and value and isinstance(value[0], dict):
            continue
        lines.append(f"{key} = {_toml_value(value)}")


def _emit_array_of_tables(lines: list[str], name: str, rows: Sequence[dict]) -> None:
    for row in rows:
        lines.append("")
        lines.append(f"[[{name}]]")
        scalars = {k: v for k, v in row.items()
                   if not (isinstance(v, (list, tuple)) and v and isinstance(v[0], dict))}
        _emit_table(lines, scalars)
        for key, value in row.items():
            if isinstance(value, (list, tuple)) and value and isinstance(value[0], dict):
                _emit_array_of_tables(lines, f"{name}.{key}", value)


def render_visual_plan(plan: VisualPlan) -> str:
    """渲染确定性 TOML 文本（中文直写，不转义为 \\uXXXX）。"""
    head = {
        "schema_version": int(plan.schema_version),
        "revision": int(plan.revision),
        "style_name": plan.style_name,
        "style_slug": plan.style_slug,
        "style_version": plan.style_version,
        "style_hash": plan.style_hash,
        "engine_profile": plan.engine_profile,
        "lib": normalize_rel_path(plan.lib) or ".",
        "asset_index_hash": plan.asset_index_hash,
        "updated_at": plan.updated_at,
    }
    lines = [
        "# 视觉生产计划（工单02-Task4）：TOML 是唯一机器事实源，Markdown 阅读稿由本文件渲染。",
        "# 任务 ID：<资产ID>-<任务种类>-vNN；所有路径为资产库内相对路径。",
        "# 状态为内部英文枚举（用户界面由渲染层转中文）；最多自动返工 3 轮。",
    ]
    _emit_table(lines, head)
    lines.append("")
    task_rows = []
    for task in plan.sorted_tasks():
        row = {
            "task_id": task.task_id,
            "asset_id": task.asset_id,
            "kind": task.kind,
            "status": task.status,
            "aspect_ratio": task.aspect_ratio,
            "style_slug": task.style_slug,
            "style_version": task.style_version,
            "style_hash": task.style_hash,
            "engine": task.engine,
            "model": task.model,
            "max_rounds": int(task.max_rounds),
            "fact_source": task.fact_source,
            "purpose": task.purpose,
            "script_source": task.script_source,
            "asset_index_key": task.asset_index_key,
            "storyboard_source": task.storyboard_source,
            "prompt": task.prompt,
            "negative_prompt": task.negative_prompt,
            "composition_path": task.composition_path,
            "composition_sha256": task.composition_sha256,
            "blocking_path": task.blocking_path,
            "blocking_sha256": task.blocking_sha256,
            "current_version": task.current_version,
            "depends_on": tuple(task.depends_on),
            "materials": tuple(task.materials),
            "user_locked": tuple(task.user_locked),
            "notes": task.notes,
            "references": [
                {"path": b.path, "role": b.role, "sha256": b.sha256, "note": b.note}
                for b in task.references
            ],
            "attempts": [
                {
                    "round": int(a.round), "image": a.image, "request": a.request,
                    "response": a.response, "qc": a.qc, "prompt_hash": a.prompt_hash,
                    "image_sha256": a.image_sha256, "model": a.model,
                    "provider": a.provider, "response_id": a.response_id,
                }
                for a in task.attempts
            ],
        }
        task_rows.append(row)
    _emit_array_of_tables(lines, "tasks", task_rows)
    text = "\n".join(lines).rstrip("\n") + "\n"
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


def write_visual_plan(plan: VisualPlan, path: Path) -> None:
    """确定性 + 原子写入：同一对象两次写出字节完全一致。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render_visual_plan(plan), encoding="utf-8", newline="\n")
    os.replace(tmp, target)


def _binding_from_row(row: dict) -> ReferenceBinding:
    return ReferenceBinding(path=str(row.get("path", "")), role=str(row.get("role", "")),
                            sha256=str(row.get("sha256", "")), note=str(row.get("note", "")))


def _attempt_from_row(row: dict) -> AttemptRecord:
    return AttemptRecord(
        round=int(row.get("round", 1)), image=str(row.get("image", "")),
        request=str(row.get("request", "")), response=str(row.get("response", "")),
        qc=str(row.get("qc", "")), prompt_hash=str(row.get("prompt_hash", "")),
        image_sha256=str(row.get("image_sha256", "")), model=str(row.get("model", "")),
        provider=str(row.get("provider", "")), response_id=str(row.get("response_id", "")),
    )


def _task_from_row(row: dict) -> VisualTask:
    return VisualTask(
        task_id=str(row.get("task_id", "")), asset_id=str(row.get("asset_id", "")),
        kind=str(row.get("kind", "")), status=str(row.get("status", "")),
        aspect_ratio=str(row.get("aspect_ratio", "")),
        style_slug=str(row.get("style_slug", "")), style_version=str(row.get("style_version", "")),
        style_hash=str(row.get("style_hash", "")), engine=str(row.get("engine", "gemini")),
        model=str(row.get("model", "")),
        depends_on=tuple(str(x) for x in row.get("depends_on", ())),
        materials=tuple(str(x) for x in row.get("materials", ())),
        references=tuple(_binding_from_row(r) for r in row.get("references", ()) if isinstance(r, dict)),
        attempts=tuple(_attempt_from_row(r) for r in row.get("attempts", ()) if isinstance(r, dict)),
        max_rounds=int(row.get("max_rounds", MAX_AUTO_ROUNDS)),
        fact_source=str(row.get("fact_source", "")),
        user_locked=tuple(str(x) for x in row.get("user_locked", ())),
        notes=str(row.get("notes", "")),
        purpose=str(row.get("purpose", "")),
        script_source=str(row.get("script_source", "")),
        asset_index_key=str(row.get("asset_index_key", "")),
        storyboard_source=str(row.get("storyboard_source", "")),
        prompt=str(row.get("prompt", "")),
        negative_prompt=str(row.get("negative_prompt", "")),
        composition_path=str(row.get("composition_path", "")),
        composition_sha256=str(row.get("composition_sha256", "")),
        blocking_path=str(row.get("blocking_path", "")),
        blocking_sha256=str(row.get("blocking_sha256", "")),
        current_version=str(row.get("current_version", "")),
    )


def load_visual_plan(path: Path) -> VisualPlan:
    """读取视觉计划；文件缺失或不可解析时显式失败（不返回空计划冒充成功）。"""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"视觉计划不存在: {target}")
    with open(target, "rb") as fh:
        data = tomllib.load(fh)
    return VisualPlan(
        schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        revision=int(data.get("revision", 0)),
        style_name=str(data.get("style_name", "")),
        style_slug=str(data.get("style_slug", "")),
        style_version=str(data.get("style_version", "")),
        style_hash=str(data.get("style_hash", "")),
        engine_profile=str(data.get("engine_profile", "gemini")),
        lib=str(data.get("lib", ".")),
        asset_index_hash=str(data.get("asset_index_hash", "")),
        updated_at=str(data.get("updated_at", "")),
        tasks=tuple(_task_from_row(r) for r in data.get("tasks", ()) if isinstance(r, dict)),
    )
