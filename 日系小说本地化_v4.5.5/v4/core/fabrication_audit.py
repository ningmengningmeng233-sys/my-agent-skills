# -*- coding: utf-8 -*-
"""本地化忠实度审计：检出「译文中出现、源稿里没有」的情节级新增（v4.5.0-dev.3）。

为什么需要独立审计：缺陷 17 的内容守卫只保护**返修环节**（章末钩子/语气返修）不再
注入编造内容；主翻译段落仍可能自行丢句加戏。而 `event_comparison` 只判定「源事件
是否被覆盖」，**看不到事件之外的新增**——实测有章节通过了全部门禁，却在石碑译文里
凭空写出可读字句「别信任何人」（源稿只说刻着无法辨认的文字）。

为什么不走确定性路线：实测比对「源稿事件数 vs 成品事件数」的比值在 0.71~1.00 之间，
**对是否编造没有区分力**（编造发生在段落内部，不体现为事件数增加）。因此采用
LLM 反向审计：以源稿为基准逐段核对成品多出的内容。

定位是 **warning（不阻断）**：本地化本身允许修辞与描写强化，硬拦截会变成误报机器；
价值在于把「情节级新增」显式摆到人面前，并作为清单 §11「无中生有」核查的提示清单。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .llm import LLMConfig, LLMError, call_llm, load_config

_SYSTEM_PROMPT = (
    "你是本地化忠实度审计员。输入是日语源稿与对应中文译文。"
    "只找出**情节级新增**：译文中出现了源稿里没有的 **事件、对白、可读字句、人物、物件、设定信息**。"
    "严格排除：修辞与描写强化（比喻、程度副词、感官细节、情绪外化）、语序调整、"
    "量词与语气补充、把原文隐含信息显化。宁缺勿滥——拿不准就不要列入。"
    '只输出 JSON：{"fabricated":[{"item":"...","why":"..."}]}，没有则空数组，不要输出其它文字。'
)

_MAX_ITEMS = 5
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_items(raw: Any) -> list[dict[str, str]]:
    """从模型输出解析情节级新增清单；任何畸形输出都降级为空（fail-open）。"""
    if not isinstance(raw, str) or not raw.strip():
        return []
    parsed: Any = None
    match = _JSON_OBJECT_RE.search(raw)
    for candidate in ([match.group(0)] if match else []) + [raw.strip()]:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            break
        parsed = None
    if not isinstance(parsed, dict):
        return []
    rows = parsed.get("fabricated")
    if not isinstance(rows, list):
        return []
    items: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = str(row.get("item", "") or "").strip()
        if not item:
            continue
        items.append({"item": item, "why": str(row.get("why", "") or "").strip()})
        if len(items) >= _MAX_ITEMS:
            break
    return items


def make_fabrication_auditor(
    config: LLMConfig | None = None,
    *,
    chapter_id: str = "",
    audit_path: Any = None,
) -> Callable[[str, str], list[dict[str, str]]]:
    """构造忠实度审计器；返回 ``(source_text, output_text) -> [{item, why}]``。"""

    def audit(source_text: str, output_text: str) -> list[dict[str, str]]:
        resolved = config or load_config()
        if not source_text.strip() or not output_text.strip():
            return []
        if not resolved.enabled or resolved.dry_run or not resolved.api_key:
            return []
        try:
            raw = call_llm(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"【源稿】\n{source_text}\n\n【译文】\n{output_text}",
                    },
                ],
                config=resolved,
                audit_path=audit_path,
                chapter_id=chapter_id,
            )
        except LLMError:
            return []
        except Exception:  # noqa: BLE001 - 审计失败绝不阻塞检查
            return []
        return _extract_items(raw)

    return audit


def fabrication_finding(items: list[dict[str, str]]) -> dict[str, str]:
    """把审计结果转成与长篇质量门同构的 finding（warning 级，不阻断）。"""
    summary = "；".join(
        f"{row['item']}" + (f"（{row['why']}）" if row.get("why") else "")
        for row in items[:3]
    )
    more = f"；另有 {len(items) - 3} 项" if len(items) > 3 else ""
    return {
        "gate": "fabrication",
        "code": "FABRICATED_CONTENT",
        "severity": "warning",
        "message": (
            f"译文含 {len(items)} 处源稿没有的情节级内容，疑似模型加戏；"
            "请人工确认是否需改写（本项为提示，不阻断合并）"
        ),
        "evidence": f"{summary}{more}"[:400],
    }
