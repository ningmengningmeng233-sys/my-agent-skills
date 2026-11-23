#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""novel_validation.py — 四层验证合同的 L0/L1 机械校验器（v1.2 Task 10）。

依据：``文档/柠檬/设计/2026-09-28-去AI味v1.2-设计方案.md`` 65–72 行「四层验证合同」、
72 行（12 字规则必须被改造成风险线索，靠调高阈值掩盖问题被明确否定）、
74–76 行（性能合同：不得让每个改写句遍历全部原句、不得循环内全表 count）。
本模块只做**确定性机械判断**；人物关系、伏笔、声线、段落是否空壳、对白的说话人/对象/
意图是否真的没变，一律不假装会做（:data:`REFUSED_JUDGEMENTS`）。

移除的脆弱规则 → 替换成什么（两个方向都有用例钉住）：

1. 「新增连续 12 字即违规」（``NOVEL_CHUNK_THRESHOLD`` / ``find_novel_chunks``）拆成
   两个**按文类与授权成形**的风险线索：
   * ``unsourced_new_content``：成稿内容片段在「原文二元组 ∪ 已授权文本二元组」里
     覆盖率过低（判据是来源覆盖率，字长不再是违规条件）。普通场景全量生效；
     小说场景只对**带事实载荷**的片段（数字/拉丁标识/书名号/时间）生效——
     小说改写本来就会产生新的长句，把它当违规等于天天误报。
   * ``verbatim_carryover_undocumented``：**整个内容单元**逐字出现在成稿里，
     却没有保护项或账本 ``operation="preserve"`` 的解释。句内复用 ≥12 字（回环句、
     固定骂词、系统面板行）不算违规；整段没加工又无解释才是线索。
2. 锁住**所有**中文引号 → 引号先分类：显式引用（归属动词、出处家具、引用块、
   登记过的保护项）逐字锁定；小说场景里没登记的引号按普通对白处理，允许在
   说话人/否定/条件/态度强度不变时编辑；区分不了时进 REVIEW
   （``indistinguishable_dialogue_citation`` 由 Task 8 账本给出，Python 不自己猜）。
3. 标题只比**级别** → 比 (级别, 文字) 序列，另加数量与顺序：
   ``heading_text_changed`` / ``heading_level_changed`` / ``heading_count_changed`` /
   ``heading_order_changed``（risk N14：只比级别时「重要结论→重要决议」静默通过）。
4. 段落数必须相同 → 段落**消失**（对齐后原文单元在成稿里找不到对应物）才报，
   且只有删除授权能解释它，缺授权进 ``paragraph_missing_without_authorization``
   REVIEW（设计方案 57 行）。改写导致段落字节变化不算删除。

Task 7/8 的账在这里收口：``Segment.needs_review`` → ``degraded_segment_needs_review``；
Task 8 的复核码（:data:`TASK8_REVIEW_CODES`）、未闭合围栏（``unclosed_code_fence``）、
ASCII 引号跨行配对（:func:`_ascii_quote_spans`）都被如实消费，聚合永不洗成 PASS。

性能（risk N5）：:func:`build_index` 单遍建立行/标题/围栏/代码/URL/链接/表格/单元/
片段/句子/数字/中文数字/引号/内容二元组索引；保护项、引文与账本摘录先用
:meth:`TextIndex.warm` 一次候选桶扫描批量定位，再按需查桶（:meth:`TextIndex.occurrences`
在桶外兜底时才计 ``rescans``）。Task 8 核实保护项时按**一批原文一次建桶**，扫描量与
条数无关，实际扫过的字符数由 ``protection_scan_chars`` 记账（``full_text_rescans`` 只
看得见本模块的索引，看不见合同内部，旧口径把「墙钟涨 13 倍」放过去了）。限定词频次
一次扫描得到（:func:`hedge_class_counts`），句/段级相似比较只在**同位窗口 ±3** 内做，
不做「每个改写句遍历全部原句」，也不在循环里对全文 ``count``。
"""
from __future__ import annotations

import hashlib
import re
import sys
import time
import tracemalloc
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping, Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------- 合同字面量

SCENES: tuple[str, ...] = ("ordinary", "novel")
STRENGTHS: tuple[str, ...] = ("conservative", "balanced", "deep")
STATUS_VALUES: tuple[str, ...] = ("PASS", "REVIEW", "FAIL", "ERROR", "NO-OP")
LEVEL_VALUES: tuple[str, ...] = ("L0", "L1")

#: ``PASS`` 的准确含义（设计方案 72 行）；机械通过不等于语义等价。
MECHANICAL_SCOPE_NOTE = (
    "PASS（机械口径 PASS_MECHANICAL）只表示 L0 文件结构与 L1 机械事实在覆盖范围内"
    "未发现漂移；它不代表语义等价。最终发布还要求 L2 分段语义与 L3 全书一致性通过。")

#: Python 明确拒绝判断的事项——写进码里就是假装看得懂，所以这里只列拒绝。
REFUSED_JUDGEMENTS: tuple[str, ...] = (
    "人物关系是否正确、关系是否被改写（归 L2 分段语义与 L3 全书一致性）",
    "伏笔是否回收、伏笔状态是否变化（归 L3）",
    "人物声线与作者文风是否等价（归 L3；voice 条目只能防趋同，不能强迫复现）",
    "被删段落是不是可删的空壳、删除后承接是否顺畅（机械验证器不猜，缺授权即 REVIEW）",
    "对白说话人、对象、意图与信息是否真的没变（只有否定、条件、限定强度可机械核实）",
    "因果链与核心立场是否等价（需 L2 语义复核）",
    "整本书一次塞进模型式的全局判断（只按索引定位可疑窗口后重读原文）",
)

FAIL, REVIEW, INFO, ERROR = "FAIL", "REVIEW", "INFO", "ERROR"

#: Task 8 账本/保护清单给出的复核码：一律 REVIEW，绝不允许聚合把它们读成 PASS。
TASK8_REVIEW_CODES: tuple[str, ...] = (
    "anchor_coordinates_stale",
    "degraded_segment_anchor",
    "indistinguishable_dialogue_citation",
    "plot_sensitive_needs_second_review",
    "plot_sensitive_new_needs_second_review",
    "certainty_ambiguous",
    "protection_missing_from_output",
    "protection_count_shortfall",
    "authorization_check_failed",
    "deletion_without_authorization",
    "authorization_unmatched",
    "authorization_identity_mismatch",
    "ordinary_dialogue_not_protectable",
    "voice_cannot_force_preserve",
)

#: 只登记事实、不改变结论的码（Task 8 的 released / soft 分支）。
INFORMATIONAL_CODES: frozenset = frozenset({
    "protection_released_by_authorization",
    "voice_is_soft_reference",
})

#: 码 → 严重度，全项目只在这里定义一次，入口脚本不再另立一套。
SEVERITY: dict[str, str] = {
    # ---- L0 文件与结构
    "source_hash_mismatch": FAIL,
    "encoding_bom_mismatch": FAIL,
    "encoding_roundtrip_mismatch": FAIL,
    # 往返做不成（模块导不进来 / 快照坏到编不了）＝这一项没判成，绝不能静默跳过
    "encoding_roundtrip_unverified": ERROR,
    "heading_text_changed": FAIL,
    "heading_level_changed": FAIL,
    "heading_count_changed": FAIL,
    "heading_order_changed": FAIL,
    "chapter_order_changed": FAIL,
    "scene_order_changed": FAIL,
    "url_changed": FAIL,
    "link_target_changed": FAIL,
    "code_block_changed": FAIL,
    "fence_language_changed": FAIL,
    "inline_code_changed": FAIL,
    "table_rows_changed": FAIL,
    "table_cell_changed": FAIL,
    "fence_parity_changed": FAIL,
    "unclosed_code_fence": REVIEW,
    "segment_coverage_incomplete": FAIL,
    "segment_reassembly_mismatch": FAIL,
    "output_reassembly_incomplete": FAIL,
    "degraded_segment_needs_review": REVIEW,
    # ---- L1 机械事实
    "number_removed": FAIL,
    "number_added": FAIL,
    "number_binding_changed": FAIL,
    "number_multiplicity_unmatched": REVIEW,
    "date_time_changed": FAIL,
    "version_changed": FAIL,
    "chinese_numeral_unmatched": REVIEW,
    "registered_entity_lost": FAIL,
    "registered_entity_unverified": REVIEW,
    "citation_altered": FAIL,
    "citation_added": FAIL,
    "citation_dialogue_indistinguishable": REVIEW,
    "unsourced_new_content": REVIEW,
    "verbatim_carryover_undocumented": REVIEW,
    "near_carryover_unit": REVIEW,
    "repeated_template_unit": REVIEW,
    "negation_polarity_flipped": REVIEW,
    "condition_restriction_dropped": REVIEW,
    "negation_condition_rewrite_uncertain": REVIEW,
    "absolute_assertion_added": REVIEW,
    "hedge_class_changed": REVIEW,
    "dialogue_attitude_changed": REVIEW,
    "speaker_label_changed": REVIEW,
    "paragraph_missing_without_authorization": REVIEW,
    # ---- Task 8 pass-through：复核级
    "anchor_coordinates_stale": REVIEW,
    "degraded_segment_anchor": REVIEW,
    "indistinguishable_dialogue_citation": REVIEW,
    "plot_sensitive_needs_second_review": REVIEW,
    "plot_sensitive_new_needs_second_review": REVIEW,
    "certainty_ambiguous": REVIEW,
    "protection_missing_from_output": REVIEW,
    "protection_count_shortfall": REVIEW,
    "authorization_check_failed": REVIEW,
    "deletion_without_authorization": REVIEW,
    "authorization_unmatched": REVIEW,
    "authorization_identity_mismatch": REVIEW,
    "ordinary_dialogue_not_protectable": REVIEW,
    "voice_cannot_force_preserve": REVIEW,
    "sample_fact_in_output_not_in_source": REVIEW,
    # ---- Task 8 pass-through：只登记
    "protection_released_by_authorization": INFO,
    "voice_is_soft_reference": INFO,
    # ---- Task 8 pass-through：入参非法（拒绝项）
    "bad_payload": FAIL, "missing_field": FAIL, "unknown_field": FAIL,
    "bad_entries": FAIL, "bad_protections": FAIL, "bad_paragraphs": FAIL,
    "bad_claim_id": FAIL, "bad_category": FAIL, "bad_certainty": FAIL,
    "bad_operation": FAIL, "bad_state": FAIL, "bad_statement": FAIL,
    "duplicate_claim_id": FAIL, "no_source_anchor": FAIL, "bad_anchor": FAIL,
    "duplicate_anchor": FAIL, "unknown_segment": FAIL, "anchor_out_of_range": FAIL,
    "anchor_hash_mismatch": FAIL, "anchor_excerpt_not_found": FAIL,
    "anchor_occurrence_out_of_range": FAIL, "bad_entry_id": FAIL,
    "duplicate_entry_id": FAIL, "bad_kind": FAIL, "bad_original_text": FAIL,
    "protection_hash_mismatch": FAIL, "protection_text_mismatch": FAIL,
    "occurrence_index_mismatch": FAIL, "bad_original_position": FAIL,
    "bad_allowed_scope": FAIL, "bad_paragraph_id": FAIL, "bad_authorization_hash": FAIL,
    "bad_reason": FAIL, "bad_check_value": FAIL, "duplicate_authorization": FAIL,
    # validate_voice_sample 的 rejected 码（Task 12 接上后一律按入参非法处理，
    # 不登记就默认落 REVIEW，等于把「rejected→FAIL」的口径自己打脸）
    "bad_sample_id": FAIL, "bad_feature": FAIL, "no_sample_features": FAIL,
    "sample_feature_not_allowed": FAIL, "bad_fact_id": FAIL, "bad_fact_kind": FAIL,
    "bad_fact_text": FAIL, "duplicate_fact_id": FAIL,
    # ---- 调用方把数据喂错（不是判定问题）
    "bad_segments": ERROR, "duplicate_segment_id": ERROR,
    "contract_unavailable": ERROR,
}

ALL_CODES: tuple[str, ...] = tuple(sorted(SEVERITY))

# ---------------------------------------------------------------- 语义令牌表
# 与 references/fidelity.md「否定与条件门（R2 加固）」同源：否定/条件/权限是
# **事实关系**，不是文风，只能 REVIEW/FAIL，绝不 PASS。

NEGATION_PHRASES = [
    "没有必要", "尚未", "暂未", "并未", "从来没有", "并没有", "没有",
    "不会", "不能", "不得", "不应", "不予", "不用", "不再", "不要", "不属于",
    "无需", "无须", "无法", "未能", "未经", "未曾", "非但", "并非",
    "禁止", "严禁", "杜绝", "避免",
    "不", "未", "无", "没", "非", "勿", "莫", "别", "否", "免",
]

CONDITION_TOKENS = ["如果", "假如", "倘若", "一旦", "只要", "只有", "除非", "要是", "若"]

HEDGE_CLASSES = {
    "not_yet": ["目前还没有", "还没有", "尚未", "暂未", "并未", "还没", "没有", "未曾", "未"],
    "restrictive": ["只有", "仅限", "仅当", "仅", "只", "唯"],
    "partial": ["部分", "有些", "某些", "一些", "个别", "局部"],
    "possibility": ["不一定", "未必", "据报道", "据传", "据说", "或许", "大概", "大约", "可能"],
    "approximate": ["基本", "相对", "通常", "一般"],
    "potential": ["潜在", "疑似"],
}

CONDITION_CLASSES = {
    "conditional": ["如果", "假如", "倘若", "一旦", "要是", "若", "则", "除非", "否则"],
    "restrictive": ["只有", "仅当", "仅限", "只能", "才可", "才能", "才", "仅", "只", "唯"],
    "exception": ["除非", "否则", "除外"],
}
PERMISSION_MARKERS = ["可以", "可", "允许", "能够", "能", "权限", "有权", "不得", "禁止"]

SKELETON_FILLERS = [
    "必须", "应当", "应该", "需要", "可以", "能够", "可能", "已经", "正在", "进行", "得到",
    "已", "会", "将", "能", "要", "可", "被", "把", "对", "为", "是", "有", "做", "作",
    "的", "了", "地", "着", "而", "就", "也", "则", "都", "很", "更", "与", "和", "及", "等",
    "中", "后", "前", "时", "我们", "他们", "它们", "其", "该", "本", "此", "这",
]
SKELETON_PUNCT = "，。、；；：！？,.;:!?（）()【】[]{}<>《》\"'“”‘’—－-…·|\n\r\t 　"

POLARITY_SIM_THRESHOLD = 0.75
RESTRICTION_SIM_THRESHOLD = 0.30
#: 内容来源覆盖率：低于它才算「原文里没有依据的内容」。判据是覆盖率，不是字长。
PROVENANCE_FLOOR = 0.20
#: 内容字符下限，只用于「零来源痕迹」的片段；覆盖率那条判据另要 ≥6 内容字符，
#: 免得把合法同义替换（本来就只复用一两个二元组）误报成凭空造事实。
MIN_UNSOURCED_CONTENT_CHARS = 4
MIN_LOW_COVERAGE_CONTENT_CHARS = 6
#: 整单元搬运的门槛：结构判据是「整个内容单元」，句内子串长度不参与判定。
MIN_CARRYOVER_UNIT_CHARS = 20
NEAR_CARRYOVER_DICE = 0.62
TEMPLATE_REPEAT_MIN = 8
ALIGN_WINDOW = 3
UNIT_DICE_FLOOR = 0.30
UNIT_CONTAIN_FLOOR = 0.80

_ABSOLUTE_WORDS = ("全部", "所有", "完全")
_STOP_CHARS = frozenset(ch for ch in SKELETON_FILLERS if len(ch) == 1) | frozenset(SKELETON_PUNCT)
_STOP_TABLE = {ord(ch): None for ch in _STOP_CHARS}

_FRAGMENT_SPLIT_RE = re.compile(
    r"[\s，。、；：！？,.;:!?\u201c\u201d'\"（）()【】\[\]<>《》——…·]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])")
_URL_RE = re.compile(r"""https?://[^\s)\]>"']+""")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
_VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9.]+)?\b")
_DATE_TIME_RE = re.compile(
    r"\b\d{4}[-/年]\d{1,2}[-/月]\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?")
