"""事件图初稿与结构校验（v4 spec §8 / §11.1）。

- 事件图字段与 ``docs/v4-artifact-schemas.json`` 的 eventGraph / event / eventRisk 对齐；
- 提供事件构造、高风险节点标记与机械结构校验；
- 结构校验绝不放行：重复 event_id、空参与者/动作/结果、悬空前置引用、
  缺失高风险原因、非法枚举、非法 ordinal 顺序。

AI 负责根据原文生成事件图初稿内容，本模块保证结构契约与高风险闭环。
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Iterable

from .audit import now_iso
from .jsonio import read_json
from .paths import ARTIFACT_SCHEMA, RULES_PATH
from .schemas import SchemaError

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
_SENTENCE_END_RE = re.compile(r"[。！？；]")
_CLAUSE_SPLIT_RE = re.compile(r"[，、]")
# 用于从句子中粗略拆分「主语 + 动作」的常用动词/助动词标记；按长度降序保证长词优先。
_ACTION_MARKERS = sorted(
    (
        "微微一笑", "拍了拍", "答应", "说明", "交谈", "推门", "走进", "进入", "走出", "离开", "来到",
        "握着", "拿着", "放进", "将", "向", "对", "说道", "问道", "回答", "心中",
        "身着", "身穿", "头戴", "手", "想", "觉得", "看", "见", "听",
        "走", "跑", "进", "出", "推", "拉", "开", "关", "拿", "握", "放", "递", "给",
        "送", "接", "穿", "戴", "笑", "哭", "点", "摇", "挥", "招", "坐", "站", "躺", "靠",
        "是", "有", "说", "道", "问", "答", "知道", "认为",
    ),
    key=len,
    reverse=True,
)
_ACTION_MARKER_RE = re.compile("|".join(re.escape(m) for m in _ACTION_MARKERS))
_RISK_REASONS = {
    "causal_chain",
    "character_goal",
    "relationship_change",
    "plot_change_candidate",
    "source_contradiction",
    "low_confidence",
    "cultural_function",
}
_RISK_STATUSES = {"not_required", "open", "approved", "rejected", "superseded"}

# 高风险事件必须由用户确认或由已验证决策文件处理（v4 spec §8.2）。
CONFIRMATION_REQUIRED_REASONS = {
    "causal_chain",
    "character_goal",
    "relationship_change",
    "plot_change_candidate",
    "source_contradiction",
    "low_confidence",
    "cultural_function",
}


def normal_risk() -> dict[str, Any]:
    return {"level": "normal", "reasons": [], "decision_status": "not_required"}


def high_risk(
    reasons: Iterable[str],
    decision_status: str = "open",
    decision_id: str | None = None,
) -> dict[str, Any]:
    """构造高风险事件 risk 对象；原因必须非空且在契约枚举内。"""
    reasons_list = list(dict.fromkeys(reasons))
    unknown = sorted(set(reasons_list) - _RISK_REASONS)
    if unknown:
        raise ValueError(f"未知高风险原因: {unknown}")
    if not reasons_list:
        raise ValueError("高风险事件必须给出至少一个原因")
    if decision_status not in _RISK_STATUSES:
        raise ValueError(f"非法 decision_status: {decision_status}")
    risk: dict[str, Any] = {
        "level": "high",
        "reasons": reasons_list,
        "decision_status": decision_status,
    }
    if decision_id is not None:
        if not _ID_RE.match(decision_id):
            raise ValueError(f"非法 decision_id: {decision_id!r}")
        risk["decision_id"] = decision_id
    return risk


def event_id_for(ordinal: int) -> str:
    if ordinal < 1:
        raise ValueError("ordinal 必须 ≥1")
    return f"e-{ordinal:04d}"


def new_event(
    event_id: str,
    ordinal: int,
    *,
    participants: list[str],
    action: str,
    result: str,
    prerequisites: list[str] | None = None,
    character_goals: list[str] | None = None,
    relationship_changes: list[str] | None = None,
    world_function: str = "无",
    confidence: float = 0.5,
    risk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一个符合 artifact 契约的 event 对象。"""
    if not isinstance(event_id, str) or not _ID_RE.match(event_id):
        raise ValueError(f"非法 event_id: {event_id!r}")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        raise ValueError(f"ordinal 必须是不小于 1 的整数: {ordinal!r}")
    if not participants or not all(isinstance(p, str) and p for p in participants):
        raise ValueError("participants 至少需要一个非空名称")
    if not action or not action.strip():
        raise ValueError("action 不能为空")
    if not result or not result.strip():
        raise ValueError("result 不能为空")
    if not world_function or not world_function.strip():
        raise ValueError("world_function 不能为空（无明确功能时填「无」）")
    try:
        confidence_float = float(confidence)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"confidence 必须是数字: {confidence!r}") from exc
    if not (0.0 <= confidence_float <= 1.0):
        raise ValueError(f"confidence 必须在 [0,1]: {confidence_float}")
    return {
        "event_id": event_id,
        "ordinal": ordinal,
        "participants": list(participants),
        "prerequisites": list(prerequisites or []),
        "action": action,
        "result": result,
        "character_goals": list(character_goals or []),
        "relationship_changes": list(relationship_changes or []),
        "world_function": world_function,
        "confidence": confidence_float,
        "risk": risk if risk is not None else normal_risk(),
    }


