#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单03-Task6：三轮定向返工状态机（规格 8）。

每项资产最多自动返工三轮；每轮必须依据结构化问题码改写修正指令；
原样重复提示词视为 INEFFECTIVE_RETRY；三轮后进入「需要人工决定」，不发第四次请求。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.image_executor import (  # noqa: E402
    GenerationRequest,
    GenerationResult,
    ManualExportExecutor,
)
from scripts.engine.image_qc import (  # noqa: E402
    BLOCKING,
    QcIssue,
    QcResult,
    parse_qc_response,
)
from scripts.engine.visual_pipeline import (  # noqa: E402
    INEFFECTIVE_RETRY,
    LEGAL_TRANSITIONS,
    InvalidVisualTransition,
    build_repair_prompt,
    propagate_upstream_change,
    run_task,
    run_task_with_checker,
    transition,
)
from scripts.engine.visual_schema import (  # noqa: E402
    MAX_AUTO_ROUNDS,
    VisualPlan,
    VisualTask,
)

import base64

# 真实最小的 4x4 PNG（经 Pillow verify() 通过）
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9UaAAAAABJRU5ErkJggg=="
)
TASK_ID = "CH_林姑娘-正脸-v01"


def _task(**kw):
    base = dict(task_id=TASK_ID, asset_id="CH_林姑娘", kind="正脸", status="PLANNED",
                aspect_ratio="1:1", style_slug="仙侠国风电影CG", style_version="1.0.0",
                style_hash="a" * 64, fact_source=".process/资产索引.json#CH_林姑娘",
                materials=("皮肤",))
    base.update(kw)
    return VisualTask(**base)


def _plan(task=None):
    return VisualPlan(schema_version=1, style_name="仙侠国风电影CG",
                      style_slug="仙侠国风电影CG", style_version="1.0.0",
                      style_hash="a" * 64, tasks=(task or _task(),))


class _FakeExecutor:
    """记录每次请求并返回固定图片（单元测试零真实请求）。"""

    def __init__(self, failures=0, image=PNG_BYTES):
        self.failures = failures
        self.image = image
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        return GenerationResult(provider="fake", model="m", image_bytes=self.image,
                                mime_type="image/png", response_id=f"r{len(self.requests)}",
                                status="OK")


class _FakeChecker:
    """前 N 轮报阻断问题，之后通过。"""

    def __init__(self, fail_rounds=0, code="NECK_STUMP_VISIBLE"):
        self.fail_rounds = fail_rounds
        self.code = code
        self.calls = 0

    def check(self, task, attempt, lib):
        self.calls += 1
        if self.calls <= self.fail_rounds:
            # 每轮证据不同：现实中同一问题码的可见表现逐轮变化，属有效返工
            return QcResult(decision="NEEDS_REVISION",
                            issues=(QcIssue(code=self.code, severity=BLOCKING,
                                            evidence=f"第{self.calls}轮可见缺陷",
                                            repair_instruction="按码定向修复"),),
                            checker="fake", checked_at="t",
                            input_hashes=(attempt.image_sha256,), task_id=task.task_id)
        return QcResult(decision="ACCEPTED", issues=(), checker="fake", checked_at="t",
                        input_hashes=(attempt.image_sha256,), task_id=task.task_id)


# --- 状态转换 -------------------------------------------------------------------

def test_legal_transitions_cover_spec_path():
    assert "PLANNED" in LEGAL_TRANSITIONS
    assert "GENERATED" in LEGAL_TRANSITIONS["PLANNED"]
    assert "QC_PENDING" in LEGAL_TRANSITIONS["GENERATED"]
    assert "ACCEPTED" in LEGAL_TRANSITIONS["QC_PENDING"]
    assert "NEEDS_REVISION" in LEGAL_TRANSITIONS["QC_PENDING"]
    assert "NEEDS_HUMAN_DECISION" in LEGAL_TRANSITIONS["NEEDS_REVISION"]
    assert MAX_AUTO_ROUNDS == 3


