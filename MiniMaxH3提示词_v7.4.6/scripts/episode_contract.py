#!/usr/bin/env python3
"""Episode-plan contract: duration verdict, timeline, source coverage, honesty.

``episode-plan.json`` is the machine plan between the packer and the renderer. This
module is its gate: it decides whether a plan may be rendered at all, and it never
returns "pass" for a plan it cannot judge.

Adjudications frozen by this module (设计方案 §5、§10—§13; the work order records the
reasoning):

1. 计划只判两层。`SCRIPT_TARGET`（剧本声明目标时长）与 `PROMPT_PLAN`（片段计划）
   可以静态判定；`ACTUAL_GENERATED_MEDIA` 与 `FINAL_EDIT` 只能如实登记申报，
   没有真实媒体时必须是 `NOT_RUN` / `null`，绝不由计划值代填。
2. 总时长以片段为准。`planned_prompt_seconds` 只是申报值：判定永远用 Σ片段时长，
   申报值与它不符时另记 `PLANNED_SECONDS_MISMATCH`——谎报总量不能把缩水洗白。
3. 形态门禁先于数值门禁。任何字段缺失、类型错、取值错都只返回 `PLAN_SCHEMA_INVALID`
   报告（点名到具体路径），不再继续算判定：形态坏掉的计划没有"部分可信"。
   唯一的例外是 `script_target_seconds` 缺失或为 0，它有自己的
   `MISSING_SCRIPT_TARGET`（设计 §12.1 第一条），因为它的处置是"回去问用户"。
4. 片段时长必须整秒。模型按时长参数出片，半秒计划不是可执行计划。
5. 有效片段范围 = `config/duration-policy.json` 的 `clip_seconds` ∩ 目标模型在
   `config/model-capabilities.json` 的 `duration_seconds`。交集为空是
   `INFEASIBLE_CLIP_RANGE`，既不静默放宽也不逐片段刷屏。
6. 来源覆盖按字符范围判定。每个内容来源单元的全部字符必须被恰好覆盖一次：
   覆盖不足记 `SOURCE_COVERAGE_GAP`，同段落被引用两次、或引用超出单元自身范围，
   记 `SOURCE_COVERAGE_OVERLAP`（`duplicate_source_units`）。长台词允许同一来源 ID
   分片，只要范围接得上。
   声明层同样不许重复：两个**同类型**来源声明了互相重叠的范围，说明同一段剧本内容
   会出现两次，记 `SOURCE_UNIT_OVERLAP`（`overlapping_source_units`）。该判据只覆盖
   "要被演出来"的两类——`dialogue` 与 `action`；`clue` / `state_change` 的字符范围是
   指向剧本子串的**指针**而非被消费的文本（一句话里可以出现两个状态变化），对它们
   判重叠只会误阻断忠实计划。跨类型重叠一律合法：同一段字符可以既承载台词又承载
   状态变化。（"同类型重叠"这条比冻结设计 §11 更严，设计侧口径回写记在工单 Task 7。）
7. 系统补充过渡走独立通道。`system_transition` 来源类型没有剧本字符范围，
   只能挂在片段的 `system_added_transitions` 上；用 `source_refs` 引用它说明
   它在冒充剧本原文（`SYSTEM_TRANSITION_UNDECLARED`），声明不存在或指向剧本
   内容的 ID 则记 `SYSTEM_TRANSITION_DANGLING`。过渡自带 `span_*` 时不参与判定：
   它们是惰性元数据，为它们挡住"插点写成零长度范围"的计划只是误阻断。
   未被任何片段使用的过渡单元不算缺陷（可以预留），也不计入台账。
8. 每个片段必须引用至少一项来源。纯系统过渡片段等于凭空造内容，形态上就拒绝。
9. 最坏结论胜出。任何 FAIL 级发现都压过时长档位（`PASS_EXACT` / `PASS_WITH_TRIM` /
   `REVIEW_REQUIRED`），因为坏计划不能靠"时长刚好"变成可交付。
10. 未知顶层键原样忽略（前向兼容），未知取值一律拒绝（类型、枚举、状态）。
11. 旧配置缺失 / 损坏抛 `EpisodeContractError`（门禁跑不起来），畸形计划只出报告
    （计划是用户内容，阻断是结论而不是崩溃）。配置校验在看计划之前完成：四个冻结
    状态必须齐全，缺一个就抛——否则"抛不抛"会取决于计划恰好落在哪个档位。
    注入的 `policy` 与磁盘上的政策受同一套规则约束（`0 < overage_review_ratio < 1`、
    statuses 不重复且四档齐全）：调用方传一份放宽的自造政策就能把 §13 的 20% 暂停
    静默关掉，那等于门禁可被参数绕过。注入的 `model_capabilities` 同理按磁盘文档校验。
12. 形态报告有界：坏字段清单按文档顺序去重输出，上限 `_MAX_INVALID_FIELDS` 条，
    超出部分以 `<+N more>` 结尾并如实计数。大计划（十万片段）因此既不会拖成平方级
    也不会撑爆输出；片段数本身不设上限（设计 §7.4）。
12. 数字必须是「可发布的数」：有限且能表示为双精度浮点。`json.loads` 可以从合法
    JSON 里造出超过浮点范围的大整数（`10**400`），它既不是可用时长，也会让下游
    `float()` 崩掉；这类值在形态阶段就拒绝，门禁因此永远不会抛 `OverflowError`。
13. 文本必须是「可落盘的文本」：非空白且可 UTF-8 编码。孤立代理项是合法 JSON，
    却写不进 `prompts/GNNN.md` 与 `duration-manifest.json`，同样在形态阶段拒绝。
14. 调用契约：`plan` 是 `json.loads` 产出的普通 `dict`。刻意让 `__getitem__` 抛异常
    的 dict 子类不在威胁模型内——那种调用方连读取都无法被可靠服务，门禁不替它兜底。
15. 本门禁**看不到剧本正文**：`script_target_seconds` 是计划自己的申报值。发布路径
    必须把剧本头部解析出的权威目标传进来（`authoritative_target_seconds`），
    与申报值不符记 `SCRIPT_TARGET_MISMATCH`；不传则该检查关闭（向后兼容，但那条
    攻击——把 120 秒剧本的目标改写成 68 秒再按 68 秒规划——就仍然可以通过）。
    同理，`actual_generated_seconds` / `final_edit_seconds` 是**申报的实测值**：本模块
    只保证三个层级不互相冒充（`NOT_RUN` 必须配 null、`COMPLETED` 必须有正的实测数、
    成片超过真实生成媒体只告警），真实媒体是否存在由 Task 9/10/11 的证据链负责。

本模块消费 `versioning.load_duration_policy()`、`duration_planner.effective_clip_range()`
与 `config/model-capabilities.json`，不新增长度常量或第二份时长政策。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from duration_planner import (  # noqa: F401  (INFEASIBLE_CLIP_RANGE 转由本模块对外提供)
    INFEASIBLE_CLIP_RANGE,
    InfeasibleClipRangeError,
    effective_clip_range,
)
from versioning import DURATION_STATUSES, VersionValidationError, load_duration_policy

EPISODE_PLAN_SCHEMA_VERSION = 1
CONFIG_UNAVAILABLE = "CONFIG_UNAVAILABLE"

STATUS_PASS_EXACT = "PASS_EXACT"
STATUS_PASS_WITH_TRIM = "PASS_WITH_TRIM"
STATUS_REVIEW_REQUIRED = "REVIEW_REQUIRED"
STATUS_FAIL = "FAIL"

PLAN_SCHEMA_INVALID = "PLAN_SCHEMA_INVALID"
MISSING_SCRIPT_TARGET = "MISSING_SCRIPT_TARGET"
SCRIPT_TARGET_MISMATCH = "SCRIPT_TARGET_MISMATCH"
UNKNOWN_MODEL = "UNKNOWN_MODEL"
PLANNED_SECONDS_MISMATCH = "PLANNED_SECONDS_MISMATCH"
PLANNED_DURATION_UNDERSHOOT = "PLANNED_DURATION_UNDERSHOOT"
OVERAGE_REVIEW_REQUIRED = "OVERAGE_REVIEW_REQUIRED"
TIMELINE_GAP = "TIMELINE_GAP"
TIMELINE_OVERLAP = "TIMELINE_OVERLAP"
TIMELINE_ORDER = "TIMELINE_ORDER"
CLIP_DURATION_OUT_OF_RANGE = "CLIP_DURATION_OUT_OF_RANGE"
SOURCE_REF_UNKNOWN = "SOURCE_REF_UNKNOWN"
SOURCE_COVERAGE_GAP = "SOURCE_COVERAGE_GAP"
SOURCE_COVERAGE_OVERLAP = "SOURCE_COVERAGE_OVERLAP"
SOURCE_UNIT_OVERLAP = "SOURCE_UNIT_OVERLAP"
SYSTEM_TRANSITION_UNDECLARED = "SYSTEM_TRANSITION_UNDECLARED"
SYSTEM_TRANSITION_DANGLING = "SYSTEM_TRANSITION_DANGLING"
RENDER_STATUS_MISMATCH = "RENDER_STATUS_MISMATCH"

REAL_RENDER_NOT_RUN = "REAL_RENDER_NOT_RUN"
DURATION_TRIMMING_REQUIRED = "DURATION_TRIMMING_REQUIRED"
NO_SAFE_TRIM_POSITION = "NO_SAFE_TRIM_POSITION"
SYSTEM_TRANSITIONS_ADDED = "SYSTEM_TRANSITIONS_ADDED"
FINAL_EDIT_NOT_MEASURED = "FINAL_EDIT_NOT_MEASURED"
FINAL_EDIT_EXCEEDS_GENERATED = "FINAL_EDIT_EXCEEDS_GENERATED"

SOURCE_KIND_DIALOGUE = "dialogue"
SOURCE_KIND_ACTION = "action"
SOURCE_KIND_CLUE = "clue"
SOURCE_KIND_STATE_CHANGE = "state_change"
SOURCE_KIND_SYSTEM_TRANSITION = "system_transition"
SOURCE_UNIT_KINDS = (
    SOURCE_KIND_DIALOGUE,
    SOURCE_KIND_ACTION,
    SOURCE_KIND_CLUE,
    SOURCE_KIND_STATE_CHANGE,
    SOURCE_KIND_SYSTEM_TRANSITION,
)

RENDER_STATUS_NOT_RUN = "NOT_RUN"
RENDER_STATUS_COMPLETED = "COMPLETED"
RENDER_STATUS_FAILED = "FAILED"
RENDER_STATUSES = (RENDER_STATUS_NOT_RUN, RENDER_STATUS_COMPLETED, RENDER_STATUS_FAILED)

# FAIL 级发现：任何一条出现，计划都不可交付，时长档位不再参与结论。
# 形态门禁（`PLAN_SCHEMA_INVALID` / `MISSING_SCRIPT_TARGET`）不在表内：它们在
# `validate_episode_plan` 的第一阶段就直接产出 FAIL 报告，根本走不到档位判定。
_FAIL_CLASS = frozenset(
    {
        UNKNOWN_MODEL,
        INFEASIBLE_CLIP_RANGE,
        PLANNED_SECONDS_MISMATCH,
        PLANNED_DURATION_UNDERSHOOT,
        SCRIPT_TARGET_MISMATCH,
        TIMELINE_GAP,
        TIMELINE_OVERLAP,
        TIMELINE_ORDER,
        CLIP_DURATION_OUT_OF_RANGE,
        SOURCE_REF_UNKNOWN,
        SOURCE_COVERAGE_GAP,
        SOURCE_COVERAGE_OVERLAP,
        SOURCE_UNIT_OVERLAP,
        SYSTEM_TRANSITION_UNDECLARED,
        SYSTEM_TRANSITION_DANGLING,
        RENDER_STATUS_MISMATCH,
    }
)

_RENDERABLE_STATUSES = (STATUS_PASS_EXACT, STATUS_PASS_WITH_TRIM)
_TOP_LEVEL_STRINGS = ("episode_id", "workflow", "model")
_CLIP_STRINGS = ("start_state", "end_state", "prompt_text")
_EPSILON = 1e-9

# 形态报告里的坏字段清单上限：十万条路径既没人能逐条修，也会把门禁输出撑爆。
# 超出部分以 `<+N more>` 结尾，数量仍如实计数。
_MAX_INVALID_FIELDS = 50


class EpisodeContractError(RuntimeError):
    """Raised when the gate itself cannot run (policy/model config unusable)."""


@dataclass(frozen=True)
class TrimCandidate:
    """One marked safe trim position, kept for the ledger and the manifest."""

    clip_id: str
    at_seconds: float
    reason: str


@dataclass(frozen=True)
class ValidationReport:
    """Machine verdict for one episode plan.

    ``planned_prompt_seconds`` is always the sum of the clip durations (the ground
    truth); a differing declared value shows up as ``PLANNED_SECONDS_MISMATCH``.

    The two duplicate lists keep their meanings apart: ``duplicate_source_units``
    are sources whose characters a clip claimed twice, ``overlapping_source_units``
    are same-kind sources whose *declared* ranges overlap (同一段剧本内容被声明两次).
    """

    status: str
    error_codes: Tuple[str, ...]
    warning_codes: Tuple[str, ...]
    may_render_full_pack: bool
    script_target_seconds: Optional[float]
    planned_prompt_seconds: Optional[float]
    overage_seconds: Optional[float]
    overage_ratio: Optional[float]
    clip_range_seconds: Optional[Tuple[int, int]]
    timeline_contiguous: bool
    coverage_ratio: float
    missing_source_units: Tuple[str, ...]
    duplicate_source_units: Tuple[str, ...]
    overlapping_source_units: Tuple[str, ...]
    trim_candidates: Tuple[TrimCandidate, ...]
    system_added_transitions: Tuple[str, ...]
    invalid_fields: Tuple[str, ...]


class _Codes:
    """Ordered, de-duplicated code list."""

    def __init__(self) -> None:
        self.items: List[str] = []

    def add(self, code: str) -> None:
        if code not in self.items:
            self.items.append(code)


class _FieldIssues:
    """Ordered, size-capped list of offending plan paths.

    No de-duplication is needed: every call site reports a given path at most once
    (each check either reports its own path or returns). Insertion and the cap test
    are O(1), so a plan with tens of thousands of broken clips stays linear, and the
    report itself is capped — a 100 000-entry field list is neither actionable nor
    readable. The cap announces itself as ``<+N more>``.
    """

    def __init__(self) -> None:
        self.items: List[str] = []
        self._overflow = 0

    def __call__(self, path: str) -> None:
        if len(self.items) < _MAX_INVALID_FIELDS:
            self.items.append(path)
        else:
            self._overflow += 1

    def finish(self) -> List[str]:
        if not self._overflow:
            return self.items
        return self.items + [f"<+{self._overflow} more>"]


def _is_finite_number(value: Any) -> bool:
    """True for a number the gate can publish: finite, and representable as a float.

    An integer wider than the double range (``10**400``, which ``json.loads``
    happily produces from valid JSON) used to reach ``float()`` deeper in the
    module and raise ``OverflowError`` — a gate crash instead of a verdict. Such
    a number is not a usable duration, so it is refused at the shape stage.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    try:
        float(value)
    except OverflowError:
        return False
    return True