def build_event_graph(
    chapter_id: str,
    source_sha256: str,
    events: list[dict[str, Any]],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """组装符合 eventGraph 契约的事件图文档。"""
    if not isinstance(chapter_id, str) or not _ID_RE.match(chapter_id):
        raise ValueError(f"非法 chapter_id: {chapter_id!r}")
    if not isinstance(source_sha256, str) or not _SHA_RE.match(source_sha256):
        raise ValueError(f"source_sha256 必须是 64 位十六进制: {source_sha256!r}")
    if not isinstance(events, list) or not events:
        raise ValueError("事件图至少需要一个事件")
    return {
        "schema_version": "v4-events-1",
        "chapter_id": chapter_id,
        "source_sha256": source_sha256,
        "events": events,
        "generated_at": generated_at or now_iso(),
    }


def _check_risk(risk: Any, prefix: str, errors: list[str]) -> None:
    if not isinstance(risk, dict):
        errors.append(f"{prefix}.risk 必须是对象")
        return
    level = risk.get("level")
    reasons = risk.get("reasons")
    status = risk.get("decision_status")
    if level not in {"normal", "high"}:
        errors.append(f"{prefix}.risk.level 必须是 normal 或 high: {level!r}")
    if not isinstance(reasons, list):
        errors.append(f"{prefix}.risk.reasons 必须是数组")
        reasons = []
    unknown = sorted(set(reasons) - _RISK_REASONS)
    if unknown:
        errors.append(f"{prefix}.risk.reasons 包含未知原因: {unknown}")
    if level == "high" and not reasons:
        errors.append(f"{prefix}.risk 高风险事件必须给出至少一个原因")
    if level == "normal" and reasons:
        errors.append(f"{prefix}.risk 有原因却标记为 normal，应标记为 high")
    if status not in _RISK_STATUSES:
        errors.append(f"{prefix}.risk.decision_status 非法: {status!r}")
    decision_id = risk.get("decision_id")
    if decision_id is not None and (
        not isinstance(decision_id, str) or not _ID_RE.match(decision_id)
    ):
        errors.append(f"{prefix}.risk.decision_id 非法: {decision_id!r}")


def structural_errors(graph: dict[str, Any]) -> list[str]:
    """机械结构校验，返回错误列表（空 = 通过）。

    覆盖：schema_version、chapter_id、source_sha256、events 非空、
    event_id 唯一、ordinal 严格递增、参与者/动作/结果/世界观功能非空、
    前置引用存在且不引用自身、confidence 范围、risk 枚举与高风险原因。
    """
    errors: list[str] = []
    if graph.get("schema_version") != "v4-events-1":
        errors.append("schema_version 必须是 v4-events-1")
    chapter_id = graph.get("chapter_id")
    if not isinstance(chapter_id, str) or not _ID_RE.match(chapter_id):
        errors.append(f"chapter_id 非法: {chapter_id!r}")
    source_sha256 = graph.get("source_sha256")
    if not isinstance(source_sha256, str) or not _SHA_RE.match(source_sha256):
        errors.append(f"source_sha256 非法: {source_sha256!r}")
    events = graph.get("events")
    if not isinstance(events, list) or not events:
        errors.append("events 必须是非空数组")
        return errors

    ids: set[str] = set()
    ordinals: list[int] = []
    for index, event in enumerate(events):
        prefix = f"events[{index}]"
        if not isinstance(event, dict):
            errors.append(f"{prefix} 不是对象")
            continue
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not _ID_RE.match(event_id):
            errors.append(f"{prefix}.event_id 非法: {event_id!r}")
        elif event_id in ids:
            errors.append(f"{prefix}.event_id 重复: {event_id}")
        else:
            ids.add(event_id)
        ordinal = event.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
            errors.append(f"{prefix}.ordinal 必须是不小于 1 的整数: {ordinal!r}")
        else:
            ordinals.append(ordinal)
        participants = event.get("participants")
        if not isinstance(participants, list) or not participants or not all(
            isinstance(p, str) and p for p in participants
        ):
            errors.append(f"{prefix}.participants 必须是非空字符串数组")
        action = event.get("action")
        if not isinstance(action, str) or not action.strip():
            errors.append(f"{prefix}.action 不能为空")
        result = event.get("result")
        if not isinstance(result, str) or not result.strip():
            errors.append(f"{prefix}.result 不能为空")
        world_function = event.get("world_function")
        if not isinstance(world_function, str) or not world_function.strip():
            errors.append(f"{prefix}.world_function 不能为空")
        confidence = event.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not (0.0 <= confidence <= 1.0)
        ):
            errors.append(f"{prefix}.confidence 必须在 [0,1]")
        _check_risk(event.get("risk"), prefix, errors)

    if len(ordinals) == len(events) and len(ordinals) >= 2:
        if any(b <= a for a, b in zip(ordinals, ordinals[1:])):
            errors.append("ordinal 必须严格递增（可排序，不可重复）")

    # 前置引用校验放在收集全部 event_id 之后，避免把指向后置事件的合法引用误报为不存在。
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        prefix = f"events[{index}]"
        event_id = event.get("event_id")
        prerequisites = event.get("prerequisites")
        if not isinstance(prerequisites, list):
            errors.append(f"{prefix}.prerequisites 必须是数组")
            continue
        for prereq in prerequisites:
            if prereq == event_id:
                errors.append(f"{prefix}.prerequisites 不能引用自身: {prereq}")
            elif prereq not in ids:
                errors.append(f"{prefix}.prerequisites 引用了不存在的 event_id: {prereq!r}")
    return errors


