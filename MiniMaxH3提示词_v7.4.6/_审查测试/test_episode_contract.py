"""Task 5 guard：整集计划 Schema 与覆盖率门禁。

覆盖八层：
1. 工单 Step 1 冻结样例逐条保留（`validate_episode_plan()` 不传能力/政策时接到真实配置）；
2. 时长合同：`P < T` 失败、`P == T` 精确、`T < P <= 1.2T` 需裁剪、`P > 1.2T` 暂停且不渲染完整包；
3. 政策与模型能力同源：`overage_review_ratio` / `statuses` / `clip_seconds` 与
   `config/model-capabilities.json` 的目标模型时长范围都必须被真正消费（改配置即改判定）；
4. 时间线与片段时长：空档 / 重叠 / 倒序 / 越界一律阻断，有效范围 = 政策 ∩ 模型能力；
5. 来源覆盖：按字符范围证明逐字覆盖、无遗漏、无重复；长台词允许分片但不允许整句重复；
6. 诚实字段：`real_model_render_status` 与真实媒体实测时长不得互相冒充（设计 §10.4 / §12.1）；
7. Schema 文件与实现同源：`templates/episode-plan.schema.json` 的必填项、枚举、schema_version 与模块常量逐一对齐；
8. Fail-Closed：任何畸形计划只返回 `FAIL` 报告而不抛异常；配置缺失 / 损坏才抛 `EpisodeContractError`。

断言一律用 `self.assertEqual` 等不可被 `python -O` 剥离的形式。

运行（目录名不是合法 Python 标识符，禁止 `-m unittest` 模块路径）：

    python "minimax-h3-craft/_审查测试/test_episode_contract.py" -v
"""
from __future__ import annotations

import copy
import dataclasses
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import episode_contract  # noqa: E402
from duration_planner import KIND_DIALOGUE, Beat, pack_beats  # noqa: E402
from episode_contract import (  # noqa: E402
    CLIP_DURATION_OUT_OF_RANGE,
    CONFIG_UNAVAILABLE,
    DURATION_TRIMMING_REQUIRED,
    EPISODE_PLAN_SCHEMA_VERSION,
    FINAL_EDIT_EXCEEDS_GENERATED,
    FINAL_EDIT_NOT_MEASURED,
    INFEASIBLE_CLIP_RANGE,
    MISSING_SCRIPT_TARGET,
    NO_SAFE_TRIM_POSITION,
    OVERAGE_REVIEW_REQUIRED,
    PLAN_SCHEMA_INVALID,
    PLANNED_DURATION_UNDERSHOOT,
    PLANNED_SECONDS_MISMATCH,
    SCRIPT_TARGET_MISMATCH,
    REAL_RENDER_NOT_RUN,
    RENDER_STATUS_COMPLETED,
    RENDER_STATUS_FAILED,
    RENDER_STATUS_MISMATCH,
    RENDER_STATUS_NOT_RUN,
    RENDER_STATUSES,
    SOURCE_COVERAGE_GAP,
    SOURCE_COVERAGE_OVERLAP,
    SOURCE_KIND_ACTION,
    SOURCE_KIND_CLUE,
    SOURCE_KIND_DIALOGUE,
    SOURCE_KIND_STATE_CHANGE,
    SOURCE_KIND_SYSTEM_TRANSITION,
    SOURCE_REF_UNKNOWN,
    SOURCE_UNIT_KINDS,
    SOURCE_UNIT_OVERLAP,
    STATUS_FAIL,
    STATUS_PASS_EXACT,
    STATUS_PASS_WITH_TRIM,
    STATUS_REVIEW_REQUIRED,
    SYSTEM_TRANSITION_DANGLING,
    SYSTEM_TRANSITION_UNDECLARED,
    SYSTEM_TRANSITIONS_ADDED,
    TIMELINE_GAP,
    TIMELINE_ORDER,
    TIMELINE_OVERLAP,
    UNKNOWN_MODEL,
    EpisodeContractError,
    TrimCandidate,
    ValidationReport,
    load_model_capabilities,
    validate_episode_plan,
)
from script_duration import parse_script_duration  # noqa: E402
from versioning import DURATION_STATUSES, VersionValidationError, load_duration_policy  # noqa: E402

POLICY = load_duration_policy()
MODEL_CAPABILITIES = load_model_capabilities()
SCHEMA_PATH = SKILL_ROOT / "templates" / "episode-plan.schema.json"

WORKFLOW = "native-drama-series"
START_STATE = "主角站在雪地边缘，面朝镜头，双手插兜。"
END_STATE = "主角向前迈出两步，视线压低，围巾被风吹起。"
PROMPT_TEXT = "【角色声音签名】本段无对白。【画面】雪地，主角拉紧围巾向前走。"


def make_ref(source_id, span_start, span_end):
    return {"source_id": source_id, "span_start": span_start, "span_end": span_end}


def make_clip(clip_id, start, duration, refs, **overrides):
    clip = {
        "id": clip_id,
        "start_seconds": start,
        "duration_seconds": duration,
        "source_refs": list(refs),
        "start_state": START_STATE,
        "end_state": END_STATE,
        "system_added_transitions": [],
        "trim_candidates": [],
        "prompt_text": PROMPT_TEXT,
    }
    clip.update(overrides)
    return clip


def make_unit(unit_id, span_start, span_end, kind=SOURCE_KIND_DIALOGUE, **overrides):
    unit = {"id": unit_id, "kind": kind, "span_start": span_start, "span_end": span_end}
    unit.update(overrides)
    return unit


def with_clip(clip, **changes):
    updated = dict(clip)
    updated.update(changes)
    return updated


_UNSET = object()


def base_plan(plan_clips, plan_units, target=_UNSET, planned=_UNSET, **overrides):
    total = sum(clip["duration_seconds"] for clip in plan_clips)
    document = {
        "episode_id": "EP01",
        "workflow": WORKFLOW,
        "model": "H3",
        "script_target_seconds": total if target is _UNSET else target,
        "source_units": list(plan_units),
        "clips": list(plan_clips),
        "planned_prompt_seconds": total if planned is _UNSET else planned,
        "real_model_render_status": RENDER_STATUS_NOT_RUN,
        "actual_generated_seconds": None,
        "final_edit_seconds": None,
    }
    document.update(overrides)
    return document


def plan(target=120, durations=(10,), **overrides):
    """A shape-valid plan whose single source unit is covered exactly by the clips.

    The source span equals the clip seconds one-to-one (1 字/秒 只是夹具口径，不代表
    语速），这样每个用例只考察它自己要考察的判定，覆盖与时间线默认都是干净的。
    """
    clips = []
    cursor = 0
    start = 0
    for index, duration in enumerate(durations, start=1):
        clips.append(
            make_clip(f"G{index:03d}", start, duration, [make_ref("S001", cursor, cursor + duration)])
        )
        cursor += duration
        start += duration
    return base_plan(clips, [make_unit("S001", 0, max(cursor, 1))], target=target, **overrides)


def plan_with_missing_and_overlap():
    """S001 少一段并被重复引用，S002 尾部未覆盖。"""
    clips = [
        make_clip("G001", 0, 5, [make_ref("S001", 0, 5)]),
        make_clip("G002", 5, 5, [make_ref("S001", 3, 8)]),
        make_clip("G003", 10, 10, [make_ref("S002", 10, 18)]),
    ]
    units = [make_unit("S001", 0, 10), make_unit("S002", 10, 20)]
    return base_plan(clips, units)


def plan_with_gap_and_three_second_clip():
    """时间线在 10—12 秒留空，首片仅 3 秒。"""
    clips = [
        make_clip("G001", 0, 3, [make_ref("S001", 0, 3)]),
        make_clip("G002", 3, 7, [make_ref("S001", 3, 10)]),
        make_clip("G003", 12, 8, [make_ref("S001", 10, 18)]),
    ]
    return base_plan(clips, [make_unit("S001", 0, 18)])


def policy_with(**changes):
    document = json.loads(json.dumps(POLICY))
    document.update(changes)
    return document


def capabilities_with(**models):
    document = json.loads(json.dumps(MODEL_CAPABILITIES))
    document["models"].update(models)
    return document


class WorkOrderSpecTest(unittest.TestCase):
    """工单 Task 5 Step 1 的五个冻结样例。"""

    def test_120_second_plan_cannot_shrink_to_68(self):
        report = validate_episode_plan(plan(target=120, durations=[5, 15, 12, 13, 15, 8]))
        self.assertEqual(report.status, "FAIL")
        self.assertIn("PLANNED_DURATION_UNDERSHOOT", report.error_codes)

    def test_exact_and_trim_statuses(self):
        self.assertEqual(validate_episode_plan(plan(120, [10] * 12)).status, "PASS_EXACT")
        self.assertEqual(validate_episode_plan(plan(120, [10] * 13)).status, "PASS_WITH_TRIM")

    def test_over_twenty_percent_pauses(self):
        report = validate_episode_plan(plan(120, [10] * 15))
        self.assertEqual(report.status, "REVIEW_REQUIRED")
        self.assertIs(report.may_render_full_pack, False)

    def test_missing_and_duplicate_source_spans_fail(self):
        report = validate_episode_plan(plan_with_missing_and_overlap())
        self.assertTrue({"SOURCE_COVERAGE_GAP", "SOURCE_COVERAGE_OVERLAP"} <= set(report.error_codes))

    def test_timeline_gap_and_out_of_range_clip_fail(self):
        report = validate_episode_plan(plan_with_gap_and_three_second_clip())
        self.assertTrue({"TIMELINE_GAP", "CLIP_DURATION_OUT_OF_RANGE"} <= set(report.error_codes))

    def test_the_two_broken_fixtures_have_no_other_findings(self):
        """两个反例夹具只考察它们各自的缺陷，避免"顺手带上别的错误码"掩盖真实原因。"""
        missing = validate_episode_plan(plan_with_missing_and_overlap())
        self.assertEqual(set(missing.error_codes), {SOURCE_COVERAGE_GAP, SOURCE_COVERAGE_OVERLAP})
        self.assertEqual(missing.status, STATUS_FAIL)
        timeline = validate_episode_plan(plan_with_gap_and_three_second_clip())
        self.assertEqual(set(timeline.error_codes), {TIMELINE_GAP, CLIP_DURATION_OUT_OF_RANGE})


