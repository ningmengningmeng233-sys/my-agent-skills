"""项目/章节 A/B/C 决策层（v4 spec §4 / §8.2 / §11.1 / §12.3）。

契约：``docs/v4-artifact-schemas.json`` 的 ``projectDecisions`` / ``chapterDecisions``。

- 结构校验：schema_version、revision、entries、来源与决策 ID 唯一；
  绝不放行 `state`/`passed` 等直接状态字段（C-035 / F-017）；
- A/B/C 分层：A 必须用户确认；B 默认自动处理但可追溯；C 冲突升级并阻塞；
- 决策哈希与开放项：open 的决策项使 ``required_open=True``、``open_count>0``，
  未决前阻塞对应章节进入 ready_for_rewrite / passed；
- 事件图高风险节点闭环：approved/rejected 事件必须引用到状态匹配的决策项，
  引用不存在或状态不符即为未闭环。

用户只能编辑 ``project-decisions.json`` 与 ``chapters/<id>/decisions.json``；
manifest 状态一律由系统维护，本模块绝不做状态伪造。
"""

from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit import append_audit, now_iso
from .events import find_high_risk_events
from .hashing import sha256_file
from .jsonio import read_json, write_json_atomic
from .paths import ARTIFACT_SCHEMA
from .schemas import artifact_schema, SchemaError

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# 与 artifact 契约一致的枚举
_CATEGORIES = {"mapping", "contradiction", "plot_change", "review", "boundary", "style"}
_STATUSES = {"open", "accepted", "rejected", "superseded"}
_SOURCE_KINDS = {"user", "system", "reviewer"}

# A/B/C 分层关键词（启发式；明确档位见 reason 中的「A档/B档/C档」）
_A_CATEGORIES = {"plot_change", "contradiction", "boundary", "style"}
_A_TARGET_KEYWORDS = (
    "书名", "主角", "女主", "男主", "核心", "世界观", "组织", "门派", "势力",
    "国家", "王国", "城池", "主角名", "译名", "人名", "设定", "地名",
)
_B_TARGET_KEYWORDS = (
    "日常", "口头禅", "语气词", "语助", "翻译腔", "标点", "格式", "排版",
    "低风险", "母语化",
)
_C_TARGET_KEYWORDS = (
    "低置信", "疑似", "多个译名", "冲突", "文化功能", "平台风险", "红线",
    "不确定", "争议", "矛盾",
)


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def classify_tier(entry: dict[str, Any], *, core_targets: set[str] | None = None) -> str:
    """返回决策项的 A/B/C 档位。

    A：必须用户确认；B：默认自动处理但可追溯；C：冲突升级并阻塞。
    显式档位（reason 中的「A档/B档/C档」）优先；否则按 category 与关键词启发式判定。
    """
    reason = f"{entry.get('target', '')} {entry.get('reason', '')}"
    explicit = re.search(r"[ABC]档", reason)
    if explicit:
        return explicit.group(0)[0]
    if any(keyword in reason for keyword in _C_TARGET_KEYWORDS):
        return "C"
    category = entry.get("category", "")
    if category in _A_CATEGORIES:
        return "A"
    if category == "mapping":
        if core_targets and entry.get("target") in core_targets:
            return "A"
        if any(keyword in reason for keyword in _A_TARGET_KEYWORDS):
            return "A"
        if any(keyword in reason for keyword in _B_TARGET_KEYWORDS):
            return "B"
        return "B"
    if category == "review":
        return "A"
    return "B"