def _is_whole_number(value: Any) -> bool:
    return _is_finite_number(value) and float(value).is_integer()


def _is_non_negative_whole(value: Any) -> bool:
    return _is_whole_number(value) and value >= 0


def _is_positive_whole(value: Any) -> bool:
    return _is_whole_number(value) and value >= 1


def _is_text(value: Any) -> bool:
    """A string that can be written to a UTF-8 artifact (blank allowed)."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_content_string(value: Any) -> bool:
    """Non-blank text that can be written to a UTF-8 artifact.

    A lone surrogate is a valid JSON string but cannot be encoded to UTF-8, so a
    plan carrying one could never be rendered into ``prompts/GNNN.md`` or
    ``duration-manifest.json``; it is refused here instead of crashing the renderer.
    """
    return _is_text(value) and bool(value.strip())


def load_model_capabilities(repo_root: Optional[str] = None) -> Dict[str, Any]:
    """Load ``config/model-capabilities.json`` strictly, with no built-in fallback.

    Unlike the legacy loader in :mod:`router` (which silently degrades to a
    hard-coded table), a missing or corrupted file raises
    :class:`EpisodeContractError`: a gate that cannot read the model table cannot
    judge the clip range, and guessing it would be the exact defect this work
    order exists to remove.

    Args:
        repo_root: Skill root path (default: inferred from this file's location).

    Returns:
        The parsed capabilities document (containing ``"models"``).

    Raises:
        EpisodeContractError: If the file is missing, unreadable, not JSON, or not
            shaped as a capabilities document.
    """
    if repo_root is None:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    path = os.path.join(repo_root, "config", "model-capabilities.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError as exc:
        raise EpisodeContractError(
            f"{CONFIG_UNAVAILABLE}: 读取不到模型能力配置 {path}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise EpisodeContractError(
            f"{CONFIG_UNAVAILABLE}: 模型能力配置无法解析 {path}: {exc}"
        ) from exc
    return _validate_capabilities_document(document)


def validate_episode_plan(
    plan: Any,
    model_capabilities: Optional[dict] = None,
    policy: Optional[dict] = None,
    authoritative_target_seconds: Optional[float] = None,
) -> ValidationReport:
    """Judge one episode plan against the duration contract and 来源覆盖.

    Args:
        plan: The parsed ``episode-plan.json`` document.
        model_capabilities: Full ``config/model-capabilities.json`` document;
            defaults to a strict on-disk read.
        policy: Duration policy document; defaults to ``load_duration_policy()``.
            An injected document is held to the same rules as the on-disk one.
        authoritative_target_seconds: The target parsed from the screenplays own
            header (``script_duration.parse_script_duration``). This module cannot
            read the script, so a plan that lowers ``script_target_seconds`` and
            plans to the lowered number would otherwise pass; the publish path
            MUST pass the parsed value here, and a differing declaration is
            refused with ``SCRIPT_TARGET_MISMATCH``.

    Returns:
        ValidationReport: Verdict, machine-readable codes, and the numbers a
        ledger or manifest must publish. A malformed plan is refused with
        ``PLAN_SCHEMA_INVALID`` rather than raising.

    Raises:
        EpisodeContractError: If the policy, the model-capability configuration,
            or the supplied authoritative target is unusable — i.e. the gate
            itself cannot run.
    """
    resolved_policy = _require_policy(policy)
    capabilities = _require_capabilities(model_capabilities)
    expected_target = _require_authoritative_target(authoritative_target_seconds)

    target_state, invalid_fields = _shape_issues(plan)
    if invalid_fields or target_state == "missing":
        codes = _Codes()
        if invalid_fields:
            codes.add(PLAN_SCHEMA_INVALID)
        if target_state == "missing":
            codes.add(MISSING_SCRIPT_TARGET)
        return _refused_report(codes.items, invalid_fields)

    return _evaluate(plan, capabilities, resolved_policy, expected_target)


def _require_authoritative_target(value: Any) -> Optional[float]:
    """Validate the caller-supplied script target (``None`` keeps it unchecked)."""
    if value is None:
        return None
    if isinstance(value, bool) or not _is_finite_number(value) or value <= 0:
        raise EpisodeContractError(
            f"{CONFIG_UNAVAILABLE}: authoritative_target_seconds 必须是正的有限秒数，得到 {value!r}"
        )
    return float(value)


def _refused_report(codes: List[str], invalid_fields: List[str]) -> ValidationReport:
    """A plan refused before any number could be trusted."""
    return ValidationReport(
        status=STATUS_FAIL,
        error_codes=tuple(codes),
        warning_codes=(),
        may_render_full_pack=False,
        script_target_seconds=None,
        planned_prompt_seconds=None,
        overage_seconds=None,
        overage_ratio=None,
        clip_range_seconds=None,
        timeline_contiguous=False,
        coverage_ratio=0.0,
        missing_source_units=(),
        duplicate_source_units=(),
        overlapping_source_units=(),
        trim_candidates=(),
        system_added_transitions=(),
        invalid_fields=tuple(invalid_fields),
    )


def _evaluate(
    plan: dict, capabilities: dict, policy: dict, expected_target: Optional[float]
) -> ValidationReport:
    clips = plan["clips"]
    units = plan["source_units"]
    target = float(plan["script_target_seconds"])
    planned = sum(clip["duration_seconds"] for clip in clips)

    errors = _Codes()
    warnings = _Codes()

    clip_range: Optional[Tuple[int, int]] = None
    model_range = _model_duration_range(capabilities, plan["model"])
    if model_range is None:
        errors.add(UNKNOWN_MODEL)
    else:
        try:
            clip_range = effective_clip_range(model_range[0], model_range[1], policy)
        except InfeasibleClipRangeError:
            errors.add(INFEASIBLE_CLIP_RANGE)

    if abs(plan["planned_prompt_seconds"] - planned) > _EPSILON:
        errors.add(PLANNED_SECONDS_MISMATCH)

    if expected_target is not None and abs(target - expected_target) > _EPSILON:
        # 剧本声明的目标时长是唯一权威：既不许多报，也不许把目标改小再按小目标规划。
        errors.add(SCRIPT_TARGET_MISMATCH)

    duration_status, duration_code = _duration_verdict(target, planned, policy)
    if duration_code is not None:
        errors.add(duration_code)
    if duration_status == STATUS_PASS_WITH_TRIM:
        warnings.add(DURATION_TRIMMING_REQUIRED)

    contiguous = _check_timeline(clips, errors)
    if clip_range is not None:
        _check_clip_durations(clips, clip_range, errors)
    missing, duplicated, overlapping, coverage_ratio = _check_coverage(clips, units, errors)
    transitions = _check_transitions(clips, units, errors, warnings)
    _check_honesty(plan, errors, warnings)

    trim_candidates = tuple(
        TrimCandidate(clip["id"], trim["at_seconds"], trim["reason"])
        for clip in clips
        for trim in clip["trim_candidates"]
    )
    if duration_status == STATUS_PASS_WITH_TRIM and not trim_candidates:
        warnings.add(NO_SAFE_TRIM_POSITION)

    status = STATUS_FAIL if any(code in _FAIL_CLASS for code in errors.items) else duration_status
    return ValidationReport(
        status=status,
        error_codes=tuple(errors.items),
        warning_codes=tuple(warnings.items),
        may_render_full_pack=status in _RENDERABLE_STATUSES,
        script_target_seconds=target,
        planned_prompt_seconds=planned,
        overage_seconds=planned - target,
        overage_ratio=(planned - target) / target,
        clip_range_seconds=clip_range,
        timeline_contiguous=contiguous,
        coverage_ratio=coverage_ratio,
        missing_source_units=tuple(missing),
        duplicate_source_units=tuple(duplicated),
        overlapping_source_units=tuple(overlapping),
        trim_candidates=trim_candidates,
        system_added_transitions=tuple(transitions),
        invalid_fields=(),
    )


def _duration_verdict(target: float, planned: float, policy: dict) -> Tuple[str, Optional[str]]:
    """设计 §5：`P < T` 失败、`P = T` 精确、`T < P <= 1.2T` 需裁剪、再高则暂停。"""
    if planned < target - _EPSILON:
        return STATUS_FAIL, PLANNED_DURATION_UNDERSHOOT
    if planned <= target + _EPSILON:
        return STATUS_PASS_EXACT, None
    if planned <= target * (1.0 + policy["overage_review_ratio"]) + _EPSILON:
        return STATUS_PASS_WITH_TRIM, None
    return STATUS_REVIEW_REQUIRED, OVERAGE_REVIEW_REQUIRED


def _check_timeline(clips: List[dict], errors: _Codes) -> bool:
    """设计 §12.1：时间线必须从 0 开始、逐片连续、不倒序。"""
    contiguous = True
    expected = 0
    previous_start: Optional[float] = None
    for index, clip in enumerate(clips):
        start = clip["start_seconds"]
        duration = clip["duration_seconds"]
        if start > expected:
            errors.add(TIMELINE_GAP)
            contiguous = False
        elif start < expected:
            if index and previous_start is not None and start < previous_start:
                errors.add(TIMELINE_ORDER)
            else:
                errors.add(TIMELINE_OVERLAP)
            contiguous = False
        previous_start = start
        expected = start + duration
    return contiguous


def _check_clip_durations(clips: List[dict], clip_range: Tuple[int, int], errors: _Codes) -> None:
    low, high = clip_range
    for clip in clips:
        if not low <= clip["duration_seconds"] <= high:
            errors.add(CLIP_DURATION_OUT_OF_RANGE)
            return


def _check_coverage(
    clips: List[dict], units: List[dict], errors: _Codes
) -> Tuple[List[str], List[str], float]:
    """设计 §11：来源 100% 覆盖、无无故重复、分片要留下字符范围。"""
    kinds = {unit["id"]: unit["kind"] for unit in units}
    spans = {
        unit["id"]: (unit["span_start"], unit["span_end"])
        for unit in units
        if unit["kind"] != SOURCE_KIND_SYSTEM_TRANSITION
    }
    declaration_ids: set[str] = set(_overlapping_units(units))
    claimed: Dict[str, List[Tuple[int, int]]] = {unit_id: [] for unit_id in spans}
    out_of_bounds: List[str] = []
    for clip in clips:
        for ref in clip["source_refs"]:
            unit_id = ref["source_id"]
            kind = kinds.get(unit_id)
            if kind is None:
                errors.add(SOURCE_REF_UNKNOWN)
                continue
            if kind == SOURCE_KIND_SYSTEM_TRANSITION:
                errors.add(SYSTEM_TRANSITION_UNDECLARED)
                continue
            span_start, span_end = spans[unit_id]
            start, end = ref["span_start"], ref["span_end"]
            if start < span_start or end > span_end:
                out_of_bounds.append(unit_id)
            clipped = (max(start, span_start), min(end, span_end))
            if clipped[0] < clipped[1]:
                claimed[unit_id].append(clipped)

    missing_ids: set[str] = set()
    clip_duplicate_ids: set[str] = set()
    covered_total = 0
    declared_total = 0
    for unit_id, (span_start, span_end) in spans.items():
        unit_length = span_end - span_start
        declared_total += unit_length
        intervals = claimed[unit_id]
        union = _union_length(intervals)
        covered_total += union
        if union < unit_length:
            missing_ids.add(unit_id)
        if sum(end - start for start, end in intervals) > union or unit_id in out_of_bounds:
            clip_duplicate_ids.add(unit_id)

    # 报告按来源声明顺序输出：清单与台账要可复现，不能随检查内部顺序漂移。
    ordered = [unit["id"] for unit in units if unit["id"] in spans]
    missing = [unit_id for unit_id in ordered if unit_id in missing_ids]
    duplicated = [unit_id for unit_id in ordered if unit_id in clip_duplicate_ids]
    overlapping = [unit_id for unit_id in ordered if unit_id in declaration_ids]
    if missing:
        errors.add(SOURCE_COVERAGE_GAP)
    if clip_duplicate_ids:
        errors.add(SOURCE_COVERAGE_OVERLAP)
    if overlapping:
        errors.add(SOURCE_UNIT_OVERLAP)
    ratio = covered_total / declared_total if declared_total else 0.0
    return missing, duplicated, overlapping, ratio


def _overlapping_units(units: List[dict]) -> List[str]:
    """Design §11 的"剧情被无故重复"，在**声明层**的同类型范围内判定。

    两个同类型来源声明了互相重叠的剧本字符范围，就意味着同一段剧本内容会出现两次
    ——即使每个来源各自只被引用一次（那种重复由 `SOURCE_COVERAGE_OVERLAP` 负责）。

    只对"要被演出来"的两类内容判定：`dialogue`（台词）与 `action`（动作）。`clue`
    与 `state_change` 的字符范围是**指针**而非被消费的文本（一句话里可以出现两个
    状态变化，它们指向同一句的相邻子串），把它们判成重复只会误阻断忠实计划。
    跨类型重叠同样合法：同一段字符可以既承载台词又承载状态变化。
    """
    blocked_kinds = (SOURCE_KIND_DIALOGUE, SOURCE_KIND_ACTION)
    grouped: Dict[str, List[Tuple[int, int, str]]] = {}
    for unit in units:
        if unit["kind"] not in blocked_kinds:
            continue
        grouped.setdefault(unit["kind"], []).append((unit["span_start"], unit["span_end"], unit["id"]))

    overlapped: set[str] = set()
    for ranges in grouped.values():
        active: List[Tuple[int, str]] = []
        for start, end, unit_id in sorted(ranges):
            active = [item for item in active if item[0] > start]
            if active:
                overlapped.add(unit_id)
                overlapped.update(item[1] for item in active)
            active.append((end, unit_id))
    return [unit["id"] for unit in units if unit["id"] in overlapped]


def _union_length(intervals: List[Tuple[int, int]]) -> int:
    total = 0
    cursor = None
    for start, end in sorted(intervals):
        if cursor is None or start > cursor:
            total += end - start
        elif end > cursor:
            total += end - cursor
        cursor = end if cursor is None else max(cursor, end)
    return total


def _check_transitions(
    clips: List[dict], units: List[dict], errors: _Codes, warnings: _Codes
) -> List[str]:
    """设计 §11：系统补充过渡必须登记在独立通道，不能伪装成剧本原文。"""
    kinds = {unit["id"]: unit["kind"] for unit in units}
    collected: List[str] = []
    for clip in clips:
        for transition_id in clip["system_added_transitions"]:
            if kinds.get(transition_id) != SOURCE_KIND_SYSTEM_TRANSITION:
                errors.add(SYSTEM_TRANSITION_DANGLING)
            elif transition_id not in collected:
                collected.append(transition_id)
    if collected:
        warnings.add(SYSTEM_TRANSITIONS_ADDED)
    return collected


def _check_honesty(plan: dict, errors: _Codes, warnings: _Codes) -> None:
    """设计 §10.4 / §12.1：计划时长不得冒充真实生成或成片时长。"""
    status = plan["real_model_render_status"]
    actual = plan["actual_generated_seconds"]
    final = plan["final_edit_seconds"]
    if status == RENDER_STATUS_COMPLETED:
        if not (_is_finite_number(actual) and actual > 0):
            errors.add(RENDER_STATUS_MISMATCH)
        if final is None:
            warnings.add(FINAL_EDIT_NOT_MEASURED)
        elif not (_is_finite_number(final) and final > 0):
            errors.add(RENDER_STATUS_MISMATCH)
        elif _is_finite_number(actual) and actual > 0 and final > actual:
            # 成片比真实生成媒体还长：可能是重剪 / 补拍 / 加了片头片尾，也可能只是
            # 把计划值抄进了两个字段。门禁分不清，所以只告警并要求证据，不阻断。
            warnings.add(FINAL_EDIT_EXCEEDS_GENERATED)
        return
    if actual is not None or final is not None:
        errors.add(RENDER_STATUS_MISMATCH)
    if status == RENDER_STATUS_NOT_RUN:
        warnings.add(REAL_RENDER_NOT_RUN)


def _shape_issues(plan: Any) -> Tuple[str, List[str]]:
    """Validate declared field shapes.

    Returns:
        ``(target_state, invalid_fields)`` where ``target_state`` is ``"ok"``,
        ``"missing"`` (absent, null or zero target) or ``"invalid"``; and
        ``invalid_fields`` lists the offending plan paths in document order,
        capped at ``_MAX_INVALID_FIELDS`` entries plus a ``<+N more>`` marker.
    """
    if not isinstance(plan, dict):
        return "invalid", ["<root>"]

    bad = _FieldIssues()

    if "schema_version" in plan:
        version = plan["schema_version"]
        if isinstance(version, bool) or version != EPISODE_PLAN_SCHEMA_VERSION:
            bad("schema_version")

    task_state = _target_state(plan.get("script_target_seconds"))
    if task_state == "invalid":
        bad("script_target_seconds")
    if task_state == "missing":
        pass  # 有自己的 MISSING_SCRIPT_TARGET 结论，不算形态错误

    for name in _TOP_LEVEL_STRINGS:
        if not _is_content_string(plan.get(name)):
            bad(name)

    render_status = plan.get("real_model_render_status")
    if not isinstance(render_status, str) or render_status not in RENDER_STATUSES:
        bad("real_model_render_status")

    planned = plan.get("planned_prompt_seconds")
    if not _is_finite_number(planned) or planned < 0:
        bad("planned_prompt_seconds")

    for name in ("actual_generated_seconds", "final_edit_seconds"):
        if name not in plan:
            bad(name)
            continue
        value = plan[name]
        if value is None:
            continue
        if not _is_finite_number(value) or value < 0:
            bad(name)

    units = plan.get("source_units")
    if not isinstance(units, list) or not units:
        bad("source_units")
    else:
        seen_units: set[str] = set()
        for index, unit in enumerate(units):
            path = f"source_units[{index}]"
            if not isinstance(unit, dict):
                bad(path)
                continue
            unit_id = unit.get("id")
            if not _is_content_string(unit_id):
                bad(f"{path}.id")
            elif unit_id in seen_units:
                bad(f"{path}.id")
            else:
                seen_units.add(unit_id)
            kind = unit.get("kind")
            if kind not in SOURCE_UNIT_KINDS:
                bad(f"{path}.kind")
            if "text" in unit and not _is_text(unit["text"]):
                bad(f"{path}.text")
            if "scene_id" in unit and not _is_content_string(unit["scene_id"]):
                bad(f"{path}.scene_id")
            _check_unit_span(unit, path, kind, bad)

    clips = plan.get("clips")
    if not isinstance(clips, list):
        bad("clips")
        return task_state, bad.finish()
    seen_clips: set[str] = set()
    for index, clip in enumerate(clips):
        path = f"clips[{index}]"
        if not isinstance(clip, dict):
            bad(path)
            continue
        clip_id = clip.get("id")
        if not _is_content_string(clip_id):
            bad(f"{path}.id")
        elif clip_id in seen_clips:
            bad(f"{path}.id")
        else:
            seen_clips.add(clip_id)
        if not _is_non_negative_whole(clip.get("start_seconds")):
            bad(f"{path}.start_seconds")
        duration = clip.get("duration_seconds")
        if not _is_positive_whole(duration):
            bad(f"{path}.duration_seconds")
        refs = clip.get("source_refs")
        if not isinstance(refs, list) or not refs:
            bad(f"{path}.source_refs")
        else:
            for ref_index, ref in enumerate(refs):
                ref_path = f"{path}.source_refs[{ref_index}]"
                if not isinstance(ref, dict):
                    bad(ref_path)
                    continue
                if not _is_content_string(ref.get("source_id")):
                    bad(f"{ref_path}.source_id")
                _check_pair_bounds(
                    ref.get("span_start"), ref.get("span_end"), ref_path, bad
                )
        for name in _CLIP_STRINGS:
            if not _is_content_string(clip.get(name)):
                bad(f"{path}.{name}")
        transitions = clip.get("system_added_transitions")
        if not isinstance(transitions, list) or not all(
            _is_content_string(item) for item in transitions
        ):
            bad(f"{path}.system_added_transitions")
        trims = clip.get("trim_candidates")
        if not isinstance(trims, list):
            bad(f"{path}.trim_candidates")
        else:
            for trim_index, trim in enumerate(trims):
                trim_path = f"{path}.trim_candidates[{trim_index}]"
                if not isinstance(trim, dict):
                    bad(trim_path)
                    continue
                at = trim.get("at_seconds")
                if (
                    not _is_finite_number(at)
                    or at < 0
                    or not _is_positive_whole(duration)
                    or at >= duration
                ):
                    bad(f"{trim_path}.at_seconds")
                if not _is_content_string(trim.get("reason")):
                    bad(f"{trim_path}.reason")
    return task_state, bad.finish()


def _target_state(value: Any) -> str:
    """``"ok"`` / ``"missing"`` (absent, null or zero) / ``"invalid"``."""
    if value is None:
        return "missing"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "invalid"
    if not _is_finite_number(value):
        return "invalid"
    if value == 0:
        return "missing"
    if value < 0:
        return "invalid"
    return "ok"


def _check_unit_span(unit: dict, path: str, kind: Any, bad) -> None:
    """Content units must carry a script character range; transitions must not.

    A ``system_transition`` unit has no script characters, so any ``span_*`` it
    carries is inert metadata: validating an inert range would only refuse plans
    that model the transition as a zero-length insertion point.
    """
    if kind == SOURCE_KIND_SYSTEM_TRANSITION:
        return
    start, end = unit.get("span_start"), unit.get("span_end")
    if start is None:
        bad(f"{path}.span_start")
    if end is None:
        bad(f"{path}.span_end")
    if start is None or end is None:
        return
    _check_pair_bounds(start, end, path, bad)


def _check_pair_bounds(start: Any, end: Any, path: str, bad) -> None:
    if not _is_non_negative_whole(start):
        bad(f"{path}.span_start")
        return
    if not _is_non_negative_whole(end):
        bad(f"{path}.span_end")
        return
    if end <= start:
        bad(f"{path}.span_end")


def _require_policy(policy: Optional[dict]) -> dict:
    """Resolve and validate the duration policy, without inventing defaults."""
    if policy is None:
        try:
            policy = load_duration_policy()
        except VersionValidationError as exc:
            raise EpisodeContractError(
                f"{CONFIG_UNAVAILABLE}: 时长政策不可用：{exc}"
            ) from exc
    if not isinstance(policy, dict):
        raise EpisodeContractError(
            f"{CONFIG_UNAVAILABLE}: policy 必须是 dict，得到 {type(policy).__name__}"
        )
    clip = policy.get("clip_seconds")
    if not isinstance(clip, dict) or set(clip) != {"min", "max"}:
        raise EpisodeContractError(
            f'{CONFIG_UNAVAILABLE}: policy["clip_seconds"] 必须恰好含 min/max'
        )
    for name in ("min", "max"):
        value = clip[name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EpisodeContractError(
                f'{CONFIG_UNAVAILABLE}: policy["clip_seconds"]["{name}"] 必须是正整数秒，'
                f"得到 {value!r}"
            )
    if clip["min"] > clip["max"]:
        raise EpisodeContractError(
            f'{CONFIG_UNAVAILABLE}: policy["clip_seconds"] 下限大于上限'
        )
    ratio = policy.get("overage_review_ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        raise EpisodeContractError(
            f'{CONFIG_UNAVAILABLE}: policy["overage_review_ratio"] 必须是数值，得到 {ratio!r}'
        )
    if not _is_finite_number(ratio) or not 0 < ratio < 1:
        # 与 versioning._validate_duration_policy_document 同一条边界：放宽到 >= 1 会让
        # REVIEW_REQUIRED 永远不可达，等于把设计 §13 的 20% 暂停静默关掉。
        raise EpisodeContractError(
            f'{CONFIG_UNAVAILABLE}: policy["overage_review_ratio"] 必须满足 0 < ratio < 1，'
            f"得到 {ratio!r}"
        )
    statuses = policy.get("statuses")
    if (
        not isinstance(statuses, list)
        or not statuses
        or not all(_is_content_string(status) for status in statuses)
    ):
        raise EpisodeContractError(
            f'{CONFIG_UNAVAILABLE}: policy["statuses"] 必须是非空的时长状态白名单'
        )
    if len(set(statuses)) != len(statuses):
        raise EpisodeContractError(
            f'{CONFIG_UNAVAILABLE}: policy["statuses"] 含重复状态 {statuses}'
        )
    # 四个冻结状态必须齐全，且必须在**看计划之前**查完：否则"要不要抛异常"会取决于
    # 这份计划恰好落在哪个档位，同一种配置错误变得时灵时不灵。
    missing_statuses = [status for status in DURATION_STATUSES if status not in set(statuses)]
    if missing_statuses:
        raise EpisodeContractError(
            f'{CONFIG_UNAVAILABLE}: policy["statuses"] 缺少必需状态 {sorted(missing_statuses)}'
            f"（得到 {statuses}）"
        )
    return policy


def _require_capabilities(model_capabilities: Optional[dict]) -> dict:
    if model_capabilities is None:
        return load_model_capabilities()
    return _validate_capabilities_document(model_capabilities)


def _validate_capabilities_document(document: Any) -> dict:
    if not isinstance(document, dict) or not isinstance(document.get("models"), dict):
        raise EpisodeContractError(
            f'{CONFIG_UNAVAILABLE}: 模型能力配置必须是含 "models" 对象的文档'
        )
    return document


def _model_duration_range(capabilities: dict, model: Any) -> Optional[Tuple[int, int]]:
    """Return the model's clip range, or ``None`` when the model is not registered."""
    entry = capabilities["models"].get(model)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise EpisodeContractError(
            f"{CONFIG_UNAVAILABLE}: 模型能力条目 {model!r} 不是对象"
        )
    duration = entry.get("duration_seconds")
    if not isinstance(duration, dict):
        raise EpisodeContractError(
            f'{CONFIG_UNAVAILABLE}: 模型 {model!r} 缺少 duration_seconds 对象'
        )
    low, high = duration.get("min"), duration.get("max")
    for name, value in (("min", low), ("max", high)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EpisodeContractError(
                f"{CONFIG_UNAVAILABLE}: 模型 {model!r} 的 duration_seconds[{name}] "
                f"必须是正整数秒，得到 {value!r}"
            )
    if low > high:
        raise EpisodeContractError(
            f"{CONFIG_UNAVAILABLE}: 模型 {model!r} 的 duration_seconds 下限大于上限"
        )
    return low, high