class ReportShapeTest(unittest.TestCase):
    """报告的返回形态是冻结契约：字段名、不可变性、元组化错误码。"""

    REPORT_FIELDS = (
        "status",
        "error_codes",
        "warning_codes",
        "may_render_full_pack",
        "script_target_seconds",
        "planned_prompt_seconds",
        "overage_seconds",
        "overage_ratio",
        "clip_range_seconds",
        "timeline_contiguous",
        "coverage_ratio",
        "missing_source_units",
        "duplicate_source_units",
        "overlapping_source_units",
        "trim_candidates",
        "system_added_transitions",
        "invalid_fields",
    )

    def test_report_is_a_frozen_dataclass_with_exact_fields(self):
        self.assertTrue(dataclasses.is_dataclass(ValidationReport))
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(ValidationReport)), self.REPORT_FIELDS
        )
        report = validate_episode_plan(plan(120, [10] * 12))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.status = "PASS_EXACT"  # type: ignore[misc]

    def test_clean_report_carries_no_findings(self):
        report = validate_episode_plan(plan(120, [10] * 12))
        self.assertEqual(report.status, STATUS_PASS_EXACT)
        self.assertEqual(report.error_codes, ())
        self.assertEqual(report.warning_codes, (REAL_RENDER_NOT_RUN,))
        self.assertIs(report.may_render_full_pack, True)
        self.assertEqual(report.script_target_seconds, 120)
        self.assertEqual(report.planned_prompt_seconds, 120)
        self.assertEqual(report.overage_seconds, 0.0)
        self.assertEqual(report.overage_ratio, 0.0)
        self.assertEqual(report.clip_range_seconds, (4, 10))
        self.assertIs(report.timeline_contiguous, True)
        self.assertEqual(report.coverage_ratio, 1.0)
        self.assertEqual(report.missing_source_units, ())
        self.assertEqual(report.duplicate_source_units, ())
        self.assertEqual(report.overlapping_source_units, ())
        self.assertEqual(report.trim_candidates, ())
        self.assertEqual(report.system_added_transitions, ())
        self.assertEqual(report.invalid_fields, ())

    def test_report_is_json_safe(self):
        report = validate_episode_plan(plan_with_missing_and_overlap())
        payload = json.loads(json.dumps(dataclasses.asdict(report), ensure_ascii=False))
        self.assertEqual(payload["status"], STATUS_FAIL)
        self.assertEqual(payload["missing_source_units"], ["S001", "S002"])
        self.assertEqual(payload["duplicate_source_units"], ["S001"])
        self.assertEqual(payload["error_codes"], [SOURCE_COVERAGE_GAP, SOURCE_COVERAGE_OVERLAP])

    def test_plans_are_plain_json_documents(self):
        document = plan(120, [10] * 12)
        round_tripped = json.loads(json.dumps(document, ensure_ascii=False))
        self.assertEqual(validate_episode_plan(round_tripped).status, STATUS_PASS_EXACT)


class DurationVerdictTest(unittest.TestCase):
    """设计 §5 时长合同的四档判定与边界。"""

    def test_undershoot_is_fail(self):
        report = validate_episode_plan(plan(120, [10] * 11))
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertEqual(report.error_codes, (PLANNED_DURATION_UNDERSHOOT,))
        self.assertIs(report.may_render_full_pack, False)
        self.assertEqual(report.planned_prompt_seconds, 110)
        self.assertEqual(report.overage_seconds, -10.0)
        self.assertEqual(report.overage_ratio, -10 / 120)

    def test_equal_total_is_exact(self):
        report = validate_episode_plan(plan(120, [10] * 12))
        self.assertEqual(report.status, STATUS_PASS_EXACT)
        self.assertIs(report.may_render_full_pack, True)

    def test_any_overage_up_to_twenty_percent_needs_trim_not_review(self):
        report = validate_episode_plan(plan(120, [10] * 13))
        self.assertEqual(report.status, STATUS_PASS_WITH_TRIM)
        self.assertEqual(report.error_codes, ())
        self.assertIn(DURATION_TRIMMING_REQUIRED, report.warning_codes)
        self.assertIs(report.may_render_full_pack, True)
        self.assertEqual(report.overage_seconds, 10.0)
        self.assertAlmostEqual(report.overage_ratio, 10 / 120)

    def test_exactly_twenty_percent_is_still_trim(self):
        report = validate_episode_plan(plan(100, [10] * 12))
        self.assertEqual(report.planned_prompt_seconds, 120)
        self.assertEqual(report.status, STATUS_PASS_WITH_TRIM)

    def test_above_twenty_percent_requires_review(self):
        report = validate_episode_plan(plan(120, [10] * 15))
        self.assertEqual(report.status, STATUS_REVIEW_REQUIRED)
        self.assertEqual(report.error_codes, (OVERAGE_REVIEW_REQUIRED,))
        self.assertIs(report.may_render_full_pack, False)
        self.assertEqual(report.overage_seconds, 30.0)

    def test_empty_clips_cannot_meet_a_target(self):
        report = validate_episode_plan(plan(120, []))
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertIn(PLANNED_DURATION_UNDERSHOOT, report.error_codes)
        self.assertEqual(report.planned_prompt_seconds, 0)
        self.assertIs(report.may_render_full_pack, False)

    def test_declared_total_must_equal_the_clip_sum(self):
        report = validate_episode_plan(plan(120, [10] * 12, planned_prompt_seconds=130))
        self.assertIn(PLANNED_SECONDS_MISMATCH, report.error_codes)
        self.assertEqual(report.planned_prompt_seconds, 120)
        self.assertEqual(report.status, STATUS_FAIL)


