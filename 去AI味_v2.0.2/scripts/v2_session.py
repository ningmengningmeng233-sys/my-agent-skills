#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_session.py - v2 中文入口与硬授权门（alpha.1 P02 + 补正 Q01/Q02）。

状态机合同（设计 6 节；用户要求 2/4/5/6/8 节）：

  - **正常链**：WAIT_MODE →(显式 mode)→ WAIT_JEV →(显式 jev)→
    WAIT_TASK_CONFIRMATION →(摘要确认)→ READY → RUNNING → COMPLETED。
    启用 Jev 但无可信凭据验证先落 WAIT_JEV_KEY（凭据验证只来自受信任
    Provider 通路 ``apply_credential_result``，用户事件不得伪造）；高风险
    候选预览门 WAIT_PATCH_CONFIRMATION 与裁判暂停 PAUSED_JUDGE_UNAVAILABLE
    在转移表内（P12/P13 接线）。
  - **auto_transitions=False**：一切状态变化必须经具名 trigger；不存在 to_*
    直达捷径。trigger 失败（当前状态不允许该转移）= 原地不动，不是错误——
    「开始吧」这类催促在等待态就是被静置，而不是被解释成默认答案。
  - **event 只认当前用户输入**：source 不是 ``user`` 的事件（稿内材料、系统
    消息、工具输出）整体拒收；fields 出现合同外字段直接 ValueError。
  - **零部分应用**（补正方案 3.4）：同一事件可携带多个独立选择，但整个事件
    先验证，再一次性应用——非法字段使原记录完全不变。
  - **摘要绑定授权**（补正方案 3.2）：摘要由 ``v2_contracts.task_digest``
    从任务内容唯一生成；task_confirmed 必须携带用户看到的 expected_digest
    且与实际任务内容一致；require_ready 核对完整内容、task_id、实际选择、
    版本与摘要。换范围/换裁判/换模式都换摘要，旧确认随之失效。

用户可见文案（入口三选一、Jev 必问、任务摘要）在 ``references/v2-interaction-zh.md``，
本模块只负责状态与授权，不重复抄文案。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from transitions import Machine

import v2_contracts as vc

# event.fields 合同字段集。凭据验证结果只从受信任 Provider 通路
# ``apply_credential_result`` 进入（补正方案 3.3）——用户事件没有
# credential_ok 字段，验证是验证器的事实，不是用户可声明的事实；
# expected_digest 是用户确认时看到的摘要回执——服务端不得把"当前 spec 的
# 任意字符串"塞进授权记录。
EVENT_FIELDS: tuple[str, ...] = ("mode", "jev", "scene", "strength",
                          "task_confirmed", "expected_digest")

# 直接推进状态机的选择字段（task_confirmed/expected_digest 单独处理）。
_STATE_FIELDS: tuple[str, ...] = ("mode", "jev")

# 事件源：只有当前用户输入能推进状态。
USER_SOURCE = "user"

EVENT_KEYS: tuple[str, ...] = ("source", "request_id", "fields")


class NotReadyError(RuntimeError):
    """正文写入授权不足：状态未到 READY，或任务内容/选择/摘要已不一致。"""


@dataclass
class _Host:
    """transitions Machine 的宿主；state 字符串由 Machine 维护。"""

    state: str


