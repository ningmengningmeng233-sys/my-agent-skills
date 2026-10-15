#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""novel_contracts.py - 来源锚点、约束账本、保护清单、删除授权与文风样本契约（v1.2 Task 8）。

**Python 的边界**（设计方案 45-57 行「有来源的约束账本」+「保护清单与删除授权」，逐字绑定）：
账本、保护清单、删除授权和文风样本特征都由**模型生成**；本模块只做
「schema、唯一性、哈希、来源锚点和恢复校验」。它不理解伏笔、人物关系或声线，
因此它的 PASS 只代表「来源可核、形状合法、身份唯一」，**不代表**陈述在语义上正确
（spec 50 行：带原文锚点的陈述仍需模型复核）。Task 10/11 不得把本模块的判定当作语义结论。

四个契约类型逐字段等于计划 Task 8 的规范：``SourceAnchor``、``LedgerEntry``、
``ProtectionEntry``、``DeletionAuthorization``（字段名、顺序、字面量集合都不增不减）。
辅助类型 ``ContractIssue`` / ``LedgerValidation`` / ``ProtectionValidation`` /
``DeletionValidation`` / ``VoiceSampleValidation`` / ``SampleFact`` /
``VoiceSampleContract`` 只是判定载体与文风样本合同，不引入新的枚举成员。

绑定行为 → 实现位置：

  1. 每条账本条目必须有 ≥1 个可核实的来源锚点（segment_id + 原文片段 + 片段哈希 +
     出现序号）；**找不到来源的条目拒绝**，不修好再用（spec 49 行）。账本一侧的
     ``start/end`` 除范围合法外还要与片段**实际定位到的位置**对上，对不上就是
     ``anchor_coordinates_stale`` → REVIEW（Task 10/11 可能按坐标切片取原文）；
     本模块**不修坐标**，修复归人工/模型。
  2. 含糊信息只能是 ``certainty="ambiguous"``；验证器**永不**把它升级成 certain，
     重跑多少次都还是 ambiguous（``validate_ledger`` 只做单向降级 certain→ambiguous）。
  3. 对剧情敏感的新条目必须先有**第二次语义复核**证据才可能被当作确定事实（spec 49 行）：
     ``second_semantic_review`` 传入已复核的 claim_id 集合。没证据时条目进 review 且被
     降级为 ambiguous。这里刻意比 spec 更严：所有 certain 的剧情敏感条目都要证据，
     因为「是不是新条目」本身是语义判断，Python 不猜（``NEW_STATE_MARKERS`` 只决定
     给出哪个判定码，不决定是否放行）。
  4. ``voice`` 条目只作软参考：可以防止角色趋同，**不能**强迫口头禅——
     ``operation="preserve"``（逐字强制复用）被拒绝；其余 voice 条目进 ``soft`` 集合，
     消费方不得据其强制复现任何短语。
  5. 保护清单收真实引文、资料引用（同走 ``quotation``）、用户锁定文字、代码、URL、标题。
     **字符起止坐标只作诊断**（risk N4）：身份是「分段 + 内容哈希 + 出现序号」，
     ``original_position`` 与 ``anchor.start/end`` 都不参与定位，所以正文前部增删之后
     保护项仍按内容与出现序号定位得到（``output_text`` 一侧同理，只按内容计数）。
     坐标越界只说明锚点是凭空写的 → 拒绝；坐标合法但已失真 → 照常定位。
  6. 普通小说对白**不自动锁定**（``ordinary_dialogue_not_protectable`` 拒绝）；
     对白与资料引文**无法区分**时，由模型把那些 entry_id 交给 ``indistinguishable`` →
     条目进 REVIEW，Python 不自行裁决它是哪种。**REVIEW 级旗标（``indistinguishable``
     与 ``degraded_segment_anchor``）压过删除授权**：被旗标的项即便缺失有授权可解释，
     也只进 review 不进 released，所以带旗标的输入永远不会给出 PASS。
  7. 整段删除必须有 ``DeletionAuthorization``：段落 ID + 原文哈希 + 删除理由 +
     独立事实检查 + 承接作用检查 + 语义复核，三项全 ``pass`` 才算授权成立。
     缺授权的段落减少 → REVIEW；**机械验证器不自行猜测段落是否为空壳**——
     看起来像空壳的段落（纯标点、省略号、单字）照样要授权。
  8. 文风样本合同只允许句长、节奏、正式度、对话密度四类特征（``SAMPLE_FEATURES``）；
     样本的专名、数字、事件或观点出现在成稿而**不出现在原文**时返回 REVIEW
     （spec「禁止带入样本事实」）。事实清单由模型给出，Python 只做子串核实。
  9. 消费 Task 7 的 ``Segment``：坐标口径是该段在解码后 ``str`` 上的码点半开区间
     ``[Segment.start, Segment.end)``；``Segment.needs_review`` 为真的降级段**不得**被
     当成干净锚点来源——依赖它的条目一律进 REVIEW 而不是 accepted。

重复 ID 一律拒绝（``duplicate_claim_id`` / ``duplicate_entry_id`` /
``duplicate_authorization`` / ``duplicate_fact_id`` / ``duplicate_anchor``），
ID 由模型给定、验证器原样保留，因此同一输入重跑结果逐字段相同（ID 稳定）。

判定口径（四个验证器一致）：``issues`` **只带 review 级判定**，拒绝项一律只进
``rejected``，不在两处重复登记；``ContractIssue.index`` 是该验证器主输入序列里的
位置，非主输入（删除授权）的判定用 ``AUTH_ISSUE_INDEX`` 哨兵、位置写进说明，
免得与「第 0 项保护项」的 index 撞车。

确定性：本模块**只校数据**——不调模型、不读写文件、不改环境变量、无全局状态；
仅标准库；所有判定码取自下面这份固定清单（每个码都必须在本 docstring 中出现）：

  bad_payload missing_field unknown_field bad_segments duplicate_segment_id
  bad_entries bad_protections bad_paragraphs
  bad_claim_id bad_category bad_certainty bad_operation bad_state bad_statement
  duplicate_claim_id no_source_anchor bad_anchor duplicate_anchor unknown_segment
  anchor_out_of_range anchor_hash_mismatch anchor_excerpt_not_found
  anchor_occurrence_out_of_range anchor_coordinates_stale
  degraded_segment_anchor certainty_ambiguous
  plot_sensitive_new_needs_second_review plot_sensitive_needs_second_review
  voice_cannot_force_preserve voice_is_soft_reference
  bad_entry_id duplicate_entry_id bad_kind ordinary_dialogue_not_protectable
  bad_original_text protection_hash_mismatch protection_text_mismatch
  occurrence_index_mismatch bad_original_position bad_allowed_scope
  indistinguishable_dialogue_citation protection_missing_from_output
  protection_count_shortfall protection_released_by_authorization
  bad_paragraph_id bad_authorization_hash bad_reason bad_check_value
  duplicate_authorization authorization_check_failed deletion_without_authorization
  authorization_unmatched authorization_identity_mismatch
  bad_sample_id bad_feature no_sample_features sample_feature_not_allowed
  bad_fact_id bad_fact_kind bad_fact_text duplicate_fact_id
  sample_fact_in_output_not_in_source

``python -B -X utf8 scripts/novel_contracts.py`` 是内置自检：拿一组真实分段与病态
数据走一遍所有闸门，末尾打印「自检：PASS」；任何断言失败都以非零码退出。
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from typing import Literal