_NUMBER_RE = re.compile(r"(?<![\w.-])\d+(?:\.\d+)?(?![\w.-])")
_MASKED_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(\S.*?)\s*$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)[ \t]*([A-Za-z0-9_+\-]*)\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\|[\s:-|-]+\|$")
_CJK_WORD_RE = re.compile(r"[一-龥A-Za-z0-9]")
_CJK_TRAIL_RE = re.compile(r"[一-龥A-Za-z0-9%％]")
_LEAD_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-龥]{2,4}")
_ID_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_ASCII_QUOTE_RE = re.compile(r'"')
_NON_WORD_RE = re.compile(r"[^\w%％]+", re.UNICODE)
_FACT_BEARING_RE = re.compile(r"[0-9A-Za-z_]|《|》|https?://|\d{1,2}:\d{2}")
_ATTRIBUTION_RE = re.compile(
    r"(说|道|讲|强调|表示|指出|称|回答|回应|答复|写道|记载|引用|出处|原文|曰|认为|"
    r"介绍|透露|宣布|提醒|补充|解释|承诺)[:：\s]{0,3}$")
_DIALOGUE_TAIL_RE = re.compile(r"(了|的|呢|吧|啊|呀|嘛)$")

_CITATION_FURNITURE = ("《", "出处", "引自", "转引", "原著", "页", "著", "出版社",
                        "报告", "文献", "白皮书", "论文", "法案", "条例", "记载")
_ATTITUDE_MARKERS = ("喊", "吼", "厉声", "怒", "冷笑", "讥", "低声", "轻声", "淡",
                     "平静", "皱眉", "叹", "恭声", "急", "慢悠悠", "阴阳怪气")
_QUOTE_PAIRS = {"“": "”", "‘": "’", "「": "」", "『": "』"}

_CN_NUMERALS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
#: 数量事实的单位表。刻意不含 个/条/头/只/口/步/声 这类冠词化量词——
#: 「一个人」「一个问题」里的「一」是冠词不是事实，收进来会把正常压缩全误报成丢数字。
_FACT_UNITS = ("天|年|月|日|时|分|秒|岁|次|位|人|名|遍|回|章|卷|层|里|米|斤|元|角|"
               "倍|成")
_CN_FACT_RE = re.compile(r"(?:第)?([零〇一二三四五六七八九十百千万两]{1,6})(" + _FACT_UNITS + ")")
_UNIT_NUMBER_RE = re.compile(r"(\d{1,9})\s?(" + _FACT_UNITS + ")")


# ---------------------------------------------------------------- 判定类型


@dataclass(frozen=True)
class ValidationPolicy:
    """brief 规范：场景 × 力度，取值域不增不减。"""

    scene: Literal["ordinary", "novel"] = "ordinary"
    strength: Literal["conservative", "balanced", "deep"] = "balanced"

    def __post_init__(self) -> None:
        if self.scene not in SCENES:
            raise ValueError(f"scene 只能是 {'/'.join(SCENES)}，收到 {self.scene!r}")
        if self.strength not in STRENGTHS:
            raise ValueError(f"strength 只能是 {'/'.join(STRENGTHS)}，收到 {self.strength!r}")