class AuthoritativeTargetTest(unittest.TestCase):
    """剧本声明的目标时长是唯一权威：发布路径必须把它传进来交叉核对。"""

    def test_the_frozen_two_argument_call_stays_valid(self):
        """工单冻结的三参数接口不因新增可选参数而失效（不传则不做交叉核对）。"""
        self.assertEqual(validate_episode_plan(plan(68, [10] * 6 + [8])).status, STATUS_PASS_EXACT)

    def test_a_matching_authoritative_target_is_accepted(self):
        report = validate_episode_plan(
            plan(120, [10] * 12),
            MODEL_CAPABILITIES,
            POLICY,
            authoritative_target_seconds=120,
        )
        self.assertEqual(report.error_codes, ())
        self.assertEqual(report.status, STATUS_PASS_EXACT)

    def test_a_lowered_declared_target_is_refused(self):
        """120 秒剧本改报 68 秒：本模块看不到剧本，但拿得到权威值就必须拦住。"""
        report = validate_episode_plan(
            plan(68, [10] * 6 + [8]),
            MODEL_CAPABILITIES,
            POLICY,
            authoritative_target_seconds=parse_script_duration("时长：120秒\n").target_seconds,
        )
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertIn(SCRIPT_TARGET_MISMATCH, report.error_codes)
        self.assertIs(report.may_render_full_pack, False)

    def test_a_raised_declared_target_is_also_refused(self):
        report = validate_episode_plan(
            plan(240, [10] * 24),
            MODEL_CAPABILITIES,
            POLICY,
            authoritative_target_seconds=120,
        )
        self.assertIn(SCRIPT_TARGET_MISMATCH, report.error_codes)

    def test_a_non_positive_authoritative_target_is_a_config_error(self):
        for value in (0, -1, "120", True, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(EpisodeContractError) as caught:
                    validate_episode_plan(
                        plan(120, [10] * 12),
                        MODEL_CAPABILITIES,
                        POLICY,
                        authoritative_target_seconds=value,
                    )
                self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_the_two_argument_integration_call_matches_the_mismatch_case(self):
        """与冻结样例同样的夹具，加上权威目标后必须由 PASS 变 FAIL。"""
        document = plan(68, [10] * 6 + [8], planned_prompt_seconds=68)
        self.assertEqual(validate_episode_plan(document).status, STATUS_PASS_EXACT)
        report = validate_episode_plan(
            document, MODEL_CAPABILITIES, POLICY, authoritative_target_seconds=120
        )
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertIn(SCRIPT_TARGET_MISMATCH, report.error_codes)

    def test_the_verdict_comes_from_the_clips_not_the_declared_field(self):
        """120 秒剧本 68 秒素材 + 谎报总量：两个缺陷各自留名，缩水不因谎报消失。"""
        report = validate_episode_plan(
            plan(120, [5, 15, 12, 13, 15, 8], planned_prompt_seconds=120)
        )
        self.assertIn(PLANNED_SECONDS_MISMATCH, report.error_codes)
        self.assertIn(PLANNED_DURATION_UNDERSHOOT, report.error_codes)
        self.assertEqual(report.status, STATUS_FAIL)

    def test_fractional_declared_total_is_tolerated_only_when_it_matches(self):
        report = validate_episode_plan(plan(120, [10] * 12, planned_prompt_seconds=120.0))
        self.assertEqual(report.status, STATUS_PASS_EXACT)
        report = validate_episode_plan(plan(120, [10] * 12, planned_prompt_seconds=120.5))
        self.assertIn(PLANNED_SECONDS_MISMATCH, report.error_codes)


class PolicyConsumptionTest(unittest.TestCase):
    """`overage_review_ratio` / `statuses` / `clip_seconds` 必须来自政策文件。"""

    def test_review_ratio_is_read_from_the_policy(self):
        strict = policy_with(overage_review_ratio=0.05)
        self.assertEqual(
            validate_episode_plan(plan(120, [10] * 13), MODEL_CAPABILITIES, strict).status,
            STATUS_REVIEW_REQUIRED,
        )
        loose = policy_with(overage_review_ratio=0.5)
        self.assertEqual(
            validate_episode_plan(plan(120, [10] * 18), MODEL_CAPABILITIES, loose).status,
            STATUS_PASS_WITH_TRIM,
        )

    def test_missing_review_ratio_is_a_config_error(self):
        broken = policy_with()
        broken.pop("overage_review_ratio")
        with self.assertRaises(EpisodeContractError) as caught:
            validate_episode_plan(plan(120, [10] * 12), MODEL_CAPABILITIES, broken)
        self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_statuses_must_cover_the_verdict(self):
        broken = policy_with(statuses=[STATUS_FAIL, STATUS_PASS_EXACT, STATUS_REVIEW_REQUIRED])
        with self.assertRaises(EpisodeContractError) as caught:
            validate_episode_plan(plan(120, [10] * 13), MODEL_CAPABILITIES, broken)
        self.assertIn(STATUS_PASS_WITH_TRIM, str(caught.exception))

    def test_a_short_status_list_is_refused_before_the_plan_is_read(self):
        """配置错误不许"时灵时不灵"：同一份坏政策配任何计划都该同样抛错。"""
        broken = policy_with(statuses=[STATUS_PASS_EXACT, STATUS_PASS_WITH_TRIM])
        for document in (plan(120, [10] * 12), plan(120, [10] * 11), {"whatever": 1}):
            with self.subTest(document=repr(document)[:40]):
                with self.assertRaises(EpisodeContractError) as caught:
                    validate_episode_plan(document, MODEL_CAPABILITIES, broken)
                self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_the_four_status_names_match_the_frozen_policy_constant(self):
        self.assertEqual(
            set(DURATION_STATUSES),
            {STATUS_PASS_EXACT, STATUS_PASS_WITH_TRIM, STATUS_REVIEW_REQUIRED, STATUS_FAIL},
        )

    def test_every_reported_status_is_in_the_policy_whitelist(self):
        observed = {
            validate_episode_plan(plan(120, [10] * 12)).status,
            validate_episode_plan(plan(120, [10] * 13)).status,
            validate_episode_plan(plan(120, [10] * 15)).status,
            validate_episode_plan(plan(120, [10] * 11)).status,
        }
        self.assertTrue(observed <= set(POLICY["statuses"]))
        self.assertEqual(observed, {STATUS_PASS_EXACT, STATUS_PASS_WITH_TRIM, STATUS_REVIEW_REQUIRED, STATUS_FAIL})

    def test_clip_bounds_come_from_the_policy(self):
        narrow = policy_with(clip_seconds={"min": 6, "max": 8})
        report = validate_episode_plan(plan(120, [10] * 12), MODEL_CAPABILITIES, narrow)
        self.assertEqual(report.clip_range_seconds, (6, 8))
        self.assertIn(CLIP_DURATION_OUT_OF_RANGE, report.error_codes)

    def test_malformed_policy_is_refused_instead_of_defaulted(self):
        for broken in (policy_with(clip_seconds={"min": 4}), policy_with(statuses=[]), [("min", 4)]):
            with self.assertRaises(EpisodeContractError):
                validate_episode_plan(plan(120, [10] * 12), MODEL_CAPABILITIES, broken)


class ModelCapabilityTest(unittest.TestCase):
    """有效片段范围 = 政策 ∩ 目标模型能力，"目标模型"必须逐个生效。"""

    def test_range_is_the_intersection_of_policy_and_model(self):
        report = validate_episode_plan(plan(120, [10] * 12))
        self.assertEqual(report.clip_range_seconds, (4, 10))

    def test_a_narrower_model_lifts_the_floor(self):
        report = validate_episode_plan(plan(120, [4] * 30, model="H3-Max"))
        self.assertEqual(report.clip_range_seconds, (5, 10))
        self.assertIn(CLIP_DURATION_OUT_OF_RANGE, report.error_codes)

    def test_a_wider_model_does_not_widen_the_policy(self):
        capabilities = capabilities_with(H3={"duration_seconds": {"min": 1, "max": 30}})
        report = validate_episode_plan(plan(120, [10] * 12), capabilities)
        self.assertEqual(report.clip_range_seconds, (4, 10))

    def test_unknown_model_is_named_and_blocks(self):
        report = validate_episode_plan(plan(120, [10] * 12, model="H9"))
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertEqual(report.error_codes, (UNKNOWN_MODEL,))
        self.assertIsNone(report.clip_range_seconds)
        self.assertIs(report.may_render_full_pack, False)

    def test_model_without_a_duration_range_is_a_config_error(self):
        capabilities = capabilities_with(H3={"modes": {}})
        with self.assertRaises(EpisodeContractError) as caught:
            validate_episode_plan(plan(120, [10] * 12), capabilities)
        self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_capabilities_document_shape_is_enforced(self):
        for broken in ({}, {"models": []}, {"models": {"H3": 4}}, ["H3"], "H3"):
            with self.assertRaises(EpisodeContractError):
                validate_episode_plan(plan(120, [10] * 12), broken)

    def test_empty_intersection_is_named_neither_silently_widened_nor_crashed(self):
        capabilities = capabilities_with(H3={"duration_seconds": {"min": 11, "max": 15}})
        report = validate_episode_plan(plan(120, [10] * 12), capabilities)
        self.assertIn(INFEASIBLE_CLIP_RANGE, report.error_codes)
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertIsNone(report.clip_range_seconds)
        self.assertNotIn(CLIP_DURATION_OUT_OF_RANGE, report.error_codes)


class TimelineTest(unittest.TestCase):
    """设计 §12.1：计划时间线空档、重叠、倒序一律阻断。"""

    def test_contiguous_timeline_passes(self):
        report = validate_episode_plan(plan(30, [10, 10, 10]))
        self.assertIs(report.timeline_contiguous, True)
        self.assertEqual(set(report.error_codes) - {REAL_RENDER_NOT_RUN}, set())

    def test_a_hole_in_the_timeline_is_a_gap(self):
        report = validate_episode_plan(plan_with_gap_and_three_second_clip())
        self.assertIn(TIMELINE_GAP, report.error_codes)
        self.assertIs(report.timeline_contiguous, False)

    def test_a_late_start_of_the_first_clip_is_a_gap(self):
        document = plan(30, [10, 10, 10])
        document["clips"][0]["start_seconds"] = 2
        report = validate_episode_plan(document)
        self.assertIn(TIMELINE_GAP, report.error_codes)

    def test_a_one_second_gap_is_still_a_gap(self):
        """空档没有"可忽略的 1 秒"：任何非连续起点都必须出码。"""
        document = plan(30, [10, 10, 10])
        document["clips"][1]["start_seconds"] = 11
        document["clips"][2]["start_seconds"] = 21
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, (TIMELINE_GAP,))
        self.assertIs(report.timeline_contiguous, False)

    def test_a_one_second_overlap_is_still_an_overlap(self):
        document = plan(30, [10, 10, 10])
        document["clips"][1]["start_seconds"] = 9
        document["clips"][2]["start_seconds"] = 19
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, (TIMELINE_OVERLAP,))
        self.assertIs(report.timeline_contiguous, False)

    def test_an_early_start_is_an_overlap(self):
        document = plan(30, [10, 10, 10])
        document["clips"][1]["start_seconds"] = 5
        document["clips"][2]["start_seconds"] = 15
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, (TIMELINE_OVERLAP,))
        self.assertIs(report.timeline_contiguous, False)

    def test_going_backwards_is_an_order_violation(self):
        document = plan(30, [10, 10, 10])
        document["clips"][2]["start_seconds"] = 5
        report = validate_episode_plan(document)
        self.assertIn(TIMELINE_ORDER, report.error_codes)
        self.assertIs(report.timeline_contiguous, False)

    def test_negative_start_is_a_shape_error(self):
        document = plan(30, [10, 10, 10])
        document["clips"][0]["start_seconds"] = -1
        report = validate_episode_plan(document)
        self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)
        self.assertIn("clips[0].start_seconds", report.invalid_fields)


class ClipRangeTest(unittest.TestCase):
    """每个片段都必须在有效范围内（设计 §7.4 / §12.1）。"""

    def test_every_clip_inside_the_range_passes(self):
        report = validate_episode_plan(plan(50, [4, 10, 5, 10, 4, 7, 10]))
        self.assertEqual(report.status, STATUS_PASS_EXACT)
        self.assertEqual(report.error_codes, ())

    def test_three_second_clip_is_out_of_range(self):
        document = plan(11, [4, 10, 4])
        document["clips"][1] = with_clip(document["clips"][1], duration_seconds=3, start_seconds=4)
        document["clips"][2] = with_clip(document["clips"][2], start_seconds=7)
        document["planned_prompt_seconds"] = 11
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, (CLIP_DURATION_OUT_OF_RANGE,))

    def test_eleven_second_clip_exceeds_the_policy_max(self):
        report = validate_episode_plan(plan(22, [11, 11]))
        self.assertIn(CLIP_DURATION_OUT_OF_RANGE, report.error_codes)

    def test_fifteen_second_clip_is_out_of_range_even_though_the_model_allows_it(self):
        report = validate_episode_plan(plan(120, [15, 15, 15, 15, 15, 15, 15, 15]))
        self.assertIn(CLIP_DURATION_OUT_OF_RANGE, report.error_codes)

    def test_zero_second_clip_is_a_shape_error(self):
        document = plan(20, [10, 10])
        document["clips"][1] = with_clip(document["clips"][1], duration_seconds=0)
        report = validate_episode_plan(document)
        self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)
        self.assertIn("clips[1].duration_seconds", report.invalid_fields)

    def test_fractional_clip_duration_is_a_shape_error(self):
        document = plan(20, [10, 10])
        document["clips"][1] = with_clip(document["clips"][1], duration_seconds=9.5)
        report = validate_episode_plan(document)
        self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)