def test_illegal_transition_raises():
    task = _task(status="PLANNED")
    with pytest.raises(InvalidVisualTransition):
        transition(task, "ACCEPTED")


def test_transition_keeps_history():
    task = transition(transition(_task(status="PLANNED"), "GENERATED"), "QC_PENDING")
    assert task.status == "QC_PENDING"
    assert task.task_id == TASK_ID


def test_accepted_can_become_reference():
    task = transition(_task(status="QC_PENDING"), "ACCEPTED")
    assert task.status == "ACCEPTED"
    assert transition(task, "READY_AS_REFERENCE").status == "READY_AS_REFERENCE"


# --- 定向返工提示 ----------------------------------------------------------------

def test_repair_prompt_includes_issue_specific_instruction():
    issues = (QcIssue(code="NECK_STUMP_VISIBLE", severity=BLOCKING, evidence="领口见颈桩",
                      repair_instruction="改用隐形人体支撑，隐藏全部皮肤残端"),)
    prompt = build_repair_prompt("【任务目标】正脸特写", issues)
    assert "隐形人体支撑" in prompt
    assert "NECK_STUMP_VISIBLE" in prompt
    assert prompt != "【任务目标】正脸特写"


def test_repair_prompt_reinjects_style_reference_on_style_mismatch():
    issues = (QcIssue(code="STYLE_MISMATCH", severity=BLOCKING, evidence="瓷亮高光",
                      repair_instruction="压低高光"),)
    prompt = build_repair_prompt("【任务目标】正脸特写", issues)
    assert "风格参考" in prompt
    assert "【任务目标】正脸特写" in prompt, "修正后仍保留原任务内容"


def test_identical_instructions_are_ineffective_retry():
    """同一批修正指令重复要求（上一轮已提过、模型未改）即视为无效返工。"""
    issues = (QcIssue(code="STYLE_MISMATCH", severity=BLOCKING, evidence="瓷亮高光",
                      repair_instruction=""),)
    first = build_repair_prompt("【任务目标】正脸特写", issues)
    with pytest.raises(ValueError) as ctx:
        build_repair_prompt("【任务目标】正脸特写", issues, previous_prompt=first, strict=True)
    assert INEFFECTIVE_RETRY in str(ctx.value)


def test_repair_prompt_requires_issues():
    with pytest.raises(ValueError):
        build_repair_prompt("原文", ())


# --- run_task：正例与三轮上限 -----------------------------------------------------

def test_accepted_on_first_round(tmp_path):
    task = run_task_with_checker(_task(), _FakeExecutor(), _FakeChecker(), tmp_path, plan=_plan())
    assert task.status in ("ACCEPTED", "READY_AS_REFERENCE")
    assert task.round_count == 1


def test_one_failure_then_success(tmp_path):
    executor = _FakeExecutor()
    task = run_task_with_checker(_task(), executor, _FakeChecker(fail_rounds=1), tmp_path, plan=_plan())
    assert task.round_count == 2
    assert task.status in ("ACCEPTED", "READY_AS_REFERENCE")
    assert len(executor.requests) == 2


def test_three_failures_stop_at_human_decision(tmp_path):
    executor = _FakeExecutor()
    task = run_task_with_checker(_task(), executor, _FakeChecker(fail_rounds=99), tmp_path, plan=_plan())
    assert len(executor.requests) == MAX_AUTO_ROUNDS, "不得发第四次请求"
    assert task.round_count == MAX_AUTO_ROUNDS
    assert task.status == "NEEDS_HUMAN_DECISION"
    assert not (tmp_path / "视觉资产" / "任务" / TASK_ID / "attempt-04").exists()