@dataclass(frozen=True)
class ValidationResult:
    """brief 规范：status × level × codes × metrics，四个字段，不多不少。"""

    status: Literal["PASS", "REVIEW", "FAIL", "ERROR", "NO-OP"] = "PASS"
    level: Literal["L0", "L1"] = "L0"
    codes: tuple[str, ...] = ()
    metrics: Mapping[str, int | float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUS_VALUES:
            raise ValueError(f"status 非法: {self.status!r}，合法值 {STATUS_VALUES}")
        if self.level not in LEVEL_VALUES:
            raise ValueError(f"level 非法: {self.level!r}，合法值 {LEVEL_VALUES}")


@dataclass(frozen=True)
class Issue:
    """一条可解释判定：码 + 给人看的说明 + 严重度 + 层。"""

    code: str
    detail: str
    severity: str
    level: str

    @property
    def message(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class ValidationInput:
    """一次校验的全部入参：原文/成稿文本、策略、快照字节、分段、账本、保护项与删除授权。"""

    source_text: str
    output_text: str
    policy: ValidationPolicy = ValidationPolicy()
    source_snapshot: object | None = None
    source_bytes: bytes | None = None
    output_bytes: bytes | None = None
    segments: Sequence = ()
    output_segments: Sequence = ()
    protections: Sequence = ()
    authorizations: Sequence = ()
    ledger: Sequence = ()
    second_semantic_review: Sequence = ()
    indistinguishable: Sequence = ()
    measure_memory: bool = False


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def severity_of(code: str) -> str:
    return SEVERITY.get(code, REVIEW)


# ---------------------------------------------------------------- 单遍索引


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    line: int
    start: int


@dataclass(frozen=True)
class QuoteSpan:
    text: str
    start: int
    kind: str          # citation | dialogue
    speaker: str
    furniture: bool


class TextIndex:
    """单遍建立的全部索引；逐片段/逐保护项的全文重扫在这里计数并可被测试抓到。"""

    __slots__ = ("text", "size", "lines", "headings", "fence_blocks", "fence_langs",
                 "inline_codes", "urls", "links", "table_rows", "paragraphs",
                 "fragments", "sentences", "numbers", "numbers_masked", "dates",
                 "versions", "quotes", "numeral_facts", "bigrams", "scans", "ops",
                 "rescans", "unclosed_fence", "elapsed_ms", "paragraph_count",
                 "hedge_passes", "_masked_text", "_buckets", "_warmed", "_hedge_counts")

    def __init__(self, text: str) -> None:
        started = time.perf_counter()
        self.text = text
        self.size = len(text)
        self.scans = 1
        self.ops = 0
        self.rescans = 0
        self.hedge_passes = 0
        self._buckets: dict | None = None
        self._warmed: frozenset = frozenset()
        self._hedge_counts: dict | None = None

        lines: list[tuple[int, int, str]] = []
        pos = 0
        for raw in text.splitlines(keepends=True):
            content = raw[:-1] if raw.endswith("\n") else raw
            if content.endswith("\r"):
                content = content[:-1]
            lines.append((pos, pos + len(content), content))
            pos += len(raw)
        self.lines = lines

        headings: list[Heading] = []
        fence_blocks: list[tuple[str, str]] = []
        fence_langs: list[str] = []
        table_rows: list[list[str]] = []
        paragraphs: list[str] = []
        in_fence = False
        cur_lang = ""
        cur_body: list[str] = []
        paragraph_start: int | None = None
        paragraph_end = 0
        for line_no, (line_start, line_end, content) in enumerate(lines):
            self.ops += 1
            stripped = content.strip()
            fence = _FENCE_RE.match(content)
            if fence:
                if in_fence:
                    fence_blocks.append((cur_lang, "\n".join(cur_body)))
                    cur_body = []
                    in_fence = False
                else:
                    cur_lang = fence.group(1)
                    fence_langs.append(cur_lang)
                    in_fence = True
                if paragraph_start is not None:
                    paragraphs.append(text[paragraph_start:paragraph_end])
                    paragraph_start = None
                continue
            if in_fence:
                cur_body.append(content)
                continue
            heading = _HEADING_RE.match(content)
            if heading:
                headings.append(Heading(len(heading.group(1)), heading.group(2),
                                        line_no, line_start))
            elif _TABLE_ROW_RE.match(content) and not _TABLE_SEP_RE.match(stripped):
                table_rows.append([cell.strip() for cell in stripped.split("|")[1:-1]])
            if stripped:
                if paragraph_start is None:
                    paragraph_start = line_start
                paragraph_end = line_end
            elif paragraph_start is not None:
                paragraphs.append(text[paragraph_start:paragraph_end])
                paragraph_start = None
        self.unclosed_fence = in_fence
        if in_fence:
            fence_blocks.append((cur_lang, "\n".join(cur_body)))
        if paragraph_start is not None:
            paragraphs.append(text[paragraph_start:paragraph_end])

        self.headings = headings
        self.fence_blocks = fence_blocks
        self.fence_langs = sorted(fence_langs)
        self.table_rows = table_rows
        self.paragraphs = paragraphs
        self.inline_codes = sorted(m.group(1) for m in _INLINE_CODE_RE.finditer(text))
        self.urls = sorted(_URL_RE.findall(text))
        self.links = sorted(m.group(1) for m in _LINK_RE.finditer(text))
        self.numbers = _NUMBER_RE.findall(text)
        masked = _VERSION_RE.sub(" ", _DATE_TIME_RE.sub(" ", text))
        self._masked_text = masked
        self.numbers_masked = [(m.group(), m.start(), m.end())
                               for m in _MASKED_NUMBER_RE.finditer(masked)]
        self.dates = sorted(_DATE_TIME_RE.findall(text))
        self.versions = sorted(_VERSION_RE.findall(text))
        self.sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
        self.fragments = [f for f in _FRAGMENT_SPLIT_RE.split(text) if f]
        self.quotes = _extract_quotes(text)
        self.numeral_facts = _numeral_facts(text)
        self.bigrams = _content_bigrams(text)
        self.elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.paragraph_count = len(paragraphs)

    # ---- 批量模式定位：一次候选桶扫描，与模式条数无关 ----

    def warm(self, patterns: Sequence[str]) -> None:
        needed = frozenset(p[:2] for p in tuple(patterns) if len(p) >= 2)
        self._warmed = frozenset(patterns)
        if self._buckets is not None or not needed:
            return
        self._buckets = {}
        self.scans += 1
        text, size = self.text, self.size
        for i in range(size - 1):
            key = text[i:i + 2]
            if key in needed:
                bucket = self._buckets.get(key)
                if bucket is None:
                    self._buckets[key] = [i]
                else:
                    bucket.append(i)
                self.ops += 1

    def occurrences(self, needle: str) -> list[int]:
        """非重叠位置；优先查桶，桶没覆盖到才退化成一次全文 find（计入 rescans）。"""
        if not needle:
            return []
        self.ops += 1
        if len(needle) == 1:
            if needle in self._warmed or self._buckets is None:
                self.rescans += 1
            return [i for i in range(self.size) if self.text[i] == needle]
        if self._buckets is not None and needle[:2] in self._buckets and \
                needle in self._warmed:
            return [i for i in self._buckets[needle[:2]] if self.text.startswith(needle, i)]
        if needle in self._warmed:
            self.rescans += 1
        return _find_all(self.text, needle)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def covers(self, needle: str) -> bool:
        return bool(self.occurrences(needle))

    @property
    def hedge_counts(self) -> dict:
        """限定词类别频次：整篇只扫一遍，扫了几遍由 ``hedge_passes`` 如实记账。"""
        if self._hedge_counts is None:
            self.hedge_passes += 1
            self._hedge_counts = hedge_class_counts(self.text)
        return self._hedge_counts

    @property
    def key_ops(self) -> int:
        return self.ops


def _find_all(haystack: str, needle: str) -> list[int]:
    if not needle:
        return []
    positions = []
    start = haystack.find(needle)
    while start != -1:
        positions.append(start)
        start = haystack.find(needle, start + len(needle))
    return positions


def _content_bigrams(text: str) -> frozenset:
    """剥离虚词与标点后的内容二元组集合（一次遍历）。"""
    content = text.translate(_STOP_TABLE)
    return frozenset(content[i:i + 2] for i in range(len(content) - 1))


def _fragment_bigrams(fragment: str) -> list:
    content = fragment.translate(_STOP_TABLE)
    if len(content) < 2:
        return [content] if content else []
    return [content[i:i + 2] for i in range(len(content) - 1)]


def _cn_value(raw: str) -> int | None:
    """中文数字折整数；折不出返回 None，不猜。"""
    if raw.isdigit():
        return int(raw)
    total = section = digit = 0
    seen = False
    for ch in raw:
        if ch in _CN_NUMERALS:
            digit = _CN_NUMERALS[ch]
            seen = True
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit >= 10000:
                section = (section + digit) * unit
                total += section
                section = digit = 0
                seen = False
            else:
                section += (digit or 1) * unit
                digit = 0
                seen = True
        else:
            return None
    value = total + section + digit
    return value if seen or value else None


def _numeral_facts(text: str) -> Counter:
    """(数值, 单位) 多重集合：中文数字与阿拉伯数字折算到同一口径（年龄/时间/日期同表）。"""
    facts: Counter = Counter()
    for m in _CN_FACT_RE.finditer(text):
        value = _cn_value(m.group(1))
        if value is None:
            facts[("?", m.group(1) + m.group(2))] += 1
        else:
            facts[(value, m.group(2))] += 1
    for m in _UNIT_NUMBER_RE.finditer(text):
        facts[(int(m.group(1)), m.group(2))] += 1
    return facts


def _speaker_before(text: str, start: int) -> str:
    window = text[max(0, start - 24):start]
    match = _ATTRIBUTION_RE.search(window)
    if not match:
        return ""
    head = [h for h in _NON_WORD_RE.split(window[:match.start()].strip()) if h]
    return head[-1][-8:] if head else ""


def _ascii_quote_spans(text: str) -> list[tuple[int, int, str]]:
    """ASCII 双引号的跨行配对（Task 7 交接硬账：分段器没做，验证器必须补上）。"""
    positions = [m.start() for m in _ASCII_QUOTE_RE.finditer(text)]
    spans = []
    for i in range(0, len(positions) - 1, 2):
        start, end = positions[i], positions[i + 1]
        body = text[start + 1:end]
        if body and body.count("\n") <= 8 and len(body) <= 600:
            spans.append((start, end + 1, body))
    return spans


def _extract_quotes(text: str) -> list[QuoteSpan]:
    spans: list[tuple[int, int, str]] = []
    for opener, closer in _QUOTE_PAIRS.items():
        pattern = re.compile(re.escape(opener) + r"([^" + re.escape(closer) +
                             r"]{1,600}?)\s*" + re.escape(closer))
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end(), match.group(1)))
    spans.extend(_ascii_quote_spans(text))
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(">"):
            body = stripped.lstrip(">").strip()
            if body:
                spans.append((offset, offset + len(line), body))
        offset += len(line)
    result: list[QuoteSpan] = []
    seen: set = set()
    for start, _end, body in sorted(spans):
        inner = body.strip()
        key = (start, inner)
        if not inner or key in seen:
            continue
        seen.add(key)
        furniture = any(token in inner for token in _CITATION_FURNITURE)
        speaker = _speaker_before(text, start)
        attributed = bool(speaker)
        if furniture:
            kind = "citation"
        elif attributed and not _DIALOGUE_TAIL_RE.search(inner.rstrip("。！？…")):
            kind = "citation"
        else:
            kind = "dialogue"
        result.append(QuoteSpan(inner, start, kind, speaker, furniture))
    return result


# ---------------------------------------------------------------- 匹配与对齐