def build_machine(initial_state: str) -> _Host:
    """构造交互状态机：转移表是授权门的唯一权威（设计 6 节）。

    auto_transitions=False 关闭 to_<state> 捷径；「条件不足就原地不动」由
    apply_event 先判字段再 trigger 实现——Machine 负责「哪个状态允许哪条边」，
    不负责「这个事件该不该被理解」。
    """
    host = _Host(state=initial_state)
    machine = Machine(
        model=host,
        states=list(vc.INTERACTION_STATES),
        initial=initial_state,
        auto_transitions=False,
        # 非法源状态上的 trigger 返回 False（原地不动）而不是抛 MachineError：
        # 「开始吧」这类催促在等待态就是被静置，不是错误。
        ignore_invalid_triggers=True,
    )
    host.machine = machine
    machine.add_transition("submit_mode", "WAIT_MODE", "WAIT_JEV")
    machine.add_transition("submit_jev_enabled", "WAIT_JEV", "WAIT_JEV_KEY")
    machine.add_transition(
        "submit_jev_disabled", "WAIT_JEV", "WAIT_TASK_CONFIRMATION")
    # 显式改选"不用 Jev"可以退出凭据等待：关闭即无需凭据。
    machine.add_transition(
        "submit_jev_disabled", "WAIT_JEV_KEY", "WAIT_TASK_CONFIRMATION")
    machine.add_transition("credential_confirmed", "WAIT_JEV_KEY",
                           "WAIT_TASK_CONFIRMATION")
    # 事件改选/换裁判使凭据失效后，重新进入凭据等待门（补正 R01）。
    machine.add_transition("require_credential", "WAIT_TASK_CONFIRMATION",
                           "WAIT_JEV_KEY")
    machine.add_transition("confirm_task", "WAIT_TASK_CONFIRMATION", "READY")
    machine.add_transition("invalidate_summary", "READY",
                           "WAIT_TASK_CONFIRMATION")
    machine.add_transition("start_run", "READY", "RUNNING")
    machine.add_transition("enter_patch_preview", "RUNNING",
                           "WAIT_PATCH_CONFIRMATION")
    machine.add_transition("accept_patches", "WAIT_PATCH_CONFIRMATION", "RUNNING")
    machine.add_transition("pause_task", "RUNNING", "PAUSED")
    machine.add_transition("judge_unavailable", "RUNNING",
                           "PAUSED_JUDGE_UNAVAILABLE")
    machine.add_transition("resume_run", "PAUSED", "RUNNING")
    machine.add_transition("resume_run", "PAUSED_JUDGE_UNAVAILABLE", "RUNNING")
    machine.add_transition("fail_task", "RUNNING", "FAILED")
    machine.add_transition("fail_task", "PAUSED", "FAILED")
    machine.add_transition("complete_task", "RUNNING", "COMPLETED")
    return host


def route_request(explicit: dict) -> vc.RequestChoices:
    """入口显式字段 → RequestChoices；缺字段 None，未知/非法值 ValueError。

    用户只说「去AI味并润色」这类三选一、或「不用 Jev」这类单字段时，其余
    字段保持 None 等待显式询问——推荐不是授权（用户要求第 5 节）。
    """
    return vc.RequestChoices.from_mapping(explicit)


def _validate_event(event: Mapping) -> Mapping:
    """事件壳与字段预检（零部分应用）：全部字段合法才返回 fields。

    - source 不是 ``user`` 的事件整体拒收（返回空 fields，调用方原地返回）。
    - fields 出现合同外字段、字段类型/枚举非法、task_confirmed=true 缺
      expected_digest，一律 ValueError——调用方拿到的原记录完全不变。
    """
    if not isinstance(event, Mapping):
        raise ValueError(f"event 必须是映射: {event!r}")
    extra = sorted(set(event) - set(EVENT_KEYS))
    if extra:
        raise ValueError(f"event 含未知字段: {','.join(extra)}")
    if event.get("source") != USER_SOURCE:
        return {}
    request_id = event.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("event.request_id 必须是非空字符串")
    fields = event.get("fields") or {}
    if not isinstance(fields, Mapping):
        raise ValueError(f"event.fields 必须是映射: {fields!r}")
    unknown = sorted(set(fields) - set(EVENT_FIELDS))
    if "credential_ok" in fields:
        # 用户事件不允许伪造凭据验证结果（补正方案 3.3）：点名拒收，
        # 错误信息直接指路，避免宿主误以为只是字段名写错。
        raise ValueError("event.fields.credential_ok 不被接受：凭据验证结果"
                         "只能来自受信任 Provider 通路 apply_credential_result")
    if unknown:
        raise ValueError(f"event.fields 含未知字段: {','.join(unknown)}")
    # 先验证整个事件，再一次性应用（补正方案 3.4：不能半应用非法字段）。
    if "mode" in fields:
        if not isinstance(fields["mode"], str):
            raise ValueError(f"event.fields.mode 必须是字符串: {fields['mode']!r}")
        vc.RequestChoices.from_mapping({"mode": fields["mode"]})  # 枚举校验
    if "jev" in fields:
        if not isinstance(fields["jev"], str):
            raise ValueError(f"event.fields.jev 必须是字符串: {fields['jev']!r}")
        vc.RequestChoices.from_mapping({"jev": fields["jev"]})  # 枚举校验
    if "credential_ok" in fields:
        # 用户事件不允许伪造凭据验证结果（补正方案 3.3）：整体拒收。
        raise ValueError("event.fields.credential_ok 不被接受：凭据验证结果"
                         "只能来自受信任 Provider 通路 apply_credential_result")
    if "scene" in fields:
        # 场景同理：用户说出来的场景才是 user_explicit；代理判出的走载荷，记受控识别。
        if not isinstance(fields["scene"], str):
            raise ValueError(f"event.fields.scene 必须是字符串: {fields['scene']!r}")
        if fields["scene"] not in vc.SCENES:
            raise ValueError(f"event.fields.scene 只能是 {'/'.join(vc.SCENES)}，"
                             f"收到 {fields['scene']!r}")
    if "strength" in fields:
        # 力度只能由用户事件明示进入；来源记 user_explicit，绝不由数值反推。
        if not isinstance(fields["strength"], str):
            raise ValueError(f"event.fields.strength 必须是字符串: "
                             f"{fields['strength']!r}")
        if fields["strength"] not in vc.STRENGTHS:
            raise ValueError(f"event.fields.strength 只能是 "
                             f"{'/'.join(vc.STRENGTHS)}，收到 {fields['strength']!r}")
    if "task_confirmed" in fields and not isinstance(fields["task_confirmed"], bool):
        raise ValueError(f"event.fields.task_confirmed 必须是布尔: "
                         f"{fields['task_confirmed']!r}")
    if "expected_digest" in fields:
        vc._require_hex(fields["expected_digest"],
                        "event.fields.expected_digest")
    if fields.get("task_confirmed") is True and "expected_digest" not in fields:
        raise ValueError("task_confirmed=true 必须携带用户看到的 expected_digest"
                         "（摘要确认事件不能没有内容绑定）")
    return fields