class SourceCoverageTest(unittest.TestCase):
    """设计 §11：来源 100% 覆盖、无无故重复、长台词分片要记录字符范围。"""

    def test_full_coverage_passes(self):
        report = validate_episode_plan(plan(20, [10, 10]))
        self.assertEqual(report.error_codes, ())
        self.assertEqual(report.coverage_ratio, 1.0)

    def test_an_unreferenced_unit_is_missing(self):
        document = plan(20, [10, 10])
        document["source_units"].append(make_unit("S002", 20, 30))
        report = validate_episode_plan(document)
        self.assertIn(SOURCE_COVERAGE_GAP, report.error_codes)
        self.assertEqual(report.missing_source_units, ("S002",))
        self.assertAlmostEqual(report.coverage_ratio, 20 / 30)

    def test_a_partially_covered_unit_is_missing(self):
        document = plan(20, [10, 10])
        document["clips"][1] = with_clip(document["clips"][1], source_refs=[make_ref("S001", 10, 15)])
        report = validate_episode_plan(document)
        self.assertIn(SOURCE_COVERAGE_GAP, report.error_codes)
        self.assertEqual(report.missing_source_units, ("S001",))
        self.assertAlmostEqual(report.coverage_ratio, 0.75)

    def test_the_same_characters_claimed_twice_are_an_overlap(self):
        document = plan_with_missing_and_overlap()
        report = validate_episode_plan(document)
        self.assertIn(SOURCE_COVERAGE_OVERLAP, report.error_codes)
        self.assertEqual(report.duplicate_source_units, ("S001",))
        self.assertAlmostEqual(report.coverage_ratio, 16 / 20)

    def test_repeating_a_whole_line_in_two_clips_is_an_overlap(self):
        """同一句完整重复两次：不是"分片"，是剧情重复（两个片段引用同一段字符）。"""
        document = base_plan(
            [
                make_clip("G001", 0, 10, [make_ref("S001", 0, 10)]),
                make_clip("G002", 10, 10, [make_ref("S001", 0, 10)]),
            ],
            [make_unit("S001", 0, 10)],
        )
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, (SOURCE_COVERAGE_OVERLAP,))
        self.assertEqual(report.duplicate_source_units, ("S001",))
        self.assertEqual(report.missing_source_units, ())
        self.assertEqual(report.coverage_ratio, 1.0)

    def test_a_long_line_split_across_two_clips_is_legal(self):
        """设计 §11：长台词跨片段允许同 ID 分片，只要字符范围接得上。"""
        document = base_plan(
            [
                make_clip("G001", 0, 10, [make_ref("S001", 0, 12)]),
                make_clip("G002", 10, 8, [make_ref("S001", 12, 20)]),
            ],
            [make_unit("S001", 0, 20)],
        )
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, ())
        self.assertEqual(report.coverage_ratio, 1.0)
        self.assertEqual(report.duplicate_source_units, ())

    def test_a_ref_beyond_the_unit_span_is_an_overlap(self):
        document = base_plan(
            [make_clip("G001", 0, 12, [make_ref("S001", 0, 12)])],
            [make_unit("S001", 0, 10)],
        )
        report = validate_episode_plan(document)
        self.assertIn(SOURCE_COVERAGE_OVERLAP, report.error_codes)
        self.assertEqual(report.duplicate_source_units, ("S001",))

    def test_a_single_uncovered_character_is_still_a_gap(self):
        """逐字覆盖没有"可忽略的 1 个字"：漏一个字就是漏。"""
        document = base_plan(
            [
                make_clip("G001", 0, 10, [make_ref("S001", 0, 9)]),
                make_clip("G002", 10, 9, [make_ref("S001", 10, 18)]),
            ],
            [make_unit("S001", 0, 18)],
        )
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, (SOURCE_COVERAGE_GAP,))
        self.assertEqual(report.missing_source_units, ("S001",))
        self.assertAlmostEqual(report.coverage_ratio, 17 / 18)

    def test_two_units_declaring_the_same_characters_are_a_duplicate(self):
        """同一段剧本字符被两个同类型来源声明：每个各被引用一次也依然是剧情重复。"""
        document = base_plan(
            [
                make_clip("G001", 0, 10, [make_ref("S001", 0, 10)]),
                make_clip("G002", 10, 10, [make_ref("S002", 5, 15)]),
            ],
            [make_unit("S001", 0, 10), make_unit("S002", 5, 15)],
        )
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, (SOURCE_UNIT_OVERLAP,))
        self.assertEqual(report.overlapping_source_units, ("S001", "S002"))
        self.assertEqual(report.duplicate_source_units, ())
        self.assertEqual(report.missing_source_units, ())
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertIs(report.may_render_full_pack, False)

    def test_two_units_with_identical_characters_are_a_duplicate(self):
        document = base_plan(
            [
                make_clip("G001", 0, 10, [make_ref("S001", 0, 10)]),
                make_clip("G002", 10, 10, [make_ref("S002", 0, 10)]),
            ],
            [make_unit("S001", 0, 10), make_unit("S002", 0, 10)],
        )
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, (SOURCE_UNIT_OVERLAP,))
        self.assertEqual(report.overlapping_source_units, ("S001", "S002"))

    def test_overlapping_pointer_kinds_are_tolerated(self):
        """clue / state_change 的范围是指向剧本子串的指针，不是被消费的文本。"""
        for kind in (SOURCE_KIND_CLUE, SOURCE_KIND_STATE_CHANGE):
            with self.subTest(kind=kind):
                document = base_plan(
                    [
                        make_clip("G001", 0, 10, [make_ref("S001", 0, 10), make_ref("P1", 2, 6)]),
                        make_clip("G002", 10, 10, [make_ref("P2", 4, 8)]),
                    ],
                    [
                        make_unit("S001", 0, 10),
                        make_unit("P1", 2, 6, kind=kind),
                        make_unit("P2", 4, 8, kind=kind),
                    ],
                )
                report = validate_episode_plan(document)
                self.assertEqual(report.error_codes, ())
                self.assertEqual(report.overlapping_source_units, ())

    def test_overlapping_action_units_are_a_duplicate(self):
        document = base_plan(
            [
                make_clip("G001", 0, 10, [make_ref("A1", 0, 10)]),
                make_clip("G002", 10, 10, [make_ref("A2", 6, 16)]),
            ],
            [
                make_unit("A1", 0, 10, kind=SOURCE_KIND_ACTION),
                make_unit("A2", 6, 16, kind=SOURCE_KIND_ACTION),
            ],
        )
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, (SOURCE_UNIT_OVERLAP,))
        self.assertEqual(report.overlapping_source_units, ("A1", "A2"))

    def test_a_clip_level_duplicate_stays_a_coverage_overlap(self):
        """两种重复不许混进同一个字段：引用层归 duplicate，声明层归 overlapping。"""
        document = base_plan(
            [
                make_clip("G001", 0, 10, [make_ref("S001", 0, 5), make_ref("S001", 3, 10)]),
            ],
            [make_unit("S001", 0, 10)],
        )
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, (SOURCE_COVERAGE_OVERLAP,))
        self.assertEqual(report.duplicate_source_units, ("S001",))
        self.assertEqual(report.overlapping_source_units, ())

    def test_different_kinds_may_share_characters(self):
        """同一段字符可以既"说了什么"（台词）又"发生了什么"（状态变化）。"""
        document = base_plan(
            [
                make_clip("G001", 0, 10, [make_ref("S001", 0, 10), make_ref("X1", 5, 10)]),
                make_clip("G002", 10, 10, [make_ref("S001", 10, 20)]),
            ],
            [
                make_unit("S001", 0, 20),
                make_unit("X1", 5, 10, kind=SOURCE_KIND_STATE_CHANGE),
            ],
        )
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, ())
        self.assertEqual(report.coverage_ratio, 1.0)

    def test_adjacent_units_are_not_overlapping(self):
        document = base_plan(
            [
                make_clip("G001", 0, 10, [make_ref("S001", 0, 10)]),
                make_clip("G002", 10, 10, [make_ref("S002", 10, 20)]),
            ],
            [make_unit("S001", 0, 10), make_unit("S002", 10, 20)],
        )
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, ())

    def test_a_ref_to_an_unknown_unit_is_named(self):
        document = plan(20, [10, 10])
        document["clips"][1] = with_clip(document["clips"][1], source_refs=[make_ref("S999", 10, 20)])
        report = validate_episode_plan(document)
        self.assertIn(SOURCE_REF_UNKNOWN, report.error_codes)
        self.assertIn(SOURCE_COVERAGE_GAP, report.error_codes)
        self.assertEqual(report.status, STATUS_FAIL)

    def test_several_units_are_checked_independently(self):
        document = base_plan(
            [
                make_clip("G001", 0, 10, [make_ref("S001", 0, 10)]),
                make_clip("G002", 10, 8, [make_ref("S002", 10, 18)]),
            ],
            [make_unit("S001", 0, 10, kind=SOURCE_KIND_ACTION), make_unit("S002", 10, 20)],
        )
        report = validate_episode_plan(document)
        self.assertEqual(report.missing_source_units, ("S002",))
        self.assertEqual(report.duplicate_source_units, ())
        self.assertAlmostEqual(report.coverage_ratio, 18 / 20)

    def test_an_empty_ref_list_is_a_shape_error(self):
        document = plan(20, [10, 10])
        document["clips"][1] = with_clip(document["clips"][1], source_refs=[])
        report = validate_episode_plan(document)
        self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)
        self.assertIn("clips[1].source_refs", report.invalid_fields)


class SystemTransitionTest(unittest.TestCase):
    """设计 §11：系统补充过渡必须走独立来源类型，不能伪装成剧本原文。"""

    def transition_plan(self, *, declared, refs=None, kind=SOURCE_KIND_SYSTEM_TRANSITION):
        units = [make_unit("S001", 0, 10), {"id": "S900", "kind": kind}]
        clip = make_clip(
            "G001",
            0,
            10,
            [make_ref("S001", 0, 10)] if refs is None else refs,
            system_added_transitions=list(declared),
        )
        return base_plan([clip], units)

    def test_a_declared_transition_is_accepted_and_collected(self):
        report = validate_episode_plan(self.transition_plan(declared=["S900"]))
        self.assertEqual(report.error_codes, ())
        self.assertEqual(report.system_added_transitions, ("S900",))
        self.assertIn(SYSTEM_TRANSITIONS_ADDED, report.warning_codes)

    def test_an_undeclared_transition_id_is_named(self):
        report = validate_episode_plan(self.transition_plan(declared=["S901"]))
        self.assertIn(SYSTEM_TRANSITION_DANGLING, report.error_codes)
        self.assertEqual(report.status, STATUS_FAIL)

    def test_a_transition_id_pointing_at_script_content_is_named(self):
        report = validate_episode_plan(self.transition_plan(declared=["S001"]))
        self.assertIn(SYSTEM_TRANSITION_DANGLING, report.error_codes)

    def test_a_transition_referenced_as_script_content_is_named(self):
        report = validate_episode_plan(
            self.transition_plan(declared=[], refs=[make_ref("S001", 0, 10), make_ref("S900", 0, 1)])
        )
        self.assertIn(SYSTEM_TRANSITION_UNDECLARED, report.error_codes)
        self.assertEqual(report.system_added_transitions, ())

    def test_an_unused_transition_unit_does_not_block(self):
        report = validate_episode_plan(self.transition_plan(declared=[]))
        self.assertEqual(report.error_codes, ())
        self.assertEqual(report.system_added_transitions, ())

    def test_a_transition_unit_needs_no_script_span(self):
        document = self.transition_plan(declared=["S900"])
        self.assertNotIn("span_start", document["source_units"][1])
        self.assertNotIn(PLAN_SCHEMA_INVALID, validate_episode_plan(document).error_codes)

    def test_a_transition_span_is_inert_metadata(self):
        """把过渡写成零长度插点、或写成任意范围，都不该被误阻断（它没有剧本字符）。"""
        for span in ({}, {"span_start": 0, "span_end": 0}, {"span_start": 3, "span_end": 9}):
            with self.subTest(span=span):
                document = self.transition_plan(declared=["S900"])
                document["source_units"][1].update(span)
                report = validate_episode_plan(document)
                self.assertEqual(report.error_codes, ())
                self.assertEqual(report.system_added_transitions, ("S900",))
                self.assertNotIn("span_start", report.invalid_fields)


