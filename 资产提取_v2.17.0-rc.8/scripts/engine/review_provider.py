#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可移植的主模型审核提供者（阶段 6 工单 6.1 / 6.5）。

普通 Python 进程不能假设自己可以直接调用当前对话里的主模型，因此审核走文件往返：

1. `prepare(task)`：生成结构化审核请求（任务事实、图片与参考图相对路径、六项绑定哈希、逐项清单）；
2. 当前宿主的主模型读图后写出 `review-result.json`；
3. `collect(task)`：读取结果并校验六项绑定；对不上就拒绝写回状态。

`FileReviewProvider` 是所有宿主的最低实现；宿主若能直接调用主模型，可注册自己的适配器，
但输出必须通过同一 `validate_review_binding`。当前主模型不能读图时任务停在等待审核，
**绝不伪造通过**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .review_contract import (
    ReviewContractError,
    ReviewRequest,
    ReviewResult,
    build_review_request,
    load_review_result,
    result_path,
    validate_review_binding,
    write_review_request,
)
from .visual_schema import VisualPlan, VisualTask, VisualTaskStatus

REVIEW_HOST_CANNOT_READ_IMAGES = "REVIEW_HOST_CANNOT_READ_IMAGES"


class ReviewProvider(Protocol):
    """宿主审核适配器协议（两条路径都必须遵守同一数据契约）。"""

    def prepare(self, task: VisualTask) -> Path: ...
    def collect(self, task: VisualTask) -> ReviewResult | None: ...


class FileReviewProvider:
    """文件往返实现：导出请求、等待主模型、导入结果并校验绑定。"""

    def __init__(self, lib: Path, plan: VisualPlan) -> None:
        self.lib = Path(lib)
        self.plan = plan

    def prepare(self, task: VisualTask) -> Path:
        request = build_review_request(self.lib, self.plan, task)
        return write_review_request(self.lib, request)

    def collect(self, task: VisualTask) -> ReviewResult | None:
        """读取并校验审核结果；未产出结果时返回 None（任务继续等待，不自动通过）。"""
        path = result_path(self.lib, task.task_id)
        if not path.is_file():
            return None
        result = load_review_result(path)
        request = self._request_for(task)
        validate_review_binding(request, result)
        return result

    def _request_for(self, task: VisualTask) -> ReviewRequest:
        """重建请求（现算六项哈希），用于与结果比对。"""
        return build_review_request(self.lib, self.plan, task)


def import_review_decision(plan: VisualPlan, task: VisualTask, result: ReviewResult) -> VisualTask:
    """把审核判定写回任务状态（发布与否由发布器单独决定）。

    - `ACCEPTED` / `PASS_WITH_NOTES` → `ACCEPTED`（随后由 `publish` 变成正式版本）
    - `NEEDS_REVISION` → `NEEDS_REVISION`
    - `NEEDS_HUMAN_DECISION` → `NEEDS_HUMAN_DECISION`
    """
    from dataclasses import replace

    _ = plan
    mapping = {
        "ACCEPTED": VisualTaskStatus.ACCEPTED.value,
        "PASS_WITH_NOTES": VisualTaskStatus.ACCEPTED.value,
        "NEEDS_REVISION": VisualTaskStatus.NEEDS_REVISION.value,
        "NEEDS_HUMAN_DECISION": VisualTaskStatus.NEEDS_HUMAN_DECISION.value,
    }
    target = mapping.get(str(result.decision))
    if target is None:
        raise ReviewContractError("REVIEW_DECISION_UNKNOWN",
                                  f"未知判定 {result.decision!r}，不能写回状态")
    note = (task.notes or "") + f"｜主模型审核：{result.decision}"
    if result.decision == "PASS_WITH_NOTES":
        note += "（通过但有备注：备注进入正式清单）"
    return replace(task, status=target, notes=note)