def match_one_to_one(source_items: Sequence, output_items: Sequence, compatible):
    """一对一匹配：返回 (匹配数, 未匹配的原文项, 被争用的成稿锚点)。

    设计方案 68 行：重复数字不能让多个原始位置复用同一个输出位置。逐个原文项找
    增广路（Kuhn），得到最大匹配；剩下的原文项若仍有「兼容但已被占用」的成稿锚点，
    就是争用（reuse），必须如实报出来而不是当作满足。

    增广路用 **BFS + 显式父指针**，不用递归：递归版深度等于组大小，单个超长章里
    1200 个同值同绑定的锚点就撞破 1000 层 RecursionError（前端表现为 ERROR/3）。
    """
    out_size = len(output_items)
    taken_by = [-1] * out_size

    matched, unmatched = 0, []
    for start in range(len(source_items)):
        parent: dict = {}                 # 成稿槽位 → 到达它的原文项
        came_from: dict = {}              # 原文项 → 它被挤出来时占着的槽位
        queue = deque([start])
        queued = {start}
        free_slot = -1
        while queue and free_slot == -1:
            item = queue.popleft()
            for slot in range(out_size):
                if slot in parent or not compatible(source_items[item], output_items[slot]):
                    continue
                parent[slot] = item
                holder = taken_by[slot]
                if holder == -1:
                    free_slot = slot
                    break
                if holder not in queued:
                    queued.add(holder)
                    came_from[holder] = slot
                    queue.append(holder)
        if free_slot == -1:
            unmatched.append(source_items[start])
            continue
        slot = free_slot
        while slot != -1:
            item = parent[slot]
            previous = came_from.get(item, -1)
            taken_by[slot] = item
            slot = previous
        matched += 1
    reused = []
    for item in unmatched:
        for slot in range(out_size):
            if compatible(item, output_items[slot]) and output_items[slot] not in reused:
                reused.append(output_items[slot])
    return matched, unmatched, reused


def _bigram_set(text: str) -> frozenset:
    content = text.translate(_STOP_TABLE)
    if len(content) < 2:
        return frozenset({content}) if content else frozenset()
    return frozenset(content[i:i + 2] for i in range(len(content) - 1))


# 单元级二元组缓存：网文/技术文档里同一段落与同一句反复出现，命中率很高；
# 只作有界缓存（超过上限就整表丢弃），不引入新的失效面。
_GRAM_CACHE: dict = {}
_GRAM_CACHE_LIMIT = 4096


def _cached_bigrams(text: str) -> frozenset:
    grams = _GRAM_CACHE.get(text)
    if grams is None:
        grams = _bigram_set(text)
        if len(_GRAM_CACHE) > _GRAM_CACHE_LIMIT:
            _GRAM_CACHE.clear()
        _GRAM_CACHE[text] = grams
    return grams


_FRAG_GRAM_CACHE: dict = {}


def _cached_fragment_bigrams(fragment: str) -> list:
    grams = _FRAG_GRAM_CACHE.get(fragment)
    if grams is None:
        grams = _fragment_bigrams(fragment)
        if len(_FRAG_GRAM_CACHE) > _GRAM_CACHE_LIMIT:
            _FRAG_GRAM_CACHE.clear()
        _FRAG_GRAM_CACHE[fragment] = grams
    return grams


def _unit_dice(a: str, b: str) -> tuple:
    """返回 (Dice 相似度, 较小集合的覆盖率)。"""
    ga, gb = _cached_bigrams(a), _cached_bigrams(b)
    if not ga and not gb:
        return 1.0
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return 2 * inter / (len(ga) + len(gb)), inter / min(len(ga), len(gb))


def _units_similar(a: str, b: str) -> bool:
    if a == b:
        return True
    dice, contain = _unit_dice(a, b)
    return dice >= UNIT_DICE_FLOOR or contain >= UNIT_CONTAIN_FLOOR


def align_units(before: Sequence[str], after: Sequence[str], similar=_units_similar):
    """线性对齐（同位窗口 ±ALIGN_WINDOW）：返回 (原文里没了对应物的单元, 配对列表)。

    先按**逐字相同**配对（哈希计数，O(n)），剩下的再用窗口内的模糊配对兜。
    exact-first 是必需的：否则「删掉中间一段」会被相邻段落抢走配对，把被删的那段
    留成配对、把后来的一段报成消失——删除授权就对不上号了。
    替代「每个改写句遍历全部原句」的平方级做法（设计方案 76 行）。
    """
    before_counts = Counter(before)
    after_counts = Counter(after)
    exact = before_counts & after_counts
    left_before: list[str] = []
    pairs: list[tuple[str, str]] = []
    budget = dict(exact)
    for unit in before:
        if budget.get(unit, 0) > 0:
            budget[unit] -= 1
            pairs.append((unit, unit))
        else:
            left_before.append(unit)
    still = dict(exact)
    left_after: list[str] = []
    for unit in after:
        if still.get(unit, 0) > 0:
            still[unit] -= 1
        else:
            left_after.append(unit)
    vanished: list[str] = []
    cursor = 0
    for unit in left_before:
        found = -1
        for slot in range(max(cursor, 0), min(len(left_after), cursor + ALIGN_WINDOW + 1)):
            if similar(unit, left_after[slot]):
                found = slot
                break
        if found >= 0:
            pairs.append((unit, left_after[found]))
            cursor = found + 1
        else:
            vanished.append(unit)
    return vanished, pairs


# ---------------------------------------------------------------- 骨架与极性


def _strip_phrases(text: str, phrases) -> str:
    for phrase in sorted(set(phrases), key=len, reverse=True):
        text = text.replace(phrase, "")
    return text


def sentence_polarity(sentence: str) -> str:
    return "negative" if any(p in sentence for p in NEGATION_PHRASES) else "positive"


def condition_classes(sentence: str) -> frozenset:
    found = set()
    for cls, markers in CONDITION_CLASSES.items():
        if any(m in sentence for m in markers):
            found.add(cls)
    return frozenset(found)


def has_permission(sentence: str) -> bool:
    return any(m in sentence for m in PERMISSION_MARKERS)


def content_skeleton(sentence: str) -> frozenset:
    text = _strip_phrases(sentence, NEGATION_PHRASES + CONDITION_TOKENS + SKELETON_FILLERS)
    for ch in SKELETON_PUNCT:
        text = text.replace(ch, "")
    return frozenset(text)


def dice(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


#: 全部限定词按「长词优先」排成一条交替式：一遍扫描就够，不在循环里反复全表
#: count（设计方案 76 行点名禁止的做法）。同处只算最长的那个匹配，与旧的
#: 「count 后 replace」口径一致；差别是不再把删掉的位置拼成新词。
_HEDGE_ALTERNATIVES = sorted({phrase for phrases in HEDGE_CLASSES.values() for phrase in phrases},
                             key=lambda s: (-len(s), s))
_HEDGE_OWNERS = {phrase: tuple(sorted(cls for cls, phrases in HEDGE_CLASSES.items()
                                      if phrase in phrases))
                 for phrase in _HEDGE_ALTERNATIVES}
_HEDGE_RE = re.compile("|".join(re.escape(phrase) for phrase in _HEDGE_ALTERNATIVES))


def hedge_class_counts(text: str) -> dict:
    """一次扫描得到每个限定词类别的非重叠频次（类别齐全，缺项记 0）。"""
    counts = {cls: 0 for cls in HEDGE_CLASSES}
    for match in _HEDGE_RE.finditer(text):
        for cls in _HEDGE_OWNERS[match.group()]:
            counts[cls] += 1
    return counts


class _Profiles:
    """按**去重后的句子**算一次骨架/极性/条件，重复句式不再重算。"""

    def __init__(self, sentences: Sequence[str]) -> None:
        self.counter = Counter(sentences)
        self.skeleton: dict = {}
        self.polarity: dict = {}
        self.conditions: dict = {}
        self.permission: dict = {}
        self.stripped: dict = {}
        for sentence in self.counter:
            self.skeleton[sentence] = content_skeleton(sentence)
            self.polarity[sentence] = sentence_polarity(sentence)
            self.conditions[sentence] = condition_classes(sentence)
            self.permission[sentence] = has_permission(sentence)
            self.stripped[sentence] = _strip_phrases(sentence,
                                                    NEGATION_PHRASES + CONDITION_TOKENS)
        self.ops = len(self.counter)


# ---------------------------------------------------------------- 校验器


class _Checker:
    def __init__(self, inp: ValidationInput, src: TextIndex, out: TextIndex) -> None:
        if not isinstance(inp.policy, ValidationPolicy):
            raise ValueError(f"policy 必须是 ValidationPolicy，收到 {inp.policy!r}")
        self.inp = inp
        self.policy = inp.policy
        self.src, self.out = src, out
        self.issues: list[Issue] = []
        self.ops = 0
        self.protection_scan_chars = 0
        self._authorized_bigrams: frozenset | None = None
        self._preserved_bigrams: frozenset | None = None
        self._alignment: tuple | None = None
        self._src_profiles: _Profiles | None = None
        self._out_profiles: _Profiles | None = None
        self.warm_patterns()

    # ---- 基础设施 ----

    def warm_patterns(self) -> None:
        patterns: list[str] = []
        patterns.extend(self.protection_texts())
        patterns.extend(self.ledger_excerpts())
        patterns.extend(q.text for q in self.src.quotes)
        patterns.extend(q.text for q in self.out.quotes)
        patterns = [p for p in patterns if p]
        if patterns:
            self.src.warm(patterns)
            self.out.warm(patterns)

    def add(self, level: str, code: str, detail: str) -> None:
        self.issues.append(Issue(code, detail, severity_of(code), level))

    def level_issues(self, level: str) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == level]

    @property
    def novel(self) -> bool:
        return self.policy.scene == "novel"

    @property
    def strength_rank(self) -> int:
        return STRENGTHS.index(self.policy.strength)

    def record_of(self, item, name: str, default=""):
        if isinstance(item, dict):
            return item.get(name, default)
        return getattr(item, name, default)

    def protection_texts(self) -> tuple:
        return tuple(t for t in (self.record_of(p, "original_text")
                                 for p in self.inp.protections) if isinstance(t, str) and t)

    def ledger_excerpts(self) -> tuple:
        excerpts: list[str] = []
        for entry in self.inp.ledger:
            if str(self.record_of(entry, "category")) not in {"name", "alias", "place"}:
                continue
            for anchor in self.record_of(entry, "source_anchors", ()) or ():
                excerpt = self.record_of(anchor, "excerpt")
                if isinstance(excerpt, str) and excerpt:
                    excerpts.append(excerpt)
        return tuple(excerpts)

    def authorized_blob_bigrams(self) -> frozenset:
        """保护项 + 账本原文 + 授权说明 = 已授权来源的二元组集合（一次建立，全程查表）。"""
        if self._authorized_bigrams is None:
            blobs: list[str] = list(self.protection_texts())
            for entry in self.inp.ledger:
                statement = self.record_of(entry, "statement")
                if isinstance(statement, str):
                    blobs.append(statement)
                blobs.extend(self.record_of(entry, "source_anchors", ()) and
                             [str(self.record_of(a, "excerpt")) for a in
                              self.record_of(entry, "source_anchors")] or [])
            blobs.extend(str(self.record_of(a, "reason")) for a in self.inp.authorizations)
            joined = "".join(b for b in blobs if b)
            self._authorized_bigrams = _content_bigrams(joined) if joined else frozenset()
        return self._authorized_bigrams

    def preserved_blob_bigrams(self) -> frozenset:
        """「刻意逐字保留」的解释来源：保护项原文 + 账本里 ``operation="preserve"``
        登记的原文。

        compress/rewrite/reorder 是「承认动过手」，不能拿来解释整段原样搬运；文档
        （references/fidelity.md 的 ``verbatim_carryover_undocumented`` 一行）承诺的
        正是「账本 preserve」，所以这里必须读 ``operation``——旧实现直接拿
        :meth:`authorized_blob_bigrams` 兜底，任何操作类型的账本条目都能免检。
        """
        if self._preserved_bigrams is None:
            blobs: list[str] = list(self.protection_texts())
            for entry in self.inp.ledger:
                if str(self.record_of(entry, "operation")) != "preserve":
                    continue
                statement = self.record_of(entry, "statement")
                if isinstance(statement, str):
                    blobs.append(statement)
                for anchor in self.record_of(entry, "source_anchors", ()) or ():
                    excerpt = self.record_of(anchor, "excerpt")
                    if isinstance(excerpt, str):
                        blobs.append(excerpt)
            joined = "".join(b for b in blobs if b)
            self._preserved_bigrams = _content_bigrams(joined) if joined else frozenset()
        return self._preserved_bigrams

    @property
    def paragraph_alignment(self) -> tuple:
        if self._alignment is None:
            self._alignment = align_units(self.src.paragraphs, self.out.paragraphs)
            self.ops += len(self.src.paragraphs)
        return self._alignment

    @property
    def sentence_profiles(self) -> tuple:
        if self._src_profiles is None:
            self._src_profiles = _Profiles(self.src.sentences)
            self._out_profiles = _Profiles(self.out.sentences)
            self.ops += self._src_profiles.ops + self._out_profiles.ops
        return self._src_profiles, self._out_profiles


