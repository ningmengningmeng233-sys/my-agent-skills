"""源稿预检：确定矛盾 / 疑似矛盾 / 可解释差异 三分法（v4 spec §7.2 / §11.1）。

- 数量检测先掩蔽序数（第X天/第X次/第一百回），绝不把序数与数量混报；
  「第二天」与「四天」、「第一次」与「第二次」不得被当作同一数量冲突；
- 启发式结果一律保留「疑似、待人工核」标记，绝不冒充确定结论；
- 确定性结论只用于可客观判定的输入卫生问题：空输入、乱码、未在规则词典覆盖的
  日文假名片段、同句数量自相矛盾；规则词典已覆盖的日语 token（如ドキドキ）按
  可解释差异处理，不阻塞。

结果语义：certain→fail（需返工/换稿），suspected→blocked（待人工核，
必须进入 decisions.json），explainable→warning（记录解释依据后不阻塞）。

思想复用 v3 ``check_translation_quality.py`` 的矛盾检测与
``check_source_health.py`` 的乱码/空输入检查，但按 v4 结果语义重写，不导入 v3 运行时。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from .chapter_split import cn2ar
from .hashing import sha256_bytes
from .jsonio import read_json
from .paths import RULES_PATH

_CN_NUM = "零〇一二两三四五六七八九十百千万亿0-9０-９"

# ── 数量检测（先掩蔽序数，再从掩蔽文本中匹配）─────────────────────────
# 序数单位：含「个」（第三个月）与「回」（第一百回），掩蔽后不会残留子串被当作数量。
_ORDINAL_MASK_RE = re.compile(
    r"第\s*[" + _CN_NUM + r"]+\s*(?:章|话|回|节|集|卷|天|次|名|位|层|轮|批|个|阶段)"
)
# 始终当作数量词的量词单位（最长优先，避免「小时/分钟/公里/金币」被拆成短单位）
_QTY_UNITS_ORDERED = (
    "小时", "分钟", "公里", "金币", "银币", "铜币",
    "天", "周", "秒", "枚", "名", "人", "件", "只", "头", "匹", "辆", "艘", "架",
    "间", "座", "家", "所", "本", "张", "把", "条", "根", "颗", "粒", "块",
    "层", "次", "回", "遍", "趟", "步", "里", "米", "个", "元",
)
_QTY_ALTERNATION = "|".join(re.escape(unit) for unit in _QTY_UNITS_ORDERED)
# 需前带「个」才视为数量词的单位（月/日/年，避免把「5月3日」「2024年」当数量）
_QTY_UNITS_WITH_GE = ("月", "日", "年")

_QUANTITY_RE = re.compile(rf"(?<!第)([{_CN_NUM}]+)({_QTY_ALTERNATION})")
_QUANTITY_GE_RE = re.compile(rf"个([{_CN_NUM}]+)({'|'.join(_QTY_UNITS_WITH_GE)})")

# ── 序数扫描（用于可解释说明，不产生阻塞）──────────────────────────────
_ORDINAL_NOTE_RE = re.compile(rf"第([{_CN_NUM}]+)(天|次|回|章|话|卷|集|阶段|批|轮|名|位|层|个)")

# ── 时间 / 日期 ──────────────────────────────────────────────────────
_DAY_MARKERS = ("清晨", "早晨", "早上", "上午", "中午", "正午", "午后", "下午", "傍晚", "黄昏", "白天")
_NIGHT_MARKERS = ("晚上", "夜晚", "夜里", "深夜", "午夜", "凌晨", "入夜", "夜幕")
_CONTRAST_WORDS = ("可是", "却", "竟然", "居然", "明明", "怎么", "反而", "偏偏")
_DATE_RE = re.compile(r"今天[^。！？]{0,12}?(\d{1,2}月\d{1,2}日)")

# ── 人物身份 / 称谓 ──────────────────────────────────────────────────
_RELATIONS = (
    "哥哥", "弟弟", "姐姐", "妹妹", "父亲", "母亲", "爸爸", "妈妈", "爹", "娘",
    "师父", "师傅", "师兄", "师姐", "师弟", "师妹", "老师", "叔叔", "舅舅", "姑姑",
    "爷爷", "奶奶", "姥爷", "姥姥", "丈夫", "妻子", "儿子", "女儿",
    "未婚妻", "未婚夫", "男朋友", "女朋友", "孙子", "孙女", "侄子", "侄女",
)
_RELATION_RE = re.compile(
    r"([一-鿿]{1,4})(?:是|就是)(?:他|她|我|你)(?:的)?(?:"
    + "|".join(_RELATIONS)
    + r")"
)
_RELATION_GROUPS: dict[str, set[str]] = {
    "male_elder": {"哥哥", "父亲", "爸爸", "爹", "师父", "师傅", "师兄", "老师", "叔叔", "舅舅", "爷爷", "姥爷", "丈夫"},
    "female_elder": {"姐姐", "母亲", "妈妈", "娘", "师娘", "师姐", "阿姨", "姑姑", "奶奶", "姥姥", "妻子", "未婚妻", "女朋友"},
    "male_younger": {"弟弟", "儿子", "师弟", "侄子", "孙子", "未婚夫", "男朋友"},
    "female_younger": {"妹妹", "女儿", "师妹", "侄女", "孙女"},
}
_NAME_CLAIM_RE = re.compile(r"(?:我叫|我的名字叫|名字叫)([一-鿿]{1,4})")

# ── 地点 / 空间 ──────────────────────────────────────────────────────
_ENTER_RE = re.compile(r"(?:走进|走入|进入|踏进|回到|进了|回了)(?:了)?([一-鿿]{1,6})")
_LEAVE_RE = re.compile(r"(?:走出|离开|出了|跑出|逃出|离开了)(?:了)?([一-鿿]{1,6})")
_TRAILING_PARTICLES = "了的在和与并就便向往到于"

# ── 事件链（成功 / 未能）──────────────────────────────────────────────
_ACTION_VERBS = (
    "打开", "拿到", "救出", "救下", "杀死", "杀掉", "通过", "完成", "解决",
    "离开", "进入", "到达", "说服", "找到", "赢得", "逃出", "击败", "启动",
    "召唤", "使用", "打开", "逃脱",
)
_ACTION_ALT = "|".join(_ACTION_VERBS)
_SUCCESS_RE = re.compile(rf"成功(?:了)?({_ACTION_ALT})")
_FAIL_RE = re.compile(rf"(?:没能|未能|没有|没|始终无法|无法)({_ACTION_ALT})")

# ── 元文本 / 完整性 ───────────────────────────────────────────────────
_STRONG_META = (
    "作者的话", "作者有话说", "作话", "作者感言", "小编的话",
    "求推荐", "求订阅", "求月票", "求收藏", "请支持正版", "首发于",
    "PS：", "Ps：", "注：", "译者注", "备注：", "本章有话说",
    "台版转自", "录入：", "校对：", "发布：", "论坛：", "转自深夜读书会",
)
_MILD_META = ("本章完", "未完待续", "下章预告", "下回分解", "全书完", "全文完")
def strong_meta_markers() -> tuple[str, ...]:
    """返回强元文本标记，供改写后端执行“剥离元文本”决策。"""
    return _STRONG_META


_CONTINUATION_STARTS = ("然后", "接着", "可是", "然而", "但是", "再说", "于是", "后来", "没想到", "不过", "而")
_TERMINAL_PUNCT = "。！？!?…\"”」』~～"

# ── 输入卫生 ─────────────────────────────────────────────────────────
_GARBLED_MARKERS = ("�", "锟斤拷", "烫烫烫", "屯屯屯", "口口口")
_KANA_RE = re.compile(r"[ぁ-んァ-ヶー]")

# 用于在判断日文原稿前，先掩蔽规则词典已覆盖的日语 token（如拟声词、称谓）
_MASK_CHAR = "￼"


def _dictionary_kana_sources() -> set[str]:
    """加载规则词典中 source 含假名的映射条目，视为可替换元素而非日文原稿。"""
    try:
        data = read_json(RULES_PATH)
    except (OSError, ValueError):
        return set()
    sources: set[str] = set()
    for tier_name in ("must_replace", "functional_adaptation"):
        for entry in data.get("tiers", {}).get(tier_name, []):
            if isinstance(entry, dict) and "source" in entry:
                source = entry["source"]
                if _KANA_RE.search(source):
                    sources.add(source)
    return sources


def _mask_covered_kana(text: str, sources: set[str]) -> str:
    """把规则词典已覆盖的含假名 source 替换为占位符，便于暴露未被覆盖的日文片段。"""
    masked = text
    for source in sorted(sources, key=len, reverse=True):
        masked = masked.replace(source, _MASK_CHAR * len(source))
    return masked
_SELF_COUNT_RE = re.compile(
    rf"有([{_CN_NUM}]+)(个|名|人)[^。！？]{{0,20}}(?:却|但|竟然|结果)(?:只|就)?有([{_CN_NUM}]+)(个|名|人)"
)

VERDICT_RESULT = {"certain": "fail", "suspected": "blocked", "explainable": "warning"}
MAX_FINDINGS_PER_CATEGORY = 5


@dataclass
class PrecheckFinding:
    finding_id: str
    category: str
    verdict: Literal["certain", "suspected", "explainable"]
    result: Literal["fail", "blocked", "warning"]
    evidence: str
    explanation: str
    decision_required: bool = True


@dataclass
class PrecheckReport:
    schema_version: str = "v4-precheck-1"
    chapter_ref: str = ""
    findings: list[PrecheckFinding] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    blocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "chapter_ref": self.chapter_ref,
            "findings": [
                {
                    "finding_id": f.finding_id,
                    "category": f.category,
                    "verdict": f.verdict,
                    "result": f.result,
                    "evidence": f.evidence,
                    "explanation": f.explanation,
                    "decision_required": f.decision_required,
                }
                for f in self.findings
            ],
            "summary": self.summary,
            "blocked": self.blocked,
        }


def _mask_ordinals(text: str) -> str:
    """把「第X天/第X次/第一百回/第三个月」等序数整体替换为等长占位符，
    使它们既不会被当作数量词，也不会残留「百回」等子串产生误报。"""
    return _ORDINAL_MASK_RE.sub(
        lambda match: "第" + "Ｘ" * (len(match.group(0)) - 1), text
    )


def _quantity_matches(text: str) -> list[tuple[int, str, str, int]]:
    """返回 [(数值, 单位, 原词, 位置)]。基于掩蔽序数后的文本匹配，
    证据上下文取自原文。"""
    masked = _mask_ordinals(text)
    out: list[tuple[int, str, str, int]] = []
    for match in _QUANTITY_RE.finditer(masked):
        out.append((cn2ar(match.group(1)), match.group(2), match.group(0), match.start()))
    for match in _QUANTITY_GE_RE.finditer(masked):
        out.append((cn2ar(match.group(1)), match.group(2), match.group(0), match.start()))
    return out


def _ordinal_notes(text: str) -> list[tuple[str, list[str]]]:
    notes: dict[str, list[str]] = defaultdict(list)
    for match in _ORDINAL_NOTE_RE.finditer(text):
        notes[match.group(2)].append(match.group(0))
    return sorted((unit, words) for unit, words in notes.items())


def _context(text: str, pos: int, width: int = 24) -> str:
    return text[max(0, pos - width):pos + width].replace("\n", " ")


def _quantity_conflicts(text: str) -> list[PrecheckFinding]:
    matches = _quantity_matches(text)
    by_unit: dict[str, dict[int, list[tuple[str, int]]]] = defaultdict(lambda: defaultdict(list))
    for value, unit, word, pos in matches:
        by_unit[unit][value].append((word, pos))
    findings: list[PrecheckFinding] = []
    for unit, values in sorted(by_unit.items()):
        if len(values) < 2:
            continue
        distinct = sorted(values)
        first = values[distinct[0]][0]
        second = values[distinct[1]][0]
        evidence = (
            f"同一量词单位「{unit}」出现不同数量："
            f"「{first[0]}」({_context(text, first[1])}…) 与 "
            f"「{second[0]}」({_context(text, second[1])}…)"
        )
        explanation = (
            "可能指向同一事实的数量冲突，也可能是不同事件/不同语境下的合法差异。"
            "未决前按疑似阻塞；需通过 decisions.json 确认：修正 / 保留 / 标记为有意设定。"
            "若为不同语境差异，可判定为可解释差异后解除阻塞。"
        )
        findings.append(
            PrecheckFinding(
                finding_id="",
                category="quantity",
                verdict="suspected",
                result="blocked",
                evidence=evidence,
                explanation=explanation,
            )
        )
    return findings


def _date_conflicts(text: str) -> list[PrecheckFinding]:
    findings: list[PrecheckFinding] = []
    for paragraph in re.split(r"\n\s*\n", text):
        dates = _DATE_RE.findall(paragraph)
        if len(set(dates)) >= 2:
            findings.append(
                PrecheckFinding(
                    finding_id="",
                    category="time",
                    verdict="suspected",
                    result="blocked",
                    evidence=f"同一段内「今天」出现不同日期：{'、'.join(sorted(set(dates)))}",
                    explanation="同一段内两个「今天」日期不同，疑为时间矛盾；需人工核，未决前阻塞。",
                )
            )
    return findings


def _time_conflicts(text: str) -> list[PrecheckFinding]:
    findings: list[PrecheckFinding] = []
    for sentence in re.split(r"[。！？\n]", text):
        has_day = any(marker in sentence for marker in _DAY_MARKERS)
        has_night = any(marker in sentence for marker in _NIGHT_MARKERS)
        if has_day and has_night and any(word in sentence for word in _CONTRAST_WORDS):
            findings.append(
                PrecheckFinding(
                    finding_id="",
                    category="time",
                    verdict="suspected",
                    result="blocked",
                    evidence=f"同句内白天与夜间时段标记并存且带转折：『{sentence[:40]}』",
                    explanation="同句既出现白天标记又出现夜间标记且带转折，疑为时间矛盾；需人工核。",
                )
            )
    return findings


def _identity_conflicts(text: str) -> list[PrecheckFinding]:
    findings: list[PrecheckFinding] = []

    relation_names: dict[str, set[str]] = defaultdict(set)
    for match in _RELATION_RE.finditer(text):
        name = match.group(1)
        if name in {"他", "她", "我", "你", "它"}:
            continue
        relation = match.group(0)
        for relation_word in _RELATIONS:
            if relation.endswith(relation_word):
                relation_names[name].add(relation_word)
                break
    for name, relations in sorted(relation_names.items()):
        groups = [
            group
            for group, members in _RELATION_GROUPS.items()
            if members & relations
        ]
        conflicting = False
        for i, left in enumerate(groups):
            for right in groups[i + 1:]:
                if not (_RELATION_GROUPS[left] & _RELATION_GROUPS[right]):
                    conflicting = True
        if conflicting:
            findings.append(
                PrecheckFinding(
                    finding_id="",
                    category="identity",
                    verdict="suspected",
                    result="blocked",
                    evidence=f"同一人物「{name}」出现互斥称谓：{'、'.join(sorted(relations))}",
                    explanation="同一人名被赋予互斥（性别/辈分/亲疏不相容）称谓，疑为人物身份矛盾；需人工核。",
                )
            )

    claimed_names = set(_NAME_CLAIM_RE.findall(text))
    if len(claimed_names) >= 2:
        findings.append(
            PrecheckFinding(
                finding_id="",
                category="identity",
                verdict="suspected",
                result="blocked",
                evidence=f"「我叫/名字叫」出现多个不同名字：{'、'.join(sorted(claimed_names))}",
                explanation="同一文本中自称多个不同名字，疑为人物身份矛盾或笔误；需人工核。",
            )
        )
    return findings


def _clean_location(location: str) -> str:
    return location.rstrip(_TRAILING_PARTICLES)


def _space_conflicts(text: str, window: int = 400) -> list[PrecheckFinding]:
    findings: list[PrecheckFinding] = []
    events: list[tuple[str, str, int]] = []
    for match in _ENTER_RE.finditer(text):
        events.append(("enter", _clean_location(match.group(1)), match.start()))
    for match in _LEAVE_RE.finditer(text):
        events.append(("leave", _clean_location(match.group(1)), match.start()))
    events.sort(key=lambda item: item[2])
    state: dict[str, dict[str, Any]] = {}
    for kind, location, pos in events:
        if not location:
            continue
        current = state.get(location)
        if kind == "enter":
            if current and current["state"] == 1 and pos - current["pos"] <= window:
                findings.append(
                    PrecheckFinding(
                        finding_id="",
                        category="space",
                        verdict="suspected",
                        result="blocked",
                        evidence=f"短窗口内未记录离开就再次进入「{location}」：{_context(text, pos)}",
                        explanation="同一地点连续两次「进入」而其间无「离开」，疑为空间逻辑矛盾或场景跳跃；需人工核。",
                    )
                )
            state[location] = {"state": 1, "pos": pos}
        else:
            if current and current["state"] == -1 and pos - current["pos"] <= window:
                findings.append(
                    PrecheckFinding(
                        finding_id="",
                        category="space",
                        verdict="suspected",
                        result="blocked",
                        evidence=f"短窗口内未记录再进入就再次离开「{location}」：{_context(text, pos)}",
                        explanation="同一地点连续两次「离开」而其间无「进入」，疑为空间逻辑矛盾；需人工核。",
                    )
                )
            state[location] = {"state": -1, "pos": pos}
    return findings


def _event_chain_conflicts(text: str) -> list[PrecheckFinding]:
    succeeded = set()
    for match in _SUCCESS_RE.finditer(text):
        succeeded.add(match.group(1))
    failed = set()
    for match in _FAIL_RE.finditer(text):
        failed.add(match.group(1))
    overlap = sorted(succeeded & failed)
    findings: list[PrecheckFinding] = []
    for verb in overlap:
        findings.append(
            PrecheckFinding(
                finding_id="",
                category="event_chain",
                verdict="suspected",
                result="blocked",
                evidence=f"同一动作「{verb}」既出现「成功…」又出现「没能/未能…」",
                explanation="同一动作既有成功又有失败的表述，疑为事件结果矛盾或先后顺序问题；需人工核。",
            )
        )
    return findings


def _meta_text(text: str) -> list[PrecheckFinding]:
    findings: list[PrecheckFinding] = []
    compact_text = re.sub(r"\s+", "", text)
    strong = [marker for marker in _STRONG_META if marker in compact_text]
    if strong:
        findings.append(
            PrecheckFinding(
                finding_id="",
                category="meta_text",
                verdict="suspected",
                result="blocked",
                evidence=f"正文发现作者/译者元文本标记：{'、'.join(strong[:5])}",
                explanation="元文本混入小说正文，可能是发布痕迹或翻译工作痕迹；需人工确认是否剥离，未决前阻塞。",
            )
        )
    mild = [marker for marker in _MILD_META if marker in compact_text]
    if mild:
        findings.append(
            PrecheckFinding(
                finding_id="",
                category="meta_text",
                verdict="explainable",
                result="warning",
                evidence=f"出现章节性收尾标记：{'、'.join(mild[:5])}",
                explanation="「本章完/未完待续」属网文常见章末标记，剥离即可，不构成事实矛盾，不阻塞。",
                decision_required=False,
            )
        )
    return findings


def _completeness(text: str) -> list[PrecheckFinding]:
    findings: list[PrecheckFinding] = []
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return findings
    first = lines[0].strip()
    if first[:2] in _CONTINUATION_STARTS or first.startswith(("……", "——")):
        findings.append(
            PrecheckFinding(
                finding_id="",
                category="completeness",
                verdict="suspected",
                result="blocked",
                evidence=f"章节开头疑似承接上文：『{first[:30]}』",
                explanation="章节首句是承接性过渡，可能缺少前情开头；需人工核，未决前阻塞。",
            )
        )
    last = lines[-1].strip()
    if last and last[-1] not in _TERMINAL_PUNCT:
        findings.append(
            PrecheckFinding(
                finding_id="",
                category="completeness",
                verdict="explainable",
                result="warning",
                evidence=f"章节结尾无终止标点：『{last[-20:]}』",
                explanation="结尾无终止标点可能是断章/留白或漏段；仅作提示，不构成事实矛盾，不阻塞。",
                decision_required=False,
            )
        )
    return findings


def _input_health(text: str) -> list[PrecheckFinding]:
    findings: list[PrecheckFinding] = []
    if not text.strip():
        findings.append(
            PrecheckFinding(
                finding_id="",
                category="input_health",
                verdict="certain",
                result="fail",
                evidence="文本为空",
                explanation="空输入，章节无法解析，需人工补稿或阻塞输入预检。",
            )
        )
        return findings
    garbled = [marker for marker in _GARBLED_MARKERS if marker in text]
    if garbled:
        findings.append(
            PrecheckFinding(
                finding_id="",
                category="input_health",
                verdict="certain",
                result="fail",
                evidence=f"发现乱码标记：{'、'.join(garbled[:5])}",
                explanation="编码或源文件损坏，需换稿或重新归一化，不得静默改写。",
            )
        )
    covered_sources = _dictionary_kana_sources()
    masked_text = _mask_covered_kana(text, covered_sources)
    remaining_kana = _KANA_RE.search(masked_text)
    if remaining_kana:
        findings.append(
            PrecheckFinding(
                finding_id="",
                category="input_health",
                verdict="certain",
                result="fail",
                evidence=f"文本含未在规则词典中覆盖的日文假名（如「{remaining_kana.group(0)}」）",
                explanation="输入疑似日文原稿或未完全中文化；v4 只接受中文小说稿，进入阻塞状态，不得静默改写。",
            )
        )
    elif _KANA_RE.search(text):
        # 假名仅出现在规则词典 source 中，改写阶段会按映射替换，不作为日文原稿阻塞。
        findings.append(
            PrecheckFinding(
                finding_id="",
                category="input_health",
                verdict="explainable",
                result="warning",
                evidence="文本含日语拟声词/称谓等可替换元素",
                explanation="这些假名 token 已出现在规则词典的 source 中，改写阶段会按映射处理；v4 仍视其为中文小说稿中的待本地化元素，不阻塞。",
                decision_required=False,
            )
        )
    for match in _SELF_COUNT_RE.finditer(text):
        left, right = cn2ar(match.group(1)), cn2ar(match.group(3))
        if left == right:
            continue
        findings.append(
            PrecheckFinding(
                finding_id="",
                category="input_health",
                verdict="suspected",
                  result="blocked",
                evidence=f"同句数量自相矛盾：『{match.group(0)[:40]}』",
                  explanation="同一句中「有N个…却只有M个」，可能是笔误，也可能是作者有意写的人数差异；需人工确认，默认保留原文。",
            )
        )
    return findings


def _ordinal_findings(text: str, quantity_matches: list[tuple[int, str, str, int]]) -> list[PrecheckFinding]:
    notes = _ordinal_notes(text)
    findings: list[PrecheckFinding] = []
    if not notes:
        return findings
    units_str = "、".join(f"第X{unit}" for unit, _words in notes)
    quantity_units = {unit for _value, unit, _word, _pos in quantity_matches}
    overlap = sorted(set(unit for unit, _words in notes) & quantity_units)
    overlap_note = ""
    if overlap:
        overlap_note = (
            "；特别注意：「第X" + "」与「".join(overlap)
            + "」这类序数（序列引用）与同单位的数量（如「四天」「两次」）不是同一数量冲突，"
            "前者表示时间/顺序位置，后者表示持续时间或频次。"
        )
    findings.append(
        PrecheckFinding(
            finding_id="",
            category="ordinal",
            verdict="explainable",
            result="warning",
            evidence=f"出现序数引用：{units_str}",
            explanation=(
                "「第X天/第X次/第X回/第一百回」等是序数（序列引用），不是数量，"
                "不与同单位的数量构成冲突" + overlap_note + "。记录此说明，不阻塞。"
            ),
            decision_required=False,
        )
    )
    return findings


def precheck_text(text: str, chapter_ref: str, *, title: str | None = None) -> PrecheckReport:
    """对单个章节的源文本执行预检，返回三分法报告。

    任一 finding 为 fail/blocked 时 ``report.blocked`` 为 True，
    对应章节不得进入 ready_for_rewrite，必须先经过 decisions.json 闭环。
    """
    findings: list[PrecheckFinding] = []

    findings.extend(_input_health(text))
    if not text.strip():
        return _finalize_report(text, chapter_ref, title, findings)

    findings.extend(_quantity_conflicts(text))
    findings.extend(_ordinal_findings(text, _quantity_matches(text)))
    findings.extend(_date_conflicts(text))
    findings.extend(_time_conflicts(text))
    findings.extend(_identity_conflicts(text))
    findings.extend(_space_conflicts(text))
    findings.extend(_event_chain_conflicts(text))
    findings.extend(_meta_text(text))
    findings.extend(_completeness(text))
    return _finalize_report(text, chapter_ref, title, findings)


def _finalize_report(
    text: str,
    chapter_ref: str,
    title: str | None,
    findings: list[PrecheckFinding],
) -> PrecheckReport:
    capped: list[PrecheckFinding] = []
    counts: dict[str, int] = defaultdict(int)
    category_counts: dict[str, int] = defaultdict(int)
    used_ids: set[str] = set()
    for finding in findings:
        if category_counts[finding.category] >= MAX_FINDINGS_PER_CATEGORY:
            continue
        category_counts[finding.category] += 1
        counts[finding.verdict] += 1
        digest = sha256_bytes(finding.evidence.encode("utf-8"))[:10]
        finding_id = f"pc-{finding.category}-{digest}"
        base_id = finding_id
        suffix = 2
        while finding_id in used_ids:
            finding_id = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(finding_id)
        finding.finding_id = finding_id
        capped.append(finding)
    blocked = any(f.result in {"fail", "blocked"} for f in capped)
    report = PrecheckReport(
        chapter_ref=chapter_ref,
        findings=capped,
        summary={
            "certain": counts["certain"],
            "suspected": counts["suspected"],
            "explainable": counts["explainable"],
            "fail": sum(1 for f in capped if f.result == "fail"),
            "blocked": sum(1 for f in capped if f.result == "blocked"),
            "warning": sum(1 for f in capped if f.result == "warning"),
        },
        blocked=blocked,
    )
    return report


def auto_decisions(findings: list[PrecheckFinding]) -> list[dict[str, Any]]:
    """把需要决策的预检发现转成章节决策项。

    - 仅处理 ``decision_required=True`` 且 ``result=="blocked"`` 的疑似项；
    - 默认方案：强元文本=剥离该段，其余=保留原文；
    - ``category="review"``，按 A 档语义必须用户一键确认后关闭。
    """
    from .decisions import new_entry

    entries: list[dict[str, Any]] = []
    for finding in findings:
        if not finding.decision_required or finding.result != "blocked":
            continue
        choice = "剥离元文本" if finding.category == "meta_text" else "保留原文"
        decision_id = f"dec-{finding.finding_id}"
        entries.append(
            new_entry(
                category="review",
                target=f"预检{finding.category}：{finding.evidence[:80]}",
                choice=choice,
                source_kind="system",
                source_value=f"precheck:{finding.finding_id}",
                reason=(
                    f"A档；预检疑似项，默认处理：{choice}；用户一键确认后关闭。"
                ),
                decision_id=decision_id,
                status="open",
            )
        )
    return entries


def reconcile_auto_decisions(
    report: PrecheckReport, doc: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """对账章节决策与预检报告：新发现补 open 决策，消失的发现置 superseded。

    返回 ``(更新后的文档, 变更说明)``；不修改输入 dict。
    """
    from copy import deepcopy

    from .decisions import add_entry, set_entry_status

    updated = deepcopy(doc)
    entries = updated.get("entries", [])
    if not isinstance(entries, list):
        return updated, ["decisions.entries 不是数组，跳过预检对账"]
    existing = {
        entry.get("decision_id"): entry
        for entry in entries
        if isinstance(entry, dict)
        and (
            str(entry.get("source", {}).get("value", "")).startswith("precheck:")
            or str(entry.get("decision_id", "")).startswith("dec-pc-")
        )
    }
    desired = {entry["decision_id"]: entry for entry in auto_decisions(report.findings)}
    changes: list[str] = []
    for decision_id, entry in desired.items():
        if decision_id not in existing:
            add_entry(updated, entry)
            changes.append(f"新增预检决策 {decision_id}")
    for decision_id, entry in existing.items():
        if decision_id not in desired and entry.get("status") != "superseded":
            set_entry_status(
                updated,
                decision_id,
                "superseded",
                source_kind="system",
                source_value="precheck_reconcile",
                reason="预检发现已消失或证据变化，决策自动失效",
            )
            changes.append(f"关闭过期预检决策 {decision_id}")
    return updated, changes