from novel_segments import Segment

# ---- 规范字面量集合（brief 原文，不增不减） --------------------------------
LEDGER_CATEGORIES = ("name", "alias", "relationship", "viewpoint", "time", "place",
                     "world-rule", "secret", "foreshadowing", "character-state", "voice")
CERTAINTIES = ("certain", "ambiguous")
LEDGER_OPERATIONS = ("preserve", "compress", "reorder", "rewrite")
PROTECTION_KINDS = ("quotation", "user-locked", "code", "url", "heading")
CHECK_VALUES = ("pass", "fail")
SAMPLE_FEATURES = ("sentence-length", "rhythm", "formality", "dialogue-density")
SAMPLE_FACT_KINDS = ("proper-noun", "number", "event", "opinion")

# 剧情敏感类别：影响后续剧情走向的事实。姓名/别名/地点与 voice 不在其列
# （前三类是身份与场所事实，voice 只作软参考）。
PLOT_SENSITIVE_CATEGORIES = ("relationship", "viewpoint", "time", "world-rule",
                             "secret", "foreshadowing", "character-state")
SOFT_CATEGORIES = ("voice",)
# state 是模型填的自由文本。这张词表只决定「用哪个判定码报告」，不决定是否放行；
# 词表之外的 state 同样按 fail-closed 要求复核证据（见模块 docstring 第 3 条）。
NEW_STATE_MARKERS = ("new", "added", "新增", "新加入", "初次出现")
# 有人想把普通对白写进保护清单时命中的 kind 词形（大小写不敏感）。
DIALOGUE_KIND_CLAIMS = ("dialogue", "对白", "对话", "conversation", "speech", "line")

MAX_ID_CHARS = 128
MAX_STATEMENT_CHARS = 2000
MAX_STATE_CHARS = 160
MAX_SCOPE_CHARS = 200
MAX_REASON_CHARS = 500
MAX_FACT_CHARS = 500

_ID_RE = re.compile(rf"^[^\s\x00-\x1f\x7f]{{1,{MAX_ID_CHARS}}}$")
_HEX_RE = re.compile(r"[0-9a-fA-F]{64}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")

#: 本模块可能给出的全部判定码（顺序不承载语义，仅便于比对与文档核对）。
ALL_CODES = (
    "bad_payload", "missing_field", "unknown_field", "bad_segments",
    "duplicate_segment_id", "bad_entries", "bad_protections", "bad_paragraphs",
    "bad_claim_id", "bad_category", "bad_certainty", "bad_operation", "bad_state",
    "bad_statement", "duplicate_claim_id", "no_source_anchor", "bad_anchor",
    "duplicate_anchor", "unknown_segment", "anchor_out_of_range",
    "anchor_hash_mismatch", "anchor_excerpt_not_found",
    "anchor_occurrence_out_of_range", "anchor_coordinates_stale",
    "degraded_segment_anchor", "certainty_ambiguous",
    "plot_sensitive_new_needs_second_review", "plot_sensitive_needs_second_review",
    "voice_cannot_force_preserve", "voice_is_soft_reference", "bad_entry_id",
    "duplicate_entry_id", "bad_kind", "ordinary_dialogue_not_protectable",
    "bad_original_text", "protection_hash_mismatch", "protection_text_mismatch",
    "occurrence_index_mismatch", "bad_original_position", "bad_allowed_scope",
    "indistinguishable_dialogue_citation", "protection_missing_from_output",
    "protection_count_shortfall", "protection_released_by_authorization",
    "bad_paragraph_id", "bad_authorization_hash", "bad_reason", "bad_check_value",
    "duplicate_authorization", "authorization_check_failed",
    "deletion_without_authorization", "authorization_unmatched",
    "authorization_identity_mismatch", "bad_sample_id", "bad_feature",
    "no_sample_features", "sample_feature_not_allowed", "bad_fact_id",
    "bad_fact_kind", "bad_fact_text", "duplicate_fact_id",
    "sample_fact_in_output_not_in_source",
)


# ---------------------------------------------------------------- 契约类型


@dataclass(frozen=True)
class SourceAnchor:
    segment_id: str
    start: int
    end: int
    excerpt: str
    text_sha256: str
    occurrence_index: int


@dataclass(frozen=True)
class LedgerEntry:
    claim_id: str
    category: Literal["name", "alias", "relationship", "viewpoint", "time", "place",
                      "world-rule", "secret", "foreshadowing", "character-state", "voice"]
    statement: str
    certainty: Literal["certain", "ambiguous"]
    state: str
    output_anchor: SourceAnchor
    source_anchors: tuple[SourceAnchor, ...]
    operation: Literal["preserve", "compress", "reorder", "rewrite"]


@dataclass(frozen=True)
class ProtectionEntry:
    entry_id: str
    kind: Literal["quotation", "user-locked", "code", "url", "heading"]
    anchor: SourceAnchor
    original_text: str
    text_sha256: str
    occurrence_index: int
    original_position: tuple[int, int]
    allowed_scope: str


@dataclass(frozen=True)
class DeletionAuthorization:
    paragraph_id: str
    text_sha256: str
    reason: str
    independent_fact_check: Literal["pass", "fail"]
    transition_check: Literal["pass", "fail"]
    semantic_review: Literal["pass", "fail"]


@dataclass(frozen=True)
class SampleFact:
    fact_id: str
    kind: Literal["proper-noun", "number", "event", "opinion"]
    text: str


@dataclass(frozen=True)
class VoiceSampleContract:
    sample_id: str
    features: tuple[str, ...]
    facts: tuple[SampleFact, ...]


@dataclass(frozen=True)
class ContractIssue:
    """一条判定：第几个输入、哪个主体、哪个码、给人看的说明。"""
    index: int
    subject_id: str
    code: str
    detail: str


@dataclass(frozen=True)
class LedgerValidation:
    accepted: tuple = ()      # 机械核实通过（仍需模型复核，见模块 docstring）
    review: tuple = ()        # 降级段来源 / ambiguous / 未过第二次复核
    soft: tuple = ()          # voice 条目：只作防趋同参考，不得强制复现
    rejected: tuple = ()      # ContractIssue：无来源、ID 重复、非法字面量等
    issues: tuple = ()        # ContractIssue：review 级说明（拒绝项只进 rejected，不重复）

    @property
    def status(self) -> str:
        return _status(self.rejected, self.review)


@dataclass(frozen=True)
class ProtectionValidation:
    accepted: tuple = ()
    released: tuple = ()      # 因删除授权而允许缺失的保护项
    review: tuple = ()
    rejected: tuple = ()
    issues: tuple = ()

    @property
    def status(self) -> str:
        return _status(self.rejected, self.review)


@dataclass(frozen=True)
class DeletionValidation:
    authorized: tuple = ()    # 被采纳的 DeletionAuthorization
    review: tuple = ()        # ContractIssue：无授权的段落减少等
    rejected: tuple = ()      # ContractIssue：授权本身不合法
    issues: tuple = ()

    @property
    def status(self) -> str:
        return _status(self.rejected, self.review)


@dataclass(frozen=True)
class VoiceSampleValidation:
    features: tuple = ()
    clean: tuple = ()
    review: tuple = ()        # SampleFact：出现在成稿却不在原文
    rejected: tuple = ()
    issues: tuple = ()

    @property
    def status(self) -> str:
        return _status(self.rejected, self.review)


