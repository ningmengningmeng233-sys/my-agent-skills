#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v2_verify.py - 把三段文本接进现有 L0–L3 验证与一次有界再检查（alpha.3 P11）。

**本模块不做任何判定，也不新建校验器**：全部结论来自仓库里已有的那几套——

  - 双向保真：``novel_validation.run_validation``（原稿→成稿、成稿→原稿各跑一次，
    只测单向会漏掉「成稿新增事实」这一整类）；
  - 真实引文逐字锁定：``novel_contracts.validate_protections``；
  - 报告与语义层级：``novel_review.validate_review``（缺 L2/L3 挂
    ``review_level_missing``，全书窗口必须有 ``reread_window_texts`` 的产物）；
  - 发布判定：``novel_review.publication_decision``；
  - 账本摘要：``novel_review.ledger_digest``（唯一实现，本模块不自算）。

三条策略线：

  1. **润色失败保留已验证的 clean**：bundle 的 ``candidate_sha256`` 描述的是
     **通过验证的那份文本**，不是最新生成的那份；润色阶段的 patch 一律拒收。
  2. **再检查有界**：``RECHECK_LIMIT`` 次自动重生成+重验，用完仍失败就写
     ``stopped_reason="recheck_limit"`` 停下——不无限回流，也不把失败说成通过。
  3. **stage 不猜**：bundle 的 ``stage`` 取任务运行身份里的 ``release_stage``，
     缺失或不在值域内直接报错。
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path

import novel_contracts as nct
import novel_review as nrv
import novel_segments as ns
import novel_validation as nv
import v2_contracts as vc

#: 自动回流上限：一次再检查，不多。第二次仍坏就停，交回给人。
RECHECK_LIMIT = 1

#: 旧版这里按扫描档位推导编辑力度（expanded→deep），等于用户没同意过就升档。
#: 现在力度只从 TaskSpec.strength 取——scan 只决定扫多广，不决定能动多少。

#: 阶段名（也是 reviewer 申报时要对上的 stage）。
STAGE_CLEAN = "clean"
STAGE_POLISHED = "polished"

#: 第二次语义复核的事实形状（G7 甲案）。底层合同仍是
#: ``novel_contracts.validate_ledger(second_semantic_review=<claim_id 集合>)``：
#: 本模块只负责**从逐 claim 的真实复核事实里投影**出那个集合，绑定不齐的一律不投影。
SECOND_REVIEW_SCHEMA = "lieflat-v2-second-semantic-review-1"
SECOND_REVIEW_VERDICTS = ("confirmed", "failed", "unknown")
#: 同宿主第二遍是允许的，但必须如实标 same_host；自称外部裁判就要给可证 routing 标识。
SECOND_REVIEW_INDEPENDENCE = ("same_host", "external")
#: 事实里必须齐备的绑定字段：少一个就当没复核过（fail-closed，不默认放行）。
SECOND_REVIEW_REQUIRED = ("claim_id", "verdict", "source_sha256", "candidate_sha256",
                          "ledger_sha256", "stage", "rule_version", "prompt_version",
                          "model_version", "judge_provider", "judge_model",
                          "judge_epoch", "independence", "artifact")


def entry_field(entry, name, default=""):
    if isinstance(entry, Mapping):
        return entry.get(name, default)
    return getattr(entry, name, default)


def is_sensitive_certain(entry) -> bool:
    """底层那张类别表说了算，本层不另立「什么算剧情敏感」。"""
    return (str(entry_field(entry, "category") or "")
            in nct.PLOT_SENSITIVE_CATEGORIES
            and str(entry_field(entry, "certainty") or "") == "certain")