def validate_decision_document(doc: Any, *, project: bool = True) -> list[str]:
    """结构校验，返回错误列表（空 = 通过）。

    覆盖：schema_version、revision、entries 类型、决策 ID 唯一、category/status/source
    枚举、target/choice 非空、created_at 可解析，以及「不得含 state/passed 状态字段」。
    """
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["决策文档必须是对象"]
    expected = "v4-project-decisions-1" if project else "v4-chapter-decisions-1"
    if doc.get("schema_version") != expected:
        errors.append(f"schema_version 必须是 {expected}")
    revision = doc.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        errors.append("revision 必须是不小于 1 的整数")
    for forbidden in ("state", "passed", "status"):
        if forbidden in doc:
            errors.append(
                f"决策文档不得包含 {forbidden} 直接状态字段（状态由 manifest 维护，C-035/F-017）"
            )
    entries = doc.get("entries")
    if not isinstance(entries, list):
        errors.append("entries 必须是数组")
        return errors
    ids: set[str] = set()
    for index, entry in enumerate(entries):
        prefix = f"entries[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} 不是对象")
            continue
        if any(entry.get(key) is not None for key in ("state", "passed")):
            errors.append(f"{prefix} 不得包含 state/passed 直接状态字段")
        decision_id = entry.get("decision_id")
        if not isinstance(decision_id, str) or not _ID_RE.match(decision_id):
            errors.append(f"{prefix}.decision_id 非法: {decision_id!r}")
        elif decision_id in ids:
            errors.append(f"{prefix}.decision_id 重复: {decision_id}")
        else:
            ids.add(decision_id)
        if entry.get("category") not in _CATEGORIES:
            errors.append(f"{prefix}.category 非法: {entry.get('category')!r}")
        if entry.get("status") not in _STATUSES:
            errors.append(f"{prefix}.status 非法: {entry.get('status')!r}")
        if not isinstance(entry.get("target"), str) or not entry["target"].strip():
            errors.append(f"{prefix}.target 不能为空")
        if not isinstance(entry.get("choice"), str) or not entry["choice"].strip():
            errors.append(f"{prefix}.choice 不能为空")
        source = entry.get("source")
        if (
            not isinstance(source, dict)
            or source.get("kind") not in _SOURCE_KINDS
            or not isinstance(source.get("value"), str)
            or not source["value"]
        ):
            errors.append(
                f"{prefix}.source 必须是 kind∈(user/system/reviewer) 且 value 非空"
            )
        if not _valid_timestamp(entry.get("created_at")):
            errors.append(f"{prefix}.created_at 非法: {entry.get('created_at')!r}")
    return errors


def schema_errors(doc: Any, name: str) -> list[str]:
    """用 ``docs/v4-artifact-schemas.json`` 的 projectDecisions/chapterDecisions 契约校验。"""
    from jsonschema import Draft202012Validator
    try:
        schema = deepcopy(artifact_schema(name))
        schema["$defs"] = read_json(ARTIFACT_SCHEMA)["$defs"]
        Draft202012Validator(schema).validate(doc)
        return []
    except Exception as exc:  # noqa: BLE001 - 契约校验失败即收集错误
        return [f"artifact schema 校验失败: {exc}"]


def validate_document(doc: Any, *, project: bool = True) -> list[str]:
    errors = validate_decision_document(doc, project=project)
    if not errors:
        errors = schema_errors(
            doc, "projectDecisions" if project else "chapterDecisions"
        )
    return errors


def load_and_validate(path: Path, *, project: bool = True) -> dict[str, Any]:
    """读取决策文件并做结构+契约双重校验；失败抛 :class:`SchemaError`。"""
    doc = read_json(path)
    errors = validate_document(doc, project=project)
    if errors:
        raise SchemaError(f"决策文件校验失败: {'; '.join(errors[:8])}")
    return doc


def open_entries(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in doc.get("entries", []) if entry.get("status") == "open"]


def required_open_entries(
    doc: dict[str, Any], *, core_targets: set[str] | None = None
) -> list[dict[str, Any]]:
    """开放且属于 A/C 档（必须用户确认）的决策项；未决前阻塞章节。"""
    return [
        entry
        for entry in open_entries(doc)
        if classify_tier(entry, core_targets=core_targets) in {"A", "C"}
    ]


def decision_summary(doc: dict[str, Any]) -> dict[str, Any]:
    """manifest 章节 `decisions` 摘要：required_open / open_count / revision。"""
    open_items = open_entries(doc)
    return {
        "required_open": bool(required_open_entries(doc)),
        "open_count": len(open_items),
        "revision": doc.get("revision", 0),
    }


