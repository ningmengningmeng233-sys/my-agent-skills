# -*- coding: utf-8 -*-
"""定向返修（Task 9.9.5）：只修问题段落窗口，不整章盲重写。

工单约束：

- 只在覆盖判定为 ``absent`` / ``partial`` / ``uncertain`` 时触发；
- 只发送**问题段落窗口**与必要相邻上下文，附带 requirement ID、源句、缺失语义、
  当前译文与必须保留的正确内容；
- 返修 Prompt 只允许返回指定段落窗口的替换正文；
- **每个问题段落最多 2 次**定向返修，超限保持 rework 并停止调用；
- 禁止以规则稿/未验证草稿作为降级交付。

本模块的纯函数部分（计划、提示、替换、账本）不访问 API 与状态机，便于离线测试；
调用 LLM 与落版本由 CLI 负责。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: 每个问题段落允许的定向返修次数上限（工单 §9.9.5）
MAX_REPAIRS_PER_PARAGRAPH = 2
#: 参与返修的覆盖状态
REPAIR_STATUSES = frozenset({"absent", "partial", "uncertain"})

#: 覆盖项 ID 形如 s-0008-001（段号-句号）；缺失句没有段落证据时用源段位置兜底
_REQUIREMENT_ID_RE = re.compile(r"^s-(\d{4})-(\d{3})$")


def _positional_fallback(requirement_id: str, order: list[str]) -> str | None:
    """按源句所在段号挑目标段落；段号超出译文段数时取末段。

    为什么需要：**完全缺失**的句子没有任何段落可指（判官只能给出空段落列表），
    但它恰恰是最需要补回的。源段位置是最接近的证据——段末句通常对应译文末段。
    无法从 ID 解析段号时返回 None（宁可不返修，也不乱改）。
    """
    match = _REQUIREMENT_ID_RE.match(requirement_id or "")
    if match is None or not order:
        return None
    ordinal = int(match.group(1))
    return order[ordinal - 1] if ordinal <= len(order) else order[-1]

REPAIR_SYSTEM_PROMPT = (
    "你是小说本地化流水线的定向返修编辑。输入是某一段的现有译文，以及**必须补回的源句语义**。\n"
    "规则：\n"
    "1. 只输出该段落窗口的替换正文，不要输出解释、编号、检查表或任何元数据；\n"
    "2. 必须补回列出的缺失语义，同时保留现有译文中正确的人名、术语、事件顺序与文风；\n"
    "3. 不要删改相邻已通过内容（你只收到目标段落，返回也只包含目标段落）；\n"
    "4. 段落数量不变：输出一段正文，不要拆成多段或用空行分隔；\n"
    "5. 不要引入源句之外的新情节。"
)


@dataclass(frozen=True)
class RepairTask:
    """一个待返修的段落窗口及其必须补回的语义。"""

    paragraph_id: str
    requirement_ids: tuple[str, ...]
    missing_semantics: tuple[str, ...]
    current_text: str
    attempt: int  # 第几次返修（1 起）
    fallback_used: str = ""  # "position" 表示目标段落由源段位置兜底得到

    def to_dict(self) -> dict[str, Any]:
        return {
            "paragraph_id": self.paragraph_id,
            "requirement_ids": list(self.requirement_ids),
            "missing_semantics": list(self.missing_semantics),
            "attempt": self.attempt,
            "fallback_used": self.fallback_used,
        }


def normalize_ledger(ledger: Mapping[str, Any] | None) -> dict[str, int]:
    """把返修账本规范化为 {段落 ID: 已返修次数}（容忍脏数据）。"""
    out: dict[str, int] = {}
    for key, value in (ledger or {}).items():
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            out[str(key)] = count
    return out


def plan_repairs(
    blocking: Sequence[Mapping[str, Any]],
    paragraph_texts: Mapping[str, str],
    *,
    ledger: Mapping[str, Any] | None = None,
    max_per_paragraph: int = MAX_REPAIRS_PER_PARAGRAPH,
    source_version: str = "",
) -> tuple[list[RepairTask], list[dict[str, Any]]]:
    """把覆盖阻断项按段落聚合成返修任务。

    返回 ``(任务列表, 超限跳过项)``：

    - 只处理 ``REPAIR_STATUSES`` 中的状态；
    - 阻断项没有段落 ID 时**不能**猜测位置（宁可不返修，也不改错段落），记入跳过项；
    - 某段已返修次数达到 ``max_per_paragraph`` → 不再调用，记入跳过项。
    """
    counts = _scoped_counts(ledger, source_version)
    grouped: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []

    for item in blocking or ():
        status = str(item.get("status", "") or "")
        if status not in REPAIR_STATUSES:
            continue
        requirement_id = str(item.get("requirement_id", "") or "")
        pids = [str(p) for p in (item.get("paragraph_ids") or []) if p]
        fallback_used = ""
        if not pids:
            # 完全缺失的句子没有段落证据：按源段位置兜底（无段号可解析才放弃）
            guessed = _positional_fallback(requirement_id, list(paragraph_texts))
            if guessed is None:
                skipped.append(
                    {
                        "requirement_id": requirement_id,
                        "status": status,
                        "reason": "无段落定位证据且无法从 requirement ID 解析源段号，未返修",
                    }
                )
                continue
            pids = [guessed]
            fallback_used = "position"
        target = pids[0]
        bucket = grouped.setdefault(
            target,
            {
                "requirement_ids": [],
                "missing_semantics": [],
                "fallback_used": "",
            },
        )
        if requirement_id:
            bucket["requirement_ids"].append(requirement_id)
        semantics = str(item.get("source_text") or "").strip()
        if semantics:
            bucket["missing_semantics"].append(semantics)
        if fallback_used and not bucket["fallback_used"]:
            bucket["fallback_used"] = fallback_used

    tasks: list[RepairTask] = []
    for paragraph_id, bucket in grouped.items():
        used = counts.get(paragraph_id, 0)
        if used >= max_per_paragraph:
            skipped.append(
                {
                    "paragraph_id": paragraph_id,
                    "attempts_used": used,
                    "reason": f"已达每段返修上限 {max_per_paragraph} 次，保持 rework",
                    "requirement_ids": bucket["requirement_ids"],
                }
            )
            continue
        current = paragraph_texts.get(paragraph_id, "")
        if not current:
            skipped.append(
                {
                    "paragraph_id": paragraph_id,
                    "attempts_used": used,
                    "reason": "目标段落不在当前版本中（版本已变化），需重新检查",
                }
            )
            continue
        tasks.append(
            RepairTask(
                paragraph_id=paragraph_id,
                requirement_ids=tuple(bucket["requirement_ids"]),
                missing_semantics=tuple(bucket["missing_semantics"]),
                current_text=current,
                attempt=used + 1,
                fallback_used=bucket["fallback_used"],
            )
        )
    return tasks, skipped


def build_repair_messages(task: RepairTask) -> list[dict[str, str]]:
    """构造返修消息（只含目标段落窗口，不含全章）。"""
    missing = "\n".join(f"- [{rid}] 源句：{sem}" for rid, sem in zip(task.requirement_ids, task.missing_semantics))
    if not missing:
        missing = "\n".join(f"- 源句：{sem}" for sem in task.missing_semantics)
    user = (
        f"【目标段落】{task.paragraph_id}\n{task.current_text}\n\n"
        f"【必须补回的缺失语义】\n{missing}\n\n"
        f"这是第 {task.attempt} 次定向返修。请只输出该段落的替换正文。"
    )
    return [
        {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def finalize_repaired_text(text: str, context: Any) -> str:
    """对返修后的正文执行与改写路径**一致**的后处理：用词归一 + 硬锁定重扫。

    为什么必须做：定向返修只替换目标段落，若跳过这一步，模型在返修文本里写回的
    词典违禁词（如 must_replace 的「榻榻米」）会直接进入新版本，被残留门判 fail，
    该章就再也无法通过——真实批次 ch-0005 的 v002/v004 正是如此（两版都通过覆盖门
    与事件门，却因「榻榻米」残留被拦；实测把同一文本过一遍本函数即变成「木地板」）。

    实现在这里而不是 CLI：让「返修产物必须与改写产物走同一后处理」这条不变量可离线测试。
    """
    from .rewrite_backend import _normalize_translation_words, _relock_rule_replacements

    finalized = _normalize_translation_words(text)
    finalized = _relock_rule_replacements(finalized, context, where="coverage_repair")
    return finalized


def apply_repair(
    output_text: str, paragraph_id: str, replacement: str, *, pid_list: Sequence[tuple[str, str]]
) -> str:
    """用返修结果替换目标段落，其余段落逐字保持不变。

    段落以 ``\\n\\n`` 连接（与 checks.paragraph_ids 的规范化一致）。
    ``replacement`` 为空或替换目标不存在时返回原文（调用方不得据此写成新版本）。
    """
    replacement = (replacement or "").strip()
    if not replacement:
        return output_text
    order = [pid for pid, _text in pid_list]
    if paragraph_id not in order:
        return output_text
    paragraphs = [text for _pid, text in pid_list]
    paragraphs[order.index(paragraph_id)] = replacement
    return "\n\n".join(paragraphs).strip("\n") + "\n"


def _scoped_counts(ledger: Mapping[str, Any] | None, source_version: str) -> dict[str, int]:
    """取「当前源版本」作用域内的返修次数。

    账本按源版本记账：整章重写会产出新草稿（新的问题段落实例），此处从 0 重新计数，
    否则一次失败的返修会让该段落永久锁死（与模型行为无关的死胡同）。旧格式（扁平
    ``{段落: 次数}``，无版本信息）按**当前版本**处理，保持保守的封顶语义不放松。
    """
    if not isinstance(ledger, Mapping):
        return {}
    counts = ledger.get("counts")
    if isinstance(counts, Mapping):
        recorded = str(ledger.get("source_version", "") or "")
        if source_version and recorded and recorded != source_version:
            return {}  # 新草稿：重新计数（外层返修轮次仍封顶）
        return normalize_ledger(counts)
    return normalize_ledger(ledger)


def update_ledger(
    ledger: Mapping[str, Any] | None,
    tasks: Iterable[RepairTask],
    *,
    source_version: str = "",
) -> dict[str, Any]:
    """返修完成后累加账本；写入 ``{source_version, counts}`` 结构。"""
    counts = _scoped_counts(ledger, source_version)
    for task in tasks:
        counts[task.paragraph_id] = counts.get(task.paragraph_id, 0) + 1
    version = source_version or str((ledger or {}).get("source_version", "") or "")
    return {"source_version": version, "counts": counts}