def _build_result(level: str, issues, metrics, *, noop: bool) -> ValidationResult:
    severities = {issue.severity for issue in issues}
    codes = tuple(sorted({issue.code for issue in issues}))
    if ERROR in severities:
        status = "ERROR"
    elif FAIL in severities:
        status = "FAIL"
    elif noop:
        status = "NO-OP"
    elif REVIEW in severities:
        status = "REVIEW"
    else:
        status = "PASS"
    merged = dict(metrics)
    merged["issue_fail"] = sum(1 for i in issues if i.severity == FAIL)
    merged["issue_review"] = sum(1 for i in issues if i.severity == REVIEW)
    merged["issue_informational"] = sum(1 for i in issues if i.severity == INFO)
    return ValidationResult(status, level, codes, merged)


def _contract_problems(check: "_Checker", level: str, func_name: str, *args, **kwargs):
    """调用 Task 8 冻结合同；ContractError 是入参事故 → ERROR，绝不当通过。"""
    try:
        module = _contracts()
    except ImportError:
        check.add(level, "contract_unavailable", "novel_contracts 不可用，机械层无法核实来源")
        return None
    try:
        call = getattr(module, func_name)
        return call(*args, **kwargs)
    except Exception as exc:            # ContractError 与入参事故都按 ERROR 处理
        code = str(getattr(exc, "code", "contract_unavailable") or "contract_unavailable")
        check.add(level, code if code in SEVERITY else "contract_unavailable",
                  f"合同校验无法进行（入参事故，不是判定结果）: {exc}")
        return None


def _contracts():
    here = str(SCRIPTS_DIR)
    if here not in sys.path:
        sys.path.insert(0, here)
    import novel_contracts          # noqa: PLC0415  按需加载：普通机械校验不碰小说层
    return novel_contracts


def _protection_scan_chars() -> int:
    """读 Task 8 的诊断计数（核实保护项扫过的字符数）；模块不在就当 0。"""
    try:
        return int(_contracts().SCAN_STATS["scan_chars"])
    except Exception:
        return 0


def _segments():
    here = str(SCRIPTS_DIR)
    if here not in sys.path:
        sys.path.insert(0, here)
    import novel_segments           # noqa: PLC0415
    return novel_segments


def _fallback_segments(text: str) -> tuple:
    module = _segments()
    return (module.Segment(segment_id="s1", kind="body", start=0, end=len(text), text=text,
                           text_sha256=sha256_text(text), chapter_index=0, scene_index=0,
                           heading_path=(), protected_ranges=(), needs_review=False),)


# ---------------------------------------------------------------- L0 判定


def _l0_checks(check: _Checker) -> None:
    level = "L0"
    _check_source_bytes(check, level)
    _check_headings(check, level)
    _check_links_code_tables(check, level)
    _check_fence_integrity(check, level)
    _check_segments(check, level)
    _check_contract_protections(check, level)


def _check_source_bytes(check: _Checker, level: str) -> None:
    inp = check.inp
    snapshot = inp.source_snapshot
    if snapshot is None:
        return
    recorded = str(getattr(snapshot, "sha256", "") or "")
    if inp.source_bytes is not None and recorded:
        produced = hashlib.sha256(inp.source_bytes).hexdigest()
        if produced != recorded:
            check.add(level, "source_hash_mismatch",
                      "重新计算的原稿字节哈希与任务快照不一致，拒绝把新旧原稿混成一次任务")
    if inp.output_bytes is not None:
        try:
            produced = _reencode(check.out.text, snapshot)
        except Exception as exc:            # 导不进来、快照坏到编不了……都是「没判成」
            check.add(level, "encoding_roundtrip_unverified",
                      f"无法按原稿编码复核成稿字节（{type(exc).__name__}: {exc}）："
                      "这一项没做成，不能当成通过")
            return
        if produced == inp.output_bytes:
            return
        bom = bytes(getattr(snapshot, "bom", b"") or b"")
        has_bom = inp.output_bytes[:3] == b"\xef\xbb\xbf"
        if bool(bom) != has_bom and bom in (b"", b"\xef\xbb\xbf"):
            check.add(level, "encoding_bom_mismatch",
                      f"成稿字节的 BOM 与原稿编码（{getattr(snapshot, 'encoding', '?')}）"
                      "不一致，候选没有按原编码往返")
        else:
            check.add(level, "encoding_roundtrip_mismatch",
                      "成稿字节与按原稿编码/换行策略重新编码的结果不一致")


def _reencode(text: str, snapshot):
    """按原稿编码/换行策略把成稿编回字节。

    失败**原样抛出**，由调用方如实记账：旧实现在这里吞掉 ImportError/编码异常返回
    ``None``，于是字节对不上的候选在 spec 66 行点名的这一项上直接静默通过。
    """
    here = str(SCRIPTS_DIR)
    if here not in sys.path:
        sys.path.insert(0, here)
    import text_source            # noqa: PLC0415
    return text_source.encode_text(text, snapshot)


def _check_headings(check: _Checker, level: str) -> None:
    src, out = check.src.headings, check.out.headings
    if [(h.level, h.text) for h in src] == [(h.level, h.text) for h in out]:
        return
    src_texts = [h.text for h in src]
    out_texts = [h.text for h in out]
    if len(src) != len(out):
        check.add(level, "heading_count_changed",
                  f"标题数量变化: 原 {len(src)} 个，现 {len(out)} 个")
    if sorted(src_texts) == sorted(out_texts) and src_texts != out_texts:
        check.add(level, "heading_order_changed",
                  f"标题顺序被改变: 原 {src_texts} -> 现 {out_texts}")
    for index in range(min(len(src), len(out))):
        before, after = src[index], out[index]
        if before.text != after.text:
            check.add(level, "heading_text_changed",
                      f"标题文字被改动: 原「{before.text}」→ 现「{after.text}」"
                      f"（第 {index + 1} 个；级别只比是不够的）")
        if before.level != after.level:
            check.add(level, "heading_level_changed",
                      f"标题层级变化: 「{before.text}」 {before.level} 级 → {after.level} 级")
    if len(src) != len(out) and sorted(src_texts) != sorted(out_texts):
        missing = [t for t in src_texts if t not in out_texts]
        added = [t for t in out_texts if t not in src_texts]
        if missing or added:
            check.add(level, "heading_text_changed",
                      f"标题集合发生变化: 缺少 {missing}，新增 {added}")


def _check_links_code_tables(check: _Checker, level: str) -> None:
    src, out = check.src, check.out
    if src.urls != out.urls:
        check.add(level, "url_changed", f"URL 发生漂移: 原 {src.urls} -> 现 {out.urls}")
    if src.links != out.links:
        check.add(level, "link_target_changed",
                  f"链接目标变化: 原 {src.links} -> 现 {out.links}")
    if src.fence_blocks != out.fence_blocks:
        check.add(level, "code_block_changed", "围栏代码块正文发生变动")
    if src.fence_langs != out.fence_langs:
        check.add(level, "fence_language_changed",
                  f"围栏语言标记变化: 原 {src.fence_langs} -> 现 {out.fence_langs}")
    if src.inline_codes != out.inline_codes:
        check.add(level, "inline_code_changed",
                  f"行内代码/标识符被篡改: 原 {src.inline_codes} -> 现 {out.inline_codes}")
    if len(src.table_rows) != len(out.table_rows):
        check.add(level, "table_rows_changed",
                  f"表格行数发生改变: 原 {len(src.table_rows)} 行，现 {len(out.table_rows)} 行")
    elif src.table_rows != out.table_rows:
        check.add(level, "table_cell_changed", "表格单元格正文被修改")


def _check_fence_integrity(check: _Checker, level: str) -> None:
    """Task 7 交接硬账：未闭合围栏会一路保护到 EOF，必须说出来而不是当成正常。"""
    if check.src.unclosed_fence or check.out.unclosed_fence:
        check.add(level, "unclosed_code_fence",
                  "存在未闭合的代码围栏：分段会把它一路保护到 EOF（原文未闭合="
                  f"{check.src.unclosed_fence} 成稿未闭合={check.out.unclosed_fence}），"
                  "网文系统面板常缺收尾围栏，需人工确认")
    if check.src.unclosed_fence != check.out.unclosed_fence:
        check.add(level, "fence_parity_changed",
                  "代码围栏闭合状态发生变化：原文与成稿的围栏配对不一致")