def schema_errors(graph: dict[str, Any]) -> list[str]:
    """用 ``docs/v4-artifact-schemas.json`` 的 eventGraph 契约校验（jsonschema 可用时）。"""
    from jsonschema import Draft202012Validator
    try:
        document = read_json(ARTIFACT_SCHEMA)
        subschema = copy.deepcopy(document["$defs"]["eventGraph"])
        subschema["$defs"] = document["$defs"]
        Draft202012Validator(subschema).validate(graph)
        return []
    except Exception as exc:  # noqa: BLE001 - 契约校验失败即收集错误
        return [f"artifact schema 校验失败: {exc}"]


def validate_event_graph(graph: dict[str, Any]) -> None:
    """结构 + 契约双重校验；失败抛出 :class:`SchemaError`。"""
    errors = structural_errors(graph)
    if not errors:
        errors = schema_errors(graph)
    if errors:
        raise SchemaError("事件图校验失败: " + "; ".join(errors[:8]))


def find_high_risk_events(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        event
        for event in graph.get("events", [])
        if isinstance(event, dict)
        and isinstance(event.get("risk"), dict)
        and event["risk"].get("level") == "high"
    ]


def open_high_risk_events(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """决策状态仍为 open 的高风险事件：未闭环，阻塞章节进入 ready_for_rewrite。"""
    return [
        event
        for event in find_high_risk_events(graph)
        if event.get("risk", {}).get("decision_status") in {"open", "superseded"}
    ]


def has_open_high_risk(graph: dict[str, Any]) -> bool:
    return bool(open_high_risk_events(graph))


def _normalize_paragraphs(text: str) -> list[str]:
    """按空行切分并清理段落，返回非空段落列表。"""
    paragraphs: list[str] = []
    for part in re.split(r"\n\s*\n", text):
        cleaned = part.strip().replace("\n", "")
        if cleaned:
            paragraphs.append(cleaned)
    return paragraphs


# 从参与者候选中剔除常见介词短语（向/对/将/给/从/在/被/与/跟 + 宾语），
# 让「森田向老板说明来意」的参与者仍能被识别为「森田」。
_PREP_TRAILING_RE = re.compile(r"(?:向|对|将|给|从|在|被|与|跟).*$")
_CONTINUATION_STARTS = (
    "然后", "接着", "可是", "然而", "但是", "再说", "于是", "后来", "没想到", "不过", "而",
)


def _cultural_terms() -> set[str]:
    """读取规则词典 functional_adaptation 的 source，用于识别文化功能段落。"""
    try:
        data = read_json(RULES_PATH)
    except (OSError, ValueError):
        return set()
    entries = data.get("tiers", {}).get("functional_adaptation", [])
    return {
        entry["source"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("source"), str) and entry["source"]
    }


def _risk_for_extraction(
    confidence: float, reasons: Iterable[str]
) -> dict[str, Any]:
    """把规则抽取结果转成 risk：明确无原因 → normal，否则 high + open。"""
    reasons_list = list(dict.fromkeys(reasons))
    if not reasons_list:
        return normal_risk()
    return high_risk(reasons_list, decision_status="open")


_UNPARSED_ROLE = "待解析角色"

#: LLM 事件抽取的分批大小。整章一次请求会在输出侧被 max_tokens 截断
#: （真实台版序章 301 段实测 finish_reason=length），随后静默回退规则抽取器、
#: 产出碎片参与者，使事件门恒 fail。分批保证输出不触顶。
_LLM_EVENT_BATCH_SIZE = 60

#: 非叙事段落（结构分隔/插图标注/纯标题）的动作标记。
#: 这类段落没有可核验的叙事内容，事件门必须**跳过**而不是判 fail——
#: 真实稿实测：`■` 与 `插图p037` 被当成事件后永远定位不到，白锁一章。
_STRUCTURAL_ACTION = "结构分隔（非叙事段，不参与事件核验）"
_STRUCTURAL_PARA_RE = re.compile(
    r"^(?:[■◆◇○●□△▲☆★※＊*\-—_=~]+"
    r"|插图\s*p?\d*|image\s*\d*|"
    r"第[一二三四五六七八九十百零\d]+[章卷节][^\n]{0,40})$",
    re.IGNORECASE,
)


def is_structural_paragraph(paragraph: str) -> bool:
    """判断段落是否为非叙事结构段（分隔符/插图标注/章节标题行）。

    只匹配**严格形态**，宁漏不误：宁可把边缘段落当叙事段走正常核验，
    也不能把真正的叙事段标成结构段而绕过事件门。
    """
    text = paragraph.strip().strip("\u3000").strip()
    if not text:
        return True
    return bool(_STRUCTURAL_PARA_RE.match(text))

# ---------------------------------------------------------------- 中文参与者校验
# 中文分支的规则抽取没有任何可信度校验（日语分支靠 _jp_subject_is_entity，中文没有
# 对应物），实测真实台版稿 301 段里 194 段抽出的"参与者"不可信：引号碎片 `「席德`、
# 连词「如果」「所以」「然而」「不过」、动词片段「我回绝了」「她带着温柔的微」、
# 插图标记 `■`。这些碎片永远匹配不到译文段落 → 事件对照门恒 fail → 真实章卡死。
#
# 判据分三层（从严到宽），任一命中即判不可信：
# 1) 标点/符号污染：含引号、书名号、■ 等，或含逗号顿号（说明截取到了短语不是实体）；
# 2) 虚词开头/结尾：连词、副词、助词收尾（「…了」「…的」「…着」多是从句中截断）；
# 3) 动宾残留：长度 >6 且含动作标记，说明连动词一起截进来了。
_ZH_PUNCT_RE = re.compile(r"[「」『』“”‘’《》〈〉【】■◆◇○●□△▲→←—…\s，、。！？；：]")

#: 句首虚词：以这些词开头说明截取到的是状语/连接成分，不是角色名
_ZH_LEADING_FUNCTION_RE = re.compile(
    r"^(如果|因此|所以|然而|但是|不过|虽然|即使|假如|要是|因为|由于|于是|然后|接着|"
    r"而且|并且|况且|何况|总之|反正|只是|可是|而是|而是说|至于|关于|对于|按照|依照|"
    r"随着|等到|直到|除非|无论|不管|哪怕|即便|除此|此外|另外|同时|随后|此时|这时|"
    r"那|这|这么|那么|这样|那样|什么|怎么|为何|为什么|然而|终于|果然|竟然|居然|"
    r"其实|确实|当然|显然|大概|也许|或许|恐怕|似乎|好像|仿佛|简直|几乎|差点|"
    r"已经|曾经|刚刚|正在|一直|总是|从来|向来|向来都|从今|今后|以后|以前)"
)

#: 结尾虚词：以这些字收尾说明是动词/形容词片段（「我回绝了」「她带着温柔的微」不是名字）
_ZH_TRAILING_FUNCTION_RE = re.compile(
    r"(了|着|过|的|地|得|吗|呢|吧|啊|呀|哦|嘛|啦|咯|的话|起来|下去|上来|出来|过来|下来)$"
)

#: 动作标记残留：说明把动词也截进了参与者
_ZH_VERB_RESIDUE_RE = re.compile(
    r"(说|道|问|答|笑|哭|走|跑|进|出|推|拉|开|关|拿|握|放|递|给|送|接|穿|戴|"
    r"坐|站|躺|靠|想|觉得|看|见|听|带着|带着|回绝|拒绝|点头|摇头|伸手)"
)

#: 单一汉字常见虚词：单独成"参与者"时不可信
_ZH_STANDALONE_STOPWORDS = frozenset("的了着过吗呢吧啊呀哦嘛与和及或但而因所以这那我你他她它")


_ZH_PRONOUN_RE = re.compile(r"^(我们|你们|他们|她们|它们|咱们|我|你|他|她|它|咱|自己|大家|众人)")


def _zh_leading_subject(clause: str) -> str:
    """从句首抢救出主语（代词优先）。

    中文长句里没有动作标记时，"参与者"会被整句吞掉（长度超限 → 哨兵）。
    但这类句子的主语往往就是句首的人称代词，实测真实台版稿里「我…」开头的
    叙述句极多。抢救出代词比整体降级更有信息量，也不会引入虚假实体。
    """
    match = _ZH_PRONOUN_RE.match(clause.strip())
    if match:
        return match.group(1)
    return ""


def _zh_subject_is_entity(name: str) -> bool:
    """中文话题提取出的名词是否足以充当参与者（与 ``_jp_subject_is_entity`` 对位）。

    中文没有日语那样的助词边界可依赖，只能靠形态与虚词判据：一旦命中即
    降级为哨兵，交由 ``low_confidence`` 与判官/人工处理——绝不把碎片
    登记成角色名，否则事件对照门会拿碎片去译文里找，永远找不到。
    """
    if not name:
        return False
    name = name.strip()
    if not name:
        return False
    if _ZH_PUNCT_RE.search(name):
        return False
    if len(name) == 1 and name in _ZH_STANDALONE_STOPWORDS:
        return False
    if _ZH_LEADING_FUNCTION_RE.match(name):
        return False
    if _ZH_TRAILING_FUNCTION_RE.search(name):
        return False
    if len(name) > 6:
        return False
    if _ZH_VERB_RESIDUE_RE.search(name):
        return False
    return True


# 日文假名：判定段落是否为日语源（汉字与中文共享，不能作判据）
_KANA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF]")

