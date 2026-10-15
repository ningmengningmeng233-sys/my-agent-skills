#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三轮定向返工状态机（规格 8，工单03-Task6）。

- 状态转换白名单：`PLANNED → GENERATED → QC_PENDING → ACCEPTED / NEEDS_REVISION
  → … → NEEDS_HUMAN_DECISION`；非法跳转抛 `InvalidVisualTransition`。
- 每项资产最多自动返工 3 轮；每轮依据结构化问题码**改写**修正指令，
  原样重复提示词视为 `INEFFECTIVE_RETRY`；三轮用尽即停在「需要人工决定」，绝不发第四次请求。
- 上游替换后下游转为 `NEEDS_REVIEW`（旧图保留，但取消可作为参考）。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, Sequence

from .image_executor import GenerationRequest, GenerationResult
from .image_qc import BLOCKING, QcIssue, QcResult, write_qc_result
from .visual_schema import (
    MAX_AUTO_ROUNDS,
    AttemptRecord,
    VisualPlan,
    VisualTask,
    VisualTaskStatus,
)
from .visual_store import save_attempt

INEFFECTIVE_RETRY = "INEFFECTIVE_RETRY"

S = VisualTaskStatus

LEGAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    S.PLANNED.value: (S.GENERATED.value, S.AWAITING_MANUAL_RESULT.value,
                      S.NEEDS_REVIEW.value, S.FAILED.value, S.BLOCKED.value),
    S.AWAITING_MANUAL_RESULT.value: (S.GENERATED.value, S.NEEDS_REVIEW.value),
    S.GENERATED.value: (S.QC_PENDING.value, S.WAITING_REVIEW.value, S.FAILED.value,
                        S.BLOCKED.value),
    S.QC_PENDING.value: (S.ACCEPTED.value, S.NEEDS_REVISION.value,
                         S.NEEDS_HUMAN_DECISION.value),
    S.WAITING_REVIEW.value: (S.ACCEPTED.value, S.NEEDS_REVISION.value,
                             S.NEEDS_HUMAN_DECISION.value, S.BLOCKED.value,
                             S.NEEDS_REVIEW.value),
    S.NEEDS_REVISION.value: (S.GENERATED.value, S.NEEDS_HUMAN_DECISION.value,
                             S.NEEDS_REVIEW.value, S.BLOCKED.value),
    S.ACCEPTED.value: (S.READY_AS_REFERENCE.value, S.NEEDS_REVIEW.value),
    S.READY_AS_REFERENCE.value: (S.NEEDS_REVIEW.value,),
    S.NEEDS_REVIEW.value: (S.GENERATED.value, S.QC_PENDING.value, S.ACCEPTED.value,
                           S.NEEDS_HUMAN_DECISION.value, S.PLANNED.value),
    S.NEEDS_HUMAN_DECISION.value: (S.GENERATED.value, S.ACCEPTED.value,
                                   S.NEEDS_REVIEW.value),
    S.BLOCKED.value: (S.GENERATED.value, S.WAITING_REVIEW.value, S.NEEDS_REVIEW.value,
                      S.NEEDS_HUMAN_DECISION.value),
    S.FAILED.value: (S.GENERATED.value, S.NEEDS_HUMAN_DECISION.value, S.BLOCKED.value),
}

# 返工指令模板：按问题码注入定向修复语（缺省用检查器给的 repair_instruction）
REPAIR_HINTS = {
    "STYLE_MISMATCH": "重注入风格参考（介质、材质、光色基线），"
                      "恢复干燥哑光至柔缎表面与宽阔中间调，去掉瓷亮高光与强制高饱和",
    "NECK_STUMP_VISIBLE": "改用隐形人体支撑：领口与肩线保持完整，"
                          "完全隐藏内部支撑物，不得出现肉色颈桩、断颈或皮肤残端",
    "HEAD_REMAINS_IN_COSTUME_VIEW": "移除头部、后脑勺、发型与残留下巴；"
                                    "本任务是服装独立图，画面中不得出现任何头部结构",
    "FORBIDDEN_MULTI_PANEL": "改为单张单幅画面，禁止多格、分格线或人物板；"
                             "拼版只能由已通过图片在本地合成",
    "SUBJECT_CROPPED": "调整构图让主体完整入画（全身图必须头顶到脚齐全）",
    "BODY_PROPORTION_ERROR": "回到正确头身比与体块关系，重心落地",
    "IDENTITY_DRIFT": "以「正脸」身份图为准重注入骨相与五官，不得重新设计",
    "FRONT_BACK_INCONSISTENT": "以正面图为结构参考重做背面，统一画布、主体占比、"
                               "相机高度、站姿与焦段",
    "COSTUME_FRONT_BACK_MISMATCH": "同一件服装的正背结构必须对应，统一款式与材质分区",
    "COSTUME_INCOMPLETE": "补齐缺失的服装件与配饰，保持与服装独立图一致",
    "SPACE_ANCHOR_MISSING": "按资产索引补回固定锚点与出入口，不得替换为其他陈设",
    "SPACE_FACT_INVENTED": "删除原文未写明的结构与陈设，回到事实边界内",
    "PROP_STATE_MISMATCH": "按原文状态重做道具（破损/发光/浸水等）",
    "PROP_SILHOUETTE_UNREADABLE": "简化背景与细节，先让道具轮廓可读",
    "MATERIAL_MISMATCH": "重注入材质参考，只决定表面反应，不覆盖资产身份",
    "EXTRA_SUBJECT": "移除画面中的额外主体（第二角色/背景路人/人物肢体）",
    "TEXT_OR_WATERMARK": "移除画面中的文字、标注与水印",
}