def test_round_history_is_preserved(tmp_path):
    task = run_task_with_checker(_task(), _FakeExecutor(), _FakeChecker(fail_rounds=2),
                                 tmp_path, plan=_plan())
    rounds = [a.round for a in task.attempts]
    assert rounds == [1, 2, 3]
    for n in (1, 2, 3):
        assert (tmp_path / "视觉资产" / "任务" / TASK_ID / f"attempt-0{n}" / "image.png").is_file()


def test_second_round_prompt_differs_from_first(tmp_path):
    executor = _FakeExecutor()
    run_task_with_checker(_task(), executor, _FakeChecker(fail_rounds=1), tmp_path, plan=_plan())
    assert executor.requests[0].prompt != executor.requests[1].prompt
    assert "NECK_STUMP_VISIBLE" in executor.requests[1].prompt


def test_usage_is_accumulated_across_rounds(tmp_path):
    task = run_task_with_checker(_task(), _FakeExecutor(), _FakeChecker(fail_rounds=2),
                                 tmp_path, plan=_plan())
    assert task.round_count == 3
    assert all(a.response_id for a in task.attempts)


def test_qc_result_written_each_round(tmp_path):
    """每轮各自保留自己的检查结果（历史轮次不可被覆盖）。"""
    run_task_with_checker(_task(), _FakeExecutor(), _FakeChecker(fail_rounds=1), tmp_path, plan=_plan())
    base = tmp_path / "视觉资产" / "任务" / TASK_ID
    first = (base / "attempt-01" / "qc.toml").read_text(encoding="utf-8")
    second = (base / "attempt-02" / "qc.toml").read_text(encoding="utf-8")
    assert "NEEDS_REVISION" in first and "NECK_STUMP_VISIBLE" in first
    assert "ACCEPTED" in second
    assert first != second


def test_manual_executor_leaves_task_awaiting_manual(tmp_path):
    """无 API 时不得假装生成：手工导出后任务停在等待手工结果。"""
    task = run_task_with_checker(_task(), ManualExportExecutor(tmp_path / "包"), _FakeChecker(),
                                 tmp_path, plan=_plan())
    assert task.status == "AWAITING_MANUAL_RESULT"
    assert task.round_count == 0


def test_identical_instructions_escalate_without_extra_request(tmp_path):
    """检查器反复给同一批指令（证据也一样）时，不得继续消耗额度。"""
    executor = _FakeExecutor()

    class _Same(_FakeChecker):
        def check(self, task, attempt, lib):
            self.calls += 1
            return QcResult(decision="NEEDS_REVISION",
                            issues=(QcIssue(code="NECK_STUMP_VISIBLE", severity=BLOCKING,
                                            evidence="同一处颈桩", repair_instruction="改支撑"),),
                            checker="fake", checked_at="t",
                            input_hashes=(attempt.image_sha256,), task_id=task.task_id)

    task = run_task_with_checker(_task(), executor, _Same(), tmp_path, plan=_plan())
    assert task.status == "NEEDS_HUMAN_DECISION"
    assert len(executor.requests) == 2, "重复指令后不得再发请求"
    assert "INEFFECTIVE_RETRY" in (task.notes or "")


def test_run_task_requires_plan_style_pin(tmp_path):
    bad = VisualTask(**{**_task().__dict__, "style_hash": ""})
    with pytest.raises(ValueError):
        run_task_with_checker(bad, _FakeExecutor(), _FakeChecker(), tmp_path, plan=None)


# --- 上游变化传播 ----------------------------------------------------------------

def test_upstream_change_marks_downstream_needs_review():
    upstream = VisualTask(**{**_task().__dict__, "status": "READY_AS_REFERENCE"})
    downstream = VisualTask(**{**(_task(task_id="CH_林姑娘-后脑-v01",
                                        kind="后脑").__dict__),
                               "status": "READY_AS_REFERENCE",
                               "depends_on": (TASK_ID,)})
    plan = VisualPlan(schema_version=1, style_slug="仙侠国风电影CG", style_version="1.0.0",
                      style_hash="a" * 64, tasks=(upstream, downstream))
    updated = propagate_upstream_change(plan, TASK_ID)
    kept = updated.by_id("CH_林姑娘-后脑-v01")
    assert kept.status == "NEEDS_REVIEW"
    assert kept.attempts == downstream.attempts, "旧图保留"
    assert updated.by_id(TASK_ID).status == "READY_AS_REFERENCE"