# 日语话题助词（含复合形式）：把「话题は、述题」切开。
# 注意：**不能**把 `$` 锚定到分句末尾。真实批次实测（10 章日语源）：
# `蒼が指でなぞると` 被尾锚定吃到最后一个助词 `と`，主语变成「蒼が指でなぞる」，
# 判为非专名 → 参与者退化成哨兵（该章 7 个事件里 6 个）；`凛は息を呑んだ` 更因
# 结尾不是助词而完全不匹配。改为匹配**第一个**助词；`の` 不入列表，否则
# `参道の石畳` 会被切成 `参道`。
_JP_TOPIC_RE = re.compile(
    r"^(?P<subj>.+?)(?:では|には|とは|にも|から|まで|より|は|が|を|に|へ|と|も|や)"
)
# 时间/频次状语特征：这类词不是角色名
_JP_ADVERBIAL_RE = re.compile(r"(後|前|頃|時|日|週|月|年|朝|夜|夕|今|昨|明|度|回|間|中)$")
# 可靠的专名形态：纯汉字/片假名/拉丁数字，长度 ≤6，不含平假名助词
_JP_NAME_RE = re.compile(r"^[\u4e00-\u9fff\u30a0-\u30ff\u30fcA-Za-z0-9]{1,6}$")


def _jp_subject_is_entity(name: str) -> bool:
    """日语话题提取出的名词是否足以充当参与者。

    含平假名（多为助词/动词活用）、过长、或带时间状语特征者一律不采信，
    交由 ``待解析角色`` 哨兵与后续判官处理。
    """
    if not name:
        return False
    if _JP_ADVERBIAL_RE.search(name):
        return False
    if not _JP_NAME_RE.match(name):
        return False
    return not _KANA_RE.search(name)


