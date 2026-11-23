#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审核请求与结果契约（阶段 2 工单 2.2）。

主模型不一定能被普通 Python 进程直接调用，因此审核走**可移植的导出/回写协议**：

1. 程序生成结构化 `review-request.json`（任务事实、图片与参考图相对路径、六项绑定哈希、审核清单版本）；
2. 当前宿主的主模型读图后产出结构化 `review-result.json`（判定、逐项检查、问题码、证据、修复指令）；
3. 程序校验任务号与六项哈希后写回状态；对不上就拒绝，绝不「按签名放行」。

路径固定：`视觉资产/审核请求/<task_id>/review-request.json` 与同目录 `review-result.json`。
没有主模型审核结果时任务停在等待审核，不得自动通过。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .visual_schema import (
    REVIEW_DECISIONS,
    ReviewBinding,
    VisualPlan,
    VisualTask,
)

REVIEW_DIR_REL = ("视觉资产", "审核请求")
REQUEST_NAME = "review-request.json"
RESULT_NAME = "review-result.json"
CHECKLIST_VERSION = 1

# 逐项审核清单（设计 §9 的硬门项）：判定必须逐项给出，缺项即为不完整审核
CHECKLIST_ITEMS = (
    "style", "identity", "hands", "proportion", "crop", "position", "viewpoint",
    "eyeline", "axis", "occlusion", "prop", "scene", "extra_subject", "text_watermark",
)
CHECK_VERDICTS = ("pass", "fail", "unknown")

# 稳定原因码
REVIEW_REQUEST_MISSING = "REVIEW_REQUEST_MISSING"
REVIEW_RESULT_MISSING = "REVIEW_RESULT_MISSING"
REVIEW_TASK_MISMATCH = "REVIEW_TASK_MISMATCH"
REVIEW_IMAGE_MISSING = "REVIEW_IMAGE_MISSING"
REVIEW_IMAGE_MISMATCH = "REVIEW_IMAGE_MISMATCH"
REVIEW_PROMPT_STALE = "REVIEW_PROMPT_STALE"
REVIEW_REFERENCE_STALE = "REVIEW_REFERENCE_STALE"
REVIEW_STYLE_STALE = "REVIEW_STYLE_STALE"
REVIEW_COMPOSITION_STALE = "REVIEW_COMPOSITION_STALE"
REVIEW_BINDING_INCOMPLETE = "REVIEW_BINDING_INCOMPLETE"
REVIEW_DECISION_UNKNOWN = "REVIEW_DECISION_UNKNOWN"
REVIEW_CHECKLIST_INCOMPLETE = "REVIEW_CHECKLIST_INCOMPLETE"
REVIEW_SECRET_FIELD = "REVIEW_SECRET_FIELD"
REVIEW_PATH_UNSAFE = "REVIEW_PATH_UNSAFE"

# 密钥样式字段：请求/结果/日志/任务包一律不得出现（只报字段名，绝不回显值）
SECRET_FIELD_PATTERN = re.compile(
    r"(api[_-]?key|authorization|bearer|access[_-]?token|secret|password|credential)",
    re.IGNORECASE)
SECRET_VALUE_PATTERN = re.compile(r"(AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9]{20,})")
ABSOLUTE_PATH_PATTERN = re.compile(r"^([A-Za-z]:[\\/]|/)")


class ReviewContractError(ValueError):
    """审核契约违规：code 为稳定原因码，detail 只写真话，绝不回显密钥。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reference_set_sha256(references: tuple[dict, ...]) -> str:
    """参考图集合哈希：按职责 + 相对路径排序后取规范文本哈希（集合与顺序无关）。"""
    rows = sorted(
        (str(item.get("role", "")), str(item.get("path", "")), str(item.get("sha256", "")))
        for item in references)
    payload = "\n".join(f"{role}|{path}|{digest}" for role, path, digest in rows)
    return sha256_text(payload)


def _scan_no_secrets(payload: object, where: str) -> None:
    """递归扫描密钥样式字段与值；命中只报字段路径，不回显内容。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if SECRET_FIELD_PATTERN.search(str(key)):
                raise ReviewContractError(REVIEW_SECRET_FIELD,
                                          f"{where} 含密钥样式字段：{key}")
            _scan_no_secrets(value, where)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            _scan_no_secrets(item, where)
    elif isinstance(payload, str):
        if SECRET_VALUE_PATTERN.search(payload):
            raise ReviewContractError(REVIEW_SECRET_FIELD, f"{where} 含密钥样式字面值")


def _check_rel(value: str, where: str) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        return ""
    if ABSOLUTE_PATH_PATTERN.match(text) or ".." in text.split("/"):
        raise ReviewContractError(REVIEW_PATH_UNSAFE,
                                  f"{where} 必须是资产库内相对路径，当前 {text!r}")
    return text


