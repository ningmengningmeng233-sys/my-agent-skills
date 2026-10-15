#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_preferences.py - 项目反馈（用户喜恶）的作用域、证据边界与缓存失效。

合同（v2.0.0 beta P14；执行补充 P14 段）：

  1. **三档作用域分开生效**：
     - ``once``（本次）：只回本轮生效视图，不落 durable、不改偏好版本、不扫缓存；
     - ``project``（项目，**默认**）：写进现有 job 的 ``state.json`` 里 ``v2`` 扩展
       的一个子键（复用 ``novel_job.update_job_extension``，不建第二任务库），
       并在现有事件层留一条审计事件；
     - ``global``（明确全局）：必须同时有调用方显式档位、事件里的用户确认
       ``global_confirmed=True`` 与显式全局根目录三把钥匙，缺一条即拒——
       绝不静默修改用户真实全局偏好。全局件位置另过
       ``novel_job._check_location``（不得落进 Skill 安装目录、``.lemon`` 台账或
       宿主 skills 安装区）。
  2. **词汇喜恶不是 AI 证据**：每条 durable 项都带 ``ai_evidence=False``，本模块
     没有任何把偏好交给判级面的出口（``v2_report.decide`` 也没有收偏好的形参）。
     偏好能影响的只有：本轮解释与披露、向量缓存的身份。
  3. **变更使对应缓存失效**：偏好版本是 ``v2_cache.cache_key`` 七项身份里的第七项
     （P07 留了槽位却一直没有人产出）。本模块是那个生产者；durable 一变，就按新
     版本把不属于它的缓存条目扫掉，不靠「下次自然 MISS」这句话冒充失效。
     规则版本/裁判纪元的失效面仍由各自的通路负责：本模块只管偏好这一维。
  4. **事件面只记摘要**：词有可能正是正文里的原话，所以 ``events.jsonl`` 只记
     ``term_sha256``，不记词本身，也不记绝对路径。

同一 ``(scope, term)`` 只留最新一条（改主意的显式表达覆盖旧条目，不攒重复项）；
项目档覆盖全局档里的同名项（更具体的作用域赢）。撤回一条偏好目前没有入口，
按边界如实登记。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import novel_job

SCOPES: tuple[str, ...] = ("once", "project", "global")
DEFAULT_SCOPE = "project"
FEEDBACK_KINDS: tuple[str, ...] = ("avoid", "prefer", "keep")
FEEDBACK_FIELD = "feedback"
FEEDBACK_KEYS: tuple[str, ...] = ("kind", "term", "global_confirmed")
PREFERENCE_SCHEMA = 1
PREFERENCES_STATE_KEY = "preferences"
GLOBAL_FILE_NAME = "v2-user-preferences.json"
MAX_TERM_CHARS = 64
#: 结构常量：偏好条目永远不构成 AI 证据，任何路径都不把它写成 true。
AI_EVIDENCE = False
EMPTY_VERSION_INPUT: tuple = ()