def _extract_japanese_event(
    paragraph: str, clauses: list[str], first_sentence: str
) -> tuple[str, str, float, list[str]]:
    """日语段落抽取：解决中文规则套日语的三类失配。

    1. 「话题は、述题」结构下，动作必须**合并述题**，否则只剩 `月は` 这类碎片；
    2. 话题助词链（`月は`/`外では`）不得直接当角色名；
    3. 不得用中文动作标记（如「道」=说）切分日语句——实测 `参道の石畳は` 会被
       截成单字 `参`。

    单分句也要走话题分支：真实批次里 `凛は息を呑んだ` 这类「主語は＋述語」，
    结尾不是助词，旧的尾锚定正则完全不匹配，参与者退化成哨兵。
    """
    reasons: list[str] = []
    first = clauses[0] if clauses else first_sentence
    topic = _JP_TOPIC_RE.match(first.strip())
    if topic is not None:
        subject = topic.group("subj").strip()
        action = (
            f"{first.strip()}、{clauses[1]}".strip("。！？； ")
            if len(clauses) >= 2
            else first_sentence.strip("。！？； ")
        )
        if _jp_subject_is_entity(subject):
            return subject, action, 0.6, reasons
        reasons.append("low_confidence")
        return _UNPARSED_ROLE, action, 0.3, reasons

    # 无话题结构（或单分句）：主语不可靠，动作保留首句完整语义
    action = first_sentence.strip("。！？； ")
    candidate = first.strip("。！？； ")
    if _jp_subject_is_entity(candidate):
        return candidate, action, 0.6, reasons
    reasons.append("low_confidence")
    return _UNPARSED_ROLE, action, 0.3, reasons


