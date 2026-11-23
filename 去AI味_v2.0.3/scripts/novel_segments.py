#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""novel_segments.py - 无损分段与动态上下文预算（v1.2 Task 7）。

整本书进不了一个上下文窗口，必须先切成**逐字符可往返**的段，逐段改写后再原样拼回。
本模块是纯文本进/文本出的确定性算法：**零写盘**、不碰任务状态（Task 6）、不调模型。
唯一的文件访问是 :func:`first_load_chars` —— 它按 ``runtime_contract.FIRST_LOAD_FILES``
**只读**真实首载文件量出 ``fixed_chars``（读不到就 OSError 原样抛给调用方），
除这一处只读之外本模块不碰任何文件。

设计方案「分段与上下文合同」（39-43 行）的绑定口径，逐条对应：

* 分段与重组**逐字符往返一致**，不重建空白：``join_segments(split_novel(t)) == t``
  对一切输入成立（含混合换行；本层不做任何换行或 Unicode 规范化）。
* 不在对话引号、代码围栏、标题与其首段之间、用户锁定区间内部切开。候选点落入
  禁切区间时**先向后找**下一个安全点（向后=区间末，合并后的区间互不触碰故必安全）；
  向后越过硬上限则回退同层级更早的候选；硬上限内确实没有安全点时**按结构单元**给
  REVIEW 判定（spec 41/244 行），绝不硬切保护内容：默认 ``on_review="degrade"`` 把
  被堵住的禁切块整块原样带走、超出预算并打 ``Segment.needs_review`` 旗标，其余单元
  照常切分返回（未闭合引号之类病态数据不再让宿主拿到零段）；``on_review="raise"``
  抛 ``cut_review_required``，details 指明病态单元的 ``unit_start/unit_end``。
  未闭合全角开栏的前向保护只到**所在段落末**，并以 :data:`MAX_UNCLOSED_QUOTE_SPAN`
  封顶——护到文末会把整本书变成一个不可切单元。
* 识别章/卷/回/篇/节、序章/序言/楔子/引子/尾声/终章/后记/前言/番外(篇)、Markdown 标题
  （``#``/``##`` 记章界，``###``+ 记场景界）和常见场景分隔线（※/＊/*/-/—/◆ 等
  连续 ≥3 字符的整行）；无标题正文退化为段落边界上的有界正文块。
  「第 1 章」这类序号夹空格的写法同样算章界（Minor 8 补齐识别面）。
* 已知边界（显式交给 Task 10）：ASCII 直引号 ``"`` 只在**同一行内**成对生效，跨行
  直引号对话**不加保护**——跨行配直引号极易把普通省略号式行文误判成引号区间，
  本层判定风险高于收益故维持现状；真实稿的这类输入由 Task 10 的 L0/复核层兜住。
* 正文预算动态计算：``available_body_chars = context_limit - fixed_chars -
  reserve_chars``；低于 :data:`MIN_BODY_CHARS`（1024）抛与 REVIEW 不同的明确错误
  :data:`CODE_BUDGET_BELOW_MINIMUM`；宿主容量未知时取保守上限
  :data:`CONSERVATIVE_CONTEXT_LIMIT_CHARS`（spec 43 行「无法取得宿主容量时采用保守
  上限」——取 32000 字符：明显低于常见 128k token 级宿主按 1 字符≈1 token 的保守
  折算，宁可少装也不谎报，且必须写成文档化常量而不是散落的字面量）。
  ``fixed_chars`` 默认量自 ``runtime_contract.FIRST_LOAD_FILES`` 真实文件的字符数
  （Skill 指令 + 首载参考），不是发明出来的数字。计划红线：任何固定字符数都不得
  作为唯一边界。1024 地板**同时**绑住显式 ``body_limit``：低于地板必须显式
  ``allow_small_limit=True``（自检/测试走查切点用）才放行，否则同样抛
  :data:`CODE_BUDGET_BELOW_MINIMUM`——生产入口没有静默绕过地板的通路。