class PreferenceError(ValueError):
    """反馈事件不成立（来源不对/形状不对/作用域缺钥匙）——零写入。"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_text(label: str, value, *, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是字符串，收到 {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{label} 不能为空或全是空白")
    if len(stripped) > limit:
        raise ValueError(f"{label} 超过 {limit} 字符上限（{len(stripped)}）")
    if any(ord(char) < 32 or ord(char) == 127 for char in stripped):
        raise ValueError(f"{label} 含控制字符或换行，不入库")
    return stripped


def _validate_event(event) -> tuple[str, dict]:
    """校验反馈事件，返回 (request_id, feedback 载荷)。

    外壳三个键沿用 ``v2_session.EVENT_KEYS``（事件形状只有一份权威），但反馈
    不是入口三问，字段集另立——两条合同不能共用一份枚举。
    """
    import v2_session as vs

    if not isinstance(event, dict):
        raise PreferenceError(f"event 必须是映射，收到 {type(event).__name__}")
    extra = sorted(set(event) - set(vs.EVENT_KEYS))
    if extra:
        raise ValueError(f"event 含未知字段: {','.join(extra)}")
    missing = sorted(set(vs.EVENT_KEYS) - set(event))
    if missing:
        raise ValueError(f"event 缺字段: {','.join(missing)}")
    if event["source"] != vs.USER_SOURCE:
        raise PreferenceError(f"事件来源 {event['source']!r} 不是当前用户输入，"
                              "不产生反馈偏好")
    request_id = _clean_text("event.request_id", event["request_id"], limit=80)
    fields = event["fields"]
    if not isinstance(fields, dict):
        raise ValueError(f"event.fields 必须是映射，收到 {type(fields).__name__}")
    if set(fields) != {FEEDBACK_FIELD}:
        raise ValueError("event.fields 必须且只能含 feedback，收到 "
                         f"{sorted(fields)}")
    payload = fields[FEEDBACK_FIELD]
    if not isinstance(payload, dict):
        raise ValueError(f"event.fields.feedback 必须是映射，收到 "
                         f"{type(payload).__name__}")
    unknown = sorted(set(payload) - set(FEEDBACK_KEYS))
    if unknown:
        raise ValueError(f"event.fields.feedback 含未知字段: {','.join(unknown)}")
    kind = payload.get("kind")
    if kind not in FEEDBACK_KINDS:
        raise ValueError(f"feedback.kind 只能是 {' / '.join(FEEDBACK_KINDS)}，"
                         f"收到 {kind!r}")
    term = _clean_text("feedback.term", payload.get("term"), limit=MAX_TERM_CHARS)
    confirmed = payload.get("global_confirmed")
    if confirmed is not None and not isinstance(confirmed, bool):
        raise ValueError("feedback.global_confirmed 必须是布尔（用户是否明确说了"
                         "「以后所有项目都这样」），不接收字符串或数字")
    return request_id, {"kind": kind, "term": term,
                        "global_confirmed": confirmed is True}


def _entry(scope: str, kind: str, term: str, request_id: str) -> dict:
    return {"scope": scope, "kind": kind, "term": term, "request_id": request_id,
            "ai_evidence": AI_EVIDENCE}


def _project_entries(job_dir: Path) -> list[dict]:
    state = novel_job._read_json(Path(job_dir) / novel_job.STATE_NAME) or {}
    payload = (state.get("v2") or {}).get(PREFERENCES_STATE_KEY) or {}
    if not isinstance(payload, dict):
        raise PreferenceError(f"{Path(job_dir).name} 的 v2.preferences 形状不合法，"
                              "拒绝在其上继续写入")
    if payload.get("schema") not in (None, PREFERENCE_SCHEMA):
        raise PreferenceError(f"偏好存储版本 {payload.get('schema')!r} 不认识，"
                              "不猜语义")
    return [dict(item) for item in (payload.get("entries") or [])]


def _global_entries(global_root: Path | str | None) -> list[dict]:
    if global_root is None:
        return []
    path = Path(global_root) / GLOBAL_FILE_NAME
    payload = novel_job._read_json(path)
    if payload is None:
        return []
    if not isinstance(payload, dict) or payload.get("schema") != PREFERENCE_SCHEMA:
        raise PreferenceError(f"全局偏好件形状不认识，拒绝使用: {path.name}")
    return [dict(item) for item in (payload.get("entries") or [])]


def _merged(project: list[dict], global_: list[dict]) -> list[dict]:
    """合并两档 durable 项：同 (scope,term) 留最新，项目档压过全局档的同名词。"""
    by_term: dict[str, dict] = {}
    for item in global_:
        by_term[item["term"]] = item
    for item in project:
        by_term[item["term"]] = item
    return sorted(by_term.values(), key=lambda item: (item["term"], item["scope"]))


def _durable_version(entries: list[dict]) -> str:
    payload = sorted((item["scope"], item["kind"], item["term"]) for item in entries)
    stable = json.dumps({"kind": "v2-preference-identity", "shape": PREFERENCE_SCHEMA,
                         "entries": payload}, sort_keys=True, ensure_ascii=False)
    return _sha(stable)


def _replace(known: list[dict], candidate: dict) -> list[dict]:
    """按 (scope, term) 覆盖：改主意不留旧条目，也不攒重复项。"""
    kept = [item for item in known
            if (item["scope"], item["term"]) != (candidate["scope"],
                                                 candidate["term"])]
    return kept + [candidate]


def load_preferences(project_dir: Path | str, *,
                     global_root: Path | str | None = None) -> dict:
    """读出合并后的 durable 偏好视图与版本（每次从磁盘重算，不缓存进程内状态）。"""
    job_dir = Path(project_dir)
    merged = _merged(_project_entries(job_dir), _global_entries(global_root))
    return {"schema": PREFERENCE_SCHEMA, "entries": merged,
            "version": _durable_version(merged)}


def preference_version(project_dir: Path | str, *,
                       global_root: Path | str | None = None) -> str:
    """当前 durable 偏好的 64 位摘要版本（无条目也有确定值，保证非空可入键）。"""
    return load_preferences(project_dir, global_root=global_root)["version"]


def cache_identity(project_dir: Path | str, *, text_hash: str, context_hash: str,
                   rule_version: str, prompt_version: str, model_revision: str,
                   judge_epoch: int,
                   global_root: Path | str | None = None) -> dict:
    """产出向量缓存的七项身份（P07 起第七槽位缺的就是这个生产者）。

    只补 ``preference_version`` 这一维，其余字段原样转交，语义仍由各自通路负责；
    偏好版本从磁盘重算，因此换进程、重启宿主都能得到同一个键。
    """
    return {"text_hash": text_hash, "context_hash": context_hash,
            "rule_version": rule_version, "prompt_version": prompt_version,
            "model_revision": model_revision, "judge_epoch": judge_epoch,
            "preference_version": preference_version(project_dir,
                                                      global_root=global_root)}


def apply_feedback(project_dir: Path | str, event: dict, *,
                   scope: str = DEFAULT_SCOPE,
                   global_root: Path | str | None = None) -> dict:
    """记录一条用户反馈，按作用域决定是否 durable，并让对应缓存身份失效。

    返回 ``{"scope", "persisted", "changed", "preference_version", "entries",
    "effective", "cache", "request_id"}``；``cache`` 只在真扫过时给计数，
    本次档是 ``None``（没扫就是没扫，不用零冒充已扫）。
    """
    import v2_cache

    if scope not in SCOPES:
        raise ValueError(f"scope 只能是 {' / '.join(SCOPES)}，收到 {scope!r}")
    request_id, payload = _validate_event(event)
    job_dir = Path(project_dir)
    novel_job.load_manifest(job_dir)              # 认目录：unknown_job 由它判
    import v2_task
    v2_task.require_no_pending_update(job_dir, "记录项目反馈")

    known_project = _project_entries(job_dir)
    known_global = _global_entries(global_root)
    if scope == "global":
        if global_root is None:
            raise ValueError("全局作用域必须显式给 global_root：不猜用户的全局件位置，"
                             "也不写进任何默认目录")
        novel_job._check_location(Path(global_root))
        if not payload["global_confirmed"]:
            raise ValueError("用户没有明确说这是全局偏好（缺少 global_confirmed=True）："
                             "拒绝写全局，请改用 scope='project' 或先追问一句")
    entry = _entry(scope, payload["kind"], payload["term"], request_id)

    if scope == "once":
        effective = _merged(known_project, known_global)
        novel_job._append_event(job_dir, "feedback_applied", detail={
            "scope": "once", "kind": entry["kind"], "term_sha256": _sha(entry["term"]),
            "request_id": request_id,
            "preference_version": _durable_version(effective), "cache": None})
        view = [dict(item, persisted=True) for item in effective]
        return {"scope": "once", "persisted": False, "changed": False,
                "preference_version": _durable_version(effective),
                "entries": len(effective),
                "effective": view + [dict(entry, persisted=False)],
                "cache": None, "request_id": request_id}

    if scope == "project":
        before = _durable_version(_merged(known_project, known_global))
        project_next = _replace(known_project, entry)
        merged_next = _merged(project_next, known_global)
        state_v2 = dict(v2_task._v2_state_payload(job_dir))
        state_v2[PREFERENCES_STATE_KEY] = {"schema": PREFERENCE_SCHEMA,
                                           "entries": project_next}
        novel_job.update_job_extension(job_dir, state_extension={"v2": state_v2})
    else:
        global_next = _replace(known_global, entry)
        merged_next = _merged(known_project, global_next)
        before = _durable_version(_merged(known_project, known_global))
        novel_job._atomic_write(Path(global_root) / GLOBAL_FILE_NAME,
                                {"schema": PREFERENCE_SCHEMA,
                                 "kind": "v2-user-preferences",
                                 "entries": global_next})
    version = _durable_version(merged_next)
    changed = version != before
    swept = v2_cache.discard_stale_preferences(job_dir,
                                               keep_preference_version=version) \
        if changed else None
    novel_job._append_event(job_dir, "feedback_applied", detail={
        "scope": scope, "kind": entry["kind"], "term_sha256": _sha(entry["term"]),
        "request_id": request_id, "preference_version": version, "cache": swept,
        "changed": changed})
    return {"scope": scope, "persisted": True, "changed": changed,
            "preference_version": version, "entries": len(merged_next),
            "effective": [dict(item, persisted=True) for item in merged_next],
            "cache": swept, "request_id": request_id}
