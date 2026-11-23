#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v2_preview.py - 按章预览、逐项接受，与唯一那条安全提交通路（alpha.3 P12）。

**本模块一个字都不往磁盘上写**：落盘、备份、哈希绑定、原子替换、回滚全部归
``novel_output.publish`` → ``file_commit``（仓库唯一写者）。这里只做四件事：

  1. ``render_preview``：把每条 patch 的原文位置、原文片段、候选片段、中文理由、
     风险与验证状态摊开给人看；文案是确定的（同样输入同样输出），否则「逐项确认」
     就变成抽奖。
  2. ``accept_patches``：只认**当前用户事件**（``source == "user"`` + 带 request_id），
     接受时把该事件写进 ``accepted_by``。拒绝与「再生成」都不是接受。
  3. 三道不猜闸：同形原文出现多次、patch 区间重叠、source 哈希与本次任务不符
     —— 一律不接受，不去挑「看起来对的那一处」。
  4. ``commit_verified``：把提交前置门按顺序核完——**请求同一性**（源路径在授权范围内、
     字节摘要对契约、文本摘要对 bundle、接受集与候选文本一致、输出形态允许落盘）、
     会话授权（模式/Jev/摘要）、高风险逐项接受、P11 的验证结论、bundle 与 patch/任务
     的一致性——**全部过了**才把请求交给现有 ``publish``。任何一道没过就在任何写之前
     抛 ``PreviewGateError``，并把说清原因的细节一起带出去。

两种摘要不许混用（C31）：``TaskSpec`` 订约哈希、``request.expected_source_sha256`` 与
``patch.source_sha256`` 是**磁盘字节摘要**；``bundle.source/candidate_sha256`` 是**文本摘要**。
读取与摘要都取现有权威（``text_source.read_source`` 与 ``novel_contracts.sha256_text``）。

