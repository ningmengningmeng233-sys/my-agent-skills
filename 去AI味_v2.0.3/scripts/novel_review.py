#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""novel_review.py — L2/L3 分层全书复核的 **Python 校验面**（v1.2 Task 11）。

依据：``文档/柠檬/设计/2026-09-28-去AI味v1.2-设计方案.md`` 59–72 行「分段改写与全书复查」
「四层验证合同」、72 行（``PASS_MECHANICAL`` 只覆盖 L0/L1，发布还要求 L2/L3）、
31 行（脚本不得假装理解人物关系、伏笔或声线）。

分工（规范原文）：**模型生成 L2 段级和 L3 全书报告；Python 校验段覆盖、锚点、
账本哈希和枚举。没有证据不得自动 PASS。** 本模块只做确定性判断，不调模型、
不读写文件、不执行任何来自正文的指令——正文里的「系统消息」「工具命令」「忽略前文」
只是待编辑材料（risk N13），连 :func:`injection_markers` 也只登记事实、不改变判定。
:data:`REFUSED_JUDGEMENTS` 列出本模块明确拒绝的判断。

消费面（全部是冻结 API，只调用不修改）：
  * Task 7 ``Segment``：坐标口径为解码 ``str`` 上的码点半开区间；``needs_review``
    的降级段**永不被读成已复核干净**（:data:`DEGRADED_CODES` 路由进 REVIEW）。
  * Task 8 ``novel_contracts``：``SourceAnchor`` / ``ContractIssue`` /
    ``ContractError``（每个 raise 点带 details）/ ``validate_ledger``——账本复核码
    （``anchor_coordinates_stale``、``anchor_hash_mismatch``、``anchor_excerpt_not_found``、
    ``degraded_segment_anchor``、``indistinguishable_dialogue_citation``、
    ``plot_sensitive_*_needs_second_review`` 等）原样透传，聚合不洗成 PASS。
  * Task 10 ``novel_validation``：``ValidationResult`` 状态语义与
    ``mechanical_verdict`` 的 ``PASS_MECHANICAL``；本模块的
    :func:`publication_decision` 收到机械结果对象做四层合并，机械通过**永远**
    不等于可以发布。说话人候选索引复用其 ``build_index`` 的引号归属（机械线索，
    不是裁决）。

判定口径（与 Task 8/10 一致）：``rejected`` 是证据造假/非法枚举/哈希不符这类
不可发布判定（FAIL）；``review`` 是「证据不足或不可信，需要补做」（REVIEW）；
两处不重复登记；``issues`` 顺序按输入位置，重跑逐字段相同。

四层推进（章节→相邻章节→卷→全书）：:data:`REVIEW_LAYERS` +
:func:`review_layer_for` 按锚点跨章形状给 finding 归层；全书层用
:func:`plan_whole_book_review` 通过索引定位可疑窗口（跨章同型句式、跨章账本声明），
再由 :func:`reread_window_texts` 交出**原文与成稿的真实窗口文本**供模型重读——
窗口逐段独立、限宽 :data:`MAX_WINDOW_CHARS`，不存在「整本书一次塞进上下文」的形状
（spec 63 行）。报告必须**逐窗交出 :class:`WindowText` 产物**（``reread``），否则
``whole_book_window_unreread`` 进 REVIEW：窗口 ID 字符串、``Window`` 对象、finding
锚点与窗口重叠都**不是**重读证据（``bad_reread_evidence``）；产物里的文本回读不到
对应分段，按证据造假处理（``reread_evidence_not_derivable`` → FAIL）。复核计划本身
由 ``validate_review`` 用手上的 ``segments`` + ``ledger`` 实算，调用方省略它或给一份
空计划都不能关掉这道闸（省略 ≠ 没有可疑窗口；与实算不符 → ``whole_book_plan_mismatch``）。

「不是 X，而是 Y」族（spec 63 行 / risk N6）：:func:`negation_hits` 把
「不是/并非/并不是/不在于/与其说/看似/貌似/你以为 … 而是/是/在于/而在于/不如说/
实则/其实/却是」变体归一到同一族 ``not-X-but-Y``，全书一遍统计分布与 claim id；
:func:`build_full_book_index` 的分布**来自全部分段一次计算**，不是逐章各数一次。
保留与修改由模型按 ``references/fiction-zh.md`` §4 的固定五问定性（①②③ 任一成立即优先
保留，只有 ④⑤ 成立才允许改；同一位置的分类不随力度变；Python 不猜）。

隐私红线（Task 6 同源）：索引只存锚点坐标与短摘要（标题、陈述、摘录各设上限），
**绝不携带整段正文**；finding.explanation 有 :data:`MAX_EXPLANATION_CHARS` 上限，
超限即构造失败。

本模块可能给出的全部判定码（固定清单，与文档一一对应）：

  校验器码：bad_report bad_finding bad_anchor bad_segments duplicate_segment_id
  bad_window bad_text
  unknown_segment anchor_out_of_range anchor_hash_mismatch anchor_excerpt_not_found
  anchor_occurrence_out_of_range anchor_coordinates_stale degraded_segment_anchor
  degraded_segment_needs_review review_level_missing segment_not_reviewed
  report_unknown_segment duplicate_segment_claim ledger_evidence_missing
  ledger_sha256_mismatch unknown_finding_code duplicate_finding_id
  single_anchor_l3_finding whole_book_window_unreread whole_book_plan_mismatch
  bad_reread_evidence reread_evidence_not_derivable awaiting_l2_l3_review
  模型 finding 码（:data:`MODEL_FINDING_CODES`）：character_address_drift
  viewpoint_drift timeline_drift world_rule_break character_state_drift
  foreshadow_state_drift new_event_introduced cross_chapter_template_residue
  chapter_incomplete dialogue_indistinct negation_pattern_empty_lift
  segment_semantic_drift protected_meaning_changed hedge_certainty_drift
  账本透传码：见 ``novel_contracts.ALL_CODES``（本模块不重定义它们的语义）。

``python -B -X utf8 scripts/novel_review.py`` 是内置自检：正反面各走一遍，
任何断言失败以非零码退出。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, is_dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from novel_contracts import (ALL_CODES as _CONTRACT_ALL_CODES, ContractError,
                             ContractIssue, SourceAnchor, validate_ledger)
from novel_segments import Segment
import novel_validation as _nv

# ---------------------------------------------------------------- 合同字面量

REVIEW_LEVELS: tuple[str, ...] = ("L2", "L3")
COMPLETABLE_LEVELS: tuple[str, ...] = ("L0", "L1", "L2", "L3")
REQUIRED_REPORT_LEVELS: tuple[str, ...] = ("L2", "L3")
FINDING_SEVERITIES: tuple[str, ...] = ("review", "fail")
DECISION_STATUSES: tuple[str, ...] = ("PASS", "REVIEW", "FAIL", "ERROR", "NO-OP")
#: 复核推进顺序（spec 63 行）：章节 → 相邻章节 → 卷 → 全书。
REVIEW_LAYERS: tuple[str, ...] = ("chapter", "adjacent_chapter", "volume", "whole_book")

#: 模型可以申报的 finding 码；表外一律按非法枚举拒绝（没有证据不得自动 PASS 的
#: 另一半：证据必须落在可核对的枚举上）。
MODEL_FINDING_CODES: frozenset = frozenset({
    "character_address_drift", "viewpoint_drift", "timeline_drift",
    "world_rule_break", "character_state_drift", "foreshadow_state_drift",
    "new_event_introduced", "cross_chapter_template_residue", "chapter_incomplete",
    "dialogue_indistinct", "negation_pattern_empty_lift", "segment_semantic_drift",
    "protected_meaning_changed", "hedge_certainty_drift",
    # v2.0.0（alpha.2 P08）：v2 分项判定结论经 v2_judge.finding_fields 映射进本枚举，
    # 复用同一套 finding/report 校验，不另起第二个复核合同。
    "v2-segmented-judgment",
})