class ContractError(RuntimeError):
    """调用方把数据喂错了（不是判定问题）：非法分段、重复 segment_id、非序列输入。

    ``details`` 是承重的：每个 raise 点都必须把病灶身份（segment_id / label / kind /
    坐标 / 计数）塞进去，排障时不必重跑就能定位到是哪一段、哪一个入参。测试
    ``test_every_contract_error_passes_details`` 用 AST 守住这一点——少一个 raise 点
    带 details 就变红，dead flexibility 不会悄悄长回来。
    """

    def __init__(self, code: str, message: str, **details):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = details


# ---------------------------------------------------------------- 基础工具


def sha256_text(text: str) -> str:
    """内容身份用的哈希：UTF-8 字节 SHA-256 小写十六进制。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _status(rejected, review) -> str:
    if rejected:
        return "FAIL"
    return "REVIEW" if review else "PASS"


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bad_id(value) -> bool:
    return not isinstance(value, str) or not _ID_RE.match(value)


def _bad_label(value, max_chars: int, *, allow_newline: bool = False) -> bool:
    """短标签（state / scope / reason / fact）：非空、有上界、无控制字符。

    上界不是洁癖：这些字段会被写进账本文件与报告，无界自由文本等于让正文
    （甚至注入提示词）混进元数据（Task 6 Important 4 同源）。
    """
    if not isinstance(value, str) or len(value) > max_chars:
        return True
    probe = value.replace("\n", "") if allow_newline else value
    if not probe.strip():
        return True
    return bool(_CONTROL_RE.search(probe))


def _occurrences(haystack: str, needle: str) -> list:
    """非重叠出现位置（码点下标）；确定性、无正则回溯风险。"""
    if not needle:
        return []
    positions = []
    start = haystack.find(needle)
    while start != -1:
        positions.append(start)
        start = haystack.find(needle, start + len(needle))
    return positions


#: 诊断计数（不参与任何判定）：核实文本时**实际扫过的字符数**与被查 needle 条数。
#: 逐条直查按「条数 × 文本长度」记，一次建桶只记一遍文本长度——risk N5 的线性闸
#: 要盯的就是这个乘积是否随保护项条数长大（``full_text_rescans`` 看不到这里）。
SCAN_STATS: dict = {"scan_chars": 0, "needle_lookups": 0}

#: 条数很少时逐条 C 级 ``find`` 比 Python 级建桶更便宜；超过就必须一次建桶。
_BUCKET_THRESHOLD = 8

#: 桶键宽度。2 字键在「同一句话反复出现」的网文里会挤成一个巨型桶（保护项
#: 「约定4号」与「约定40号」共享前缀时，逐条验证退化成条数 × 桶大小），所以按
#: 4 字建键；不足 4 字的原文条目太少、又太常见，留在逐条直查那一侧。
_BUCKET_KEY_WIDTH = 4


def _prefix_buckets(haystack: str, keys, width: int) -> dict:
    """一遍扫全文，按 needle 的前 ``width`` 字建位置桶（与 TextIndex.warm 同型）。

    桶里保留**重叠**位置：键自重叠（如「aa」）时非重叠扫法会漏掉后面那次出现，
    从而漏数 needle。
    """
    buckets: dict = {}
    for index in range(len(haystack) - width + 1):
        key = haystack[index:index + width]
        if key in keys:
            bucket = buckets.get(key)
            if bucket is None:
                buckets[key] = [index]
            else:
                bucket.append(index)
    return buckets


def _greedy_count(haystack: str, needle: str, positions) -> int:
    """候选位置升序取最左非重叠次数：与 :func:`_occurrences` 的结果逐字等价。"""
    count, limit = 0, -1
    for index in positions:
        if index < limit or not haystack.startswith(needle, index):
            continue
        count += 1
        limit = index + len(needle)
    return count


def _occurrence_counts(haystack: str, needles) -> dict:
    """一批 needle 在最左非重叠口径下的出现次数。

    为什么不是「每条保护项各扫一遍全文」：网文里保护项随篇幅增长，逐条重扫就是
    平方级（实测 1M 字符 × 4000 条 = 33 秒）。这里全文最多扫一遍。
    """
    distinct = tuple(dict.fromkeys(n for n in needles if n))
    if not distinct:
        return {}
    if not haystack:
        return {needle: 0 for needle in distinct}
    counts: dict = {}
    bucketed = tuple(n for n in distinct if len(n) >= _BUCKET_KEY_WIDTH)
    direct = tuple(n for n in distinct if len(n) < _BUCKET_KEY_WIDTH)
    if len(bucketed) > _BUCKET_THRESHOLD:
        buckets = _prefix_buckets(haystack, {needle[:_BUCKET_KEY_WIDTH] for needle in bucketed},
                                  _BUCKET_KEY_WIDTH)
        SCAN_STATS["scan_chars"] += len(haystack)
        for needle in bucketed:
            counts[needle] = _greedy_count(
                haystack, needle, buckets.get(needle[:_BUCKET_KEY_WIDTH], ()))
    else:
        direct = direct + bucketed          # 条数太少，不值得为它扫一遍 Python
        bucketed = ()
    if direct:
        # C 级逐条 find：比 Python 建桶更便宜，也覆盖不足键宽的短原文。
        for needle in direct:
            counts[needle] = len(_occurrences(haystack, needle))
        SCAN_STATS["scan_chars"] += len(haystack) * len(direct)
    SCAN_STATS["needle_lookups"] += len(distinct)
    return counts


def _as_record(item, cls):
    """dataclass 实例或模型 JSON 映射 → 字段字典；返回 (dict|None, 判定列表)。"""
    names = tuple(f.name for f in fields(cls))
    if isinstance(item, cls):
        return {name: getattr(item, name) for name in names}, ()
    if isinstance(item, Mapping):
        data = dict(item)
        missing = sorted(name for name in names if name not in data)
        unknown = sorted(name for name in data if name not in names)
        problems = []
        if missing:
            problems.append(("missing_field", f"{cls.__name__} 缺字段: {','.join(missing)}"))
        if unknown:
            problems.append(("unknown_field",
                             f"{cls.__name__} 含未知字段: {','.join(unknown)}"))
        if problems:
            return None, tuple(problems)
        return data, ()
    return None, (("bad_payload",
                   f"必须是 {cls.__name__} 或映射，收到 {type(item).__name__}"),)


def _sequence(value) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _index_segments(segments, label: str) -> dict:
    """Task 7 分段 → id 索引，并校坐标口径（码点半开区间与 text 长度一致）。"""
    if not _sequence(segments):
        raise ContractError("bad_segments", f"{label}必须是 Segment 序列",
                            label=label, kind=type(segments).__name__)
    by_id: dict = {}
    for segment in segments:
        if not isinstance(segment, Segment):
            raise ContractError("bad_segments",
                                f"{label}含非 Segment 元素: {type(segment).__name__}",
                                label=label, element_type=type(segment).__name__)
        if segment.start < 0 or segment.end - segment.start != len(segment.text):
            raise ContractError("bad_segments",
                                f"{label}段 {segment.segment_id} 的坐标与文本长度不一致",
                                label=label, segment_id=segment.segment_id,
                                start=segment.start, end=segment.end,
                                length=len(segment.text))
        if segment.segment_id in by_id:
            raise ContractError("duplicate_segment_id",
                                f"{label}出现重复 segment_id: {segment.segment_id}",
                                label=label, segment_id=segment.segment_id)
        by_id[segment.segment_id] = segment
    return by_id


# ---------------------------------------------------------------- 锚点校验


def _validate_anchor(anchor, segments_by_id, *, role: str, resolve: bool = True):
    """返回 (SourceAnchor|None, 拒绝判定, 复核备注)。

    解析出的出现位置不外传：需要时消费方用 :func:`_occurrences` 自行重算，
    多一份返回值就多一处会失同步的账。
    """
    data, problems = _as_record(anchor, SourceAnchor)
    if problems:
        return None, (("bad_anchor",
                       f"{role}锚点形状不合法: {problems[0][1]}"),), ()
    segment_id = data["segment_id"]
    start, end = data["start"], data["end"]
    excerpt, digest = data["excerpt"], data["text_sha256"]
    occurrence = data["occurrence_index"]
    bad = []
    if _bad_id(segment_id):
        bad.append("segment_id 必须是非空、无空白与控制字符的短 ID")
    if not (_is_int(start) and _is_int(end) and _is_int(occurrence)):
        bad.append("start/end/occurrence_index 必须是整数")
    elif occurrence < 0:
        bad.append("出现序号不能为负")
    if not isinstance(excerpt, str) or not excerpt:
        bad.append("原文片段不能为空")
    if not isinstance(digest, str) or not _HEX_RE.fullmatch(digest):
        bad.append("片段哈希必须是 64 位十六进制 SHA-256")
    if bad:
        return None, (("bad_anchor", f"{role}锚点不合法: " + "；".join(bad)),), ()
    built = SourceAnchor(segment_id=segment_id, start=start, end=end, excerpt=excerpt,
                        text_sha256=digest.lower(), occurrence_index=occurrence)
    if not resolve:
        # 没有输出分段可对照时只校形状；不假装已经核实过内容。
        return built, (), ()
    segment = segments_by_id.get(segment_id)
    if segment is None:
        return built, (("unknown_segment",
                        f"{role}锚点指向不存在的分段 {segment_id}"),), ()
    rejected = []
    if not (segment.start <= start < end <= segment.end):
        rejected.append(("anchor_out_of_range",
                         f"{role}锚点坐标 ({start}, {end}) 不在分段 {segment_id} "
                         f"[{segment.start}, {segment.end}) 内"))
    if sha256_text(excerpt) != built.text_sha256:
        rejected.append(("anchor_hash_mismatch",
                         f"{role}锚点片段哈希与片段内容不符（分段 {segment_id}）"))
    positions = _occurrences(segment.text, excerpt)
    if not positions:
        rejected.append(("anchor_excerpt_not_found",
                         f"{role}锚点片段在分段 {segment_id} 里找不到，条目无来源"))
    elif occurrence >= len(positions):
        rejected.append(("anchor_occurrence_out_of_range",
                         f"{role}锚点出现序号 {occurrence} 超出该片段在分段 "
                         f"{segment_id} 内的出现次数 {len(positions)}"))
    if rejected:
        return built, tuple(rejected), ()
    notes = ()
    if segment.needs_review:
        notes = (("degraded_segment_anchor",
                  f"分段 {segment_id} 带 needs_review 旗标（Task 7 按单元降级），"
                  "不能当作干净锚点来源使用"),)
    return built, (), notes


# ---------------------------------------------------------------- 账本校验


def _stale_coordinate_notes(anchors, segments_by_id):
    """账本 start/end 与片段实际定位到的位置不符 → (码, 说明)；**本模块不修坐标**。

    :func:`_validate_anchor` 只校坐标落在所属分段内（范围合法）。但 Task 10/11 可能
    直接按 start/end 切片取原文：合法却失真的坐标一旦被当成干净数据交给下游，切出来
    的就是另一段话。所以这里按 REVIEW 级如实报告（spec 50 行：带原文锚点的陈述仍需
    模型复核），把它「改对」不是机械验证器的活。

    只用于账本：保护项一侧的 ``anchor.start/end`` 与 ``original_position`` 按 risk N4
    明确「只作诊断」，正文前部增删后仍要照常定位得到，不得因此进 REVIEW。
    """
    notes = []
    for anchor in anchors:
        segment = segments_by_id.get(anchor.segment_id)
        if segment is None:
            continue
        positions = _occurrences(segment.text, anchor.excerpt)
        if anchor.occurrence_index >= len(positions):
            continue
        located = segment.start + positions[anchor.occurrence_index]
        expected = (located, located + len(anchor.excerpt))
        if (anchor.start, anchor.end) != expected:
            notes.append(("anchor_coordinates_stale",
                          f"锚点坐标 ({anchor.start}, {anchor.end}) 与片段在分段 "
                          f"{anchor.segment_id} 内第 {anchor.occurrence_index} 次出现的"
                          f"实际位置 {expected} 不符；坐标不修复，交人工/模型复核"))
    return notes


def validate_ledger(entries, segments, *, second_semantic_review=(),
                    output_segments=None) -> LedgerValidation:
    """核实模型生成的账本条目；返回 accepted / review / soft / rejected。

    ``second_semantic_review`` 是**已通过第二次语义复核**的 claim_id 集合；
    ``output_segments`` 给出时 output_anchor 同样按内容核实，否则只校形状。
    """
    by_id = _index_segments(segments, "来源分段")
    out_by_id = (None if output_segments is None
                 else _index_segments(output_segments, "输出分段"))
    if not _sequence(entries):
        raise ContractError("bad_entries", "entries 必须是账本条目序列",
                            kind=type(entries).__name__)
    reviewed = frozenset(item for item in second_semantic_review if isinstance(item, str))
    accepted, review, soft, rejected, issues = [], [], [], [], []
    seen: set = set()

    for index, item in enumerate(entries):
        data, shape = _as_record(item, LedgerEntry)
        if shape:
            subject = "" if data is None else str(data.get("claim_id", ""))
            for code, detail in shape:
                rejected.append(ContractIssue(index, subject, code, detail))
            continue

        hard, notes = [], []
        claim_id = data["claim_id"]
        if _bad_id(claim_id):
            hard.append(("bad_claim_id", "条目 ID 必须非空、无空白与控制字符、≤128 字符"))
        else:
            if claim_id in seen:
                hard.append(("duplicate_claim_id", f"条目 ID 重复: {claim_id}"))
            seen.add(claim_id)
        category = data["category"]
        if category not in LEDGER_CATEGORIES:
            hard.append(("bad_category", f"类别不在规范集合内: {category!r}"))
        certainty = data["certainty"]
        if certainty not in CERTAINTIES:
            hard.append(("bad_certainty", f"确定程度只能是 certain/ambiguous: {certainty!r}"))
        operation = data["operation"]
        if operation not in LEDGER_OPERATIONS:
            hard.append(("bad_operation", f"操作不在规范集合内: {operation!r}"))
        if _bad_label(data["state"], MAX_STATE_CHARS):
            hard.append(("bad_state", f"状态必须是非空短标签（≤{MAX_STATE_CHARS} 字符）"))
        if _bad_label(data["statement"], MAX_STATEMENT_CHARS, allow_newline=True):
            hard.append(("bad_statement",
                         f"陈述必须非空且 ≤{MAX_STATEMENT_CHARS} 字符"))

        anchors_raw = data["source_anchors"]
        built_anchors, degraded = [], False
        if not _sequence(anchors_raw) or not anchors_raw:
            hard.append(("no_source_anchor", "每条账本至少要有 1 个来源锚点"))
        else:
            identities = set()
            for raw in anchors_raw:
                anchor, problems, anchor_notes = _validate_anchor(raw, by_id, role="")
                if problems:
                    hard.extend(problems)
                    continue
                built_anchors.append(anchor)
                identity = (anchor.segment_id, anchor.text_sha256, anchor.occurrence_index)
                if identity in identities:
                    hard.append(("duplicate_anchor",
                                 f"同一身份的分段+哈希+出现序号重复登记: {identity[0]}"))
                identities.add(identity)
                if anchor_notes:
                    degraded = True
        built_output, output_problems, _ = _validate_anchor(
            data["output_anchor"], out_by_id if out_by_id is not None else {},
            role="输出", resolve=out_by_id is not None)
        if output_problems:
            hard.extend(output_problems)

        if category in SOFT_CATEGORIES and operation == "preserve":
            hard.append(("voice_cannot_force_preserve",
                         "voice 条目只能作软参考：preserve 等于强迫角色复现口头禅"))

        if hard:
            for code, detail in _ordered(hard):
                rejected.append(ContractIssue(index,
                                              claim_id if isinstance(claim_id, str) else "",
                                              code, detail))
            continue

        entry = LedgerEntry(claim_id=claim_id, category=category,
                            statement=data["statement"], certainty=certainty,
                            state=data["state"], output_anchor=built_output,
                            source_anchors=tuple(built_anchors), operation=operation)

        stale = _stale_coordinate_notes(built_anchors, by_id)
        if out_by_id is not None:
            stale = stale + _stale_coordinate_notes([built_output], out_by_id)

        if degraded or stale:
            review.append(entry)
            if degraded:
                issues.append(ContractIssue(index, claim_id, "degraded_segment_anchor",
                                            "锚点所在分段被 Task 7 标为待复核"))
            for code, detail in stale:
                issues.append(ContractIssue(index, claim_id, code, detail))
        elif certainty == "ambiguous":
            review.append(entry)
            issues.append(ContractIssue(index, claim_id, "certainty_ambiguous",
                                        "含糊条目按 spec 49 行留在 REVIEW，永不升级为确定事实"))
        elif category in PLOT_SENSITIVE_CATEGORIES and certainty == "certain" \
                and claim_id not in reviewed:
            marker = str(data["state"]).strip().casefold()
            code = ("plot_sensitive_new_needs_second_review"
                    if marker in NEW_STATE_MARKERS else "plot_sensitive_needs_second_review")
            review.append(replace(entry, certainty="ambiguous"))
            issues.append(ContractIssue(index, claim_id, code,
                                        "剧情敏感条目在第二次语义复核之前不得被当作确定事实"))
        else:
            accepted.append(entry)
            if category in SOFT_CATEGORIES:
                soft.append(entry)
                issues.append(ContractIssue(index, claim_id, "voice_is_soft_reference",
                                            "voice 条目仅作防趋同的软参考，不得强制复现"))

    return LedgerValidation(accepted=tuple(accepted), review=tuple(review),
                            soft=tuple(soft), rejected=tuple(rejected),
                            issues=tuple(issues))


def _ordered(problems):
    """同一主体的判定按码排序去重，保证重跑逐字段相同。"""
    seen, out = set(), []
    for code, detail in sorted(problems, key=lambda pair: pair[0]):
        if code in seen:
            continue
        seen.add(code)
        out.append((code, detail))
    return out


# -------------------------------------------------------------- 保护清单校验


#: ``validate_protections`` 的主输入序列是 protections；授权判定用哨兵索引，
#: 免得「第 n 项授权」的判定与「第 n 项保护项」的 index 撞成同一个数（消费方按
#: index 归并判定时会把两件事当成一件）。授权位置写进说明，主体写 paragraph_id。
AUTH_ISSUE_INDEX = -1


def _protection_release_hashes(authorizations):
    """从授权集合里取出「可以解释某段内容缺失」的哈希；不合法的授权自己报错。"""
    usable, notes = set(), []
    if not _sequence(authorizations):
        raise ContractError("bad_protections", "authorizations 必须是授权序列",
                            count=len(authorizations) if authorizations is not None else None,
                            kind=type(authorizations).__name__)
    for offset, item in enumerate(authorizations):
        position = f"（第 {offset + 1} 项授权）"
        data, shape = _as_record(item, DeletionAuthorization)
        if shape:
            notes.extend(ContractIssue(AUTH_ISSUE_INDEX, "", code, detail + position)
                         for code, detail in shape)
            continue
        issues = _authorization_problems(data)
        if issues:
            notes.extend(ContractIssue(AUTH_ISSUE_INDEX, str(data["paragraph_id"]),
                                       code, detail + position)
                         for code, detail in issues)
            continue
        if not all(data[name] == "pass" for name in
                   ("independent_fact_check", "transition_check", "semantic_review")):
            notes.append(ContractIssue(AUTH_ISSUE_INDEX, data["paragraph_id"],
                                       "authorization_check_failed",
                                       "三项检查未全部通过，授权不能解释内容缺失" + position))
            continue
        usable.add(str(data["text_sha256"]).lower())
    return usable, notes


def _authorization_problems(data) -> list:
    problems = []
    if _bad_id(data["paragraph_id"]):
        problems.append(("bad_paragraph_id", "段落 ID 必须非空、无空白与控制字符、≤128 字符"))
    digest = data["text_sha256"]
    if not isinstance(digest, str) or not _HEX_RE.fullmatch(digest):
        problems.append(("bad_authorization_hash", "原文哈希必须是 64 位十六进制 SHA-256"))
    else:
        data["text_sha256"] = digest.lower()
    if _bad_label(data["reason"], MAX_REASON_CHARS, allow_newline=True):
        problems.append(("bad_reason", f"删除理由必须非空且 ≤{MAX_REASON_CHARS} 字符"))
    for name in ("independent_fact_check", "transition_check", "semantic_review"):
        if data[name] not in CHECK_VALUES:
            problems.append(("bad_check_value", f"{name} 只能是 pass/fail: {data[name]!r}"))
    return problems


def _authorization_from(data) -> DeletionAuthorization:
    return DeletionAuthorization(
        paragraph_id=data["paragraph_id"], text_sha256=data["text_sha256"],
        reason=data["reason"], independent_fact_check=data["independent_fact_check"],
        transition_check=data["transition_check"], semantic_review=data["semantic_review"])


def validate_protections(protections, segments, *, output_text=None, authorizations=(),
                        indistinguishable=()) -> ProtectionValidation:
    """核实保护清单：真实引文/资料引用/用户锁定文字/代码/URL/标题。

    身份是「分段 + 内容哈希 + 出现序号」；``original_position`` 与锚点坐标只作诊断，
    所以正文前部增删后保护项仍定位得到（risk N4）。``output_text`` 给出时按内容
    计数核实它是否还在成稿里；缺失只在**有对应删除授权且该项没有待复核旗标**时才
    算被合法释放——带 ``indistinguishable`` 旗标或锚在 ``needs_review`` 降级段上的
    保护项一律进 REVIEW，绝不因为拿到授权就返回 PASS（spec 55/57 行）。
    """
    by_id = _index_segments(segments, "来源分段")
    if not _sequence(protections):
        raise ContractError("bad_protections", "protections 必须是保护项序列",
                            kind=type(protections).__name__)
    released_by = frozenset(item for item in indistinguishable if isinstance(item, str))
    authorized_hashes, auth_issues = _protection_release_hashes(authorizations)

    accepted, released, review, rejected, issues = [], [], [], [], []
    issues.extend(auth_issues)
    pending: list = []          # (index, entry_id, entry, review_codes)
    seen: set = set()

    for index, item in enumerate(protections):
        data, shape = _as_record(item, ProtectionEntry)
        if shape:
            subject = "" if data is None else str(data.get("entry_id", ""))
            for code, detail in shape:
                rejected.append(ContractIssue(index, subject, code, detail))
            continue

        hard = []
        entry_id = data["entry_id"]
        if _bad_id(entry_id):
            hard.append(("bad_entry_id", "保护项 ID 必须非空、无空白与控制字符、≤128 字符"))
        else:
            if entry_id in seen:
                hard.append(("duplicate_entry_id", f"保护项 ID 重复: {entry_id}"))
            seen.add(entry_id)
        kind = data["kind"]
        if isinstance(kind, str) and kind.strip().lower() in DIALOGUE_KIND_CLAIMS:
            hard.append(("ordinary_dialogue_not_protectable",
                         "普通小说对白不自动锁定：允许在保留说话人、对象、意图、信息、"
                         "否定、条件与态度强度的前提下编辑"))
        elif kind not in PROTECTION_KINDS:
            hard.append(("bad_kind", f"保护类型不在规范集合内: {kind!r}"))

        original_text = data["original_text"]
        if not isinstance(original_text, str) or not original_text:
            hard.append(("bad_original_text", "被保护的原文不能为空"))
        digest = data["text_sha256"]
        if not isinstance(digest, str) or not _HEX_RE.fullmatch(digest):
            hard.append(("protection_hash_mismatch", "原文哈希必须与原文内容自洽（SHA-256）"))
        elif isinstance(original_text, str) and sha256_text(original_text) != digest.lower():
            hard.append(("protection_hash_mismatch", "原文哈希与原文内容不符"))

        anchor, anchor_problems, anchor_notes = _validate_anchor(
            data["anchor"], by_id, role="保护项")
        hard.extend(anchor_problems)
        if anchor is not None and isinstance(original_text, str) \
                and anchor.excerpt != original_text:
            hard.append(("protection_text_mismatch",
                         "锚点片段与保护原文必须是同一段文字"))
        if not _is_int(data["occurrence_index"]) or data["occurrence_index"] < 0:
            hard.append(("occurrence_index_mismatch", "出现序号必须是非负整数"))
        elif anchor is not None and anchor.occurrence_index != data["occurrence_index"]:
            hard.append(("occurrence_index_mismatch",
                         "保护项出现序号与锚点出现序号不一致"))
        position = data["original_position"]
        if not (_sequence(position) and len(position) == 2
                and all(_is_int(value) and value >= 0 for value in position)
                and position[0] <= position[1]):
            hard.append(("bad_original_position",
                         "原始位置只作诊断，但仍必须是 (起, 止) 非负整数对且起≤止"))
        if _bad_label(data["allowed_scope"], MAX_SCOPE_CHARS):
            hard.append(("bad_allowed_scope",
                         f"允许范围必须是非空短说明（≤{MAX_SCOPE_CHARS} 字符）"))

        if hard:
            for code, detail in _ordered(hard):
                rejected.append(ContractIssue(index,
                                              entry_id if isinstance(entry_id, str) else "",
                                              code, detail))
            continue

        entry = ProtectionEntry(entry_id=entry_id, kind=kind, anchor=anchor,
                               original_text=original_text, text_sha256=digest.lower(),
                               occurrence_index=data["occurrence_index"],
                               original_position=tuple(position),
                               allowed_scope=data["allowed_scope"])
        review_codes = list(anchor_notes)
        if entry_id in released_by:
            review_codes.append(("indistinguishable_dialogue_citation",
                                 "对白与资料引文无法区分，按 spec 57 行进 REVIEW 交人工/模型裁决"))
        pending.append((index, entry_id, entry, review_codes, original_text))

    # 成稿一侧的恢复核实：只按内容与出现次数，绝不读诊断坐标（risk N4）。
    demand: dict = {}
    available_counts: dict = {}
    if output_text is not None:
        for _, _, _, _, text in pending:
            demand[text] = demand.get(text, 0) + 1
        # 一次批量定位全部保护原文：扫描量与保护项条数解耦（risk N5）。
        available_counts = _occurrence_counts(output_text, tuple(demand))

    for index, entry_id, entry, review_codes, original_text in pending:
        authorized = entry.text_sha256 in authorized_hashes
        missing = False
        if output_text is not None:
            available = available_counts.get(original_text, 0)
            if available == 0:
                missing = True
            elif available < demand.get(original_text, 1):
                review_codes.append(("protection_count_shortfall",
                                     f"成稿里该原文出现 {available} 次，不足保护项要求的 "
                                     f"{demand[original_text]} 次：多个保护项不得复用同一位置"))
        if missing and authorized and not review_codes:
            released.append(entry)
            issues.append(ContractIssue(index, entry_id,
                                        "protection_released_by_authorization",
                                        "保护项内容缺失由删除授权解释"))
            continue

        if missing:
            # 缺失与旗标是两件事，两个码都要报（review_codes 非空时不能把缺失吞掉）。
            review_codes.append((
                "protection_missing_from_output",
                "保护项在成稿里找不到"
                + ("（有删除授权可解释缺失，但该项带待复核旗标，仍交人工/模型裁决）"
                   if authorized else "，且没有删除授权")))
        if review_codes:
            for code, detail in review_codes:
                issues.append(ContractIssue(index, entry_id, code, detail))
            review.append(entry)
        else:
            accepted.append(entry)

    return ProtectionValidation(accepted=tuple(accepted), released=tuple(released),
                                review=tuple(review), rejected=tuple(rejected),
                                issues=tuple(issues))


# -------------------------------------------------------------- 删除授权校验


def split_paragraphs(text: str) -> tuple:
    """按空行分块（纯机械，不判断段落是否是「空壳」）。

    返回内容段落，段内原有换行符逐字保留；换行符本身不属于任何段落，
    所以 ``check_deletions`` 比的是内容哈希而不是坐标。
    """
    paragraphs, current_start, last_end = [], None, 0
    pos = 0
    while pos <= len(text):
        match = _NEWLINE_RE.search(text, pos)
        line_end = match.start() if match else len(text)
        next_pos = match.end() if match else len(text)
        if text[pos:line_end].strip():
            if current_start is None:
                current_start = pos
            last_end = line_end
        elif current_start is not None:
            paragraphs.append(text[current_start:last_end])
            current_start = None
        if match is None:
            break
        pos = next_pos
    if current_start is not None:
        paragraphs.append(text[current_start:last_end])
    return tuple(paragraphs)


def check_deletions(original_paragraphs, output_paragraphs, authorizations, *,
                    original_paragraph_ids=None) -> DeletionValidation:
    """段落减少必须逐条对上 DeletionAuthorization，否则 REVIEW。

    Python 只认「身份 = 段落 ID + 原文哈希」这一层事实；某段是不是可以删的空壳、
    删掉后承接是否顺畅，全部归模型的三项检查，脚本不做该类判断（spec 57 行）。
    """
    originals = _paragraph_sequence(original_paragraphs, "original_paragraphs")
    outputs = _paragraph_sequence(output_paragraphs, "output_paragraphs")
    if not _sequence(authorizations):
        raise ContractError("bad_paragraphs", "authorizations 必须是授权序列",
                            kind=type(authorizations).__name__)
    ids = None
    if original_paragraph_ids is not None:
        ids = tuple(original_paragraph_ids)
        if len(ids) != len(originals):
            raise ContractError("bad_paragraphs", "段落 ID 数量与原文段落数不符",
                                ids=len(ids), paragraphs=len(originals))

    authorized, review, rejected = [], [], []
    usable: list = []
    seen_ids: set = set()
    for index, item in enumerate(authorizations):
        data, shape = _as_record(item, DeletionAuthorization)
        if shape:
            subject = "" if data is None else str(data.get("paragraph_id", ""))
            for code, detail in shape:
                rejected.append(ContractIssue(index, subject, code, detail))
            continue
        problems = _authorization_problems(data)
        if problems:
            for code, detail in problems:
                rejected.append(ContractIssue(index, str(data["paragraph_id"]), code, detail))
            continue
        if data["paragraph_id"] in seen_ids:
            rejected.append(ContractIssue(index, data["paragraph_id"],
                                          "duplicate_authorization",
                                          f"同一段落的授权重复登记: {data['paragraph_id']}"))
            continue
        seen_ids.add(data["paragraph_id"])
        entry = _authorization_from(data)
        if not all(getattr(entry, name) == "pass" for name in
                   ("independent_fact_check", "transition_check", "semantic_review")):
            review.append(ContractIssue(index, entry.paragraph_id,
                                        "authorization_check_failed",
                                        "独立事实检查/承接作用检查/语义复核未全部通过"))
            continue
        usable.append(entry)

    remaining = [sha256_text(paragraph) for paragraph in outputs]
    absent = []
    for position, paragraph in enumerate(originals):
        digest = sha256_text(paragraph)
        if digest in remaining:
            remaining.remove(digest)
        else:
            absent.append((position, digest,
                           ids[position] if ids is not None else digest))

    used = set()
    for position, digest, subject in absent:
        match = next((entry for entry in usable
                      if entry.text_sha256 == digest and id(entry) not in used), None)
        if match is None:
            review.append(ContractIssue(position, subject, "deletion_without_authorization",
                                        "整段删除缺少 DeletionAuthorization，按 spec 57 行进 REVIEW"))
            continue
        if ids is not None and match.paragraph_id != subject:
            # 身份对不上就既不采纳、也不登记 used：这条授权仍未解释任何被删段落，
            # 必须由下面的 authorization_unmatched 如实报出来，不能悄悄消失。
            review.append(ContractIssue(position, subject, "authorization_identity_mismatch",
                                        "授权的段落 ID 与被删段落对不上，不予采纳"))
            continue
        used.add(id(match))
        authorized.append(match)

    for entry in usable:
        if id(entry) not in used:
            review.append(ContractIssue(AUTH_ISSUE_INDEX, entry.paragraph_id,
                                        "authorization_unmatched",
                                        "授权指向的段落并没有被删除，先查身份再放行"))

    # issues 口径与其余验证器一致：只带 review 级判定，拒绝项归 rejected 不重复。
    return DeletionValidation(authorized=tuple(authorized), review=tuple(review),
                              rejected=tuple(rejected), issues=tuple(review))


def _paragraph_sequence(value, label: str) -> tuple:
    if isinstance(value, (str, bytes)) or not _sequence(value):
        raise ContractError("bad_paragraphs",
                            f"{label}必须是段落序列；纯文本请先交给 split_paragraphs",
                            label=label, kind=type(value).__name__)
    for paragraph in value:
        if not isinstance(paragraph, str):
            raise ContractError("bad_paragraphs", f"{label}含非字符串段落",
                                label=label, element_type=type(paragraph).__name__)
    return tuple(value)


# ---------------------------------------------------------- 文风样本契约校验


def validate_voice_sample(contract, *, source_text: str, output_text: str) -> VoiceSampleValidation:
    """文风样本只准带特征；样本事实出现在非源输出时返回 REVIEW。

    事实清单（专名/数字/事件/观点）由模型给出，Python 只做子串核实——它不会自己
    判断某个短语究竟是事实还是笔法。
    """
    data, shape = _as_record(contract, VoiceSampleContract)
    rejected, issues, clean, review = [], [], [], []
    if shape:
        for code, detail in shape:
            rejected.append(ContractIssue(0, "", code, detail))
        # issues 只带 review 级判定（与其它验证器同口径）：形状问题全在 rejected 里。
        return VoiceSampleValidation(rejected=tuple(rejected))

    sample_id = data["sample_id"]
    if _bad_id(sample_id):
        rejected.append(ContractIssue(0, sample_id if isinstance(sample_id, str) else "",
                                      "bad_sample_id", "样本 ID 必须非空、无空白、≤128 字符"))
    features = data["features"]
    if not _sequence(features):
        features = ()
        rejected.append(ContractIssue(0, sample_id, "bad_feature", "特征必须是序列"))
    elif not features:
        rejected.append(ContractIssue(0, sample_id, "no_sample_features",
                                      "样本合同必须至少声明一项允许特征"))
    else:
        for position, feature in enumerate(features):
            if not isinstance(feature, str):
                rejected.append(ContractIssue(position, sample_id, "bad_feature",
                                              "特征名必须是字符串"))
            elif feature not in SAMPLE_FEATURES:
                rejected.append(ContractIssue(
                    position, sample_id, "sample_feature_not_allowed",
                    f"只允许句长/节奏/正式度/对话密度特征，收到 {feature!r}；"
                    "口头禅式强制复现不在允许之列"))

    facts = data["facts"]
    built_facts = []
    if not _sequence(facts):
        rejected.append(ContractIssue(0, sample_id, "bad_fact_id", "事实清单必须是序列"))
        facts = ()
    seen_fact_ids: set = set()
    for position, raw in enumerate(facts):
        fact_data, fact_shape = _as_record(raw, SampleFact)
        if fact_shape:
            for code, detail in fact_shape:
                rejected.append(ContractIssue(position, sample_id, code, detail))
            continue
        problems = []
        fact_id, kind, text = fact_data["fact_id"], fact_data["kind"], fact_data["text"]
        if _bad_id(fact_id):
            problems.append(("bad_fact_id", "事实 ID 必须非空、无空白、≤128 字符"))
        if kind not in SAMPLE_FACT_KINDS:
            problems.append(("bad_fact_kind",
                             f"事实类别只能是专名/数字/事件/观点，收到 {kind!r}"))
        if _bad_label(text, MAX_FACT_CHARS):
            problems.append(("bad_fact_text",
                             f"事实文本必须是非空短句（≤{MAX_FACT_CHARS} 字符）"))
        if problems:
            for code, detail in problems:
                rejected.append(ContractIssue(position,
                                              fact_id if isinstance(fact_id, str) else "",
                                              code, detail))
            continue
        if fact_id in seen_fact_ids:
            rejected.append(ContractIssue(position, fact_id, "duplicate_fact_id",
                                          f"事实 ID 重复: {fact_id}"))
            continue
        seen_fact_ids.add(fact_id)
        built_facts.append((position, SampleFact(fact_id=fact_id, kind=kind, text=text)))

    for position, fact in built_facts:
        if fact.text in output_text and fact.text not in source_text:
            review.append(fact)
            issues.append(ContractIssue(position, fact.fact_id,
                                        "sample_fact_in_output_not_in_source",
                                        f"样本{fact.kind}「{fact.text}」出现在成稿但不来自"
                                        "原文，禁止带入样本事实"))
        else:
            clean.append(fact)

    return VoiceSampleValidation(features=tuple(features), clean=tuple(clean),
                                 review=tuple(review), rejected=tuple(rejected),
                                 issues=tuple(issues))


# ---------------------------------------------------------------- 内置自检


def self_check() -> int:
    """对真实分段与病态数据走一遍所有闸门；返回非零即失败。"""
    try:
        from novel_segments import split_novel

        doc = ("第一章 起\n\n林昭握紧剑柄，雨落下来。\n\n「你终于来了。」她说。\n\n"
               "第二章 承\n\n他想起《左传》里那句「多行不义必自毙」，"
               "https://example.com/a 就写在批注里。\n")
        segments = split_novel(doc, body_limit=80, allow_small_limit=True)
        home = next(seg for seg in segments if "林昭握紧剑柄" in seg.text)
        hit = home.text.index("林昭握紧剑柄")
        anchor = SourceAnchor(segment_id=home.segment_id, start=home.start + hit,
                             end=home.start + hit + len("林昭握紧剑柄"),
                             excerpt="林昭握紧剑柄",
                             text_sha256=sha256_text("林昭握紧剑柄"), occurrence_index=0)
        entry = LedgerEntry(claim_id="claim-1", category="name", statement="林昭是主角。",
                           certainty="certain", state="已公开", output_anchor=anchor,
                           source_anchors=(anchor,), operation="compress")
        verdict = validate_ledger([entry], segments)
        assert verdict.status == "PASS", verdict
        # 账本坐标合法但已失真 → REVIEW（不修复坐标；保护项一侧按 risk N4 仍只作诊断）
        drifted = replace(anchor, start=home.start, end=home.end)
        verdict = validate_ledger([replace(entry, claim_id="claim-6",
                                          source_anchors=(drifted,),
                                          output_anchor=drifted)], segments)
        assert verdict.accepted == () and verdict.status == "REVIEW", verdict
        assert codes_of(verdict.issues) == ["anchor_coordinates_stale"], verdict
        assert verdict.review[0].source_anchors == (drifted,), "坐标原样保留，不修好再用"
        # 无来源 → 拒绝，不修好再用
        ghost = replace(anchor, excerpt="根本没出现过的句子",
                       text_sha256=sha256_text("根本没出现过的句子"))
        verdict = validate_ledger([replace(entry, claim_id="claim-2",
                                          source_anchors=(ghost,), output_anchor=ghost)],
                                 segments)
        assert verdict.status == "FAIL" and codes_of(verdict.rejected) == [
            "anchor_excerpt_not_found"], verdict
        # 含糊与剧情敏感：只降级、不升级
        verdict = validate_ledger([replace(entry, claim_id="claim-3", certainty="ambiguous")],
                                 segments)
        assert verdict.status == "REVIEW" and verdict.review[0].certainty == "ambiguous"
        # voice 不能强迫口头禅（preserve = 逐字强制复用）
        verdict = validate_ledger(
            [replace(entry, claim_id="claim-4", category="voice", operation="preserve")],
            segments)
        assert codes_of(verdict.rejected) == ["voice_cannot_force_preserve"], verdict
        verdict = validate_ledger([replace(entry, claim_id="claim-5", category="voice")],
                                 segments)
        assert [e.claim_id for e in verdict.soft] == ["claim-5"], verdict
        # 重复 ID
        verdict = validate_ledger([entry, entry], segments)
        assert codes_of(verdict.rejected) == ["duplicate_claim_id"], verdict
        # 保护项：坐标失真仍按内容定位
        quote_home = next(seg for seg in segments if "「多行不义必自毙」" in seg.text)
        quote = "「多行不义必自毙」"
        protection = ProtectionEntry(
            entry_id="prot-1", kind="quotation",
            anchor=SourceAnchor(segment_id=quote_home.segment_id, start=quote_home.start,
                               end=quote_home.end, excerpt=quote,
                               text_sha256=sha256_text(quote), occurrence_index=0),
            original_text=quote, text_sha256=sha256_text(quote), occurrence_index=0,
            original_position=(10, 20), allowed_scope="逐字保留")
        shifted = "整段新增的前情提要。\n\n" + doc
        verdict = validate_protections([protection], segments, output_text=shifted)
        assert verdict.status == "PASS", verdict
        verdict = validate_protections([protection], segments,
                                       output_text=doc.replace(quote, ""))
        assert verdict.status == "REVIEW", verdict
        verdict = validate_protections([protection], segments,
                                       output_text=doc.replace(quote, ""),
                                       authorizations=[DeletionAuthorization(
                                           paragraph_id="para-1",
                                           text_sha256=sha256_text(quote),
                                           reason="整段并入上一段",
                                           independent_fact_check="pass",
                                           transition_check="pass",
                                           semantic_review="pass")])
        assert verdict.status == "PASS" and len(verdict.released) == 1, verdict
        # 旗标压过授权：被标 indistinguishable 的项即使有授权也只进 REVIEW，绝不 PASS
        verdict = validate_protections([replace(protection, entry_id="prot-flag")], segments,
                                       output_text=doc.replace(quote, ""),
                                       authorizations=[DeletionAuthorization(
                                           paragraph_id="para-1",
                                           text_sha256=sha256_text(quote),
                                           reason="整段并入上一段",
                                           independent_fact_check="pass",
                                           transition_check="pass",
                                           semantic_review="pass")],
                                       indistinguishable=("prot-flag",))
        assert verdict.released == (), verdict
        assert verdict.status == "REVIEW", verdict
        assert "indistinguishable_dialogue_citation" in codes_of(verdict.issues), verdict
        assert "protection_missing_from_output" in codes_of(verdict.issues), verdict
        # 普通对白不自动锁定
        verdict = validate_protections([replace(protection, entry_id="prot-2",
                                               kind="dialogue")], segments)
        assert codes_of(verdict.rejected) == ["ordinary_dialogue_not_protectable"], verdict
        # 删除授权：缺授权 → REVIEW；像空壳的段落也一样要授权
        original_paras = split_paragraphs("。\n\n……\n\n真。\n")
        assert original_paras == ("。", "……", "真。"), original_paras
        verdict = check_deletions(original_paras, (), ())
        assert verdict.status == "REVIEW" and len(verdict.review) == 3, verdict
        verdict = check_deletions(original_paras, original_paras, ())
        assert verdict.status == "PASS", verdict
        # 文风样本事实不得进入非源输出
        contract = VoiceSampleContract(sample_id="sample-1",
                                      features=("rhythm", "sentence-length"),
                                      facts=(SampleFact(fact_id="f-1", kind="proper-noun",
                                                       text="沈渡"),))
        verdict = validate_voice_sample(contract, source_text="原文没有这个名字。",
                                        output_text="成稿里写沈渡来了。")
        assert verdict.status == "REVIEW", verdict
        verdict = validate_voice_sample(
            replace(contract, features=("catchphrase",)), source_text="原文。",
            output_text="成稿。")
        assert codes_of(verdict.rejected) == ["sample_feature_not_allowed"], verdict
    except BaseException as error:  # noqa: BLE001 - 自检要把任何失败如实报非零
        print(f"自检：FAIL {type(error).__name__}: {error}")
        return 1
    print("自检：PASS")
    return 0


def codes_of(issues) -> list:
    """判定码集合（排序去重），自检与消费方都用它比对期望结果。"""
    return sorted({issue.code for issue in issues})


if __name__ == "__main__":  # pragma: no cover - 自检失败必须以非零码退出
    raise SystemExit(self_check())