class InvalidVisualTransition(ValueError):
    """非法状态跳转（白名单之外一律拒绝）。"""

    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"非法状态跳转：{current} → {target}"
                         f"（允许 {LEGAL_TRANSITIONS.get(current, ())}）")


class ImageChecker(Protocol):
    """检查器协议：人工/Codex/Gemini 都实现 check。"""

    def check(self, task: VisualTask, attempt: AttemptRecord, lib: Path) -> QcResult:
        ...  # pragma: no cover


def transition(task: VisualTask, target_status: str) -> VisualTask:
    """按白名单转换状态（保留轮次历史与人工锁定字段）。"""
    allowed = LEGAL_TRANSITIONS.get(task.status, ())
    if target_status not in allowed:
        raise InvalidVisualTransition(task.status, target_status)
    return replace(task, status=target_status)


def build_repair_prompt(previous_request: str, qc_issues: Sequence[QcIssue],
                        previous_prompt: str | None = None, strict: bool = False) -> str:
    """依据结构化问题码改写修正指令。

    同一批修正指令若在上一轮已经提过（模型没改），视为 `INEFFECTIVE_RETRY` 并显式失败；
    `strict` 仅为签名兼容保留（该判定本身始终生效）。
    """
    _ = strict
    issues = [i for i in qc_issues if getattr(i, "code", "")]
    if not issues:
        raise ValueError("返工必须依据结构化问题码（issues 为空不得返工）")
    base = str(previous_request or "").strip()
    lines = ["【本轮定向修正】"]
    for issue in issues:
        hint = REPAIR_HINTS.get(issue.code) or str(getattr(issue, "repair_instruction", "") or "")
        if not hint:
            hint = "按该问题码修正（检查器未给出细则）"
        evidence = str(getattr(issue, "evidence", "") or "").strip()
        lines.append(f"- {issue.code}｜{hint}" + (f"（依据：{evidence}）" if evidence else ""))
    if any(i.code == "STYLE_MISMATCH" for i in issues):
        lines.append("- 重注入风格参考图，风格约束不得低于首轮")
    repair_block = "\n".join(lines).strip()
    if previous_prompt is not None and repair_block in str(previous_prompt):
        raise ValueError(
            f"{INEFFECTIVE_RETRY}：同一批修正指令已在上一轮提过，模型未按指令修改；"
            "请更换修正策略或转入人工决定")
    return "\n".join([
        "以下是上一轮提示词与本次定向修正指令，请只修正指出的问题，其余保持逐字不变：",
        "",
        base,
        "",
        repair_block,
    ]).rstrip() + "\n"


def _accepted_status(task: VisualTask) -> str:
    """通过后直接可作为下游参考（本任务自身已通过检查）。"""
    return S.READY_AS_REFERENCE.value


def run_task(task: VisualTask, executor: Any, checker: ImageChecker | None = None, lib: Path = None,
             plan: VisualPlan | None = None,
             request: GenerationRequest | None = None) -> VisualTask:
    """正式路径：**只生成到「等待审核」**，不终审（阶段 6 工单 6.4）。

    Gemini/Codex/手工三条生成路径都到这里为止：生成 → 落盘 → 转 `WAITING_REVIEW`。
    通过与否只由当前主模型的结构化审核结果决定（`review-export` / `review-import`），
    生成器永远没有签发权；`checker` 参数保留仅为兼容旧调用，**不参与自动通过**。
    """
    _ = checker
    lib = Path(lib)
    if not task.style_slug or not task.style_version or not task.style_hash:
        raise ValueError("STYLE_UNPINNED：任务未固定风格包版本与内容哈希，拒绝执行")
    if request is None:
        request = GenerationRequest(
            task_id=task.task_id,
            prompt=_base_prompt(task),
            negative_prompt=task.negative_prompt,
            references=task.references,
            aspect_ratio=task.aspect_ratio,
            style_name=(plan.style_name if plan else task.style_slug),
            style_version=task.style_version,
        )
    current = transition(task, S.GENERATED.value)
    result: GenerationResult = executor.generate(request)
    if getattr(result, "awaiting_manual", False):
        # 无 API 场景：停在等待手工结果，不产生轮次、不冒充生成成功
        return replace(current, status=S.AWAITING_MANUAL_RESULT.value,
                       notes=(current.notes or "") + "｜已导出手工任务包，等待人工回传")
    record = save_attempt(lib, current, result, request.to_manifest())
    current = replace(current, attempts=(*current.attempts, record))
    current = transition(current, S.WAITING_REVIEW.value)
    return replace(current, notes=(current.notes or "")
                   + "｜已生成，等待当前主模型审核（生成器无签发权）")