class RenderHonestyTest(unittest.TestCase):
    """设计 §10.4 / §12.1：计划时长不得冒充真实生成或成片时长。"""

    def test_no_real_render_must_be_marked_not_run_with_nulls(self):
        report = validate_episode_plan(plan(120, [10] * 12))
        self.assertIn(REAL_RENDER_NOT_RUN, report.warning_codes)

    def test_not_run_with_a_number_is_a_mismatch(self):
        report = validate_episode_plan(plan(120, [10] * 12, actual_generated_seconds=120.0))
        self.assertIn(RENDER_STATUS_MISMATCH, report.error_codes)
        self.assertEqual(report.status, STATUS_FAIL)

    def test_not_run_with_a_final_edit_length_is_a_mismatch(self):
        report = validate_episode_plan(plan(120, [10] * 12, final_edit_seconds=118.0))
        self.assertIn(RENDER_STATUS_MISMATCH, report.error_codes)

    def test_failed_render_must_not_claim_media(self):
        report = validate_episode_plan(
            plan(120, [10] * 12, real_model_render_status=RENDER_STATUS_FAILED, actual_generated_seconds=120.0)
        )
        self.assertIn(RENDER_STATUS_MISMATCH, report.error_codes)

    def test_failed_render_with_nulls_is_accepted_without_the_not_run_warning(self):
        report = validate_episode_plan(plan(120, [10] * 12, real_model_render_status=RENDER_STATUS_FAILED))
        self.assertEqual(report.error_codes, ())
        self.assertNotIn(REAL_RENDER_NOT_RUN, report.warning_codes)

    def test_completed_without_measured_seconds_is_a_mismatch(self):
        report = validate_episode_plan(plan(120, [10] * 12, real_model_render_status=RENDER_STATUS_COMPLETED))
        self.assertIn(RENDER_STATUS_MISMATCH, report.error_codes)

    def test_completed_with_zero_seconds_is_a_mismatch(self):
        report = validate_episode_plan(
            plan(
                120,
                [10] * 12,
                real_model_render_status=RENDER_STATUS_COMPLETED,
                actual_generated_seconds=0,
            )
        )
        self.assertIn(RENDER_STATUS_MISMATCH, report.error_codes)

    def test_completed_with_measured_seconds_passes_and_flags_the_missing_final_edit(self):
        report = validate_episode_plan(
            plan(
                120,
                [10] * 12,
                real_model_render_status=RENDER_STATUS_COMPLETED,
                actual_generated_seconds=131.0,
                final_edit_seconds=None,
            )
        )
        self.assertEqual(report.error_codes, ())
        self.assertIn(FINAL_EDIT_NOT_MEASURED, report.warning_codes)

    def test_a_fully_measured_episode_has_no_warnings(self):
        report = validate_episode_plan(
            plan(
                120,
                [10] * 12,
                real_model_render_status=RENDER_STATUS_COMPLETED,
                actual_generated_seconds=131.0,
                final_edit_seconds=129.0,
            )
        )
        self.assertEqual(report.warning_codes, ())

    def test_an_unknown_render_status_is_a_shape_error(self):
        report = validate_episode_plan(plan(120, [10] * 12, real_model_render_status="DONE"))
        self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)
        self.assertIn("real_model_render_status", report.invalid_fields)

    def test_completed_with_a_zero_final_edit_is_a_mismatch(self):
        report = validate_episode_plan(
            plan(
                120,
                [10] * 12,
                real_model_render_status=RENDER_STATUS_COMPLETED,
                actual_generated_seconds=131.0,
                final_edit_seconds=0,
            )
        )
        self.assertIn(RENDER_STATUS_MISMATCH, report.error_codes)

    def test_a_final_edit_longer_than_the_generated_media_is_warned(self):
        """成片比真实生成媒体还长：可能是片头片尾，也可能是照着计划值抄的，只告警。"""
        report = validate_episode_plan(
            plan(
                120,
                [10] * 12,
                real_model_render_status=RENDER_STATUS_COMPLETED,
                actual_generated_seconds=10.0,
                final_edit_seconds=990.0,
            )
        )
        self.assertEqual(report.error_codes, ())
        self.assertIn(FINAL_EDIT_EXCEEDS_GENERATED, report.warning_codes)
        self.assertEqual(report.status, STATUS_PASS_EXACT)

    def test_a_final_edit_within_the_generated_media_is_not_warned(self):
        report = validate_episode_plan(
            plan(
                120,
                [10] * 12,
                real_model_render_status=RENDER_STATUS_COMPLETED,
                actual_generated_seconds=131.0,
                final_edit_seconds=129.0,
            )
        )
        self.assertEqual(report.warning_codes, ())

    def test_a_final_edit_without_a_measured_render_is_not_compared(self):
        """实测生成时长缺失时先报 mismatch：不许拿它去和成片时长比较（会崩）。"""
        report = validate_episode_plan(
            plan(
                120,
                [10] * 12,
                real_model_render_status=RENDER_STATUS_COMPLETED,
                actual_generated_seconds=None,
                final_edit_seconds=129.0,
            )
        )
        self.assertEqual(report.error_codes, (RENDER_STATUS_MISMATCH,))
        self.assertNotIn(FINAL_EDIT_EXCEEDS_GENERATED, report.warning_codes)

    def test_the_gate_sees_only_the_plan_so_task_9_must_cross_check_the_script(self):
        """本门禁看不到剧本正文：把 120 秒剧本的申报目标改写成 68 秒不在它的可见范围内。

        它只能挡住"素材比申报目标还短"；把申报目标本身改小、再按小目标规划，属于
        Task 9 总门禁的职责（重新解析剧本声明并与计划申报值比对）。这条用例把这个
        边界钉住，避免有人误以为 Task 5 已经覆盖了那类攻击。
        """
        report = validate_episode_plan(plan(68, [10] * 6 + [8]))
        self.assertEqual(report.status, STATUS_PASS_EXACT)


class TrimCandidateTest(unittest.TestCase):
    """设计 §8 / §5：裁剪位置只做标记，超 20% 前必须列出候选。"""

    def test_candidates_are_collected_in_clip_order(self):
        document = plan(120, [10] * 13)
        document["clips"][0] = with_clip(
            document["clips"][0],
            trim_candidates=[{"at_seconds": 0.3, "reason": "开场呼吸留白"}, {"at_seconds": 8.0, "reason": "动作余势"}],
        )
        document["clips"][1] = with_clip(
            document["clips"][1], trim_candidates=[{"at_seconds": 9.5, "reason": "定格尾端"}]
        )
        report = validate_episode_plan(document)
        self.assertEqual(
            report.trim_candidates,
            (
                TrimCandidate("G001", 0.3, "开场呼吸留白"),
                TrimCandidate("G001", 8.0, "动作余势"),
                TrimCandidate("G002", 9.5, "定格尾端"),
            ),
        )
        self.assertNotIn(NO_SAFE_TRIM_POSITION, report.warning_codes)

    def test_trimming_without_any_candidate_is_warned(self):
        report = validate_episode_plan(plan(120, [10] * 13))
        self.assertIn(DURATION_TRIMMING_REQUIRED, report.warning_codes)
        self.assertIn(NO_SAFE_TRIM_POSITION, report.warning_codes)

    def test_no_trim_warning_when_nothing_exceeds_the_target(self):
        report = validate_episode_plan(plan(120, [10] * 12))
        self.assertNotIn(DURATION_TRIMMING_REQUIRED, report.warning_codes)
        self.assertNotIn(NO_SAFE_TRIM_POSITION, report.warning_codes)

    def test_a_trim_position_outside_the_clip_is_a_shape_error(self):
        document = plan(20, [10, 10])
        document["clips"][0] = with_clip(
            document["clips"][0], trim_candidates=[{"at_seconds": 10.0, "reason": "越界"}]
        )
        report = validate_episode_plan(document)
        self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)
        self.assertIn("clips[0].trim_candidates[0].at_seconds", report.invalid_fields)

    def test_a_trim_candidate_without_a_reason_is_a_shape_error(self):
        document = plan(20, [10, 10])
        document["clips"][0] = with_clip(
            document["clips"][0], trim_candidates=[{"at_seconds": 1.0, "reason": "  "}]
        )
        report = validate_episode_plan(document)
        self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)


