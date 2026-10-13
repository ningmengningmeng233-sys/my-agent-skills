#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可选双审核子代理的证据合并（阶段 6 工单 6.3，规格九 / 十）。

职责边界（不可协商）：

- **子代理默认关闭**：项目设置 `review_subagents = false` 时本模块的所有入口都拒绝工作，
  需要宿主显式开启。
- **两个角色只提供各自领域的证据**：美术代理（画风／身份／服装／场景／跨图连续性）与技术代理
  （手部／比例／接触关系／构图／道具数量／画面伪影）。越域报告一律拒绝，
  以免出现「美术代理用技术理由否决」这类越权。
- **二者没有签发权**：合并产物是给当前主模型看证据的草稿，不含判定、不写正式版本、
  不改任务状态；主模型必须重新看图并对冲突证据做终裁。
- **不得把 Gemini 当子审核代理**：报告署名里出现 Gemini 一律拒绝。

合并只做事实层面的机械合并：并集、排序、去重、覆盖度、冲突标注。
判定与发布仍走 `review_contract` / `visual_publisher` 的同一条链路。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .image_qc import ISSUE_CODES, QcIssue
from .review_contract import REVIEW_DIR_REL

ART_ROLE = "美术"
TECH_ROLE = "技术"

SUBAGENT_MERGE_NAME = "subagent-merge.json"

# 美术代理证据域（画风 / 身份 / 服装 / 场景 / 跨图连续性）
ART_CODES = frozenset({
    "STYLE_MISMATCH", "MATERIAL_MISMATCH", "LIGHTING_MISMATCH", "LOW_DETAIL",
    "REFERENCE_OVERREACH", "IDENTITY_DRIFT", "HAIR_STRUCTURE_ERROR",
    "COSTUME_INCOMPLETE", "COSTUME_FRONT_BACK_MISMATCH", "FRONT_BACK_INCONSISTENT",
    "SPACE_ANCHOR_MISSING", "SPACE_RELATION_ERROR", "SPACE_FACT_INVENTED",
    "SCENE_STRUCTURE_MISMATCH", "REFERENCE_CHAIN_INVALID",
})

# 技术代理证据域（手部 / 比例 / 接触关系 / 构图 / 道具数量 / 伪影）
TECH_CODES = frozenset({
    "HAND_FINGER_DEFECT", "BODY_PROPORTION_ERROR", "BODY_PROPORTION_DEFECT",
    "NECK_STUMP_VISIBLE", "HEAD_REMAINS_IN_COSTUME_VIEW", "CROP_MISMATCH",
    "SUBJECT_CROPPED", "POSITION_MISMATCH", "VIEWPOINT_MISMATCH", "EYELINE_MISMATCH",
    "AXIS_CROSSING", "OCCLUSION_MISMATCH", "PROP_COUNT_MISMATCH", "PROP_STATE_MISMATCH",
    "PROP_PROPORTION_ERROR", "PROP_SILHOUETTE_UNREADABLE", "EXTRA_SUBJECT",
    "TEXT_OR_WATERMARK", "FORBIDDEN_MULTI_PANEL",
})

DOMAIN_BY_ROLE = {ART_ROLE: ART_CODES, TECH_ROLE: TECH_CODES}

_FORBIDDEN_AGENT_PATTERN = "gemini"

MERGE_SUBAGENTS_DISABLED = "MERGE_SUBAGENTS_DISABLED"
MERGE_ROLE_MISMATCH = "MERGE_ROLE_MISMATCH"
MERGE_DOMAIN_VIOLATION = "MERGE_DOMAIN_VIOLATION"
MERGE_DECISION_FORBIDDEN = "MERGE_DECISION_FORBIDDEN"
MERGE_SUBAGENT_GEMINI_FORBIDDEN = "MERGE_SUBAGENT_GEMINI_FORBIDDEN"
MERGE_CODE_UNKNOWN = "MERGE_CODE_UNKNOWN"
MERGE_TASK_MISMATCH = "MERGE_TASK_MISMATCH"
MERGE_IMAGE_MISMATCH = "MERGE_IMAGE_MISMATCH"
MERGE_REPORT_UNREADABLE = "MERGE_REPORT_UNREADABLE"