def _extract_event_from_paragraph(
    paragraph: str, *, cultural_terms: set[str] | None = None
) -> tuple[list[str], str, str, float, list[str]]:
    """从段落规则抽取「参与者/动作/结果/置信度/风险原因」。

    - action 只取第一分句；result 只在段落存在第二句及以上时取段尾句，
      否则显式填「无明确结果」，不用同一句冒充结果；
    - 参与者只取动作标记前的主语并剔除介词短语；过长或无法识别时降级
      为「待解析角色」并标 low_confidence；
    - **含假名的段落走日语抽取器**（话题-述题合并、助词边界、不用中文动作标记
      切分），避免把 `月は`/`外では`/`参` 这类碎片当角色名；
    - 段落含 functional_adaptation 词典 source 时标 cultural_function；
    - **非叙事结构段**（`■` 分隔符、`插图p037` 标注、纯章节标题行）返回
      ``_STRUCTURAL_ACTION``，供事件门跳过，避免拿它们当事件核验而永久 fail。
    """
    if is_structural_paragraph(paragraph):
        return [_UNPARSED_ROLE], _STRUCTURAL_ACTION, "无明确结果", 0.0, ["low_confidence"]

    sentences = [s.strip() for s in _SENTENCE_END_RE.split(paragraph) if s.strip()]
    if not sentences:
        sentences = [paragraph.strip()]

    first_sentence = sentences[0]
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(first_sentence) if c.strip()]

    if _KANA_RE.search(first_sentence):
        participant, action, confidence, reasons = _extract_japanese_event(
            paragraph, clauses, first_sentence
        )
        participants = [participant]
    else:
        main_clause = clauses[0] if clauses else first_sentence
        match = _ACTION_MARKER_RE.search(main_clause)
        if match:
            participant_raw = main_clause[: match.start()].strip("，、。！？； ")
            participant = _PREP_TRAILING_RE.sub("", participant_raw).strip() or participant_raw
            action = main_clause.strip("。！？； ")
            confidence = 0.6
        else:
            participant = main_clause.strip("。！？； ")
            action = main_clause.strip("。！？； ")[:120]
            confidence = 0.3

        reasons = []
        # 校验顺序很重要：先判"能不能识别出参与者"，再看"有没有动作标记"。
        # 早期写法把 `not match` 放在前面，于是没有动作标记的段落直接跳过形态校验，
        # 「我回绝了」「「席德」「如果」这类片段照样被登记成角色名。
        if not participant or len(participant) > 8 or re.search(r"[，、]", participant):
            # 长句/无法识别时先尝试从句首抢救主语（代词）；
            # 救不回来才用显式哨兵——绝不使用 paragraph[:8] 伪造 8 字窗口实体。
            salvaged = _zh_leading_subject(main_clause)
            if salvaged:
                participant = salvaged
                confidence = min(confidence, 0.3)
                reasons.append("low_confidence")
            else:
                participant = _UNPARSED_ROLE
                confidence = 0.0
                reasons.append("low_confidence")
        elif not _zh_subject_is_entity(participant):
            # 形态/虚词判据不过：引号碎片、连词、动词片段一律降级为哨兵。
            # 与日语分支对齐——宁可标 low_confidence 交人工/判官，也不能把
            # 「如果」「我回绝了」登记成角色名，让事件对照门拿碎片去译文里找。
            participant = _UNPARSED_ROLE
            confidence = min(confidence, 0.3)
            reasons.append("low_confidence")
        elif not match:
            reasons.append("low_confidence")
        participants = [participant]

    action = action[:120] or paragraph[:120]
    result = sentences[-1].strip("。！？； ")[:120] if len(sentences) >= 2 else "无明确结果"
    result = result or "无明确结果"

    terms = set(cultural_terms or ())
    if terms and any(term in paragraph for term in terms):
        confidence = min(confidence, 0.3)
        reasons.append("cultural_function")

    return participants, action, result, confidence, reasons