def second_review_facts(reviewer, *, original: str, candidate: str, ledger,
                        stage: str, task: vc.TaskSpec, judge_epoch,
                        context: str | None = None) -> tuple:
    """逐 claim 的真实二次复核入口：一条一条问，一条一条记，绝不整批发 ID。

    reviewer 没有 ``review_claim`` 这条能力时返回空事实——那是「没复核过」，
    不是「复核通过」；本层不代它编造，也不退化成把整本 ledger 的 claim_id 交出去。
    """
    if not isinstance(task, vc.TaskSpec):
        raise ValueError("二次复核必须绑任务身份（TaskSpec）：没有身份就没有可核对的出处")
    ask = getattr(reviewer, "review_claim", None)
    if not callable(ask):
        return ()
    judge = dict(task.judge_ref or {})
    entries = tuple(ledger or ())
    facts: list = []
    for entry in entries:
        if not is_sensitive_certain(entry):
            continue
        claim_id = str(entry_field(entry, "claim_id") or "").strip()
        if not claim_id:
            continue
        answer = ask(dict(entry) if not isinstance(entry, Mapping) else dict(entry),
                     original=original, candidate=candidate,
                     context=(original if context is None else context),
                     ledger=entries, stage=stage)
        if not isinstance(answer, Mapping):
            raise ValueError(f"review_claim 必须返回映射，收到 {type(answer).__name__}")
        verdict = str(answer.get("verdict") or "unknown")
        if verdict not in SECOND_REVIEW_VERDICTS:
            verdict = "unknown"
        independence = str(answer.get("independence") or "same_host")
        if independence not in SECOND_REVIEW_INDEPENDENCE:
            independence = "same_host"
        routing = str(answer.get("routing") or "").strip()
        facts.append({
            "schema": SECOND_REVIEW_SCHEMA, "claim_id": claim_id,
            "category": str(entry_field(entry, "category") or ""),
            "verdict": verdict,
            "note": str(answer.get("note") or "")[:400],
            "source_sha256": _sha(original), "candidate_sha256": _sha(candidate),
            "ledger_sha256": nrv.ledger_digest(entries), "stage": stage,
            "rule_version": str(task.rule_version), "prompt_version": str(task.prompt_version),
            "model_version": str(task.model_version),
            "judge_provider": str(judge.get("provider") or ""),
            # 自称外部裁判时记的是可证 routing 标识；同宿主第二遍记宿主申报的模型
            "judge_model": routing if independence == "external" else str(judge.get("model") or ""),
            "judge_epoch": int(judge_epoch), "independence": independence,
            "routing": routing, "artifact": str(answer.get("artifact") or "").strip()})
    return tuple(facts)


def second_review_projection(facts, *, original: str, candidate: str, ledger,
                             stage: str, task: vc.TaskSpec, judge_epoch) -> dict:
    """把复核事实**投影**成底层合同要的 claim_id 集合：绑定不齐的一条都不进。

    对不上就退回「该 claim 仍缺第二次复核」的原判（由底层给出
    ``plot_sensitive_needs_second_review``），本层不新造判定码也不改类别。
    「confirmed 但一句依据都没写」与「点名的 claim 不在这份账本里」同样不进：
    生产端拒空依据，消费端若不看同一栏，绕过生产端直接喂 facts 就能把没复核洗成已复核。
    """
    if not isinstance(task, vc.TaskSpec):
        raise ValueError("投影二次复核需要任务身份：否则无法核对规则与裁判版本")
    judge = dict(task.judge_ref or {})
    want = {"source_sha256": _sha(original), "candidate_sha256": _sha(candidate),
            "ledger_sha256": nrv.ledger_digest(tuple(ledger or ())), "stage": stage,
            "rule_version": str(task.rule_version),
            "prompt_version": str(task.prompt_version),
            "model_version": str(task.model_version)}
    projected: list = []
    rejected: list = []
    seen: set = set()
    known = {str(entry_field(entry, "claim_id") or "").strip()
             for entry in tuple(ledger or ()) if is_sensitive_certain(entry)}
    known.discard("")
    for fact in tuple(facts or ()):
        if not isinstance(fact, Mapping) or fact.get("schema") != SECOND_REVIEW_SCHEMA:
            rejected.append({"claim_id": str(entry_field(fact, "claim_id") or ""),
                             "reason": "schema"})
            continue
        claim_id = str(fact.get("claim_id") or "").strip()
        problem = None
        if not claim_id or claim_id in seen:
            problem = "claim_id"
        elif str(fact.get("verdict") or "") != "confirmed":
            problem = "verdict"
        elif str(fact.get("note") or "").strip() == "":
            problem = "note"
        elif any(str(fact.get(key) or "") != value for key, value in want.items()):
            problem = next(key for key, value in want.items()
                           if str(fact.get(key) or "") != value)
        elif str(fact.get("judge_provider") or "") != str(judge.get("provider") or ""):
            problem = "judge_provider"
        elif str(fact.get("judge_model") or "") != str(
                fact.get("routing") or "" if fact.get("independence") == "external"
                else judge.get("model") or ""):
            problem = "judge_model"
        elif fact.get("independence") not in SECOND_REVIEW_INDEPENDENCE:
            problem = "independence"
        elif (fact.get("independence") == "external"
              and not str(fact.get("routing") or "").strip()):
            problem = "routing"
        elif str(fact.get("artifact") or "").strip() == "":
            problem = "artifact"
        elif claim_id not in known:
            problem = "claim_not_in_ledger"
        else:
            try:
                if int(fact.get("judge_epoch")) != int(judge_epoch):
                    problem = "judge_epoch"
            except (TypeError, ValueError):
                problem = "judge_epoch"
        if problem:
            rejected.append({"claim_id": claim_id, "reason": problem})
            continue
        seen.add(claim_id)
        projected.append(claim_id)
    return {"projected": tuple(projected), "rejected": tuple(rejected)}