def _check_segments(check: _Checker, level: str) -> None:
    inp = check.inp
    if not inp.segments:
        return
    try:
        module = _segments()
    except ImportError:
        check.add(level, "contract_unavailable", "novel_segments 不可用，无法核对回拼")
        return
    Segment, join_segments = module.Segment, module.join_segments
    segments = [seg for seg in inp.segments if isinstance(seg, Segment)]
    if len(segments) != len(tuple(inp.segments)):
        check.add(level, "bad_segments", "segments 含非 Segment 元素，无法据其核对结构")
        return
    if join_segments(segments) != check.src.text:
        covered = sum(seg.end - seg.start for seg in segments)
        check.add(level, "segment_reassembly_mismatch",
                  f"分段未能逐字符回拼原稿（覆盖 {covered}/{len(check.src.text)} 字符）")
    if any(seg.end - seg.start != len(seg.text) for seg in segments):
        check.add(level, "segment_coverage_incomplete", "分段坐标与分段文本长度不一致")
    degraded = [seg.segment_id for seg in segments if seg.needs_review]
    if degraded:
        check.add(level, "degraded_segment_needs_review",
                  f"Task 7 按单元降级的分段共 {len(degraded)} 段（{degraded[:5]}）："
                  "这些段在预算内找不到安全切点，不得进入自动成稿，必须送人工/模型复核")
    if inp.output_segments:
        out_segments = [seg for seg in inp.output_segments if isinstance(seg, Segment)]
        if len(out_segments) != len(tuple(inp.output_segments)):
            check.add(level, "bad_segments", "output_segments 含非 Segment 元素")
        elif join_segments(out_segments) != check.out.text:
            check.add(level, "output_reassembly_incomplete",
                      "成稿分段无法逐字符回拼成稿文本")
        _check_chapter_scene_order(check, level, segments, out_segments)


def _check_chapter_scene_order(check: _Checker, level: str, segments, out_segments) -> None:
    def marks(items):
        return [(seg.kind, seg.chapter_index, seg.scene_index, seg.heading_path)
                for seg in items if seg.kind in ("chapter", "scene")]
    src_marks, out_marks = marks(segments), marks(out_segments)
    if src_marks == out_marks:
        return
    src_titles = [(m[0], m[3]) for m in src_marks]
    out_titles = [(m[0], m[3]) for m in out_marks]
    if sorted(src_titles) == sorted(out_titles):
        check.add(level, "chapter_order_changed",
                  f"章/景顺序被改变: 原有 {len(src_marks)} 个结构标记，成稿顺序不同"
                  f"（{out_titles[:3]}）")
    else:
        check.add(level, "scene_order_changed",
                  f"章/景标记集合变化: 原 {src_titles[:3]} -> 现 {out_titles[:3]}")


def _check_contract_protections(check: _Checker, level: str) -> None:
    inp = check.inp
    if not (inp.protections or inp.segments):
        return
    segments = tuple(inp.segments) or _fallback_segments(check.src.text)
    before = _protection_scan_chars()
    validation = _contract_problems(check, level, "validate_protections",
                                    inp.protections, segments,
                                    output_text=check.out.text,
                                    authorizations=inp.authorizations,
                                    indistinguishable=inp.indistinguishable)
    check.protection_scan_chars += _protection_scan_chars() - before
    if validation is None:
        return
    for issue in tuple(validation.rejected) + tuple(validation.issues):
        check.add(level, issue.code, issue.detail)


# ---------------------------------------------------------------- L1 判定


def _l1_checks(check: _Checker) -> None:
    level = "L1"
    _check_numbers(check, level)
    _check_numeral_facts(check, level)
    _check_dates_versions(check, level)
    _check_quotes(check, level)
    _check_unsourced_content(check, level)
    _check_carryover(check, level)
    _check_polarity_and_condition(check, level)
    _check_hedges(check, level)
    _check_paragraph_survival(check, level)
    _check_ledger(check, level)


def _check_numbers(check: _Checker, level: str) -> None:
    src, out = check.src, check.out
    src_counter, out_counter = Counter(src.numbers), Counter(out.numbers)
    for num, count in sorted((out_counter - src_counter).items()):
        check.add(level, "number_added", f"无来源新增数值: {num} (新增 {count} 次)")
    for num, count in sorted((src_counter - out_counter).items()):
        check.add(level, "number_removed", f"数值出现次数减少或被删除: {num} (原 {count} 次)")

    grouped_src: dict = {}
    for value, start, end in src.numbers_masked:
        grouped_src.setdefault(value, []).append(
            (value, _binding_signature(src._masked_text, start, end)))
    grouped_out: dict = {}
    for value, start, end in out.numbers_masked:
        grouped_out.setdefault(value, []).append(
            (value, _binding_signature(out._masked_text, start, end)))

    for value, items in sorted(grouped_src.items()):
        candidates = grouped_out.get(value, [])
        if not candidates:
            continue
        _matched, unmatched, _reused = match_number_group(items, candidates)
        check.ops += len(items)
        probes = _distinct_candidates(candidates)     # 同一绑定只比一次
        for item in unmatched:
            lead, trail = item[1][0], item[1][1]
            if any(_number_compatible(item, cand) for cand in probes):
                check.add(level, "number_multiplicity_unmatched",
                          f"数字 {value} 的某个原始位置只能复用已被占用的成稿锚点"
                          f"（原上下文: {lead}{value}{trail}）：重复数字必须一对一")
            else:
                check.add(level, "number_binding_changed",
                          f"数字 {value} 紧邻对象/单位发生改变或换绑 (原: {lead}{value}{trail})")


#: 同一数值的最坏匹配工作量。增广路是 O(原文项 × 候选)：单个超长章里同值锚点成千，
#: 跑满会把 L1 拖死（递归版更是直接爆栈）。超过上限改按**精确绑定签名计数**——
#: 组大到这个量级时「哪个原始位置对上哪个成稿位置」本就无从区分，能定案的只有
#: 多重集合对不对得上；模糊兼容换不来额外事实，所以宁可多报 REVIEW 不多放通过。
MATCH_WORK_LIMIT = 4096


def _number_key(item: tuple) -> tuple:
    """匹配项的可哈希指纹（签名里的集合转 frozenset），用于计数与去重。"""
    lead, trail, lead_chars, trail_chars, tokens, ids = item[1]
    return (item[0], lead, trail, frozenset(lead_chars), frozenset(trail_chars),
            frozenset(tokens), frozenset(ids))


def match_number_group(items, candidates):
    """同值锚点一对一匹配：小组走增广路，大组按精确签名计数（risk N5）。"""
    if len(items) * len(candidates) <= MATCH_WORK_LIMIT:
        return match_one_to_one(items, candidates, _number_compatible)
    pool = Counter(_number_key(candidate) for candidate in candidates)
    matched, unmatched = 0, []
    for item in items:
        key = _number_key(item)
        if pool.get(key, 0) > 0:
            pool[key] -= 1
            matched += 1
        else:
            unmatched.append(item)
    return matched, unmatched, []


def _distinct_candidates(candidates) -> list:
    """按签名去重的候选：兼容性只跟不同绑定比，不与重复位置比。"""
    seen, probes = set(), []
    for candidate in candidates:
        key = _number_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        probes.append(candidate)
    return probes


def _number_compatible(before: tuple, after: tuple) -> bool:
    """匹配项是 (数值, 绑定签名)；数值必须先相同，再比绑定对象。"""
    return before[0] == after[0] and _bindings_compatible(before[1], after[1])


def _binding_signature(masked: str, start: int, end: int) -> tuple:
    lead = masked[max(0, start - 10):start].strip()
    trail = masked[end:min(len(masked), end + 10)].strip()
    return (lead, trail,
            set(_CJK_WORD_RE.findall(lead[-6:])),
            set(_CJK_TRAIL_RE.findall(trail[:6])),
            set(_LEAD_TOKEN_RE.findall(lead[-8:])),
            set(_ID_TOKEN_RE.findall(lead[-6:])))


def _bindings_compatible(before: tuple, after: tuple) -> bool:
    b_lead_chars, b_trail_chars, _b_tokens, b_ids = before[2], before[3], before[4], before[5]
    a_lead_chars, a_trail_chars, _a_tokens, a_ids = after[2], after[3], after[4], after[5]
    if b_ids and a_ids and b_ids != a_ids:
        return False                      # 主体代号不同：严禁跨实体换绑
    lead_ok = (not b_lead_chars) or \
        (len(b_lead_chars & a_lead_chars) >= max(1, len(b_lead_chars) - 1))
    trail_ok = (not b_trail_chars) or (len(b_trail_chars & a_trail_chars) >= 1)
    return lead_ok and trail_ok


def _check_numeral_facts(check: _Checker, level: str) -> None:
    missing = check.src.numeral_facts - check.out.numeral_facts
    for (value, unit), count in sorted(missing.items(), key=lambda kv: str(kv[0])):
        display = _cn_display(value) if isinstance(value, int) else value
        if value == "?":
            check.add(level, "chinese_numeral_unmatched",
                      f"无法折算的中文数字「{unit}」在成稿里找不到同形写法（共 {count} 处）")
            continue
        display = _cn_display(value)
        check.add(level, "chinese_numeral_unmatched",
                  f"数量事实 {display}{unit}（数值 {value}，共 {count} 处）在成稿里"
                  "没有同值同单位的写法；中文数字与阿拉伯数字互换不算变化")