def run_task_with_checker(task: VisualTask, executor: Any, checker: ImageChecker, lib: Path = None,
                          plan: VisualPlan | None = None,
                          request: GenerationRequest | None = None) -> VisualTask:
    """**测试专用**：带内联检查器的三轮定向返工状态机。

    正式路径不得调用本函数——发布路径只认主模型的结构化审核结果（见 `run_task`）。
    保留它是为了继续验证「三轮上限、重复指令无效、定向修复语」这些编排规则。
    """
    lib = Path(lib)
    if not task.style_slug or not task.style_version or not task.style_hash:
        raise ValueError("STYLE_UNPINNED：任务未固定风格包版本与内容哈希，拒绝执行")
    if request is None:
        request = GenerationRequest(
            task_id=task.task_id,
            prompt=_base_prompt(task),
            negative_prompt="",
            references=task.references,
            aspect_ratio=task.aspect_ratio,
            style_name=(plan.style_name if plan else task.style_slug),
            style_version=task.style_version,
        )

    current = task
    round_no = 0
    prompt = request.prompt
    while round_no < MAX_AUTO_ROUNDS:
        round_no += 1
        current = transition(current, S.GENERATED.value)
        req = replace(request, prompt=prompt)
        result: GenerationResult = executor.generate(req)

        if getattr(result, "awaiting_manual", False):
            # 无 API 场景：停在等待手工结果，不产生轮次、不冒充生成成功
            return replace(current, status=S.AWAITING_MANUAL_RESULT.value,
                           notes=(current.notes or "") + "｜已导出手工任务包，等待人工回传")

        record = save_attempt(lib, current, result, req.to_manifest())
        current = replace(current, attempts=(*current.attempts, record))
        current = transition(current, S.QC_PENDING.value)

        qc = checker.check(current, record, lib)
        write_qc_result(lib, record, qc)
        current = replace(current, attempts=tuple(
            replace(a, qc=a.qc if a.round != record.round else record.qc)
            for a in current.attempts))

        if qc.decision in ("ACCEPTED",):
            current = transition(current, S.ACCEPTED.value)
            return transition(current, _accepted_status(current))

        blocking = [i for i in qc.issues if i.severity == BLOCKING] or list(qc.issues)
        if not blocking:
            blocking = [QcIssue(code="STYLE_MISMATCH", severity=BLOCKING,
                                evidence=qc.untrustworthy_reason or "检查未给出结构化问题",
                                repair_instruction="按风格基线重做")]
        if round_no >= MAX_AUTO_ROUNDS:
            current = transition(current, S.NEEDS_HUMAN_DECISION.value)
            return replace(current, notes=(current.notes or "")
                           + f"｜{MAX_AUTO_ROUNDS} 轮仍未通过，进入人工决定")
        current = transition(current, S.NEEDS_REVISION.value)
        try:
            prompt = build_repair_prompt(prompt, blocking, previous_prompt=prompt)
        except ValueError:
            # 同一批修正指令重复 → 返工无效，不再消耗额度，直接转人工决定
            current = transition(current, S.NEEDS_HUMAN_DECISION.value)
            return replace(current, notes=(current.notes or "")
                           + f"｜{INEFFECTIVE_RETRY}：修正指令与上一轮重复，"
                             "自动返工无效，转人工决定")
    return current


def _base_prompt(task: VisualTask) -> str:
    return (f"【任务目标】{task.kind}\n【不可变事实】{task.fact_source}\n"
            f"（由 create_visual_plan/请求编译器填写的正式提示词）")


def propagate_upstream_change(plan: VisualPlan, changed_task_id: str) -> VisualPlan:
    """上游变化传播：直接下游的已通过任务转为 NEEDS_REVIEW（旧图保留，取消参考资格）。"""
    affected = {t.task_id for t in plan.tasks if changed_task_id in t.depends_on}
    if not affected:
        return plan
    updated: list[VisualTask] = []
    for task in plan.tasks:
        if task.task_id in affected and task.status in (S.ACCEPTED.value,
                                                        S.READY_AS_REFERENCE.value):
            updated.append(replace(task, status=S.NEEDS_REVIEW.value))
        else:
            updated.append(task)
    return replace(plan, tasks=tuple(updated))