* 切点优先级：章节边界（结构先于预算：段天然对齐章/景界）→ 空行段落边界 → 句末 →
  安全字符。同一层级内取不越硬上限的最右安全点。超预算结构单元被拆分时，第一块
  保留结构身份（kind=chapter/scene，标题与其首段同行），后续块为 body 并继承
  chapter_index/heading_path。
* 每段携带只读 ``context_before``/``context_after`` 窗口（原文档坐标上的邻接切片），
  默认窗口为 0（空串）由调用方（T10/11）按预算决定携带量；它们**绝不**进入
  ``Segment.text``，最终组装只吃 ``text``——重叠上下文不会被写第二遍
  （:func:`join_segments` 的实现与「总长恰等于原文」的测试共同断言这一点）。

**坐标系（交给 Task 8 的锚点合同）**：``start``/``end``/``protected_ranges`` 一律是
调用方传入的解码后 ``str`` 的 Python 码点下标（半开区间 ``[start, end)``），不是字节
偏移、不是行号；``text_sha256`` 是 ``segment.text.encode("utf-8")`` 的 SHA-256，
即对该段精确切片（含原有混合换行）的哈希。保护区间输入输出同坐标系；输出按段裁剪
（clamp）进段边界，被段完整包含的区间原样回显。

失败形态（:class:`SegmentError`，code 风格与 Task 5/6 一致）：

* ``budget_below_minimum`` —— 动态预算差额 < 1024，或显式 ``body_limit`` 低于 1024
  却没给 ``allow_small_limit=True``：明确报错而不是静默切成小碎段；
* ``cut_review_required`` —— 仅 ``on_review="raise"`` 时抛出：硬上限内没有安全切点
  （用户锁定区间过长、未闭合围栏跨预算等），交人工/模型复核，不硬切保护内容；
  默认 ``degrade`` 模式改为逐单元打 ``needs_review`` 旗标并返回其余段；
* ``bad_protected_range`` —— 保护区间不是原文档坐标内的合法半开区间；
* ``bad_body_limit`` —— 显式 ``body_limit`` 不是正整数（形状错误，与预算不足区分）。

性能口径：全文一遍扫描（行、引号、句末、段落、候选各一条线性 pass；标题往后找
首段按索引走，绝不复制剩余行表），切点用有序数组 + 二分；百万字符与标题密集输入
都按输入规模线性完成（数量级见 :file:`tests/test_novel_segments.py` 的
``TestHeadingDenseScaling``）。