# ---------------------------------------------------------------- FP 证据合同字面量
# FP 专项迭代（设计：文档/柠檬/设计/2026-09-29-小说伪文艺腔专项迭代-设计方案.md）：
# Python 只校验 schema、rule ID、枚举、锚点、聚集计数与三档 decision 一致性；
# FP01–FP08 哪些表达该删该留由模型按 references/fiction-prose-patterns-zh.md 申报，
# 本模块不判断文学质量，也不从文本推断人物声线、视角或叙事功能。

#: FP 规则编号枚举（FP01–FP08）。
FP_RULE_IDS: tuple[str, ...] = tuple(f"FP{index:02d}" for index in range(1, 9))
#: FP 的 decision 枚举；同一原文位置三档必须同 decision，只有 operation 随力度变。
FP_DECISIONS: tuple[str, ...] = ("preserve", "rewrite-local", "rewrite-needs-structure",
                                 "insufficient-evidence", "protected")
#: 证据覆盖范围；聚集型规则不得申报 single。
FP_SCOPES: tuple[str, ...] = ("single", "segment", "chapter", "whole-book")
#: 三档力度（与 SKILL.md §4 的 in-place/balanced/rebuild 一一对应）。
FP_STRENGTHS: tuple[str, ...] = ("conservative", "balanced", "deep")
#: 必须跨段/跨人物/跨章聚集触发的规则；其余三条单处可触发。
CLUSTER_FP_RULES: frozenset = frozenset({"FP02", "FP03", "FP04", "FP06", "FP07"})
SINGLE_FP_RULES: frozenset = frozenset({"FP01", "FP05", "FP08"})
#: operation 枚举与各力度的权限（设计方案「三档力度」：operation 必须属于该力度权限）。
FP_OPERATIONS: tuple[str, ...] = ("delete", "compress", "recompose-sentence",
                                  "merge-duplicates", "recompose-paragraph",
                                  "reorder-paragraphs", "preserve", "no-op", "review")
FP_STRENGTH_OPERATIONS: dict = {
    "conservative": frozenset({"delete", "compress", "preserve", "no-op", "review"}),
    "balanced": frozenset({"delete", "compress", "recompose-sentence",
                           "merge-duplicates", "preserve", "no-op", "review"}),
    "deep": frozenset(FP_OPERATIONS),
}
MAX_NARRATIVE_FUNCTION_CHARS = 120
MAX_PRESERVE_REASON_CHARS = 200
MAX_POSITIONS_ENTRIES = 16
MAX_POSITION_CHARS = 40
MAX_OPERATION_CHARS = 40

#: 本模块校验器自身的判定码（与 Task 8 同名的码语义完全对齐）。
VALIDATOR_CODES: frozenset = frozenset({
    "bad_report", "bad_finding", "bad_anchor", "bad_segments", "duplicate_segment_id",
    "bad_window", "bad_text",
    "unknown_segment", "anchor_out_of_range", "anchor_hash_mismatch",
    "anchor_excerpt_not_found", "anchor_occurrence_out_of_range",
    "anchor_coordinates_stale", "degraded_segment_anchor",
    "degraded_segment_needs_review", "review_level_missing", "segment_not_reviewed",
    "report_unknown_segment", "duplicate_segment_claim", "ledger_evidence_missing",
    "ledger_sha256_mismatch", "unknown_finding_code", "duplicate_finding_id",
    "single_anchor_l3_finding", "whole_book_window_unreread", "whole_book_plan_mismatch",
    "bad_reread_evidence", "reread_evidence_not_derivable",
    "awaiting_l2_l3_review",
    # FP 证据形状码（全部 REVIEW：证据不足或形状不符时补做，不自动 FAIL）。
    "fp_evidence_payload_missing", "fp_payload_code_mismatch",
    "fp_payload_on_non_fp_finding", "fp_cluster_evidence_insufficient",
    "fp_operation_outside_strength", "fp_preserve_reason_missing",
    "fp_cross_strength_decision_drift", "fp_cross_strength_evidence_missing",
})

ALL_REVIEW_CODES: tuple[str, ...] = tuple(sorted(
    MODEL_FINDING_CODES | VALIDATOR_CODES | set(FP_RULE_IDS)
    | set(_CONTRACT_ALL_CODES)))

#: Python 明确拒绝的判断（spec 31 行）——写进代码就等于假装看得懂。
REFUSED_JUDGEMENTS: tuple[str, ...] = (
    "人物关系是否正确、称谓是否漂移（由模型产出 L2/L3 finding，Python 只校锚点与枚举）",
    "伏笔是否回收、伏笔状态是否变化（归 L3 模型复核；本模块只核实 finding 证据形状）",
    "人物声线与叙述距离是否等价（归 L3；说话人索引只是机械归属候选，不是裁决）",
    "「不是 X，而是 Y」该保留还是该改（按 fiction-zh.md §4 的固定五问定性，"
    "由模型决定；同一位置三档必须得到同一分类，Python 只给全书规范化分布）",
    "FP01–FP08 哪处该删该留、叙事功能是否成立（模型按 fiction-prose-patterns-zh.md "
    "申报；Python 只校验证据形状、聚集计数与三档 decision 一致性，不从文本推断）",
    "整本书一次进上下文式的全局判断（只按索引定位可疑窗口、交原文与成稿窗口重读）",
)

MAX_EXPLANATION_CHARS = 200
MAX_ID_CHARS = 128
MAX_INDEX_SUMMARY_CHARS = 120
MAX_INDEX_EXCERPT_CHARS = 40
#: 可疑窗口限宽：全书层的重读单位，远小于任何「整本进上下文」的想象。
MAX_WINDOW_CHARS = 1200
DEFAULT_CONTEXT_CHARS = 300
DEFAULT_VOLUME_CHAPTERS = 10
#: 同型句式在 ≥2 个章节出现即进全书层可疑窗口（逐章各看一次永远看不到这种密集）。
DEFAULT_MIN_CHAPTERS = 2

_ID_RE = re.compile(rf"^[^\s\x00-\x1f\x7f]{{1,{MAX_ID_CHARS}}}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")

#: 「不是 X，而是 Y」族的规范化：变体再花，分布统计只认一个族。
_NEGATION_RE = re.compile(
    r"(并不是|不是|并非|不在于|与其说|看似|貌似|你以为)"
    r"[^。！？；\n]{0,30}?"
    r"(而是|而在于|不如说|实则|其实|却是|在于|是)")
NEGATION_FAMILY = "not-X-but-Y"

#: 注入形状（risk N13）：只用于**登记材料**，从不改变判定，也从不触发任何执行。
INJECTION_MARKERS: tuple[str, ...] = (
    "忽略前文", "忽略以上", "ignore previous instructions", "disregard the above",
    "你现在是助手", "系统消息", "system:", "工具命令", "执行以下命令",
)

TRACKED_PUNCTUATION = "，。、；：！？…—《》“”‘’「」『』（）()【】"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_id(value) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _clip(value: str, cap: int) -> str:
    return value if len(value) <= cap else value[:cap - 1] + "…"


def _as_mapping(value, cls_name: str, index):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise ContractError("bad_finding", f"{cls_name} 第 {index} 项必须是 {cls_name} "
                                      f"或映射，收到 {type(value).__name__}",
                        cls_name=cls_name, index=index,
                        element_type=type(value).__name__)


def anchor_from(value) -> SourceAnchor:
    """模型 JSON 锚点 → ``SourceAnchor``；形状不合法直接 ValueError（枚举闸门）。"""
    data = _as_mapping(value, "SourceAnchor", -1)
    missing = [name for name in ("segment_id", "start", "end", "excerpt",
                                 "text_sha256", "occurrence_index")
               if name not in data]
    if missing:
        raise ValueError(f"锚点缺字段: {','.join(missing)}")
    extra = sorted(set(data) - {"segment_id", "start", "end", "excerpt",
                                "text_sha256", "occurrence_index"})
    if extra:
        raise ValueError(f"锚点含未知字段: {','.join(extra)}")
    if not _is_id(data["segment_id"]):
        raise ValueError("锚点 segment_id 必须是非空、无空白与控制字符、≤128 字符")
    for name in ("start", "end", "occurrence_index"):
        if not isinstance(data[name], int) or isinstance(data[name], bool):
            raise ValueError(f"锚点 {name} 必须是整数")
    if data["occurrence_index"] < 0:
        raise ValueError("锚点出现序号不能为负")
    if not isinstance(data["excerpt"], str) or not data["excerpt"]:
        raise ValueError("锚点片段不能为空")
    if not isinstance(data["text_sha256"], str) or not _HEX_RE.match(data["text_sha256"]):
        raise ValueError("锚点片段哈希必须是 64 位十六进制 SHA-256")
    return SourceAnchor(segment_id=data["segment_id"], start=data["start"],
                        end=data["end"], excerpt=data["excerpt"],
                        text_sha256=data["text_sha256"].lower(),
                        occurrence_index=data["occurrence_index"])