class PlanShapeTest(unittest.TestCase):
    """计划字段一律 Fail-Closed：缺、错类型、错值都点名到具体路径。"""

    def broken_plan(self):
        return plan(120, [10, 10], planned_prompt_seconds=20)

    def assert_shape_error(self, document, path=None):
        report = validate_episode_plan(document)
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)
        self.assertIs(report.may_render_full_pack, False)
        self.assertTrue(report.invalid_fields)
        if path is not None:
            self.assertIn(path, report.invalid_fields)

    def test_top_level_fields_are_required(self):
        for name in (
            "episode_id",
            "workflow",
            "model",
            "source_units",
            "clips",
            "planned_prompt_seconds",
            "real_model_render_status",
            "actual_generated_seconds",
            "final_edit_seconds",
        ):
            document = self.broken_plan()
            document.pop(name)
            self.assert_shape_error(document, name)

    def test_clip_fields_are_required(self):
        for name in (
            "id",
            "start_seconds",
            "duration_seconds",
            "source_refs",
            "start_state",
            "end_state",
            "system_added_transitions",
            "trim_candidates",
            "prompt_text",
        ):
            document = self.broken_plan()
            del document["clips"][1][name]
            self.assert_shape_error(document, f"clips[1].{name}")

    def test_blank_strings_are_not_content(self):
        for name in ("episode_id", "workflow", "model"):
            document = self.broken_plan()
            document[name] = "   "
            self.assert_shape_error(document, name)
        document = self.broken_plan()
        document["clips"][0]["prompt_text"] = " "
        self.assert_shape_error(document, "clips[0].prompt_text")
        document = self.broken_plan()
        document["clips"][0]["start_state"] = ""
        self.assert_shape_error(document, "clips[0].start_state")

    def test_duplicate_ids_are_refused(self):
        document = self.broken_plan()
        document["clips"][1]["id"] = "G001"
        self.assert_shape_error(document, "clips[1].id")
        document = self.broken_plan()
        document["source_units"].append(dict(document["source_units"][0]))
        self.assert_shape_error(document, "source_units[1].id")

    def test_unknown_source_kind_is_refused(self):
        document = self.broken_plan()
        document["source_units"][0]["kind"] = "voiceover"
        self.assert_shape_error(document, "source_units[0].kind")

    def test_spans_must_be_ordered_and_present_for_script_content(self):
        document = self.broken_plan()
        document["source_units"][0]["span_end"] = 0
        self.assert_shape_error(document, "source_units[0].span_end")
        document = self.broken_plan()
        document["source_units"][0].pop("span_start")
        self.assert_shape_error(document, "source_units[0].span_start")
        document = self.broken_plan()
        document["clips"][0]["source_refs"][0]["span_end"] = "10"
        self.assert_shape_error(document, "clips[0].source_refs[0].span_end")

    def test_numbers_must_not_be_booleans_or_strings(self):
        for name, value in (
            ("script_target_seconds", True),
            ("script_target_seconds", "120"),
            ("planned_prompt_seconds", False),
            ("actual_generated_seconds", "131"),
        ):
            document = self.broken_plan()
            document[name] = value
            self.assert_shape_error(document, name)

    def test_non_finite_numbers_are_refused(self):
        for name, value in (
            ("script_target_seconds", float("nan")),
            ("script_target_seconds", float("inf")),
            ("planned_prompt_seconds", float("nan")),
            ("actual_generated_seconds", float("inf")),
        ):
            document = self.broken_plan()
            document[name] = value
            self.assert_shape_error(document, name)

    def test_a_future_schema_version_is_refused(self):
        document = self.broken_plan()
        document["schema_version"] = EPISODE_PLAN_SCHEMA_VERSION + 1
        self.assert_shape_error(document, "schema_version")

    def test_the_current_schema_version_is_accepted(self):
        document = self.broken_plan()
        document["schema_version"] = EPISODE_PLAN_SCHEMA_VERSION
        self.assertNotIn(PLAN_SCHEMA_INVALID, validate_episode_plan(document).error_codes)

    def test_unknown_extra_keys_are_tolerated(self):
        document = self.broken_plan()
        document["notes"] = "制作备注"
        document["clips"][0]["notes"] = "现场备注"
        report = validate_episode_plan(document)
        self.assertNotIn(PLAN_SCHEMA_INVALID, report.error_codes)

    def test_unknown_extra_keys_are_tolerated_even_when_they_hold_absurd_values(self):
        """前向兼容只针对未知键：它们的取值不参与判定，也不得触发形态错误。"""
        document = plan(20, [10, 10])
        document["future_field"] = 10 ** 400
        document["clips"][0]["future_field"] = {"nested": [1, 2, 3]}
        report = validate_episode_plan(document)
        self.assertNotIn(PLAN_SCHEMA_INVALID, report.error_codes)
        self.assertEqual(report.status, STATUS_PASS_EXACT)

    def test_non_object_plan_is_refused(self):
        for value in (None, [], "plan", 42, 3.5):
            report = validate_episode_plan(value)
            self.assertEqual(report.status, STATUS_FAIL)
            self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)
            self.assertEqual(report.invalid_fields, ("<root>",))

    def test_empty_source_units_is_refused(self):
        document = self.broken_plan()
        document["source_units"] = []
        self.assert_shape_error(document, "source_units")

    def test_a_unit_without_a_usable_id_is_named(self):
        document = self.broken_plan()
        document["source_units"][0]["id"] = ""
        self.assert_shape_error(document, "source_units[0].id")

    def test_a_ref_without_a_usable_source_id_is_named(self):
        document = self.broken_plan()
        document["clips"][0]["source_refs"][0]["source_id"] = "  "
        self.assert_shape_error(document, "clips[0].source_refs[0].source_id")

    def test_a_negative_span_start_is_named(self):
        document = self.broken_plan()
        document["source_units"][0]["span_start"] = -1
        self.assert_shape_error(document, "source_units[0].span_start")

    def test_a_transition_unit_may_carry_a_well_formed_range(self):
        document = base_plan(
            [make_clip("G001", 0, 10, [make_ref("S001", 0, 10)], system_added_transitions=["S900"])],
            [
                make_unit("S001", 0, 10),
                {
                    "id": "S900",
                    "kind": SOURCE_KIND_SYSTEM_TRANSITION,
                    "span_start": 10,
                    "span_end": 12,
                },
            ],
        )
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, ())
        self.assertEqual(report.system_added_transitions, ("S900",))

    def test_a_broken_transition_range_is_inert_not_blocking(self):
        document = base_plan(
            [make_clip("G001", 0, 10, [make_ref("S001", 0, 10)], system_added_transitions=["S900"])],
            [
                make_unit("S001", 0, 10),
                {"id": "S900", "kind": SOURCE_KIND_SYSTEM_TRANSITION, "span_start": 5, "span_end": 5},
            ],
        )
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, ())
        self.assertEqual(report.system_added_transitions, ("S900",))

    def test_optional_unit_text_and_scene_are_typed_when_present(self):
        for name, value in (("text", 123), ("text", None), ("scene_id", None), ("scene_id", "  ")):
            with self.subTest(name=name, value=value):
                document = plan(20, [10, 10])
                document["source_units"][0][name] = value
                report = validate_episode_plan(document)
                self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)
                self.assertIn(f"source_units[0].{name}", report.invalid_fields)

    def test_optional_unit_text_and_scene_are_accepted_when_well_formed(self):
        document = plan(20, [10, 10])
        document["source_units"][0]["text"] = "第一句台词。"
        document["source_units"][0]["scene_id"] = "SC01"
        report = validate_episode_plan(document)
        self.assertEqual(report.error_codes, ())


