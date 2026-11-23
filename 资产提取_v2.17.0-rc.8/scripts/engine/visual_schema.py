#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉计划契约的兼容门面（T07）。

类型/校验/编解码已拆到 visual_types、visual_validation、visual_io，
本模块只负责**显式 re-export**：`scripts.engine.visual_schema` 的每一个原有顶层
名字（含私有助手）都必须继续可导入且是同一个对象。身份测试见
`测试/unit/test_visual_schema_compat.py`。
"""

from __future__ import annotations

from .visual_types import (
    ASPECT_BY_KIND,
    AttemptRecord,
    CompositionSpec,
    DEPTH_VALUES,
    MAX_AUTO_ROUNDS,
    PRODUCED_STATUSES,
    PUBLISHABLE_STATUSES,
    PropPlacement,
    QcDecision,
    QcRecord,
    REVIEW_DECISIONS,
    ReferenceBinding,
    ReferenceRole,
    ReviewBinding,
    SCHEMA_VERSION,
    SHA256_RE,
    SOURCE_LEVELS,
    STORY_TASK_KINDS,
    SubjectPlacement,
    TASK_ID_RE,
    USER_DECISION_LABELS,
    USER_STATUS_LABELS,
    VisualPlan,
    VisualTask,
    VisualTaskKind,
    VisualTaskStatus,
    ZONE_VALUES,
    is_relative_safe,
    normalize_rel_path,
    user_decision_label,
    user_status_label,
)
from .visual_validation import (
    ValidationIssue,
    _find_cycle,
    validate_visual_plan,
)
from .visual_io import (
    _attempt_from_row,
    _binding_from_row,
    _emit_array_of_tables,
    _emit_table,
    _task_from_row,
    _toml_value,
    load_visual_plan,
    render_visual_plan,
    write_visual_plan,
)

__all__ = [
    "ASPECT_BY_KIND",
    "AttemptRecord",
    "CompositionSpec",
    "DEPTH_VALUES",
    "MAX_AUTO_ROUNDS",
    "PRODUCED_STATUSES",
    "PUBLISHABLE_STATUSES",
    "PropPlacement",
    "QcDecision",
    "QcRecord",
    "REVIEW_DECISIONS",
    "ReferenceBinding",
    "ReferenceRole",
    "ReviewBinding",
    "SCHEMA_VERSION",
    "SHA256_RE",
    "SOURCE_LEVELS",
    "STORY_TASK_KINDS",
    "SubjectPlacement",
    "TASK_ID_RE",
    "USER_DECISION_LABELS",
    "USER_STATUS_LABELS",
    "ValidationIssue",
    "VisualPlan",
    "VisualTask",
    "VisualTaskKind",
    "VisualTaskStatus",
    "ZONE_VALUES",
    "_attempt_from_row",
    "_binding_from_row",
    "_emit_array_of_tables",
    "_emit_table",
    "_find_cycle",
    "_task_from_row",
    "_toml_value",
    "is_relative_safe",
    "load_visual_plan",
    "normalize_rel_path",
    "render_visual_plan",
    "user_decision_label",
    "user_status_label",
    "validate_visual_plan",
    "write_visual_plan",
]