#: 判词文档＝二次复核**生产端**的落盘形状。G7 接好了消费端（事实→按阶段投影→
#: 提交前出处门→发布复验字节绑定），生产端此前只存在于测试的可控构造评审器里：
#: 真实宿主上没有任何已发布的件能产出事实，剧情敏感 certain 条目因此恒 REVIEW。
#: 这里补的就是那一小段接线——判定、字段、投影一律沿用上面的现有权威，不另造引擎。
VERDICT_DOC_SCHEMA = "lieflat-v2-second-review-verdicts-1"
DOC_FIELDS = frozenset({"schema", "verdicts"})
VERDICT_FIELDS = frozenset({"claim_id", "verdict", "note", "independence",
                            "routing", "artifact"})
NO_EVIDENCE_NOTE = "判词文档里没有这条 claim 的可用依据"


def claims_to_review(ledger) -> tuple:
    """这份账本里必须逐条复核的 claim（筛子就是上面那一个，不在此重定义类别）。"""
    out = []
    for entry in tuple(ledger or ()):
        if not is_sensitive_certain(entry):
            continue
        claim_id = str(entry_field(entry, "claim_id") or "").strip()
        if claim_id:
            out.append({"claim_id": claim_id,
                        "category": str(entry_field(entry, "category") or "")})
    return tuple(out)


