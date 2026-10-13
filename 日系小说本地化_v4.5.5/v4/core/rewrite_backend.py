"""改写后端：规则优先 + LLM 兜底（ADR 0001）。

- 规则层：按 ``dictionary.json`` 三档中的 ``source/target`` 映射做确定性替换；
- LLM 层：规则替换后仍含功能适配元素时，调用 LLM 做语义级本地化；
- 离线/dry-run 时只跑规则层，不伪造 LLM 输出。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from . import checks
from . import exemplar_guard
from . import localization_tone
from . import prompt_assets
from .audit import append_audit, now_iso
from .context_blocks import ContextBlock
from .context_compiler import (
    PromptManifest,
    build_manifest,
    compile_prompt,
    compute_prompt_budget,
    new_prompt_run_id,
    save_prompt_manifest,
)
from .llm import LLMConfig, LLMError, call_llm, load_config
from .rewrite import RewriteContext

RewriteBackend = Callable[[RewriteContext], str]


@dataclass(frozen=True, slots=True)
class Mapping:
    """单条 source → target 映射。"""

    source: str
    target: str
    tier: str
    note: str = ""


def _collect_mappings(tiers: dict[str, list[Any]]) -> list[Mapping]:
    """从 tiers 中提取带 source/target 的映射对象；纯字符串 token 仅用于残留检查，不参与改写。"""
    mappings: list[Mapping] = []
    for tier_name, entries in tiers.items():
        for entry in entries:
            if isinstance(entry, dict) and "source" in entry and "target" in entry:
                mappings.append(
                    Mapping(
                        source=entry["source"],
                        target=entry["target"],
                        tier=tier_name,
                        note=entry.get("note", ""),
                    )
                )
    return mappings


def _replace_all(text: str, source: str, target: str) -> str:
    """单次 source → target 替换，跳过 target 后缀与后续原文重叠的部分。

    场景：source 为 ``ドキドキ``、target 为 ``怦怦直跳``，原文紧跟 ``直跳`` 时，
    普通 ``str.replace`` 会输出 ``怦怦直跳直跳``。本函数在替换时若发现 target
    的某个后缀与 source 之后的文本前缀相同，则吃掉该重叠字符，避免相邻重复词。
    """
    if not source or source == target:
        return text

    src_len = len(source)
    tgt_len = len(target)
    i = 0
    parts: list[str] = []
    while i <= len(text) - src_len:
        if text[i : i + src_len] == source:
            parts.append(target)
            following = text[i + src_len :]
            max_overlap = min(tgt_len, len(following))
            overlap = 0
            for k in range(max_overlap, 0, -1):
                if target[-k:] == following[:k]:
                    overlap = k
                    break
            i += src_len + overlap
        else:
            parts.append(text[i])
            i += 1
    parts.append(text[i:])
    return "".join(parts)


def _replace_mapping(text: str, mapping: Mapping) -> str:
    """替换前掩蔽复合词豁免（君子/夫君、软体动物、土司制度等），避免误伤正常中文。"""
    exceptions = checks.compound_exceptions(mapping.source)
    if not exceptions:
        return _replace_all(text, mapping.source, mapping.target)
    placeholders: dict[str, str] = {}
    masked = text
    for index, exception in enumerate(exceptions):
        if exception not in masked:
            continue
        placeholder = f"\uE000{index:04d}"
        masked = masked.replace(exception, placeholder)
        placeholders[placeholder] = exception
    masked = _replace_all(masked, mapping.source, mapping.target)
    for placeholder, exception in placeholders.items():
        masked = masked.replace(placeholder, exception)
    return masked


def _apply_rule_replacement(text: str, mappings: Sequence[Mapping]) -> str:
    """确定性替换：长 source 优先，避免短 source 被长 source 包含时误替换。

    单字映射做复合词豁免；每条映射使用重叠感知替换，防止 target 后缀与
    原文后续字符拼接产生重复词。
    """
    ordered = sorted(mappings, key=lambda m: len(m.source), reverse=True)
    result = text
    for mapping in ordered:
        if mapping.source not in result:
            continue
        result = _replace_mapping(result, mapping)
    return result


_KEEP_CHOICES = {"保留原文", "保留原名", "保留原词", "保留功能", "保留", "不替换"}


def _accepted_mapping_entries(doc: dict[str, Any]) -> list[dict[str, Any]]:
    entries = doc.get("entries", []) if isinstance(doc, dict) else []
    return [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("status") == "accepted"
        and entry.get("category") == "mapping"
        and isinstance(entry.get("target"), str)
        and entry.get("target")
        and isinstance(entry.get("choice"), str)
        and entry.get("choice")
    ]


def _decision_mappings(context: RewriteContext) -> list[Mapping]:
    """把已接受的项目/章节译名决策转成规则层映射；章节决策覆盖项目决策。

    同一项目词存在多个不同已接受译名时不应用（冲突需用户决策）。
    """
    project_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in _accepted_mapping_entries(context.project_decisions):
        project_groups[entry["target"]].append(entry)

    chosen: dict[str, dict[str, Any]] = {}
    for target, entries in project_groups.items():
        choices = {entry["choice"] for entry in entries}
        if len(choices) == 1:
            chosen[target] = entries[0]
    for entry in _accepted_mapping_entries(context.chapter_decisions):
        chosen[entry["target"]] = entry

    mappings: list[Mapping] = []
    for target, entry in chosen.items():
        choice = entry["choice"]
        if choice in _KEEP_CHOICES:
            continue
        mapped = "" if choice == "删除" else choice
        mappings.append(
            Mapping(
                source=target,
                target=mapped,
                tier="decision",
                note=f"{entry.get('decision_id', '')}:{entry.get('source', {}).get('value', '')}",
            )
        )
    return mappings


def _body_paragraphs(text: str, title: str = "") -> list[str]:
    paragraphs = _split_paragraphs(text)
    if title and paragraphs and paragraphs[0].strip() == title.strip():
        return paragraphs[1:]
    return paragraphs


def _strip_code_fences(text: str) -> str:
    """移除 LLM 可能包裹的 ``` 围栏与语言标记，只保留正文。"""
    text = re.sub(r"```(?:markdown|md|text)?\s*\n?", "", text)
    return text.replace("```", "").strip()


def _looks_like_refusal(text: str, expected_paragraphs: int) -> bool:
    """识别 API 拒答/内容过滤：只认“很短 + 开头出现拒答特征词”的回复。

    长篇正文正常出现「不能/无法」不能判为拒答；段落数不符由调用方单独处理。
    """
    markers = ("无法", "不能", "抱歉", "对不起", "内容政策", "违规", "AI 助手", "我无法处理")
    return len(text) < 200 and any(marker in text[:200] for marker in markers)


def _format_event_graph(context: RewriteContext) -> str:
    lines: list[str] = []
    for event in context.events.get("events", []):
        participants = "、".join(event.get("participants", [])) or "-"
        lines.append(
            f"E{event.get('ordinal', '?')}: 参与者={participants}；"
            f"动作={event.get('action', '')}；结果={event.get('result', '')}；"
            f"世界观功能={event.get('world_function', '无')}"
        )
    return "\n".join(lines)


def _format_decision_summary(context: RewriteContext) -> str:
    accepted = []
    for label, doc in (
        ("项目", context.project_decisions),
        ("章节", context.chapter_decisions),
    ):
        for entry in doc.get("entries", []):
            if entry.get("status") != "accepted":
                continue
            accepted.append(
                f"[{label}] {entry.get('target', '')} -> {entry.get('choice', '')}"
            )
    return "\n".join(accepted[:40]) or "（无已接受决策）"


def _relevant_dictionary(context: RewriteContext, text: str) -> str:
    lines: list[str] = []
    for tier in ("must_replace", "functional_adaptation", "default_keep"):
        for entry in context.rules_tiers.get(tier, []):
            source = entry.get("source", entry) if isinstance(entry, dict) else entry
            target = entry.get("target", "") if isinstance(entry, dict) else ""
            if isinstance(source, str) and source in text:
                lines.append(f"[{tier}] {source} -> {target or '（保留）'}")
    return "\n".join(lines[:40]) or "（本章无相关词典条目）"


def _split_paragraphs(text: str) -> list[str]:
    """按空行分段并清理。"""
    paragraphs: list[str] = []
    for block in text.split("\n\n"):
        cleaned = block.strip()
        if cleaned:
            paragraphs.append(cleaned)
    return paragraphs


def _strip_confirmed_meta_paragraphs(
    paragraphs: list[str], context: RewriteContext
) -> list[str]:
    """已确认“剥离元文本”决策时，真正删除含强元文本标记的段落。"""
    docs = (context.project_decisions, context.chapter_decisions)
    confirmed = any(
        isinstance(entry, dict)
        and entry.get("status") == "accepted"
        and entry.get("choice") == "剥离元文本"
        for doc in docs
        for entry in doc.get("entries", [])
    )
    if not confirmed:
        return paragraphs
    from .precheck import strong_meta_markers

    markers = strong_meta_markers()
    kept: list[str] = []
    removed = 0
    for paragraph in paragraphs:
        compact = re.sub(r"\s+", "", paragraph)
        if any(re.sub(r"\s+", "", marker) in compact for marker in markers):
            removed += 1
            continue
        kept.append(paragraph)
    if removed:
        _audit_fallback(
            context.workspace,
            context.chapter_id,
            f"meta_text_stripped: {removed} 段（用户已确认剥离元文本）",
        )
    return kept


def rule_based_rewrite(context: RewriteContext) -> str:
    """规则层改写：应用词典/译名映射，并执行已确认的元文本剥离。"""
    mappings = _collect_mappings(context.rules_tiers) + _decision_mappings(context)
    paragraphs = _split_paragraphs(context.source_text)
    paragraphs = _strip_confirmed_meta_paragraphs(paragraphs, context)
    title = (context.title or "").strip()

    output_paragraphs: list[str] = []
    for paragraph in paragraphs:
        # 若首段与章节标题完全一致，则在 output 中保留一次标题即可
        if title and paragraph == title and not output_paragraphs:
            continue
        output_paragraphs.append(_apply_rule_replacement(paragraph, mappings))

    body = "\n\n".join(output_paragraphs)
    if title:
        return f"{title}\n\n{body}"
    return body