# ---------------------------------------------------------------- brief 规范的三个 dataclass


@dataclass(frozen=True)
class FictionPatternEvidence:
    """FP01–FP08 的结构化证据载荷（模型申报，Python 只校验形状）。

    ``decision`` 不随力度变化；``operation`` 必须属于 :data:`FP_STRENGTH_OPERATIONS`
    里该力度的权限。聚集型规则（FP02/03/04/06/07）必须提供跨段/人物/章节分布证据，
    缺失时只能 REVIEW——Python 不判断分布背后的文学语义。
    """

    rule_id: str
    scope: str
    occurrences: int
    character_count: int
    positions: tuple[str, ...]
    narrative_function: str
    decision: str
    preserve_reason: str | None
    strength: str
    operation: str

    def __post_init__(self) -> None:
        if self.rule_id not in FP_RULE_IDS:
            raise ValueError(f"rule_id 必须在 FP 枚举里，收到 {self.rule_id!r}")
        if self.scope not in FP_SCOPES:
            raise ValueError(f"scope 只能是 {'/'.join(FP_SCOPES)}，收到 {self.scope!r}")
        for name in ("occurrences", "character_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} 必须是非负整数，收到 {value!r}")
        positions = tuple(self.positions or ())
        if len(positions) > MAX_POSITIONS_ENTRIES:
            raise ValueError(f"positions 超过 {MAX_POSITIONS_ENTRIES} 项")
        for pos in positions:
            if not isinstance(pos, str) or not pos.strip() or len(pos) > MAX_POSITION_CHARS:
                raise ValueError("positions 每项必须是非空、≤%d 字符的字符串"
                                 % MAX_POSITION_CHARS)
        object.__setattr__(self, "positions", positions)
        if (not isinstance(self.narrative_function, str)
                or not self.narrative_function.strip()
                or len(self.narrative_function) > MAX_NARRATIVE_FUNCTION_CHARS):
            raise ValueError(f"narrative_function 必须是非空、≤{MAX_NARRATIVE_FUNCTION_CHARS}"
                             " 字符的短说明（模型申报，Python 不推断）")
        if self.decision not in FP_DECISIONS:
            raise ValueError(f"decision 只能是 {'/'.join(FP_DECISIONS)}，"
                             f"收到 {self.decision!r}")
        if self.preserve_reason is not None:
            if (not isinstance(self.preserve_reason, str)
                    or not self.preserve_reason.strip()
                    or len(self.preserve_reason) > MAX_PRESERVE_REASON_CHARS):
                raise ValueError("preserve_reason 必须是非空、≤%d 字符的短说明或 None"
                                 % MAX_PRESERVE_REASON_CHARS)
        if self.strength not in FP_STRENGTHS:
            raise ValueError(f"strength 只能是 {'/'.join(FP_STRENGTHS)}，"
                             f"收到 {self.strength!r}")
        if (not isinstance(self.operation, str) or self.operation not in FP_OPERATIONS
                or len(self.operation) > MAX_OPERATION_CHARS):
            raise ValueError(f"operation 必须在枚举 {'/'.join(FP_OPERATIONS)} 里")


@dataclass(frozen=True)
class ReviewFinding:
    """模型的一条复核结论。锚点非空、枚举合法、说明只能是短摘要（隐私红线）。"""

    finding_id: str
    level: Literal["L2", "L3"]
    severity: Literal["review", "fail"]
    code: str
    anchors: tuple[SourceAnchor, ...]
    explanation: str
    pattern_evidence: "FictionPatternEvidence | None" = None

    def __post_init__(self) -> None:
        if not _is_id(self.finding_id):
            raise ValueError("finding_id 必须是非空、无空白与控制字符、≤128 字符")
        if self.level not in REVIEW_LEVELS:
            raise ValueError(f"level 只能是 {'/'.join(REVIEW_LEVELS)}，收到 {self.level!r}")
        if self.severity not in FINDING_SEVERITIES:
            raise ValueError(f"severity 只能是 {'/'.join(FINDING_SEVERITIES)}，"
                             f"收到 {self.severity!r}")
        if not isinstance(self.code, str) or not _ID_RE.match(self.code.replace("-", "x")):
            raise ValueError(f"code 必须是非空短标识，收到 {self.code!r}")
        if isinstance(self.anchors, (str, bytes)) or not isinstance(
                self.anchors, (tuple, list)):
            raise ValueError("anchors 必须是 SourceAnchor 序列")
        if not self.anchors:
            raise ValueError("finding 至少要有一个锚点——没有证据就没有结论")
        object.__setattr__(self, "anchors", tuple(anchor_from(a) for a in self.anchors))
        if not isinstance(self.explanation, str) or not self.explanation.strip():
            raise ValueError("explanation 必须是非空短说明")
        if len(self.explanation) > MAX_EXPLANATION_CHARS:
            raise ValueError(f"explanation 超过 {MAX_EXPLANATION_CHARS} 字符——"
                             "日志不得复制整段私人小说")
        evidence = self.pattern_evidence
        if evidence is not None and not isinstance(evidence, FictionPatternEvidence):
            if isinstance(evidence, Mapping):
                evidence = FictionPatternEvidence(**evidence)
            else:
                raise ValueError("pattern_evidence 必须是 FictionPatternEvidence、"
                                 "映射或 None")
        object.__setattr__(self, "pattern_evidence", evidence)


@dataclass(frozen=True)
class ReviewReport:
    """模型报告：段覆盖 + 完成层级 + findings + 账本哈希（字段即合同）。"""

    segment_ids: tuple[str, ...]
    completed_levels: tuple[Literal["L0", "L1", "L2", "L3"], ...]
    findings: tuple[ReviewFinding, ...]
    ledger_sha256: str

    def __post_init__(self) -> None:
        ids = tuple(self.segment_ids)
        for sid in ids:
            if not _is_id(sid):
                raise ValueError(f"segment_ids 含非法 ID: {sid!r}")
        object.__setattr__(self, "segment_ids", ids)
        levels = tuple(self.completed_levels)
        for level in levels:
            if level not in COMPLETABLE_LEVELS:
                raise ValueError(f"completed_levels 含非法层级: {level!r}，"
                                 f"合法值 {COMPLETABLE_LEVELS}")
        if len(set(levels)) != len(levels):
            raise ValueError(f"completed_levels 重复: {levels}")
        object.__setattr__(self, "completed_levels", levels)
        built = []
        for index, item in enumerate(self.findings):
            if isinstance(item, ReviewFinding):
                built.append(item)
                continue
            data = _as_mapping(item, "ReviewFinding", index)
            extra = sorted(set(data) - {"finding_id", "level", "severity", "code",
                                         "anchors", "explanation", "pattern_evidence"})
            if extra:
                raise ValueError(f"finding 第 {index} 项含未知字段: {','.join(extra)}")
            built.append(ReviewFinding(**data))
        object.__setattr__(self, "findings", tuple(built))
        if not isinstance(self.ledger_sha256, str) or not _HEX_RE.match(self.ledger_sha256):
            raise ValueError("ledger_sha256 必须是 64 位十六进制 SHA-256")
        object.__setattr__(self, "ledger_sha256", self.ledger_sha256.lower())


@dataclass(frozen=True)
class PublicationDecision:
    """发布判定：状态枚举与 Task 10 对齐；codes 是全部阻断原因的并集（排序去重）。"""

    status: Literal["PASS", "REVIEW", "FAIL", "ERROR", "NO-OP"]
    codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in DECISION_STATUSES:
            raise ValueError(f"status 非法: {self.status!r}，合法值 {DECISION_STATUSES}")
        if isinstance(self.codes, (str, bytes)) or not isinstance(
                self.codes, (tuple, list)):
            raise ValueError("codes 必须是字符串元组")
        object.__setattr__(self, "codes", tuple(str(c) for c in self.codes))


