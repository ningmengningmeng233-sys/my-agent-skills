#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_judge.py - 信息与叙事功能的分项判定合同（v2.0.0 alpha.2 P08）。

分工（设计方案 5/7/8 节）：**本模块不判文学质量**。它只规定裁判要问哪五个分项、
分项答案怎么组合成三值结论，以及结论到操作与发布权的映射。真实判定由宿主模型或
Jev 给出，规则层拿不到语义发布权。

五条分项（分开问，再组合）：

  1. ``information_increment`` 是否有独立动作、结果、线索、风险、空间信息、决定、
     状态或有价值感官信息；
  2. ``narrative_function`` 是否承担声音、关系、节奏、视角、伏笔或承接功能；
  3. ``repetition_with_function`` 是否重复已有内容，重复是否刻意且有功能；
  4. ``deletion_or_merge_risk`` 删除或合并是否损失否定、条件、主体、确定程度和时间范围；
  5. ``real_improvement`` 候选修改是否改善目标问题，而非只换同义词。

组合政策：

  - **KEEP 与证据不足分开**：缺分项、缺上下文、只换同义词都是
    ``insufficient_evidence``，绝不折算成「质量达标」。
  - **判定先于力度**：结论只由分项事实决定，``review_depth`` 之类的力度标注不参与判定，
    操作顺序也不随力度改变（与 FP 三档稳定合同一致）。
  - **概率不相乘**：组合只看分项结论，置信度另存；需要代表值时取最弱一环，
    不把多个 0.9 乘成 0.59 或吹成 0.99。
  - **未校准组合不开放高风险自动提交**；纯规则裁判不获得 L2/L3 语义发布权。