def _needs_llm(paragraph: str, mappings: Sequence[Mapping]) -> bool:
    """兼容旧调用：段落仍含功能适配 source 时需要 LLM 兜底。

    当前质量优先策略是在线即整章调用；本函数仅供测试与外部调用者判断。
    """
    return any(
        m.tier == "functional_adaptation" and m.source in paragraph for m in mappings
    )



_SCENE_SIGNALS: dict[str, tuple[str, ...]] = {
    "打脸碾压": ("打脸", "碾压", "秒杀", "扮猪", "隐藏实力", "展现实力", "震慑", "身份反转", "马甲", "众人震惊", "不敢置信"),
    "围观震撼": ("围观", "震惊", "哗然", "议论", "倒吸", "目瞪口呆", "全场"),
    "战斗对决": ("战斗", "对决", "出招", "交手", "闪避", "拔剑", "剑光", "剑锋", "出剑", "魔法", "试炼", "比赛"),
    "升级突破": ("突破", "晋级", "境界", "觉醒", "变强", "修炼", "提升"),
    "感情线": ("喜欢", "爱上", "心动", "心跳", "脸红", "告白", "暧昧", "约会", "牵手", "拥抱", "吃醋"),
    "日常过渡": ("马车", "学园", "校园", "教室", "社团", "逛街", "吃饭", "聊天", "日常", "散步", "休息", "沉默", "天气"),
    "搞笑吐槽": ("吐槽", "搞笑", "玩笑", "得意", "装傻", "玩梗", "脑补"),
    "危机化解": ("危机", "危险", "绝境", "逃跑", "逃走", "追杀", "阴谋", "陷阱", "生死"),
    "恐怖悬疑": ("恐怖", "诡异", "阴森", "失踪", "悬疑", "尸体", "调查"),
    "虐心催泪": ("眼泪", "流泪", "大哭", "离别", "遗憾", "心痛", "死亡"),
    "复仇归来": ("复仇", "归来", "血债", "仇人", "王者归来"),
    "金句霸气": ("金句", "霸气", "宣言", "名台词"),
}

#: 显著性命中阈值。分数必须达到「绝对下限」与「最高分的该比例」两者中的较大值，
#: 才算场景证据。真实章节实测：「恐怖悬疑」靠「尸体×1＋调查×2」拿到 3 分、
#: 「战斗对决」靠通用名词「剑」拿到 22 分，而首位场景是 35 分；这种量级的命中
#: 不构成证据，却足以挤进前三并改变选卡结果（详见 v4/tests/test_scene_signals.py）。
_MIN_SCENE_SCORE = 3
_MIN_SCENE_RELATIVE = 0.20


def _derive_scene_queries(title: str, text: str, *, limit: int = 3) -> list[str]:
    """按章节标题+正文信号词推导场景，避免用整段标题直接匹配文风卡导致零命中。

    信号词表只收**场景特有的行为/情绪**，不收单字与通用名词：「剑」这类器物
    在任何幻想题材里都高频，拿它当战斗证据会把日常章判成战斗章，进而把修真
    大战的范例喂给学园日常章（审计实证：一个无战斗的学园章被喂了《仙逆》
    第1005~1014章作 style_card 与 style_exemplar）。滤噪见
    ``_MIN_SCENE_SCORE`` / ``_MIN_SCENE_RELATIVE``。
    """
    haystack = f"{title}\n{text[:12000]}"
    scored: list[tuple[int, str]] = []
    for scene, signals in _SCENE_SIGNALS.items():
        score = sum(haystack.count(word) for word in signals)
        if score > 0:
            scored.append((score, scene))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if scored:
        top = scored[0][0]
        # 首位场景恒保留（min 保证阈值不高于最高分），只滤掉下方的噪声场景。
        threshold = min(top, max(_MIN_SCENE_SCORE, top * _MIN_SCENE_RELATIVE))
        scored = [item for item in scored if item[0] >= threshold]
    scenes = [scene for _score, scene in scored[:limit]]
    if not scenes:
        scenes = ["打脸碾压", "日常过渡"]
    if "章末钩子" not in scenes:
        scenes.append("章末钩子")
    return scenes


def _card_snippet(card: dict[str, Any]) -> str:
    """把文风卡压缩成高信号规则块：一句话指纹 + 轴规则/观察，不复制锚点原句。

    2026-09-16 起改为**覆盖全部轴**、上限放宽（原为 axes[:4] / 1100 字）：
    实测旧口径下单张卡只有约 15% 的内容进到 prompt，模型学不到卡的文风，
    产出被用户判为"看不出学了库"。轴是有序的，砍掉后半段等于砍掉卡的收尾
    与雷区部分，故不再按轴数截断，只保留一个防爆量的字符上限。
    """
    lines: list[str] = []
    fingerprint = card.get("fingerprint")
    if fingerprint:
        lines.append(f"一句话指纹：{str(fingerprint)[:120]}")
    for axis in card.get("axes", []):
        rules = [str(rule).strip() for rule in axis.get("rules", []) if str(rule).strip()][:6]
        observations = [str(item).strip() for item in axis.get("observations", []) if str(item).strip()][:3]
        if not rules and not observations:
            continue
        joined = "；".join(rules + observations)
        lines.append(f"[{axis.get('axis', '')}] {joined}")
    return "\n".join(lines)[:_CARD_SNIPPET_MAX_CHARS]


_MAX_STYLE_CARDS = 1
_MAX_INDEX_CHAPTERS = 1

#: 单张文风卡注入 prompt 的字符上限。旧值 1100（且只取 axes[:4]）实测覆盖率约 15%，
#: 模型看不到卡的收尾/雷区轴，用户判定"看不出学了库"；放宽后再靠 Token 预算裁决。
_CARD_SNIPPET_MAX_CHARS = 2600
#: 文风范例（真实网文正文）每块字符上限与默认块数。范例是"学笔触"的唯一来源，
#: 但必须是真句子，故不截断句子内部，只按整段累计到上限。
#: 默认 2 段对应 A 方案契约下限（普通章 2 / 复杂章 3，由 PromptBudget 覆盖）。
_EXEMPLAR_MAX_CHARS = 700
_MAX_EXEMPLARS = 2
#: 范例块头。防抄守卫按此前缀回收范例原文（v4.core.exemplar_guard）。
_EXEMPLAR_HEADER = (
    "【文风范例（只学节奏与质感：禁止照抄任何一句、禁止引入范例中的人物与情节）】"
)


def _is_complex_chapter(context: RewriteContext, scene_queries: list[str], text: str) -> bool:
    """按章节复杂度信号判定是否启用 2+2 参考配额（多场景/长章/事件密集）。"""
    events = context.events.get("events", []) if isinstance(context.events, dict) else []
    event_count = len(events) if isinstance(events, list) else 0
    complex_signals = sum(
        [
            len(scene_queries) >= 4,
            event_count >= 6,
            len(text) >= 7000,
            any(k in scene_queries for k in ("战斗", "感情线", "反转")),
        ]
    )
    return complex_signals >= 2


