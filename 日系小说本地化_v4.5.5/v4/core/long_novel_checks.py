"""五项长篇连续性质量门（v4.5.0-dev.3 Batch 7）。

全部为纯函数：无 I/O、无全局状态，输入输出完全确定，便于审计回放。

- CharacterGate：死亡人物在本章行动（依据连续性账本死亡事实）→ blocked；
- TimelineGate：同一角色无移动交代下先后出现两地 → blocked；
- TerminologyGate：已锁定译名（accepted 映射决策）的源词残留 → blocked；
- KnowledgeGate：锁定秘密（accepted ``plot_change`` + ``延迟揭示`` 决策）
  提前出现在正文 → blocked；
- PromiseGate：长期未回收的承诺 → warning（提示人工回收，不硬拦截）。

blocked 级 findings 由 ``checks.run_checks`` 并入 failures → blockers 链路；
全部 findings 结构化记录进 ``checks/vNNN.json``。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DEATH_MARKERS: tuple[str, ...] = (
    "被杀死", "身亡", "殒命", "毙命", "断气", "咽气", "陨落",
    "阵亡", "丧命", "气绝", "被杀", "惨死", "战死",
)
PROMISE_MARKERS: tuple[str, ...] = ("承诺", "约定", "发誓", "起誓", "答应")
_KEEP_CHOICES: frozenset[str] = frozenset(
    {"保留原文", "保留原名", "保留原词", "保留功能", "保留", "不替换"}
)
_ARRIVAL_RE = r"(?:抵达|赶到|回到|潜入)"
_LEFT_RE = r"(?:离开|动身|启程)"


@dataclass(frozen=True, slots=True)
class GateFinding:
    """单条质量门发现；severity ∈ {blocked, warning}。"""

    gate: str
    code: str
    message: str
    evidence: str = ""
    severity: str = "blocked"

    def to_dict(self) -> dict[str, str]:
        return {
            "gate": self.gate,
            "code": self.code,
            "message": self.message,
            "evidence": self.evidence,
            "severity": self.severity,
        }


def _snapshot_facts(snapshot: dict) -> list[dict]:
    facts = snapshot.get("facts", []) if isinstance(snapshot, dict) else []
    return [item for item in facts if isinstance(item, dict)]


def _dead_characters(earlier_snapshots: list[dict]) -> dict[str, str]:
    """从早前章节事实提取死亡角色 → 死亡依据文本。"""
    dead: dict[str, str] = {}
    for snapshot in earlier_snapshots:
        for participant in snapshot.get("participants", []) or []:
            name = str(participant)
            if not name:
                continue
            for fact in _snapshot_facts(snapshot):
                blob = f"{fact.get('action', '')}->{fact.get('result', '')}"
                if any(marker in blob for marker in DEATH_MARKERS) and name in blob:
                    if name in blob:
                        dead.setdefault(name, f"{snapshot.get('chapter_id', '')}: {blob}")
    return dead


def character_gate(
    *,
    earlier_snapshots: list[dict],
    output_text: str,
    chapter_id: str,
) -> list[GateFinding]:
    """死亡人物行动门：账本中已死亡的角色不得在本章正文行动。"""
    findings: list[GateFinding] = []
    for name, evidence in _dead_characters(earlier_snapshots).items():
        if name in output_text:
            findings.append(
                GateFinding(
                    gate="character",
                    code="DEAD_CHARACTER_ACTS",
                    message=(
                        f"角色「{name}」在前文已死亡（{evidence}），"
                        f"但章节 {chapter_id} 正文中再次出现；"
                        "需改为回忆/幻象并显式标注，或修订前文事实"
                    ),
                    evidence=evidence,
                    severity="blocked",
                )
            )
    return findings


def timeline_gate(*, output_text: str, names: list[str]) -> list[GateFinding]:
    """时间线门：同一角色无「离开/动身」交代下先后抵达两地 → 同一时间多地出现。"""
    findings: list[GateFinding] = []
    for name in names:
        if not name or name not in output_text:
            continue
        arrivals = re.findall(
            re.escape(name) + r"[^。！？\n]{0,15}?" + _ARRIVAL_RE + r"([^，。！？!\s]{1,12})",
            output_text,
        )
        if len(set(arrivals)) < 2:
            continue
        moved = re.search(
            re.escape(name) + r"[^。！？\n]{0,20}" + _LEFT_RE, output_text
        )
        if moved is not None:
            continue
        findings.append(
            GateFinding(
                gate="timeline",
                code="SAME_TIME_TWO_PLACES",
                message=(
                    f"角色「{name}」先后抵达 {sorted(set(arrivals))} 而无移动交代，"
                    "疑似同一时间多地出现；请补充转场或修正地点"
                ),
                evidence="、".join(sorted(set(arrivals))),
                severity="blocked",
            )
        )
    return findings


def terminology_gate(
    *,
    output_text: str,
    accepted_mappings: list[dict],
) -> list[GateFinding]:
    """术语门：已锁定译名的源词残留于正文。同名映射/保留类选择不报。"""
    findings: list[GateFinding] = []
    for entry in accepted_mappings:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "accepted":
            continue
        target = str(entry.get("target", "") or "")
        choice = str(entry.get("choice", "") or "")
        if not target or not choice:
            continue
        if choice == target or choice in _KEEP_CHOICES:
            continue
        if target in output_text:
            findings.append(
                GateFinding(
                    gate="terminology",
                    code="LOCKED_TERM_RESIDUE",
                    message=(
                        f"锁定术语「{target}→{choice}」的源词残留于正文；"
                        "请替换为锁定译名"
                    ),
                    evidence=target,
                    severity="blocked",
                )
            )
    return findings


def knowledge_gate(*, output_text: str, locked_secrets: list[str]) -> list[GateFinding]:
    """知识门：锁定秘密提前揭示。秘密来源由决策层承载（延迟揭示）。"""
    findings: list[GateFinding] = []
    for secret in locked_secrets:
        secret = str(secret or "")
        if secret and secret in output_text:
            findings.append(
                GateFinding(
                    gate="knowledge",
                    code="SPOILER_REVEALED",
                    message=(
                        f"锁定秘密「{secret}」提前出现在本章正文；"
                        "该信息被决策标记为延迟揭示，请改写回避"
                    ),
                    evidence=secret,
                    severity="blocked",
                )
            )
    return findings


def promise_gate(*, earlier_snapshots: list[dict], stale_after: int = 10) -> list[GateFinding]:
    """承诺门：未回收承诺超期 → warning（提示人工回收，不硬拦截）。"""
    span: dict[str, tuple[int, int]] = {}
    ordered = [s for s in earlier_snapshots if isinstance(s, dict)]
    for index, snapshot in enumerate(ordered):
        for fact in _snapshot_facts(snapshot):
            blob = f"{fact.get('action', '')}->{fact.get('result', '')}"
            if not any(marker in blob for marker in PROMISE_MARKERS):
                continue
            first, last = span.get(blob, (index, index))
            span[blob] = (first, max(last, index))
    findings: list[GateFinding] = []
    total = len(ordered)
    for blob, (first, last) in sorted(span.items(), key=lambda kv: kv[1][0]):
        unresolved = last == first  # 提出后再未提及视为未回收
        age = (total - first) if unresolved else (last - first)
        if age >= stale_after:
            findings.append(
                GateFinding(
                    gate="promise",
                    code="PROMISE_STALE",
                    message=(
                        f"承诺「{blob}」自第 {first + 1} 章起已 "
                        f"{age} 章未回收；请安排兑现或显式取消"
                    ),
                    evidence=blob,
                    severity="warning",
                )
            )
    return findings


def _accepted_mappings(project_decisions: dict, chapter_decisions: dict) -> list[dict]:
    entries: list[dict] = []
    for doc in (project_decisions, chapter_decisions):
        if isinstance(doc, dict):
            entries.extend(
                entry for entry in doc.get("entries", []) if isinstance(entry, dict)
            )
    return [e for e in entries if e.get("category") == "mapping"]


def _locked_secrets(project_decisions: dict) -> list[str]:
    """锁定秘密 = accepted plot_change 决策且 choice == 延迟揭示（柠檬裁决）。"""
    secrets: list[str] = []
    if not isinstance(project_decisions, dict):
        return secrets
    for entry in project_decisions.get("entries", []):
        if not isinstance(entry, dict):
            continue
        if (
            entry.get("category") == "plot_change"
            and entry.get("status") == "accepted"
            and str(entry.get("choice", "")).strip() == "延迟揭示"
        ):
            target = str(entry.get("target", "") or "")
            if target:
                secrets.append(target)
    return secrets


def evaluate_long_novel_gates(
    *,
    output_text: str,
    earlier_snapshots: list[dict],
    project_decisions: dict,
    chapter_decisions: dict,
    chapter_id: str = "",
    events_graph: dict | None = None,
    stale_after: int = 10,
) -> list[GateFinding]:
    """编排入口：依次运行五项门，返回合并 findings（确定性顺序）。"""
    names: list[str] = []
    for snapshot in earlier_snapshots:
        for participant in snapshot.get("participants", []) or []:
            name = str(participant)
            if name and name not in names:
                names.append(name)
    if isinstance(events_graph, dict):
        for event in events_graph.get("events", []) or []:
            if isinstance(event, dict):
                for participant in event.get("participants", []) or []:
                    name = str(participant)
                    if name and name not in names:
                        names.append(name)

    findings: list[GateFinding] = []
    findings.extend(
        character_gate(
            earlier_snapshots=earlier_snapshots,
            output_text=output_text,
            chapter_id=chapter_id,
        )
    )
    findings.extend(timeline_gate(output_text=output_text, names=names))
    findings.extend(
        terminology_gate(
            output_text=output_text,
            accepted_mappings=_accepted_mappings(project_decisions, chapter_decisions),
        )
    )
    findings.extend(
        knowledge_gate(
            output_text=output_text,
            locked_secrets=_locked_secrets(project_decisions),
        )
    )
    findings.extend(promise_gate(earlier_snapshots=earlier_snapshots, stale_after=stale_after))
    return findings