"""
from __future__ import annotations

from collections.abc import Mapping

import v2_contracts as vc

QUESTION_VERSION = "v2-judge-q1"
Q_INFORMATION = "information_increment"
Q_FUNCTION = "narrative_function"
Q_REDUNDANCY = "repetition_with_function"
Q_DELETION_RISK = "deletion_or_merge_risk"
Q_IMPROVEMENT = "real_improvement"
QUESTION_ORDER: tuple[str, ...] = (Q_INFORMATION, Q_FUNCTION, Q_REDUNDANCY,
                                   Q_DELETION_RISK, Q_IMPROVEMENT)
#: 中文题名：只用于「哪一项没答」这类申报，不参与判级。
QUESTION_TITLES: dict[str, str] = {
    Q_INFORMATION: "信息增量",
    Q_FUNCTION: "叙事功能",
    Q_REDUNDANCY: "重复及其功能性",
    Q_DELETION_RISK: "删除/合并风险",
    Q_IMPROVEMENT: "修改是否真改善",
}
ANSWER_VALUES: tuple[str, ...] = ("yes", "no", "unknown")

#: 纯规则/无裁判的名字集合：只检查机械合同，不产语义判定，也不给发布权。
RULE_ONLY_PROVIDERS: tuple[str, ...] = ("rule", "rule_only", "none", "mechanical")

#: finding 码：接入现有 novel_review 的模型 finding 枚举，不另起第二套校验器。
FINDING_CODE = "v2-segmented-judgment"

#: 操作顺序（设计方案 5 节）：保留永远在第一位，不得按排序机械地优先删除。
OPERATION_SEQUENCE: tuple[str, ...] = ("保留", "无损删除", "无损合并", "局部修复",
                                       "必要段内重组")

_ASKS: dict[str, str] = {
    Q_INFORMATION: "本候选是否给出前文没有的独立动作、结果、线索、风险、空间信息、"
                   "决定、状态或有价值的感官信息？",
    Q_FUNCTION: "本候选是否承担人物声音、关系、节奏、视角、伏笔或承接功能？",
    Q_REDUNDANCY: "本候选是否重复已经写过的内容？若重复，是否刻意且有功能？",
    Q_DELETION_RISK: "删除或合并本候选是否会丢失否定、条件、主体、确定程度或时间范围？",
    Q_IMPROVEMENT: "候选修改是否真的改善目标问题，而不是只换一组同义词？",
}

_MUST_CHECK: dict[str, tuple[str, ...]] = {
    Q_INFORMATION: ("动作", "结果", "线索", "风险", "空间信息", "决定", "状态", "感官信息"),
    Q_FUNCTION: ("声音", "关系", "节奏", "视角", "伏笔", "承接"),
    Q_REDUNDANCY: ("是否已写过", "是否刻意", "是否有功能"),
    Q_DELETION_RISK: ("否定", "条件", "主体", "确定程度", "时间范围"),
    Q_IMPROVEMENT: ("目标问题是否缓解", "是否只换同义词", "是否新增事实"),
}

# 类别侧的额外必查面：低价值比较只看「有没有动作结果」会误删，风险与线索本身
# 就是信息（设计方案 5 节对第 2 类问题的明确要求）。
_KIND_EXTRA_MUST_CHECK: dict[str, dict[str, tuple[str, ...]]] = {
    "LOW_INFORMATION_COMPARISON": {
        Q_INFORMATION: ("比较对象是否引入新风险", "比较是否给出新线索")},
    "RESULT_ECHO": {Q_REDUNDANCY: ("结果是否已被前文承担",)},
    "TEMPLATE_REUSE": {Q_FUNCTION: ("是否承担节奏或人物口癖",)},
    "SEMANTIC_ECHO": {Q_REDUNDANCY: ("两处是否同一信息而非呼应",)},
}


def _entry_of(answers: Mapping, question_id: str) -> dict:
    """分项答案统一成映射形状；裸字符串是 {"value": ...} 的简写。"""
    entry = answers.get(question_id)
    if entry is None:
        return {"value": "unknown"}
    if isinstance(entry, str):
        return {"value": entry}
    if isinstance(entry, Mapping):
        return dict(entry)
    raise ValueError(f"分项 {question_id} 的答案形状非法: {entry!r}")


def _value_of(answers: Mapping, question_id: str) -> str:
    value = _entry_of(answers, question_id).get("value", "unknown")
    value = str(value)
    if value not in ANSWER_VALUES:
        raise ValueError(f"分项 {question_id} 的答案只能是 {ANSWER_VALUES}，收到 {value!r}")
    return value


def _extra_of(answers: Mapping, question_id: str, name: str):
    return _entry_of(answers, question_id).get(name)


def build_questions(candidate: vc.CandidateRecord, *, source_window: str,
                    ledger: tuple) -> dict:
    """按候选类别生成五个分项问题：每题带提问、必查面、锚点与需要核对的命题。

    ``source_window`` 必须是真实读到的原文窗口——没有窗口就不出题，不凭候选摘录猜上下文；
    ``ledger`` 是现有账本元组，用于把命题钉回可核对的来源。
    """
    if not isinstance(candidate, vc.CandidateRecord):
        raise ValueError(f"candidate 必须是 CandidateRecord，收到 {type(candidate).__name__}")
    if not isinstance(source_window, str) or not source_window.strip():
        raise ValueError("缺少原文窗口，不得凭摘录出题（证据不足由裁判定，不由出题猜）")
    if not isinstance(ledger, tuple):
        raise ValueError(f"ledger 必须是账本元组，收到 {type(ledger).__name__}")
    extra = _KIND_EXTRA_MUST_CHECK.get(candidate.kind, {})
    questions = []
    for question_id in QUESTION_ORDER:
        questions.append({
            "id": question_id,
            "ask": _ASKS[question_id],
            "must_check": tuple(_MUST_CHECK[question_id]) + tuple(extra.get(question_id, ())),
            "candidate_id": candidate.candidate_id,
            "anchor": candidate.source_anchor,
            "propositions": tuple(
                f"{candidate.rule_id}：{check}" for check in
                tuple(_MUST_CHECK[question_id]) + tuple(extra.get(question_id, ()))),
        })
    return {"question_version": QUESTION_VERSION,
            "candidate_id": candidate.candidate_id,
            "questions": tuple(questions),
            "ledger_refs": tuple(getattr(item, "entry_id", str(item)) for item in ledger)}


def weakest_confidence(answers: Mapping) -> float | None:
    """代表置信度取最弱一环；相乘会把 5 个 0.9 说成 0.59，也可能反过来吹高。"""
    values = []
    for question_id in QUESTION_ORDER:
        entry = answers.get(question_id)
        if not isinstance(entry, Mapping):
            continue
        for field in ("confidence", "probability"):
            value = entry.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append(float(value))
    if not values:
        return None
    return min(values)


def combine_judgment(candidate: vc.CandidateRecord, answers: dict, *,
                     provider: str, actual_model: str, judge_epoch: int,
                     calibration: dict,
                     extra_claims: tuple[str, ...] = ()) -> vc.JudgmentRecord:
    """把五个分项答案组合成三值结论，判定只由分项事实决定。

    ``extra_claims`` 承载**裁判层自己发现的形状问题**（分项没回答、返回选项与
    概率冲突、宿主没给答案）：这些事实必须进「为什么证据不足」的理由，
    不能被「分项未给出结论」这类通用文案吞掉。它不参与判级，只补申报。
    """
    if not isinstance(extra_claims, tuple) or not all(
            isinstance(item, str) for item in extra_claims):
        raise ValueError(f"extra_claims 必须是字符串元组，收到 {extra_claims!r}")
    extra_claims_clean = tuple(claim for claim in extra_claims if claim.strip())
    if not isinstance(candidate, vc.CandidateRecord):
        raise ValueError(f"candidate 必须是 CandidateRecord，收到 {type(candidate).__name__}")
    if not isinstance(answers, Mapping):
        raise ValueError(f"answers 必须是映射，收到 {type(answers).__name__}")
    for key, value in answers.items():
        if key in QUESTION_ORDER:
            continue
        # 力度之类的标注允许随附，但不得冒充分项；形状像分项却不在册 → 拒，不静默吞掉。
        if isinstance(value, Mapping) and "value" in value:
            raise ValueError(f"未知分项 {key!r}（在册分项：{QUESTION_ORDER}）")
    for name, value in (("provider", provider), ("actual_model", actual_model)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须是非空字符串，收到 {value!r}")
    if not isinstance(calibration, dict):
        raise ValueError(f"calibration 必须是映射，收到 {type(calibration).__name__}")

    atomic = {question_id: _entry_of(answers, question_id)
              for question_id in QUESTION_ORDER}
    anchors = (candidate.source_anchor,) + tuple(candidate.context_anchors)

    if provider.strip().lower() in RULE_ONLY_PROVIDERS:
        return vc.JudgmentRecord(
            candidate_id=candidate.candidate_id, provider=provider,
            actual_model=actual_model, judge_epoch=judge_epoch,
            question_version=QUESTION_VERSION, atomic_answers=atomic,
            status="insufficient_evidence", evidence_anchors=anchors,
            reason_claims=("纯规则层只核机械合同，不产语义判定",))

    values = {qid: _value_of(answers, qid) for qid in QUESTION_ORDER}

    # 保留证据只看「明确答出来的支持项」；缺项不参与保留判断，也不被当成否定。
    keep_claims: list[str] = []
    if values[Q_FUNCTION] == "yes":
        kind = _extra_of(answers, Q_FUNCTION, "function")
        keep_claims.append(f"承担叙事功能（{kind}）" if kind else "承担叙事功能")
    if values[Q_INFORMATION] == "yes":
        keep_claims.append("给出前文没有的独立信息")
    if values[Q_DELETION_RISK] == "yes":
        keep_claims.append("删除或合并会丢失否定、条件、主体、确定程度或时间范围")
    if (values[Q_REDUNDANCY] == "yes"
            and _extra_of(answers, Q_REDUNDANCY, "intentional") is True
            and _extra_of(answers, Q_REDUNDANCY, "functional") is True):
        keep_claims.append("重复是刻意且有功能的（节奏、回声或人物口癖）")
    if keep_claims:
        return vc.JudgmentRecord(
            candidate_id=candidate.candidate_id, provider=provider,
            actual_model=actual_model, judge_epoch=judge_epoch,
            question_version=QUESTION_VERSION, atomic_answers=atomic,
            status="keep", evidence_anchors=anchors,
            reason_claims=tuple(keep_claims))

    # 没有保留证据时，先确认五项都真的答过：unknown 或缺键就是证据不足，
    # 绝不能被读成「没有信息增量」「没有叙事功能」再推出可改。
    unanswered = [QUESTION_TITLES[qid] for qid in QUESTION_ORDER if values[qid] == "unknown"]
    if unanswered:
        return vc.JudgmentRecord(
            candidate_id=candidate.candidate_id, provider=provider,
            actual_model=actual_model, judge_epoch=judge_epoch,
            question_version=QUESTION_VERSION, atomic_answers=atomic,
            status="insufficient_evidence", evidence_anchors=anchors,
            reason_claims=("分项未给出结论，不能当作否定：" + "、".join(unanswered),)
                          + extra_claims_clean)

    # 五项齐备且都不支持保留：还要看改善是不是只是换词、重复是否成立。
    if values[Q_REDUNDANCY] == "no":
        block = "未确认存在重复，证据不足"
    elif values[Q_IMPROVEMENT] == "no":
        block = "候选修改未改善目标问题，证据不足"
    elif _extra_of(answers, Q_IMPROVEMENT, "synonym_only") is True:
        block = "候选修改只是换同义词，证据不足"
    else:
        block = ""
    if block:
        return vc.JudgmentRecord(
            candidate_id=candidate.candidate_id, provider=provider,
            actual_model=actual_model, judge_epoch=judge_epoch,
            question_version=QUESTION_VERSION, atomic_answers=atomic,
            status="insufficient_evidence", evidence_anchors=anchors,
            reason_claims=(block,) + extra_claims_clean)

    # 走到这里：重复成立且非刻意功能性、无独立信息、无叙事功能、删除不损语义、
    # 修改有真实改善——五项都是明确答出来的事实，理由只复述已答项。
    return vc.JudgmentRecord(
        candidate_id=candidate.candidate_id, provider=provider,
        actual_model=actual_model, judge_epoch=judge_epoch,
        question_version=QUESTION_VERSION, atomic_answers=atomic,
        status="rewrite", evidence_anchors=anchors,
        reason_claims=("重复成立且非刻意、非功能性；信息增量与叙事功能均明确为无；"
                       "删除或合并不丢否定、条件、主体、确定程度与时间范围；"
                       "候选修改改善目标问题而非只换同义词",))


def operation_order_for(judgment: vc.JudgmentRecord) -> tuple[str, ...]:
    """操作顺序由判定结论决定，与力度无关；保留始终是一等可选结果。"""
    if judgment.status != "rewrite":
        return ("保留",)
    return OPERATION_SEQUENCE


def publish_rights(judgment: vc.JudgmentRecord) -> dict:
    """L2/L3 语义发布权只给真实裁判；纯规则即使给出结论也不算复核已过。"""
    granted = judgment.provider.strip().lower() not in RULE_ONLY_PROVIDERS
    return {"l2": granted, "l3": granted}


#: 校准记录 schema 版本；旧格式（只有 calibrated+rules 门槛）一律视为无校准证据。
CALIBRATION_SCHEMA_VERSION = 2
#: 高风险自动提交必须逐项吻合的组合身份；任一项对不上就关闭。
CALIBRATION_IDENTITY_FIELDS: tuple[str, ...] = ("rule_id", "provider", "actual_model",
                                                "question_version", "judge_epoch")


def high_risk_auto_commit_allowed(candidate: vc.CandidateRecord,
                                  judgment: vc.JudgmentRecord,
                                  calibration: dict) -> bool:
    """高风险自动提交只放行给「本组合已被明确校准」的那一次判定。

    需要同时满足：判定结论是 rewrite；判定确实属于这个候选（candidate_id 与 rule_id
    都对得上）；校准记录是新版 schema，并且存在一条与 rule_id/provider/actual_model/
    question_version/judge_epoch 五项全等、且带可核对证据标识的记录。
    本函数只是**必要条件之一**，不代替用户的预览与高风险操作授权。
    """
    if judgment.status != "rewrite":
        return False
    if judgment.candidate_id != candidate.candidate_id:
        return False
    if not isinstance(calibration, dict):
        return False
    if calibration.get("schema_version") != CALIBRATION_SCHEMA_VERSION:
        return False
    combinations = calibration.get("combinations")
    if not isinstance(combinations, (list, tuple)) or not combinations:
        return False
    wanted = {"rule_id": candidate.rule_id, "provider": judgment.provider,
              "actual_model": judgment.actual_model,
              "question_version": judgment.question_version,
              "judge_epoch": judgment.judge_epoch}
    for entry in combinations:
        if not isinstance(entry, Mapping):
            continue
        if any(entry.get(field) != value for field, value in wanted.items()):
            continue
        if not str(entry.get("evidence", "")).strip():
            continue
        return True
    return False


def finding_fields(judgment: vc.JudgmentRecord) -> dict:
    """映射进现有 novel_review.ReviewFinding 字段，不新建第二份 finding 合同。"""
    severity = "review" if judgment.status != "rewrite" else "fail"
    explanation = "；".join(judgment.reason_claims)[:197]
    return {
        "finding_id": f"judg::{judgment.candidate_id}::{judgment.judge_epoch}",
        "level": "L2",
        "severity": severity,
        "code": FINDING_CODE,
        "anchors": tuple(judgment.evidence_anchors),
        "explanation": explanation,
    }