def _cn_display(value: int) -> str:
    digits = "零一二三四五六七八九"
    if value < 10:
        return digits[value]
    if value < 20:
        return "十" + (digits[value % 10] if value % 10 else "")
    if value < 100:
        return digits[value // 10] + "十" + (digits[value % 10] if value % 10 else "")
    return str(value)


def _check_dates_versions(check: _Checker, level: str) -> None:
    src, out = check.src, check.out
    b_dates, a_dates = set(src.dates), set(out.dates)
    if a_dates - b_dates:
        check.add(level, "date_time_changed",
                  f"无来源新增日期/时间: {sorted(a_dates - b_dates)}")
    if b_dates - a_dates:
        check.add(level, "date_time_changed", f"原始日期/时间丢失: {sorted(b_dates - a_dates)}")
    if src.versions != out.versions:
        check.add(level, "version_changed",
                  f"版本号发生变化: 原 {src.versions} -> 现 {out.versions}")


def _check_quotes(check: _Checker, level: str) -> None:
    """真实引文逐字锁定；小说对白可编辑；无法区分 → REVIEW（设计方案 55/57 行）。

    普通场景：所有引号一律按显式引用处理（v1.1 的行为，fail-closed）。
    小说场景：只有带引用家具（《》/出处/出版社…）或登记过保护项/账本的引文才锁定，
    其余按普通对白处理——小说里「某某说：“……”」绝大多数是虚构对白，全锁等于
    否掉整条小说改写通路；区分不了的情况由 Task 8 的 ``indistinguishable`` 送 REVIEW。
    """
    novel = check.novel
    authorized = check.authorized_blob_bigrams()
    for quote in check.src.quotes:
        if novel:
            grams = _cached_bigrams(quote.text)
            locked = quote.furniture or quote.kind == "citation" and bool(grams) and \
                grams <= authorized
        else:
            locked = True
        if not locked:
            continue
        if check.out.covers(quote.text):
            continue
        check.add(level, "citation_altered",
                  f"引语/引用内容被篡改或反转: 原引语「{quote.text[:40]}」未在改写稿中完整保留")
    for quote in check.out.quotes:
        if novel or quote.kind != "citation":
            continue
        if not check.src.covers(quote.text):
            check.add(level, "citation_added", f"无来源新增引语: 「{quote.text[:40]}」")
    if novel:
        for before, after in _pair_dialogue(check.src.quotes, check.out.quotes):
            if before.speaker and after.speaker and before.speaker != after.speaker:
                check.add(level, "speaker_label_changed",
                          f"对白归属可能变化: 原「{before.speaker}说」→「{after.speaker}说」；"
                          "说话人与对象是否真的没变属 L2，本层只报线索")
            b_attitude = {m for m in _ATTITUDE_MARKERS if m in before.speaker + before.text}
            a_attitude = {m for m in _ATTITUDE_MARKERS if m in after.speaker + after.text}
            if b_attitude != a_attitude:
                check.add(level, "dialogue_attitude_changed",
                          f"对白态度强度标记变化: {sorted(b_attitude)} → {sorted(a_attitude)}"
                          "（态度强度不变才允许编辑）")
    flagged = tuple(x for x in check.inp.indistinguishable if isinstance(x, str))
    if flagged:
        check.add(level, "citation_dialogue_indistinguishable",
                  f"{len(flagged)} 项引文与对白无法区分，按设计方案进 REVIEW 交人工/模型裁决")


def _pair_dialogue(src_quotes, out_quotes):
    """对白配对只在同位窗口内比骨架，不拿每句对白去扫全部原句。"""
    used, pairs = set(), []
    for index, before in enumerate(src_quotes):
        best, best_sim = -1, 0.0
        for slot in range(index, min(len(out_quotes), index + ALIGN_WINDOW * 3 + 3)):
            if slot in used:
                continue
            sim = dice(content_skeleton(out_quotes[slot].text),
                       content_skeleton(before.text))
            if sim > best_sim:
                best, best_sim = slot, sim
        if best >= 0 and best_sim >= 0.45:
            used.add(best)
            pairs.append((before, out_quotes[best]))
    return pairs


def _check_unsourced_content(check: _Checker, level: str) -> None:
    """12 字规则的正统后代：来源覆盖率 + 文类 + 授权，字长不再是违规条件。"""
    base = check.src.bigrams | check.authorized_blob_bigrams()
    if not base:
        return
    flagged, samples = 0, []
    for fragment in check.out.fragments:
        content = fragment.translate(_STOP_TABLE)
        if len(content) < MIN_UNSOURCED_CONTENT_CHARS:
            continue
        if check.novel and not _FACT_BEARING_RE.search(fragment):
            continue
        grams = _cached_fragment_bigrams(fragment)
        if not grams:
            continue
        check.ops += 1
        hit = sum(1 for g in grams if g in base)
        if hit == 0:
            flagged += 1            # 一个来源二元组都没有：模型前言、凭空断言都落在这档
        elif (len(content) >= MIN_LOW_COVERAGE_CONTENT_CHARS
              and hit / len(grams) < PROVENANCE_FLOOR):
            flagged += 1            # 有来源痕迹但覆盖率极低（大段新增里只蹭到一两个词）
        else:
            continue
        if len(samples) < 3:
            samples.append(fragment[:28])
    if flagged:
        check.add(level, "unsourced_new_content",
                  f"成稿有 {flagged} 处内容片段在原文与已授权文本里都没有来源依据"
                  f"（示例：{samples}）")


def _check_carryover(check: _Checker, level: str) -> None:
    """整单元原样搬运且无授权解释；句内复用长片段（回环句）不算违规。

    只在小说场景生效：普通文稿里「这一段本来就不用改」是正常结果，不是风险线索；
    小说通路的预期是每个单元都被改写过或被明确登记保留，所以没解释的整段搬运才是线索。
    """
    if not check.novel:
        return
    if check.strength_rank < 1:
        return
    if check.out.text == check.src.text:
        return                      # 整篇没动是交付层的 NO-OP，不是保真风险线索
    _vanished, pairs = check.paragraph_alignment
    protected = check.protection_texts()
    preserved = check.preserved_blob_bigrams()
    carried, near = [], []
    for before, after in pairs:
        check.ops += 1
        content = after.translate(_STOP_TABLE)
        if len(content) < MIN_CARRYOVER_UNIT_CHARS:
            continue
        if before != after:
            if check.strength_rank >= 2:
                dice_value = _unit_dice(before, after)[0]
                if dice_value >= NEAR_CARRYOVER_DICE:
                    near.append(after[:30])
            continue
        grams = frozenset(_cached_fragment_bigrams(after))
        if any(after == p for p in protected):
            continue
        if grams and grams <= preserved:
            continue
        carried.append(after[:30])
    if carried:
        check.add(level, "verbatim_carryover_undocumented",
                  f"成稿有 {len(carried)} 个内容单元与原文逐字相同，但没有保护项或"
                  f"账本 preserve 解释它（示例：{carried[:3]}）——"
                  "这是风险线索，不是字数阈值")
    if near:
        check.add(level, "near_carryover_unit",
                  f"成稿有 {len(near)} 个单元与原文高度接近（二元组相似度"
                  f"≥{NEAR_CARRYOVER_DICE}），可能是漏改，需确认是刻意保留还是没动")
    if check.strength_rank >= 2:
        counts = Counter(check.src.paragraphs)
        repeated = [p[:24] for p, n in counts.items()
                    if n >= TEMPLATE_REPEAT_MIN
                    and len(p.translate(_STOP_TABLE)) >= MIN_CARRYOVER_UNIT_CHARS]
        if repeated:
            check.add(level, "repeated_template_unit",
                      f"同一内容单元在原文重复 {TEMPLATE_REPEAT_MIN} 次以上，"
                      f"属跨章模板残留线索（示例：{repeated[:2]}）")


def _check_polarity_and_condition(check: _Checker, level: str) -> None:
    if check.src.text == check.out.text:
        return
    src_profiles, out_profiles = check.sentence_profiles
    stripped_before = set(src_profiles.stripped.values())
    # 4f 同源规则：成稿里出现次数超过原文的同一句子，且剥离否定/条件后与原文某句重合
    for sentence, count in out_profiles.counter.items():
        if count <= src_profiles.counter.get(sentence, 0):
            continue
        if out_profiles.stripped[sentence] in stripped_before:
            check.add(level, "negation_condition_rewrite_uncertain",
                      f"疑似否定/条件关系翻转或增删，需语义复核: \"{sentence}\"")
    _best_pairs = _polarity_pairs(check.src.sentences, check.out.sentences, src_profiles,
                                  out_profiles)
    check.ops += len(_best_pairs)
    for before, after in _best_pairs:
        if before == after:
            continue
        b_pol, a_pol = src_profiles.polarity[before], out_profiles.polarity[after]
        b_skel, a_skel = src_profiles.skeleton[before], out_profiles.skeleton[after]
        sim = dice(b_skel, a_skel)
        if sim >= POLARITY_SIM_THRESHOLD and b_pol != a_pol and not _is_resegmentation(before, after):
            check.add(level, "negation_polarity_flipped",
                      f"否定极性被翻转，需语义复核: 原句[{b_pol}]「{before.strip()}」"
                      f" → 改写[{a_pol}]「{after.strip()}」(骨架相似度 {sim:.2f})")
            continue
        dropped = {cls for cls in (src_profiles.conditions[before]
                                   & {"restrictive", "exception"})
                   if cls not in out_profiles.conditions[after]}
        if dropped and sim >= RESTRICTION_SIM_THRESHOLD and (
                src_profiles.permission[before] or out_profiles.permission[after]):
            check.add(level, "condition_restriction_dropped",
                      f"限制条件被移除，需语义复核: 原句含「{'、'.join(sorted(dropped))}」类限制"
                      f"「{before.strip()}」 → 改写「{after.strip()}」不再保留该限制"
                      f" (骨架相似度 {sim:.2f})")


#: 短句集（去重后 ≤64 句）沿用 v1.1 口径：每句在全部原句里找最相似的；
#: 长文只在同位窗口 ±8 句内找，避免「每个改写句遍历全部原句」（设计方案 76 行）。
GLOBAL_COMPARE_MAX = 64
POLARITY_WINDOW = 8


def _polarity_pairs(src_sentences, out_sentences, src_profiles, out_profiles):
    pairs = []
    src_unique = list(dict.fromkeys(src_sentences))
    out_unique = list(dict.fromkeys(out_sentences))
    global_mode = (len(src_unique) <= GLOBAL_COMPARE_MAX
                   and len(out_unique) <= GLOBAL_COMPARE_MAX)
    for index, after in enumerate(out_unique):
        if global_mode:
            candidates = src_unique
        else:
            candidates = src_unique[max(0, index - POLARITY_WINDOW):
                                    index + POLARITY_WINDOW + 1]
        best, best_sim = None, -1.0
        a_skel = out_profiles.skeleton[after]
        for before in candidates:
            sim = dice(src_profiles.skeleton[before], a_skel)
            if sim > best_sim:
                best, best_sim = before, sim
        if best is not None and best_sim >= RESTRICTION_SIM_THRESHOLD:
            pairs.append((best, after))
    return pairs


def _is_resegmentation(before: str, after: str) -> bool:
    """识别「重新分句」而不是「改写事实」。

    小说里把「。」换成「；」会让原句被拆成两句：其中一半是原句的**子串**。
    这不是事实翻转，把它判成翻转就是把语法改动当成事实改动（误报会让整条小说通路
    天天 REVIEW）。去掉标点与空白后互为子串即判定为重新分句，交给 L2 语义层判。
    """
    a = _NON_WORD_RE.sub("", before)
    b = _NON_WORD_RE.sub("", after)
    if not a or not b or a == b:
        return False
    return b in a or a in b


def _check_hedges(check: _Checker, level: str) -> None:
    src_text, out_text = check.src.text, check.out.text
    if src_text == out_text:
        return
    for word in _ABSOLUTE_WORDS:
        if word not in src_text and word in out_text:
            check.add(level, "absolute_assertion_added",
                      f"新增全量化/绝对化断言词: 凭空新增「{word}」，需人工/模型语义复核")
    for cls in HEDGE_CLASSES:
        before = check.src.hedge_counts[cls]
        after = check.out.hedge_counts[cls]
        if before != after:
            check.add(level, "hedge_class_changed",
                      f"限定词类别「{cls}」频次变化: 原 {before} 次，改写后 {after} 次，"
                      "需核验情态是否漂移")


def _check_paragraph_survival(check: _Checker, level: str) -> None:
    """段落数不再要求「必须相同才算通过」：减少只进 REVIEW，并由删除授权解释。

    判据是设计方案 57 行的「段落减少」= 内容单元**数量**减少，不是「哪一段被改了」：
    改写本来就会让每段字节变化，用文本相似度判删除会把合法压缩全部误报成删除。
    某段是不是可删的空壳由模型判断（:data:`REFUSED_JUDGEMENTS`），机械层只问有没有授权。
    """
    delta = check.src.paragraph_count - check.out.paragraph_count
    if delta <= 0:
        return
    inp = check.inp
    if not inp.authorizations:
        check.add(level, "paragraph_missing_without_authorization",
                  f"成稿比原文少 {delta} 个内容单元，且没有任何删除授权；"
                  "是否为可删空壳由模型判定，机械层不猜")
        return
    if len(tuple(inp.authorizations)) < delta:
        check.add(level, "paragraph_missing_without_authorization",
                  f"成稿比原文少 {delta} 个内容单元，删除授权只有 "
                  f"{len(tuple(inp.authorizations))} 条，数量对不上")
        return
    validation = _contract_problems(check, level, "check_deletions",
                                    check.src.paragraphs, check.out.paragraphs,
                                    inp.authorizations)
    if validation is None:
        return
    for issue in validation.rejected:
        check.add(level, issue.code, issue.detail)
    for issue in validation.issues:
        # 「段落减少」已经由上面的 delta 表达；这里只采纳授权自身的身份/合法性判定，
        # 避免同一件事被登记两次（Task 8 的 issues 口径也是 review 级）。
        if issue.code == "deletion_without_authorization":
            continue
        check.add(level, issue.code, issue.detail)


def _check_ledger(check: _Checker, level: str) -> None:
    inp = check.inp
    if not inp.ledger:
        return
    segments = tuple(inp.segments) or _fallback_segments(check.src.text)
    output_segments = tuple(inp.output_segments) or None
    validation = _contract_problems(check, level, "validate_ledger", inp.ledger, segments,
                                    second_semantic_review=inp.second_semantic_review,
                                    output_segments=output_segments)
    if validation is None:
        return
    for issue in tuple(validation.rejected) + tuple(validation.issues):
        check.add(level, issue.code, issue.detail)
    missing = [excerpt for excerpt in check.ledger_excerpts()
               if not check.out.covers(excerpt)]
    check.ops += len(missing)
    if not missing:
        return
    code = "registered_entity_unverified" if check.novel else "registered_entity_lost"
    for excerpt in missing[:3]:
        if check.novel:
            check.add(level, code,
                      f"登记的姓名/别名/地点「{excerpt}」在成稿里找不到原形；小说允许改用"
                      "别名，但是不是同一个人属 L2 判断，本层只报线索")
        else:
            check.add(level, code,
                      f"登记的专名「{excerpt}」在成稿里消失（人名/别名/地点逐字保护）")
    if len(missing) > 3:
        check.add(level, code, f"另有 {len(missing) - 3} 个登记专名同样缺失")


# ---------------------------------------------------------------- 公共入口


def build_index(text: str) -> TextIndex:
    return TextIndex(text)


def _indexes(inp: ValidationInput) -> tuple[TextIndex, TextIndex]:
    if not isinstance(inp.source_text, str) or not isinstance(inp.output_text, str):
        raise ValueError("ValidationInput 的原文/成稿必须是解码后的 str（Task 5 产物）")
    return build_index(inp.source_text), build_index(inp.output_text)


def _metrics(check: _Checker, extra: Mapping[str, int | float]) -> dict:
    data: dict = {
        "source_chars": check.src.size,
        "output_chars": check.out.size,
        "index_ms": round(check.src.elapsed_ms + check.out.elapsed_ms, 3),
        "key_ops": check.src.key_ops + check.out.key_ops + check.ops,
        "full_text_rescans": check.src.rescans + check.out.rescans,
        # Task 8 核实保护项实际扫过的字符数：full_text_rescans 只看 TextIndex 自己，
        # 看不到合同内部的逐条搜索，线性闸必须量这个（risk N5 修复轮 1）。
        "protection_scan_chars": check.protection_scan_chars,
        "hedge_passes": check.src.hedge_passes + check.out.hedge_passes,
        "index_scans": check.src.scans + check.out.scans,
        "headings": len(check.out.headings),
        "paragraphs": check.out.paragraph_count,
        "segments": len(tuple(check.inp.segments)),
        "protections": len(tuple(check.inp.protections)),
        "ledger_entries": len(tuple(check.inp.ledger)),
        "elapsed_ms": 0.0,
        "peak_memory_bytes": 0,
    }
    data.update(extra)
    return data


def collect_issues(inp: ValidationInput) -> list[Issue]:
    """跑 L0+L1，返回全部判定（含给人看的说明）；入口脚本据此打印可解释理由。"""
    src, out = _indexes(inp)
    check = _Checker(inp, src, out)
    _l0_checks(check)
    _l1_checks(check)
    return check.issues


def validate_l0(inp: ValidationInput) -> ValidationResult:
    src, out = _indexes(inp)
    check = _Checker(inp, src, out)
    _l0_checks(check)
    return _build_result("L0", check.level_issues("L0"), _metrics(check, {}),
                         noop=(inp.output_text == inp.source_text))


def validate_l1(inp: ValidationInput) -> ValidationResult:
    src, out = _indexes(inp)
    check = _Checker(inp, src, out)
    _l1_checks(check)
    return _build_result("L1", check.level_issues("L1"), _metrics(check, {}), noop=False)


def run_validation(inp: ValidationInput) -> tuple[ValidationResult, ValidationResult]:
    """一次建立两份索引，L0/L1 共用同一批判定；返回 (L0 结果, L1 结果)。"""
    started = time.perf_counter()
    tracing = tracemalloc.is_tracing()
    measuring = bool(inp.measure_memory)
    if measuring and not tracing:
        tracemalloc.start()
    try:
        src, out = _indexes(inp)
        check = _Checker(inp, src, out)
        _l0_checks(check)
        _l1_checks(check)
        peak = tracemalloc.get_traced_memory()[1] if measuring else 0
        shared = _metrics(check, {
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "peak_memory_bytes": int(peak),
            "numbers": len(out.numbers),
        })
        l0 = _build_result("L0", check.level_issues("L0"), shared,
                           noop=(inp.output_text == inp.source_text))
        l1 = _build_result("L1", check.level_issues("L1"), shared, noop=False)
        return l0, l1
    finally:
        if measuring and not tracing:
            tracemalloc.stop()


def mechanical_verdict(l0: ValidationResult, l1: ValidationResult) -> str:
    """L0/L1 的机械结论。``PASS_MECHANICAL`` 只覆盖这两层，含义见 MECHANICAL_SCOPE_NOTE。"""
    statuses = (l0.status, l1.status)
    if "ERROR" in statuses:
        return "ERROR"
    if "FAIL" in statuses:
        return "FAIL"
    if "NO-OP" in statuses:
        return "NO-OP"
    if "REVIEW" in statuses:
        return "REVIEW"
    return "PASS_MECHANICAL"


def publication_ready(*results) -> bool:
    """机械层永远说「还没到发布」：必须有 L2/L3 结果一起通过才行（Task 11）。"""
    if len(results) < 4:
        return False
    return all(getattr(item, "status", None) in ("PASS", "NO-OP") for item in results)


def self_check() -> int:
    """内置自检：四条脆弱规则的正反两向都要落在期望状态上。"""
    cases = [
        ("# 重要结论\n\n正文。\n", "# 重要决议\n\n正文。\n", "ordinary", FAIL),
        ("第一段。\n\n第二段。\n\n第三段。\n", "第一段。\n\n第三段。\n", "ordinary", REVIEW),
        ("系统升级将在近期完成。\n",
         "系统升级将在近期完成，升级期间所有历史数据将由第三方服务商全量迁移至"
         "境外灾备中心并保留十年。\n", "ordinary", REVIEW),
        ("林晚说：“我不去。”\n\n他低头擦剑。\n",
         "林晚说：“我不去。”\n\n他低头擦剑。\n", "novel", "PASS"),
        ("天剑宗外门弟子林晚又一次被罚打扫藏经阁。他把扫帚靠在门边。\n",
         "天剑宗外门弟子林晚又一次被罚打扫藏经阁。扫帚靠回门边。\n", "novel", "PASS"),
    ]
    failures = []
    for source, output, scene, expected in cases:
        l0, l1 = run_validation(ValidationInput(source, output,
                                                ValidationPolicy(scene, "balanced")))
        got = mechanical_verdict(l0, l1)
        if expected == FAIL and got != FAIL:
            failures.append(f"{scene}: 期望 FAIL，实得 {got} {l0.codes}{l1.codes}")
        elif expected == REVIEW and got not in (REVIEW, FAIL):
            failures.append(f"{scene}: 期望 REVIEW，实得 {got} {l0.codes}{l1.codes}")
        elif expected == "PASS" and got not in ("PASS_MECHANICAL", "NO-OP"):
            failures.append(f"{scene}: 期望机械通过，实得 {got} {l0.codes}{l1.codes}")
    for code in TASK8_REVIEW_CODES:
        if SEVERITY.get(code) != REVIEW:
            failures.append(f"Task 8 复核码 {code} 的严重度不是 REVIEW")
    if failures:
        for line in failures:
            print("SELF-CHECK FAILED:", line)
        return 1
    print(f"novel_validation 自检通过：{len(cases)} 例双向判定 + "
          f"{len(ALL_CODES)} 个判定码，覆盖 L0/L1。")
    print("含义提醒：", MECHANICAL_SCOPE_NOTE)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(self_check())
