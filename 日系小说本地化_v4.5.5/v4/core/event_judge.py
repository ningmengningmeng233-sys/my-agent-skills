"""跨语言事件对照判官（v4.5.0-dev.3 清单 P0 路线 A）。

本地化场景（日语源 → 中文正文）中，事件图由**源语言**抽取，`_locate_event`
以子串匹配目标语言正文必然零命中，使 `event_comparison` 永久 fail。本模块
提供判官通道：把**确定性定位失败**的事件连同本章段落交给 LLM，请其裁定该
事件是否已被目标语言段落覆盖。

设计约束（安全优先）：

- 只发送被判事件与本章段落：不发全小说历史、不发密钥、不发决策内部字段；
- 严格遵守「只认真实段落 ID」：编造 ID 在调用方 ``_call_event_judge`` 被丢弃；
- 任何异常/畸形 JSON 都降级为空裁定，绝不崩溃、绝不静默放行；
- 仅在显式启用（``enabled`` 且非 ``dry_run`` 且存在 api_key）时发起调用。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Sequence

from .llm import LLMConfig, LLMError, call_llm, load_config

_SYSTEM_PROMPT = (
    "你是小说本地化流水线的事件对照判官。输入是源语言（如日语）抽取的事件清单，"
    "以及目标语言（中文）章节的段落清单。请判断每个事件是否已被某些段落覆盖。\n"
    "规则：\n"
    "1. 只能使用段落清单里出现过的段落 ID（形如 p-0002）；\n"
    "2. 只依据段落是否转述了该事件的动作与结果来判断，不做文学评价；\n"
    "3. **只有确实找到对应内容时才输出该事件，且 covers 必须为 true**；"
    "找不到就**不要**为该事件输出任何条目（不要输出 covers=false 的条目）；\n"
    "4. **paragraph_ids 只给最能代表该事件的那一处段落**（通常 1 个，最多 2 个"
    "且必须相邻）。正文里重复出现的分隔符、重复的寒暄语容易匹配到多处，"
    "此时请选**该事件在叙事顺序中实际发生的那一处**，不要把所有可能位置都列出"
    "——多列会让事件顺序判定失去唯一性；\n"
    "5. reason 不超过 30 字，且**不要使用英文双引号**（用中文引号「」代替），"
    "否则会破坏 JSON；\n"
    "6. 只输出 JSON，不要输出任何解释性文字或代码围栏。\n"
    '输出格式：{"cover": {"<event_id>": {"covers": true, "paragraph_ids": ["p-0002"], '
    '"reason": "简短理由"}}}'
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
# 逐条抢救：匹配 "事件ID": { ... } 的条目（body 内不允许再出现花括号）
_ENTRY_RE = re.compile(r'"([A-Za-z0-9_\-]+)"\s*:\s*\{([^{}]*)\}', re.DOTALL)
_PIDS_RE = re.compile(r'"paragraph_ids"\s*:\s*\[([^\]]*)\]', re.DOTALL)
_PID_RE = re.compile(r'"(p-[0-9A-Za-z_\-]+)"')
_REASON_RE = re.compile(r'"reason"\s*:\s*"(.*?)"\s*(?:[,}]|$)', re.DOTALL)
_COVERS_RE = re.compile(r'"covers"\s*:\s*(true|false)', re.IGNORECASE)

# 单次送审事件上限。真实批次实测：一次送审 7~8 个未定位事件时，判官只覆盖其中
# 一部分（ch-0006 送审 2 个则全部覆盖，送审 7 个时漏掉 地面/月 两个）；被漏掉的
# 事件随即被 event_comparison 判 fail，整章硬门失败。分批 + 补审可消除这种
# 「与判定能力无关、只与一次问多少有关」的假失败。
_MAX_EVENTS_PER_CALL = 5


def _build_messages(
    events: Sequence[dict[str, Any]],
    pid_list: Sequence[tuple[str, str]],
) -> list[dict[str, str]]:
    event_lines = []
    for event in events:
        event_lines.append(
            json.dumps(
                {
                    "event_id": str(event.get("event_id", "")),
                    "ordinal": event.get("ordinal"),
                    "participants": list(event.get("participants", []) or []),
                    "action": str(event.get("action", "")),
                    "result": str(event.get("result", "")),
                },
                ensure_ascii=False,
            )
        )
    para_lines = [
        json.dumps({"paragraph_id": pid, "text": text}, ensure_ascii=False)
        for pid, text in pid_list
    ]
    user = (
        "【待判定事件】\n"
        + "\n".join(event_lines)
        + "\n\n【目标语言段落】\n"
        + "\n".join(para_lines)
        + "\n\n请按约定 JSON 输出覆盖关系。"
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出中提取首个 JSON 对象；失败返回 None。"""
    if not isinstance(text, str):
        return None
    candidates: list[str] = []
    match = _JSON_OBJECT_RE.search(text)
    if match:
        candidates.append(match.group(0))
    candidates.append(text.strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _salvage_entries(raw: str, valid_pids: set[str]) -> dict[str, dict[str, Any]]:
    """逐条抢救：从整体畸形的响应中抽出仍然完好的条目。

    真实 DeepSeek 实测表明，模型偶尔会在 ``reason`` 中写入未转义的英文双引号，
    使整串 JSON 失效。若因此丢弃全部裁定，跨语言章节会被整体判 fail——一条
    理由的格式瑕疵不应升级为章节级失败。此函数按条目独立解析，坏条目跳过。
    """
    out: dict[str, dict[str, Any]] = {}
    for match in _ENTRY_RE.finditer(raw):
        eid, body = match.group(1), match.group(2)
        if eid in {"cover", "paragraph_ids", "reason", "covers"}:
            continue
        covers = _COVERS_RE.search(body)
        if covers is None or covers.group(1).lower() != "true":
            # 未明确肯定覆盖（缺失 covers 或 covers=false）一律不采纳：
            # 真实实测中模型会对未覆盖事件给出 covers=false 却仍附段落 ID。
            continue
        pids_match = _PIDS_RE.search(body)
        if not pids_match:
            continue
        kept = [p for p in _PID_RE.findall(pids_match.group(1)) if p in valid_pids]
        if not kept:
            continue
        reason_match = _REASON_RE.search(body)
        reason = reason_match.group(1) if reason_match else ""
        out[eid] = {"covers": True, "paragraph_ids": kept, "reason": reason}
    return out


def _parse_verdicts(raw: str, valid_pids: set[str]) -> dict[str, dict[str, Any]]:
    """把模型输出规范化为 {event_id: {covers, paragraph_ids, reason}}。

    先走严格 JSON 快路径；失败则逐条抢救（见 ``_salvage_entries``）。
    两者都要求 ``covers`` 明确为 true——判官未明确肯定即视为未覆盖（fail-closed）。
    """
    parsed = _extract_json(raw)
    if parsed is None:
        return _salvage_entries(raw, valid_pids) if isinstance(raw, str) else {}
    table = parsed.get("cover") if isinstance(parsed.get("cover"), dict) else parsed
    if not isinstance(table, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for eid, verdict in table.items():
        if not isinstance(verdict, dict):
            continue
        if verdict.get("covers") is not True:
            continue
        pids = verdict.get("paragraph_ids")
        if not isinstance(pids, list):
            continue
        kept = [p for p in pids if isinstance(p, str) and p in valid_pids]
        if not kept:
            continue
        out[str(eid)] = {
            "covers": True,
            "paragraph_ids": kept,
            "reason": str(verdict.get("reason", "") or ""),
        }
    return out


def make_event_judge(
    config: LLMConfig | None = None,
    *,
    chapter_id: str = "",
    audit_path: Any = None,
) -> Callable[[Sequence[dict[str, Any]], Sequence[tuple[str, str]]], dict[str, Any]]:
    """构造事件对照判官；返回的 callable 可交给 ``checks.event_comparison_check``。

    分批送审并对未覆盖事件补审一次：判官「漏答」会直接升级为章节级硬门失败，
    而这与判定能力无关，只与一次问多少有关（见 ``_MAX_EVENTS_PER_CALL``）。
    补审仍不覆盖的事件保持未裁定——fail-closed 语义不变。
    """

    def _ask(
        events: Sequence[dict[str, Any]],
        pid_list: Sequence[tuple[str, str]],
        valid_pids: set[str],
    ) -> dict[str, Any]:
        try:
            raw = call_llm(
                _build_messages(events, pid_list),
                config=config or load_config(),
                audit_path=audit_path,
                chapter_id=chapter_id,
            )
        except LLMError:
            return {}
        except Exception:  # noqa: BLE001 - 判官任何故障都不得中断 check
            return {}
        return _parse_verdicts(raw, valid_pids)

    def judge(
        events: Sequence[dict[str, Any]],
        pid_list: Sequence[tuple[str, str]],
    ) -> dict[str, Any]:
        resolved = config or load_config()
        if not events or not pid_list:
            return {}
        if not resolved.enabled or resolved.dry_run or not resolved.api_key:
            return {}
        valid_pids = {pid for pid, _text in pid_list}
        verdicts: dict[str, Any] = {}
        pending = list(events)
        while pending:
            batch, pending = pending[:_MAX_EVENTS_PER_CALL], pending[_MAX_EVENTS_PER_CALL:]
            verdicts.update(_ask(batch, pid_list, valid_pids))
        # 补审：首轮未被覆盖的事件单独再问一次（此时每一批都已 ≤ 上限）。
        unresolved = [
            event
            for event in events
            if str(event.get("event_id", "")) not in verdicts
        ]
        for event in unresolved:
            verdicts.update(_ask([event], pid_list, valid_pids))
        return verdicts

    return judge


# ── 逐句内容覆盖判官（Task 9.9.3）────────────────────────────────────────────
# 复用同一判官通道（同一 LLM 配置、同一审计、同一「只认真实段落 ID」纪律），
# 不另建平行审查体系。判官**只判断语义是否被译文覆盖**，不得改写正文。

_COVERAGE_SYSTEM_PROMPT = (
    "你是小说本地化流水线的逐句覆盖判官。输入是源语言（如日语）的**逐句清单**"
    "（每句带 requirement_id）与目标语言（中文）章节的段落清单。"
    "请判断**每一句的语义**是否已被译文覆盖。\n"
    "规则：\n"
    "1. 只看语义是否被转述，不看句子形式；允许改写、合并、拆分、换序；\n"
    "2. 只要译文某段表达了该句的核心语义（人物、动作、状态、景物变化、收束意），"
    "即判 present；\n"
    "3. 核心语义缺失，或只剩部分要素（例如只译出「地面」而没译出「只剩黑渍」）"
    "判 partial；完全没有对应语义判 absent；无法判断判 uncertain；\n"
    "4. 专名（地名、人名、组织名）以【译名对照】里的目标语译名出现即算命中；"
    "正文里没有源语写法**不是**缺失，不要因此判 partial；\n"
    "5. 只能使用段落清单里出现过的段落 ID（形如 p-0003）；\n"
    "6. reason 不超过 30 字，且不要使用英文双引号（用中文引号「」代替），"
    "否则会破坏 JSON；\n"
    "7. 只输出 JSON，不要输出任何解释性文字或代码围栏。\n"
    '输出格式：{"coverage": {"<requirement_id>": {"status": "present|partial|absent|uncertain", '
    '"paragraph_ids": ["p-0003"], "reason": "简短理由"}}}'
)

# 单次送审句数上限：与事件判官同理（一次问太多会漏答，见 _MAX_EVENTS_PER_CALL）
_MAX_REQUIREMENTS_PER_CALL = 5
_COVERAGE_STATUSES = frozenset({"present", "partial", "absent", "uncertain"})

#: 送审段落窗口上限。**不得全量重发全章段落清单**：真实台版序章 301 段时，
#: 每次调用都把 301 段重发，判官要在 301 段里为 5 个句子找落点，注意力被淹没，
#: 实测 486 个 requirement 里 217 个未取得判定（全部记 uncertain）→ 覆盖门 fail，
#: 而这与成品质量无关。裁剪按"与该批句子有字面/词面关联"优先，再按位置补足窗口。
_MAX_PARAGRAPHS_PER_CALL = 48
#: 兜底窗口：无任何关联命中时送审的段落数（取句序对应位置的邻域）。
_FALLBACK_WINDOW = 30


def _trim_paragraph_window(
    requirements: Sequence[Any], pid_list: Sequence[tuple[str, str]]
) -> list[tuple[str, str]]:
    """为一批 requirement 裁剪出送审段落窗口。

    策略（保守优先，宁可多送也不能把答案裁掉）：
    1. 逐句抽取长度 ≥2 的字面片段（按标点切分），段落包含任一即入选；
    2. 若入选不足 ``_FALLBACK_WINDOW``，按**句序的段落位置**补入邻域
       （源句序号与段落序号在本地化流水线里是强对应的，见段落数保持约束）；
    3. 仍不足时从段落清单头部补齐，保证窗口非空。
    最终按原段落顺序排列，并按 ``_MAX_PARAGRAPHS_PER_CALL`` 截断。
    """
    import re as _re

    if not pid_list:
        return []
    if len(pid_list) <= _MAX_PARAGRAPHS_PER_CALL:
        return list(pid_list)

    chosen: set[int] = set()

    for requirement in requirements:
        text = str(getattr(requirement, "source_text", "") or "")
        fragments = [f for f in _re.split(r"[，。！？；、：\s（）「」『』“”‘’…—]+", text) if len(f) >= 2]
        # 长片段优先（信息量更高），只取前若干避免过散
        for fragment in sorted(fragments, key=len, reverse=True)[:6]:
            for i, (_pid, para) in enumerate(pid_list):
                if fragment in para:
                    chosen.add(i)

    if len(chosen) < _FALLBACK_WINDOW:
        # 按句序定位：requirement 的 id 形如 s-0008-001 → 第 8 段附近
        for requirement in requirements:
            rid = str(getattr(requirement, "id", "") or "")
            match = _re.match(r"^s-(\d{4})-", rid)
            if not match:
                continue
            center = int(match.group(1)) - 1
            for offset in range(-3, 4):
                idx = center + offset
                if 0 <= idx < len(pid_list):
                    chosen.add(idx)
        # 仍不足则从头部补齐
        for i in range(len(pid_list)):
            if len(chosen) >= _FALLBACK_WINDOW:
                break
            chosen.add(i)

    ordered = sorted(chosen)
    if len(ordered) > _MAX_PARAGRAPHS_PER_CALL:
        ordered = ordered[:_MAX_PARAGRAPHS_PER_CALL]
    return [pid_list[i] for i in ordered]


def _name_mapping_block(name_mappings: Sequence[tuple[str, str]] | None) -> str:
    """构造「译名对照」小节；无映射时返回空串（不产生空标题噪声）。

    为什么判官需要它：源句里写的是源语专名（「你怎么会在林德布尔姆？」），
    译文里出现的是它的目标语译名（「你怎么会在圣辉城？」）。判官只看字面就会把
    **正确的译文**判成 partial → 覆盖门 fail → hard_gate 卡死（实测 v005 三条
    残留缺口里有两条属于此类）。
    """
    pairs = [
        (str(source).strip(), str(target).strip())
        for source, target in (name_mappings or ())
        if str(source).strip() and str(target).strip()
    ]
    if not pairs:
        return ""
    lines = [f"{source} → {target}" for source, target in pairs]
    return (
        "【译名对照（源语专名 → 目标语译名）】\n"
        + "\n".join(lines)
        + "\n（专名以其目标语译名出现即视为命中，不要因为源语写法未出现而判 partial）\n\n"
    )


def _build_coverage_messages(
    requirements: Sequence[Any],
    pid_list: Sequence[tuple[str, str]],
    *,
    name_mappings: Sequence[tuple[str, str]] | None = None,
) -> list[dict[str, str]]:
    req_lines = []
    for requirement in requirements:
        req_lines.append(
            json.dumps(
                {
                    "requirement_id": getattr(requirement, "id", ""),
                    "source_sentence": getattr(requirement, "source_text", ""),
                    "linked_events": list(getattr(requirement, "linked_event_ids", ()) or ()),
                },
                ensure_ascii=False,
            )
        )
    para_lines = [
        json.dumps({"paragraph_id": pid, "text": text}, ensure_ascii=False)
        for pid, text in _trim_paragraph_window(requirements, pid_list)
    ]
    user = (
        "【待判定源句】\n"
        + "\n".join(req_lines)
        + "\n\n"
        + _name_mapping_block(name_mappings)
        + "【目标语言段落】\n"
        + "\n".join(para_lines)
        + "\n\n请按约定 JSON 输出逐句覆盖判定。"
    )
    return [
        {"role": "system", "content": _COVERAGE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _parse_coverage_verdicts(raw: str, valid_pids: set[str]) -> dict[str, dict[str, Any]]:
    """解析覆盖判定：只接受白名单状态与真实段落 ID；畸形输出一律丢弃（fail-closed）。"""
    parsed = _extract_json(raw)
    if parsed is None:
        return {}
    table = parsed.get("coverage") if isinstance(parsed.get("coverage"), dict) else parsed
    if not isinstance(table, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for req_id, verdict in table.items():
        if not isinstance(verdict, dict):
            continue
        status = str(verdict.get("status", "") or "").strip().lower()
        if status not in _COVERAGE_STATUSES:
            continue
        pids = verdict.get("paragraph_ids")
        kept = (
            [p for p in pids if isinstance(p, str) and p in valid_pids]
            if isinstance(pids, list)
            else []
        )
        out[str(req_id)] = {
            "status": status,
            "paragraph_ids": kept,
            "evidence": str(verdict.get("reason", "") or ""),
        }
    return out


def make_coverage_judge(
    config: LLMConfig | None = None,
    *,
    chapter_id: str = "",
    audit_path: Any = None,
    name_mappings: Sequence[tuple[str, str]] | None = None,
) -> Callable[[Sequence[Any], Sequence[tuple[str, str]]], dict[str, Any]]:
    """构造逐句覆盖判官；返回 ``judge(requirements, pid_list) -> {req_id: verdict}``。

    与事件判官同一纪律：分批送审（≤5 句/次）、对未判定项补审一次、只认真实段落 ID、
    任何异常降级为空裁定（未判定项在聚合时按 uncertain 处理，绝不静默放行）。

    ``name_mappings`` 是本章生效的「源语专名 → 目标语译名」对照（来自词典
    must_replace 与已接受译名决策）。必须传：源句写的是源语专名，译文里出现的是
    它的译名，判官不知道这层映射就会把正确译文判成 partial。
    """

    def _ask(
        batch: Sequence[Any], pid_list: Sequence[tuple[str, str]], valid: set[str]
    ) -> dict[str, Any]:
        try:
            raw = call_llm(
                _build_coverage_messages(batch, pid_list, name_mappings=name_mappings),
                config=config or load_config(),
                audit_path=audit_path,
                chapter_id=chapter_id,
            )
        except LLMError:
            return {}
        except Exception:  # noqa: BLE001 - 判官故障不得中断 check
            return {}
        return _parse_coverage_verdicts(raw, valid)

    def judge(
        requirements: Sequence[Any], pid_list: Sequence[tuple[str, str]]
    ) -> dict[str, Any]:
        resolved = config or load_config()
        if not requirements or not pid_list:
            return {}
        if not resolved.enabled or resolved.dry_run or not resolved.api_key:
            return {}
        valid = {pid for pid, _text in pid_list}
        verdicts: dict[str, Any] = {}
        pending = list(requirements)
        while pending:
            batch, pending = (
                pending[:_MAX_REQUIREMENTS_PER_CALL],
                pending[_MAX_REQUIREMENTS_PER_CALL:],
            )
            verdicts.update(_ask(batch, pid_list, valid))
        unresolved = [
            item for item in requirements if str(getattr(item, "id", "")) not in verdicts
        ]
        for item in unresolved:
            verdicts.update(_ask([item], pid_list, valid))
        return verdicts

    return judge
