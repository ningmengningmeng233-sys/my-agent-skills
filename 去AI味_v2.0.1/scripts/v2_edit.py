#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v2_edit.py - 受约束的候选生成接线（v2.0.0 alpha.3 P10）。

**本模块不产生文本，也不建第二套引擎**：正文由宿主模型经注入的生成器给出，
本层只负责三件事——

  1. **要不要问**：只检查模式（``check``）、判定 ``keep`` 或证据不足，一次都不调用
     生成器。「没有可改」是合法结果，不存在必须产出 diff 的配额；最强档
     （``scan=thorough``）只扩大检查覆盖，不扩大改写权限。
  2. **问什么**：把候选、原文窗口、分项判定与「不得做」清单交给宿主；
     不把整本书塞进请求，也不把力度当授权。
  3. **收回来先过闸**：位置/哈希过期、判定属于别的候选、只换同义词、
     补写原文没有的内容、只把标点与停顿「磨平」——都直接丢弃，不猜位置、
     不改写判定结论。真实的事实核验与双向保真在 P11，本层的闸只挡明显越界。

去味（clean）与润色（polish）是两个独立阶段：``propose_clean`` 绝不调用润色通路；
``propose_polish`` 只在模式允许润色时进行，且看的是**已产出的去味稿**，不是原稿。
段内重组（换序、搬句）标 ``high`` 风险，交给 P12 的逐项预览与用户明确接受。
"""
from __future__ import annotations

import difflib
import hashlib
from collections.abc import Mapping
from typing import Protocol

import v2_contracts as vc
import v2_judge as vj

#: 会进入改写阶段的处理方式；``check`` 不在其中。
EDIT_MODES: tuple[str, ...] = ("clean", "clean_polish", "polish_only")
POLISH_MODES: tuple[str, ...] = ("clean_polish", "polish_only")

#: 交给宿主的禁止清单（用户要求「克制」「作者声音」）。这是约束声明，
#: 不是生效保证——生成器不遵守时由本层的丢弃闸兜住。
MUST_NOT_DO: tuple[str, ...] = ("新增事实", "统一纠正方言与停顿", "只换同义词",
                                 "改写人物声音与视角")

#: 不可逆或结构性改动一律高风险，必须逐项预览（P12）。
HIGH_RISK_OPERATIONS: tuple[str, ...] = ("delete", "merge", "paragraph_rewrite")

#: 中英文标点与空白：判断「只磨平标点」与「新增内容」时先剥掉它们。
_PUNCT = ("，。、；：？！…—－～·‧“”‘’\"'（）()《》〈〉【】〔〕「」『』,.:;"
          "?!"
          "-~ \t\r\n")
_SENTENCE_END = "。！？!?；;\n"


class EditPolicyError(ValueError):
    """策略拒绝：调用方式本身不合法（模式不允许、缺生成器、身份对不上）。"""


def _authorize_generation(task: vc.TaskSpec, session, *, stage: str) -> None:
    """生成器被碰之前的授权门：没有 READY 会话记录，一次生成都不许发生。

    session 缺省即「没带授权记录」——直接拒。门本身在 v2_session.authorize_body_output，
    与入口答复 body_output_allowed 同一份读数；这里不判断「看起来像不像已确认」。
    """
    import v2_session as vs

    if session is None:
        raise EditPolicyError(
            f"{stage} 阶段缺会话授权记录：摘要未展示或未确认时不得调用宿主生成器")
    vs.authorize_body_output(session, task, surface=task.output_policy.get("output"))


class SuggestionGenerator(Protocol):
    """宿主生成器的最小形状：两个独立通路，各自返回建议文本或 ``None``。

    ``None``／原样退回都表示「这一段没有可改」，不是错误。
    """

    def generate_clean(self, request: Mapping) -> str | None:
        ...

    def generate_polish(self, request: Mapping) -> str | None:
        ...


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bare(text: str) -> str:
    """剥掉标点与空白后的正文，用于「只换标点」与新增片段的判断。"""
    return "".join(ch for ch in text if ch not in _PUNCT)


def _sentences(text: str) -> tuple[str, ...]:
    chunks, current = [], ""
    for ch in text:
        current += ch
        if ch in _SENTENCE_END:
            stripped = _bare(current)
            if stripped:
                chunks.append(stripped)
            current = ""
    tail = _bare(current)
    if tail:
        chunks.append(tail)
    return tuple(chunks)


def _adds_content(before: str, after: str, source: str) -> bool:
    """after 里出现 before 与本次原文都没有的片段：判为补写，整条丢弃。

    逐片段核对而不是比长度：把两句并成一句会删字，删字不是补写。
    """
    matcher = difflib.SequenceMatcher(a=_bare(before), b=_bare(after), autojunk=False)
    source_bare = _bare(source)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            piece = _bare(after)[j1:j2]
            if piece and piece not in source_bare:
                return True
    return False


def _is_punctuation_flattening(before: str, after: str) -> bool:
    """正文一字未动、只有标点与空白变了：这是磨平作者声音，不是去味。"""
    return _bare(before) == _bare(after) and before != after


def _is_rearrangement(before: str, after: str) -> bool:
    """同一批句子换顺序：属段内重组，必须标高风险。"""
    if _bare(before) == _bare(after):
        return False
    return _sentences(before) != _sentences(after) and sorted(
        _sentences(before)) == sorted(_sentences(after))


def _check_identity(candidate: vc.CandidateRecord, judgment: vc.JudgmentRecord,
                    original: str) -> None:
    """身份与位置闸：判定必须属于这个候选，锚点必须还能在原文里逐字读到。"""
    if judgment.candidate_id != candidate.candidate_id:
        raise EditPolicyError(
            f"判定属于候选 {judgment.candidate_id!r}，与要改写的候选 "
            f"{candidate.candidate_id!r} 不一致：不接受张冠李戴的授权")
    anchor = candidate.source_anchor
    if not isinstance(original, str) or not original:
        raise EditPolicyError("缺少原文，无法核对候选位置")
    window = original[anchor.start:anchor.end]
    if window != anchor.excerpt:
        raise ValueError(
            f"候选锚点与原文不符（位置 {anchor.start}:{anchor.end} 读到 "
            f"{len(window)} 字）：源文本已变化，不猜位置")
    if _sha(anchor.excerpt) != anchor.text_sha256:
        raise ValueError("候选摘录与登记哈希不符：该候选已失效")
    if _sha(original) != candidate.source_hash:
        raise ValueError("原文哈希与候选登记的 source_hash 不一致：候选已过期")


def _base_request(candidate: vc.CandidateRecord, *, task: vc.TaskSpec,
                  window: str, stage: str) -> dict:
    """给宿主的受约束请求：只带本次需要的片段与边界，不塞整本书。"""
    return {
        "stage": stage,
        "candidate_id": candidate.candidate_id,
        "rule_id": candidate.rule_id,
        "kind": candidate.kind,
        "old_text": candidate.source_anchor.excerpt,
        "source_window": window,
        "allowed_operations": tuple(vj.OPERATION_SEQUENCE),
        "must_not_do": MUST_NOT_DO,
        "question_version": vj.QUESTION_VERSION,
        "judge_epoch": task.judge_epoch,
        "reason_claims": (),
    }


def _patch(*, candidate_id: str, anchor, old_text: str, new_text: str,
            source_sha256: str, context_hash: str, operation: str,
            notes: tuple[str, ...]) -> vc.PatchRecord:
    risk = "high" if operation in HIGH_RISK_OPERATIONS else "low"
    return vc.PatchRecord(
        patch_id=f"patch::{candidate_id}::{_sha(old_text + '|' + new_text)[:12]}",
        candidate_id=candidate_id, source_anchor=anchor, old_text=old_text,
        new_text=new_text, context_hash=context_hash, source_sha256=source_sha256,
        risk=risk, operation=operation,
        validation={"stage": "proposed", "verified": False, "risk_notes": notes})


def propose_clean(candidate: vc.CandidateRecord, judgment: vc.JudgmentRecord, *,
                  original: str, task: vc.TaskSpec,
                  generator: SuggestionGenerator,
                  session=None) -> tuple[vc.PatchRecord, ...]:
    """只检查、KEEP 与证据不足都不生成；判定为 rewrite 才向宿主取一份建议文本。"""
    if not isinstance(candidate, vc.CandidateRecord):
        raise EditPolicyError("candidate 必须是 CandidateRecord")
    if not isinstance(judgment, vc.JudgmentRecord):
        raise EditPolicyError("judgment 必须是 JudgmentRecord")
    if generator is None:
        raise TypeError("propose_clean 需要宿主注入生成器，本层不自建文本引擎")
    if task.choices.mode not in EDIT_MODES:
        return ()
    if judgment.status != "rewrite":
        return ()
    _check_identity(candidate, judgment, original)
    if _improvement_is_synonym_only(judgment):
        # 判定已说明「只换同义词」：再问一次宿主只是白花开销与授权面。
        return ()

    anchor = candidate.source_anchor
    request = _base_request(candidate, task=task, window=original, stage="clean")
    request["reason_claims"] = tuple(judgment.reason_claims)
    _authorize_generation(task, session, stage="clean")
    suggested = generator.generate_clean(request)
    return _patches_from_suggestion(
        suggested, candidate_id=candidate.candidate_id, anchor=anchor,
        old_text=anchor.excerpt, original=original, source_sha256=candidate.source_hash,
        context_hash=candidate.context_hash)


def _improvement_is_synonym_only(judgment: vc.JudgmentRecord) -> bool:
    entry = judgment.atomic_answers.get(vj.Q_IMPROVEMENT)
    return isinstance(entry, Mapping) and entry.get("synonym_only") is True


def _patches_from_suggestion(suggested, *, candidate_id: str, anchor, old_text: str,
                             original: str, source_sha256: str,
                             context_hash: str) -> tuple[vc.PatchRecord, ...]:
    """把宿主建议收成 patch；四类越界形状直接丢弃。"""
    if suggested is None:
        return ()
    if not isinstance(suggested, str):
        raise EditPolicyError(f"宿主返回的类型不是字符串: {type(suggested).__name__}")
    if suggested == old_text:
        return ()
    if not suggested.strip():
        return (_patch(candidate_id=candidate_id, anchor=anchor, old_text=old_text,
                       new_text="", source_sha256=source_sha256,
                       context_hash=context_hash, operation="delete",
                       notes=("整段建议为空：按无损删除处理，交 P11 保真与 P12 预览",)),)
    if _is_punctuation_flattening(old_text, suggested):
        return ()
    if _adds_content(old_text, suggested, original):
        return ()
    if _is_rearrangement(old_text, suggested):
        return (_patch(candidate_id=candidate_id, anchor=anchor, old_text=old_text,
                       new_text=suggested, source_sha256=source_sha256,
                       context_hash=context_hash, operation="paragraph_rewrite",
                       notes=("句子换位：属段内重组，标高风险并逐项预览",)),)
    return (_patch(candidate_id=candidate_id, anchor=anchor, old_text=old_text,
                   new_text=suggested, source_sha256=source_sha256,
                   context_hash=context_hash, operation="local_rewrite",
                   notes=()),)


def propose_polish(clean_text: str, *, original: str, task: vc.TaskSpec,
                   generator: SuggestionGenerator,
                   candidate_id_prefix: str = "polish",
                   session=None) -> tuple[vc.PatchRecord, ...]:
    """润色是独立阶段：逐段问一次宿主，看的是去味稿，边界仍是本次原文。"""
    if generator is None:
        raise TypeError("propose_polish 需要宿主注入生成器，本层不自建文本引擎")
    if task.choices.mode not in POLISH_MODES:
        raise EditPolicyError(
            f"处理方式 {task.choices.mode!r} 未授权润色：去味阶段不调用润色通路")
    if not isinstance(clean_text, str) or not clean_text.strip():
        raise EditPolicyError("缺少已产出的去味稿，无法独立润色")

    _authorize_generation(task, session, stage="polish")
    source_sha256 = _sha(original)
    context_hash = _sha(clean_text)
    paragraphs = clean_text.split("\n\n")
    patches: list[vc.PatchRecord] = []
    offset = 0
    for index, paragraph in enumerate(paragraphs):
        anchor = vc.SourceAnchor(segment_id=f"polish-{index + 1}", start=offset,
                                 end=offset + len(paragraph), excerpt=paragraph,
                                 text_sha256=_sha(paragraph), occurrence_index=index)
        offset += len(paragraph) + len("\n\n")
        request = {
            "stage": "polish",
            "index": index,
            "text": paragraph,
            "source_of_truth": clean_text,
            "boundary_source": original,
            "must_not_do": MUST_NOT_DO,
            "question_version": vj.QUESTION_VERSION,
            "judge_epoch": task.judge_epoch,
        }
        suggested = generator.generate_polish(request)
        if suggested is None or suggested == paragraph:
            continue
        if not isinstance(suggested, str):
            raise EditPolicyError("宿主润色建议必须是字符串或 None")
        if _is_punctuation_flattening(paragraph, suggested):
            continue
        if _adds_content(paragraph, suggested, clean_text) or \
                _adds_content(paragraph, suggested, original):
            continue
        operation = ("paragraph_rewrite" if _is_rearrangement(paragraph, suggested)
                     else "local_rewrite")
        notes = (("句子换位：属段内重组，标高风险并逐项预览",)
                 if operation == "paragraph_rewrite" else ())
        patches.append(_patch(candidate_id=f"{candidate_id_prefix}::{index + 1}",
                              anchor=anchor, old_text=paragraph, new_text=suggested,
                              source_sha256=source_sha256, context_hash=context_hash,
                              operation=operation, notes=notes))
    return tuple(patches)