# ---------------------------------------------------------------- 复核校验结果


@dataclass(frozen=True)
class ReviewValidation:
    """Python 对一份模型报告的全部判定。``issues``/``rejected`` 不重复登记。"""

    accepted: tuple = ()     # 通过全部校验的 findings（含被降级的 L3 单锚 finding 原对象）
    review: tuple = ()       # ContractIssue：证据不足/需补做（REVIEW）
    rejected: tuple = ()     # ContractIssue：证据造假/非法枚举/哈希不符（FAIL）
    metrics: Mapping = field(default_factory=dict)

    @property
    def codes(self) -> tuple:
        return tuple(sorted({i.code for i in self.review} |
                            {i.code for i in self.rejected}))

    @property
    def status(self) -> str:
        if self.rejected:
            return "FAIL"
        if self.review:
            return "REVIEW"
        return "PASS"


# ---------------------------------------------------------------- 账本摘要


def _entry_dict(entry) -> dict:
    data = asdict(entry) if is_dataclass(entry) and not isinstance(entry, type) else None
    if data is None:
        if not isinstance(entry, Mapping):
            raise ContractError("bad_entries", "账本条目必须是 LedgerEntry 或映射",
                                kind=type(entry).__name__)
        data = dict(entry)
    return {key: (_anchor_plain(value) if key in ("output_anchor",) else
                  tuple(_anchor_plain(v) for v in value)
                  if isinstance(value, (tuple, list)) and key == "source_anchors"
                  else value)
            for key, value in sorted(data.items())}


def _anchor_plain(anchor) -> dict:
    data = asdict(anchor) if is_dataclass(anchor) else dict(anchor)
    return {key: data[key] for key in sorted(data)}


def ledger_digest(entries) -> str:
    """账本的规范化 SHA-256：**与条目顺序完全无关**，内容变则哈希变。

    排序键是「claim_id + 条目自身的规范化 JSON」，不是光 claim_id——重复 claim_id
    （Task 8 会判 ``duplicate_claim_id``）在摘要这一层也不得让顺序影响结果，
    否则同一份账本两次登记会算出两个哈希，发布证据就不可重算。
    重复判定归 Task 8 的 :func:`novel_contracts.validate_ledger`，本函数不做第二份。
    """
    if not isinstance(entries, (tuple, list)):
        raise ContractError("bad_entries", "ledger 必须是账本条目序列",
                            kind=type(entries).__name__)
    normalized = []
    for entry in entries:
        data = _entry_dict(entry)
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
        normalized.append((str(data.get("claim_id", "")), payload, data))
    ordered = [item[2] for item in sorted(normalized, key=lambda item: (item[0], item[1]))]
    return sha256_text(json.dumps(ordered, ensure_ascii=False, sort_keys=True,
                                  default=str))


# ---------------------------------------------------------------- 分段索引与锚点解析


def _index_segments(segments) -> dict:
    if not isinstance(segments, (tuple, list)):
        raise ContractError("bad_segments", "segments 必须是 Segment 序列",
                            kind=type(segments).__name__)
    by_id: dict = {}
    for segment in segments:
        if not isinstance(segment, Segment):
            raise ContractError("bad_segments",
                                f"segments 含非 Segment 元素: {type(segment).__name__}",
                                element_type=type(segment).__name__)
        if segment.start < 0 or segment.end - segment.start != len(segment.text):
            raise ContractError("bad_segments",
                                f"段 {segment.segment_id} 的坐标与文本长度不一致",
                                segment_id=segment.segment_id, start=segment.start,
                                end=segment.end, length=len(segment.text))
        if segment.segment_id in by_id:
            raise ContractError("duplicate_segment_id",
                                f"重复 segment_id: {segment.segment_id}",
                                segment_id=segment.segment_id)
        by_id[segment.segment_id] = segment
    return by_id


def _occurrences(haystack: str, needle: str) -> list:
    positions = []
    start = haystack.find(needle)
    while start != -1:
        positions.append(start)
        start = haystack.find(needle, start + len(needle))
    return positions


def _resolve_anchor(anchor: SourceAnchor, segment) -> tuple[list, list]:
    """返回 (拒绝码, 复核码)；口径与 Task 8 `_validate_anchor` 逐码对齐。"""
    hard, notes = [], []
    if not (segment.start <= anchor.start < anchor.end <= segment.end):
        hard.append(("anchor_out_of_range",
                     f"锚点坐标 ({anchor.start}, {anchor.end}) 不在分段 "
                     f"{segment.segment_id} [{segment.start}, {segment.end}) 内"))
        return hard, notes
    if sha256_text(anchor.excerpt) != anchor.text_sha256:
        hard.append(("anchor_hash_mismatch",
                     f"锚点片段哈希与片段内容不符（分段 {segment.segment_id}）"))
    positions = _occurrences(segment.text, anchor.excerpt)
    if not positions:
        hard.append(("anchor_excerpt_not_found",
                     f"锚点片段在分段 {segment.segment_id} 里找不到，结论无来源"))
        return hard, notes
    if anchor.occurrence_index >= len(positions):
        hard.append(("anchor_occurrence_out_of_range",
                     f"锚点出现序号 {anchor.occurrence_index} 超出该片段在分段 "
                     f"{segment.segment_id} 内的出现次数 {len(positions)}"))
        return hard, notes
    located = segment.start + positions[anchor.occurrence_index]
    if (anchor.start, anchor.end) != (located, located + len(anchor.excerpt)):
        notes.append(("anchor_coordinates_stale",
                      "锚点坐标与片段实际定位不符；坐标不修复，交人工/模型复核"))
    if segment.needs_review:
        notes.append(("degraded_segment_anchor",
                      f"分段 {segment.segment_id} 带 needs_review 旗标（Task 7 按单元"
                      "降级），不能当作干净锚点来源使用"))
    return hard, notes


# ---------------------------------------------------------------- 四层推进与窗口


def review_layer_for(anchors, segments, *,
                     volume_chapters: int = DEFAULT_VOLUME_CHAPTERS) -> str:
    """按锚点跨章形状给结论归层：章节 → 相邻章节 → 卷 → 全书。"""
    by_id = {s.segment_id: s for s in segments}
    chapters = sorted({by_id[a.segment_id].chapter_index for a in anchors
                       if a.segment_id in by_id})
    if not chapters:
        return "whole_book"
    low, high = chapters[0], chapters[-1]
    if low == high:
        return "chapter"
    if high - low == 1:
        return "adjacent_chapter"
    if low // volume_chapters == high // volume_chapters:
        return "volume"
    return "whole_book"


@dataclass(frozen=True)
class Window:
    """全书层的一个可疑窗口：段内坐标 + 定位它的全书 claim。"""

    window_id: str
    segment_id: str
    start: int
    end: int
    layer: str
    reason: str


@dataclass(frozen=True)
class ReviewPlan:
    windows: tuple = ()
    #: 结构性的「我没有把整本书当一次上下文」声明：复核只能逐窗重读。
    single_context: bool = False


@dataclass(frozen=True)
class WindowText:
    """全书层一次重读的证据**产物**：:func:`validate_review` 只认这个类型。

    ``source_text``/``output_text`` 必须是 :func:`reread_window_texts` 从真实分段
    切出来的窗口文本（可被同一分段重算），不是窗口名、不是摘要、不是模型转述。
    """

    window_id: str
    source_text: str
    output_text: str
    truncated: bool = False


def _window_from_hit(segment_id: str, start: int, end: int, reason: str) -> Window:
    end = min(end, start + MAX_WINDOW_CHARS)
    return Window(window_id=f"whole_book:{segment_id}:{start}-{end}",
                  segment_id=segment_id, start=start, end=end,
                  layer="whole_book", reason=reason)


