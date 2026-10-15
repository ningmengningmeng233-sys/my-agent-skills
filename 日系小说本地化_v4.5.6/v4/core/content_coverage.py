# -*- coding: utf-8 -*-
"""逐句内容覆盖清单（Task 9.9.2，工单 §9.9.2）。

职责边界（严格）：本模块**不访问 API、不访问数据库、不触状态机**，只负责
构造与校验数据。

存在理由（真实批次实测）：译者在改写阶段会漏掉源句，最典型是段末景物/收束句。
ch-0006 的 `地面には、黒い染みだけが残った` 连续 8 个版本全部漏译；事件门正确
拦截了它，但当时「低置信事件降级为 warning」的放宽让 5 个内容缺失的章节被误批准
（该放宽已回退）。事件图只能覆盖被抽取成事件的情节句，**景物句与普通句没有兜底**，
因此这里为**每一个源句**建立必需项。

判定强度（工单 §9.9.3 表）：

============  ==================================================
status        结果
============  ==================================================
present       通过
exempted      仅当存在精确指向该覆盖项 ID 的已批准豁免决策时通过
partial       fail（须返修）
absent        fail（须返修）
uncertain     blocked（不得默认放行）
（缺判定）     按 uncertain 处理——漏检不得静默通过
============  ==================================================

低置信事件关联句只提高**人工复核优先级**（``manual_review_priority='high'``），
永远是必需项；不得因置信度低而免除。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: 句子终止符（日文与西文）
_SENTENCE_END = "。！？!?"
#: 右引号/右括号：紧随终止符时并入上一句（`「今だ！」と叫んだ。` 是**一句**）
_CLOSERS = "」』）)】》"
#: 右引号后仍可能继续同句的终止符
_QUOTE_TRAIL = _CLOSERS + "。！？!?"

#: 视为「非情节句」的置信度上限：低于此值的关联事件会让该句进入高优先级人工复核
LOW_CONFIDENCE = 0.5

#: 允许的状态取值
STATUSES = ("present", "partial", "absent", "uncertain", "exempted")
#: 直接判 fail 的状态
FAIL_STATUSES = frozenset({"absent", "partial"})
#: 判 blocked 的状态
BLOCKED_STATUSES = frozenset({"uncertain"})


@dataclass(frozen=True)
class CoverageRequirement:
    """一个源句的覆盖要求（不可变；豁免需显式决策 ID）。"""

    id: str
    chapter_id: str
    source_paragraph_id: str
    source_sentence_id: str
    source_text: str
    linked_event_ids: tuple[str, ...] = ()
    mandatory: bool = True
    manual_review_priority: str = "normal"  # normal / high
    omission_exception_decision_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chapter_id": self.chapter_id,
            "source_paragraph_id": self.source_paragraph_id,
            "source_sentence_id": self.source_sentence_id,
            "source_text": self.source_text,
            "linked_event_ids": list(self.linked_event_ids),
            "mandatory": self.mandatory,
            "manual_review_priority": self.manual_review_priority,
            "omission_exception_decision_id": self.omission_exception_decision_id,
        }


@dataclass(frozen=True)
class CoverageFinding:
    """对某个覆盖项的判定结果（由覆盖判官产出）。"""

    requirement_id: str
    status: str
    output_paragraph_ids: tuple[str, ...] = ()
    evidence: str = ""
    judge_run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "status": self.status,
            "output_paragraph_ids": list(self.output_paragraph_ids),
            "evidence": self.evidence,
            "judge_run_id": self.judge_run_id,
        }


def split_source_sentences(paragraph: str) -> list[str]:
    """把源段落稳定切分为句子。

    - 按 `。！？!?` 切分，并把紧随其后的右引号并入上一句
      （`「今だ！」と森田が叫んだ。` 只算一句，不拆成 `「今だ！` + 剩余）；
    - 空白片段丢弃；不改变任何字符内容（只 strip 首尾空白）。
    """
    text = (paragraph or "").strip()
    if not text:
        return []
    parts = re.split(f"(?<=[{_SENTENCE_END}])", text)
    merged: list[str] = []
    for part in parts:
        stripped = part.lstrip()
        if merged and stripped and stripped[0] in _CLOSERS:
            merged[-1] += part
        else:
            merged.append(part)
    # 终止符后的残余（如 `……。」` 之后的尾巴）再合并一次，保证不再有孤立右引号
    final: list[str] = []
    for sentence in merged:
        stripped = sentence.strip()
        if final and stripped and stripped[0] in _QUOTE_TRAIL and stripped[0] in _CLOSERS:
            final[-1] += sentence
        else:
            final.append(sentence)
    return [s.strip() for s in final if s.strip()]


def _source_paragraphs(source_text: str) -> list[str]:
    """按空行切分源稿段落；**包含标题行**（工单示例 `s-0008-001` 即从 1 起编号）。"""
    return [p.strip() for p in re.split(r"\n\s*\n", source_text or "") if p.strip()]


def _terms(text: str, *, min_len: int = 2, max_len: int = 30) -> list[str]:
    """切出可比对的词元（本地实现，避免与 checks 形成循环依赖）。"""
    out: list[str] = []
    for run in re.split(r"[^\u4e00-\u9fff\u3040-\u30ff\u30a0-\u30ffA-Za-z0-9]+", text or ""):
        if min_len <= len(run) <= max_len:
            out.append(run)
    return out


def _linked_events(sentence: str, events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """找出与该句内容重叠的事件（动作或结果词元命中）。"""
    hits: list[Mapping[str, Any]] = []
    for event in events or ():
        action = str(event.get("action", "") or "")
        result = str(event.get("result", "") or "")
        candidates = _terms(action) + ([] if result == "无明确结果" else _terms(result))
        if any(term and term in sentence for term in candidates):
            hits.append(event)
    return hits


def _confidence(event: Mapping[str, Any]) -> float | None:
    raw = event.get("confidence")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def build_coverage_requirements(
    *,
    chapter_id: str,
    source_text: str,
    events_graph: Mapping[str, Any] | None = None,
    input_language: str = "ja",
) -> list[CoverageRequirement]:
    """为源稿每一句构造覆盖要求（默认全部必需）。

    句子 ID 形如 ``s-<段号4位>-<句号3位>``（从 1 起，含标题行段）。
    低置信事件关联句标记 ``manual_review_priority='high'``，但 ``mandatory`` 仍为 True。
    """
    events = list((events_graph or {}).get("events", []) or [])
    requirements: list[CoverageRequirement] = []
    for para_index, paragraph in enumerate(_source_paragraphs(source_text), start=1):
        paragraph_id = f"s-p{para_index:04d}"
        for sent_index, sentence in enumerate(split_source_sentences(paragraph), start=1):
            linked = _linked_events(sentence, events)
            confidences = [c for c in (_confidence(e) for e in linked) if c is not None]
            priority = "high" if linked and (not confidences or min(confidences) < LOW_CONFIDENCE) else "normal"
            requirements.append(
                CoverageRequirement(
                    id=f"s-{para_index:04d}-{sent_index:03d}",
                    chapter_id=chapter_id,
                    source_paragraph_id=paragraph_id,
                    source_sentence_id=f"{paragraph_id}-s{sent_index:03d}",
                    source_text=sentence,
                    linked_event_ids=tuple(str(e.get("event_id", "")) for e in linked if e.get("event_id")),
                    mandatory=True,
                    manual_review_priority=priority,
                )
            )
    return requirements


def aggregate_coverage(
    requirements: Sequence[CoverageRequirement],
    findings: Iterable[CoverageFinding],
    *,
    approved_exemption_ids: Iterable[str] = (),
) -> tuple[str, list[dict[str, Any]]]:
    """汇总覆盖判定：返回 (``pass`` | ``fail`` | ``blocked``, 阻断项列表)。

    - ``absent`` / ``partial`` → fail；
    - ``uncertain`` 或**缺少判定**（漏检）→ blocked；
    - ``exempted`` 仅在精确指向该覆盖项 ID 的已批准决策存在时通过，否则 fail；
    - 未知状态视为 uncertain（fail-closed）。
    """
    approved = {str(item) for item in approved_exemption_ids}
    by_id: dict[str, CoverageFinding] = {}
    for finding in findings or ():
        by_id[str(finding.requirement_id)] = finding

    blocking: list[dict[str, Any]] = []
    blocked_seen = False
    failed_seen = False
    for requirement in requirements:
        finding = by_id.get(requirement.id)
        status = str(finding.status) if finding is not None else "uncertain"
        if finding is None:
            evidence = "未产出覆盖判定（漏检），按 uncertain 处理"
        else:
            evidence = finding.evidence
        if status == "present":
            continue
        if status == "exempted":
            if requirement.id in approved:
                continue
            failed_seen = True
            blocking.append(
                {
                    "requirement_id": requirement.id,
                    "status": "exempted",
                    "evidence": f"exempted 缺少精确指向该覆盖项（{requirement.id}）的已批准豁免决策",
                }
            )
            continue
        if status in FAIL_STATUSES:
            failed_seen = True
        else:
            blocked_seen = True
        blocking.append(
            {
                "requirement_id": requirement.id,
                "status": status,
                "source_text": requirement.source_text,
                "manual_review_priority": requirement.manual_review_priority,
                "evidence": evidence,
            }
        )

    if failed_seen:
        return "fail", blocking
    if blocked_seen:
        return "blocked", blocking
    return "pass", []