def project_mapping_conflicts(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """同一原词出现多个已接受的项目级译名 → C 档冲突，不自动覆盖（C-033）。"""
    by_target: dict[str, set[str]] = defaultdict(set)
    for entry in doc.get("entries", []):
        if entry.get("category") == "mapping" and entry.get("status") == "accepted":
            by_target[entry["target"]].add(entry["choice"])
    return [
        {"target": target, "choices": sorted(choices)}
        for target, choices in sorted(by_target.items())
        if len(choices) > 1
    ]


def check_event_decision_closure(
    graph: dict[str, Any], chapter_decisions: dict[str, Any]
) -> list[str]:
    """事件图高风险节点决策闭环：approved/rejected 必须引用到状态匹配的决策项。"""
    issues: list[str] = []
    entries = chapter_decisions.get("entries", []) if isinstance(chapter_decisions, dict) else []
    by_id = {
        entry.get("decision_id"): entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("decision_id")
    }
    for event in find_high_risk_events(graph):
        risk = event.get("risk", {})
        status = risk.get("decision_status")
        decision_id = risk.get("decision_id")
        event_id = event.get("event_id", "")
        if status == "open":
            issues.append(f"事件 {event_id} 高风险未确认（decision_status=open）")
        elif status in {"approved", "rejected", "superseded"}:
            if not decision_id:
                issues.append(f"事件 {event_id} 标记 {status} 但缺少 decision_id")
                continue
            entry = by_id.get(decision_id)
            expected = "accepted" if status == "approved" else "rejected" if status == "rejected" else "superseded"
            if entry is None:
                issues.append(f"事件 {event_id} 引用不存在的决策 {decision_id}")
            elif entry.get("status") != expected:
                issues.append(
                    f"事件 {event_id} 决策 {decision_id} 状态为 {entry.get('status')}，期望 {expected}"
                )
    return issues


def closure_status(
    project_decisions: dict[str, Any],
    chapter_decisions: dict[str, Any],
    events_graph: dict[str, Any],
) -> tuple[str, list[str]]:
    """决策闭环状态：``pass`` / ``blocked`` / ``fail``。

    - 任一决策文件结构或契约非法 → fail；
    - 存在任何 open 决策项（未决）→ blocked；
    - 高风险事件决策未闭环 → blocked。
    """
    issues: list[str] = []
    for label, doc, is_project in (
        ("project-decisions", project_decisions, True),
        ("chapter-decisions", chapter_decisions, False),
    ):
        errors = validate_document(doc, project=is_project)
        issues.extend(f"{label}: {error}" for error in errors[:5])
    if issues:
        return "fail", issues
    conflicts = project_mapping_conflicts(project_decisions)
    if conflicts:
        detail = [
            f"项目级译名冲突 {item['target']}: {'/'.join(item['choices'])}"
            for item in conflicts[:5]
        ]
        return "blocked", detail
    open_items = required_open_entries(project_decisions) + required_open_entries(
        chapter_decisions
    )
    if open_items:
        detail = [
            f"未决 {classify_tier(entry)} 档决策 {entry['decision_id']}: {entry['target']}"
            for entry in open_items[:5]
        ]
        return "blocked", detail
    link_issues = check_event_decision_closure(events_graph, chapter_decisions)
    if link_issues:
        return "blocked", link_issues[:5]
    return "pass", []


def derive_blockers(
    project_decisions: dict[str, Any],
    chapter_decisions: dict[str, Any],
    events_graph: dict[str, Any],
) -> list[dict[str, Any]]:
    """从决策闭环失败原因生成可操作阻塞项列表。"""
    blockers: list[dict[str, Any]] = []
    for label, doc, is_project in (
        ("project-decisions", project_decisions, True),
        ("chapter-decisions", chapter_decisions, False),
    ):
        for error in validate_document(doc, project=is_project)[:5]:
            blockers.append(
                {
                    "code": "DECISION_SCHEMA_FAIL",
                    "kind": "decision",
                    "message": f"{label}: {error}",
                }
            )
    if blockers:
        return blockers

    open_items = required_open_entries(project_decisions) + required_open_entries(
        chapter_decisions
    )
    for entry in open_items:
        tier = classify_tier(entry)
        message = f"未决 {tier} 档决策 {entry['decision_id']}: {entry['target']}"
        if entry.get("reason"):
            message += f"（{entry['reason']}）"
        blockers.append(
            {
                "code": f"OPEN_{tier}_TIER_DECISION",
                "kind": "decision",
                "message": message,
            }
        )

    for item in project_mapping_conflicts(project_decisions)[:5]:
        blockers.append(
            {
                "code": "PROJECT_MAPPING_CONFLICT",
                "kind": "decision",
                "message": (
                    f"项目级译名冲突 {item['target']}: "
                    f"{'/'.join(item['choices'])}（C 档，请修改 project-decisions）"
                ),
            }
        )

    for issue in check_event_decision_closure(events_graph, chapter_decisions):
        code = (
            "HIGH_RISK_EVENT_OPEN"
            if "未确认" in issue
            else "EVENT_DECISION_MISMATCH"
        )
        blockers.append({"code": code, "kind": "decision", "message": issue})

    return blockers


def new_entry(
    *,
    category: str,
    target: str,
    choice: str,
    source_kind: str,
    source_value: str,
    reason: str = "",
    decision_id: str | None = None,
    status: str = "open",
    created_at: str | None = None,
) -> dict[str, Any]:
    """构造一个通过结构校验的决策项；非法枚举直接抛错。"""
    if category not in _CATEGORIES:
        raise ValueError(f"非法 category: {category!r}")
    if status not in _STATUSES:
        raise ValueError(f"非法 status: {status!r}")
    if source_kind not in _SOURCE_KINDS:
        raise ValueError(f"非法 source.kind: {source_kind!r}")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target 不能为空")
    if not isinstance(choice, str) or not choice.strip():
        raise ValueError("choice 不能为空")
    if decision_id is not None and not _ID_RE.match(decision_id):
        raise ValueError(f"非法 decision_id: {decision_id!r}")
    entry: dict[str, Any] = {
        "decision_id": decision_id or "",
        "category": category,
        "target": target,
        "choice": choice,
        "status": status,
        "source": {"kind": source_kind, "value": source_value},
        "created_at": created_at or now_iso(),
    }
    if reason:
        entry["reason"] = reason
    return entry


def add_entry(doc: dict[str, Any], entry: dict[str, Any], *, decision_id: str | None = None) -> dict[str, Any]:
    """向决策文档追加一条已构造的决策项；自动分配唯一 ID 并递增 revision。"""
    entries = doc.setdefault("entries", [])
    if not isinstance(entries, list):
        raise ValueError("entries 必须是数组")
    chosen = decision_id or entry.get("decision_id") or f"dec-{len(entries) + 1:04d}"
    existing = {item.get("decision_id") for item in entries if item.get("decision_id")}
    if chosen in existing:
        raise ValueError(f"decision_id 已存在: {chosen}")
    entry = dict(entry)
    entry["decision_id"] = chosen
    is_project = doc.get("schema_version") == "v4-project-decisions-1"
    # 先临时校验新项本身；章节决策文档需要 chapter_id 字段
    trial_doc: dict[str, Any] = {"schema_version": doc["schema_version"], "revision": 1, "entries": [entry]}
    if not is_project:
        trial_doc["chapter_id"] = doc.get("chapter_id", "")
    trial = validate_document(trial_doc, project=is_project)
    if trial:
        raise SchemaError("新决策项校验失败: " + "; ".join(trial[:5]))
    entries.append(entry)
    doc["revision"] = int(doc.get("revision", 1)) + 1
    return doc


def set_entry_status(
    doc: dict[str, Any],
    decision_id: str,
    status: str,
    *,
    source_kind: str = "user",
    source_value: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """把指定决策项置为 accepted/rejected/superseded 并递增 revision。"""
    if status not in _STATUSES:
        raise ValueError(f"非法 status: {status!r}")
    for entry in doc.get("entries", []):
        if entry.get("decision_id") == decision_id:
            entry["status"] = status
            if source_kind and source_value:
                entry["source"] = {"kind": source_kind, "value": source_value}
            if reason:
                entry["reason"] = reason
            doc["revision"] = int(doc.get("revision", 1)) + 1
            return doc
    raise KeyError(f"决策项不存在: {decision_id}")


def hash_decision(path: Path) -> str:
    return sha256_file(path)


def record_decision(
    path: Path,
    doc: dict[str, Any],
    *,
    event_type: str,
    workspace: Path,
    project_id: str | None = None,
    chapter_id: str | None = None,
    **fields: Any,
) -> str:
    """原子写决策文件 + 追加审计，返回写入后的 SHA-256。"""
    errors = validate_document(doc, project=doc.get("schema_version") == "v4-project-decisions-1")
    if errors:
        raise SchemaError("决策文件校验失败: " + "; ".join(errors[:5]))
    write_json_atomic(path, doc)
    sha = sha256_file(path)
    append_audit(
        workspace / "audit.jsonl",
        event_type,
        project_id=project_id,
        chapter_id=chapter_id,
        decision_path=path.name,
        decision_sha256=sha,
        revision=doc["revision"],
        **fields,
    )
    return sha