def provisional_event_graph(
    chapter_id: str,
    source_sha256: str,
    source_text: str,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """生成规则抽取的事件图初稿。

    - 普通明确段落标 normal，自动通过，不逐条打扰用户；
    - 参与者抓不清/结果缺失/承接连段/含文化功能词标 high + open，
      必须由用户确认或 LLM 增强后再进入改写（spec §8.2）。
    """
    paragraphs = _normalize_paragraphs(source_text)
    if not paragraphs:
        raise ValueError("无法从空文本生成事件图骨架")

    cultural = _cultural_terms()
    events_list: list[dict[str, Any]] = []
    for ordinal, paragraph in enumerate(paragraphs, start=1):
        participants, action, result, confidence, reasons = _extract_event_from_paragraph(
            paragraph, cultural_terms=cultural
        )
        prerequisites: list[str] = []
        if ordinal > 1 and paragraph.startswith(_CONTINUATION_STARTS):
            prerequisites = [event_id_for(ordinal - 1)]
            confidence = min(confidence, 0.3)
            reasons.append("low_confidence")
        has_cultural = any(term in paragraph for term in cultural)
        events_list.append(
            new_event(
                event_id=event_id_for(ordinal),
                ordinal=ordinal,
                participants=participants,
                action=action,
                result=result,
                prerequisites=prerequisites,
                world_function="待确认" if has_cultural else "无",
                confidence=confidence,
                risk=_risk_for_extraction(confidence, reasons),
            )
        )
    graph = build_event_graph(chapter_id, source_sha256, events_list)
    errors = structural_errors(graph)
    if errors:
        raise ValueError("LLM 事件图结构校验失败: " + "; ".join(errors[:5]))
    return graph


def _clean_llm_participants(raw: Any) -> list[str]:
    """过滤 LLM 给出的参与者：只保留像角色名的项。

    模型偶尔仍会把连词/碎片写进 participants（与规则抽取器同类的错误），
    这里用同一套中文形态判据复筛一次，过不了的丢弃。全部被丢弃时返回
    哨兵，交由 low_confidence 与人工/判官处理——不伪造实体。
    """
    rows = raw if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
    cleaned: list[str] = []
    for item in rows:
        name = str(item or "").strip()
        if not name:
            continue
        if name in _UNPARSED_ROLE:
            continue
        # 只用中文判据，不得 OR 上日语判据：日语判据允许"2~6 个纯汉字"，
        # 于是「如果」「所以」「然而」这些中文连词会被它当成合法角色名放行。
        # 中文判据对假名名（アルファ）同样放行，不需要日语判据兜底。
        if _zh_subject_is_entity(name):
            cleaned.append(name)
    return cleaned[:8] or [_UNPARSED_ROLE]


def llm_event_graph(
    chapter_id: str,
    source_sha256: str,
    source_text: str,
    *,
    title: str | None = None,
    config: Any | None = None,
    audit_path: Path | None = None,
    batch_size: int = _LLM_EVENT_BATCH_SIZE,
) -> dict[str, Any]:
    """用 LLM 抽取事件图初稿；失败时由调用方回退规则图。

    LLM 输出必须能解析为与段落数一致的 JSON 事件数组；所有事件按 A 档
    待确认处理（low_confidence + open），用户确认后才可参与 check。

    **必须分批**：整章一次请求会在输出侧被 max_tokens 截断（真实台版序章
    301 段实测 `finish_reason=length`），解析失败 → 静默回退规则抽取器 →
    产出「契机」「就只」「语毕」这类碎片参与者 → 事件对照门恒 fail。
    分批后每批段落数受控，输出不会触顶，且批间不共享上下文避免互相污染。
    """
    import json

    from .llm import LLMConfig, call_llm, load_config

    cfg: LLMConfig = config or load_config()
    paragraphs = _normalize_paragraphs(source_text)
    if not paragraphs:
        raise ValueError("无法从空文本生成 LLM 事件图")
    system = (
        "你是小说事件图抽取器。从给定中文小说章节中抽取段落级事件图。"
        "每个正文段落对应一个事件，段落数必须与输入段落数一致。"
        "每个事件字段：ordinal（段落序号，与输入【P序号】一致）、"
        "participants（字符串数组，写真实角色名；对白段取说话人，"
        "景物/叙述段取可推断的主语，无法确定时用空数组，"
        "**绝对不要**把连词、副词、动词片段、标点符号当角色名）、"
        "action、result、prerequisites（event_id 数组，可空）、"
        "character_goals（可空）、relationship_changes（可空）、"
        "world_function、confidence（0-1）。"
        "只输出 JSON 数组，不要任何说明。"
    )

    batch_size = max(1, int(batch_size))
    events_list: list[dict[str, Any]] = []
    for start_index in range(0, len(paragraphs), batch_size):
        chunk = paragraphs[start_index : start_index + batch_size]
        first = start_index + 1
        last = start_index + len(chunk)
        user = (
            f"章节标题：{title or ''}\n"
            f"（以下为本章第 {first}~{last} 段，共 {len(paragraphs)} 段；"
            f"ordinal 请使用【P序号】里的全局序号）\n\n"
            + "\n\n".join(
                f"【P{i}】{p}" for i, p in enumerate(chunk, start=first)
            )
        )
        raw = call_llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            config=cfg,
            audit_path=audit_path,
            chapter_id=chapter_id,
        )
        text = raw.strip()
        j_start, j_end = text.find("["), text.rfind("]")
        if j_start == -1 or j_end == -1 or j_end <= j_start:
            raise ValueError("LLM 事件图输出不是 JSON 数组")
        payload = json.loads(text[j_start : j_end + 1])
        if not isinstance(payload, list):
            raise ValueError("LLM 事件图 JSON 顶层不是数组")
        if len(payload) != len(chunk):
            raise ValueError(
                f"LLM 事件图批次段落数不一致：事件 {len(payload)} 个，"
                f"本批段落 {len(chunk)} 个（第 {first}~{last} 段）"
            )
        by_ordinal: dict[int, dict[str, Any]] = {}
        for offset, item in enumerate(payload, start=first):
            if not isinstance(item, dict):
                raise ValueError(f"事件 {offset} 不是对象")
            declared = item.get("ordinal")
            ordinal = int(declared) if isinstance(declared, (int, float)) else offset
            by_ordinal[ordinal] = item
        for ordinal in range(first, last + 1):
            item = by_ordinal.get(ordinal) or by_ordinal.get(ordinal - start_index)
            if item is None:
                raise ValueError(f"LLM 事件图缺少段 {ordinal} 的事件")
            events_list.append(
                new_event(
                    event_id=event_id_for(ordinal),
                    ordinal=ordinal,
                    participants=_clean_llm_participants(item.get("participants")),
                    action=str(item.get("action") or paragraphs[ordinal - 1])[:120],
                    result=str(item.get("result") or "无明确结果")[:120],
                    prerequisites=[
                        str(p)
                        for p in (item.get("prerequisites") or [])
                        if str(p).startswith("e-")
                    ],
                    character_goals=[str(g) for g in (item.get("character_goals") or [])],
                    relationship_changes=[
                        str(r) for r in (item.get("relationship_changes") or [])
                    ],
                    world_function=str(item.get("world_function") or "无"),
                confidence=min(max(float(item.get("confidence", 0.5)), 0.0), 1.0),
                risk=high_risk(["low_confidence"], decision_status="open"),
            )
        )
    graph = build_event_graph(chapter_id, source_sha256, events_list)
    errors = structural_errors(graph)
    if errors:
        raise ValueError("LLM 事件图结构校验失败: " + "; ".join(errors[:5]))
    return graph