class FailClosedTest(unittest.TestCase):
    """畸形计划只出报告，配置问题才抛异常；任何情况下都不返回"通过"。"""

    def test_missing_target_is_named_as_missing_not_as_malformed(self):
        document = plan(120, [10] * 12)
        document.pop("script_target_seconds")
        report = validate_episode_plan(document)
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertEqual(report.error_codes, (MISSING_SCRIPT_TARGET,))
        self.assertEqual(report.invalid_fields, ())
        self.assertIs(report.may_render_full_pack, False)

    def test_zero_target_is_treated_as_missing(self):
        report = validate_episode_plan(plan(0, [10] * 12))
        self.assertEqual(report.error_codes, (MISSING_SCRIPT_TARGET,))

    def test_a_zero_target_never_yields_a_pass(self):
        for target in (None, 0, -5, "120", float("nan")):
            report = validate_episode_plan(plan(target, [10] * 12))
            self.assertNotEqual(report.status, STATUS_PASS_EXACT)
            self.assertEqual(report.status, STATUS_FAIL)

    def test_absurd_durations_are_refused_without_allocating(self):
        report = validate_episode_plan(plan(120, [10 ** 30]))
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertIn(CLIP_DURATION_OUT_OF_RANGE, report.error_codes)

    def test_numbers_beyond_the_float_range_are_refused_not_crashed(self):
        """合法 JSON 能造出 `10**400`：它是畸形数字，不是能让门禁崩掉的输入。"""
        for mutate in (
            lambda p: p.update(script_target_seconds=10 ** 400),
            lambda p: p["clips"][0].update(duration_seconds=10 ** 400),
            lambda p: p["clips"][0].update(start_seconds=10 ** 400),
            lambda p: p["source_units"][0].update(span_end=10 ** 400),
            lambda p: p["clips"][0]["source_refs"][0].update(span_end=10 ** 400),
            lambda p: p.update(planned_prompt_seconds=10 ** 400),
            lambda p: p.update(actual_generated_seconds=10 ** 400),
            lambda p: p.update(final_edit_seconds=10 ** 400),
        ):
            document = plan(20, [10, 10])
            mutate(document)
            report = validate_episode_plan(document)
            self.assertEqual(report.status, STATUS_FAIL)
            self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)

    def test_a_json_document_with_a_huge_integer_is_refused_not_crashed(self):
        document = json.loads(json.dumps(plan(20, [10, 10]), ensure_ascii=False))
        document["clips"][0]["duration_seconds"] = int("9" * 400)
        report = validate_episode_plan(document)
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)

    def test_text_that_cannot_be_written_as_utf8_is_refused(self):
        """孤立代理项是合法 JSON，却写不进任何 UTF-8 交付物。"""
        document = plan(20, [10, 10])
        document["clips"][0]["prompt_text"] = "\ud800"
        report = validate_episode_plan(document)
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)
        self.assertIn("clips[0].prompt_text", report.invalid_fields)

    def test_a_thousand_clips_do_not_trip_any_internal_limit(self):
        report = validate_episode_plan(plan(10_000, [10] * 1000))
        self.assertEqual(report.status, STATUS_PASS_EXACT)
        self.assertEqual(report.coverage_ratio, 1.0)

    def test_the_validator_never_raises_on_a_malformed_plan(self):
        mutated = [
            None,
            {},
            {"clips": "G001"},
            plan(120, [10, 10], source_units=[{"id": "S001"}]),
            plan(120, [10, 10], clips=[{"id": "G001"}]),
            plan(120, [10, 10], clips=[None]),
            plan(120, [10, 10], source_units=[None]),
            plan(120, [10, 10], clips=[with_clip(plan(120, [10, 10])["clips"][0], source_refs=[None])]),
            plan(120, [10, 10], clips=[with_clip(plan(120, [10, 10])["clips"][0], trim_candidates=[None])]),
            plan(120, [10, 10], clips=[with_clip(plan(120, [10, 10])["clips"][0], system_added_transitions="S900")]),
            plan(120, [10], model=None),
            plan(120, [10], workflow=7),
            plan(120, [10], source_units="S001"),
            plan(120, [10], clips=()),
            plan(120, [10], planned_prompt_seconds=None),
        ]
        for document in mutated:
            with self.subTest(document=repr(document)[:60]):
                report = validate_episode_plan(document)
                self.assertEqual(report.status, STATUS_FAIL)
                self.assertIn(report.status, POLICY["statuses"])
                self.assertIs(report.may_render_full_pack, False)
                self.assertTrue(report.error_codes)

    def test_a_broken_config_raises_instead_of_passing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(EpisodeContractError) as caught:
                load_model_capabilities(tmp)
            self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_a_corrupt_capability_document_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            config_dir.mkdir()
            (config_dir / "model-capabilities.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(EpisodeContractError) as caught:
                load_model_capabilities(tmp)
            self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_a_capability_document_without_models_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            config_dir.mkdir()
            (config_dir / "model-capabilities.json").write_text('{"schema_version": "2.0.0"}', encoding="utf-8")
            with self.assertRaises(EpisodeContractError):
                load_model_capabilities(tmp)

    def test_the_real_capability_document_is_loaded_strictly(self):
        models = MODEL_CAPABILITIES["models"]
        self.assertIn("H3", models)
        self.assertEqual(models["H3"]["duration_seconds"], {"min": 4, "max": 15})


class ConfigHardeningTest(unittest.TestCase):
    """配置本身坏掉时门禁必须拒绝运行，而不是退回内置默认值。"""

    def test_a_policy_that_cannot_be_loaded_is_wrapped(self):
        original = episode_contract.load_duration_policy

        def broken(*args, **kwargs):
            raise VersionValidationError("损坏的政策文件")

        episode_contract.load_duration_policy = broken
        try:
            with self.assertRaises(EpisodeContractError) as caught:
                validate_episode_plan(plan(120, [10] * 12))
        finally:
            episode_contract.load_duration_policy = original
        self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_non_positive_clip_bounds_are_refused(self):
        for clip_seconds in ({"min": 0, "max": 10}, {"min": 4, "max": 0}, {"min": 4.5, "max": 10}):
            with self.subTest(clip_seconds=clip_seconds):
                with self.assertRaises(EpisodeContractError) as caught:
                    validate_episode_plan(
                        plan(120, [10] * 12), MODEL_CAPABILITIES, policy_with(clip_seconds=clip_seconds)
                    )
                self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_inverted_clip_bounds_are_refused(self):
        with self.assertRaises(EpisodeContractError) as caught:
            validate_episode_plan(
                plan(120, [10] * 12),
                MODEL_CAPABILITIES,
                policy_with(clip_seconds={"min": 12, "max": 10}),
            )
        self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_a_non_finite_review_ratio_is_refused(self):
        for ratio in (float("inf"), float("nan")):
            with self.subTest(ratio=ratio):
                with self.assertRaises(EpisodeContractError) as caught:
                    validate_episode_plan(
                        plan(120, [10] * 12),
                        MODEL_CAPABILITIES,
                        policy_with(overage_review_ratio=ratio),
                    )
                self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_broken_model_duration_bounds_are_refused(self):
        for duration in ({"min": 0, "max": 15}, {"min": 4, "max": "15"}, {"min": 16, "max": 15}, {}):
            with self.subTest(duration=duration):
                with self.assertRaises(EpisodeContractError) as caught:
                    validate_episode_plan(
                        plan(120, [10] * 12), capabilities_with(H3={"duration_seconds": duration})
                    )
                self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_an_overage_ratio_outside_the_frozen_band_is_refused(self):
        """放宽到 ratio >= 1 会让 REVIEW_REQUIRED 永远不可达，等于静默关掉 20% 暂停。"""
        for ratio in (0, 0.0, -1, 1, 1.0, 5.0, float("nan"), float("inf"), "0.2"):
            with self.subTest(ratio=ratio):
                with self.assertRaises(EpisodeContractError) as caught:
                    validate_episode_plan(
                        plan(120, [10] * 12),
                        MODEL_CAPABILITIES,
                        policy_with(overage_review_ratio=ratio),
                    )
                self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_duplicate_status_entries_are_refused(self):
        with self.assertRaises(EpisodeContractError) as caught:
            validate_episode_plan(
                plan(120, [10] * 12),
                MODEL_CAPABILITIES,
                policy_with(
                    statuses=[
                        STATUS_PASS_EXACT,
                        STATUS_PASS_EXACT,
                        STATUS_PASS_WITH_TRIM,
                        STATUS_REVIEW_REQUIRED,
                        STATUS_FAIL,
                    ]
                ),
            )
        self.assertIn(CONFIG_UNAVAILABLE, str(caught.exception))

    def test_injected_policies_are_never_more_permissive_than_the_disk_policy(self):
        """注入政策与磁盘政策必须同源同强度：磁盘拒绝的，注入路径也要拒绝。"""
        variants = (
            {"overage_review_ratio": 0.0},
            {"overage_review_ratio": 1.0},
            {"overage_review_ratio": 2.5},
            {"overage_review_ratio": -0.2},
            {"overage_review_ratio": float("nan")},
            {"overage_review_ratio": float("inf")},
            {"overage_review_ratio": "0.2"},
            {"statuses": [STATUS_PASS_EXACT, STATUS_PASS_WITH_TRIM, STATUS_REVIEW_REQUIRED]},
            {"statuses": []},
            {"statuses": [STATUS_PASS_EXACT, STATUS_PASS_EXACT, STATUS_FAIL, STATUS_PASS_WITH_TRIM]},
            {"clip_seconds": {"min": 0, "max": 10}},
            {"clip_seconds": {"min": 12, "max": 10}},
            {"clip_seconds": {"min": 4}},
        )
        # 只对**影响判定**的字段声明同强度：padding_policy 门禁根本不消费（它由 Task 4/6
        # 读取），注入路径不校验它既不放宽任何结论，也不该被这条性质牵连。
        for change in variants:
            with self.subTest(change=change):
                document = json.loads(json.dumps(POLICY))
                document.update(change)
                disk_rejected = False
                with tempfile.TemporaryDirectory() as tmp:
                    config_dir = Path(tmp) / "config"
                    config_dir.mkdir()
                    (config_dir / "duration-policy.json").write_text(
                        json.dumps(document, ensure_ascii=False), encoding="utf-8"
                    )
                    try:
                        load_duration_policy(tmp)
                    except VersionValidationError:
                        disk_rejected = True
                injected_rejected = False
                try:
                    validate_episode_plan(
                        plan(120, [10] * 12), MODEL_CAPABILITIES, copy.deepcopy(document)
                    )
                except EpisodeContractError:
                    injected_rejected = True
                if disk_rejected:
                    self.assertTrue(injected_rejected, f"磁盘政策拒绝但注入政策放行：{change}")


class ShapeReportBoundTest(unittest.TestCase):
    """坏字段清单有界且线性：十万片段既不能拖成平方级，也不能撑爆输出。"""

    def test_a_huge_broken_plan_is_capped_and_marked(self):
        clips = []
        for index in range(4000):
            clips.append(
                make_clip(f"G{index:03d}", 0, 0, [make_ref("S001", 0, 1)]),
            )
        document = base_plan(clips, [make_unit("S001", 0, 1)])
        report = validate_episode_plan(document)
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertIn(PLAN_SCHEMA_INVALID, report.error_codes)
        self.assertLessEqual(len(report.invalid_fields), 51)
        self.assertTrue(report.invalid_fields[-1].startswith("<+"))
        self.assertTrue(report.invalid_fields[-1].endswith("more>"))

    def test_a_capped_report_still_names_the_first_real_problems(self):
        clips = [make_clip(f"G{index:03d}", 0, 0, [make_ref("S001", 0, 0)]) for index in range(200)]
        document = base_plan(clips, [make_unit("S001", 0, 1)])
        report = validate_episode_plan(document)
        self.assertIn("clips[0].duration_seconds", report.invalid_fields)
        self.assertIn("clips[0].source_refs[0].span_end", report.invalid_fields)
        self.assertTrue(report.invalid_fields[-1].startswith("<+"))

    def test_a_large_clean_plan_is_reported_without_field_noise(self):
        document = plan(6000, [10] * 600)
        report = validate_episode_plan(document)
        self.assertEqual(report.status, STATUS_PASS_EXACT)
        self.assertEqual(report.invalid_fields, ())


class SchemaFileTest(unittest.TestCase):
    """`templates/episode-plan.schema.json` 必须与实现冻结在同一套字段与枚举上。"""

    def schema(self):
        self.assertTrue(SCHEMA_PATH.is_file(), f"缺少计划 Schema 文件：{SCHEMA_PATH}")
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_schema_is_a_json_object_document(self):
        schema = self.schema()
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["type"], "object")
        self.assertIs(schema["additionalProperties"], True)

    def test_top_level_required_fields_match_the_work_order(self):
        schema = self.schema()
        self.assertEqual(
            set(schema["required"]),
            {
                "episode_id",
                "workflow",
                "model",
                "script_target_seconds",
                "source_units",
                "clips",
                "planned_prompt_seconds",
                "real_model_render_status",
                "actual_generated_seconds",
                "final_edit_seconds",
            },
        )

    def test_clip_required_fields_match_the_work_order(self):
        schema = self.schema()
        self.assertEqual(
            set(schema["$defs"]["clip"]["required"]),
            {
                "id",
                "start_seconds",
                "duration_seconds",
                "source_refs",
                "start_state",
                "end_state",
                "system_added_transitions",
                "trim_candidates",
                "prompt_text",
            },
        )

    def test_source_refs_must_carry_character_ranges(self):
        schema = self.schema()
        self.assertEqual(
            set(schema["$defs"]["source_ref"]["required"]), {"source_id", "span_start", "span_end"}
        )

    def test_enums_match_the_module_constants(self):
        schema = self.schema()
        self.assertEqual(
            set(schema["$defs"]["source_unit"]["properties"]["kind"]["enum"]), set(SOURCE_UNIT_KINDS)
        )
        self.assertEqual(
            set(schema["properties"]["real_model_render_status"]["enum"]), set(RENDER_STATUSES)
        )
        self.assertEqual(schema["properties"]["schema_version"]["const"], EPISODE_PLAN_SCHEMA_VERSION)

    def test_the_schema_documents_every_field_the_validator_consumes(self):
        schema = self.schema()
        document = plan(120, [10])
        self.assertTrue(set(document) <= set(schema["properties"]))
        self.assertTrue(
            set(document["clips"][0]) <= set(schema["$defs"]["clip"]["properties"])
        )
        self.assertTrue(
            set(document["source_units"][0]) <= set(schema["$defs"]["source_unit"]["properties"])
        )
        self.assertTrue(
            {"at_seconds", "reason"} <= set(schema["$defs"]["trim_candidate"]["properties"])
        )

    def test_the_schema_names_the_duration_policy_as_the_clip_bound_source(self):
        schema = self.schema()
        description = json.dumps(schema, ensure_ascii=False)
        self.assertIn("duration-policy.json", description)
        self.assertIn("model-capabilities.json", description)