``python -B -X utf8 scripts/novel_segments.py`` 是内置自检：对一组病态输入走一遍
往返与禁切断言，末尾打印「自检：PASS」。
"""
from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass
from typing import Literal, Sequence

from runtime_contract import FIRST_LOAD_FILES, REPO_ROOT

MIN_BODY_CHARS = 1024
CONSERVATIVE_CONTEXT_LIMIT_CHARS = 32_000
# reserve 预留：命中规则 + 相关账本 + 保护清单 + 前后文窗口 + 验证材料（spec 43 行）。
DEFAULT_RESERVE_CHARS = 8_000
# 未闭合全角开栏的前向保护上界：只护到所在段落末，且最长跨度由此常量封顶。
# 真实爬取稿里「引号没关上」是常态（漏一个闭引号、说话人跨段），护到文末会让
# 整本书变成一个不可用的 REVIEW 单元，故必须有界（Task 7 review Critical 1）。
MAX_UNCLOSED_QUOTE_SPAN = 4_000

CODE_BUDGET_BELOW_MINIMUM = "budget_below_minimum"
CODE_BAD_BODY_LIMIT = "bad_body_limit"
CODE_CUT_REVIEW_REQUIRED = "cut_review_required"
CODE_BAD_PROTECTED_RANGE = "bad_protected_range"

REVIEW_DEGRADE = "degrade"
REVIEW_RAISE = "raise"
_REVIEW_MODES = (REVIEW_DEGRADE, REVIEW_RAISE)

_QUOTE_OPENERS = {"“": "”", "「": "」", "『": "』"}
_QUOTE_CLOSERS = {"”", "」", "』"}
_SENTENCE_ENDS = "。！？…；!?;"
_HEADING_MAX_LEN = 60
_NUM = rf"[0-9０-９零〇一二三四五六七八九十百千万两]"
_SEP_CHARS = r"[　 :：、.。\-—·]+"
# 「第 1 章」「第 一章」这类夹空格的写法在 scraped 稿里成片出现（Minor 8）；
# 只容忍 第/序号/名目 之间的少量空格，正文长句依旧不会被误判成章界。
_INNER_SEP = r"[ \t　]{0,2}"
_CHAPTER_RE = re.compile(
    rf"^第{_INNER_SEP}{_NUM}{{1,12}}{_INNER_SEP}[章回篇](?:{_SEP_CHARS}\S.{{0,40}})?$")
_VOLUME_RE = re.compile(
    rf"^第{_INNER_SEP}{_NUM}{{1,12}}{_INNER_SEP}卷(?:{_SEP_CHARS}\S.{{0,40}})?$")
_SECTION_RE = re.compile(
    rf"^第{_INNER_SEP}{_NUM}{{1,12}}{_INNER_SEP}节(?:{_SEP_CHARS}\S.{{0,40}})?$")
_SPECIAL_RE = re.compile(
    rf"^(?:序章|序言|楔子|引子|尾声|终章|后记|番外篇|番外|前言)(?:{_SEP_CHARS}\S.{{0,40}})?$")
_MD_RE = re.compile(r"^(#{1,6})[ \t]+(\S.{0,58})$")
_SEPARATOR_RE = re.compile(r"^[※＊*—–\-•·◆◇☆○●□■≡]{3,}$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")
_QUOTE_CHARS_RE = re.compile("[“”「」『』]")


@dataclass(frozen=True)
class Segment:
    """brief 规定的 10 个字段（名称与顺序即合同）+ 追加的只读窗口与复核旗标。

    ``context_before`` / ``context_after`` 是原文档坐标上的邻接切片，仅供提示词
    携带；永远不进入 ``text``，组装只吃 ``text``。

    ``needs_review``（Task 7 review Critical 1 追加，带默认值，不动前 10 个字段）：
    该段所在的结构单元在预算内找不到任何安全切点，于是把禁切块整块原样带走、
    超出预算并标记送人工/模型复核——文档不再因此丢失，往返不变量照常成立。
    """

    segment_id: str
    kind: Literal["chapter", "scene", "body"]
    start: int
    end: int
    text: str
    text_sha256: str
    chapter_index: int
    scene_index: int
    heading_path: tuple[str, ...]
    protected_ranges: tuple[tuple[int, int], ...]
    context_before: str = ""
    context_after: str = ""
    needs_review: bool = False


class SegmentError(RuntimeError):
    """分段层的确定性失败；``code`` 区分预算 ERROR、REVIEW 与非法输入等形态。"""

    def __init__(self, code: str, message: str, **details):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = details


def first_load_chars() -> int:
    """真实首载集合（``runtime_contract.FIRST_LOAD_FILES``）的字符总数。

    ``available_body_chars`` 的 ``fixed_chars`` 默认值由它量出来——预算扣的是实际
    会被带进上下文的那几份文本，不是抄来的常数。文件缺失属安装事故，OSError 原样抛。
    """
    total = 0
    for rel in FIRST_LOAD_FILES:
        total += len((REPO_ROOT / rel).read_text(encoding="utf-8"))
    return total


def available_body_chars(context_limit=None, reserve_chars=None, fixed_chars=None) -> int:
    """正文可用字符数 = 宿主容量 - 固定开销 - 预留；低于 1024 抛明确 ERROR。

    ``context_limit=None`` 表示宿主容量未知，走 :data:
    ``CONSERVATIVE_CONTEXT_LIMIT_CHARS``（spec 43 行）。
    """
    if context_limit is None:
        context_limit = CONSERVATIVE_CONTEXT_LIMIT_CHARS
    if reserve_chars is None:
        reserve_chars = DEFAULT_RESERVE_CHARS
    if fixed_chars is None:
        fixed_chars = first_load_chars()
    for name, value in (("context_limit", context_limit),
                        ("reserve_chars", reserve_chars),
                        ("fixed_chars", fixed_chars)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{name} 必须是整数，收到 {value!r}")
    available = context_limit - fixed_chars - reserve_chars
    if available < MIN_BODY_CHARS:
        raise SegmentError(
            CODE_BUDGET_BELOW_MINIMUM,
            "正文可用预算低于 1024 字符，拒绝静默切成小碎段：请缩小固定开销或换更大宿主",
            context_limit=context_limit, fixed_chars=fixed_chars,
            reserve_chars=reserve_chars, available=available)
    return available


# ---------------------------------------------------------------- 结构识别

def _iter_lines(text: str):
    """产出 (content_start, content_end, line_end_next)；行内容不含换行符。"""
    pos = 0
    n = len(text)
    for m in _NEWLINE_RE.finditer(text):
        yield pos, m.start(), m.end()
        pos = m.end()
        if pos >= n:
            return
    if pos < n:
        yield pos, n, n


def _classify_heading(content: str):
    """返回 (kind, level, title) 或 None。level：1=卷级，2=章级，3=场景级。"""
    stripped = content.strip()
    if not stripped or len(stripped) > _HEADING_MAX_LEN:
        return None
    m = _MD_RE.match(stripped)
    if m:
        level = len(m.group(1))
        if level <= 2:
            return ("chapter", level, m.group(2))
        return ("scene", 3, m.group(2))
    if _VOLUME_RE.match(stripped):
        return ("chapter", 1, stripped)
    if _CHAPTER_RE.match(stripped) or _SPECIAL_RE.match(stripped):
        return ("chapter", 2, stripped)
    if _SECTION_RE.match(stripped):
        return ("scene", 3, stripped)
    if _SEPARATOR_RE.match(stripped):
        return ("scene", 0, None)  # 分隔线开场景，不提供标题
    return None


# ---------------------------------------------------------------- 禁切区间

def _paragraph_arrays(text: str, lines):
    """空行分隔的段落区间数组 (starts, ends)；ends 含段落末行的换行符。"""
    starts, ends = [], []
    cur_start = cur_end = None
    for line_start, content_end, line_end_next in lines:
        if text[line_start:content_end].strip() == "":
            if cur_start is not None:
                starts.append(cur_start)
                ends.append(cur_end)
                cur_start = None
            continue
        if cur_start is None:
            cur_start = line_start
        cur_end = line_end_next
    if cur_start is not None:
        starts.append(cur_start)
        ends.append(cur_end)
    return starts, ends


def _quote_intervals(text: str, lines) -> list:
    """对话引号覆盖的禁切半开区间 [open+1, close+1)；未闭合开栏的保护**有界**。

    全角三对（“”/「」/『』）跨行配对（网文正文的常态）；ASCII 直引号只在同一行内
    成对生效——跨行配直引号太容易误伤正文。闭引号本身不许孤悬到下一段开头。

    找不到配对闭栏的开栏（含 ``「…“…”`` 这类嵌套错配）只护到**所在段落末**，并用
    :data:`MAX_UNCLOSED_QUOTE_SPAN` 封顶：护到文末会把整本书变成一个不可切的禁区，
    宿主拿到的就是零段（Task 7 review Critical 1）。真的跨段没关上的引号退化为按
    段落边界切——段落边界本就是安全切口，正文一个字都不丢。
    """
    out = []
    opens = []
    for m in _QUOTE_CHARS_RE.finditer(text):
        ch = m.group(0)
        if ch in _QUOTE_OPENERS:
            opens.append((ch, m.start()))
        elif ch in _QUOTE_CLOSERS:
            if opens and _QUOTE_OPENERS[opens[-1][0]] == ch:
                _ch, start = opens.pop()
                out.append((start + 1, m.start() + 1))
            # 找不到配对开栏的闭引号：当作普通符号，忽略
    if opens:
        p_starts, p_ends = _paragraph_arrays(text, lines)
        n = len(text)
        for _ch, start in opens:
            i = bisect.bisect_right(p_starts, start) - 1
            para_end = p_ends[i] if i >= 0 else n
            out.append((start + 1, min(para_end, start + MAX_UNCLOSED_QUOTE_SPAN, n)))
    for start, content_end, _nxt in lines:
        marks = [i for i in range(start, content_end) if text[i] == '"']
        for k in range(0, len(marks) - 1, 2):
            out.append((marks[k] + 1, marks[k + 1] + 1))
    return out


def _merge_intervals(intervals) -> tuple:
    """合并成不相交、互不触碰的半开区间列表，返回 (starts, ends)。"""
    items = sorted((a, b) for a, b in intervals if b > a)
    merged: list = []
    for a, b in items:
        if merged and a <= merged[-1][1]:
            if b > merged[-1][1]:
                merged[-1][1] = b
        else:
            merged.append([a, b])
    return ([m[0] for m in merged], [m[1] for m in merged])


def _cover(c: int, starts, ends):
    """位置 c 落在哪个禁切区间内（[start, end) 半开）；返回区间下标或 None。"""
    j = bisect.bisect_right(starts, c) - 1
    if j >= 0 and c < ends[j]:
        return j
    return None


# ---------------------------------------------------------------- 主扫描

def _scan_structure(text: str, protected):
    """一遍扫全文：结构边界原始清单、禁切区间、空行/句末候选。"""
    n = len(text)
    lines = list(_iter_lines(text))
    raw_bounds = []           # (pos, kind, level, title)
    heading_lines = []        # (line_idx, content_start, line_end_next)
    heading_starts_set = set()
    fences = []
    fence_open = None
    blank_candidates = []
    pending_blank = False
    for idx, (start, content_end, line_end_next) in enumerate(lines):
        content = text[start:content_end]
        if fence_open is not None:
            if _FENCE_RE.match(content):
                fences.append((fence_open, line_end_next))
                fence_open = None
            continue
        if _FENCE_RE.match(content):
            fence_open = start
            continue
        cls = _classify_heading(content)
        if cls is not None:
            kind, level, title = cls
            raw_bounds.append((start, kind, level, title))
            heading_starts_set.add(start)
            if title is not None:
                heading_lines.append((idx, start, line_end_next))
        if content.strip() == "":
            pending_blank = True
        elif pending_blank:
            blank_candidates.append(start)
            pending_blank = False
    if fence_open is not None:
        fences.append((fence_open, n))          # 未闭合围栏护到文末

    sentence_candidates = [m.end() for m in re.finditer(
        "[" + re.escape(_SENTENCE_ENDS) + "]", text) if m.end() < n]

    forbidden = []
    for a, b in protected:
        forbidden.append((a + 1, b))            # 锁定区间的两个端点本身允许切
    forbidden += _quote_intervals(text, lines)
    for a, b in fences:
        forbidden.append((a + 1, b))
    # 标题与其首段「之间」禁切：从标题行内至少一格起，到首段起始码点（含）为止。
    # 首段内部按普通安全点照切（Fix round 1 Important 3：spec 41 行说的是「标题与
    # 其首段之间」，读成「标题+首段整体不可分」会让超长首段直接变成不可切单元）。
    total_lines = len(lines)
    for line_idx, h_start, h_end_next in heading_lines:
        para_start = None
        for j in range(line_idx + 1, total_lines):   # 按索引走，绝不复制剩余行表
            nxt_start, nxt_end, _nxt_next = lines[j]
            if text[nxt_start:nxt_end].strip() == "":
                continue
            if nxt_start not in heading_starts_set:      # 标题紧跟标题：没有首段
                para_start = nxt_start
            break
        if para_start is None:
            forbidden.append((h_start + 1, h_end_next))  # 只锁标题行本身
        else:
            forbidden.append((h_start + 1, para_start + 1))
    return raw_bounds, _merge_intervals(forbidden), blank_candidates, sentence_candidates


def _pick_cut(pos, ceiling, blanks, sents, starts, ends):
    """按 空行 → 句末 → 安全字符 取不越硬上限的最右安全点。

    候选点落在禁切区间：先向后跳到区间末（合并后区间互不触碰，区间末必安全）；
    向后越上限则回退同层级更早的候选；全部落空返回 None（REVIEW 信号）。
    """
    for lst in (blanks, sents):
        i = bisect.bisect_right(lst, ceiling) - 1
        while i >= 0 and lst[i] > pos:
            c = lst[i]
            j = _cover(c, starts, ends)
            if j is None:
                return c
            if ends[j] <= ceiling:
                return ends[j]
            i -= 1
    c = ceiling
    while c > pos:
        j = _cover(c, starts, ends)
        if j is None:
            return c
        if ends[j] <= ceiling:
            return ends[j]
        c = starts[j] - 1                       # 向前（左）退到区间起点之前
    return None


def _blocked_span(ceiling, starts, ends):
    """硬上限处堵住去路的禁切块 (start, end)；没有命中区间时返回零宽区间。"""
    j = _cover(ceiling, starts, ends)
    if j is None:
        return (ceiling, ceiling)
    return (starts[j], ends[j])


def _build_units(text: str, raw_bounds, starts, ends):
    """结构边界（先滤掉不安全切口）→ 单元序列（含章序/景序/标题路径）。"""
    n = len(text)
    survivors = []
    for pos, kind, level, title in raw_bounds:
        if pos == 0:
            survivors.append((pos, kind, level, title))  # 文首不切割，但身份要生效
        elif _cover(pos, starts, ends) is None:
            # 落在引号/围栏/保护区间/标题-首段之间的章界被放弃（跨章节引号不切断）
            survivors.append((pos, kind, level, title))
    meta = {p: (k, lv, t) for p, k, lv, t in survivors}
    cuts = [0] + sorted({p for p, _k, _l, _t in survivors if p > 0}) + [n]
    units = []
    lvl1 = lvl2 = None
    chapter_index = -1
    scene_index = 0
    for i in range(len(cuts) - 1):
        s, e = cuts[i], cuts[i + 1]
        if s >= e:
            continue
        bound = meta.get(s)
        if bound is not None:
            kind, level, title = bound
            if kind == "chapter":
                chapter_index += 1
                scene_index = 0
                if level == 1:
                    lvl1, lvl2 = title, None
                else:
                    lvl2 = title
                path = tuple(x for x in (lvl1, lvl2) if x is not None)
                units.append((s, e, "chapter", chapter_index, 0, path))
            else:  # scene
                scene_index += 1
                path = tuple(x for x in (lvl1, lvl2) if x is not None)
                if title is not None:
                    path = path + (title,)
                units.append((s, e, "scene", max(chapter_index, 0), scene_index, path))
        else:
            path = tuple(x for x in (lvl1, lvl2) if x is not None)
            units.append((s, e, "body", max(chapter_index, 0), 0, path))
    return units


def split_novel(text: str, *, protected_ranges: Sequence = (), body_limit=None,
                context_limit=None, reserve_chars=None, fixed_chars=None,
                context_chars: int = 0,
                on_review: Literal["degrade", "raise"] = REVIEW_DEGRADE,
                allow_small_limit: bool = False) -> tuple:
    """无损分段：结构优先，预算兜底；返回覆盖原文档 [0, len) 的段序列。

    预算来源三选一档：显式 ``body_limit``（T10/11 拿算好的数进来）；否则由
    ``context_limit``/``reserve_chars``/``fixed_chars`` 走 :func:`available_body_chars`；
    全默认 = 宿主容量未知的保守上限。``protected_ranges`` 是原文档坐标的
    ``(start, end)`` 半开区间序列，分段绝不从它们内部下刀。

    ``on_review`` 决定「某个结构单元在预算内找不到任何安全切点」时的形态（**逐单元**
    判定，不是整篇判定）：

    * ``"degrade"``（默认）——把挡住去路的那个禁切块整块原样带走，超出预算，
      打上 ``Segment.needs_review=True``，其余单元照常切分返回。宿主永远拿得到
      段序列而不是零段（Task 7 review Critical 1）。
    * ``"raise"`` —— 抛 :class:`SegmentError`（``cut_review_required``），details 里
      给出病态单元的 ``unit_start/unit_end`` 与被堵住的区间；调用方自己决定换更大
      预算还是整单元送人工。
    """
    if not isinstance(text, str):
        raise TypeError("split_novel 只接受解码后的 str（Task 5 的产物），"
                        f"收到 {type(text).__name__}")
    if on_review not in _REVIEW_MODES:
        raise TypeError(f"on_review 只能是 {' 或 '.join(_REVIEW_MODES)}，"
                        f"收到 {on_review!r}")
    if not isinstance(allow_small_limit, bool):
        raise TypeError(f"allow_small_limit 必须是布尔，收到 {allow_small_limit!r}")
    if not isinstance(context_chars, int) or isinstance(context_chars, bool) \
            or context_chars < 0:
        raise TypeError(f"context_chars 必须是非负整数，收到 {context_chars!r}")
    n = len(text)
    normalized = []
    for i, rng in enumerate(protected_ranges):
        try:
            a, b = rng
        except (TypeError, ValueError):
            raise SegmentError(CODE_BAD_PROTECTED_RANGE,
                               f"保护区间必须是 (start, end) 二元组：#{i}",
                               index=i) from None
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (a, b)) \
                or a < 0 or b > n or a >= b:
            raise SegmentError(CODE_BAD_PROTECTED_RANGE,
                               f"保护区间不在原文档坐标内或起止倒挂：#{i}",
                               index=i)
        normalized.append((a, b))
    if body_limit is not None:
        if not isinstance(body_limit, int) or isinstance(body_limit, bool):
            raise TypeError(f"body_limit 必须是整数，收到 {body_limit!r}")
        # 1024 地板同时绑住动态公式与显式预算：静默的小预算会让宿主拿到一片碎段却
        # 以为预算充足（Fix round 1 Important 4）。自检与测试要拿小预算走查切点行为，
        # 必须显式 allow_small_limit=True——生产入口没有静默绕过的通路。
        if body_limit < 1:
            # 这是形状错误（给的压根不是一个可用预算），不是预算不足，code 必须可分。
            raise SegmentError(CODE_BAD_BODY_LIMIT,
                               f"body_limit={body_limit} 不是正整数，无法分段",
                               body_limit=body_limit)
        if body_limit < MIN_BODY_CHARS and not allow_small_limit:
            raise SegmentError(
                CODE_BUDGET_BELOW_MINIMUM,
                f"显式 body_limit={body_limit} 低于 {MIN_BODY_CHARS} 字符地板；"
                "确认只是走查小预算请显式传 allow_small_limit=True",
                body_limit=body_limit, floor=MIN_BODY_CHARS)
        limit = body_limit
    else:
        limit = available_body_chars(context_limit, reserve_chars, fixed_chars)
    if n == 0:
        return ()

    raw_bounds, (starts, ends), blanks, sents = _scan_structure(text, normalized)
    units = _build_units(text, raw_bounds, starts, ends)

    chunks = []
    for u_start, u_end, kind, chap, scen, path in units:
        pos = u_start
        while pos < u_end:
            if u_end - pos <= limit:
                chunks.append((pos, u_end, kind, chap, scen, path, False))
                break
            ceiling = pos + limit
            cut = _pick_cut(pos, ceiling, blanks, sents, starts, ends)
            if cut is None or cut <= pos:
                blocked = _blocked_span(ceiling, starts, ends)
                if on_review == REVIEW_RAISE:
                    raise SegmentError(
                        CODE_CUT_REVIEW_REQUIRED,
                        "硬上限内没有安全切点（保护区间/引号/围栏/标题首段所致），"
                        "交人工复核而不是硬切",
                        unit_start=u_start, unit_end=u_end, position=pos,
                        ceiling=ceiling, blocked_start=blocked[0],
                        blocked_end=blocked[1])
                # 逐单元降级：被堵住的禁切块原样带走（绝不硬切保护内容），
                # 打上复核旗标，同一单元的剩余部分与其余单元照常切分。
                cut = min(max(blocked[1], pos + 1), u_end)
                chunks.append((pos, cut, kind, chap, scen, path, True))
                pos = cut
                kind = "body"
                continue
            chunks.append((pos, cut, kind, chap, scen, path, False))
            pos = cut
            kind = "body"           # 拆分后的续块是正文块；首块保留结构身份

    segments = []
    for i, (s, e, kind, chap, scen, path, review) in enumerate(chunks):
        body = text[s:e]
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        echoed = tuple(sorted(
            (max(a, s), min(b, e)) for a, b in normalized if max(a, s) < min(b, e)))
        segments.append(Segment(
            segment_id=f"seg-{i:05d}-{digest[:12]}",
            kind=kind, start=s, end=e, text=body, text_sha256=digest,
            chapter_index=chap, scene_index=scen, heading_path=tuple(path),
            protected_ranges=echoed,
            context_before=text[max(0, s - context_chars):s] if context_chars else "",
            context_after=text[e:min(n, e + context_chars)] if context_chars else "",
            needs_review=review,
        ))
    return tuple(segments)


def join_segments(segments) -> str:
    """最终组装只吃 ``Segment.text``：上下文窗口天然不参与，不会被写第二遍。"""
    parts = []
    for seg in segments:
        if not isinstance(seg, Segment):
            raise TypeError("join_segments 只接受 Segment 序列")
        parts.append(seg.text)
    return "".join(parts)


def _selfcheck() -> None:
    """内置自检：病态输入全量往返 + 禁切 + REVIEW + 预算门。"""
    quote_inner = "这句引号里的话非常长，跨过预算边界也不能被切开。" * 7
    cases = [
        "", "哈", "正文一。\r\n正文二\r正文三\n",
        "第一章 甲\n\n他说：“引号开着跨过下一章。\n\n第二章 乙\n\n才结束。”\n",
        "前文。" * 50 + "\n\n```\n围栏里的空行\n\n也不能切\n```\n\n" + "后文。" * 50,
        "前置铺垫的句子。" * 25 + f"他说：“{quote_inner}”\n\n" + "结尾的句子。" * 20,
        "啊" * 5000, "※※※\n\n正文一。\n\n※※※\n\n正文二。\n",
    ]
    for src in cases:
        segs = split_novel(src, body_limit=300, allow_small_limit=True)
        assert join_segments(segs) == src, src[:20]
        assert all(s.text == src[s.start:s.end] for s in segs)
    review_cases = [
        ("他说：“" + "没有结尾的长篇引号。" * 300, {}),
        ("可自由正文。" * 600, {"protected_ranges": [(0, 3400)]}),
    ]
    for src, kw in review_cases:
        try:
            split_novel(src, body_limit=1024, on_review="raise", **kw)
        except SegmentError as err:
            assert err.code == CODE_CUT_REVIEW_REQUIRED, err.code
            assert 0 <= err.details["unit_start"] < err.details["unit_end"] <= len(src)
        else:
            raise AssertionError("raise 模式应返回 REVIEW")
    # 逐单元降级：未闭合开栏只拖累它自己那个单元，其余章照常拿到段
    degraded_src = ("第一章 起\n\n正文句子。\n\n" * 40
                    + "第二章 承\n\n他说：“开栏没关上，" + "病态正文。" * 300
                    + "\n\n第三章 合\n\n正文句子。\n\n" * 40)
    degraded = split_novel(degraded_src, body_limit=1024)
    assert join_segments(degraded) == degraded_src
    assert any(s.needs_review for s in degraded)
    assert any(not s.needs_review for s in degraded), "其余单元必须仍有段可用"
    assert available_body_chars(100_000, 5_000, 3_000) == 92_000
    assert first_load_chars() > 0
    try:
        available_body_chars(2_000, 500, 800)
    except SegmentError as err:
        assert err.code == CODE_BUDGET_BELOW_MINIMUM
    else:
        raise AssertionError("预算不足应报错")
    print("自检：PASS")


if __name__ == "__main__":
    _selfcheck()