def _apply_field(host: _Host, record: vc.SessionRecord, event: Mapping,
                 field: str, value) -> vc.SessionRecord:
    """应用单个明确字段；转移不被当前状态允许时原地不动（返回原记录）。"""
    request_id = event["request_id"]
    if field == "mode":
        if host.trigger("submit_mode"):
            return replace(record, interaction_state=host.state,
                           authorization_event_refs=record.authorization_event_refs
                           + (request_id,))
        return record
    if field == "jev":
        trigger = "submit_jev_enabled" if value == "enabled" else "submit_jev_disabled"
        if host.trigger(trigger):
            return replace(record, interaction_state=host.state,
                           authorization_event_refs=record.authorization_event_refs
                           + (request_id,))
        return record
    raise ValueError(f"event.fields 含未知字段: {field}")  # pragma: no cover


def _receipt_bound(receipt, spec: vc.TaskSpec) -> bool:
    """receipt 是否为与当前裁判绑定一致的可信验证结果（补正方案 3.3）。"""
    return (isinstance(receipt, vc.CredentialReceipt) and receipt.verified
            and receipt.provider == spec.judge_ref.get("provider")
            and receipt.model == spec.judge_ref.get("model"))


def mark_summary_presented(record: vc.SessionRecord, spec: vc.TaskSpec,
                           *, request_id: str) -> vc.SessionRecord:
    """记下「这份任务版本的具体摘要已在 ``request_id`` 那一回合展示给用户」。

    G2 补正的核心接线：展示是一次**有记录的事件**，不是一句措辞。确认事件必须
    指向一个**不同的、更晚的**回合，Agent 不能在同一轮里自己展示、自己算摘要码、
    自己确认。只补这一处记录，不新建第二个状态机或授权引擎。
    """
    if record.interaction_state != "WAIT_TASK_CONFIRMATION":
        return record
    # 空回合号会让这条记录只剩一半：revision 与摘要哈希写进去、request_id 落成 None，
    # 于是下一回合的确认被拒且查不出原因（G6 宿主实跑撞到的形状）。要写就得给得出回合。
    shown_in = str(request_id or "").strip()
    if not shown_in:
        raise ValueError("request_id 必须是非空字符串：摘要展示是一次有记录的事件，"
                         "没有回合号就不能登记为已展示")
    return replace(record, presented_revision=spec.task_revision,
                   presented_request_id=shown_in,
                   presented_summary_sha256=summary_sha256(spec))