def plan_whole_book_review(index, *, min_chapters: int = DEFAULT_MIN_CHAPTERS) -> ReviewPlan:
    """全书层：通过索引定位可疑窗口（跨章同型句式、跨章账本声明），不做整本判断。"""
    segment_chapter = {}
    for chapter in index.chapters:
        for sid in chapter.segment_ids:
            segment_chapter[sid] = chapter.chapter_index
    windows: list = []
    seen: set = set()

    def add(window: Window):
        if window.window_id not in seen:
            seen.add(window.window_id)
            windows.append(window)

    for claim in index.patterns:
        chapters_hit = [ch for ch, count in claim.per_chapter if count > 0]
        if len(chapters_hit) >= min_chapters and claim.total >= min_chapters:
            for hit in claim.hits:
                add(_window_from_hit(hit.segment_id, hit.start, hit.end, claim.claim_id))
    for group in (index.characters, index.timeline, index.world_rules, index.viewpoints):
        for entry in group:
            chapters = {segment_chapter.get(a.segment_id) for a in entry.anchors}
            chapters.discard(None)
            if len(chapters) >= min_chapters:
                for a in entry.anchors:
                    add(_window_from_hit(a.segment_id, a.start, a.end, entry.claim_id))
    return ReviewPlan(windows=tuple(windows))


def _segment_window(window: Window, segments):
    for segment in segments:
        if segment.segment_id == window.segment_id:
            return segment
    return None


def reread_window_texts(window: Window, source_segments, output_segments=(),
                        *, context_chars: int = DEFAULT_CONTEXT_CHARS) -> WindowText:
    """交出窗口的**原文与成稿真实文本**——全书层复核靠逐窗重读，不靠记忆。"""
    if not isinstance(window, Window):
        raise ContractError("bad_window", "window 必须是 Window",
                            kind=type(window).__name__)
    source = _segment_window(window, source_segments)
    if source is None:
        raise ContractError("unknown_segment", "窗口指向不存在的分段",
                            segment_id=window.segment_id, window_id=window.window_id)
    local_start = max(0, window.start - source.start - context_chars)
    local_end = min(len(source.text), window.end - source.start + context_chars)
    source_text = source.text[local_start:local_end]
    output_text = ""
    if output_segments:
        out = _segment_window(window, output_segments)
        if out is not None:
            output_text = out.text[min(local_start, len(out.text)):
                                   min(local_end, len(out.text))]
    return WindowText(window_id=window.window_id, source_text=source_text,
                      output_text=output_text,
                      truncated=local_end - local_start < (window.end - window.start)
                      + 2 * context_chars)


def derived_plan(segments, ledger=()) -> ReviewPlan:
    """按手上的分段与账本实算全书复核计划——这道闸**不能**靠省略参数关掉。"""
    return plan_whole_book_review(build_full_book_index(segments=segments, ledger=ledger))


def _reread_evidence(reread, windows_by_id, by_id, out_by_id):
    """只认 :func:`reread_window_texts` 的产物：返回 (覆盖窗口, 复核项, 拒绝项)。

    三类伪证据一律不给覆盖：窗口 ID 字符串、``Window`` 对象、只读原文不读成稿
    （→ ``bad_reread_evidence``）；文本回算不到所声称的分段 → ``reread_evidence_not_derivable``
    （证据造假，与锚点片段回读不到同一口径，走 FAIL）。
    """
    covered: set = set()
    notes: list = []
    hard: list = []
    for position, item in enumerate(reread):
        window_id = getattr(item, "window_id", None)
        if not isinstance(item, WindowText) or not isinstance(window_id, str):
            notes.append((position, _clip(str(window_id or f"reread[{position}]"),
                                          MAX_ID_CHARS),
                          "bad_reread_evidence",
                          "重读证据必须是 reread_window_texts 交出的 WindowText；"
                          "窗口 ID 与 Window 形状只是坐标，不含原文与成稿"))
            continue
        window = windows_by_id.get(window_id)
        if window is None:
            notes.append((position, window_id, "bad_reread_evidence",
                          "重读证据指向的窗口不在本轮按数据实算的复核计划里"))
            continue
        if not isinstance(item.source_text, str) or not item.source_text.strip():
            notes.append((position, window_id, "bad_reread_evidence",
                          "重读证据没有原文文本，窗口内容没有被读过"))
            continue
        source = by_id.get(window.segment_id)
        if source is None or item.source_text not in source.text:
            hard.append((position, window_id, "reread_evidence_not_derivable",
                         f"声称的原文窗口回算不到分段 {window.segment_id} 的正文"))
            continue
        if not isinstance(item.output_text, str) or not item.output_text.strip():
            notes.append((position, window_id, "bad_reread_evidence",
                          "只读了原文：spec 63 行要求原文与成稿窗口都重读"))
            continue
        if out_by_id:
            out = out_by_id.get(window.segment_id)
            if out is None or item.output_text not in out.text:
                hard.append((position, window_id, "reread_evidence_not_derivable",
                             f"声称的成稿窗口回算不到输出分段 {window.segment_id}"))
                continue
        covered.add(window_id)
    return covered, notes, hard


def injection_markers(text: str) -> tuple:
    """登记正文里的注入形状——只说明「这是材料」，不改变任何判定。"""
    if not isinstance(text, str):
        raise ContractError("bad_text", "injection_markers 只吃解码后的 str",
                            kind=type(text).__name__)
    return tuple(sorted(m for m in INJECTION_MARKERS if m in text))


# ---------------------------------------------------------------- FP 证据校验


def _anchor_identity(anchor: SourceAnchor) -> tuple:
    """锚点的原文位置身份：只认 (segment_id, start, end)——工单 Task 4 钉死的三元组。

    同坐标配不同 occurrence 序号在几何上自相矛盾（第 N 次出现的坐标必然不同），
    不得当成第二个不同锚点去满足聚集门；坐标与序号不符仍由
    :func:`validate_review` 的 ``anchor_coordinates_stale`` 单独压 REVIEW。
    """
    return (anchor.segment_id, anchor.start, anchor.end)


def validate_fiction_pattern_evidence(finding: ReviewFinding, *,
                                      segments) -> tuple:
    """只校验 FP 证据的 schema、锚点关系、聚集门与力度权限，不判断文学语义。

    返回的全部是 REVIEW 级 ContractIssue（证据不足或形状不符，需模型补做）：
    证据造假（锚点回读不到原文）仍由 :func:`validate_review` 的既有锚点解析按
    FAIL 处理；枚举非法在 :class:`FictionPatternEvidence` 构造时直接 raise。
    """
    by_id = _index_segments(segments)
    issues: list = []
    evidence = finding.pattern_evidence
    if finding.code in FP_RULE_IDS:
        if evidence is None:
            return (ContractIssue(0, finding.finding_id, "fp_evidence_payload_missing",
                                  f"FP finding {finding.code} 缺 pattern_evidence 载荷；"
                                  "没有结构化证据就申报 FP 结论，不得自动通过"),)
        if evidence.rule_id != finding.code:
            issues.append(ContractIssue(
                0, finding.finding_id, "fp_payload_code_mismatch",
                f"载荷 rule_id {evidence.rule_id} 与 finding code {finding.code} 不一致"))
        if evidence.operation not in FP_STRENGTH_OPERATIONS.get(evidence.strength,
                                                                frozenset()):
            issues.append(ContractIssue(
                0, finding.finding_id, "fp_operation_outside_strength",
                f"operation {evidence.operation!r} 不在 {evidence.strength} 档的"
                "权限内；力度只决定允许的修改操作"))
        if evidence.decision in ("preserve", "protected") and not evidence.preserve_reason:
            issues.append(ContractIssue(
                0, finding.finding_id, "fp_preserve_reason_missing",
                "decision 为 preserve/protected 时必须写明保留依据（人物、视角、情节、"
                "伏笔、节奏或题材功能）"))
        if evidence.rule_id in CLUSTER_FP_RULES:
            distinct = {_anchor_identity(a) for a in finding.anchors}
            if (evidence.scope == "single" or len(distinct) < 2
                    or evidence.occurrences < len(distinct)):
                issues.append(ContractIssue(
                    0, finding.finding_id, "fp_cluster_evidence_insufficient",
                    f"{evidence.rule_id} 是聚集型规则：scope 不得为 single，且至少两个"
                    f"不同锚点、occurrences ≥ 不同锚点数（现申报 occurrences="
                    f"{evidence.occurrences}，不同锚点 {len(distinct)} 个）"))
    elif evidence is not None:
        issues.append(ContractIssue(
            0, finding.finding_id, "fp_payload_on_non_fp_finding",
            f"非 FP finding {finding.code} 携带了 pattern_evidence 载荷，"
            "证据形状与 finding 类型不符"))
    return tuple(issues)