@dataclass(frozen=True)
class ReviewRequest:
    """一次审核请求：任务事实 + 图片/参考图相对路径 + 六项绑定哈希 + 清单版本。"""

    task_id: str = ""
    checklist_version: int = CHECKLIST_VERSION
    binding: ReviewBinding = field(default_factory=ReviewBinding)
    facts: dict = field(default_factory=dict)
    image: str = ""
    references: tuple[dict, ...] = ()
    composition: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "checklist_version": int(self.checklist_version),
            "task_id": self.task_id,
            "binding": self.binding.as_dict(),
            "facts": dict(self.facts),
            "image": self.image,
            "references": [dict(item) for item in self.references],
            "composition": dict(self.composition),
        }

    def is_complete(self) -> bool:
        return self.binding.is_complete and bool(self.task_id)


@dataclass(frozen=True)
class ReviewResult:
    """一次审核结果：判定 + 逐项检查 + 问题码/证据/修复指令 + 审核者与时间。"""

    task_id: str = ""
    decision: str = ""
    binding: ReviewBinding = field(default_factory=ReviewBinding)
    checks: tuple[dict, ...] = ()
    issues: tuple[dict, ...] = ()
    reviewer: str = ""
    reviewed_at: str = ""
    notes: str = ""

    @property
    def is_accepted(self) -> bool:
        return self.decision in ("ACCEPTED", "PASS_WITH_NOTES")


def request_dir(lib: Path, task_id: str) -> Path:
    return Path(lib) / REVIEW_DIR_REL[0] / REVIEW_DIR_REL[1] / task_id


def request_path(lib: Path, task_id: str) -> Path:
    return request_dir(lib, task_id) / REQUEST_NAME


def result_path(lib: Path, task_id: str) -> Path:
    return request_dir(lib, task_id) / RESULT_NAME


def build_review_request(lib: Path, plan: VisualPlan, task: VisualTask) -> ReviewRequest:
    """由计划任务与已落盘产物构建审核请求（六项哈希现场计算，不用计划里的旧值冒充）。"""
    lib = Path(lib)
    attempt = task.latest_attempt
    if attempt is None or not str(attempt.image or "").strip():
        raise ReviewContractError(REVIEW_IMAGE_MISSING, f"任务 {task.task_id} 尚无已落盘产物")
    image_rel = _check_rel(attempt.image, "产物图片")
    image_path = lib / image_rel
    if not image_path.is_file():
        raise ReviewContractError(REVIEW_IMAGE_MISSING, f"产物文件不存在：{image_rel}")

    prompt_source = task.prompt or attempt.prompt_hash or ""
    references = tuple(dict(item) for item in _references_of(task))
    composition_source = _composition_source(lib, task)
    binding = ReviewBinding(
        task_id=task.task_id,
        image_sha256=sha256_file(image_path),
        prompt_sha256=sha256_text(prompt_source) if prompt_source else "",
        reference_set_sha256=reference_set_sha256(references),
        style_sha256=str(task.style_hash or ""),
        composition_sha256=composition_source,
    )
    facts = {
        "purpose": task.purpose,
        "kind": task.kind,
        "asset_id": task.asset_id,
        "asset_index_key": task.asset_index_key,
        "script_source": task.script_source,
        "storyboard_source": task.storyboard_source,
        "fact_source": task.fact_source,
        "prompt": task.prompt,
        "negative_prompt": task.negative_prompt,
        "user_locked": list(task.user_locked),
        "aspect_ratio": task.aspect_ratio,
        "current_version": task.current_version,
    }
    composition = {
        "path": task.composition_path,
        "sha256": task.composition_sha256,
        "blocking_path": task.blocking_path,
        "blocking_sha256": task.blocking_sha256,
    }
    request = ReviewRequest(task_id=task.task_id, binding=binding, facts=facts,
                            image=image_rel, references=references, composition=composition)
    _scan_no_secrets(request.to_dict(), "review-request")
    return request


def _references_of(task: VisualTask) -> list[dict]:
    return [{"path": b.path, "role": b.role, "sha256": b.sha256}
            for b in task.references if str(b.path or "").strip()]


def _composition_source(lib: Path, task: VisualTask) -> str:
    """构图卡哈希：优先用构图卡文件现算，其次用任务上已登记的哈希。

    没有构图卡的基础资产（正脸/服装/道具）用**空构图事实的规范哈希**占位，
    使六项绑定对每项任务都完整可算；「无构图卡」与「有构图卡」因此天然不同。
    """
    rel = _check_rel(task.composition_path, "构图卡路径")
    if rel:
        path = Path(lib) / rel
        if path.is_file():
            return sha256_file(path)
    declared = str(task.composition_sha256 or "").strip().lower()
    if declared:
        return declared
    return sha256_text("")