def _index_snippets(
    index_lib: Any,
    scene_queries: list[str],
    *,
    chapter_id: str,
    limit: int = _MAX_INDEX_CHAPTERS,
    select_provider: Any | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """从章节索引按场景标签取一条「怎么写」条目，避免参考资产挤占正文。

    ``select_provider`` 存在时，记录“选取”经统一协议
    （``provider.search_chapter_indexes``），内容抽取仍走库对象。
    """
    parts: list[str] = []
    used: list[dict[str, Any]] = []
    if select_provider is not None:
        try:
            records = list(
                select_provider.search_chapter_indexes(scene_queries, limit=limit)
            )
        except Exception:  # noqa: BLE001
            return parts, used
    else:
        try:
            records = index_lib.search_tags(scene_queries, kinds=["index_chapter"], limit=limit)
        except Exception:  # noqa: BLE001
            return parts, used
    for record in records[:limit]:
        try:
            sections = index_lib.get_chapter_index(record.rel_path)
        except Exception:  # noqa: BLE001
            continue
        chosen: list[str] = []
        preferred = ("创作手法", "金句", "名场面")
        fallback = ("爽点", "看点", "伏笔")
        for section in sections[:4]:
            for subsection_name, items in section.subsections.items():
                if any(key in subsection_name for key in preferred):
                    for item in items[:2]:
                        text_item = " ".join(part if isinstance(part, str) else str(part) for part in item) if isinstance(item, tuple) else str(item)
                        chosen.append(text_item[:130])
                    break
            if len(chosen) >= 5:
                break
        if not chosen:
            for section in sections[:2]:
                for subsection_name, items in section.subsections.items():
                    if not any(key in subsection_name for key in fallback):
                        continue
                    for item in items[:2]:
                        text_item = " ".join(part if isinstance(part, str) else str(part) for part in item) if isinstance(item, tuple) else str(item)
                        chosen.append(text_item[:130])
                    if len(chosen) >= 4:
                        break
                if len(chosen) >= 4:
                    break
        if not chosen:
            continue
        # 原文结构摘要：只提炼节奏/结构，不直接喂原文句面
        source_rel = record.metadata.get("源文件")
        line_range = sections[0].line_range if sections else None
        if source_rel and hasattr(index_lib, "original_structure_digest"):
            digest = index_lib.original_structure_digest(str(source_rel), line_range)
            if digest:
                chosen.insert(0, "原文结构摘要：" + digest)
                try:
                    original_sha = index_lib.hash_asset(str(source_rel))
                except Exception:  # noqa: BLE001
                    original_sha = "0" * 64
                used.append(
                    {
                        "chapter_id": chapter_id,
                        "asset_kind": "index_original",
                        "asset_path": str(source_rel),
                        "asset_sha256": original_sha,
                        "purpose": "structure",
                        "selection_reason": f"从章节索引定位原文 {source_rel} 提炼结构摘要",
                        "location": str(line_range),
                    }
                )
        label = f"[索引库章节索引 {record.rel_path}] " + "；".join(chosen[:6])[:900]
        parts.append(label)
        used.append(
            {
                "chapter_id": chapter_id,
                "asset_kind": "index_chapter",
                "asset_path": record.rel_path,
                "asset_sha256": index_lib.hash_asset(record.rel_path),
                "purpose": "scene",
                "selection_reason": f"场景信号命中: {scene_queries[:3]}",
                "location": record.rel_path,
            }
        )
    return parts, used


def _negative_rules_from_card(card: dict[str, Any]) -> list[str]:
    """从文风卡里提取“不可写/雷区/禁令”类反例规则。"""
    rules: list[str] = []
    for axis in card.get("axes", []):
        axis_name = str(axis.get("axis", ""))
        if not any(k in axis_name for k in ("不可写", "雷区", "禁止", "禁令")):
            continue
        for rule in axis.get("rules", []):
            text = str(rule).strip()
            if text and text not in rules:
                rules.append(text)
    return rules[:10]


def _exemplar_parts(
    index_lib: Any,
    scene_queries: list[str],
    *,
    chapter_id: str,
    limit: int = _MAX_EXEMPLARS,
    max_chars: int = _EXEMPLAR_MAX_CHARS,
) -> tuple[list[str], list[dict[str, Any]]]:
    """注入**真实网文正文**片段作为文风范例——"笔触"只能从真句子里学。

    与 ``_index_snippets`` 的分工：章节索引只给节奏统计与要点（0 句面），
    本函数给真句子。为防照抄，块头写明"禁止照抄任何一句、禁止引入范例中的
    人物与情节"，并由 ``v4.core.exemplar_guard`` 在检查侧做 n-gram 重合拦截。
    """
    parts: list[str] = []
    used: list[dict[str, Any]] = []
    if index_lib is None or not hasattr(index_lib, "original_prose_fragment"):
        return parts, used
    try:
        if hasattr(index_lib, "find_exemplar_chapter_indexes"):
            # 按书优选：场景标签检索没有区分度（3536 条章节索引里 1774 条带
            # 「日常过渡」标签，并列后按文件名字典序，仙逆恒胜），且目标书
            # 可能一条都不带该标签（龙族 9 条索引全无「日常过渡」）。
            records = list(
                index_lib.find_exemplar_chapter_indexes(scene_queries, limit=limit)
            )
        else:
            records = list(
                index_lib.search_tags(scene_queries, kinds=["index_chapter"], limit=limit)
            )
    except Exception:  # noqa: BLE001 - 取不到范例不影响改写
        return parts, used

    for record in records[:limit]:
        try:
            sections = index_lib.get_chapter_index(record.rel_path)
        except Exception:  # noqa: BLE001
            continue
        source_rel = record.metadata.get("源文件") if getattr(record, "metadata", None) else None
        if not source_rel:
            continue
        line_range = sections[0].line_range if sections else None
        try:
            fragment = index_lib.original_prose_fragment(
                str(source_rel), line_range, max_chars=max_chars
            )
        except Exception:  # noqa: BLE001
            continue
        if not fragment.strip():
            continue
        parts.append(f"{_EXEMPLAR_HEADER}\n{fragment}")
        try:
            original_sha = index_lib.hash_asset(str(source_rel))
        except Exception:  # noqa: BLE001
            original_sha = "0" * 64
        used.append(
            {
                "chapter_id": chapter_id,
                "asset_kind": "style_exemplar",
                "asset_path": str(source_rel),
                "asset_sha256": original_sha,
                "purpose": "style_texture",
                "selection_reason": f"场景信号命中: {scene_queries[:3]}；取真实正文片段作笔触范例",
                "location": str(source_rel),
            }
        )
    return parts, used


def _reference_parts(
    context: RewriteContext,
    text: str,
    *,
    style_card_limit: int = _MAX_STYLE_CARDS,
    index_chapter_limit: int = _MAX_INDEX_CHAPTERS,
    exemplar_limit: int = _MAX_EXEMPLARS,
) -> tuple[list[str], list[dict[str, Any]]]:
    """构造只读参考资产块列表；返回 (块文本列表, 用于审计的资产列表)。

    v4.5.0-dev.3 起：
    - 配额由 ``compute_prompt_budget`` 决定（普通 1+1 卡与索引 + 2 范例；
      复杂 2+2 + 3 范例）；
    - 不再做字符级截断，块级取舍交给 ``compile_prompt`` 按 Token 预算与权威级裁决；
    - 从章节标题+正文推导场景（打脸/战斗/感情线/吐槽…），而不是拿长标题硬匹配；
    - 文风卡输出「一句话指纹 + 轴规则/观察」，不复制锚点句面；
    - 文风范例取真实正文片段（笔触通道），每段受 ``_EXEMPLAR_MAX_CHARS`` 限长。
    """
    parts: list[str] = []
    used: list[dict[str, Any]] = []
    scene_queries = _derive_scene_queries(context.title or "", text)
    max_style_cards = style_card_limit
    max_index_chapters = index_chapter_limit
    max_exemplars = exemplar_limit

    provider = getattr(context, "knowledge_provider", None)
    if provider is not None:
        # v4.5.0-dev.3（Task 6.5）：参考资产“选取”经统一 Provider 协议；
        # 内容抽取经 provider.get()/provider.fingerprint()，不再直连 IndexLibrary。
        card_records = []
        try:
            card_records = list(
                provider.search_style_cards(scene_queries, limit=max_style_cards)
            )
        except Exception:  # noqa: BLE001 - 选取失败时落入稳定保底路径
            card_records = []
        for record in card_records:
            try:
                card = provider.get(record.rel_path)
                snippet = _card_snippet(card)
                if not snippet.strip():
                    continue
                parts.append(f"[索引库文风卡 {record.rel_path}]\n{snippet}")
                used.append(
                    {
                        "chapter_id": context.chapter_id,
                        "asset_kind": "style_card",
                        "asset_path": record.rel_path,
                        "asset_sha256": provider.fingerprint(record.rel_path),
                        "purpose": "scene",
                        "selection_reason": f"统一协议选取；场景信号命中: {scene_queries[:3]}",
                        "location": record.rel_path,
                    }
                )
                negative_rules = _negative_rules_from_card(card)
                if negative_rules:
                    parts.append(
                        "[索引库反例/禁止清单]\n- "
                        + "\n- ".join(negative_rules[:8])
                    )
                    used.append(
                        {
                            "chapter_id": context.chapter_id,
                            "asset_kind": "style_card",
                            "asset_path": record.rel_path,
                            "asset_sha256": provider.fingerprint(record.rel_path),
                            "purpose": "negative_example",
                            "selection_reason": f"从 {record.rel_path} 抽取不可写/雷区反例",
                            "location": record.rel_path,
                        }
                    )
            except Exception:  # noqa: BLE001 - 单个参考资产损坏不阻塞改写
                continue

        index_parts, index_used = _index_snippets(
            provider.library if hasattr(provider, "library") else context.index_lib,
            scene_queries,
            chapter_id=context.chapter_id,
            limit=max_index_chapters,
            select_provider=provider,
        )
        parts.extend(index_parts)
        used.extend(index_used)

    elif context.index_lib is not None:
        card_records: list[Any] = []
        unverified_paths: set[str] = set()
        seen_cards: set[str] = set()
        if hasattr(context.index_lib, "find_style_cards"):
            for scene in scene_queries:
                verified = context.index_lib.find_style_cards(
                    scene, limit=max_style_cards, verified_only=True
                )
                unverified = [
                    r for r in context.index_lib.find_style_cards(scene, limit=max_style_cards)
                    if r.rel_path not in {item.rel_path for item in verified}
                ]
                for record in verified + unverified:
                    if record.rel_path in seen_cards:
                        continue
                    seen_cards.add(record.rel_path)
                    card_records.append(record)
                    if record in unverified:
                        unverified_paths.add(record.rel_path)
                    if len(card_records) >= max_style_cards:
                        break
                if len(card_records) >= max_style_cards:
                    break
        if not card_records and hasattr(context.index_lib, "assets"):
            for record in context.index_lib.assets("style_card"):
                if "audit_log" in record.rel_path:
                    continue
                card_records.append(record)
                if len(card_records) >= max_style_cards:
                    break
        for record in card_records:
            try:
                card = context.index_lib.get_style_card(record.rel_path)
                snippet = _card_snippet(card)
                if not snippet.strip():
                    continue
                parts.append(f"[索引库文风卡 {record.rel_path}]\n{snippet}")
                used.append(
                    {
                        "chapter_id": context.chapter_id,
                        "asset_kind": "style_card",
                        "asset_path": record.rel_path,
                        "asset_sha256": context.index_lib.hash_asset(record.rel_path),
                        "purpose": "scene",
                        "selection_reason": (
                            f"场景信号命中: {scene_queries[:3]}"
                            + (" [unverified_fallback]" if record.rel_path in unverified_paths else "")
                        ),
                        "location": record.rel_path,
                    }
                )
                negative_rules = _negative_rules_from_card(card)
                if negative_rules:
                    parts.append(
                        "[索引库反例/禁止清单]\n- "
                        + "\n- ".join(negative_rules[:8])
                    )
                    used.append(
                        {
                            "chapter_id": context.chapter_id,
                            "asset_kind": "style_card",
                            "asset_path": record.rel_path,
                            "asset_sha256": context.index_lib.hash_asset(record.rel_path),
                            "purpose": "negative_example",
                            "selection_reason": f"从 {record.rel_path} 抽取不可写/雷区反例",
                            "location": record.rel_path,
                        }
                    )
            except Exception:  # noqa: BLE001 - 单个参考资产损坏不阻塞改写
                continue

        index_parts, index_used = _index_snippets(
            context.index_lib, scene_queries, chapter_id=context.chapter_id, limit=max_index_chapters
        )
        parts.extend(index_parts)
        used.extend(index_used)

    # 文风范例：只要拿到索引库就注入，不区分 provider / 直连两条取值路径——
    # 早前误放进 elif 分支，而 CLI 恒定走 provider 分支，导致范例从未生效。
    if context.index_lib is not None:
        exemplar_parts, exemplar_used = _exemplar_parts(
            context.index_lib,
            scene_queries,
            chapter_id=context.chapter_id,
            limit=max(1, max_exemplars),
        )
        parts.extend(exemplar_parts)
        used.extend(exemplar_used)

    if context.writing_guide is not None:
        modules = ("chapter_ending",) if "章末钩子" in scene_queries else ("dialogue",)
        for module in modules:
            try:
                rules = context.writing_guide.module_rules(module)
                if rules.hard_rules:
                    snippet = "；".join(rules.hard_rules)[:240]
                    parts.append(f"[写作指导 {module}] {snippet}")
                selection = context.writing_guide.retrieve_examples(
                    module, text[:200], positive_n=1, negative_n=1
                )
                for item in (selection.positive + selection.negative)[:3]:
                    parts.append(
                        f"[写作指导示例 {'正例' if not item.is_negative else '反例'}] "
                        f"{item.snippet[:160]}"
                    )
                file_record = next(iter(rules.files.values()), None)
                if file_record is not None:
                    used.append(
                        {
                            "chapter_id": context.chapter_id,
                            "asset_kind": "writing_module",
                            "asset_path": file_record.rel_path,
                            "asset_sha256": file_record.sha256,
                            "purpose": "consistency" if module == "consistency_review" else module,
                            "selection_reason": f"改写前加载 {module} 模块规则与示例",
                            "location": file_record.rel_path,
                        }
                    )
            except Exception:  # noqa: BLE001 - 单个模块损坏不阻塞改写
                continue

    # 长篇连续性：仅注入与本章角色重合的最近快照，不读取整本历史。
    if context.workspace is not None:
        try:
            from .continuity import relevant_context

            names: list[str] = []
            for event in (context.events.get("events", []) if isinstance(context.events, dict) else []):
                for name in event.get("participants", []) or []:
                    if str(name) not in names:
                        names.append(str(name))
            continuity = relevant_context(context.workspace, names, limit=2)
            if continuity:
                compact = []
                for row in continuity:
                    facts = "；".join(
                        f"{item.get('action', '')}->{item.get('result', '')}" for item in row.get("facts", [])[:3]
                    )
                    compact.append(f"{row.get('chapter_id', '')}: {facts[:160]}")
                parts.append("[长篇连续性快照]\n" + "\n".join(compact)[:420])
        except Exception:  # noqa: BLE001 - 连续性增强不可阻断改写
            pass

    return parts, used


def _reference_block(
    context: RewriteContext, text: str
) -> tuple[str, list[dict[str, Any]]]:
    """兼容封装：把参考块列表拼成一段文本（cli 审计路径仍使用资产列表）。"""
    parts, used = _reference_parts(context, text)
    return "\n".join(parts), used


def _format_event_graph_range(context: RewriteContext, start: int, end: int) -> str:
    """只输出指定 ordinal 范围内的事件，避免长章节 prompt 超限。"""
    lines: list[str] = []
    for event in context.events.get("events", []):
        ordinal = event.get("ordinal")
        if isinstance(ordinal, int) and start <= ordinal <= end:
            participants = "、".join(event.get("participants", [])) or "-"
            lines.append(
                f"E{ordinal}: 参与者={participants}；"
                f"动作={event.get('action', '')}；结果={event.get('result', '')}；"
                f"世界观功能={event.get('world_function', '无')}"
            )
    return "\n".join(lines) or "（该分段无事件）"


_REFERENCE_AUTHORITY_PREFIXES: tuple[tuple[str, str, int], ...] = (
    ("[长篇连续性快照]", "continuity", 53),
    ("[索引库文风卡", "style_rule", 50),
    ("[索引库反例/禁止清单]", "style_rule", 54),
    ("[索引库章节索引", "chapter_index", 51),
    ("[写作指导", "writing_guide", 52),
)


def _reference_authority(part: str) -> tuple[str, int]:
    """按块前缀映射编译权威级与展示优先级（权威级决定预算裁剪顺序）。"""
    for prefix, authority, priority in _REFERENCE_AUTHORITY_PREFIXES:
        if part.startswith(prefix):
            return authority, priority
    return "example", 55


def build_prompt_run(
    context: RewriteContext,
    rule_output: str,
    *,
    paragraph_start: int = 1,
    paragraph_end: int | None = None,
    config: LLMConfig | None = None,
    input_budget_override: int | None = None,
    retry_feedback: str | None = None,
) -> tuple[list[dict[str, str]], PromptManifest]:
    """经 ContextCompiler 编译正式改写 prompt，并产出可落盘的 PromptManifest。

    ``paragraph_start/paragraph_end`` 是全局段落序号（1 起）；分段调用时
    只放对应段落和事件，但系统提示仍要求保持全局事件图语义。
    必选块（事件图/决策/词典/规则稿段落）合计超预算时抛 ``PromptBudgetExceeded``。

    ``retry_feedback`` 为重试时的纠正指令（上一轮被判失败的具体原因）。真实批次
    实测：不带纠正信息的重试是**字节级重发同一 prompt**（首次与重试的
    ``request_hash`` 完全相同），模型重复同样错误，重试等同掷硬币。
    """
    body_paragraphs = _body_paragraphs(rule_output, context.title)
    if paragraph_end is None:
        paragraph_end = len(body_paragraphs)
    chunk = body_paragraphs[paragraph_start - 1 : paragraph_end]
    numbered = "\n\n".join(
        f"【P{index}】{paragraph}"
        for index, paragraph in enumerate(chunk, start=paragraph_start)
    )
    event_graph = (
        _format_event_graph(context)
        if paragraph_start == 1 and paragraph_end == len(body_paragraphs)
        else _format_event_graph_range(context, paragraph_start, paragraph_end)
    )
    local_rule_block = prompt_assets.build_local_rule_block()
    voice_hints = prompt_assets.load_voice_hints(context.workspace)
    # 增写开关：关闭时本段文案与改造前逐字一致；开启时把「只许润色」换成
    # 「按白名单增写」，与 prompt_assets.hard_instruction(expanding=True) 保持一致。
    expanding = prompt_assets.expansion_enabled()
    system = (
        "你是中文日系小说本地化编辑。请在已做基础替换的规则稿基础上，"
        "把给定文本进一步本地化为自然、可读的中文小说正文。"
        "必须保留事件图、人物、事件顺序、因果关系和世界观功能；"
        "不得增删或合并段落，不得改变段落数量；"
        + (
            "段落内部允许润色、调整表达，并按【允许增写】白名单增写，"
            if expanding
            else "段落内部可以润色和调整表达，"
        )
        + "但每个事件都必须能在对应段落定位；"
        "必须保留源稿每一句的叙事事实、动作、状态、景物变化和收束信息；"
        "段末景物、余韵、结果句同样属于必译内容，不得因「信息量低」而省略；"
        "可以重组表达，但不得删除任何源句的核心语义；"
        "不得为了简洁而牺牲描写：源稿的景物、氛围、光线、声音、动作细节与语气词"
        "属于笔触，必须保留其质感与分量，不得为压字数而淡化、抽象化或删去；"
        "句子长短要有起伏：允许并需要写舒展的长句长段，不要把所有句子都砍成"
        "同样长度的短句——通篇一律的短句就是白开水；"
        "输出格式：不要输出全局段落编号；每个正文段落之间用空行分隔；"
        "不要输出任何计划/说明/质检/映射/审批/审计元数据，"
        "不要使用 Markdown 代码围栏。"
    ) + "\n\n" + prompt_assets.build_system_prompt(local_rule_block, voice_hints)

    scene_queries = _derive_scene_queries(context.title or "", rule_output)
    output_reserve = config.max_tokens if config is not None else 4096
    budget = compute_prompt_budget(
        is_complex=_is_complex_chapter(context, scene_queries, rule_output),
        output_reserve=output_reserve,
    )
    input_budget = (
        input_budget_override if input_budget_override is not None else budget.input_budget
    )

    dictionary_text = _relevant_dictionary(context, context.source_text + "\n" + rule_output)
    blocks: list[ContextBlock] = [
        ContextBlock(
            block_id="event_graph", kind="event_graph", authority="event_graph",
            priority=20, pinned=True, content=f"【相关事件图】\n{event_graph}",
        ),
        ContextBlock(
            block_id="accepted_decisions", kind="accepted_decision", authority="accepted_decision",
            priority=30, pinned=True, content=f"【已接受决策】\n{_format_decision_summary(context)}",
        ),
        ContextBlock(
            block_id="locked_dictionary", kind="terminology", authority="hard_rule",
            priority=40, pinned=True,
            content=f"【锁定词典（源词不得再出现，目标词不得改回）】\n{dictionary_text}",
        ),
        ContextBlock(
            block_id="rule_paragraphs", kind="source_paragraphs", authority="source",
            priority=60, pinned=True,
            content=(
                f"【规则稿段落（全局第 {paragraph_start}~{paragraph_end} 段，"
                f"共 {len(chunk)} 段，段落数不可变）】\n"
                f"{_chunk_length_guidance(chunk)}\n{numbered}"
            ),
        ),
    ]
    reference_parts, _used = _reference_parts(
        context,
        rule_output,
        style_card_limit=budget.style_card_limit,
        index_chapter_limit=budget.index_chapter_limit,
        exemplar_limit=budget.exemplar_limit,
    )
    for index, part in enumerate(reference_parts):
        authority, priority = _reference_authority(part)
        header = "【只读参考资产（仅参考结构/规则，禁止复制句面）】\n" if index == 0 else ""
        blocks.append(
            ContextBlock(
                block_id=f"reference_{index:03d}", kind="reference_asset",
                authority=authority, priority=priority, pinned=False,
                content=header + part,
            )
        )

    compiled = compile_prompt(blocks, input_budget)
    ordered = sorted(compiled.blocks, key=lambda block: (block.priority, block.block_id))
    user = (
        f"章节标题：{context.title}\n\n"
        + "\n\n".join(block.content for block in ordered)
        + "\n\n请输出这些段落的本地化结果，不要包含章节标题。"
    )
    if retry_feedback:
        # 纠正指令放在最后：与首轮 prompt 不同（避免字节级重发），且紧邻输出位置。
        user = f"{user}\n\n{retry_feedback}"

    if input_budget_override is not None and input_budget_override != budget.input_budget:
        budget = replace(budget, input_budget=input_budget)
    manifest = build_manifest(
        compiled=compiled,
        prompt_run_id=new_prompt_run_id(),
        chapter_id=context.chapter_id,
        budget=budget,
        paragraph_start=paragraph_start,
        paragraph_end=paragraph_end,
        system_text=system,
        user_text=user,
        created_at=now_iso(),
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], manifest


def _build_llm_prompt(
    context: RewriteContext,
    rule_output: str,
    *,
    paragraph_start: int = 1,
    paragraph_end: int | None = None,
) -> list[dict[str, str]]:
    """兼容封装：正式 prompt 全部经 ``build_prompt_run`` 的 Compiler 管线构造。"""
    messages, _manifest = build_prompt_run(
        context,
        rule_output,
        paragraph_start=paragraph_start,
        paragraph_end=paragraph_end,
    )
    return messages




def _lock_mappings(context: RewriteContext) -> list[Mapping]:
    """返回必须在 LLM 输出上重新执行的硬锁定映射（词典 must_replace + 已接受译名决策）。"""
    return [
        mapping
        for mapping in _collect_mappings(context.rules_tiers)
        if mapping.tier == "must_replace"
    ] + _decision_mappings(context)


_TRANSLATION_WORD_FIXES: dict[str, str] = {
    "倘若": "如果",
    "端看": "看",
    "我等": "我们",
    "尔等": "你们",
}


_TITLE_NOISE_RE = re.compile(r"[\s\u3000—–\-—…~～・·:：,，.。!！?？「」『』“”\"'（）()【】\[\]]+")
_TITLE_VOLUME_RE = re.compile(r"^第[一二三四五六七八九十百零\d]+[卷部篇]\s*")
#: 标题形态判据：短句 + 含章/卷/序标记 + 不含句末标点。用于防止把正文误判成标题行。
_TITLE_MARKER_RE = re.compile(r"第[一二三四五六七八九十百零\d]+[卷部篇章回]|[序楔]章|^序|终章|尾声")
_TITLE_SIMILARITY_MIN = 0.62


def _title_signature(text: str) -> str:
    """标题指纹：去装饰标点、去卷前缀，只留可比较的实词序列。"""
    normalized = _TITLE_VOLUME_RE.sub("", (text or "").strip())
    return _TITLE_NOISE_RE.sub("", normalized)


def _looks_like_title(text: str) -> bool:
    """标题形态：短、含章卷标记、且不是完整句子。

    只看形态不看内容——避免为了去重而误删正文段。真实正文段通常更长且含
    句末标点（。！？），标题则短且带「第N卷/序章」这类标记。
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 40:
        return False
    if _TITLE_MARKER_RE.search(stripped):
        return True
    # 无卷章标记时，要求短且无句末标点（「前往圣地林德布尔姆吧！」这种标题）
    return len(stripped) <= 24 and not re.search(r"[。！？!?]$", stripped)


def _same_title(candidate: str, signature: str) -> bool:
    """候选段是否与标题同源：签名相等，或高相似（LLM 会把「前往」改写成「去」）。"""
    candidate_signature = _title_signature(candidate)
    if not candidate_signature:
        return False
    if candidate_signature == signature:
        return True
    if not _looks_like_title(candidate):
        return False
    ratio = SequenceMatcher(None, candidate_signature, signature).ratio()
    return ratio >= _TITLE_SIMILARITY_MIN


def _dedupe_opening_title(text: str, title: str) -> str:
    """最终输出前防御：标题行下方若再出现一次同一标题（含改写变体），只保留一次。

    为什么不能只比「相等」，也不能只比传入的 title：源稿标题是
    「第二卷 序章 前往圣地林德布尔姆吧！」，经词典替换成本地化标题
    「第二卷 序章 前往圣辉城吧！」，而 LLM 会自己再写一条改写版标题
    「第二卷 序章——去圣辉城吧！」顶在下一段（实测两范例版成品开头两行标题）。
    传入的 ``title`` 仍是**未本地化的源稿标题**，拿它去比第二段必然不中，
    因此判据取「**输出自己的前两段**是否同源标题」。
    """
    del title  # 参数保留以兼容调用点；判据不依赖它（见 docstring）
    paragraphs = [part.strip() for part in _split_paragraphs(text)]
    if len(paragraphs) < 2:
        return text
    first_signature = _title_signature(paragraphs[0])
    if not first_signature:
        return text
    # 第一段必须是标题形态，否则「前两段相似」只是正常正文，不能删
    if not _looks_like_title(paragraphs[0]):
        return text
    if _same_title(paragraphs[1], first_signature):
        return paragraphs[0] + "\n\n" + "\n\n".join(paragraphs[2:])
    return text


def _content_chars(text: str) -> dict[str, int]:
    """统计文本中的汉字多重集（用于判断返修是否保留原内容）。"""
    counts: dict[str, int] = {}
    for char in text or "":
        if "\u4e00" <= char <= "\u9fff":
            counts[char] = counts.get(char, 0) + 1
    return counts


def _repair_preserves_content(
    original: str, repaired: str, *, min_ratio: float = 0.5
) -> bool:
    """返修是否保留了原段落内容（汉字多重集包含率）。

    返修**只允许改表达，不允许丢内容或加新剧情**。真实日语源实测：章末钩子
    返修把「三天后，两人已经成了可以称为挚友的关系」整段换成「车门关上的那一瞬，
    凛瞥见站台尽头有个穿黑斗篷的人正盯着她」——既丢掉源内容又注入编造剧情，
    并直接导致该章 event_comparison 失败（门禁正确拦截）。

    用字符级包含率而非词元集合：中文整句常是一个连续汉字串，token 集合会退化成
    单一元素而失去区分度。原段落无汉字时不做判断（放行），避免误伤纯对白/符号段。
    """
    source_chars = _content_chars(original)
    if not source_chars:
        return True
    target_chars = _content_chars(repaired)
    kept = sum(min(count, target_chars.get(char, 0)) for char, count in source_chars.items())
    return kept / sum(source_chars.values()) >= min_ratio


def _repair_chapter_hook(
    context: RewriteContext,
    config: LLMConfig,
    llm_output: str,
    title: str,
) -> str:
    """章末钩子定向返修：只重写最后一段，把平淡收尾改成强钩子。

    依据索引库跨书共识：最后一句必须是新问题/新威胁/未完动作/金句，
    不能平静总结。不改事件图，只强化原结尾落点。
    """
    hook = localization_tone.hook_signal_check(llm_output)
    if hook.result != "warning":
        return llm_output

    body = _body_paragraphs(llm_output, title)
    if not body:
        return llm_output

    system = (
        prompt_assets.build_system_prompt(prompt_assets.build_local_rule_block())
        + "\n你是章末返修编辑。只重写最后一段，把平淡收尾改成强钩子；"
        "禁止增加新剧情、禁止改变事件结果，只强化悬念/威胁/金句落点。"
    )
    user = (
        f"这是本章最后一段：\n{body[-1]}\n\n"
        "请只输出重写后的最后一段（一段，不要编号，不要说明）。"
    )
    audit_path = (
        context.workspace / "audit.jsonl" if context.workspace is not None else None
    )
    try:
        raw = call_llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            config=config,
            audit_path=audit_path,
            chapter_id=context.chapter_id,
        )
    except LLMError as exc:
        _audit_fallback(
            context.workspace,
            context.chapter_id,
            f"llm_hook_repair_error: {exc}",
        )
        return llm_output

    cleaned = _strip_code_fences(raw).strip()
    repaired = _body_paragraphs(cleaned, title)
    if len(repaired) != 1 or not repaired[0].strip():
        _audit_fallback(
            context.workspace,
            context.chapter_id,
            f"llm_hook_repair_count_mismatch: expected=1, actual={len(repaired)}",
        )
        return llm_output

    if not _repair_preserves_content(body[-1], repaired[0]):
        # 返修丢了原内容或注入了新剧情：保留原结尾（宁可钩子平淡，不可丢内容）
        _audit_fallback(
            context.workspace,
            context.chapter_id,
            "llm_hook_repair_content_lost: 返修未保留原段落内容，已保留原结尾",
        )
        return llm_output

    body[-1] = repaired[0]
    _audit_fallback(
        context.workspace,
        context.chapter_id,
        "llm_hook_repair: 章末已强化",
    )
    repaired_text = "\n\n".join(body)
    return f"{title}\n\n{repaired_text}" if title else repaired_text


def _normalize_translation_words(text: str) -> str:
    """机械清除高频翻译腔词/顽固句式（只处理可安全替换的情况）。"""
    for source, target in _TRANSLATION_WORD_FIXES.items():
        text = text.replace(source, target)
    # 顽固句式：所谓的X无关Y而是在于Z → 用户确认的口语化表达
    text = re.sub(
        r"所谓的[^。！？]{0,18}无关[^。！？]{0,18}而是在于[^。！？]{0,18}",
        "强不强跟力量没关系，关键看你活成什么样",
        text,
    )
    # 句尾“喽”在“要来喽/走喽/上喽”等口语里可安全换成“了”
    text = re.sub(r"喽(?=[。！!?])", "了", text)
    # 顽固台版腔：句尾“的说”删除；句尾“喔/耶/呐”改“啊”
    text = re.sub(r"的说(?=[，。！？!?…~」』\"'\n]|$)", "", text)
    text = re.sub(r"[喔耶呐](?=[。！？!?…~」』])", "啊", text)
    return text


def _relock_rule_replacements(
    text: str, context: RewriteContext, *, where: str
) -> str:
    """LLM 可能把机械替换结果改回源词（核弹/散修/软体等）；对硬锁定映射重扫一遍。

    残留针与检查侧共用 ``checks._residue_needles``：词典声明的是
    `圣地林德布尔姆 → 圣辉城`，但 LLM 可能只留下**裸地名**「林德布尔姆」。
    只比完整条目会放行这类残留 → 成品带未本地化地名交付。词干直接映射到
    target：词干本身就是被本地化的那个专名，前缀「圣地」是属性词，中文里
    允许只留属性词（「我站在圣辉城的钟塔上」）。
    """
    relocked = text
    for mapping in _lock_mappings(context):
        for needle in checks._residue_needles(mapping.source):
            if not needle or needle not in relocked:
                continue
            if needle == mapping.source:
                relocked = _replace_mapping(relocked, mapping)
            else:
                relocked = _replace_all(relocked, needle, mapping.target)
    if relocked != text:
        _audit_fallback(
            context.workspace,
            context.chapter_id,
            f"dictionary_relocked@{where}",
        )
    return relocked


def index_surface_similarity_audit(
    context: RewriteContext,
    output_text: str,
    used_assets: list[dict[str, Any]],
) -> None:
    """warning 级句面相似度审计：检测输出是否照抄了索引库锚点/索引片段。

    只写审计，不设硬门槛；避免短句撞车造成大量误报。
    """
    if context.index_lib is None:
        return
    references: list[tuple[str, str]] = []
    for item in used_assets or []:
        asset_path = str(item.get("asset_path", ""))
        kind = item.get("asset_kind", "")
        try:
            if kind == "style_card":
                card = context.index_lib.get_style_card(asset_path)
                for axis in card.get("axes", []):
                    for anchor in axis.get("anchors", []):
                        if isinstance(anchor, str) and len(anchor.strip()) >= 8:
                            references.append((asset_path, anchor.strip()))
            elif kind == "index_chapter":
                sections = context.index_lib.get_chapter_index(asset_path)
                for section in sections:
                    for items in section.subsections.values():
                        for it in items:
                            text_item = " ".join(it) if isinstance(it, tuple) else str(it)
                            if len(text_item.strip()) >= 12:
                                references.append((asset_path, text_item.strip()))
        except Exception:  # noqa: BLE001
            continue

    if not references:
        return
    findings = 0
    for paragraph in _split_paragraphs(output_text):
        for asset_path, ref in references:
            match = SequenceMatcher(None, paragraph, ref).find_longest_match(
                0, len(paragraph), 0, len(ref)
            )
            if match.size >= 10:
                matched = paragraph[match.a:match.a + match.size]
                append_audit(
                    context.workspace / "audit.jsonl" if context.workspace is not None else None,
                    "index_surface_similarity",
                    chapter_id=context.chapter_id,
                    asset_path=asset_path,
                    matched_fragment=matched[:80],
                    severity="warning",
                )
                findings += 1
                break
        if findings >= 10:
            break


def _repair_tone_violations(
    context: RewriteContext,
    config: LLMConfig,
    llm_output: str,
    title: str,
) -> str:
    """LLM 输出后做一次定向返修：只重写被台版腔/翻译腔硬门槛命中的段落。

    若 LLM 首轮保留了少量「的说/喔/喽/呐/一匹」，这里用短 prompt 逐段返修，
    保持其余段落不动。返修失败或段数不匹配时保持原输出，让 check 正常拦下。
    """
    body = _body_paragraphs(llm_output, title)
    offenders: list[int] = []
    for index, paragraph in enumerate(body):
        paragraph_report = localization_tone.scan_tone(paragraph)
        if paragraph_report.has_hard or paragraph_report.has_blocked:
            offenders.append(index)
            if len(offenders) >= 40:
                break
    # 追加“原句照抄”的段，一并定向返修（body 已去掉标题，索引要基于 body 计算）
    body_text = "\n\n".join(body)
    unchanged_indexes = checks.unchanged_paragraph_indexes(
        context.source_text,
        body_text,
        rules_tiers=context.rules_tiers,
        project_decisions=context.project_decisions,
        chapter_decisions=context.chapter_decisions,
    )
    for index in unchanged_indexes:
        if index not in offenders:
            offenders.append(index)
    offenders.sort()
    if not offenders:
        return llm_output

    numbered = "\n\n".join(
        f"【P{index + 1}】{body[index]}" for index in offenders
    )
    system = (
        prompt_assets.build_system_prompt(prompt_assets.build_local_rule_block())
        + "\n你是质检返修编辑。下面每一段都被「台版腔/翻译腔」硬门槛标记，"
        "必须逐段重写：去掉喔/耶～/喽/呐/的说/一匹(动物量词)、端看/我等/而已等台日腔，"
        "改成大陆网文口吻；如果某段与原文逐字相同，必须换一种中文说法；"
        "保持该段事件、人物与信息不变。"
    )
    user = (
        "以下段落需要返修（共 "
        f"{len(offenders)} 段，段落数不可变）：\n{numbered}\n\n"
        "每个段落之间用空行分隔，不要输出编号，不要输出任何说明。"
    )
    audit_path = (
        context.workspace / "audit.jsonl" if context.workspace is not None else None
    )
    try:
        raw = call_llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            config=config,
            audit_path=audit_path,
            chapter_id=context.chapter_id,
        )
    except LLMError as exc:
        _audit_fallback(
            context.workspace,
            context.chapter_id,
            f"llm_tone_repair_error: {exc}",
        )
        return llm_output

    cleaned = _strip_code_fences(raw)
    repaired = _body_paragraphs(cleaned, title)
    if not repaired or len(repaired) != len(offenders):
        _audit_fallback(
            context.workspace,
            context.chapter_id,
            f"llm_tone_repair_count_mismatch: expected={len(offenders)}, actual={len(repaired)}",
        )
        return llm_output

    applied = 0
    for index, paragraph in zip(offenders, repaired):
        # 与章末钩子返修同一守卫：返修不得丢内容或注入新剧情
        if not _repair_preserves_content(body[index], paragraph):
            continue
        body[index] = paragraph
        applied += 1
    if applied < len(offenders):
        _audit_fallback(
            context.workspace,
            context.chapter_id,
            f"llm_tone_repair_content_lost: {len(offenders) - applied} 段未保留原内容，已保留原文",
        )
    _audit_fallback(
        context.workspace,
        context.chapter_id,
        f"llm_tone_repair: {applied} 段返修",
    )
    repaired_text = "\n\n".join(body)
    return f"{title}\n\n{repaired_text}" if title else repaired_text


def _audit_fallback(workspace: Path | None, chapter_id: str, reason: str) -> None:
    if workspace is None:
        return
    append_audit(
        workspace / "audit.jsonl",
        "llm_fallback",
        chapter_id=chapter_id,
        reason=reason,
    )


_LLM_CHUNK_THRESHOLD = 150
_LLM_CHUNK_SIZE = 100
#: 增写通道用的分段规模。100 段是「长度必须不变」时代选的——那时压字数是免费的，
#: 不存在「命中字数区间」这个任务。2026-09-18 探针（真实 prompt 构建器，同章分区间）：
#:   40 段 → 输出/源 = 0.97 / 0.95（命中区间），句长 21.5±11.2、19.3±10.5（优于源稿）
#:   100 段 → 0.48（completion 撞 12288 顶被截断）/ 0.83（偏薄）
#: 缩小分段后模型才管得住长度；顺带失败半径从 1/3 章降到 1/8 章。
_LLM_CHUNK_SIZE_EXPANDING = 40
_LLM_CHUNK_THRESHOLD_EXPANDING = 40
#: 二分重试的下限与深度上限。低于该跨度再拆就没有意义（上下文太窄），
#: 深度上限防止「一直失败一直拆」把调用量放大成灾难。
_LLM_CHUNK_BISECT_MIN = 20
_LLM_CHUNK_BISECT_MAX_DEPTH = 2
_LLM_PARAGRAPH_TOLERANCE_RATIO = 0.08
_LLM_CHUNK_TOLERANCE_RATIO = 0.10
_LLM_CHUNK_TOLERANCE_MIN = 4
#: 增写通道的分段段数容差下限。为什么要单独收紧：2026-09-18 实测末尾分段
#: 281-301（21 段）丢了整整一段（源稿第 301 段「我消除自身的气息，以「透过
#: 高速移动消失在黑暗中」这样的演出离开。」在成品里完全消失），而
#: ``max(4, expected×10%)`` 让**丢 4 段都算合格**。这条路径在**加字**，
#: 自然并段几乎不会发生，丢段就是丢内容——覆盖门抓到了它（s-0301-001 absent），
#: 但改写侧本该自己拦住。
_LLM_CHUNK_TOLERANCE_MIN_EXPANDING = 1
# 小段数分段（expected ≤ 5，典型为整章单段）允许的绝对拆段上限：
# 长段转多段是正常译文形态，但超出该上限即判为异常膨胀。
_LLM_CHUNK_SMALL_EXPANSION = 8
# 同一分支允许的绝对并段上限：两个短段并成一段是合法排版取舍。
# 方向必须对称——只准拆不准并会让整章回退基线文本（见 _paragraph_count_ok）。
_LLM_CHUNK_SMALL_CONTRACTION = 1

#: 分段级增写区间。为什么必须给「本段绝对字数」而不是「整章百分比」：
#: 模型一次只看得到本段的 100 段，算不出整章比例，于是按感觉往长里写。
#: 2026-09-18 实测（增写开关首跑）：三次分段调用**全部** `finish_reason=length`
#: 被截断 → 三个分段全回退规则稿 → 成品 185/301 段与源稿一字不差，
#: 而 rewrite 报「改写完成」、退出码 0。比例算不出的地方，机器替它算成绝对数。
_CHUNK_EXPAND_MIN_RATIO = 0.95
_CHUNK_EXPAND_MAX_RATIO = 1.25


def _chunk_source_chars(
    rule_output: str, title: str, paragraph_start: int, paragraph_end: int
) -> int:
    """本分段的源（规则稿）字数——用于校验输出是否写薄。"""
    body = _body_paragraphs(rule_output, title)
    return sum(len(p) for p in body[paragraph_start - 1 : paragraph_end])


def _chunk_length_guidance(paragraphs: list[str]) -> str:
    """把本段允许的字数区间算成绝对字数（模型算不出的比例要替它算）。"""
    total = sum(len(p) for p in paragraphs)
    low = int(total * _CHUNK_EXPAND_MIN_RATIO)
    high = int(total * _CHUNK_EXPAND_MAX_RATIO)
    return (
        f"本段源字数 {total} 字。输出总字数必须落在 {low}~{high} 字之间："
        f"低于 {low} 字会被判为**写薄**、整段作废重写；超过 {high} 字会被输出长度"
        "上限截断，同样作废。目标是把内容写成区间内的具体文字，不是只做词语替换。"
    )

# 日文假名（平假名 + 片假名）：标题是否需要本地化的可靠判据。
# 汉字与中文共享，不能单独作为判据（「対峙」无需翻译即已是中文形态）。
_KANA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF]")

_TITLE_SYSTEM_PROMPT = (
    "你是小说标题本地化助手。把用户给出的日文章节标题翻译成自然的中文标题。"
    "只输出翻译后的标题本身，不要解释、不要加引号、不要保留任何假名。"
)


def _contains_japanese(text: str) -> bool:
    """文本是否含日文假名（据此判定标题是否需要本地化）。"""
    return bool(text) and bool(_KANA_RE.search(text))


def _replace_leading_title(text: str, source_title: str, target_title: str) -> str:
    """把正文首块（标题行）替换为本地化标题；未匹配则原样返回。"""
    if not text or not target_title or not source_title:
        return text
    blocks = text.split("\n\n")
    if blocks and blocks[0].strip() == source_title.strip():
        blocks[0] = target_title
        return "\n\n".join(blocks)
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip() == source_title.strip():
            lines[index] = target_title
            return "\n".join(lines)
    return text


def _localize_title(
    context: RewriteContext, config: LLMConfig | None, title: str
) -> str:
    """把章节标题本地化：先走规则映射，仍含假名时再请求一次极短 LLM 翻译。

    为什么必须做：成品标题必须与正文同语言。真实日语源实测中，未译标题
    「第3章 学院の門」被残留硬门槛判为 `門` 残留 → 整章永久 rework；
    「第5章 学长の忠告」还导致标题事件无法在正文中定位 → event_comparison fail。

    降级策略：规则层结果永远可用；LLM 不可用、报错或返回仍含假名时，
    退回规则层结果（绝不因标题翻译失败而中断改写）。
    """
    if not title:
        return title
    mapped = title
    try:
        tiers = getattr(context, "rules_tiers", None) or {}
        for mapping in _collect_mappings(tiers):
            if mapping.source and mapping.target and mapping.source in mapped:
                mapped = _replace_all(mapped, mapping.source, mapping.target)
    except Exception:  # noqa: BLE001 - 规则层异常退回原标题
        mapped = title
    if not _contains_japanese(mapped):
        return mapped
    if config is None or not config.enabled or config.dry_run:
        return mapped
    try:
        raw = call_llm(
            [
                {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
                {"role": "user", "content": title},
            ],
            config=config,
            audit_path=(context.workspace / "audit.jsonl") if context.workspace else None,
            chapter_id=context.chapter_id,
        )
    except Exception:  # noqa: BLE001 - 标题翻译失败不阻塞改写
        return mapped
    cleaned = _strip_code_fences(raw)
    first_line = cleaned.splitlines()[0].strip() if cleaned else ""
    first_line = first_line.strip("“”\"'「」")
    if not first_line or _contains_japanese(first_line):
        return mapped
    return first_line


def _paragraph_count_ok(
    actual: int,
    expected: int,
    *,
    ratio: float = _LLM_PARAGRAPH_TOLERANCE_RATIO,
    minimum: int = 2,
    small_expansion: int = _LLM_CHUNK_SMALL_EXPANSION,
    small_contraction: int = _LLM_CHUNK_SMALL_CONTRACTION,
) -> bool:
    """LLM 输出段数是否可接受。

    ``expected <= 5``（典型为「整章只有一个长段」）时容差必须**双向对称**：
    把长段拆成多段、或把两个短段并成一段，都是翻译中的合法排版取舍。旧实现
    只允许拆段（``expected <= actual <= expected + 8``）而拒绝并段
    （``actual < expected`` 一律判失败），方向不对称。

    真实日语源实测（10 章盲测批次）：ch-0007/0008/0009 的模型输出为
    ``expected=5, actual=4``（两个短段被合并），两次尝试均判失败 → 整章回退
    规则稿，交付正文里 50%~83% 的段落是**未经本地化的基线文本**。方向不对称
    的代价（整章质量塌陷）远大于它想防的风险（并段）。

    内容正确性不由段数兜底：实测 LLM 本地化正文对规则稿的汉字保留率仅
    0.29~0.51（日文→中文长度约翻倍），字符多重集无法区分「合法改写」与
    「吞段丢内容」。吞段由事件门兜住——段落内容整体丢失会使该事件无法定位，
    ``event_comparison_check`` 不可能判 pass。
    """
    if actual == expected:
        return True
    if expected <= 5:
        # 下限：允许多段并成少一段，但**不得塌成单段**（expected ≥ 2 时下限为 2）。
        # 段落 ID（p-000N）是事件定位与连续性的事实基准，塌成一段会让除首段外的
        # 映射全部失效；而下限为 1 时 2 段章可以塌成 1 段，等于放弃结构。
        lower = 1 if expected <= 1 else max(2, expected - small_contraction)
        return lower <= actual <= expected + small_expansion
    tolerance = max(minimum, int(expected * ratio))
    return abs(actual - expected) <= tolerance


def _retry_feedback(reason: str, expected: int, start: int, end: int) -> str:
    """把首轮失败原因转成可执行的纠正指令（重试必须比首轮多带信息）。

    真实批次实测：不带纠正信息的重试与首轮 **request_hash 完全相同**，模型
    大概率重复同一错误，白付一次调用且仍然回退基线文本。

    截断（``finish_reason=length``）必须走**独立分支**：它的成因是「写太长」，
    而通用纠正说的是「输出 N 段」，等于把同一个过长要求原样再发一遍。
    """
    text = reason or ""
    if "finish_reason=length" in text or "未完整结束" in text:
        return (
            "【重试纠正】上一轮输出被**输出长度上限截断**，整段作废并已回退规则稿。"
            "本次只准重写句式、不准扩写：删掉多余的修饰与重复意象，"
            f"严格输出 {expected} 段（全局第 {start}~{end} 段），"
            "总字数压缩到源字的 1.25 倍以内。宁可写短，也不要写不完。"
        )
    if "llm_chunk_too_thin" in text or "写薄" in text:
        return (
            "【重试纠正】上一轮输出被判为**写薄了**（字数低于下限），整段作废。"
            "本次必须写厚：按【允许增写】白名单补感官描写、身体反应、主角吐槽，"
            "把抽象说法换成具体物件与动作，"
            f"严格输出 {expected} 段（全局第 {start}~{end} 段），"
            "字数写回源字的 95%~125% 区间以内——低于下限算失败，超过上限会被截断。"
        )
    match = re.search(r"actual=(\d+)", text)
    if match:
        return (
            f"【重试纠正】上一轮输出的段落数校验未通过：实际 {int(match.group(1))} 段，"
            f"要求 {expected} 段（全局第 {start}~{end} 段）。"
            "请严格保持段落边界，逐段一一对应输出，既不要合并段落，也不要拆分段落。"
        )
    return (
        f"【重试纠正】上一轮输出被判定为不可用（{reason or '未知原因'}）。"
        f"请严格输出 {expected} 段本地化正文（全局第 {start}~{end} 段），"
        "段落之间用空行分隔，不要输出任何计划、说明或元数据。"
    )


def _call_chunk(
    context: RewriteContext,
    config: LLMConfig,
    rule_output: str,
    title: str,
    *,
    paragraph_start: int,
    paragraph_end: int,
    expected: int,
    retry_feedback: str | None = None,
    failure_sink: list[str] | None = None,
    floor_soft: bool = False,
) -> list[str] | None:
    """调用 LLM 处理一个分段；返回段落列表，失败返回 None。

    PromptManifest 在调用 LLM 前原子落盘（``chapters/<id>/prompts/``），
    LLM 失败时同样保留，保证任意一次正式生成可回放审计。

    ``retry_feedback`` 为重试纠正指令；``failure_sink`` 追加失败原因（机器可读），
    供调用方构造纠正指令，避免「原因丢失 → 重发同一 prompt」。

    ``floor_soft``：字数下限违规时**放行并记录原因**而不是判失败。用于重试那一轮——
    模型在长度上是双峰的（写短 0.70 / 写长 2 倍撞顶），硬判失败会让整章从
    「薄但可读」直接变成「什么都没有」（2026-09-18 三跑实测：三个分段全被拒）。
    薄是能人工判断的事，零产出不是。
    """
    def _fail(reason: str) -> None:
        _audit_fallback(context.workspace, context.chapter_id, reason)
        if failure_sink is not None:
            failure_sink.append(reason)

    messages, manifest = build_prompt_run(
        context,
        rule_output,
        paragraph_start=paragraph_start,
        paragraph_end=paragraph_end,
        config=config,
        retry_feedback=retry_feedback,
    )
    if context.workspace is not None:
        try:
            save_prompt_manifest(context.workspace, context.chapter_id, manifest)
        except Exception:  # noqa: BLE001 - 落盘失败不阻塞改写，但记录审计
            _audit_fallback(context.workspace, context.chapter_id, "prompt_manifest_save_failed")
        # 范例原文另存缓存：检查侧的防抄检测需要正文，而 manifest 只存哈希。
        try:
            from .context_compiler import save_exemplar_cache

            exemplars = exemplar_guard.exemplars_from_parts(
                (m.get("content", "") for m in messages if isinstance(m, dict))
            )
            if exemplars:
                save_exemplar_cache(
                    context.workspace, context.chapter_id, manifest.prompt_run_id, exemplars
                )
        except Exception:  # noqa: BLE001 - 缓存失败不阻塞改写，但记录审计
            _audit_fallback(context.workspace, context.chapter_id, "exemplar_cache_save_failed")
    audit_path = (
        context.workspace / "audit.jsonl" if context.workspace is not None else None
    )
    try:
        raw = call_llm(
            messages,
            config=config,
            audit_path=audit_path,
            chapter_id=context.chapter_id,
        )
    except LLMError as exc:
        _fail(f"llm_error@chunk_{paragraph_start}-{paragraph_end}: {exc}")
        return None

    cleaned = _strip_code_fences(raw)
    if not cleaned:
        _fail("llm_empty_output")
        return None
    if _looks_like_refusal(cleaned, expected):
        _fail("llm_refusal_or_filtered")
        return None
    body = _body_paragraphs(cleaned, title)
    expanding = prompt_assets.expansion_enabled()
    if not _paragraph_count_ok(
        len(body),
        expected,
        ratio=_LLM_CHUNK_TOLERANCE_RATIO,
        minimum=(
            _LLM_CHUNK_TOLERANCE_MIN_EXPANDING
            if expanding
            else _LLM_CHUNK_TOLERANCE_MIN
        ),
    ):
        _fail(f"llm_paragraph_count_mismatch: expected={expected}, actual={len(body)}")
        return None
    if expanding:
        # 字数下限必须是真闸门：只写在 prompt 里等于没有——二跑实测提示词要求
        # 95%~125%，模型照压到 80.4%，节奏门仍 6 项不达标。
        source_chars = _chunk_source_chars(rule_output, title, paragraph_start, paragraph_end)
        actual_chars = sum(len(p) for p in body)
        if source_chars > 0:
            ratio = actual_chars / source_chars
            if ratio < _CHUNK_EXPAND_MIN_RATIO:
                reason = (
                    f"llm_chunk_too_thin: 输出 {actual_chars} 字 / 源 {source_chars} 字 "
                    f"= {ratio:.2f}，低于下限 {_CHUNK_EXPAND_MIN_RATIO}"
                )
                if not floor_soft:
                    _fail(reason)
                    return None
                _audit_fallback(context.workspace, context.chapter_id, reason)
                if failure_sink is not None:
                    failure_sink.append(reason)
    return body


def llm_enhanced_rewrite(
    context: RewriteContext,
    *,
    config: LLMConfig | None = None,
    summary_sink: list[str] | None = None,
) -> str:
    """规则 + LLM 双层改写（质量优先）。

    - 短章（<=150 段）整章一次调用；长章按 100 段分块调用，块失败整章回退；
    - 段落数按 ±2%（至少 ±2 段）容差校验，防止 LLM 自然合并少段时全部回退；
    - 空输出 / 拒答 / 纯度问题 / 代码围栏仍整章回退并审计。

    ``summary_sink``：本次改写若发生**分段回退规则稿**，把结论追加进去交给
    调用方显示。回退段落未经本地化，交付前必须让人看得见——2026-09-18 实测
    三个分段全回退时 `rewrite` 只报「改写完成 → awaiting_checks」，成品看上去
    正常却 61.5% 与源稿一字不差。
    """
    config = config or load_config()
    rule_output = rule_based_rewrite(context)
    title = (context.title or "").strip()
    # 标题必须与正文同语言：否则残留门把日文标题判为残留、事件对照也会失配。
    # 规则稿首块同步替换，保证下游「按标题剥离/装配」一致。
    localized_title = _localize_title(context, config, title)
    if localized_title and localized_title != title:
        rule_output = _replace_leading_title(rule_output, title, localized_title)
        title = localized_title
    body_paragraphs = _body_paragraphs(rule_output, title)
    expected = len(body_paragraphs)

    if config.dry_run or not config.enabled:
        _audit_fallback(
            context.workspace,
            context.chapter_id,
            "dry_run_or_disabled" if config.dry_run else "llm_disabled",
        )
        return rule_output

    output_paragraphs: list[str] = []
    expanding = prompt_assets.expansion_enabled()
    chunk_size = _LLM_CHUNK_SIZE_EXPANDING if expanding else _LLM_CHUNK_SIZE
    chunk_threshold = (
        _LLM_CHUNK_THRESHOLD_EXPANDING if expanding else _LLM_CHUNK_THRESHOLD
    )
    if expected <= chunk_threshold:
        chunks = [(1, expected)]
    else:
        chunks = [
            (start, min(start + chunk_size - 1, expected))
            for start in range(1, expected + 1, chunk_size)
        ]

    accepted_chunks = 0
    total_chunks = len(chunks)
    fallback_ranges: list[tuple[int, int]] = []
    thin_ranges: list[tuple[int, int]] = []
    # 待处理区间队列：失败的分段先二分重试，仍失败才回退该子区间。
    # 为什么值得：2026-09-18 判官版 check 实测，那个 1/8 回退区（161-200）是
    # **所有门禁红字的来源**——覆盖门 4 条里 3 条 absent 落在它边界，事件门的
    # 1 blocked/1 fail 也在附近。40 段整块回退一次丢 40 段；二分把失败半径
    # 减半再减半，而小跨度本来就更容易命中字数区间（探针：40 段 0.97、100 段 0.48）。
    pending: list[tuple[int, int, int]] = [(start, end, 0) for start, end in chunks]
    cursor = 0
    while cursor < len(pending):
        paragraph_start, paragraph_end, depth = pending[cursor]
        cursor += 1
        chunk_expected = paragraph_end - paragraph_start + 1
        failure_reasons: list[str] = []
        chunk_body = _call_chunk(
            context,
            config,
            rule_output,
            title,
            paragraph_start=paragraph_start,
            paragraph_end=paragraph_end,
            expected=chunk_expected,
            failure_sink=failure_reasons,
        )
        if chunk_body is None:
            # 分段失败常为瞬时（段数校验不通过 / 拒答 / 截断）：先重试一次再回退。
            # 跨语言下「回退规则稿」等于回退**未翻译的源文**（整句日文），该章必然
            # 被残留门拦住而无法通过，所以重试的收益很高、代价只在失败路径上。
            _audit_fallback(
                context.workspace,
                context.chapter_id,
                f"llm_chunk_retry@{paragraph_start}-{paragraph_end}: 首次失败，重试一次",
            )
            retry_reasons: list[str] = []
            chunk_body = _call_chunk(
                context,
                config,
                rule_output,
                title,
                paragraph_start=paragraph_start,
                paragraph_end=paragraph_end,
                expected=chunk_expected,
                # 带上首轮失败原因：否则重试是字节级重发同一 prompt，等同掷硬币。
                retry_feedback=_retry_feedback(
                    failure_reasons[-1] if failure_reasons else "",
                    chunk_expected,
                    paragraph_start,
                    paragraph_end,
                ),
                failure_sink=retry_reasons,
                # 重试这轮用软下限：写薄了放行并报告，不让整章全灭。
                floor_soft=True,
            )
            if chunk_body is not None and any(
                "too_thin" in reason for reason in retry_reasons
            ):
                thin_ranges.append((paragraph_start, paragraph_end))
        if chunk_body is None:
            if chunk_expected > _LLM_CHUNK_BISECT_MIN and depth < _LLM_CHUNK_BISECT_MAX_DEPTH:
                mid = (paragraph_start + paragraph_end) // 2
                _audit_fallback(
                    context.workspace,
                    context.chapter_id,
                    f"llm_chunk_bisect@{paragraph_start}-{paragraph_end}: "
                    f"整块失败，二分重试（{paragraph_start}-{mid} / {mid + 1}-{paragraph_end}）",
                )
                # 必须插在当前游标处**就地**处理：子区间的输出是按处理顺序
                # extend 进 output_paragraphs 的，若 append 到队尾，被二分的那段
                # 内容会整体挪到章尾（2026-09-18 实测事故：章首的「契机是阿尔法
                # 捎来的一封信」跑到了第 124 段，而长度/节奏指标全部正常，
                # 只有顺序能抓到——见 test_bisected_chunk_keeps_output_order）。
                pending[cursor:cursor] = [
                    (paragraph_start, mid, depth + 1),
                    (mid + 1, paragraph_end, depth + 1),
                ]
                continue
            # 二分到底仍失败才回退该区间到规则稿，保留其他分段 LLM 成果。
            # 回退段落**未经本地化**，必须让操作者看得见（见 llm_quality_findings）：
            # 盲测/交付时无法分辨的降级正文会污染验收证据。
            fallback_paragraphs = body_paragraphs[paragraph_start - 1 : paragraph_end]
            _audit_fallback(
                context.workspace,
                context.chapter_id,
                f"llm_chunk_failed@{paragraph_start}-{paragraph_end}: 本分段回退规则稿（{len(fallback_paragraphs)} 段）",
            )
            fallback_ranges.append((paragraph_start, paragraph_end))
            output_paragraphs.extend(fallback_paragraphs)
        else:
            accepted_chunks += 1
            output_paragraphs.extend(chunk_body)
    total_chunks = len(pending)

    if accepted_chunks == 0 and total_chunks > 0:
        # 工单 §三.5 的守卫不能只护日文：中文源稿全回退时成品读起来「正常」
        # （就是源稿本体），反而更容易被当成一次成功改写交付——2026-09-18
        # 实测中文源稿全回退，成品 185/301 段与源稿一字不差，rewrite 报成功。
        # 一个分段都没成活 = 这不是本地化成果，一律响亮失败，不写任何版本。
        raise LLMError(
            f"全部 {total_chunks} 个分段的 LLM 调用均失败：拒绝提交未本地化的规则稿"
            "（成品将回退规则稿、而非本地化成果；章节进入 error，"
            "可运行 novel recover 后重试，或调小分段规模后重跑）"
        )

    if summary_sink is not None and fallback_ranges:
        ranges = "、".join(f"{start}-{end}" for start, end in fallback_ranges)
        summary_sink.append(
            f"{len(fallback_ranges)}/{total_chunks} 个分段回退了规则稿"
            f"（第 {ranges} 段）——这些段落没有经过本地化，"
            "交付前必须人工确认或重跑，不要直接当成改写成果"
        )
    if summary_sink is not None and thin_ranges:
        ranges = "、".join(f"{start}-{end}" for start, end in thin_ranges)
        summary_sink.append(
            f"{len(thin_ranges)}/{total_chunks} 个分段写薄了（第 {ranges} 段）——"
            "重试后仍未达到字数下限，正文可读但比源稿薄，节奏会偏平；"
            "建议对这几段做定向加厚，或调大分段规模后重跑"
        )

    if len(output_paragraphs) != expected and not _paragraph_count_ok(
        len(output_paragraphs), expected, ratio=0.08, minimum=5
    ):
        _audit_fallback(
            context.workspace,
            context.chapter_id,
            f"llm_total_paragraph_mismatch: expected={expected}, actual={len(output_paragraphs)}",
        )
        return rule_output

    llm_output = f"{title}\n\n" + "\n\n".join(output_paragraphs) if title else "\n\n".join(output_paragraphs)
    llm_output = _repair_tone_violations(context, config, llm_output, title)
    llm_output = _repair_chapter_hook(context, config, llm_output, title)
    llm_output = _normalize_translation_words(llm_output)
    llm_output = _dedupe_opening_title(llm_output, title)
    llm_output = _relock_rule_replacements(llm_output, context, where="llm_total")
    purity = checks.purity_issues(llm_output)
    if purity:
        _audit_fallback(
            context.workspace,
            context.chapter_id,
            f"llm_purity_fail: {'; '.join(purity[:3])}",
        )
        return rule_output
    return llm_output


def rule_then_llm_backend(
    config: LLMConfig | None = None,
    *,
    summary_sink: list[str] | None = None,
) -> RewriteBackend:
    """返回默认双层改写后端（规则优先 + LLM 兜底）。

    ``summary_sink`` 透传给 ``llm_enhanced_rewrite``：分段回退规则稿的结论
    必须能被调用方**显示出来**——只写进审计等于没人看得见，而回退段落读起来
    正常，最容易被当成一次成功改写交付。
    """

    def backend(context: RewriteContext) -> str:
        return llm_enhanced_rewrite(
            context, config=config, summary_sink=summary_sink
        )

    return backend


def default_backend(*, summary_sink: list[str] | None = None) -> RewriteBackend:
    """与 ``rule_then_llm_backend()`` 相同，保留别名便于 CLI 调用。"""
    return rule_then_llm_backend(summary_sink=summary_sink)