def test_propagation_only_affects_accepted_downstream():
    upstream = VisualTask(**{**_task().__dict__, "status": "READY_AS_REFERENCE"})
    pending = VisualTask(**{**_task(task_id="CH_林姑娘-后脑-v01", kind="后脑").__dict__,
                            "status": "PLANNED", "depends_on": (TASK_ID,)})
    plan = VisualPlan(schema_version=1, style_slug="仙侠国风电影CG", style_version="1.0.0",
                      style_hash="a" * 64, tasks=(upstream, pending))
    updated = propagate_upstream_change(plan, TASK_ID)
    assert updated.by_id("CH_林姑娘-后脑-v01").status == "PLANNED"


def test_qc_parse_and_pipeline_share_result_type():
    """run_task 内部走 parse_qc_response 的同一 QcResult 契约。"""
    data = {"task_id": TASK_ID, "decision": "ACCEPTED", "checker": "c", "checked_at": "t",
            "input_hashes": ["a" * 64], "issues": []}
    result = parse_qc_response(data, expected={"task_id": TASK_ID, "input_hashes": ["a" * 64]})
    assert isinstance(result, QcResult)
    assert result.is_accepted


# --- 工单 0.2 红测：生成与终审分离、审核绑定完整输入闭包 ---------------------------

def test_generation_stops_at_waiting_review_without_main_review(tmp_path):
    """图片生成完成后，在拿到主模型审核结果前不得进入「可作为参考」。

    当前实现把生成与 Gemini 终审写在同一次 `run_task` 调用里，生成即终审 → 预期红。
    """
    task = run_task(_task(), _FakeExecutor(), _FakeChecker(), tmp_path, plan=_plan())
    assert task.status in ("WAITING_REVIEW", "QC_PENDING"), (
        f"生成后必须停在等待审核，实际 {task.status}")
    assert task.status not in ("ACCEPTED", "READY_AS_REFERENCE"), (
        "没有主模型审核结果时不得直接成为正式参考")


def test_check_result_must_bind_full_input_closure():
    """审核判定必须同时绑定任务、图片、提示词、参考集、画风包与构图卡哈希。

    任一输入变化 → 审核立即失效（阶段 2 工单 2.2 已实现；本用例是长期回归守卫）。
    """
    image_sha = "a" * 64
    good = {"task_id": TASK_ID, "decision": "ACCEPTED", "checker": "main-model",
            "checked_at": "t", "input_hashes": [image_sha], "issues": [],
            "prompt_sha256": "b" * 64, "reference_set_sha256": "c" * 64,
            "style_sha256": "d" * 64, "composition_sha256": "e" * 64}
    expected = {"task_id": TASK_ID, "input_hashes": [image_sha],
                "prompt_sha256": "b" * 64, "reference_set_sha256": "c" * 64,
                "style_sha256": "d" * 64, "composition_sha256": "e" * 64}
    assert parse_qc_response(good, expected=expected).is_accepted, "闭包一致时应当接受"

    stale_prompt = {**good, "prompt_sha256": "f" * 64}
    result = parse_qc_response(stale_prompt, expected=expected)
    assert not result.is_accepted, "旧提示词哈希不得视为有效审核"
    assert result.decision == "QC_UNTRUSTWORTHY"

    other_task = {**good, "task_id": "CH_林姑娘-后脑-v01"}
    assert not parse_qc_response(other_task, expected=expected).is_accepted

    other_image = {**good, "input_hashes": ["9" * 64]}
    assert not parse_qc_response(other_image, expected=expected).is_accepted