def compare_cross_strength_decisions(findings_by_strength: Mapping) -> tuple:
    """同一 (rule_id, 锚点集合) 的 decision 必须跨三档一致；换 finding_id 绕不过。

    规范键是 ``(rule_id, 排序后的锚点位置身份)``，不是 finding ID。只比较至少出现在
    两个力度里的键；三档缺口单独报 ``fp_cross_strength_evidence_missing``（调用方
    申报三档比较时，缺哪一档都要说出来）。漂移一律 REVIEW——问题分类先于改写力度。
    """
    issues: list = []
    missing = [s for s in FP_STRENGTHS if not findings_by_strength.get(s)]
    if missing:
        issues.append(ContractIssue(
            0, "cross-strength", "fp_cross_strength_evidence_missing",
            f"三档比较缺少 {'、'.join(missing)} 的 finding 输出；缺档无法核对"
            " decision 一致性"))
    by_key: dict = {}
    for strength in FP_STRENGTHS:
        for finding in findings_by_strength.get(strength) or ():
            if not isinstance(finding, ReviewFinding):
                finding = ReviewFinding(**_as_mapping(finding, "ReviewFinding", -1))
            if finding.code not in FP_RULE_IDS or finding.pattern_evidence is None:
                continue
            key = (finding.code,
                   tuple(sorted(_anchor_identity(a) for a in finding.anchors)))
            by_key.setdefault(key, {})[strength] = finding.pattern_evidence.decision
    for key, decisions in sorted(by_key.items()):
        if len(decisions) < 2:
            continue
        unique = sorted(set(decisions.values()))
        if len(unique) > 1:
            detail = "，".join(f"{s}={d}" for s, d in sorted(decisions.items()))
            issues.append(ContractIssue(
                0, key[0], "fp_cross_strength_decision_drift",
                f"{key[0]} 在同一锚点集合上三档 decision 不一致（{detail}）；"
                "问题判定先于改写力度，只有 operation 随力度变"))
    return tuple(issues)


# ---------------------------------------------------------------- 报告校验


def validate_review(report: ReviewReport, *, segments, ledger=(),
                    ledger_sha256: str | None = None, second_semantic_review=(),
                    plan: ReviewPlan | None = None, reread=(),
                    output_segments=()) -> ReviewValidation:
    """校验模型报告：段覆盖、锚点解析、账本证据、枚举、层级、窗口重读证据。

    本函数**不产生**语义结论（人物关系/伏笔/声线归模型），只回答
    「这份报告的证据是否全部可核」。任何一项不过都阻断发布；缺证据一律 REVIEW。

    账本证据的口径（Important B，说实话）：``ledger_sha256`` 只是调用方自报的一个串，
    本函数**无法**从它反推账本内容——没人能重算的哈希不是证据。因此要发布
    （状态可能为 PASS）**必须**传真实的 ``ledger``，摘要由 :func:`ledger_digest`
    现场实算；只给 ``ledger_sha256`` 时报告挂 ``ledger_evidence_missing`` 进 REVIEW
    （哈希对不上仍按造假走 FAIL）。

    全书层（Critical）：复核计划**总是**由手上的 ``segments`` + ``ledger`` 实算，
    省略 ``plan`` 不等于没有可疑窗口；调用方给的 ``plan`` 只作交叉核对，与实算不符
    挂 ``whole_book_plan_mismatch``。窗口只有被交出 :func:`reread_window_texts` 的
    产物（原文 + 成稿，且能从对应分段重算）才算读过；``output_segments`` 给出时
    成稿文本同样按内容重算核实。
    """
    if not isinstance(report, ReviewReport):
        raise ContractError("bad_report", "report 必须是 ReviewReport",
                            kind=type(report).__name__)
    by_id = _index_segments(segments)
    out_by_id = _index_segments(output_segments) if output_segments else {}
    review: list = []
    rejected: list = []
    seen_findings: set = set()
    anchored: list = []

    # ---- 账本哈希与 Task 8 账本复核 ----
    expected = ledger_sha256
    data_derived = bool(ledger)
    if ledger:
        expected = ledger_digest(ledger)
        result = validate_ledger(ledger, segments,
                                 second_semantic_review=second_semantic_review)
        rejected.extend(result.rejected)
        review.extend(result.issues)
    if expected is None:
        review.append(ContractIssue(0, "ledger", "ledger_evidence_missing",
                                    "没拿到账本摘要就没有发布证据：拒绝自动 PASS"))
    else:
        if not data_derived:
            review.append(ContractIssue(
                0, "ledger", "ledger_evidence_missing",
                "账本摘要只有调用方自报的字符串，没有任何数据可重算：可发布的报告"
                "必须带 ledger 实体"))
        if report.ledger_sha256 != expected.lower():
            rejected.append(ContractIssue(
                0, "ledger", "ledger_sha256_mismatch",
                f"报告账本哈希 {report.ledger_sha256[:12]}… 与实算 {expected.lower()[:12]}… "
                "不符，报告对应的不是这一版账本"))

    # ---- 层级完成声明 ----
    for level in REQUIRED_REPORT_LEVELS:
        if level not in report.completed_levels:
            review.append(ContractIssue(0, level, "review_level_missing",
                                        f"{level} 未完成：没有该级证据不得发布"))

    # ---- 段覆盖 ----
    claimed = set()
    for index, sid in enumerate(report.segment_ids):
        if sid in claimed:
            rejected.append(ContractIssue(index, sid, "duplicate_segment_claim",
                                          f"重复申报已复核分段: {sid}"))
            continue
        claimed.add(sid)
        if sid not in by_id:
            rejected.append(ContractIssue(index, sid, "report_unknown_segment",
                                          f"报告引用了不存在的分段 {sid}"))
    for index, segment in enumerate(by_id.values()):
        if segment.segment_id not in claimed:
            review.append(ContractIssue(index, segment.segment_id, "segment_not_reviewed",
                                        f"分段 {segment.segment_id} 没有进入复核"))
        if segment.needs_review:
            review.append(ContractIssue(
                index, segment.segment_id, "degraded_segment_needs_review",
                "Task 7 降级段不得被读成已复核干净：边界需人工确认后重跑"))

    # ---- findings ----
    for index, finding in enumerate(report.findings):
        if finding.finding_id in seen_findings:
            rejected.append(ContractIssue(index, finding.finding_id,
                                          "duplicate_finding_id",
                                          f"finding_id 重复: {finding.finding_id}"))
            continue
        seen_findings.add(finding.finding_id)
        hard: list = []
        notes: list = []
        if finding.code not in MODEL_FINDING_CODES and finding.code not in FP_RULE_IDS:
            hard.append(("unknown_finding_code",
                         f"码 {finding.code!r} 不在复核码枚举里，结论不可核对"))
        for anchor in finding.anchors:
            if anchor.segment_id not in by_id:
                hard.append(("unknown_segment",
                             f"锚点指向不存在的分段 {anchor.segment_id}"))
                continue
            seg_hard, seg_notes = _resolve_anchor(anchor, by_id[anchor.segment_id])
            hard.extend(seg_hard)
            notes.extend(seg_notes)
        if hard:
            for code, detail in sorted(set(hard)):
                rejected.append(ContractIssue(index, finding.finding_id, code, detail))
            continue
        for code, detail in sorted(set(notes)):
            review.append(ContractIssue(index, finding.finding_id, code, detail))
        anchored.append(finding)
        # FP 证据形状：schema/聚集门/力度权限问题全部进 REVIEW；锚点造假已在上面的
        # hard 分支按 FAIL 处理，不会走到这里（也不得让 FP 问题被聚合洗成 PASS）。
        seen_fp: set = set()
        for issue in validate_fiction_pattern_evidence(finding, segments=segments):
            if issue.code in seen_fp:
                continue
            seen_fp.add(issue.code)
            review.append(ContractIssue(index, finding.finding_id, issue.code,
                                        issue.detail))
        # L3 全书结论至少要两个锚点；单点新增事实只配 REVIEW，不配 FAIL/PASS。
        if finding.level == "L3" and len(finding.anchors) < 2:
            review.append(ContractIssue(
                index, finding.finding_id, "single_anchor_l3_finding",
                "L3 结论只有一个锚点：单点新事实按 spec 返回 REVIEW，不升级为 FAIL"))
            continue
        issue = ContractIssue(index, finding.finding_id, finding.code,
                              finding.explanation)
        (rejected if finding.severity == "fail" else review).append(issue)

    # ---- 全书层：计划实算 + 只认重读产物 ----
    layers: Counter = Counter()
    for finding in anchored:
        if finding.level == "L3":
            layers[review_layer_for(finding.anchors, segments)] += 1
    derived = derived_plan(segments, ledger)
    windows_by_id = {w.window_id: w for w in derived.windows}
    if plan is not None:
        declared = {w.window_id for w in plan.windows}
        if declared != set(windows_by_id):
            review.append(ContractIssue(
                0, "plan", "whole_book_plan_mismatch",
                f"调用方计划申报 {len(declared)} 个可疑窗口，按同一份分段与账本实算是 "
                f"{len(windows_by_id)} 个：以实算为准，省略或清空计划关不掉这道闸"))
    covered_ids, reread_notes, reread_hard = _reread_evidence(reread, windows_by_id,
                                                             by_id, out_by_id)
    for position, subject, code, detail in reread_notes:
        review.append(ContractIssue(position, subject, code, detail))
    for position, subject, code, detail in reread_hard:
        rejected.append(ContractIssue(position, subject, code, detail))
    windows_covered = 0
    for position, window in enumerate(derived.windows):
        if window.window_id in covered_ids:
            windows_covered += 1
            continue
        review.append(ContractIssue(
            position, window.window_id, "whole_book_window_unreread",
            "全书层可疑窗口没有重读证据（reread_window_texts 交出的原文+成稿窗口），"
            "不得自动发布"))

    metrics = {
        "segment_total": len(by_id),
        "segments_degraded": sum(1 for s in by_id.values() if s.needs_review),
        "findings": len(report.findings),
        "pattern_findings": sum(1 for f in report.findings
                                if f.pattern_evidence is not None),
        "layers": dict(layers),
        "suspicious_windows": len(derived.windows),
        "windows_covered": windows_covered,
        "ledger_evidence": "digest" if data_derived else ("self-reported" if expected
                                                          else "missing"),
        "max_window_chars": MAX_WINDOW_CHARS,
    }
    return ReviewValidation(accepted=tuple(anchored), review=tuple(review),
                            rejected=tuple(rejected),
                            metrics=MappingProxyType(metrics))