接口对工单的唯一加项：``accept_patches``/``render_preview`` 强制要 ``original`` 原文。
没有原文就没有位置判据，宁可报缺参也不静默跳过这道闸。
"""
from __future__ import annotations

import dataclasses
import difflib
import hashlib
from pathlib import Path

import novel_contracts
import novel_output
import text_source
import v2_contracts as vc
import v2_session as vs

#: 用户事件里允许出现的字段（其余一律拒收，不静默忽略）。
EVENT_FIELDS: tuple[str, ...] = ("accepted_patch_ids", "rejected_patch_ids",
                                 "regenerate_patch_ids")
SOURCE_USER = "user"
EVENT_KEYS: tuple[str, ...] = ("source", "request_id", "fields")

_RISK_LABEL = {"low": "低风险", "high": "高风险"}
_OPERATION_LABEL = {"delete": "无损删除", "merge": "无损合并",
                    "local_rewrite": "局部改写", "paragraph_rewrite": "段内重组"}
_NO_REASON = "未附理由（理由应来自判定与验证，不由本层编造）"
#: 另存交付没有前态也没有备份，登记里用契约层的声明值（单一权威，不在此另造串）。
CREATED_BACKUP_REF = vc.CREATED_BACKUP_REF


class PreviewGateError(ValueError):
    """提交前置门未通过：在任何一个字节被写出去之前拒绝，并列出全部未过码。"""

    def __init__(self, codes: tuple[str, ...], detail: str) -> None:
        super().__init__(f"{detail}：{','.join(codes)}")
        self.codes = tuple(codes)


class CommitRegistrationFailed(RuntimeError):
    """文件已按现有通路写成，但逐文件提交登记没记上——**这不是成功**。

    带上全部已成立的事实（``facts``）与现场留下的 ``commit_intent`` 线索：
    任务层据此能在重启后按哈希证明备份归属、补完登记（见 ``v2_task``）。
    绝不返回一个 PASS 的 PublishResult 让调用方以为闭环已完成。
    """

    def __init__(self, facts: dict, reason: str) -> None:
        super().__init__(f"交付已落盘但提交登记失败：{reason}；"
                         f"file={facts.get('file')} sha256={(facts.get('sha256') or '')[:12]}…")
        self.facts = dict(facts)
        self.reason = reason


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_original(original) -> str:
    if not isinstance(original, str) or not original:
        raise ValueError("缺少本次原文：没有位置判据时不接受任何改写，也不猜位置")
    return original


def _task_source_hashes(task: vc.TaskSpec) -> frozenset:
    return frozenset(item.sha256 for item in task.source_files)


def _validate_event(event, patches: tuple[vc.PatchRecord, ...]) -> tuple[dict, str]:
    if not isinstance(event, dict):
        raise ValueError(f"event 必须是映射，收到 {type(event).__name__}")
    extra = sorted(set(event) - set(EVENT_KEYS))
    if extra:
        raise ValueError(f"event 含未知字段: {','.join(extra)}")
    if event.get("source") != SOURCE_USER:
        raise ValueError("只有当前用户输入能构成接受：稿内文字与工具输出不是授权")
    request_id = event.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("event.request_id 必须是非空字符串（接受要能追溯到具体事件）")
    fields = event.get("fields") or {}
    unknown = sorted(set(fields) - set(EVENT_FIELDS))
    if unknown:
        raise ValueError(f"event.fields 含未知字段: {','.join(unknown)}")
    known = {item.patch_id for item in patches}
    chosen = {name: tuple(fields.get(name) or ()) for name in EVENT_FIELDS}
    for name, ids in chosen.items():
        stray = sorted(set(ids) - known)
        if stray:
            raise ValueError(f"event.fields.{name} 引用了不存在的 patch: {','.join(stray)}")
    overlap = (set(chosen["accepted_patch_ids"]) & set(chosen["rejected_patch_ids"])
               | set(chosen["accepted_patch_ids"]) & set(chosen["regenerate_patch_ids"])
               | set(chosen["rejected_patch_ids"]) & set(chosen["regenerate_patch_ids"]))
    if overlap:
        raise ValueError(f"同一条 patch 不可能既接受又拒绝/再生成: {','.join(sorted(overlap))}")
    return chosen, request_id


def position_warnings(patches: tuple[vc.PatchRecord, ...], *, original: str) -> dict:
    """把「不能定位/不唯一/已过期」的位置问题一次性摊开，供预览与接受共用。"""
    text = _require_original(original)
    warnings: dict[str, str] = {}
    for item in patches:
        old = item.old_text
        if text.count(old) > 1:
            warnings[item.patch_id] = (f"同形原文出现 {text.count(old)} 次")
            continue
        window = text[item.source_anchor.start:item.source_anchor.end]
        if window != old:
            warnings[item.patch_id] = ("登记的坐标与本次原文对不上"
                                       "（源文本已变化，不重新定位）")
    ordered = sorted((item.source_anchor.start, item.source_anchor.end, item.patch_id)
                     for item in patches)
    for (start_a, end_a, id_a), (start_b, _end_b, id_b) in zip(ordered, ordered[1:]):
        if start_b < end_a:
            for name, other in ((id_a, id_b), (id_b, id_a)):
                warnings.setdefault(name, f"与 {other} 的改写区间重叠")
    return warnings


def preview_decisions(patches: tuple[vc.PatchRecord, ...], event: dict, *,
                      task: vc.TaskSpec, original: str) -> dict:
    """每条 patch 的处置：accepted / rejected / regenerate / not_answered。"""
    chosen, _request_id = _validate_event(event, patches)
    warnings = position_warnings(patches, original=original)
    hashes = _task_source_hashes(task)
    decisions: dict[str, str] = {}
    for item in patches:
        if item.patch_id in warnings or item.source_sha256 not in hashes:
            decisions[item.patch_id] = "refused"
        elif item.patch_id in chosen["accepted_patch_ids"]:
            decisions[item.patch_id] = "accepted"
        elif item.patch_id in chosen["rejected_patch_ids"]:
            decisions[item.patch_id] = "rejected"
        elif item.patch_id in chosen["regenerate_patch_ids"]:
            decisions[item.patch_id] = "regenerate"
        else:
            decisions[item.patch_id] = "not_answered"
    return decisions


def accept_patches(patches: tuple[vc.PatchRecord, ...], event: dict, *,
                   task: vc.TaskSpec, original: str) -> tuple[vc.PatchRecord, ...]:
    """只把「用户明确接受、且位置与哈希都对得上」的 patch 标上 accepted_by。

    位置不唯一、区间重叠、源哈希过期这三类一律不进入接受结果——本层不猜位置，
    也不替用户「顺手」接受没点过的条目。
    """
    text = _require_original(original)
    chosen, request_id = _validate_event(event, patches)
    warnings = position_warnings(patches, original=text)
    hashes = _task_source_hashes(task)
    accepted: list[vc.PatchRecord] = []
    for item in patches:
        if item.patch_id not in chosen["accepted_patch_ids"]:
            continue
        if item.patch_id in warnings or item.source_sha256 not in hashes:
            continue
        accepted.append(dataclasses.replace(item, accepted_by=request_id))
    return tuple(accepted)


def render_preview(patches: tuple[vc.PatchRecord, ...], *, original: str,
                   known_warnings: dict | None = None) -> str:
    """人看的中文预览：位置、原文、候选、理由、风险、验证与接受状态。

    ``known_warnings`` 是调用方（宿主界面）已经掌握的位置告警；本层自己也会再算一遍，
    两边取并集——预览少一行提示，用户就会在错误的假设下点接受。
    """
    text = _require_original(original)
    warnings = position_warnings(patches, original=text)
    warnings.update(dict(known_warnings or {}))
    lines = ["按章预览（每条都要你明确接受、拒绝或再生成；未点过的不动）", ""]
    ordered = sorted(patches, key=lambda item: (item.source_anchor.segment_id,
                                                 item.source_anchor.start))
    for index, item in enumerate(ordered, 1):
        anchor = item.source_anchor
        validation = dict(item.validation or {})
        lines.append(f"第 {index} 处 · {anchor.segment_id} · 位置 {anchor.start}–{anchor.end}"
                     f" · {_RISK_LABEL.get(item.risk, item.risk)}"
                     f" · {_OPERATION_LABEL.get(item.operation, item.operation)}")
        lines.append(_diff_block(item, text))
        notes = tuple(validation.get("risk_notes") or ())
        lines.append("理由：" + ("；".join(notes) if notes else _NO_REASON))
        lines.append(f"验证：{'已通过' if validation.get('verified') is True else '未通过'}")
        lines.append(f"接受：{item.accepted_by or '待你确认'}")
        if item.patch_id in warnings:
            lines.append(f"位置告警：{warnings[item.patch_id]}——不猜位置，本条不接受")
        lines.append("")
    if not ordered:
        lines.append("本次没有可提交的修改（保留与证据不足都不产生 patch）。")
    return "\n".join(lines).rstrip() + "\n"


def _diff_block(item: vc.PatchRecord, original: str) -> str:
    window = original[item.source_anchor.start:item.source_anchor.end] or item.old_text
    diff = difflib.unified_diff(window.splitlines() or [window],
                                (item.new_text or "").splitlines() or [""],
                                lineterm="", n=0)
    body = "\n".join(f"  {line}" for line in diff)
    return f"原文：{window}\n候选：{item.new_text or '（删除，不留文字）'}\n逐字对照：\n{body}"



def _request_reading(request):
    """读 request 真正指向的文件，取现有权威给的两种摘要（字节 / 文本）。

    读不出来不是「跳过这道闸」，是拒绝并说清是哪一份材料取不到。
    返回 ``(snapshot, source_text, candidate_text, problem)``，problem 非空时其余为 None。
    """
    try:
        snapshot, source_text = text_source.read_source(request.source)
    except text_source.SourceInputError as exc:
        return None, None, None, f"request_source_unreadable[{exc.code}]"
    except OSError as exc:
        return None, None, None, f"request_source_unreadable:{type(exc).__name__}"
    try:
        candidate_raw = Path(request.candidate).read_bytes()
    except OSError as exc:
        return snapshot, source_text, None, f"request_candidate_unreadable:{type(exc).__name__}"
    try:
        candidate_text, _codec, _bom = text_source.decode_source_bytes(candidate_raw)
    except text_source.SourceInputError as exc:
        return snapshot, source_text, None, f"request_candidate_unreadable[{exc.code}]"
    return snapshot, source_text, candidate_text, None


def _binding_codes(request, task: vc.TaskSpec, patches, bundle,
                   channel: str) -> tuple:
    """实际请求与授权对象的同一性：路径、字节摘要、文本摘要、接受集、输出形态。

    刻意区分两种摘要（工单 C31）：磁盘字节摘要 ``snapshot.sha256`` 归 TaskSpec 订约
    哈希、``request.expected_source_sha256`` 与 ``patch.source_sha256`` 用；文本摘要
    ``novel_contracts.sha256_text(文本)`` 归 bundle 的 source/candidate 用。强行把两者
    当同一个值，带 BOM 或 CRLF 的真稿就会假红——那是自造尺子咬自己。
    """
    import v2_task            # 来源唯一身份的权威在那边，本层不重算一份

    snapshot, source_text, candidate_text, problem = _request_reading(request)
    if problem:
        return [problem.split(":")[0].split("[")[0]], [problem]
    codes: list[str] = []
    details: list[str] = []
    scope = {v2_task._canonical_source_id(ref.path): ref.sha256
             for ref in task.source_files}
    this_source = v2_task._canonical_source_id(request.source)
    contract_sha = scope.get(this_source)
    if contract_sha is None:
        # 不早退：其余能算的门照样算完，读数才完整（一次拒清楚，不挤牙膏）。
        details.append(f"request 指向的任务外来源：{this_source}")
        codes.append("request_source_out_of_scope")
    declared = str(request.expected_source_sha256 or "").lower()
    if contract_sha is None:
        pass
    elif snapshot.sha256 != contract_sha or declared != contract_sha:
        codes.append("request_source_hash_mismatch")
        details.append("源文件当前字节摘要与任务订约哈希或请求声明不一致："
                       f"盘上 {snapshot.sha256[:12]}… / 契约 {contract_sha[:12]}… "
                       f"/ 声明 {declared[:12] or '—'}…")
    if bundle.source_sha256 != snapshot.text_sha256:
        codes.append("bundle_source_not_request")
        details.append(f"bundle 的源文本摘要 {bundle.source_sha256[:12]}… 不是这份原稿的"
                       f"文本摘要 {snapshot.text_sha256[:12]}…")
    if bundle.candidate_sha256 != novel_contracts.sha256_text(candidate_text):
        codes.append("bundle_candidate_not_request")
        details.append(f"bundle 描述的候选 {bundle.candidate_sha256[:12]}… 与实际交出去的"
                       f"候选文本 {novel_contracts.sha256_text(candidate_text)[:12]}… 不是同一份")
    for item in patches:
        if not str(item.accepted_by or "").strip():
            codes.append("patch_not_accepted")
            details.append(f"{item.patch_id} 没有接受凭据（accepted_by 为空）")
        if contract_sha is not None and item.source_sha256 != contract_sha:
            codes.append("patch_source_not_request")
            details.append(f"{item.patch_id} 绑定的源哈希不属于这份请求的源文件")
        if item.old_text and item.old_text not in source_text:
            codes.append("patch_old_not_in_source")
            details.append(f"{item.patch_id} 的原文不在这次授权的源文本里")
        if item.new_text and item.new_text not in candidate_text:
            codes.append("patch_text_not_in_candidate")
            details.append(f"{item.patch_id} 的改动文字没出现在候选文本里")
    if channel not in novel_output.CHANNELS:
        codes.append("channel_not_authorized")
    if getattr(request, "strength", None) != task.strength:
        codes.append("strength_not_authorized")
        details.append(f"提交请求的力度 {getattr(request, 'strength', None)!r} "
                       f"与这次授权规格的力度 {task.strength!r} 不一致："
                       "改力度使旧确认与在途结果一并失效，需重新展示摘要确认")
    if task.output_policy.get("output") != "file":
        codes.append("output_policy_does_not_allow_file_write")
        details.append(f"输出形态 {task.output_policy.get('output')!r} 不允许往磁盘写正文")
    return sorted(set(codes)), details


def _gate_codes(request: novel_output.CommitRequest, task: vc.TaskSpec,
                patches: tuple[vc.PatchRecord, ...], bundle: vc.VerificationBundle,
                channel: str) -> tuple:
    """提交前置门的完整读数：先认「这份请求是不是那个被授权的对象」，再核原有各道门。"""
    binding_codes, details = _binding_codes(request, task, patches, bundle, channel)
    codes: list[str] = list(binding_codes)
    if not patches:
        codes.append("no_patch_to_commit")
    if task.choices.mode == "check" or not task.output_policy.get("write_body"):
        codes.append("mode_does_not_allow_body_write")
    identity = dict(task.runtime_identity or {})
    if identity.get("release_stage") != bundle.stage:
        codes.append("bundle_stage_mismatch")
    hashes = _task_source_hashes(task)
    for item in patches:
        if item.validation.get("verified") is not True:
            codes.append("patch_not_verified")
        if item.source_sha256 not in hashes:
            codes.append("patch_source_stale")
        if str(item.validation.get("bundle_candidate_sha256") or "") \
                != bundle.candidate_sha256:
            codes.append("bundle_candidate_mismatch")
        if item.risk == "high" and not item.accepted_by:
            codes.append("high_risk_without_acceptance")
    return sorted(set(codes)), details



def _member_job_covers(job_dir, source) -> bool:
    """成员 job 清单里那份原稿的规范路径，是否就是本次请求要动的源。

    路径由清单的 ``dir_rel`` + ``name`` 复原（现有恢复端同一套复原规则），
    再按来源唯一身份比较——「写到没登记过的目录上」在这种比较下过不去。
    """
    import novel_job
    from pathlib import PurePosixPath

    import v2_task

    record = novel_job.load_manifest(job_dir).get("source") or {}
    if not record.get("dir_rel") or not record.get("name"):
        return False
    derived = (Path(job_dir).resolve() / PurePosixPath(record["dir_rel"])).resolve()
    derived = derived / record["name"]
    return v2_task._canonical_source_id(derived) == v2_task._canonical_source_id(source)


def _require_ready_to_write(member, intent_id: str) -> None:
    """动手前的两道闸，都在记提交意图之前，所以「被拒的一轮」连意图都不该留。

    1. **状态**：成员 job 没走到可发布状态（或已 published 走幂等认领）就没有可交付的
       复核结论。以前这一步交给现有通路在收尾时报「跳步被拒」——那已经改过正文了，
       不是零触碰（独立复核那份反例正是停在 created 上把原稿换掉）。
    2. **上一轮没收尾**：这份 job 里还欠着一次已写盘未登记、或缺交付定位信息的意图时，
       不许在旁边再生成一份交付。发现不等于批准：这里只拒绝继续写正文，并把恢复入口
       点名给调用方。
    """
    import novel_job
    import v2_task                        # 未收尾的分类只有恢复面一份算法

    state = novel_job.load_state(member).get("state")
    unfinished = v2_task.unfinished_deliveries(member)
    if state == "published":
        settled = {str(item.get("intent_id") or "")
                   for item in novel_job.commit_records(member)}
        if (intent_id not in settled
                and not any(item["intent_id"] == intent_id for item in unfinished)):
            # 现有通路的幂等收尾按「任务记着的目标哈希」认领交付：同一轮重试成立；
            # 但换了一份正文再来，就会把上一次那份文件当成这次的交付——假归属。
            raise PreviewGateError(
                ("job_already_published",),
                f"成员 job {Path(member).name} 已按别的意图交付过：这次是另一份正文，"
                "不认领那份旧交付，也不写第二份；要出新件请开新一轮任务")
    elif state != novel_output.PUBLISHABLE_STATE:
        raise PreviewGateError(
            ("job_not_publishable",),
            f"成员 job {Path(member).name} 当前状态是 {state!r}，没走到 "
            f"{novel_output.PUBLISHABLE_STATE!r} 就没有可发布的复核结论："
            "拒绝动手，正文与目录一字节不改")
    for item in unfinished:
        if item["shape"] in v2_task.RECOVERABLE_SHAPES and item["intent_id"] != intent_id:
            raise PreviewGateError(
                ("delivery_unfinished",),
                f"成员 job {Path(member).name} 还欠着一轮没收尾的交付"
                f"（形状 {item['shape']}，意图 {str(item['intent_id'])[:12]}…）："
                "先用 v2_task.register_pending_commits 收尾，不许再生成一份交付")


def _second_review_gate(request, task, session) -> tuple:
    """提交前核复核事实的**出处**：旧裁判、旧规则/题集版本、旧纪元的复核放行不了这次交付。

    字节绑定（源稿/候选/账本摘要）由 ``novel_output.publish`` 的复验核 —— 它看不到
    TaskSpec 与 session；这一层看不到磁盘字节 —— 两层各核各的，既不重复也不留缝。
    没有复核事实时这里不判：缺证据该由账本类别那道门说，不在两处各说一遍。
    """
    import json
    from collections.abc import Mapping
    from pathlib import Path

    path = Path(str(getattr(request, "protection_manifest", "") or ""))
    if not path.is_file():
        return (), []                       # 证据不可读自有现有那道门报
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return (), []
    facts = [item for item in (doc.get("second_semantic_review") or ())
             if isinstance(item, Mapping)]
    if not facts:
        return (), []
    want = {"rule_version": str(task.rule_version),
            "prompt_version": str(task.prompt_version),
            "model_version": str(task.model_version),
            "judge_provider": str(dict(task.judge_ref or {}).get("provider") or "")}
    bad: list = []
    for item in facts:
        claim = str(item.get("claim_id") or "?")
        for field, value in want.items():
            if str(item.get(field) or "") != value:
                bad.append(f"{claim}:{field}")
        try:
            epoch_ok = int(item.get("judge_epoch")) == int(session.judge_epoch)
        except (TypeError, ValueError):
            epoch_ok = False
        if not epoch_ok:
            bad.append(f"{claim}:judge_epoch")
    if not bad:
        return (), []
    return ("second_review_provenance_mismatch",), [
        "复核事实的出处不属于这次任务或当前裁判纪元：" + "、".join(sorted(set(bad))[:8])
        + "；旧裁判、旧规则版本或旧纪元的复核不能放行这份文本"]


def commit_verified(request: novel_output.CommitRequest, *, task: vc.TaskSpec,
                   session: vc.SessionRecord,
                   patches: tuple[vc.PatchRecord, ...],
                   bundle: vc.VerificationBundle,
                   job_dir, channel: str, output_dir=None,
                   mixed_newline_authorization: str | None = None,
                   resume_identity=None, reasons: list | None = None
                   ) -> novel_output.PublishResult:
    """过完全部门之后交给现有那条通路；未过时抛错且零触碰；写成了就必须登记。

    ``job_dir`` 是**必需**的：提交登记落在真实成员 job 的事件层，没有 job 就没有可恢复
    的账本，也就没有「已提交」这件事。``channel`` 同样必需且原样透传给现有通路
    （本层不改写授权过的通道，也不默认永远另存）。

    门的顺序刻意是：会话授权 → 请求同一性与通道 → patch/验证/bundle 一致 →
    现有 ``publish``。会话没到 READY 时直接透传 ``NotReadyError``：那是「还没问完」，
    不是「被拒的提交」。发布动手之前先记一条 ``commit_intent``（要覆盖哪个文件、
    覆盖前的字节摘要、走哪条通道、哪份候选、哪个裁判），这样「写成了但登记没记上」
    的半成状态在重启后仍然可发现；写成之后按真实前后哈希做初次登记。
    """
    import novel_job
    import v2_task        # 来源唯一身份与成员 job 的归属判断在那边

    vs.require_ready(session, task)
    codes, details = _gate_codes(request, task, tuple(patches), bundle, channel)
    if codes:
        detail = "提交前置门未通过"
        if details:
            detail += "：" + "；".join(details)
        raise PreviewGateError(codes, detail)
    sr_codes, sr_details = _second_review_gate(request, task, session)
    if sr_codes:
        # 与上面同一口径：在记提交意图之前就拒，被拒的一轮连意图都不留
        raise PreviewGateError(sr_codes, "提交前置门未通过：" + "；".join(sr_details))
    job = Path(job_dir)
    snapshot, _source_text, candidate_text, problem = _request_reading(request)
    if problem:                       # 门前已经判过，这里只是取读数用
        raise PreviewGateError((problem.split(":")[0].split("[")[0],), problem)
    # job_dir 就是这份源的成员 job：登记落在它自己的事件层，交付前还要证明它俩相配。
    member = job
    if not _member_job_covers(member, request.source):
        raise PreviewGateError(("job_source_mismatch",),
                               f"job {job.name} 登记的原稿不是这份请求指向的源")
    sink: list = reasons if reasons is not None else []
    identity = {
        "file": str(Path(request.source).resolve()),
        "before_sha256": snapshot.sha256, "channel": channel, "task_id": task.task_id,
        "candidate_sha256": bundle.candidate_sha256, "stage": bundle.stage,
        "judge_epoch": session.judge_epoch,
        "output_dir": str(Path(output_dir).resolve()) if output_dir else ""}
    intent_id = novel_job.intent_identity(identity)
    _require_ready_to_write(member, intent_id)
    novel_job.record_commit_intent(member, dict(
        identity, judge_provider=str(task.judge_ref.get("provider")),
        judge_model=str(task.judge_ref.get("model")),
        intent_schema=novel_job.COMMIT_INTENT_SCHEMA, intent_id=intent_id))
    result = novel_output.publish(
        request, channel=channel, output_dir=output_dir, job_dir=member,
        resume_identity=resume_identity,
        mixed_newline_authorization=mixed_newline_authorization, reasons=sink)
    if result.status != "PASS":
        return result                 # 现有通路的判定与理由原样交回，不在此改写结论
    # 现有通路在「稿已写成但 job 终态没落进」时仍返回 PASS，并把原因放进 reasons。
    # 对本层来说那不算完成：交付事实已经落进事件层，恢复入口按它收尾并登记，
    # 但这一次调用绝不返回「已提交」。
    state = novel_job.load_state(member)
    if state.get("state") != "published":
        raise CommitRegistrationFailed(
            {"file": str(result.path), "path": str(Path(request.source).resolve()),
             "status": result.status, "sha256": result.sha256,
             "before_sha256": snapshot.sha256, "job_dir": member,
             "channel": channel, "intent_id": intent_id, "reasons": list(sink)},
            f"任务终态停在 {state.get('state')!r}，发布收尾没落进 job")
    if any(str(item.get("intent_id") or "") == intent_id
           for item in novel_job.commit_records(member)):
        # 同一轮意图已经记过事实：幂等重试（现有通路按目标哈希认领那次交付）
        # 不再追加第二条，否则会把真实备份引用换成「另存无前态」的声明值，溯源断掉。
        return result
    record = {
        "file": str(result.path), "before_sha256": snapshot.sha256,
        "after_sha256": result.sha256,
        "backup_ref": str(result.backup_path) if result.backup_path is not None
        else CREATED_BACKUP_REF,
        "stage": bundle.stage, "status": "committed", "judge_epoch": session.judge_epoch,
        "judge_provider": str(task.judge_ref.get("provider")),
        "judge_model": str(task.judge_ref.get("model")), "intent_id": intent_id}
    try:
        novel_job.record_commit(member, record)
    except Exception as exc:                       # 登记失败绝不能被吞成成功
        raise CommitRegistrationFailed(
            {"file": record["file"], "path": str(Path(request.source).resolve()),
             "status": result.status, "sha256": result.sha256,
             "before_sha256": record["before_sha256"], "job_dir": member,
             "backup_ref": record["backup_ref"], "channel": channel,
             "intent_id": intent_id},
            f"{type(exc).__name__}: {exc}") from exc
    return result