class SchemaDifferentialTest(unittest.TestCase):
    """用真正的 JSON Schema 校验器做差分：Schema 既不能形同虚设，也不能比门禁更松。

    没有安装 `jsonschema` 时整类跳过——本套件其余部分只用标准库。
    """

    SCHEMA_REQUIRED = (
        "episode_id",
        "workflow",
        "model",
        "script_target_seconds",
        "source_units",
        "clips",
        "planned_prompt_seconds",
        "real_model_render_status",
        "actual_generated_seconds",
        "final_edit_seconds",
    )

    @classmethod
    def setUpClass(cls):
        try:
            import jsonschema  # noqa: F401
        except ImportError:  # pragma: no cover
            cls.validator = None
            return
        cls.schema_document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def validator_or_skip(self):
        try:
            import jsonschema
        except ImportError:  # pragma: no cover
            self.skipTest("未安装 jsonschema")
        return jsonschema.Draft202012Validator(self.schema_document)

    def test_a_shape_clean_plan_is_always_schema_valid(self):
        validator = self.validator_or_skip()
        samples = [
            plan(120, [10] * 12),
            plan(120, [10] * 13),
            plan_with_missing_and_overlap(),
            plan_with_gap_and_three_second_clip(),
            plan(120, [10] * 12, real_model_render_status=RENDER_STATUS_COMPLETED,
                 actual_generated_seconds=131.0, final_edit_seconds=129.0),
        ]
        for sample in samples:
            report = validate_episode_plan(sample)
            if report.invalid_fields:
                continue
            errors = sorted(validator.iter_errors(sample), key=lambda error: list(error.path))
            self.assertEqual(
                [], errors, f"门禁判 shape-clean 但 Schema 拒绝：{[e.message for e in errors][:2]}"
            )

    def test_the_gate_is_never_more_permissive_than_the_schema_on_mutations(self):
        """生成式差分：形态干净的计划必须能被已发布 Schema 接受（抽样 5 例抓不到漂移）。"""
        validator = self.validator_or_skip()
        rng = random.Random(20260918)
        pool = [None, True, False, 0, -1, 3.7, "", "  ", "x", [], {}, 10 ** 400, [None], {"a": 1}]
        base = json.loads(json.dumps(plan(30, [10, 10, 10]), ensure_ascii=False))
        benign = self.benign_variations(base, rng, 80)

        checked = 0
        for case, document in enumerate(benign):
            self.assert_schema_valid(validator, document, f"benign case={case}")
            checked += 1

        # 对抗突变：门禁拒绝时只要求两边都拒绝；门禁放行时必须与 Schema 一致。
        for case in range(600):
            document = json.loads(json.dumps(base, ensure_ascii=False))
            for _ in range(rng.randint(1, 3)):
                clips = document.get("clips")
                if rng.random() < 0.5 and isinstance(clips, list) and clips and isinstance(clips[0], dict):
                    clip = clips[rng.randrange(len(clips))]
                    if isinstance(clip, dict):
                        clip[rng.choice(list(clip))] = copy.deepcopy(rng.choice(pool))
                        continue
                document[rng.choice(list(document))] = copy.deepcopy(rng.choice(pool))
            report = validate_episode_plan(document)
            if (
                report.invalid_fields
                or PLAN_SCHEMA_INVALID in report.error_codes
                or MISSING_SCRIPT_TARGET in report.error_codes
            ):
                continue
            # 形态门禁对"缺目标 / 零目标"有自己的 MISSING_SCRIPT_TARGET 结论（已在上一步
            # 排除），与 Schema 的必填语义是两条路、同一个拒绝结论；其余形态干净的计划
            # 必须被已发布 Schema 接受。
            if any(name not in document for name in self.SCHEMA_REQUIRED):
                continue
            checked += 1
            self.assert_schema_valid(validator, document, f"mutation case={case}")
        self.assertGreater(checked, 50, "差分样本太少，性质没有被真正检验")

    def assert_schema_valid(self, validator, document, label):
        errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
        self.assertEqual(
            [], errors, f"{label} 门禁判 shape-clean 但 Schema 拒绝：{[e.message for e in errors][:2]}"
        )

    def benign_variations(self, base, rng, count):
        """合法但有变化的计划：证明"通过的都被 Schema 接受"不是靠单一夹具。"""
        documents = []
        for case in range(count):
            document = json.loads(json.dumps(base, ensure_ascii=False))
            document["episode_id"] = f"EP{case:02d}"
            document["workflow"] = rng.choice(
                ["native-drama-series", "native-general", "ref2va-en", "native-zh"]
            )
            document["model"] = rng.choice(["H3", "H3-Max", "H3-Max-Turbo"])
            if case % 3 == 0:
                document["real_model_render_status"] = "FAILED"
            if case % 5 == 0:
                document["real_model_render_status"] = "COMPLETED"
                document["actual_generated_seconds"] = 30.0
                document["final_edit_seconds"] = 29.5
            document["source_units"][0]["text"] = "第" * (case % 7 + 1) + "句台词。"
            document["source_units"][0]["scene_id"] = f"SC{case % 4}"
            document["extra_note"] = {"case": case}
            document["clips"][0]["trim_candidates"] = [
                {"at_seconds": 0.3, "reason": f"开场留白 {case}"}
            ]
            documents.append(document)
        return documents

    def test_the_schema_is_not_vacuous(self):
        validator = self.validator_or_skip()
        unknown_status = plan(120, [10])
        unknown_status["real_model_render_status"] = "DONE"
        missing_clips = plan(120, [10])
        missing_clips.pop("clips")
        wrong_type = plan(120, [10])
        wrong_type["clips"][0]["duration_seconds"] = "10"
        for broken in ({"episode_id": "EP01"}, unknown_status, missing_clips, wrong_type):
            self.assertTrue(
                list(validator.iter_errors(broken)), "Schema 接受了残缺计划，形同虚设"
            )


class UpstreamIntegrationTest(unittest.TestCase):
    """真实链路：Task 3 目标时长 + Task 2 政策 + Task 4 装箱 → Task 5 门禁。"""

    BEAT_TEXT = "一二三四五六七八九十甲乙丙丁戊"  # 可发音 15 字

    def build_plan(self, clips, beats, target):
        units = []
        refs_by_beat = {}
        cursor = 0
        for beat in beats:
            units.append(
                make_unit(
                    f"U{beat.id}",
                    cursor,
                    cursor + len(beat.text),
                    kind=beat.kind,
                )
            )
            refs_by_beat[beat.id] = make_ref(f"U{beat.id}", cursor, cursor + len(beat.text))
            cursor += len(beat.text)
        planned_clips = []
        start = 0
        for index, clip in enumerate(clips, start=1):
            planned_clips.append(
                make_clip(
                    f"G{index:03d}",
                    start,
                    clip.duration_seconds,
                    [refs_by_beat[beat_id] for beat_id in clip.beat_ids],
                )
            )
            start += clip.duration_seconds
        return base_plan(planned_clips, units, target=target)

    def beats(self):
        return [
            Beat(
                id=f"B{index:03d}",
                scene_id="S1",
                estimated_seconds=5.0,
                text=self.BEAT_TEXT,
                kind=KIND_DIALOGUE,
                splittable=False,
                speaker_ids=("A",),
            )
            for index in range(1, 7)
        ]

    def test_a_planner_built_plan_passes_the_gate(self):
        beats = self.beats()
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual([clip.duration_seconds for clip in clips], [10, 10, 10])
        target = parse_script_duration("时长：30秒\n【画面】雪地。").target_seconds
        report = validate_episode_plan(self.build_plan(clips, beats, target))
        self.assertEqual(report.status, STATUS_PASS_EXACT)
        self.assertEqual(report.error_codes, ())
        self.assertEqual(report.coverage_ratio, 1.0)
        self.assertIs(report.timeline_contiguous, True)
        self.assertEqual(report.clip_range_seconds, (4, 10))
        self.assertEqual(report.planned_prompt_seconds, 30)

    def test_the_same_plan_fails_when_the_script_declares_a_longer_target(self):
        beats = self.beats()
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        target = parse_script_duration("时长：60秒\n【画面】雪地。").target_seconds
        report = validate_episode_plan(self.build_plan(clips, beats, target))
        self.assertEqual(report.status, STATUS_FAIL)
        self.assertIn(PLANNED_DURATION_UNDERSHOOT, report.error_codes)

    def test_dropping_a_clip_is_caught_as_a_coverage_gap_not_as_a_smaller_target(self):
        beats = self.beats()
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        dropped_beat_ids = clips[-1].beat_ids
        document = self.build_plan(clips, beats, 30)
        document["clips"].pop()
        document["planned_prompt_seconds"] = 20
        report = validate_episode_plan(document)
        self.assertIn(SOURCE_COVERAGE_GAP, report.error_codes)
        self.assertIn(PLANNED_DURATION_UNDERSHOOT, report.error_codes)
        self.assertTrue(
            {f"U{beat_id}" for beat_id in dropped_beat_ids} <= set(report.missing_source_units)
        )
        self.assertEqual(report.planned_prompt_seconds, 20)

    def test_an_action_only_episode_still_needs_source_coverage(self):
        beats = [
            Beat(
                id=f"B{index:03d}",
                scene_id="S1",
                estimated_seconds=5.0,
                text=f"第{index}组动作：主角推门、回头。",
                kind=SOURCE_KIND_ACTION,
                splittable=False,
            )
            for index in range(1, 5)
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        target = parse_script_duration("时长：20秒\n").target_seconds
        report = validate_episode_plan(self.build_plan(clips, beats, target))
        self.assertEqual(report.status, STATUS_PASS_EXACT)
        self.assertEqual(report.coverage_ratio, 1.0)


if __name__ == "__main__":
    unittest.main()