def publication_decision(l0, l1, review: ReviewValidation | None = None
                         ) -> PublicationDecision:
    """四层合并：PASS 要求 L0–L3 全过。``PASS_MECHANICAL``（只有 L0/L1）永远不够。"""
    if l0 is None or l1 is None:
        raise ContractError("bad_report", "publication_decision 需要 L0/L1 机械结果",
                            l0=l0 is None, l1=l1 is None)
    codes: set = set()
    statuses = []
    for result in (l0, l1):
        status = getattr(result, "status", None)
        if status not in DECISION_STATUSES:
            raise ContractError("bad_report", "机械结果状态不在合同枚举里",
                                status=str(status))
        statuses.append(status)
        if status != "PASS":
            codes.update(getattr(result, "codes", ()) or ())
    if review is None:
        statuses.append("REVIEW")
        codes.add("awaiting_l2_l3_review")
    else:
        if not isinstance(review, ReviewValidation):
            raise ContractError("bad_report", "review 必须是 ReviewValidation",
                                kind=type(review).__name__)
        statuses.append(review.status)
        if review.status != "PASS":
            codes.update(review.codes)
    if "ERROR" in statuses:
        status = "ERROR"
    elif "FAIL" in statuses:
        status = "FAIL"
    elif "REVIEW" in statuses:
        status = "REVIEW"
    elif "NO-OP" in statuses:
        status = "NO-OP"
    else:
        status = "PASS"
    return PublicationDecision(status, tuple(sorted(codes)))


# ---------------------------------------------------------------- 全书索引


@dataclass(frozen=True)
class ChapterIndex:
    chapter_index: int
    titles: tuple
    scene_count: int
    segment_ids: tuple
    degraded: bool


@dataclass(frozen=True)
class ClaimAnchor:
    segment_id: str
    start: int
    end: int
    excerpt: str


@dataclass(frozen=True)
class IndexEntry:
    claim_id: str
    category: str
    summary: str
    anchors: tuple


@dataclass(frozen=True)
class SpeakerIndex:
    segment_id: str
    start: int
    end: int
    speaker: str
    excerpt: str


@dataclass(frozen=True)
class PatternHit:
    claim_id: str
    segment_id: str
    family: str
    variant: tuple
    start: int
    end: int
    excerpt: str


@dataclass(frozen=True)
class PatternClaim:
    claim_id: str
    family: str
    total: int
    per_chapter: tuple
    hits: tuple


@dataclass(frozen=True)
class FullBookIndex:
    """全书索引四类 + 分布。只存锚点与短摘要，绝不携带整段正文。"""

    chapters: tuple
    characters: tuple
    timeline: tuple
    world_rules: tuple
    viewpoints: tuple
    speakers: tuple
    patterns: tuple
    punctuation: tuple

    def to_dict(self) -> dict:
        return asdict(self)


def negation_hits(segments) -> tuple:
    """「不是 X，而是 Y」族的全书规范化命中（一遍遍历全书，不按章各扫一次）。

    claim id 由**分段 + 位置**推出，不靠枚举序号：换一种分段顺序重跑，同一处的 id
    不变（序号版本会整体漂移，索引就失去可比性）。
    """
    hits: list = []
    for segment in segments:
        for match in _NEGATION_RE.finditer(segment.text):
            hits.append(PatternHit(
                claim_id=_clip(f"pat-{NEGATION_FAMILY}:{segment.segment_id}"
                               f":{segment.start + match.start()}", MAX_ID_CHARS),
                segment_id=segment.segment_id,
                family=NEGATION_FAMILY, variant=(match.group(1), match.group(2)),
                start=segment.start + match.start(), end=segment.start + match.end(),
                excerpt=_clip(match.group(0), MAX_INDEX_EXCERPT_CHARS)))
    return tuple(hits)


def negation_distribution(segments) -> tuple:
    """按族聚合的全书分布：总数 + 逐章计数 + 逐处 claim id。"""
    chapter_of = {s.segment_id: s.chapter_index for s in segments}
    claims: list = []
    hits = negation_hits(segments)
    if not hits:
        return ()
    per_chapter = Counter(chapter_of.get(h.segment_id, -1) for h in hits)
    claims.append(PatternClaim(
        claim_id=f"pat-{NEGATION_FAMILY}", family=NEGATION_FAMILY, total=len(hits),
        per_chapter=tuple(sorted((ch, count) for ch, count in per_chapter.items())),
        hits=hits))
    return tuple(claims)


def punctuation_distribution(segments) -> tuple:
    counts: Counter = Counter()
    for segment in segments:
        counts.update(ch for ch in segment.text if ch in TRACKED_PUNCTUATION)
    return tuple(sorted(((ch, n) for ch, n in counts.items() if n),
                        key=lambda pair: (-pair[1], pair[0])))


def _index_anchor(anchor) -> ClaimAnchor:
    built = anchor if isinstance(anchor, SourceAnchor) else anchor_from(anchor)
    return ClaimAnchor(built.segment_id, built.start, built.end,
                       _clip(built.excerpt, MAX_INDEX_EXCERPT_CHARS))