class MergeError(ValueError):
    """子代理报告或合并失败：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


@dataclass(frozen=True)
class SubagentReport:
    """一个子代理的证据报告（只有证据，没有判定）。"""

    role: str = ""
    checker: str = ""
    task_id: str = ""
    image_sha256: str = ""
    issues: tuple[QcIssue, ...] = ()
    notes: str = ""
    checked_at: str = ""


@dataclass(frozen=True)
class MergeConflict:
    """两个子代理对同一问题码给出的严重度不一致（只能由主模型裁决）。"""

    code: str
    severities: tuple[str, ...]
    evidence: tuple[str, ...]
    resolution_hint: str = "两代理意见冲突：请当前主模型重新查看图片后裁决"


@dataclass(frozen=True)
class MergeDraft:
    """合并草稿：给当前主模型看证据，**不含判定**。"""

    task_id: str = ""
    image_sha256: str = ""
    issues: tuple[QcIssue, ...] = ()
    conflicts: tuple[MergeConflict, ...] = ()
    agreed_codes: tuple[str, ...] = ()
    coverage: Mapping[str, bool] = field(default_factory=dict)
    notes: str = ""

    @property
    def requires_main_model(self) -> bool:
        return True

    @property
    def hard_gate_codes(self) -> tuple[str, ...]:
        from .image_qc import HARD_GATE_CODES

        gates = set(HARD_GATE_CODES)
        return tuple(sorted(i.code for i in self.issues if i.code in gates))

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "image_sha256": self.image_sha256,
            "requires_main_model": True,
            "subagent_count": len([role for role, seen in self.coverage.items() if seen]),
            "coverage": {str(role): bool(seen) for role, seen in sorted(self.coverage.items())},
            "agreed_codes": list(self.agreed_codes),
            "hard_gate_codes": list(self.hard_gate_codes),
            "conflicts": [
                {"code": c.code, "severities": list(c.severities),
                 "evidence": list(c.evidence), "resolution_hint": c.resolution_hint}
                for c in self.conflicts],
            "issues": [
                {"code": i.code, "severity": i.severity, "evidence": i.evidence,
                 "repair_instruction": i.repair_instruction} for i in self.issues],
            "notes": self.notes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=False)


def merge_path(lib: Path, task_id: str) -> Path:
    """合并草稿路径（与审核请求同目录，供主模型读取）。"""
    return Path(lib) / REVIEW_DIR_REL[0] / REVIEW_DIR_REL[1] / str(task_id) / \
        SUBAGENT_MERGE_NAME


def _issues_from_rows(rows: Any) -> tuple[QcIssue, ...]:
    if rows is None:
        return ()
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise MergeError(MERGE_REPORT_UNREADABLE, "issues 必须是列表")
    out: list[QcIssue] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise MergeError(MERGE_REPORT_UNREADABLE, "问题项必须是对象")
        code = str(row.get("code") or "").strip()
        if code not in ISSUE_CODES:
            raise MergeError(MERGE_CODE_UNKNOWN, f"未知问题码 {code!r}")
        try:
            out.append(QcIssue(code=code, severity=str(row.get("severity") or "blocking"),
                               evidence=str(row.get("evidence") or ""),
                               repair_instruction=str(row.get("repair_instruction")
                                                      or row.get("repair") or "")))
        except ValueError as exc:
            raise MergeError(MERGE_REPORT_UNREADABLE, str(exc)) from exc
    return tuple(out)


def load_subagent_report(source: Mapping | Path | str,
                         *, expected_role: str) -> SubagentReport:
    """读取一份子代理报告并校验角色、领域、署名与「不得带判定」。"""
    if isinstance(source, Mapping):
        data: Mapping = source
    else:
        path = Path(source)
        if not path.is_file():
            raise MergeError(MERGE_REPORT_UNREADABLE, f"报告不存在：{path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MergeError(MERGE_REPORT_UNREADABLE,
                             f"报告不可读：{type(exc).__name__}") from exc
    if not isinstance(data, Mapping):
        raise MergeError(MERGE_REPORT_UNREADABLE, "报告必须是结构化映射")

    role = str(data.get("role") or "").strip()
    if role not in DOMAIN_BY_ROLE:
        raise MergeError(MERGE_ROLE_MISMATCH, f"未知角色 {role!r}")
    if role != expected_role:
        raise MergeError(MERGE_ROLE_MISMATCH, f"角色不符：{role!r} ≠ {expected_role!r}")

    checker = str(data.get("checker") or "").strip()
    if _FORBIDDEN_AGENT_PATTERN in checker.lower():
        raise MergeError(MERGE_SUBAGENT_GEMINI_FORBIDDEN,
                         f"审核子代理不得是 Gemini（署名 {checker!r}）")
    if not checker:
        raise MergeError(MERGE_REPORT_UNREADABLE, "子代理报告必须署名（checker）")

    if "decision" in data or "verdict" in data:
        raise MergeError(MERGE_DECISION_FORBIDDEN,
                         "子代理没有签发权：报告不得携带判定字段")

    issues = _issues_from_rows(data.get("issues"))
    outside = sorted({i.code for i in issues} - DOMAIN_BY_ROLE[role])
    if outside:
        raise MergeError(MERGE_DOMAIN_VIOLATION,
                         f"{role}代理报告了越域问题码：{outside}")

    return SubagentReport(
        role=role, checker=checker, task_id=str(data.get("task_id") or ""),
        image_sha256=str(data.get("image_sha256") or ""), issues=issues,
        notes=str(data.get("notes") or ""), checked_at=str(data.get("checked_at") or ""))


def merge_subagent_reports(art: SubagentReport,
                           tech: SubagentReport) -> MergeDraft:
    """把两份子代理报告合成一份证据草稿（不判定、不发布、冲突显式化）。"""
    if art.role != ART_ROLE or tech.role != TECH_ROLE:
        raise MergeError(MERGE_ROLE_MISMATCH, "合并需要一份美术报告与一份技术报告")
    if art.task_id != tech.task_id:
        raise MergeError(MERGE_TASK_MISMATCH,
                         f"两份报告任务不一致：{art.task_id!r} ≠ {tech.task_id!r}")
    if art.image_sha256 != tech.image_sha256:
        raise MergeError(MERGE_IMAGE_MISMATCH, "两份报告不是针对同一张图（图片哈希不同）")

    by_code: dict[str, list[QcIssue]] = {}
    for issue in (*art.issues, *tech.issues):
        by_code.setdefault(issue.code, []).append(issue)

    merged: list[QcIssue] = []
    conflicts: list[MergeConflict] = []
    agreed: list[str] = []
    for code in sorted(by_code):
        rows = by_code[code]
        severities = sorted({r.severity for r in rows})
        if len(severities) > 1:
            conflicts.append(MergeConflict(code=code, severities=tuple(severities),
                                           evidence=tuple(r.evidence for r in rows)))
        elif len(rows) > 1:
            agreed.append(code)
        # 同一事实只保留一条证据；冲突时同样只留一条，冲突本身单独列出
        merged.append(rows[0])

    return MergeDraft(
        task_id=art.task_id, image_sha256=art.image_sha256, issues=tuple(merged),
        conflicts=tuple(conflicts), agreed_codes=tuple(agreed),
        coverage={ART_ROLE: True, TECH_ROLE: True},
        notes="；".join(note for note in (art.notes.strip(), tech.notes.strip()) if note))


def subagents_enabled(lib: Path) -> bool:
    """子代理默认关闭：设置缺失或损坏一律按关闭处理。"""
    try:
        from .project_settings import load_project_settings

        return bool(load_project_settings(Path(lib)).review_subagents)
    except (OSError, ValueError):
        return False


def merge_for_library(lib: Path, task_id: str, art_path: Path,
                      tech_path: Path) -> MergeDraft:
    """按项目设置合并两份报告，并把草稿写到审核请求目录供主模型读取。"""
    lib = Path(lib)
    if not subagents_enabled(lib):
        raise MergeError(MERGE_SUBAGENTS_DISABLED,
                         "审核子代理默认关闭；需在项目设置里 review_subagents = true")
    art = load_subagent_report(art_path, expected_role=ART_ROLE)
    tech = load_subagent_report(tech_path, expected_role=TECH_ROLE)
    for report in (art, tech):
        if report.task_id and report.task_id != str(task_id):
            raise MergeError(MERGE_TASK_MISMATCH,
                             f"报告任务 {report.task_id!r} 与请求任务 {task_id!r} 不一致")
    draft = merge_subagent_reports(replace(art, task_id=str(task_id)),
                                   replace(tech, task_id=str(task_id)))
    target = merge_path(lib, task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(draft.to_json() + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return draft