def write_review_request(lib: Path, request: ReviewRequest) -> Path:
    """原子写出审核请求；含密钥样式字段一律拒绝写出。"""
    payload = request.to_dict()
    _scan_no_secrets(payload, "review-request")
    target = request_path(Path(lib), request.task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(target, payload)
    return target


def load_review_result(path: Path) -> ReviewResult:
    """读取并校验审核结果结构（判定/清单/绑定完整性）；不合法即稳定码失败。"""
    target = Path(path)
    if not target.is_file():
        raise ReviewContractError(REVIEW_RESULT_MISSING, f"审核结果不存在：{target.name}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ReviewContractError(REVIEW_RESULT_MISSING, f"审核结果不可解析：{e}") from e
    if not isinstance(data, dict):
        raise ReviewContractError(REVIEW_RESULT_MISSING, "审核结果必须是 JSON 对象")
    _scan_no_secrets(data, "review-result")

    decision = str(data.get("decision") or "").strip()
    if decision not in REVIEW_DECISIONS:
        raise ReviewContractError(REVIEW_DECISION_UNKNOWN,
                                  f"判定只允许 {REVIEW_DECISIONS}，当前 {decision!r}")

    raw_checks = data.get("checks")
    if not isinstance(raw_checks, list):
        raise ReviewContractError(REVIEW_CHECKLIST_INCOMPLETE, "审核结果缺少逐项检查清单（checks）")
    verdicts = {str(row.get("item", "")): str(row.get("verdict", "")) for row in raw_checks
                if isinstance(row, dict)}
    missing = [item for item in CHECKLIST_ITEMS if item not in verdicts]
    if missing:
        raise ReviewContractError(REVIEW_CHECKLIST_INCOMPLETE,
                                  f"逐项检查缺项：{'、'.join(missing)}")
    bad = [item for item, verdict in verdicts.items() if verdict not in CHECK_VERDICTS]
    if bad:
        raise ReviewContractError(REVIEW_CHECKLIST_INCOMPLETE,
                                  f"逐项判定只允许 {CHECK_VERDICTS}：{bad}")

    binding = _binding_from_dict(data.get("binding") or {})
    issues = tuple(dict(row) for row in (data.get("issues") or []) if isinstance(row, dict))
    return ReviewResult(task_id=str(data.get("task_id") or "").strip(), decision=decision,
                        binding=binding, checks=tuple(dict(row) for row in raw_checks),
                        issues=issues, reviewer=str(data.get("reviewer") or "").strip(),
                        reviewed_at=str(data.get("reviewed_at") or "").strip(),
                        notes=str(data.get("notes") or "").strip())


def _binding_from_dict(data: dict) -> ReviewBinding:
    try:
        return ReviewBinding(
            task_id=str(data.get("task_id", "")), image_sha256=str(data.get("image_sha256", "")),
            prompt_sha256=str(data.get("prompt_sha256", "")),
            reference_set_sha256=str(data.get("reference_set_sha256", "")),
            style_sha256=str(data.get("style_sha256", "")),
            composition_sha256=str(data.get("composition_sha256", "")),
        )
    except ValueError as e:
        raise ReviewContractError(REVIEW_BINDING_INCOMPLETE, str(e)) from e


def validate_review_binding(request: ReviewRequest, result: ReviewResult) -> None:
    """校验审核结果确实针对本次请求：任务号 + 六项哈希逐项比对，任一不符即拒绝。"""
    if not request.is_complete():
        raise ReviewContractError(REVIEW_BINDING_INCOMPLETE,
                                  "审核请求缺少任务号或六项哈希，无法建立绑定")
    if str(result.task_id) != str(request.task_id):
        raise ReviewContractError(
            REVIEW_TASK_MISMATCH, f"审核结果属于 {result.task_id!r}，本次请求是 {request.task_id!r}")
    if not result.binding.is_complete:
        raise ReviewContractError(REVIEW_BINDING_INCOMPLETE, "审核结果缺少六项绑定哈希")

    want = request.binding.as_dict()
    got = result.binding.as_dict()
    checks = (
        ("image_sha256", REVIEW_IMAGE_MISMATCH, "图片哈希与请求不一致（图已被替换）"),
        ("prompt_sha256", REVIEW_PROMPT_STALE, "提示词哈希与请求不一致（提示词已改）"),
        ("reference_set_sha256", REVIEW_REFERENCE_STALE, "参考图集合哈希与请求不一致"),
        ("style_sha256", REVIEW_STYLE_STALE, "画风包哈希与请求不一致（画风包已变）"),
        ("composition_sha256", REVIEW_COMPOSITION_STALE, "构图卡哈希与请求不一致"),
    )
    for key, code, detail in checks:
        if str(want.get(key, "")) != str(got.get(key, "")):
            raise ReviewContractError(
                code, f"{detail}（请求 {str(want.get(key, ''))[:12]}… 结果 {str(got.get(key, ''))[:12]}…）")


def _atomic_json(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, target)
