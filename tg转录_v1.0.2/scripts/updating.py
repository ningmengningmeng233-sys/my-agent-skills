"""Task 8：增量提交、编辑保留与有证据的删除状态（scripts/updating.py）。

接口（工单逐字）：

```python
plan_update(manifest: dict, upper_bounds: dict, options: dict) -> dict
commit_update(database: Path, plan: dict, fetched: list[dict]) -> dict
```

口径来自设计第 160–168 行（增量更新与变更保留）与其上面的共同数据边界：

1. **追加 + 有限回查**。默认只回查上次快照末端 7 天，并与用户原授权范围取交集；用户可
   显式换成别的**有界**区间（``recheck_since`` + ``recheck_until``），换成无界
   （``recheck_all``、``lookback_days=None``）一律拒绝。计划与报告都写出**实际回查到的
   范围**，并声明更早的编辑未核验——任何一轮都不宣称全历史同步（设计第 164 行）。
2. **在线水位只认凭据**。``importing.py`` 给本地导入标的是 ``online_coverage=
   "unverified"`` / ``online_complete=False``：没有验收出处的本地源**不继承在线水位**
   （``mode="bootstrap"``，本轮从原授权范围起点重覆盖），也不能把在线水位推到自己的
   最大 ID。最大消息 ID 只是定位信息，``max_id_proves_completeness`` 恒为 False。
3. **每轮上界一次定死**。``plan_update`` 把新上界写进计划并给出 ``round_id``；带
   ``resume_round`` 复跑时沿用已冻结的上界，新的探测值只记 ``deferred_upper_bound``，
   不扩大本轮范围（全局合同：恢复旧任务不重新探测上界）。
4. **提交走 catalog 现成的事务接口**：``connect``/``ensure_schema``/``begin``/
   ``stage_archive``/``publish_staged``/``commit``/``abort``。合并身份是 peer 类型 +
   chat ID + message ID（Task 5 冻结的归档模型）：重复区间既不重复成行、也不重复成检索
   命中；被编辑的消息更新当前视图，旧版本留在 ``revisions`` 里可取回。互斥只用
   ``state.acquire_archive_lock`` 那**一份**归档锁（锁根＝被锁资源，与 Task 5/6 同源），
   本模块不内联加锁三步、不新造第二把锁。
5. **删除要有证据**。只有后端给出明确删除事件（``backend_delete_event``），或对确切已存
   消息作成功且**权威**的复查（``authoritative_recheck`` + ``result=absent``）时，才写
   tombstone 与其证据（本模块自己的 ``update_events`` 台账表，与消息行同事务提交）；
   缺 ID、查询失败、权限丢失、网络失败、范围变化分别记 ``unverified`` / ``inaccessible``，
   绝不伪装成撤回。原档与历史**永不物理删除**：正文行保留、旧版本保留、原档文件只读。
6. **元数据不足就只追加**。来源没有可用时间字段时既无法界定回查窗口、也无法比较编辑
   前后，计划降级为 ``append_only`` 并写出原因；提交时不改写已存正文，逐条记
   ``append_only_skipped_edit``。
7. **先构建、验证通过才切版本指针**。派生版本先写进候选目录，构建报告可被磁盘核对（状态
   complete 且申报构件真实存在且非空）才原子切换 ``versions.json`` 的当前指针（复用
   ``state.publish_text_files`` 那份唯一实现）；构建抛错或验证不过时旧指针与旧视图原样
   保留，本轮记为待恢复（``pending``），同一 ``round_id`` 复跑即可续上。所选媒体未验收
   完成时该轮不报 complete。
8. **失败永不推进已提交水位**。水位只在本轮无失败、追加区间被带凭据的抓取完整覆盖、
   回查目标全部有结论、媒体已验收且派生版本切换成功时才推进；否则保持原值（没有凭据的
   本地源本来就没有在线水位，保持 ``None``）。

边界：不登录、不联网、不跑 tdl、不读真实归档；抓取结果由调用方（组合层或假后端）交进来，
本模块只做本地调度与状态处理。真实账号验证在所有报告里都是 ``NOT_RUN``。
"""

import copy
import datetime
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import catalog
import state

PLAN_KIND = "telegram-archive-update-plan"
REPORT_KIND = "telegram-archive-update-report"
STATE_KIND = "telegram-archive-update-state"
DELETION_KIND = "telegram-archive-deletion-states"
EVENT_KIND = "telegram-archive-update-event"
FAILURE_KIND = "telegram-archive-update-failure"
VERSION = 1
DEFAULT_LOOKBACK_DAYS = 7
DAY_SECONDS = 86400
MIN_LOOKBACK_DAYS = 1
MAX_LOOKBACK_DAYS = 365
PEER_TYPES = catalog.PEER_TYPES
VIEW = "raw"                                    # 规范层视图；阅读版属于派生版本那一层
ARCHIVE_KIND = catalog.ARCHIVE_KIND
ARCHIVE_VERSION = catalog.ARCHIVE_VERSION
NOT_RUN = "NOT_RUN"

# 可信的删除依据：明确删除事件，或对确切已存消息的权威复查成功。其余一律不算删除。
CREDIBLE_DELETION_BASES = ("backend_delete_event", "authoritative_recheck")
# 权限/会话类失败记「不可访问」，其余失败记「未核验」——两者都不写 tombstone。
INACCESSIBLE_REASONS = ("permission_lost", "session_lost", "not_a_member")
UNVERIFIED_REASONS = ("query_failed", "network_error", "range_changed", "partial_response",
                      "timeout", "unknown")
# 每条消息只留一条结论：越具体、越要紧的越优先（数字越小越优先），同级先来先留。
_RANK = {"deleted": 0, "inaccessible": 0, "append_only_skipped_edit": 1, "unverified": 2}
OPTION_KEYS = frozenset(("lookback_days", "recheck_since", "recheck_until", "recheck_all",
                         "resume_round", "media", "outputs", "lock_root", "now"))
EXIT_COMPLETE, EXIT_INCOMPLETE, EXIT_REJECTED, EXIT_CAPABILITY = 0, 1, 2, 3