def _same_event_content(old_event: dict[str, Any], paragraph: str) -> bool:
    """判断旧事件内容是否仍能在新段落中定位（用于段落数变化时保留决策）。"""
    old_action = str(old_event.get("action", "")).strip("。！？； ")
    old_result = str(old_event.get("result", "")).strip("。！？； ")
    if old_action and old_action in paragraph:
        return True
    if old_result and old_result not in {"无明确结果", "待事件图确认"} and old_result in paragraph:
        return True
    return False


def _risk_after_alignment(
    old_event: dict[str, Any] | None,
    paragraph: str,
    confidence: float,
    reasons: list[str],
) -> dict[str, Any]:
    """段落数不变按位置保留已确认决策；段落数变化只保留内容能对上号的决策。"""
    if old_event is None:
        return _risk_for_extraction(confidence, reasons)
    old_risk = old_event.get("risk", {})
    if isinstance(old_risk, dict) and old_risk.get("level") == "high":
        if _same_event_content(old_event, paragraph):
            return copy.deepcopy(old_risk)
        return high_risk(["low_confidence"], decision_status="open")
    return _risk_for_extraction(confidence, reasons)


def align_event_graph_to_output(
    graph: dict[str, Any],
    output_text: str,
    source_sha256: str,
) -> dict[str, Any]:
    """按 output.md 段落对齐事件图，已确认决策不因位置漂移错配。

    - 段落数与事件数一致：按 ordinal 复用旧事件的 risk/decision_id；
    - 段落数变化：只有新旧动作/结果内容能对上号的旧事件保留决策，
      对不上的旧批准作废并重置为 low_confidence + open，等待重确认；
    - 悬空 prerequisites 一律过滤。
    """
    chapter_id = graph.get("chapter_id", "")
    existing_events = sorted(
        graph.get("events", []), key=lambda e: e.get("ordinal", 0)
    )
    paragraphs = _normalize_paragraphs(output_text)
    if not paragraphs:
        raise ValueError("output 文本无段落，无法对齐事件")
    count_changed = len(paragraphs) != len(existing_events)

    kept_ids = {event_id_for(i) for i in range(1, len(paragraphs) + 1)}
    cultural = _cultural_terms()
    new_events: list[dict[str, Any]] = []
    for ordinal, paragraph in enumerate(paragraphs, start=1):
        old_event = existing_events[ordinal - 1] if ordinal <= len(existing_events) else None
        participants, action, result, confidence, reasons = _extract_event_from_paragraph(
            paragraph, cultural_terms=cultural
        )
        if old_event is not None:
            event = copy.deepcopy(old_event)
            risk = (
                copy.deepcopy(old_event.get("risk", {}))
                if not count_changed or _same_event_content(old_event, paragraph)
                else high_risk(["low_confidence"], decision_status="open")
            )
        else:
            event = new_event(
                event_id=event_id_for(ordinal),
                ordinal=ordinal,
                participants=participants,
                action=action,
                result=result,
                world_function="待确认" if any(t in paragraph for t in cultural) else "无",
                confidence=confidence,
                risk=_risk_for_extraction(confidence, reasons),
            )
            risk = event["risk"]

        event["event_id"] = event_id_for(ordinal)
        event["ordinal"] = ordinal
        event["participants"] = participants
        event["action"] = action
        event["result"] = result
        event["risk"] = risk
        event["prerequisites"] = [
            p for p in event.get("prerequisites", []) if p in kept_ids
        ]
        new_events.append(event)

    return build_event_graph(chapter_id, source_sha256, new_events, generated_at=now_iso())