def apply_request_event(record: vc.SessionRecord, event: dict,
                        spec: vc.TaskSpec) -> tuple[vc.TaskSpec, vc.SessionRecord]:
    """新旧规格协调入口（补正方案 3.3/3.4）：事件只接纳当前用户显式选择。

    - 实际选择写进 SessionRecord.choices；与规格不一致时按同一版本重造
      TaskSpec（不允许事件选择一套、执行另一套）。
    - READY 上模式/裁判变化先失效旧摘要确认，再等待重新确认。
    - 摘要确认必须携带 expected_digest 且等于实际任务内容摘要
      （task_digest(spec_of_record)）；不符保持等待，不绑定任何授权。
    - 返回 (协调后规格, 新会话记录)；调用方必须持久化并执行返回的规格。
    """
    fields = _validate_event(event)
    if not fields:
        return spec, record
    # 合并显式选择：以会话已记录选择为基线；无记录时以规格选择为基线
    #（规格本身来自任务创建时的显式选择，不虚构新同意）。
    base = record.choices if record.choices is not None else spec.choices
    merged = base
    if "mode" in fields or "jev" in fields:
        merged = replace(base, mode=fields.get("mode", base.mode),
                         jev=fields.get("jev", base.jev))
    # 规格协调：实际选择 ≠ 规格选择 → 同版本重造规格（摘要由中央实现重算）。
    spec_of_record = spec
    if merged != spec.choices:
        content = vc.content_of_task_spec(spec)
        content["choices"] = {"mode": merged.mode, "jev": merged.jev,
                              "scan": merged.scan, "output": merged.output,
                              "explicit_sources": list(merged.explicit_sources)}
        spec_of_record = vc.form_task_spec(content)
    if "scene" in fields or "strength" in fields:
        # 用户事件给场景/力度：值与来源一起进规格，来源写 user_explicit；
        # 同值重记也算变更（来源含义变了就是另一份授权，仍要重新确认）。
        content = vc.content_of_task_spec(spec_of_record)
        changed = False
        if "scene" in fields and (content["scene"] != fields["scene"]
                                  or content["scene_origin"] != "user_explicit"):
            content["scene"] = fields["scene"]
            content["scene_origin"] = "user_explicit"
            changed = True
        if "strength" in fields and (content["strength"] != fields["strength"]
                                     or content["strength_origin"] != "user_explicit"):
            content["strength"] = fields["strength"]
            content["strength_origin"] = "user_explicit"
            changed = True
        if changed:
            spec_of_record = vc.form_task_spec(content)
    host = build_machine(record.interaction_state)
    current = record
    # READY 上的选择/内容变化使旧摘要确认失效（设计 6 节：模式/裁判/范围
    # 变化不能沿用旧授权；裁判等绑定字段变化体现为摘要不一致）。
    if (record.interaction_state == "READY"
            and (merged != base
                 # 值或来源真的变了才失效；重复声明同一份不额外作废授权
                 # （场景变了也一样：默认力度与规则层都跟着场景走）
                 or spec_of_record.request_digest != spec.request_digest
                 or record.summary_digest != spec_of_record.request_digest)):
        if host.trigger("invalidate_summary"):
            current = replace(current, interaction_state=host.state,
                              summary_digest=None, confirmed_revision=None,
                              presented_revision=None, presented_request_id=None,
                              presented_summary_sha256=None)
    # 启用 Jev 而无可信凭据（重新）进入凭据等待门（补正 R01）：
    # 事件改选启用或换裁判后，必须重新验证才能走向摘要确认。
    if (current.interaction_state == "WAIT_TASK_CONFIRMATION"
            and merged.jev == "enabled"
            and not _receipt_bound(current.credential_receipt, spec_of_record)):
        if host.trigger("require_credential"):
            current = replace(current, interaction_state=host.state)
    for field in _STATE_FIELDS:
        if field not in fields:
            continue
        current = _apply_field(host, current, event, field, fields[field])
    if fields.get("task_confirmed") is True:
        expected = fields["expected_digest"]
        # 摘要核对：用户确认的必须是将要执行的任务内容；不符保持等待。
        # G2 补正：还必须确认**在前一回合展示过的那份摘要**——同一回合里
        # 自己算码自己确认（「按摘要来」「开始吧」「都行」）不算确认。
        # 展示事实三件齐全：同一任务版本、同一份摘要文本、且确认来自**另一次**
        # 用户事件（source 不是 user 的事件在入口就整体拒收；换一个 request_id 不算回合）。
        shown = (record.presented_revision == spec_of_record.task_revision
                 and record.presented_request_id is not None
                 and record.presented_summary_sha256 == summary_sha256(spec_of_record)
                 and str(event["request_id"]) != record.presented_request_id)
        if (shown and expected == vc.task_digest(spec_of_record)
                and host.trigger("confirm_task")):
            current = replace(current, interaction_state=host.state,
                              summary_digest=expected,
                              confirmed_revision=spec_of_record.task_revision,
                              authorization_event_refs=
                              current.authorization_event_refs
                              + (event["request_id"],))
    if "mode" in fields or "jev" in fields:
        current = replace(current, choices=merged,
                          task_revision=spec_of_record.task_revision)
    return spec_of_record, current