_DDL = {
    "update_rounds": """
        round_id TEXT NOT NULL PRIMARY KEY,
        identities TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending','partial','complete','failed')),
        started_at TEXT NOT NULL, committed_at TEXT, upper_bound INTEGER,
        recheck_time_lower INTEGER, recheck_time_upper INTEGER, recheck_ids TEXT,
        blocking_reasons TEXT NOT NULL, report TEXT NOT NULL""",
    # 每身份一行已提交水位：只有带在线完成凭据且整轮通过时才推进
    "update_state": """
        identity TEXT NOT NULL PRIMARY KEY,
        watermark INTEGER, watermark_basis TEXT, local_max_message_id INTEGER,
        online_coverage TEXT NOT NULL, online_complete INTEGER NOT NULL
            CHECK (online_complete IN (0,1)),
        credential TEXT, round_id TEXT NOT NULL, updated_at TEXT NOT NULL""",
    # 本模块自有的台账表：tombstone + 证据，以及未核验/不可访问结论（删除语义不进
    # catalog 的正式表，catalog.py 由 Task 6 冻结，本任务不改它）
    "update_events": """
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id TEXT NOT NULL, view TEXT NOT NULL, identity TEXT NOT NULL,
        message_id INTEGER NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL,
        basis TEXT, evidence TEXT, detail TEXT, recorded_at TEXT NOT NULL,
        UNIQUE (round_id, view, identity, message_id, kind, reason)""",
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _now(value=None):
    if value:
        return str(value)
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _digest(*parts):
    return hashlib.sha256("\u241f".join("" if item is None else str(item)
                                         for item in parts).encode("utf-8")).hexdigest()


def _int(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value > 0 else None
    text = str(value).strip()
    if text.lstrip("-").isdigit() and int(text) > 0:
        return int(text)
    return None


def _min(values):
    picked = [item for item in values if item is not None]
    return min(picked) if picked else None


def _max(values):
    picked = [item for item in values if item is not None]
    return max(picked) if picked else None


def _epoch(value, label):
    """UTC 秒或带时区 ISO 日期都标准化成 UTC 秒；裸日期不猜时区，直接拒。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("%s 必须是 UTC 秒整数或带时区的 ISO 日期：%r" % (label, value))
    if isinstance(value, (int, float)):
        _require(value >= 0, "%s 不能是负数：%r" % (label, value))
        return int(value)
    _require(isinstance(value, str), "%s 类型不认识：%r" % (label, value))
    text = value.strip()
    try:
        moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("%s 不是可解析的 ISO 日期：%s（必须带时区，不猜本地时区）"
                         % (label, value)) from None
    _require(moment.tzinfo is not None,
             "%s %s 缺少时区：无法确定回查边界，请给带时区的 ISO 日期或 UTC 秒" % (label, value))
    return int(moment.timestamp())


def _identity_of(entry, label):
    identity = entry.get("identity")
    _require(isinstance(identity, str) and ":" in identity,
             "覆盖清单 %s 缺少 identity（应为 peer_type:chat_id）：增量按身份归档，"
             "不从显示名或文件名猜聊天" % label)
    kind, _, tail = identity.partition(":")
    _require(kind in PEER_TYPES and tail.strip().lstrip("-").isdigit(),
             "覆盖清单 %s 的 identity %r 形状不认识（peer 类型限 %s）"
             % (label, identity, "、".join(PEER_TYPES)))
    chat = _int(tail)
    _require(entry.get("peer_type") in (None, kind),
             "覆盖清单 %s 的 peer_type=%s 与 identity=%s 不一致：不代为改写身份"
             % (label, entry.get("peer_type"), identity))
    _require(entry.get("chat_id") in (None, chat),
             "覆盖清单 %s 的 chat_id=%s 与 identity=%s 不一致：不代为改写身份"
             % (label, entry.get("chat_id"), identity))
    return identity, kind, chat


def _bounds(value, label, required=True):
    raw = value if isinstance(value, dict) else {}
    lower = raw.get("lower")
    upper = raw.get("upper")
    if required:
        _require(lower is not None and upper is not None,
                 "%s 需要 lower 与 upper 两个边界：%r" % (label, value))
    out = {"lower": _int(lower) if lower is not None else None,
           "upper": _int(upper) if upper is not None else None}
    _require(out["lower"] is None or out["upper"] is None or out["lower"] <= out["upper"],
             "%s 的 lower 不得大于 upper：%r" % (label, value))
    return out


def _credential_of(entry):
    """在线完成凭据三件套：覆盖已验证 + 完成为真 + 可指认的验收出处，缺一不可。"""
    coverage = str(entry.get("online_coverage") or "unverified")
    credential = entry.get("completion_credential")
    usable = isinstance(credential, dict) and bool(credential.get("basis"))
    if coverage != "verified":
        return False, ("在线覆盖=%s（不是 verified）：本地导入不产生在线覆盖凭据，"
                       "不能据此继承在线水位或跳过在线导出" % coverage)
    if not entry.get("online_complete"):
        return False, "在线完成凭据 online_complete 不为真：不能继承在线水位"
    if not usable:
        return False, ("没有可指认的验收出处 completion_credential（已验收段清单/最终成品"
                       "摘要）：口头上的 verified 不构成在线水位")
    return True, None


def _stored_messages(entry, label):
    stored = entry.get("stored_messages")
    _require(isinstance(stored, list),
             "覆盖清单 %s 缺少 stored_messages：回查窗口要按已存消息的确切时间与 ID 界定，"
             "只拿最大消息 ID 当凭据不够" % label)
    out = []
    for item in stored:
        _require(isinstance(item, dict),
                 "覆盖清单 %s 的 stored_messages 有非映射项：%r" % (label, item))
        message_id = _int(item.get("message_id"))
        _require(message_id is not None,
                 "覆盖清单 %s 有已存消息缺少整数 message_id：%r" % (label, item))
        out.append({"message_id": message_id,
                    "date_utc": None if item.get("date_utc") is None
                    else _epoch(item["date_utc"], "stored_messages.date_utc")})
    out.sort(key=lambda item: item["message_id"])
    return out


# ---------------------------------------------------------------- plan_update

def plan_update(manifest, upper_bounds, options):
    """按已验收覆盖清单 + 本轮探测上界，产出**有界回查**的增量计划（纯本地，不联网）。"""
    _require(isinstance(manifest, dict), "manifest 必须是已验收覆盖清单(dict)：%s"
             % type(manifest).__name__)
    entries = manifest.get("chats")
    _require(isinstance(entries, list) and entries,
             "增量必须依托已验收的覆盖清单（manifest.chats 非空）：没有覆盖清单就没有水位，"
             "也不许顺手全量重抓")
    _require(isinstance(upper_bounds, dict), "upper_bounds 必须是各聊天本轮上界探测值(dict)：%s"
             % type(upper_bounds).__name__)
    opts = dict(options or {})
    unknown = sorted(str(key) for key in opts if key not in OPTION_KEYS)
    _require(not unknown, "未知选项：%s（可用：%s）" % ("、".join(unknown),
                                                      "、".join(sorted(OPTION_KEYS))))
    if opts.get("recheck_all"):
        raise ValueError("不接受无界回查（recheck_all）：请给出有界的 recheck_since/"
                         "recheck_until，或沿用默认末端 %d 天。全历史核对只在用户明确选定"
                         "该范围后另行执行，本轮不宣称全历史同步" % DEFAULT_LOOKBACK_DAYS)
    days = opts.get("lookback_days", DEFAULT_LOOKBACK_DAYS)
    lookback_days = _int(days)
    if lookback_days is None and opts.get("recheck_since") is None:
        raise ValueError("回查窗口必须是有界区间：lookback_days 要求正整数（默认 %d 天），"
                         "不接受「整段历史都核一遍」这种无界回查——那等于宣称全历史同步"
                         % DEFAULT_LOOKBACK_DAYS)
    _require(lookback_days is None or MIN_LOOKBACK_DAYS <= lookback_days <= MAX_LOOKBACK_DAYS,
             "回查天数限 %d..%d（默认 %d 天）；要更大范围请显式给出有界的 recheck_since/"
             "recheck_until" % (MIN_LOOKBACK_DAYS, MAX_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS))
    both = opts.get("recheck_since") is not None and opts.get("recheck_until") is not None
    _require(not (opts.get("recheck_since") is not None or opts.get("recheck_until") is not None)
             or both,
             "替换回查区间必须同时给出 recheck_since 与 recheck_until：半开区间就是无界回查，"
             "无法回答「实际回查到了哪里」，也不允许据此宣称全历史同步")
    resume = opts.get("resume_round")
    carried = {}
    if resume is None:
        pass
    elif isinstance(resume, dict):
        for item in resume.get("chats") or []:
            if isinstance(item, dict) and item.get("identity"):
                carried[str(item["identity"])] = item
    else:
        raise ValueError("resume_round 必须是上一次 plan_update 交出的计划(dict)：%s"
                         % type(resume).__name__)

    media = opts.get("media") if opts.get("media") is not None \
        else {"selected": False, "status": "not_selected"}
    _require(isinstance(media, dict), "media 必须是映射(dict)：%r" % (media,))
    outputs = opts.get("outputs")
    if outputs is not None:
        _require(isinstance(outputs, dict) and outputs.get("root"),
                 "outputs 需要给出候选版本的 root 目录：%r" % (outputs,))

    planned = []
    for position, raw in enumerate(entries):
        _require(isinstance(raw, dict), "覆盖清单第 %d 项不是映射(dict)" % (position + 1))
        identity, peer_type, chat_id = _identity_of(raw, "第 %d 项" % (position + 1))
        authorized = _bounds(raw.get("authorized_range"), "聊天 %s 的原授权 ID 范围" % identity)
        authorized_time = _bounds(raw.get("authorized_time_range"),
                                  "聊天 %s 的原授权时间范围" % identity, required=False)
        stored = _stored_messages(raw, identity)
        probe = upper_bounds.get(identity)
        if probe is None and len(entries) == 1 and len(upper_bounds) == 1:
            probe = next(iter(upper_bounds.values()))
        _require(probe is not None,
                 "聊天 %s 缺本轮上界探测值：没有新的快照上界就不做增量，不拿旧上界冒充" % identity)
        observed = _int(probe)
        _require(observed is not None, "聊天 %s 的本轮上界 %r 不是正整数消息 ID"
                 % (identity, probe))

        inherited, watermark_reason = _credential_of(raw)
        previous_watermark = _int(raw.get("watermark")) if inherited else None
        detection = raw.get("edit_detection") if isinstance(raw.get("edit_detection"), dict) \
            else {}
        detectable = bool(detection.get("detectable")) and any(
            item["date_utc"] is not None for item in stored)
        metadata_gap = None if detectable else (
            "来源缺少可用的时间字段（date_utc）：既无法界定回查窗口，也无法比较同一消息"
            "编辑前后的正文差异。本轮只追加新消息，已存正文一律不改写、不声称检测过编辑。"
            "清单给出的原因：%s" % (detection.get("basis") or "未说明"))

        holder = carried.get(identity)
        if holder is not None and _int(holder.get("new_upper_bound")) is not None:
            new_upper = _int(holder["new_upper_bound"])
            bound_source = "resumed"
        else:
            new_upper, bound_source = observed, "probed"
        floor = previous_watermark if previous_watermark else authorized["lower"]
        _require(new_upper >= floor,
                 "聊天 %s 的本轮上界 %s 低于已提交水位/授权起点 %s：区间倒退，"
                 "既不扩大也不缩小本轮范围，拒绝规划" % (identity, new_upper, floor))

        append = {"id_lower": max((previous_watermark + 1) if previous_watermark
                                  else authorized["lower"], authorized["lower"]),
                  "id_upper": min(new_upper, authorized["upper"]), "unit": "message_id"}
        mode = "append_only" if not detectable else ("bootstrap" if not inherited
                                                     else "append_and_recheck")
        anchor = None
        snapshot = raw.get("snapshot") if isinstance(raw.get("snapshot"), dict) else {}
        if snapshot.get("date_upper") is not None:
            anchor = _epoch(snapshot["date_upper"], "snapshot.date_upper")
        if both:
            window = {"lower": _epoch(opts["recheck_since"], "recheck_since"),
                      "upper": _epoch(opts["recheck_until"], "recheck_until")}
            source, days_used = "user", lookback_days
        elif anchor is not None and lookback_days:
            window = {"lower": anchor - lookback_days * DAY_SECONDS, "upper": anchor}
            source = ("default_7d" if lookback_days == DEFAULT_LOOKBACK_DAYS
                      else "default_%dd" % lookback_days)
            days_used = lookback_days
        else:
            window, source, days_used = {"lower": None, "upper": None}, "unavailable", None
        clamped = False
        if window["lower"] is not None:
            lower, upper = window["lower"], window["upper"]
            if authorized_time["lower"] is not None and lower < authorized_time["lower"]:
                lower, clamped = authorized_time["lower"], True
            if authorized_time["upper"] is not None and upper > authorized_time["upper"]:
                upper, clamped = authorized_time["upper"], True
            window = {"lower": min(lower, upper), "upper": max(lower, upper)}
        if mode == "append_only":
            window, source, days_used, clamped = {"lower": None, "upper": None}, \
                "skipped_missing_metadata", None, False
        if mode == "bootstrap":
            stamps = [item["date_utc"] for item in stored if item["date_utc"] is not None]
            window = {"lower": authorized_time["lower"] if authorized_time["lower"]
                      is not None else _min(stamps),
                      "upper": authorized_time["upper"] if authorized_time["upper"]
                      is not None else _max(stamps)}
            source, days_used, clamped = "covered_by_bootstrap_append", None, False

        recheck_ids = [item["message_id"] for item in stored
                       if mode != "append_only" and item["date_utc"] is not None
                       and window["lower"] is not None
                       and window["lower"] <= item["date_utc"] <= window["upper"]]
        stored_ids = [item["message_id"] for item in stored]
        older = [item for item in stored_ids if item not in recheck_ids]
        statement = "实际回查范围：%s（回查目标：%s）" % (
            "无（本轮不声明任何回查）" if window["lower"] is None
            else "UTC 秒 %s..%s" % (window["lower"], window["upper"]),
            "、".join("ID %d" % item for item in recheck_ids) if recheck_ids else "无")
        planned.append({
            "identity": identity, "peer_type": peer_type, "chat_id": chat_id,
            "display_name": raw.get("display_name"), "archive_path": raw.get("archive_path"),
            "batch_id": raw.get("batch_id"),
            "authorized_range": authorized, "authorized_time_range": authorized_time,
            "previous_watermark": previous_watermark,
            "online_watermark_inherited": inherited,
            "watermark_reason": watermark_reason or "沿用已验收的在线覆盖清单水位",
            "snapshot": {"upper_bound": _int(snapshot.get("upper_bound")),
                         "date_upper": anchor},
            "observed_upper_bound": observed, "new_upper_bound": new_upper,
            "upper_bound_source": bound_source,
            "deferred_upper_bound": observed if (bound_source == "resumed"
                                                 and observed != new_upper) else None,
            "upper_bound_is_locator": True,
            "append": append, "mode": mode,
            "edits_detectable": detectable, "metadata_gap": metadata_gap,
            "lookback": {"days": days_used, "source": source, "time_range": window,
                         "message_ids": recheck_ids, "clamped": clamped,
                         "basis": {"default_7d": "上次快照末端 7 天与原授权范围的交集（默认）",
                                   "user": "用户显式替换的有界区间（仍与原授权范围取交集）",
                                   "skipped_missing_metadata": "来源元数据不足，无法界定窗口",
                                   "covered_by_bootstrap_append": "无在线水位可继承：本轮重覆盖"
                                                                  "原授权范围，回查即随追加一起做",
                                   "unavailable": "清单未给出快照末端时间，无法界定窗口"}
                                   .get(source, source)},
            "stored_message_ids": stored_ids,
            "unverified": {"message_ids": older, "edits_before": window["lower"],
                           "statement": "回查窗口之外的已存消息共 %d 条（%s），其编辑状态未核验："
                                        "本轮不声称核验过它们；更早的历史同步需要用户明确"
                                        "选定该范围后另行执行"
                                        % (len(older), "、".join("ID %d" % item
                                                                 for item in older)
                                           or "无")},
            "recheck_statement": statement,
            "claims": {"full_history_sync": False, "max_id_proves_completeness": False,
                       "lookback_is_bounded": window["lower"] is not None,
                       "real_account_verification": NOT_RUN},
            "limitations": {"recheck_window": "回查是有界窗口，不是全历史核对",
                            "max_message_id": "最大消息 ID 只用于定位，不证明区间内无缺失，"
                                              "也不证明没有更晚的编辑",
                            "older_edits": "窗口外的编辑未核验，逐条列在未核验清单里"},
        })

    round_id = _digest("round", [(item["identity"], item["previous_watermark"],
                                  item["new_upper_bound"], item["mode"])
                                 for item in planned])
    return {
        "kind": PLAN_KIND, "version": VERSION, "round_id": round_id, "frozen": True,
        "planned_at": _now(opts.get("now")),
        "lookback_days_default": DEFAULT_LOOKBACK_DAYS,
        "lock_root": str(opts.get("lock_root") or Path.cwd()),
        "outputs": copy.deepcopy(outputs), "media": copy.deepcopy(media),
        "chats": planned,
        "claims": {"full_history_sync": False, "max_id_proves_completeness": False,
                   "online_watermark_from_credential_only": True, "network_used": False,
                   "login_used": False, "tdl_invoked": False,
                   "real_account_verification": NOT_RUN,
                   "note": "计划不联网、不抓取：只把已验收覆盖清单与本轮冻结上界换算成有界的"
                           "追加/回查区间"},
        "limitations": {"lookback": "默认回查上次快照末端 %d 天；用户可显式换成别的有界区间"
                                  % DEFAULT_LOOKBACK_DAYS,
                        "completeness": "任何一轮都不宣称全历史已同步",
                        "locator": "最大消息 ID 是定位信息，不是完成凭据"},
    }


def apply_committed_watermarks(manifest, database):
    """把索引库里**已提交水位**回灌进覆盖清单（组合层每轮都这样接上上一轮的真实结果）。

    只推进数值水位，不伪造凭据：``online_coverage``/``completion_credential`` 原样保留，
    所以没有验收凭据的本地源仍然拿不到在线水位。
    """
    out = copy.deepcopy(manifest)
    committed = load_update_state(database)
    by_identity = committed.get("watermarks") or {}
    if not by_identity:
        return out
    stamps = _stored_dates(database)
    for entry in out.get("chats") or []:
        identity = str(entry.get("identity") or "")
        watermark = by_identity.get(identity)
        if watermark is None:
            continue
        current = _int(entry.get("watermark"))
        if current is not None and watermark <= current:
            continue
        entry["watermark"] = watermark
        snapshot = dict(entry.get("snapshot") or {})
        snapshot["upper_bound"] = watermark
        stamp = (stamps.get(identity) or {}).get(watermark)
        if stamp is not None:
            snapshot["date_upper"] = stamp
        entry["snapshot"] = snapshot
        entry["watermark_source"] = "committed_ledger"
    return out


# ---------------------------------------------------------------- 抓取结果分类

def _classify_fetched(fetched, identities):
    """把组合层/假后端交来的抓取结果分成消息批次、删除事件、失败三类，并钉死身份归属。"""
    batches, events, failures = [], [], []
    for position, record in enumerate(fetched or []):
        _require(isinstance(record, dict),
                 "第 %d 项抓取结果不是映射(dict)：不接受裸消息或裸数字" % (position + 1))
        kind = record.get("kind")
        identity = str(record.get("identity") or "")
        if kind == ARCHIVE_KIND:
            _require(identity in identities,
                     "抓取结果里的聊天 %s 不在本轮计划里（计划含 %s）：不代为合并陌生身份"
                     % (identity, "、".join(sorted(identities))))
            batches.append(record)
        elif kind == EVENT_KIND:
            _require(identity in identities,
                     "删除事件里的聊天 %s 不在本轮计划里：不往陌生身份上写标记" % identity)
            _require(_int(record.get("message_id")) is not None,
                     "删除事件缺少整数 message_id：%r（指不到确切消息就无法留证据）" % record)
            events.append(record)
        elif kind == FAILURE_KIND:
            _require(identity in identities, "失败记录里的聊天 %s 不在本轮计划里" % identity)
            reason = str(record.get("reason") or "unknown")
            _require(reason in INACCESSIBLE_REASONS + UNVERIFIED_REASONS,
                     "失败原因 %r 不认识（可用：%s）：不认识的失败既不能判撤回也不能判通过"
                     % (reason, "、".join(sorted(INACCESSIBLE_REASONS + UNVERIFIED_REASONS))))
            failures.append(record)
        else:
            raise ValueError("第 %d 项抓取结果的 kind=%r 不认识：只接受已归一化归档(%s)、%s 或 %s"
                             % (position + 1, kind, ARCHIVE_KIND, EVENT_KIND, FAILURE_KIND))
    return batches, events, failures


def _batch_credential(batch):
    """一批抓取是否带在线完成凭据：三件套齐全才算，且交出它覆盖的 ID 区间。"""
    coverage = batch.get("coverage") or {}
    credential = coverage.get("completion_credential")
    ok = (str(coverage.get("online_coverage") or "") == "verified"
          and bool(coverage.get("online_complete"))
          and isinstance(credential, dict) and bool(credential.get("basis")))
    return ok, _int(coverage.get("id_lower")), _int(coverage.get("id_upper"))


def _incoming_messages(batches, entry):
    """按身份合并本轮消息：返回 ``message_id -> 消息``、重复条数与覆盖凭据区间。"""
    authorized = entry["authorized_range"]
    window = entry["lookback"]["time_range"]
    allowed = set(entry["lookback"]["message_ids"]) | set(entry["stored_message_ids"])
    watermark = entry["previous_watermark"] or 0
    merged, duplicates = {}, 0
    covered_lower, covered_upper, credentialed = None, None, False
    for batch in batches:
        if str(batch.get("identity")) != entry["identity"]:
            continue
        _require(_int(batch.get("version")) == ARCHIVE_VERSION,
                 "聊天 %s 这一批来源的归档 schema 版本是 %r，本实现只支持 %s：来历不明的"
                 "结构不并进本轮，整轮按失败处理并已回滚"
                 % (entry["identity"], batch.get("version"), ARCHIVE_VERSION))
        _require(isinstance(batch.get("source"), dict) and batch["source"].get("batch_id"),
                 "聊天 %s 这一批来源缺少 source.batch_id：每批都要能追溯，不能事后靠文件名猜"
                 % entry["identity"])
        ok, lower, upper = _batch_credential(batch)
        credentialed = credentialed or ok
        if ok:
            covered_lower = _min([covered_lower, lower])
            covered_upper = _max([covered_upper, upper])
        for item in batch.get("messages") or []:
            message_id = _int(item.get("message_id"))
            _require(message_id is not None,
                     "归档 %s 有消息缺少整数 message_id：不猜归属" % batch.get("identity"))
            _require(authorized["lower"] <= message_id <= authorized["upper"],
                     "抓取到的消息 %s（聊天 %s）越出原授权 ID 范围 [%s, %s]：不扩大范围，"
                     "也不把它塞进本轮" % (message_id, entry["identity"], authorized["lower"],
                                          authorized["upper"]))
            stamp = item.get("date_utc")
            in_window = message_id in allowed or message_id > watermark or (
                window["lower"] is not None and isinstance(stamp, int)
                and window["lower"] <= stamp <= window["upper"])
            _require(in_window,
                     "消息 %s（聊天 %s）既不在追加区间也不在回查窗口里：本轮不收，"
                     "请留给明确选定该范围的下一轮" % (message_id, entry["identity"]))
            if message_id in merged:
                duplicates += 1
                continue                        # 先给的来源为准，不静默覆盖
            normalized = copy.deepcopy(item)
            normalized["identity_key"] = "%s#%d" % (entry["identity"], message_id)
            normalized["attachments"] = [dict(node) for node in (item.get("attachments") or [])]
            normalized["sender"] = dict(item.get("sender") or {})
            normalized["reply_to"] = dict(item.get("reply_to") or {})
            merged[message_id] = normalized
    return merged, duplicates, {"covered_lower": covered_lower, "covered_upper": covered_upper,
                                "credentialed": credentialed}


# ---------------------------------------------------------------- 索引库当前视图

def _message_from_row(identity, row, attachments):
    return {"message_id": row["message_id"], "identity_key": "%s#%d" % (identity,
                                                                        row["message_id"]),
            "date_utc": row["date_utc"], "display_time": row["display_time"],
            "time_missing": bool(row["time_missing"]), "text": row["text"],
            "text_missing": not row["text"],
            "sender": {"name": row["sender_name"], "id": row["sender_id"],
                       "missing": bool(row["sender_missing"])},
            "reply_to": {"message_id": row["reply_to_message_id"]},
            "attachments": attachments, "service": bool(row["service"]),
            "action": row["action"]}


def _current_document(conn, identity):
    """把已提交视图里的行读回来：合并永远从**旧行 + 本轮结果**出发，缺 ID 不会消失。"""
    attachments = {}
    for row in conn.execute("SELECT * FROM attachments WHERE view = ? AND identity = ?"
                            " ORDER BY message_id, position", (VIEW, identity)):
        attachments.setdefault(row["message_id"], []).append({
            "category": row["category"], "path": row["path"], "mime_type": row["mime_type"],
            "declared_media_type": row["declared_media_type"], "width": row["width"],
            "height": row["height"], "duration_seconds": row["duration_seconds"],
            "title": row["title"]})
    messages = [_message_from_row(identity, row, attachments.get(row["message_id"], []))
                for row in conn.execute("SELECT * FROM messages WHERE view = ? AND identity = ?"
                                        " ORDER BY message_id", (VIEW, identity))]
    chat = conn.execute("SELECT * FROM chats WHERE identity = ?", (identity,)).fetchone()
    batch = conn.execute("SELECT * FROM source_batches WHERE view = ? AND identity = ?"
                         " ORDER BY indexed_at DESC LIMIT 1", (VIEW, identity)).fetchone()
    return {"messages": messages, "chat": dict(chat) if chat else {},
            "batch": dict(batch) if batch else {}}


def _stored_dates(database):
    """已提交视图里每条消息的时间：水位回灌时要按新水位那条的真实时间定回查末端。"""
    out = {}
    path = Path(str(database))
    if not path.is_file():
        return out
    conn = sqlite3.connect(str(path))
    try:
        for row in conn.execute("SELECT identity, message_id, date_utc FROM messages"
                                " WHERE view = 'raw'"):
            out.setdefault(row[0], {})[int(row[1])] = row[2]
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return out


def _has_ledger(database):
    path = Path(str(database))
    if not path.is_file():
        return False
    conn = sqlite3.connect(str(path))
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return "update_state" in names


# ---------------------------------------------------------------- 台账读取

def load_update_state(database, identity=None):
    """读增量台账：已提交水位、待恢复轮次与逐轮结论（只读；缺库缺表不编造）。"""
    path = Path(str(database))
    empty = {"kind": STATE_KIND, "version": VERSION, "database": str(path),
             "ledger_present": False, "identity": identity, "watermark": None,
             "watermarks": {}, "state": [], "rounds": [], "pending": []}
    if not _has_ledger(path):
        return empty
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        rounds = [dict(row) for row in conn.execute(
            "SELECT * FROM update_rounds ORDER BY started_at")]
        states = [dict(row) for row in conn.execute(
            "SELECT * FROM update_state ORDER BY identity")]
    except sqlite3.Error:
        return empty
    finally:
        conn.close()
    watermarks = {str(row["identity"]): (None if row["watermark"] is None
                                         else int(row["watermark"])) for row in states}
    wanted = identity if identity is not None else (
        next(iter(watermarks)) if len(watermarks) == 1 else None)
    picked = [row for row in rounds if wanted is None or wanted in json.loads(row["identities"])]
    return {"kind": STATE_KIND, "version": VERSION, "database": str(path),
            "ledger_present": True, "identity": wanted,
            "watermark": watermarks.get(wanted), "watermarks": watermarks,
            "state": states, "rounds": picked,
            "pending": [{"round_id": row["round_id"], "identities": row["identities"],
                         "status": row["status"], "started_at": row["started_at"],
                         "blocking_reasons": json.loads(row["blocking_reasons"])}
                        for row in picked if row["status"] in ("pending", "partial")]}


def deletion_states(database, identity=None, view=VIEW):
    """已确证的删除标记（tombstone）与其证据；证据不足的一律不在此列。"""
    path = Path(str(database))
    if not _has_ledger(path):
        return {"kind": DELETION_KIND, "version": VERSION, "identity": identity,
                "ledger_present": False, "deletions": [], "count": 0}
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    clauses, params = ["kind = 'deleted'", "view = ?"], [view]
    if identity is not None:
        clauses.append("identity = ?")
        params.append(identity)
    try:
        rows = conn.execute("SELECT * FROM update_events WHERE %s"
                            " ORDER BY identity, message_id, recorded_at, event_id"
                            % " AND ".join(clauses), params).fetchall()
    except sqlite3.Error:
        rows = []
    conn.close()
    latest = {}
    for row in rows:
        latest[(str(row["identity"]), int(row["message_id"]))] = row
    deletions = [{"identity": key[0], "message_id": key[1], "view": row["view"],
                  "kind": "deleted", "basis": row["basis"], "reason": row["reason"],
                  "evidence": json.loads(row["evidence"]) if row["evidence"] else None,
                  "detail": row["detail"], "round_id": row["round_id"],
                  "recorded_at": row["recorded_at"]}
                 for key, row in sorted(latest.items())]
    return {"kind": DELETION_KIND, "version": VERSION, "identity": identity,
            "ledger_present": True, "deletions": deletions, "count": len(deletions),
            "claims": {"evidence_required": True, "raw_archive_deleted": False,
                       "missing_ids_are_not_deletions": True,
                       "real_account_verification": NOT_RUN}}


# ---------------------------------------------------------------- 派生版本与指针

def _verify_build(directory, built):
    """构建报告要能被磁盘核对：状态 complete + 申报构件真实存在且非空。"""
    if not isinstance(built, dict):
        return False, "构建器没有交回报告(dict)：无法验证新版本"
    if str(built.get("status")) != "complete":
        return False, "构建报告状态=%s（不是 complete）：不切换当前版本" % built.get("status")
    files = built.get("files")
    if not isinstance(files, list) or not files:
        return False, "构建报告没有申报构件文件清单：无法确认新版本可读"
    base = Path(str(directory))
    missing = []
    for name in files:
        candidate = Path(str(name))
        if not candidate.is_absolute():
            candidate = base / str(name)
        if not candidate.is_file() or candidate.stat().st_size == 0:
            missing.append(str(name))
    if missing:
        return False, "构建报告申报的文件在候选目录里不存在或为空：%s" % "、".join(missing)
    return True, None


def _switch_pointer(root, record):
    """原子切换当前版本指针（复用 state.publish_text_files 那份唯一实现）。"""
    pointer = Path(root) / "versions.json"
    current = {}
    if pointer.is_file():
        try:
            loaded = json.loads(pointer.read_text(encoding="utf-8"))
            current = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            current = {}
    previous = current.get("current")
    history = [item for item in (current.get("history") or [])
               if item.get("round_id") != (previous or {}).get("round_id")]
    if previous:
        history.append(previous)
    payload = {"kind": "telegram-archive-derived-versions", "version": VERSION,
               "current": record, "previous": previous, "history": history[-20:],
               "switched_at": record["switched_at"],
               "claims": {"verified_before_switch": True, "raw_archive_modified": False,
                          "real_account_verification": NOT_RUN}}
    state.publish_text_files(Path(root), {"versions.json": json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True)})
    return payload


def index_version_builder(archives):
    """默认构建器：把本轮合并后的内容真建成一个新的索引版本（可只读复核）。

    交给 ``catalog.index_archive`` 走它自己的事务与归档锁（锁根＝候选目录，与本模块
    持有的归档锁不同源但互不阻塞），建完再用 ``catalog.database_status`` 读一遍：
    读不通就抛，指针绝不切换。
    """
    def build(candidate_dir):
        directory = Path(str(candidate_dir))
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "catalog-index.db"
        report = catalog.index_archive(target, archives, VIEW, rebuild=True,
                                       lock_root=directory)
        name = "索引状态.json"
        (directory / name).write_text(json.dumps(
            {"reindexed": report["reindexed"], "counts": report["counts"],
             "view": report["view"], "database": target.name}, ensure_ascii=False,
            indent=2), encoding="utf-8")
        catalog.database_status(target)
        return {"status": report["status"], "files": [name, "catalog-index.db"]}
    return build


# ---------------------------------------------------------------- commit_update

def _add_finding(store, message_id, kind, reason, basis=None, evidence=None, detail=None):
    """一条消息可以同时有多个结论（例如「窗口外未核验」+「只追加时跳过的编辑」）。

    同一个 ``(消息, 结论, 原因)`` 只记一次；越具体、越要紧的结论排得越前。
    """
    key = (message_id, kind, reason)
    store.setdefault(key, {"message_id": message_id, "kind": kind, "reason": reason,
                           "basis": basis, "evidence": evidence, "detail": detail})


def _sorted_findings(store):
    """结论清单：同一条消息可以带多条不同结论，按 (消息, 结论要紧度, 原因) 稳定排序。"""
    return [store[key] for key in sorted(store, key=lambda item: (item[0],
                                                                  _RANK.get(item[1], 9),
                                                                  item[2]))]


def _drop_findings(store, message_id):
    """可信删除事件给这条消息下了结论，就把此前「没查到」式的临时结论撤掉。"""
    for key in [item for item in store if item[0] == message_id]:
        del store[key]


def commit_update(database, plan, fetched):
    """把一轮增量提交进本地索引：合并消息、留证据、必要时推进水位，最后才切版本指针。"""
    _require(isinstance(plan, dict) and plan.get("kind") == PLAN_KIND,
             "commit_update 需要 plan_update 交出的计划(kind=%s)：%r"
             % (PLAN_KIND, plan.get("kind") if isinstance(plan, dict) else plan))
    entries = plan.get("chats") or []
    _require(entries, "计划里没有任何聊天：不提交空轮")
    identities = [str(item["identity"]) for item in entries]
    _require(len(set(identities)) == len(identities), "计划里有重复身份：%s" % identities)
    round_id = str(plan.get("round_id") or _digest("round", identities))
    started_at = str(plan.get("planned_at") or _now())
    stamp = plan.get("planned_at") or _now()
    database = Path(str(database))
    lock_root = Path(str(plan.get("lock_root") or database.parent))
    batches, events, failures = _classify_fetched(fetched, set(identities))

    lock = state.acquire_archive_lock(lock_root, resource=lock_root,
                                      owner={"label": "update", "round_id": round_id})
    try:
        merged = _commit_transaction(database, plan, entries, identities, round_id,
                                     started_at, batches, events, failures)
        message_ok = all(item["advance"] for item in merged) and bool(merged)
        blocking = [reason for item in merged for reason in item["blocking_reasons"]]
        versions = _handle_versions(database, plan, round_id, entries, merged)
        if not versions["accepted"]:
            message_ok = False
            blocking.append(versions["reason"])
            for item in merged:
                item["blocking_reasons"].append(versions["reason"])
        advanced = bool(message_ok and versions["accepted"])
        report = _build_report(plan, entries, merged, round_id, started_at, advanced,
                               blocking, versions, stamp)
        _finalize_state(database, round_id, merged, advanced, blocking, stamp, report)
        return report
    finally:
        state.release_archive_lock(lock)


def _stage_document(plan, entry, identity, staged, previous, coverage, stamp):
    """把合并结果包成 Task 5 归档 dict，交给 catalog.stage_archive 校验与写入。"""
    messages = [staged[key] for key in sorted(staged)]
    dates = [item["date_utc"] for item in messages if isinstance(item.get("date_utc"), int)]
    online = "verified" if (entry["online_watermark_inherited"] and coverage["credentialed"]) \
        else "unverified"
    batch_id = "update-%s" % _digest(plan.get("round_id"), identity)[:24]
    return {"kind": ARCHIVE_KIND, "version": ARCHIVE_VERSION, "identity": identity,
            "peer_type": entry["peer_type"], "chat_id": entry["chat_id"],
            "identity_basis": {"peer_type": "accepted_coverage_manifest",
                               "chat_id": "accepted_coverage_manifest",
                               "guessed_from_name_or_title": False},
            "chat": {"peer_type": entry["peer_type"], "id": entry["chat_id"],
                     "display_name": (previous["chat"] or {}).get("display_name")
                     or entry.get("display_name"),
                     "display_name_basis": "accepted_coverage_manifest",
                     "locator": (previous["chat"] or {}).get("locator")},
            "source": {"format": "telegram-update-round", "adapter": "updating",
                       "path": entry.get("archive_path")
                       or (previous["batch"] or {}).get("source_path"),
                       "name": "update-round-%s" % batch_id,
                       "sha256_before": _digest(batch_id, identity, entry["new_upper_bound"]),
                       "batch_id": batch_id, "read_at": stamp},
            "time": {"utc_field": "date_utc",
                     "display_timezone": (previous["batch"] or {}).get("display_timezone")
                     or "unknown"},
            "coverage": {"message_count": len(messages),
                         "id_lower": _min(list(staged)), "id_upper": _max(list(staged)),
                         "date_lower": _min(dates), "date_upper": _max(dates),
                         "online_coverage": online, "online_complete": online == "verified",
                         "online_completion_credential": None if online == "unverified" else {
                             "basis": "accepted_coverage_manifest",
                             "reference": str(plan.get("round_id"))},
                         "claim": "本轮在线抓取已验收" if online == "verified"
                         else "在线覆盖未验证：不能据此跳过在线导出或推进在线水位",
                         "max_message_id_is_locator_only": True},
            "messages": messages, "archive_path": entry.get("archive_path")}


def _merge_chat(conn, plan, entry, batches, events, failures):
    """一个聊天的合并：返回新行集合、发现清单与「本轮能不能推进水位」。"""
    identity = str(entry["identity"])
    mode = entry["mode"]
    previous = _current_document(conn, identity)
    stored_rows = {item["message_id"]: item for item in previous["messages"]}
    incoming, duplicates, coverage = _incoming_messages(batches, entry)

    findings = {}
    for record in failures:
        if str(record.get("identity")) != identity:
            continue
        reason = str(record.get("reason"))
        kind = "inaccessible" if reason in INACCESSIBLE_REASONS else "unverified"
        affected = ((record.get("affected") or {}).get("message_ids")) or []
        targets = [_int(item) for item in affected] if affected else list(stored_rows)
        for message_id in targets:
            if message_id is None:
                continue
            _add_finding(findings, message_id, kind, reason, basis="backend_failure",
                         evidence=record.get("affected") or None, detail=record.get("detail"))

    deleted = {}
    for record in events:
        if str(record.get("identity")) != identity:
            continue
        message_id = _int(record.get("message_id"))
        evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
        basis = str(evidence.get("basis") or record.get("basis") or "")
        credible = basis in CREDIBLE_DELETION_BASES and str(record.get("event")) == "deleted"
        if basis == "authoritative_recheck":
            credible = credible and str(evidence.get("result")) == "absent" \
                and bool(evidence.get("reference") or evidence.get("checked_by"))
        if credible:
            deleted[message_id] = {"basis": basis, "evidence": evidence,
                                   "reason": basis, "detail": record.get("detail")}
            _drop_findings(findings, message_id)
        else:
            _add_finding(findings, message_id, "unverified", "uncredible_deletion_basis",
                         basis=basis or None, evidence=evidence,
                         detail="依据 %r 不在可信删除依据（%s）里：抓取里缺这条 ID 只说明"
                                "没查到，不能判成撤回" % (basis, "、".join(CREDIBLE_DELETION_BASES)))

    merged = dict(stored_rows)
    appended, edited, skipped, unchanged = [], [], [], []
    for message_id in sorted(incoming):
        if message_id in deleted:
            continue
        before = stored_rows.get(message_id)
        message = incoming[message_id]
        if before is None:
            merged[message_id] = message
            appended.append(message_id)
        elif (message.get("text") or "") != (before.get("text") or ""):
            if mode == "append_only":
                skipped.append(message_id)
                _add_finding(findings, message_id, "append_only_skipped_edit",
                             "metadata_insufficient",
                             detail="来源元数据不足以判定编辑：本轮只追加，未改写已存正文")
            else:
                merged[message_id] = message
                edited.append(message_id)
        else:
            unchanged.append(message_id)

    requested = list(entry["lookback"]["message_ids"])
    checked = sorted(item for item in requested
                     if item in deleted or (item in incoming and item not in skipped))
    not_covered = [item for item in requested if item not in checked]
    for message_id in not_covered:
        _add_finding(findings, message_id, "unverified", "recheck_not_covered",
                     detail="已请求回查但本轮抓取没交回这条，也没有可信删除事件：记未核验，"
                            "不记撤回")
    older = list(entry["unverified"]["message_ids"])
    for message_id in older:
        if message_id in deleted:
            continue
        _add_finding(findings, message_id, "unverified", "outside_recheck_window",
                     detail="在回查窗口之外，本轮未核验其编辑状态")

    archive = _stage_document(plan, entry, identity, merged, previous, coverage,
                              str(plan.get("planned_at") or _now()))
    stats = catalog.stage_archive(conn, VIEW, archive)

    append_plan = entry["append"]
    append_open = append_plan["id_lower"] <= append_plan["id_upper"]
    append_covered = (not append_open) or (
        coverage["credentialed"] and coverage["covered_lower"] is not None
        and coverage["covered_lower"] <= append_plan["id_lower"]
        and coverage["covered_upper"] is not None
        and coverage["covered_upper"] >= append_plan["id_upper"])
    blocking = []
    for record in failures:
        if str(record.get("identity")) == identity:
            blocking.append("聊天 %s 本轮失败（%s）：记未核验/不可访问，不记撤回"
                            % (identity, record.get("reason")))
    if not entry["online_watermark_inherited"]:
        blocking.append("聊天 %s 没有可继承的在线水位（%s）：本轮可提交消息，但在线覆盖"
                        "仍记未验证" % (identity, entry["watermark_reason"]))
    if not append_covered:
        blocking.append("聊天 %s 的追加区间 [%s, %s] 没有被带在线完成凭据的抓取完整覆盖："
                        "缺段不推进完成水位" % (identity, append_plan["id_lower"],
                                                append_plan["id_upper"]))
    if not_covered:
        blocking.append("聊天 %s 的回查目标未全部有结论（未覆盖：%s）"
                        % (identity, "、".join(str(item) for item in not_covered)))
    media = plan.get("media") or {}
    if media.get("selected") and str(media.get("status")) != "complete":
        blocking.append("所选媒体未验收完成（status=%s）：本轮混合更新不报全部完成"
                        % media.get("status"))
    before_watermark = entry["previous_watermark"]
    return {"identity": identity, "mode": mode, "upper_bound": entry["new_upper_bound"],
            "lookback": {"time_range": entry["lookback"]["time_range"]},
            "watermark_reason": (
                "水位保持 %s：%s" % (before_watermark, "；".join(blocking)) if blocking else
                "本轮追加、回查、媒体全部通过，水位推进到冻结上界 %s"
                % entry["new_upper_bound"]),
            "recheck": {"requested_ids": requested, "checked_ids": checked,
                        "not_covered_ids": not_covered, "older_unverified_ids": older,
                        "time_range": entry["lookback"]["time_range"],
                        "statement": "%s；回查窗口之外的编辑未核验（%s）"
                                     % (entry["recheck_statement"],
                                        entry["unverified"]["statement"])},
            "findings": [findings[key] for key in sorted(findings)],
            "deleted": sorted(deleted),
            "deletion_records": [dict(deleted[key], message_id=key) for key in sorted(deleted)],
            "counts": {"appended": len(appended), "edited": len(edited),
                       "unchanged": len(unchanged), "duplicate_ids": duplicates,
                       "deleted": len(deleted),
                       "unverified": sum(1 for item in findings.values()
                                         if item["kind"] == "unverified"),
                       "inaccessible": sum(1 for item in findings.values()
                                           if item["kind"] == "inaccessible"),
                       "append_only_skipped_edit": len(skipped),
                       "older_unverified": len(older),
                       "rows_staged": int(stats["rows_staged"])},
            "blocking_reasons": blocking, "advance": not blocking,
            "before": before_watermark,
            "after": entry["new_upper_bound"] if not blocking else before_watermark,
            "local_max_message_id": _max(list(merged)),
            "online_coverage": "verified" if (entry["online_watermark_inherited"]
                                              and coverage["credentialed"]) else "unverified",
            "credential": json.dumps({"prior_batch_id": entry.get("batch_id"),
                                      "round_id": str(plan.get("round_id")),
                                      "watermark_reason": entry["watermark_reason"]},
                                     ensure_ascii=False),
            "mode_reason": entry["metadata_gap"] or "来源元数据足够，可按身份比较编辑前后",
            "batch_id": archive["source"]["batch_id"], "staged": len(merged),
            "merged_archive": archive}


def _write_events(conn, round_id, merged, stamp):
    """本轮结论逐条落台账：可信删除写 tombstone+证据，其余写未核验/不可访问。

    同一轮次复跑先清自己名下的行（``round_id`` 相同），既不重复堆同一条结论，也不动
    别的轮次已经写下的证据。
    """
    conn.execute("DELETE FROM update_events WHERE round_id = ?", (round_id,))
    for item in merged:
        for record in item["deletion_records"]:
            conn.execute("INSERT INTO update_events (round_id, view, identity, message_id,"
                         " kind, reason, basis, evidence, detail, recorded_at)"
                         " VALUES (?, ?, ?, ?, 'deleted', ?, ?, ?, ?, ?)",
                         (round_id, VIEW, item["identity"], record["message_id"],
                          record["basis"], record["basis"],
                          json.dumps(record.get("evidence"), ensure_ascii=False),
                          record.get("detail"), stamp))
        for finding in item["findings"]:
            conn.execute("INSERT INTO update_events (round_id, view, identity, message_id,"
                         " kind, reason, basis, evidence, detail, recorded_at)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (round_id, VIEW, item["identity"], finding["message_id"],
                          finding["kind"], finding["reason"], finding.get("basis"),
                          json.dumps(finding.get("evidence"), ensure_ascii=False)
                          if finding.get("evidence") is not None else None,
                          finding.get("detail"), stamp))


def _commit_transaction(database, plan, entries, identities, round_id, started_at, batches,
                        events, failures):
    """一次事务里合并、发布并写台账；任何一步失败都整体回滚，旧视图原样可用。"""
    conn = catalog.connect(database)
    try:
        catalog.ensure_schema(conn)
        catalog.begin(conn)
        for name, body in _DDL.items():
            conn.execute("CREATE TABLE IF NOT EXISTS %s (%s)" % (name, body))
        merged = [_merge_chat(conn, plan, entry, batches, events, failures) for entry in entries]
        published = catalog.publish_staged(conn, VIEW)
        _write_events(conn, round_id, merged, str(plan.get("planned_at") or _now()))
        conn.execute("INSERT OR REPLACE INTO update_rounds (round_id, identities, status,"
                     " started_at, committed_at, upper_bound, recheck_time_lower,"
                     " recheck_time_upper, recheck_ids, blocking_reasons, report)"
                     " VALUES (?, ?, 'pending', ?, NULL, ?, ?, ?, ?, ?, '{}')",
                     (round_id, json.dumps(identities, ensure_ascii=False), started_at,
                      _max([item["upper_bound"] for item in merged]),
                      _min([item["lookback"]["time_range"]["lower"] for item in merged]),
                      _max([item["lookback"]["time_range"]["upper"] for item in merged]),
                      json.dumps([item["recheck"]["requested_ids"] for item in merged],
                                 ensure_ascii=False),
                      json.dumps([reason for item in merged
                                  for reason in item["blocking_reasons"]], ensure_ascii=False)))
        catalog.commit(conn)
    except Exception:
        catalog.abort(conn)
        raise
    finally:
        conn.close()
    for item in merged:
        item["published"] = published
    return merged


def _write_state_rows(conn, round_id, merged, advanced, stamp):
    for item in merged:
        conn.execute("INSERT INTO update_state (identity, watermark, watermark_basis,"
                     " local_max_message_id, online_coverage, online_complete, credential,"
                     " round_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                     " ON CONFLICT(identity) DO UPDATE SET"
                     " watermark = CASE WHEN excluded.online_complete = 1"
                     "                    AND excluded.watermark IS NOT NULL"
                     "              THEN excluded.watermark ELSE update_state.watermark END,"
                     " watermark_basis = excluded.watermark_basis,"
                     " local_max_message_id = excluded.local_max_message_id,"
                     " online_coverage = excluded.online_coverage,"
                     " online_complete = CASE WHEN excluded.online_complete = 1"
                     "              THEN 1 ELSE update_state.online_complete END,"
                     " credential = excluded.credential, round_id = excluded.round_id,"
                     " updated_at = excluded.updated_at",
                     (item["identity"], item["after"] if advanced else item["before"],
                      "水位推进到本轮冻结上界 %s（追加、回查、媒体与派生版本全部通过）"
                      % item["upper_bound"] if advanced else
                      "水位保持 %s：%s" % (item["before"],
                                        "；".join(item["blocking_reasons"])
                                        or "本轮未全部通过"),
                      item["local_max_message_id"], item["online_coverage"],
                      1 if advanced else 0, item["credential"], round_id, stamp))


def _finalize_state(database, round_id, merged, advanced, blocking, stamp, report):
    """第二个事务：写每身份的水位与本轮结论（版本切换结果已并入 blocking/report）。"""
    conn = catalog.connect(database)
    try:
        catalog.begin(conn)
        _write_state_rows(conn, round_id, merged, advanced, _now(stamp))
        conn.execute("UPDATE update_rounds SET status = ?, committed_at = ?,"
                     " blocking_reasons = ?, report = ? WHERE round_id = ?",
                     ("complete" if advanced else "partial",
                      _now(stamp) if advanced else None,
                      json.dumps(blocking, ensure_ascii=False),
                      json.dumps(report, ensure_ascii=False), round_id))
        catalog.commit(conn)
    except Exception:
        catalog.abort(conn)
        raise
    finally:
        conn.close()


def _handle_versions(database, plan, round_id, entries, merged):
    outputs = plan.get("outputs")
    if not outputs:
        return {"required": False, "accepted": True, "switched": False,
                "reason": "本轮未选定派生版本输出：未构建也未切换指针（这不等于阅读版已验证）",
                "candidate": None, "current": None, "previous": None}
    root = Path(str(outputs["root"]))
    candidate = root / "versions" / ("%s-%s" % (round_id[:16],
                                                _digest(round_id,
                                                        [item["identity"] for item in entries])
                                                [:12]))
    builder = outputs.get("builder") or index_version_builder(
        [item["merged_archive"] for item in merged])
    try:
        built = builder(candidate)
    except Exception as error:
        return {"required": True, "accepted": False, "switched": False,
                "reason": "新版本构建中断（%s: %s）：不切换当前版本指针，旧视图保持可读；"
                          "候选目录 %s 作为待恢复批次保留，同一轮次复跑即可续上"
                          % (type(error).__name__, error, candidate.as_posix()),
                "candidate": str(candidate), "current": None, "previous": None}
    ok, reason = _verify_build(candidate, built)
    if not ok:
        return {"required": True, "accepted": False, "switched": False,
                "reason": "新版本未通过验证（%s）：不切换当前版本指针，旧视图保持可读" % reason,
                "candidate": str(candidate), "current": None, "previous": None}
    record = {"round_id": round_id, "directory": candidate.as_posix(),
              "files": list(built.get("files")), "switched_at": _now(),
              "verify": "构建报告申报的构件已在候选目录逐条核对存在且非空"}
    payload = _switch_pointer(root, record)
    return {"required": True, "accepted": True, "switched": True, "reason": None,
            "candidate": str(candidate), "current": record, "previous": payload.get("previous")}


def _build_report(plan, entries, merged, round_id, started_at, advanced, blocking, versions,
                  stamp):
    totals = {"appended": 0, "edited": 0, "unchanged": 0, "duplicate_ids": 0, "deleted": 0,
              "unverified": 0, "inaccessible": 0, "append_only_skipped_edit": 0,
              "older_unverified": 0, "rows_staged": 0}
    findings, recheck = [], []
    for item in merged:
        for key, value in item["counts"].items():
            totals[key] = totals.get(key, 0) + value
        findings.extend(dict(finding, identity=item["identity"]) for finding in item["findings"])
        recheck.append(dict(item["recheck"], identity=item["identity"]))
    first = merged[0]
    published = first.get("published") or {}
    media = plan.get("media") or {}
    accepted_media = not media.get("selected") or str(media.get("status")) == "complete"
    after_values = [item["after"] if advanced else item["before"] for item in merged]
    aggregate = after_values[0] if len(set(after_values)) == 1 else None
    return {
        "kind": REPORT_KIND, "version": VERSION, "round_id": round_id,
        "status": "complete" if advanced else "partial",
        "committed_at": _now(stamp) if advanced else None, "views": [VIEW],
        "mode": first["mode"],
        "mode_reason": first["mode_reason"] if len(merged) == 1 else "；".join(
            "%s：%s" % (item["identity"], item["mode_reason"]) for item in merged),
        "chats": [{"identity": item["identity"], "mode": item["mode"],
                   "new_upper_bound": item["upper_bound"], "previous_watermark": item["before"],
                   "committed_watermark": item["after"] if advanced else item["before"],
                   "counts": item["counts"], "blocking_reasons": item["blocking_reasons"],
                   "mode_reason": item["mode_reason"],
                   "recheck": item["recheck"], "append": entry["append"],
                   "lookback": item["lookback"]}
                  for item, entry in zip(merged, entries)],
        "append": [{"identity": item["identity"], **entry["append"]}
                   for item, entry in zip(merged, entries)],
        "recheck": first["recheck"] if len(merged) == 1 else {"by_chat": recheck},
        "recheck_by_chat": recheck, "findings": findings,
        "deletions": [{"identity": item["identity"], "message_ids": item["deleted"],
                       "records": item["deletion_records"]} for item in merged if item["deleted"]],
        "counts": dict(totals, index_rows=published.get("messages", 0),
                       revisions_added=published.get("revisions_added", 0),
                       duplicate_rows_ignored=published.get("duplicate_rows_ignored", 0)),
        "watermark": {"before": _min([item["before"] for item in merged]),
                      "after": aggregate if aggregate is not None else first["after"],
                      "advanced": bool(advanced),
                      "reason": first["watermark_reason"] if len(merged) == 1 else
                                "；".join(item["watermark_reason"] for item in merged),
                      "by_identity": {item["identity"]: (item["after"] if advanced
                                                         else item["before"])
                                      for item in merged}},
        "media": dict(media, accepted_complete=accepted_media),
        "versions": {"switched": bool(versions["switched"]), "required": versions["required"],
                     "accepted": versions["accepted"], "current": versions["current"],
                     "previous": versions["previous"], "candidate": versions["candidate"],
                     "reason": versions["reason"]},
        "blocking_reasons": blocking,
        "lock": {"kind": "archive", "root": str(plan.get("lock_root")),
                 "implementation": "state.acquire_archive_lock（全仓仅此一份归档锁实现）"},
        "transaction": {"interface": "catalog.connect/ensure_schema/begin/stage_archive/"
                                     "publish_staged/commit/abort", "published": published},
        "claims": {"full_history_sync": False, "max_id_proves_completeness": False,
                   "raw_archive_deleted": False, "history_rewritten": False,
                   "tombstone_requires_evidence": True, "missing_ids_are_not_deletions": True,
                   "failure_advances_watermark": False,
                   "online_full_export_verified": bool(advanced), "network_used": False,
                   "login_used": False, "tdl_invoked": False,
                   "real_account_verification": NOT_RUN,
                   "note": "本轮状态只描述本地台账与派生层；真实账号、真实在线导出与真实"
                           "媒体下载一律 NOT_RUN"},
        "limitations": {"lookback": "回查是有界窗口，窗口外的编辑未核验，逐条列在 findings",
                        "reading_view": "阅读版那一层的成员变化由派生版本构建器负责，"
                                        "本函数只提交规范层（raw）",
                        "watermark": "水位只在带在线完成凭据且整轮通过时推进",
                        "media": "媒体未验收完成时整轮不报 complete"},
    }


# ---------------------------------------------------------------- CLI 入口

def _read_json(path, label):
    target = Path(str(path))
    _require(target.is_file(), "%s 指向的文件不存在：%s" % (label, target))
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError("%s 不是合法 JSON（%s）：%s" % (label, type(error).__name__,
                                                       target.name)) from None


def _load_fetched(path):
    payload = _read_json(path, "抓取结果")
    records = payload.get("fetched") if isinstance(payload, dict) else payload
    _require(isinstance(records, list), "抓取结果必须是列表（或含 fetched 列表的映射），收到 %s"
             % type(records).__name__)
    out = []
    for item in records:
        _require(isinstance(item, dict), "抓取结果有非映射项：%r" % (item,))
        if item.get("kind") == ARCHIVE_KIND and item.get("archive_path") and \
                not item.get("messages") and Path(str(item["archive_path"])).is_file():
            item = _read_json(item["archive_path"], "归档")
        out.append(item)
    return out


def build_parser():
    import argparse
    parser = argparse.ArgumentParser(prog="updating.py",
                                     description="Telegram 归档增量更新（本地调度与状态处理）")
    parser.add_argument("command", nargs="?", help="子命令（本模块只实现 update）")
    parser.add_argument("--manifest", help="已验收覆盖清单 JSON（必需）")
    parser.add_argument("--probe", help="本轮各聊天上界探测值 JSON（必需）")
    parser.add_argument("--fetched", help="本轮抓取结果 JSON（必需；假后端亦可）")
    parser.add_argument("--database", help="本地索引库路径（必需）")
    parser.add_argument("--lock-root", dest="lock_root",
                        help="归档锁根（默认取索引库所在目录；与导出/导入/检索同源才互斥）")
    parser.add_argument("--out", help="把本轮报告写到该 JSON 文件")
    parser.add_argument("--lookback-days", dest="lookback_days", type=int,
                        help="回查天数（默认 %d，必须有界）" % DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--recheck-since", dest="recheck_since",
                        help="显式替换回查起点（UTC 秒或带时区 ISO）")
    parser.add_argument("--recheck-until", dest="recheck_until",
                        help="显式替换回查终点（UTC 秒或带时区 ISO）")
    parser.add_argument("--resume-round", dest="resume_round",
                        help="上一轮计划 JSON：沿用其冻结上界，不随重跑漂移")
    return parser


def main(argv=None):
    """update 的组合入口（archive.py 空闲时由总控一行注册，见交付报告）。"""
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as signal:
        return EXIT_REJECTED if signal.code not in (0, None) else EXIT_COMPLETE
    if args.command not in (None, "update"):
        print("本模块只有 update 一个动作，收到：%s" % args.command)
        return EXIT_REJECTED
    missing = [name for name, value in (("--manifest", args.manifest), ("--probe", args.probe),
                                        ("--fetched", args.fetched),
                                        ("--database", args.database)) if not value]
    if missing:
        print("update 缺少必需参数：%s（--manifest/--probe/--fetched/--database 都要显式给出，"
              "不默认、不猜）" % "、".join(missing))
        return EXIT_REJECTED
    options = {}
    for key, value in (("lock_root", args.lock_root), ("lookback_days", args.lookback_days),
                       ("recheck_since", args.recheck_since), ("recheck_until", args.recheck_until),
                       ("resume_round", args.resume_round)):
        if value is not None:
            options[key] = _read_json(value, "上一轮计划") if key == "resume_round" else value
    try:
        manifest = apply_committed_watermarks(_read_json(args.manifest, "覆盖清单"),
                                              args.database)
        payload = plan_update(manifest, _read_json(args.probe, "上界探测值"), options)
        report = commit_update(Path(args.database), payload, _load_fetched(args.fetched))
    except state.LockBusy as busy:
        print("update 未能开始：同一归档的归档锁被占用（%s）；增量更新与导出/导入/检索按归档锁"
              "互斥，请等对方结束后再试，按能力缺口退出码 3" % busy)
        return EXIT_CAPABILITY
    except catalog.CatalogError as error:
        print("update 未能完成：本地索引不可用（%s）。原始归档未被改动、仍可直接读取；"
              "索引是派生层，可按用户选定的范围从原档重建，按能力缺口退出码 3" % error)
        return EXIT_CAPABILITY
    except (ValueError, OSError) as error:
        print("update 被拒绝：%s" % error)
        return EXIT_REJECTED
    recheck = report["recheck"]
    print("本轮%s" % recheck.get("statement", "按聊天分别回查，详见报告"))
    print("已提交水位：%s（本轮%s）" % (report["watermark"]["after"],
                                     "已推进" if report["watermark"]["advanced"] else "未推进"))
    print("未核验/不可访问：未核验 %d 条、不可访问 %d 条、删除标记 %d 条"
          % (report["counts"]["unverified"], report["counts"]["inaccessible"],
             report["counts"]["deleted"]))
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    return EXIT_COMPLETE if report["status"] == "complete" else EXIT_INCOMPLETE