def build_full_book_index(*, segments, ledger=()) -> FullBookIndex:
    """全书索引（spec 63 行的四类）。人物/时间线/世界规则**来自账本条目**
    （模型产出、Task 8 已核来源），Python 只做归组与锚点携带，不推断关系。"""
    by_id = _index_segments(segments)
    groups: dict = {}
    for segment in segments:
        groups.setdefault(segment.chapter_index, []).append(segment)
    chapters = tuple(
        ChapterIndex(
            chapter_index=ch,
            titles=tuple(dict.fromkeys(_clip(s.heading_path[-1] if s.heading_path else "",
                                             MAX_INDEX_SUMMARY_CHARS)
                                       for s in segs)),
            scene_count=len({s.scene_index for s in segs}),
            segment_ids=tuple(s.segment_id for s in segs),
            degraded=any(s.needs_review for s in segs))
        for ch, segs in sorted(groups.items()))

    buckets = {"characters": ("name", "alias", "relationship", "character-state"),
               "timeline": ("time",), "world_rules": ("world-rule",),
               "viewpoints": ("viewpoint",)}
    collected: dict = {key: [] for key in buckets}
    for index, entry in enumerate(ledger):
        data = _as_mapping(entry, "LedgerEntry", index)
        category = data.get("category")
        for key, categories in buckets.items():
            if category in categories:
                anchors = tuple(_index_anchor(a) for a in data.get("source_anchors", ())
                                or ())
                collected[key].append(IndexEntry(
                    claim_id=_clip(str(data.get("claim_id", "")), MAX_ID_CHARS),
                    category=category,
                    summary=_clip(str(data.get("statement", "")),
                                   MAX_INDEX_SUMMARY_CHARS), anchors=anchors))
                break

    speakers: list = []
    for segment in segments:
        for quote in _nv.build_index(segment.text).quotes:
            if not quote.speaker:
                continue
            speakers.append(SpeakerIndex(
                segment_id=segment.segment_id,
                start=segment.start + quote.start,
                end=segment.start + quote.start + len(quote.text),
                speaker=_clip(quote.speaker, MAX_INDEX_EXCERPT_CHARS),
                excerpt=_clip(quote.text, MAX_INDEX_EXCERPT_CHARS)))
    return FullBookIndex(
        chapters=chapters, characters=tuple(collected["characters"]),
        timeline=tuple(collected["timeline"]), world_rules=tuple(collected["world_rules"]),
        viewpoints=tuple(collected["viewpoints"]), speakers=tuple(speakers),
        patterns=negation_distribution(segments),
        punctuation=punctuation_distribution(segments))


# ---------------------------------------------------------------- 内置自检


def self_check() -> int:
    failures: list = []
    t1 = "第一章 山门\n他不是怕，是不甘。\n"
    t2 = "第二章 试炼\n这不是演习，而是一次撤离。\n"
    book = [
        Segment("s1", "chapter", 0, len(t1), t1, sha256_text(t1), 0, 0,
                ("第一章 山门",), ()),
        Segment("s2", "chapter", len(t1), len(t1) + len(t2), t2, sha256_text(t2), 1, 0,
                ("第二章 试炼",), ()),
    ]

    def anchor(s, excerpt):
        i = s.text.find(excerpt)
        return {"segment_id": s.segment_id, "start": s.start + i,
                "end": s.start + i + len(excerpt), "excerpt": excerpt,
                "text_sha256": sha256_text(excerpt), "occurrence_index": 0}

    ledger = [{"claim_id": "clm-self", "category": "name", "statement": "林晚是主角",
               "certainty": "certain", "state": "已确认", "operation": "rewrite",
               "output_anchor": anchor(book[0], "山门"),
               "source_anchors": (anchor(book[0], "山门"),)}]
    digest = ledger_digest(ledger)
    plan = derived_plan(book, ledger)
    evidence = [reread_window_texts(w, book, book) for w in plan.windows]
    pass_result = _nv.ValidationResult(status="PASS", level="L0")
    pass_l1 = _nv.ValidationResult(status="PASS", level="L1")
    good = ReviewReport(tuple(s.segment_id for s in book), COMPLETABLE_LEVELS, (), digest)
    result = validate_review(good, segments=book, ledger=ledger, plan=plan, reread=evidence)
    if result.status != "PASS":
        failures.append(f"干净报告应为 PASS，实得 {result.status} {result.codes}")
    if publication_decision(pass_result, pass_l1, result).status != "PASS":
        failures.append("四层全过应可发布")
    bad = ReviewReport(tuple(s.segment_id for s in book), ("L0", "L1", "L2"),
                       (), digest)
    if validate_review(bad, segments=book, ledger=ledger, plan=plan,
                       reread=evidence).status == "PASS":
        failures.append("缺 L3 层级还能 PASS？")
    forged = ReviewReport(tuple(s.segment_id for s in book), COMPLETABLE_LEVELS,
                          [{"finding_id": "f1", "level": "L2", "severity": "fail",
                            "code": "new_event_introduced",
                            "anchors": [dict(anchor(book[0], "山门"),
                                             excerpt="没出现过的句子",
                                             text_sha256=sha256_text("没出现过的句子")),
                                        anchor(book[1], "试炼")],
                            "explanation": "编造证据"}], digest)
    if validate_review(forged, segments=book, ledger=ledger, plan=plan,
                       reread=evidence).status != "FAIL":
        failures.append("编造锚点没被拒")
    index = build_full_book_index(segments=book, ledger=ledger)
    if len(plan.windows) < 2 or plan.single_context:
        failures.append("跨章同型句式没有被全书层定位")
    if {w.window_id for w in plan.windows} != {w.window_id for w in
                                               plan_whole_book_review(index).windows}:
        failures.append("同一份数据的复核计划实算两次给出不同窗口")
    echo = validate_review(good, segments=book, ledger=ledger, plan=plan,
                           reread=[w.window_id for w in plan.windows])
    if echo.status == "PASS" or "bad_reread_evidence" not in echo.codes:
        failures.append("窗口 ID echo 仍然清得掉全书重读闸门")
    forged_text = validate_review(
        good, segments=book, ledger=ledger, plan=plan,
        reread=[WindowText(window_id=plan.windows[0].window_id,
                           source_text="根本没出现的一段话", output_text="也没有的一段话")]
        + evidence[1:])
    if forged_text.status != "FAIL" or "reread_evidence_not_derivable" not in forged_text.codes:
        failures.append("回算不出来的重读产物没按造假拒掉")
    untracked = validate_review(good, segments=book, ledger=ledger, plan=plan)
    if "whole_book_window_unreread" not in untracked.codes:
        failures.append("窗口没重读却没拦住")
    tracked = validate_review(good, segments=book, ledger=ledger, plan=plan, reread=evidence)
    if tracked.status != "PASS":
        failures.append(f"逐窗交出重读产物后仍被阻断: {tracked.codes}")
    if tracked.metrics["windows_covered"] != len(plan.windows):
        failures.append("重读产物没有被记成窗口覆盖")
    self_reported = validate_review(good, segments=book, ledger_sha256=digest)
    if "ledger_evidence_missing" not in self_reported.codes:
        failures.append("只有自报哈希、没有账本实体却当作发布证据")
    omitted_plan = validate_review(good, segments=book, ledger=ledger)
    if omitted_plan.metrics["suspicious_windows"] < 2 or omitted_plan.status == "PASS":
        failures.append("省略 plan 就等于没有可疑窗口——闸门仍可被省略关掉")
    empty_plan = validate_review(good, segments=book, ledger=ledger, plan=ReviewPlan())
    if "whole_book_plan_mismatch" not in empty_plan.codes:
        failures.append("交一份空 plan 就能抹掉实算窗口")
    if failures:
        for line in failures:
            print("SELF-CHECK FAILED:", line)
        return 1
    print(f"novel_review 自检通过：{len(ALL_REVIEW_CODES)} 个可核对码，覆盖 L2/L3 报告、"
          f"四层推进 {REVIEW_LAYERS} 与全书索引四类。")
    print("含义提醒：模型生成 L2/L3 报告，Python 只校验段覆盖、锚点、账本哈希与枚举；"
          "没有证据不得自动 PASS，PASS_MECHANICAL 永远不等于可以发布。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(self_check())