def load_verdict_document(path) -> dict:
    """读判词文档：形状不对当场拒，不做「差不多就收下」的宽容。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"判词文档读不出来：{path}（{exc}）") from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"判词文档不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(doc, dict):
        raise ValueError("判词文档顶层必须是对象")
    unknown = sorted(set(doc) - DOC_FIELDS)
    if unknown:
        raise ValueError(f"判词文档含未知顶层字段: {','.join(unknown)}")
    if doc.get("schema") != VERDICT_DOC_SCHEMA:
        raise ValueError(f"判词文档 schema 必须是 {VERDICT_DOC_SCHEMA}，"
                         f"收到 {doc.get('schema')!r}")
    verdicts = doc.get("verdicts")
    if not isinstance(verdicts, list):
        raise ValueError("判词文档的 verdicts 必须是列表（没有判词就交空列表）")
    seen: set = set()
    for index, item in enumerate(verdicts):
        if not isinstance(item, Mapping):
            raise ValueError(f"第 {index + 1} 条判词不是对象")
        extra = sorted(set(item) - VERDICT_FIELDS)
        if extra:
            raise ValueError(f"第 {index + 1} 条判词含未知字段: {','.join(extra)}")
        claim_id = str(item.get("claim_id") or "").strip()
        if not claim_id:
            raise ValueError(f"第 {index + 1} 条判词缺 claim_id")
        if claim_id in seen:
            raise ValueError(f"同一条 claim 判了两次，不知以哪条为准：{claim_id}")
        seen.add(claim_id)
    return {"schema": VERDICT_DOC_SCHEMA, "verdicts": tuple(verdicts)}


class VerdictDocumentReviewer:
    """只回答「文档里对这条 claim 怎么判」，不替复核方编结论。

    fail-closed 三条：文档里**没有**这条 claim 记 unknown；判 confirmed 却**没有 note**
    记 unknown（空依据不算复核过）；**非法判定词**记 unknown。``artifact`` 缺省就是
    这份文档自己的绝对路径，复核事实因此永远指向一件真实落盘的证据。
    """

    def __init__(self, doc: Mapping, *, artifact: str):
        self._by_id = {str(item.get("claim_id") or "").strip(): dict(item)
                       for item in doc.get("verdicts") or ()}
        self.artifact = str(artifact)
        self.asked: list = []

    def review_claim(self, claim, *, original, candidate, context, ledger, stage) -> dict:
        claim_id = str((claim or {}).get("claim_id") or "").strip()
        self.asked.append(claim_id)
        answer = dict(self._by_id.get(claim_id) or {})
        verdict = str(answer.get("verdict") or "").strip()
        note = str(answer.get("note") or "").strip()
        if verdict not in SECOND_REVIEW_VERDICTS or not note:
            verdict = "unknown"
            note = note or NO_EVIDENCE_NOTE
        return {"verdict": verdict, "note": note,
                "independence": str(answer.get("independence") or "same_host"),
                "routing": str(answer.get("routing") or "").strip(),
                "artifact": str(answer.get("artifact") or "").strip() or self.artifact}


def facts_from_verdict_document(path, *, original: str, candidate: str, ledger,
                                stage: str, task: vc.TaskSpec, judge_epoch) -> tuple:
    """判词文档 → :func:`second_review_facts` 的绑定事实（唯一的接合点）。"""
    doc = load_verdict_document(path)
    reviewer = VerdictDocumentReviewer(
        doc, artifact=str(Path(path).resolve().as_posix()))
    return second_review_facts(reviewer, original=original, candidate=candidate,
                               ledger=ledger, stage=stage, task=task,
                               judge_epoch=judge_epoch)


_MECHANICAL_LEVELS = ("L0", "L1")
_WORST_ORDER = ("PASS", "NO-OP", "REVIEW", "FAIL", "ERROR")
#: 现有 ``mechanical_verdict`` 的词表与本模块阶段状态词表之间唯一的翻译点。
#: ``PASS_MECHANICAL`` 只说明机械两层过了，**不等于可发布**（MECHANICAL_SCOPE_NOTE）；
#: 可发布与否一律由 novel_review.publication_decision 说，本模块不改口径。
_VERDICT_BY_MECHANICAL = {"PASS_MECHANICAL": "PASS"}


def _status_of(verdict: str) -> str:
    return _VERDICT_BY_MECHANICAL.get(verdict, verdict)


def sha256_of_text(text: str) -> str:
    """文本摘要走现有权威（与 ``v2_preview`` 的绑定门同一个函数）。"""
    return nct.sha256_text(text)


_sha = sha256_of_text


def _worst(*statuses: str) -> str:
    known = [item for item in statuses if item in _WORST_ORDER]
    if not known:
        return "ERROR"
    return max(known, key=_WORST_ORDER.index)


def _require_text(label: str, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串，收到 {type(value).__name__}")
    return value


def _segments(text: str, protection_items: tuple = ()):
    """按现有分段器切段；保护项的原始坐标作为禁切区间交进去（分段不从引文内部下刀）。

    ``split_novel`` 只认 (start, end) 文档坐标，保护清单本身由调用方（现有 run
    manifest）带进来，本层不自己判定「什么算引文」。
    """
    ranges = tuple((int(item["original_position"][0]), int(item["original_position"][1]))
                   for item in tuple(protection_items or ())
                   if isinstance(item, Mapping) and len(item.get("original_position") or ()) == 2)
    return tuple(ns.split_novel(text, protected_ranges=ranges))


def _fidelity(source: str, target: str, *, policy, ledger, source_segments,
              target_segments, second_semantic_review=()) -> dict:
    """一次比较的 L0/L1 读数（全部来自现有 run_validation，本层不自己判保真）。

    「双向」按仓库口径由同一次比较里的两类判定共同覆盖：
    丢了登记实体/保护项（``registered_entity_lost`` 等）与成稿新增来源不足的内容
    （``unsourced_new_content`` 等）。把参数对调再跑一遍会让账本 output_anchor
    指向另一侧分段，凭空造出 ``unknown_segment``——那是假红，不是结论。
    """
    l0, l1 = nv.run_validation(nv.ValidationInput(
        source_text=source, output_text=target, policy=policy,
        segments=source_segments, output_segments=target_segments, ledger=ledger,
        second_semantic_review=tuple(second_semantic_review or ())))
    return {"status": _status_of(nv.mechanical_verdict(l0, l1)),
            "levels": _MECHANICAL_LEVELS,
            "codes": tuple(sorted(set(l0.codes) | set(l1.codes))),
            "results": (l0, l1)}


def _stage_status(stage: dict) -> str:
    return _worst(stage["fidelity"]["status"], stage["protections"]["status"])


def _protection_reading(protections, segments, *, text: str) -> dict:
    """真实引文逐字锁定：直接吃 validate_protections 的判定码。"""
    if not tuple(protections or ()):
        return {"status": "PASS", "codes": (), "entries": ()}
    try:
        result = nct.validate_protections(tuple(protections), segments, output_text=text)
    except nct.ContractError as exc:
        return {"status": "ERROR", "codes": (f"protection_contract:{exc.code}",),
                "entries": ()}
    codes = tuple(sorted({issue.code for issue in
                          tuple(result.issues) + tuple(result.rejected)}))
    status = result.status if result.status in _WORST_ORDER else "REVIEW"
    return {"status": status, "codes": codes,
            "entries": tuple({"entry_id": item.entry_id,
                              "excerpt": item.original_text}
                             for item in getattr(result, "accepted", ()))}


def _review_reading(reviewer, text, *, original, ledger, stage, output_segments):
    """语义层级与 finding 来自宿主 reviewer；层级值域仍由 novel_review 说了算。"""
    answer = reviewer.review(text, original=original, ledger=ledger, stage=stage)
    if not isinstance(answer, dict):
        raise ValueError(f"reviewer.review 必须返回映射，收到 {type(answer).__name__}")
    levels = tuple(answer.get("completed_levels") or ())
    if not levels:
        raise ValueError("reviewer 没有申报跑完了哪些语义层级：空申报不等于没问题")
    for level in levels:
        if level not in nrv.COMPLETABLE_LEVELS:
            raise ValueError(f"reviewer 申报了不存在的层级 {level!r}"
                             f"（在册：{nrv.COMPLETABLE_LEVELS}）")
    report = nrv.ReviewReport(
        segment_ids=tuple(item.segment_id for item in output_segments),
        completed_levels=levels,
        findings=tuple(answer.get("findings") or ()),
        ledger_sha256=nrv.ledger_digest(ledger))
    return report


def _validation_of(report, *, segments, ledger, output_segments,
                   second_semantic_review=()) -> tuple:
    """报告校验走 validate_review，全书窗口交出真实重读产物（不给就是没证据）。"""
    plan = nrv.plan_whole_book_review(nrv.build_full_book_index(
        segments=segments, ledger=tuple(ledger)))
    reread = [nrv.reread_window_texts(window, segments, output_segments)
              for window in plan.windows]
    validation = nrv.validate_review(report, segments=segments, ledger=tuple(ledger),
                                     output_segments=output_segments, plan=plan,
                                     reread=reread,
                                     second_semantic_review=tuple(second_semantic_review or ()))
    return validation, plan, reread


def _release_stage(task: vc.TaskSpec) -> str:
    identity = dict(task.runtime_identity or {})
    stage = identity.get("release_stage")
    if not isinstance(stage, str) or not stage.strip():
        raise ValueError("运行身份里没有 release_stage：不许默认成某个 alpha，"
                         "请先经 v2_task.runtime_identity() 产出真实身份")
    if stage not in vc.RELEASE_STAGES:
        raise ValueError(f"release_stage {stage!r} 不在值域 {vc.RELEASE_STAGES} 内")
    return stage


def stage_outcomes(original: str, clean: str, polished: str | None = None, *,
                   task: vc.TaskSpec, ledger: tuple, reviewer: object,
                   protections: tuple = (), second_review: tuple | None = None,
                   judge_epoch=None) -> dict:
    """三段文本的双向保真 + 保护核实 + 语义报告，汇成一个可追溯的结果字典。

    ``verified_stage`` 是**通过验证的那一份**；bundle 的 ``candidate_sha256``
    与它一致，因此「留的是哪份文本」在证据里可见，不靠口头承诺。

    ``second_review`` 是逐 claim 的第二次语义复核**事实**（由
    :func:`second_review_facts` 产出）。带事实就必须同时给 ``judge_epoch``：
    纪元不许猜。每一轮都按「这一步的候选文本」重新投影，所以 clean 的复核
    放行不了新的 polished。
    """
    if not isinstance(task, vc.TaskSpec):
        raise ValueError("task 必须是 TaskSpec")
    if reviewer is None or not hasattr(reviewer, "review"):
        raise ValueError("reviewer 必须提供 review(text, *, original, ledger, stage)")
    if second_review and judge_epoch is None:
        raise ValueError("带二次复核事实必须交出当前裁判纪元（judge_epoch）：不许默认某个纪元")
    _require_text("original", original)
    _require_text("clean", clean)
    if polished is not None:
        _require_text("polished", polished)
    ledger = tuple(ledger)
    items = tuple(protections or ())
    source_segments = _segments(original, items)
    policy = nv.ValidationPolicy(scene="novel",
                                 strength=task.strength)

    candidates = [(STAGE_CLEAN, clean)]
    if polished is not None:
        candidates.append((STAGE_POLISHED, polished))
    stages: dict[str, dict] = {}
    for name, text in candidates:
        target_segments = _segments(text, items)
        projection = second_review_projection(
            second_review, original=original, candidate=text, ledger=ledger,
            stage=name, task=task, judge_epoch=judge_epoch)
        stage = {
            "text": text,
            "fidelity": _fidelity(original, text, policy=policy, ledger=ledger,
                                  source_segments=source_segments,
                                  target_segments=target_segments,
                                  second_semantic_review=projection["projected"]),
            "protections": _protection_reading(protections, source_segments, text=text),
            "second_review": projection,
        }
        stage["status"] = _stage_status(stage)
        stage["codes"] = tuple(sorted(set(stage["fidelity"]["codes"])
                                      | set(stage["protections"]["codes"])))
        stages[name] = stage

    verified_name = next((name for name in (STAGE_POLISHED, STAGE_CLEAN)
                          if name in stages and stages[name]["status"] == "PASS"), None)
    kept_text = stages[verified_name]["text"] if verified_name else ""
    kept_segments = _segments(kept_text, items) if kept_text else source_segments
    # 语义报告申报的是**被审的那份分段**（原稿分段），与现有 make_report 与发布端的
    # 「复核必须覆盖全部分段」同口径；申报成稿分段会让 validate_review 判
    # report_unknown_segment / segment_not_reviewed（C32 接真实发布时照出来的）。
    report = _review_reading(reviewer, kept_text or clean, original=original,
                             ledger=ledger, stage=verified_name or STAGE_CLEAN,
                             output_segments=source_segments)
    validation, plan, reread = _validation_of(
        report, segments=source_segments, ledger=ledger, output_segments=kept_segments,
        second_semantic_review=(stages[verified_name]["second_review"]["projected"]
                                if verified_name else ()))
    bundle = vc.VerificationBundle(
        mechanical_levels=_MECHANICAL_LEVELS, review_report=report,
        review_validation=validation, ledger_sha256=nrv.ledger_digest(ledger),
        source_sha256=_sha(original), candidate_sha256=_sha(kept_text or clean),
        stage=_release_stage(task))
    # 顶层给「被验证那一份」的保护读数；各阶段原始读数留在 stages 里，不丢细节。
    reading = stages.get(verified_name) if verified_name in stages \
        else stages.get(STAGE_CLEAN) or stages.get(STAGE_POLISHED)
    return {"verified_stage": verified_name, "stages": stages, "rechecks": 0,
            "second_review": {"facts": tuple(second_review or ()),
                              "projection": {name: stages[name]["second_review"]
                                             for name in stages}},
            "stopped_reason": None if verified_name else "no_stage_verified",
            "bundle": bundle, "review_validation": validation, "review_report": report,
            "protections": dict(reading["protections"]),
            # 分段与账本原文一并带出：现有发布通路吃的就是这几份分段记录，
            # 写证据文档时再切一次会造出第二份「同一段落的不同形状」。
            "source_segments": source_segments, "output_segments": kept_segments,
            "ledger": ledger, "protection_items": items, "verified_text": kept_text or clean,
            "plan": plan, "reread": tuple(reread), "reviewer": reviewer,
            "policy": policy}



def _facts_for_evidence(facts, *, source_sha256: str, candidate_sha256: str) -> list:
    """复核事实进证据文档：原样带判定与出处，另记这份证据绑的磁盘字节摘要。

    旧行为是把 manifest 那一栏硬写成 ``[]`` —— 那等于「每次都没复核过」。
    未通过或绑定不齐的事实照样落盘（要看得见发生过什么），但发布复验只认
    ``verdict == confirmed`` 且字节绑定与这份证据一致的那几条。
    """
    return [dict(item, bytes_bindings={"source_sha256": source_sha256,
                                       "candidate_sha256": candidate_sha256})
            for item in tuple(facts or ()) if isinstance(item, Mapping)]


def evidence_documents(result: dict, *, candidate_text: str, source_sha256: str,
                       candidate_sha256: str, scene: str = "novel",
                       strength: str = "balanced",
                       break_candidate_binding: bool = False,
                       second_review=None) -> dict:
    """把一次验证的真实读数落成三份证据文档字典（不落盘）。

    字段形状是现有安全交付合同要求的形状（``novel_output`` 里的三个 schema 常量），
    数据全部来自本次实际计算：L0/L1 是 ``run_validation`` 的读数、分段是 ``split_novel``
    的读数、账本摘要是 ``ledger_digest`` 的读数、复核结论是 ``validate_review`` 的读数。
    空 ledger / 空 protections 也照实写——它证明「算过且是空」，与「没算」不同：
    摘要是现场算出来的值，不是占位串。

    ``source_sha256`` / ``candidate_sha256`` 是**磁盘字节摘要**（现有 ``_check_binding``
    比的就是它），与 bundle 里的文本摘要是两回事，各写各的位置。
    """
    import novel_output as no

    verified = result["verified_stage"] or STAGE_CLEAN
    fidelity = result["stages"][verified]["fidelity"]
    l0, l1 = fidelity["results"]
    validation_answer = result["review_validation"]
    binding_candidate = (sha256_of_text("被换掉的证据绑定") if break_candidate_binding
                         else candidate_sha256)
    shared = {"source_sha256": source_sha256, "candidate_sha256": binding_candidate,
              "scene": scene, "strength": strength}
    ledger = tuple(result["ledger"])
    segments = tuple(result["source_segments"])
    output_segments = tuple(result["output_segments"])
    validation = {"schema": no.VALIDATION_REPORT_SCHEMA, **shared,
                  "l0": {"status": l0.status, "level": l0.level, "codes": list(l0.codes)},
                  "l1": {"status": l1.status, "level": l1.level, "codes": list(l1.codes)}}
    manifest = {"schema": no.PROTECTION_MANIFEST_SCHEMA, **shared,
                "segments": [dict(item.__dict__) for item in segments],
                "output_segments": [dict(item.__dict__) for item in output_segments],
                "protections": [dict(item) for item in result["protection_items"]],
                "authorizations": [],
                "ledger": [dict(item) for item in ledger],
                "second_semantic_review": _facts_for_evidence(
                    (result.get("second_review") or {}).get("facts", ())
                    if second_review is None else second_review,
                    source_sha256=source_sha256, candidate_sha256=candidate_sha256),
                "indistinguishable": []}
    review = {"schema": no.REVIEW_REPORT_SCHEMA, **shared,
              "completed_levels": list(result["review_report"].completed_levels),
              "segment_ids": [item.segment_id for item in segments],
              "ledger_sha256": nrv.ledger_digest(ledger),
              "review": {"status": validation_answer.status,
                         "codes": list(validation_answer.codes)}}
    return {"validation": validation, "manifest": manifest, "review": review}


def write_evidence(result: dict, *, candidate_text: str, out_dir, source_sha256: str,
                   candidate_sha256: str, scene: str = "novel",
                   strength: str = "balanced", ledger=None,
                   break_candidate_binding: bool = False, second_review=None) -> dict:
    """三份证据文档落盘（复用 ``novel_job._atomic_write``，不自己拼临时文件）。

    返回 ``{"validation": Path, "manifest": Path, "review": Path}``，直接填进
    现有 ``CommitRequest`` 的三个报告字段。
    """
    import novel_job

    if ledger is not None:
        result = dict(result, ledger=tuple(ledger))
    docs = evidence_documents(
        result, candidate_text=candidate_text, source_sha256=source_sha256,
        candidate_sha256=candidate_sha256, scene=scene, strength=strength,
        break_candidate_binding=break_candidate_binding, second_review=second_review)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    names = {"validation": "v2-validation-report.json",
             "manifest": "v2-protection-manifest.json", "review": "v2-review-report.json"}
    written = {}
    for key, name in names.items():
        path = out / name
        novel_job._atomic_write(path, docs[key])
        written[key] = path
    return written


def verify_stages(original: str, clean: str, polished: str | None = None, *,
                  task: vc.TaskSpec, ledger: tuple, reviewer: object,
                  protections: tuple = (), second_review=None,
                  judge_epoch=None) -> vc.VerificationBundle:
    """工单钉住的接口：返回描述「验证过的那份文本」的 VerificationBundle。"""
    return stage_outcomes(original, clean, polished, task=task, ledger=ledger,
                          reviewer=reviewer, protections=protections,
                          second_review=second_review, judge_epoch=judge_epoch)["bundle"]


def publication_status(result: dict) -> nrv.PublicationDecision:
    """发布判定仍然只由现有 publication_decision 给出（本层不自己宣布可发布）。

    没有通过验证的文本时，取 clean 阶段的 L0/L1 读数去合流——它自己就会把
    ``FAIL``/``REVIEW`` 带出来，本层不去伪造一个「不可发布」的判定码。
    """
    verified = result["verified_stage"] or STAGE_CLEAN
    stage = result["stages"][verified]
    l0, l1 = stage["fidelity"]["results"]
    return nrv.publication_decision(l0, l1, result["review_validation"])


def verify_with_recheck(original: str, *, clean: str, generate_polished,
                        task: vc.TaskSpec, ledger: tuple, reviewer: object,
                        protections: tuple = (), second_review=None, judge_epoch=None,
                        recheck_limit: int = RECHECK_LIMIT) -> dict:
    """润色失败时最多自动再检查 ``recheck_limit`` 次；用完仍坏就停下。

    再检查只是「再要一次建议、再验一遍」，不追加新的判定口径：每一遍都走同一个
    ``stage_outcomes``，所以「通过」的含义全程一致，不会越滚越松。
    """
    if not callable(generate_polished):
        raise ValueError("generate_polished 必须是可调用的宿主生成入口")
    if recheck_limit < 0:
        raise ValueError(f"recheck_limit 必须是非负整数，收到 {recheck_limit}")
    result = stage_outcomes(original, clean, generate_polished(), task=task,
                            ledger=ledger, reviewer=reviewer, protections=protections,
                            second_review=second_review, judge_epoch=judge_epoch)
    rechecks = 0
    while (result["verified_stage"] != STAGE_POLISHED and rechecks < recheck_limit):
        rechecks += 1
        result = stage_outcomes(original, clean, generate_polished(), task=task,
                                ledger=ledger, reviewer=reviewer,
                                protections=protections, second_review=second_review,
                                judge_epoch=judge_epoch)
        result = dict(result, rechecks=rechecks)
    result = dict(result, rechecks=rechecks)
    if result["verified_stage"] != STAGE_POLISHED:
        result = dict(result, stopped_reason="recheck_limit")
    return result


def mark_patches(clean_patches: tuple, polish_patches: tuple, result: dict) -> tuple:
    """把验证结果回标到 patch 上：润色未通过验证时该阶段 patch 全部拒收。

    返回值仍是现有 ``PatchRecord``（只换 ``validation`` 字段），不新增第二套
    patch 类型；``accepted_by`` 留给 P12 的用户明确接受事件。
    """
    verified = result["verified_stage"]
    accepted: list = []
    rejected: list = []
    for patch in tuple(clean_patches):
        if verified is None:
            rejected.append(_refuse(patch, "no_stage_verified"))
            continue
        accepted.append(dataclasses.replace(
            patch, validation={**dict(patch.validation), "verified": True,
                               "stage": verified,
                               "bundle_candidate_sha256": result["bundle"]
                               .candidate_sha256}))
    for patch in tuple(polish_patches):
        if verified == STAGE_POLISHED:
            accepted.append(dataclasses.replace(
                patch, validation={**dict(patch.validation), "verified": True,
                                   "stage": verified,
                                   "bundle_candidate_sha256": result["bundle"]
                                   .candidate_sha256}))
            continue
        rejected.append(_refuse(patch, "polish_stage_not_verified"))
    return tuple(accepted), tuple(rejected)


def _refuse(patch, reason: str):
    return dataclasses.replace(
        patch, validation={**dict(patch.validation), "verified": False,
                           "refused_reason": reason})