def apply_judge_failure(record: vc.SessionRecord, *, reason: str) -> vc.SessionRecord:
    """裁判调用故障的会话侧接线（alpha.3 P09）。

    - 只有 **RUNNING** 会被具名 trigger ``judge_unavailable`` 推进到
      ``PAUSED_JUDGE_UNAVAILABLE``；等待态（含 WAIT_JEV_KEY）原地不动——
      凭据还没验证完就报「裁判不可用」不是暂停，是继续等；
    - ``judge_epoch`` 不变：故障不是换裁判的理由，换裁判必须由用户当前明确
      事件驱动（P13），这里绝不静默改 provider 或 epoch；
    - ``reason`` 必须是非空字符串且不含凭据材料，由调用方写进现有 job 事件层
      （P13 接线）；本函数不新开一套状态账本。
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"judge failure 需要非空 reason，收到 {reason!r}")
    host = build_machine(record.interaction_state)
    if host.trigger("judge_unavailable"):
        return replace(record, interaction_state=host.state)
    return record


def apply_credential_result(record: vc.SessionRecord,
                            result: vc.CredentialReceipt,
                            spec: vc.TaskSpec) -> vc.SessionRecord:
    """受信任凭据验证通路（补正方案 3.3）：验证器的事实从这里进入会话。

    - receipt 必须 verified 且 provider/model 与当前 spec.judge_ref 绑定一致，
      否则清除已有凭据并保持等待——绝不放行；
    - 绑定一致且当前处于 WAIT_JEV_KEY：推进到 WAIT_TASK_CONFIRMATION 并记录
      receipt；其他状态不接纳（调用方需按状态门路由）。
    - alpha.1 没有真实 Jev 通路（P09）：桩验证结果只证明接口行为，
      不构成实际联通。
    """
    if not isinstance(result, vc.CredentialReceipt):
        raise ValueError(f"credential result 必须是 CredentialReceipt: "
                         f"{result!r}")
    if not _receipt_bound(result, spec):
        # 绑定不符（换裁判/换模型/未通过）：旧凭据一并清除，保持等待。
        if record.credential_receipt is None:
            return record
        return replace(record, credential_receipt=None)
    host = build_machine(record.interaction_state)
    if host.trigger("credential_confirmed"):
        return replace(record, interaction_state=host.state,
                       credential_receipt=result)
    return record


def apply_event(record: vc.SessionRecord, event: dict,
                spec: vc.TaskSpec) -> vc.SessionRecord:
    """旧签名严格入口：事件显式选择与任务规格矛盾即拒绝（补正工单 Q01）。

    需要按用户实际选择改写任务规格时，必须走 ``apply_request_event`` 协调
    并持久化返回的新规格——本入口不接受"事件一套、执行另一套"。
    """
    spec_of_record, current = apply_request_event(record, event, spec)
    if spec_of_record != spec:
        raise ValueError("事件显式选择与任务规格不一致：拒绝放行；"
                         "需要按实际选择更新任务必须走 apply_request_event 协调")
    return current


def require_ready(record: vc.SessionRecord, spec: vc.TaskSpec) -> None:
    """正文写入前的完整授权门（补正方案 3.4）：任何一项不符都拒绝放行。

    - 状态必须 READY；
    - 记录与规格 task_id 一致（授权不能跨任务复用）；
    - 记录的实际选择与规格一致（不能按另一套选择执行）；
    - 确认版本与当前任务版本一致；
    - 摘要一致，且按当前内容重算摘要仍一致（确认后改内容即失效）；
    - 启用 Jev 时必须持有与当前裁判绑定一致的可信凭据验证 receipt
      （补正 R01：未验证凭据不得 READY；没有真实验证通路就保持等待）。
    """
    if record.interaction_state != "READY":
        raise NotReadyError(
            f"当前状态 {record.interaction_state} 不允许写入正文：必须先完成"
            "处理方式确认、Jev 询问与任务摘要确认")
    if record.task_id != spec.task_id:
        raise NotReadyError(
            f"会话记录 task_id={record.task_id!r} 与任务规格 "
            f"task_id={spec.task_id!r} 不一致：授权不能跨任务复用")
    if record.choices != spec.choices:
        raise NotReadyError(
            "会话记录的实际选择与任务规格不一致：不能按另一套选择执行，"
            "必须以用户显式选择重新协调")
    if record.confirmed_revision != spec.task_revision:
        raise NotReadyError(
            "确认的任务版本与当前任务版本不一致：任务内容变化后必须重新确认摘要")
    if (record.presented_revision != spec.task_revision
            or not record.presented_request_id):
        raise NotReadyError(
            "当前任务版本的具体摘要未曾展示：不存在可确认的摘要，"
            "必须先展示具体摘要，再等用户在后续回合确认")
    if record.presented_summary_sha256 != summary_sha256(spec):
        raise NotReadyError(
            "确认所绑定的摘要文本与当前规格要展示的那一份不一致：那次确认作废，"
            "必须重新展示具体摘要再等确认")
    if record.summary_digest is None or record.summary_digest != spec.request_digest:
        raise NotReadyError(
            "任务摘要未确认或与规格不一致：必须重新确认任务摘要")
    receipt = record.credential_receipt
    if spec.choices.jev == "enabled":
        if receipt is None:
            raise NotReadyError(
                "已启用 Jev 但没有可信凭据验证结果：必须先完成凭据验证"
                "（验证结果只能来自受信任 Provider 通路）")
        if not _receipt_bound(receipt, spec):
            raise NotReadyError(
                "凭据验证结果与当前裁判绑定不一致：换裁判后必须重新验证")
    if vc.task_digest(spec) != spec.request_digest:
        raise NotReadyError(
            "任务内容在确认后被改动（重算摘要与声明摘要不符）：旧授权已失效，"
            "必须重新确认")


def summary_sha256(spec: vc.TaskSpec) -> str:
    """实际交到用户手上那份摘要文本的 SHA-256（展示与确认的绑定对象）。"""
    return hashlib.sha256(describe_task(spec).encode("utf-8")).hexdigest()


def authorize_body_output(record: vc.SessionRecord, spec: vc.TaskSpec,
                          *, surface: str) -> None:
    """产出改写正文前的同一道授权门：聊天／文件／内联共用，不另建第二套判断。

    过门条件与 require_ready 完全同源，再加两条：本次规格必须授权写正文
    （只检查模式恒不授权），以及要产出的面在规格值域内。抛错即调用方不得生成，
    生成器一次都不该被碰到。
    """
    if surface not in vc.REQUEST_OUTPUT:
        raise ValueError(f"未知输出面 {surface!r}，合法值 {vc.REQUEST_OUTPUT}")
    require_ready(record, spec)
    if not spec.output_policy.get("write_body"):
        raise NotReadyError(
            f"本次任务未授权产出改写正文（输出面 {surface}）：只检查或未确认的任务"
            "不得把改写稿交到用户手上")


# ---------------------------------------------------------------- 入口调度（Q07）

# 用户可见问句的权威文案在 ``references/v2-interaction-zh.md`` 第 1/2/3 节，这里只
# 按状态选一句开场问话，不复制模板正文，也不因文案而放宽状态机。
_MODE_QUESTION = ("请选择处理方式：1. 只检查；2. 去AI味（推荐）；"
                  "3. 去AI味并润色。也可以直接说「只查不改」。")
_JEV_QUESTION = ("本次是否启用 Jev 裁判模型？1. 配置并启用（需要有效的 Jev API Key）；"
                 "2. 本次不使用 Jev。")
_JEV_KEY_QUESTION = ("请安全输入 Jev API Key 以完成验证（Key 不进聊天记录、报告与日志）；"
                     "或改选本次不使用 Jev。验证未通过前不会进入下一步。")
_SUMMARY_HEADLINE = "本次任务摘要（确认后才开始，未确认不写正文）"

_MODE_LABELS = {"check": "只检查", "clean": "去AI味",
                "clean_polish": "去AI味并润色", "polish_only": "只润色"}
_LIGHT_POLISH = "轻度（仅病句、语序、衔接、重复、节奏、自然表达）"


def _source_scope_lines(spec: vc.TaskSpec) -> list:
    """文件范围行：逐件列出规格里的实际来源，只报数量不算确认范围（S02）。

    - 清单按编号列全，不截断、不用「等等」冒充完整范围；
    - 跨目录同名文件靠完整路径区分，另给一行共同目录说明；
    - 只显示路径与任务字段，不读稿件内容、不回显 Key 或凭据引用；
    - 范围与确认码同源于这份 spec，改范围必然改清单、改清单必然改码。
    """
    paths = [str(ref.path) for ref in spec.source_files]
    lines = [f"- 文件：{len(paths)} 个正文文件，以下清单就是本次全部处理范围，"
             "清单之外的文件一律不动："]
    for index, path in enumerate(paths, 1):
        lines.append(f"  {index}. {path}")
    label = _common_dir_label(paths)
    if label:
        lines.append(f"  共同目录：{label}（不同目录里的同名文件按上面的完整路径区分）")
    return lines


def _common_dir_label(paths: list) -> str | None:
    """多来源时的共同目录（纯展示用）；同目录或找不到公共目录就不显示。"""
    if len(paths) < 2:
        return None
    dirs = []
    for path in paths:
        normalized = path.replace("\\", "/")
        cut = normalized.rfind("/")
        dirs.append(normalized[:cut + 1] if cut >= 0 else "")
    if len(set(dirs)) == 1:
        return None
    import os
    prefix = os.path.commonprefix(dirs)
    prefix = prefix[:prefix.rfind("/") + 1] if "/" in prefix else ""
    return prefix.rstrip("/") or None


def describe_task(spec: vc.TaskSpec) -> str:
    """任务摘要（用户要求第七节）：逐项取自规格实际内容，不填默认答案。"""
    choices = spec.choices
    mode = _MODE_LABELS.get(choices.mode, "未确认")
    jev = {"enabled": "已启用", "disabled": "本次不使用"}.get(choices.jev, "未确认")
    polish = (_LIGHT_POLISH if choices.mode in ("clean_polish", "polish_only")
              else "无")
    write_body = bool(spec.output_policy.get("write_body"))
    lines = [
        _SUMMARY_HEADLINE,
        f"- 处理方式：{mode}",
    ]
    lines += _source_scope_lines(spec)
    lines += [
        f"- Jev：{jev}",
        f"- 润色：{polish}",
        f"- 场景：{_scene_line(spec)}",
        f"- 改写力度：{_strength_line(spec)}",
        "- 作者声音保护：严格（方言、停顿、半句、重复可能是刻意设计，不默认修改）",
        f"- 备份：{'已开启（逐文件备份、哈希绑定、可回滚）' if spec.backup else '未开启'}",
        f"- 是否写入正文：{'是' if write_body else '否（只检查，不落盘改写）'}",
        f"- 确认码：{spec.request_digest}",
    ]
    return "\n".join(lines)


def _scene_line(spec: vc.TaskSpec) -> str:
    """场景读数：场景中文名 + **记录下来的来源**（用户明示／受控识别／未定）。

    代理自己按内容判出的场景不能写成用户选的；用户说出来的才算明示。
    """
    label = vc.SCENE_LABELS.get(spec.scene, spec.scene)
    origin = vc.SCENE_ORIGIN_LABELS.get(spec.scene_origin, str(spec.scene_origin))
    return f"{label}（{origin}）"


def _strength_line(spec: vc.TaskSpec) -> str:
    """力度读数：档位 + **事件记录下来的来源** + 改档要重新确认。

    来源不能由数值相等反推：用户明确选到与默认相同的值仍是明示，
    Agent 自选一个非默认值也不算用户明示（run06 现场就是这么记错的）。
    """
    label = vc.STRENGTH_LABELS.get(spec.strength, spec.strength)
    origin = vc.STRENGTH_ORIGIN_LABELS.get(spec.strength_origin,
                                           str(spec.strength_origin))  # 来源只照抄记录
    return f"{label}（{origin}；改力度需重新确认）"


def default_strength_for(spec: vc.TaskSpec) -> str:
    """这份规格所在场景的合法默认力度（来源判断不用它，只供对照）。"""
    policy = dict(spec.output_policy or {})
    return vc.default_strength(spec.scene, policy.get("output"),
                               bool(policy.get("write_body")))


def next_prompt_for(record: vc.SessionRecord, spec: vc.TaskSpec):
    """当前状态该问用户的那句中文提示；不需要用户回答的状态返回 None。"""
    state = record.interaction_state
    if state == "WAIT_MODE":
        return _MODE_QUESTION
    if state == "WAIT_JEV":
        return _JEV_QUESTION
    if state == "WAIT_JEV_KEY":
        return _JEV_KEY_QUESTION
    if state == "WAIT_TASK_CONFIRMATION":
        return describe_task(spec)
    if state in ("READY", "RUNNING", "COMPLETED", "FAILED", "PAUSED",
                 "PAUSED_JUDGE_UNAVAILABLE", "WAIT_PATCH_CONFIRMATION"):
        # 执行中／终态／P12–P13 才接线的预览与裁判暂停：本阶段不假装会问出下一步。
        return None
    raise ValueError(f"未知交互状态: {state!r}")


def handle_request(job_dir, explicit_event: dict,
                   spec: vc.TaskSpec, *, prior_session=None) -> dict:
    """v2 开发入口的最薄调度（补正方案 7 节 / 工单 Q07 + S01 成对持久化）。

    只做四件事，不新建状态机、UI 或宿主服务：

    1. 用 ``apply_request_event``（Q01/Q02 的权威）协调显式事件与实际选择；
    2. 给了 ``job_dir`` 就先检查有没有未完成的授权更新（半更新一律拒绝，需显式
       ``recover_task_session``），再核对传入规格与 job 已登记规格一致；
    3. 用 ``persist_task_session`` 把协调后的**规格与会话成对**落回该 job
       （Q03/S01）——旧 S0–S7 的「不持久化」只描述单次编辑子流程，v2 授权与断点
       恢复必须落在现有 job 上；
    4. 只在保存完成、配对一致之后给出下一条中文提示，并如实回答能否写正文。

    ``body_write_allowed`` 只在 ``require_ready`` 全项通过**且**规格授权写正文时为真；
    只检查模式恒为假。真实宿主的自然语言触发仍属未验证面，本函数不代替宿主。
    """
    import v2_task
    previous_digest = None
    if job_dir is not None:
        job = Path(job_dir)
        v2_task.require_no_pending_update(job, "推进授权")
        stored = v2_task.load_spec(job)
        if stored != spec:
            raise ValueError("传入的任务规格与 job 已登记的规格不一致："
                             "拒绝据此推进授权（请使用上一次返回的规格或重新加载任务）")
        previous_digest = stored.request_digest
        record = v2_task.load_session(job)
    else:
        # 没有 job 也要能把上一回合的展示记录带回来：``prior_session`` 就是调用方
        # 上一轮拿到的会话记录。不传就按新会话起步——那一定没展示过摘要，
        # 因此同一条事件里的 ``task_confirmed`` 不会被当成确认（G2）。
        record = (prior_session if prior_session is not None
                  else v2_task.initial_session(spec))
    spec, record = apply_request_event(record, explicit_event, spec)
    # 把「具体摘要在这一回合交到用户面前」记成事实（G2）：随后落盘的记录带着
    # presented_revision/presented_request_id，确认事件必须来自**另一个**回合。
    # 纯查询式调用（不带 request_id 的事件，例如 `{}` 只要下一句提示）不登记展示：
    # 没有归属回合就没有可信的展示事实，硬写会造出半成品记录（G6 宿主实跑形状）。
    shown_in = str((explicit_event or {}).get("request_id") or "").strip()
    if shown_in:
        record = mark_summary_presented(record, spec, request_id=shown_in)
    if job_dir is not None:
        v2_task.persist_task_session(Path(job_dir), spec, record,
                                     expected_previous_digest=previous_digest)
        v2_task.require_no_pending_update(Path(job_dir), "返回授权结果")
    # 磁盘面与各输出面同一读数：逐个面都过同一道门，不在这里另写一份判断。
    surfaces = {}
    for surface in vc.REQUEST_OUTPUT:
        try:
            authorize_body_output(record, spec, surface=surface)
        except NotReadyError:
            surfaces[surface] = False
        else:
            surfaces[surface] = True
    body_write_allowed = bool(spec.output_policy.get("write_body")) and surfaces["file"]
    return {"spec": spec, "session": record,
            "next_prompt": next_prompt_for(record, spec),
            "body_write_allowed": body_write_allowed,
            "body_output_allowed": surfaces}
