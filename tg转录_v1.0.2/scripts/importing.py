"""离线源适配 + 共同归档模型（Task 5）。

把用户**明确选定**的本地导出（tdl JSON / Telegram Desktop ``result.json``）归一化成
同一份可重建派生归档，供 Task 6 检索、Task 7 分享、Task 8 增量共用。

边界（与工单一致）：
* 纯离线：不登录、不联网、不启动任何子进程、不执行 tdl；``import`` 没有
  tdl / storage / namespace 也必须完成（合同第 86 行）。
* 原始文件只读：处理前后各取一次 SHA-256 并核对，绝不改写用户导出。
* 这里产出的是**用户导入的本地快照**，``coverage.online_coverage`` 恒为
  ``unverified``；没有在线覆盖凭据不得标记在线全量完成，也不能据此跳过在线导出。

结构依据（Desktop 侧字段拼写只认公开文档列出的键，文档沉默的一律申报缺口）：
* ``https://core.telegram.org/import-export``（Telegram Data Export Schema，2026-10 读取）：
  聊天对象列出 ``id`` / ``name`` / ``type`` / ``messages``；消息对象列出 ``id``、``type``
  （"The type is message for regular messages and service for service messages"）、
  ``action``、``date``（"Message date as an ISO 8601 timestamp"）、``date_unixtime``
  （"Message date as a unix timestamp"）、``from`` / ``from_id``、``reply_to_message_id``、
  ``forwarded_from``、``text`` / ``text_entities``（"String if there are no entities" /
  "array of [MessageEntity] in the order they appear"）、``photo`` / ``file`` /
  ``thumbnail`` / ``width`` / ``height`` / ``media_type`` / ``mime_type`` /
  ``duration_seconds`` / ``title`` 等。实体 ``type`` 取值集合也取自该页
  （unknown/mention/hashtag/bot_command/link/email/bold/italic/code/pre/plain/
  text_link/mention_name/phone/cashtag/underline/strikethrough/blockquote/bank_card/
  spoiler/custom_emoji）。
* tdl 侧**没有**可指认的公开 JSON 字段表（Task 3 报告已记 ``raw_json_type_names`` 未核对），
  因此本适配器只吃仓库既有代码点名的键：``state.MESSAGE_ID_KEYS`` /
  ``state.DECLARED_CHAT_KEYS``（state.py）、顶层身份 ``state.TDL_TOP_ID_KEYS``
  （Task 6 真实反例：活体 tdl 导出顶层用 ``id`` 声明 chat 身份；消息层身份仍只认
  ``state.DECLARED_CHAT_KEYS``，消息自带 ``id`` 是消息 ID）、``reading.ID_KEYS`` / ``DATE_KEYS`` /
  ``TEXT_KEYS`` / ``SENDER_KEYS`` / ``FILE_KEYS``（reading.py:72-76）、
  ``media.CLASSIFICATION_INPUT_FIELDS``（media.py:77-78）。其余键一律进
  ``gaps.unknown_fields``，不静默丢弃、也不猜含义。
* ``date`` 那条 ISO 串官方 schema 未标注是本地时间还是 UTC → 只原样存进
  ``display_time``，UTC 秒一律取 ``date_unixtime``；tdl 侧非数值的日期不参与 UTC 计算。

内存边界：``iter_normalized_sources`` 一次只产出一个聊天的归档，
``import_sources`` 处理完一个聊天就立刻写入派生存储再取下一个（一次只处理单来源单聊天）。
"""

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePath

import media
import reading
import state
from job import PEER_TYPES, chat_identity, validate_job

ARCHIVE_KIND = "telegram-archive"
ARCHIVE_VERSION = 1
BATCH_KIND = "telegram-archive-import-batches"
BATCH_VERSION = 1
REPORT_KIND = "telegram-archive-import-report"
DESKTOP_DOC_URL = "https://core.telegram.org/import-export"
SUPPORTED_FORMATS = ("tdl_json", "desktop_json")
DERIVED_SUBDIR = "derived"
MANIFEST_NAME = "imported.json"
ONLINE_COVERAGE = "unverified"
LOCAL_SNAPSHOT_CLAIM = "用户导入的本地快照，在线覆盖未验证"

# Desktop 官方 schema 已列出的消息键（原页摘录；只用于判定「这键有没有出处」）。
DESKTOP_MESSAGE_KEYS = frozenset((
    "id", "type", "action", "date", "date_unixtime", "from", "from_id", "edited",
    "edited_unixtime", "reply_to_message_id", "text", "text_entities", "members", "actor",
    "actor_id", "photo", "width", "height", "file", "thumbnail", "title", "inviter",
    "message_id", "game_message_id", "score", "amount", "currency", "invoice_message_id",
    "recurring", "duration_seconds", "discard_reason", "information_text", "reason_app_id",
    "reason_app_name", "reason_domain", "values", "to_id", "to", "distance", "period",
    "schedule_date", "emoticon", "cost", "months", "new_title", "new_icon_emoji_id",
    "button_id", "peer_id", "author", "forwarded_from", "saved_from", "via_bot",
    "media_type", "performer", "mime_type", "contact_information", "contact_vcard",
    "location_information", "place_name", "address", "game_title", "game_description",
    "game_link", "invoice_information", "poll", "self_destruct_period_seconds",
    # 2026-10 复核该页再补六个已列出键（此前会被误报成「文档未列出」）：
    "live_location_period_seconds", "gift_code", "boost_peer_id", "unclaimed",
    "via_giveaway", "giveaway_information",
))
# Desktop 已列出但本模块**不**建立映射的键（正文/身份之外的服务消息细节）：显式申报，不猜。
DESKTOP_UNMAPPED_KEYS = frozenset((
    "edited", "edited_unixtime", "members", "actor", "actor_id", "inviter", "message_id",
    "game_message_id", "score", "amount", "currency", "invoice_message_id", "recurring",
    "discard_reason", "information_text", "reason_app_id", "reason_app_name",
    "reason_domain", "values", "to_id", "to", "distance", "period", "schedule_date",
    "emoticon", "cost", "months", "new_title", "new_icon_emoji_id", "button_id",
    "peer_id", "saved_from", "via_bot", "performer", "author", "contact_information",
    "contact_vcard", "location_information", "place_name", "address", "game_title",
    "game_description", "game_link", "invoice_information", "poll",
    "self_destruct_period_seconds",
    # 2026-10 复核补录：官方 schema 列出但本模块不建立映射的键（申报 documented: true）：
    "live_location_period_seconds", "gift_code", "boost_peer_id", "unclaimed",
    "via_giveaway", "giveaway_information",
))
DESKTOP_CHAT_KEYS = frozenset(("id", "name", "type", "messages"))
# 实体 type 取值集合（同一页 MessageEntity 段）；不在表内的一律进 unknown_entities。
DESKTOP_ENTITY_TYPES = frozenset((
    "unknown", "mention", "hashtag", "bot_command", "link", "email", "bold", "italic",
    "code", "pre", "plain", "text_link", "mention_name", "phone", "cashtag", "underline",
    "strikethrough", "blockquote", "bank_card", "spoiler", "custom_emoji",
))
# 聊天 type 取值 -> 归档 peer 类型（同页列出的取值；未知取值要求用户补 metadata，不猜）。
DESKTOP_TYPE_TO_PEER = {
    "personal_chat": "user", "saved_messages": "user", "replies": "user",
    "bot_chat": "user", "private_group": "chat", "private_supergroup": "channel",
    "public_supergroup": "channel", "private_channel": "channel",
    "public_channel": "channel",
}
# tdl 侧本模块会消费的键：仓库既有代码点名过的键集（见模块 docstring 的行号出处）。
# Task 6：顶层身份另认 state.TDL_TOP_ID_KEYS（含真实 tdl 导出的顶层 id），消息层
# 身份仍只认 state.DECLARED_CHAT_KEYS（消息自带 id 是消息 ID，不可误判）。
TDL_CONSUMED_KEYS = frozenset(set(state.MESSAGE_ID_KEYS) | set(state.DECLARED_CHAT_KEYS)
                              | set(state.TDL_TOP_ID_KEYS)
                              | set(reading.ID_KEYS) | set(reading.DATE_KEYS)
                              | set(reading.TEXT_KEYS) | set(reading.SENDER_KEYS)
                              | set(reading.FILE_KEYS)
                              | set(media.CLASSIFICATION_INPUT_FIELDS)
                              | {"reply_to_message_id"})
# 附件描述里可以原样带走的 tdl 侧节点（分类判据的输入，media.py 已点名）
ATTACHMENT_NODE_KEYS = tuple(media.CLASSIFICATION_INPUT_FIELDS)
MISSING = "missing"

LIMITATIONS = {
    "local_snapshot_only": "本地导入只覆盖用户选定文件里已有的消息，不证明在线全量历史",
    "online_coverage": "在线覆盖一律 unverified：导入不产生在线完成凭据，不能据此跳过导出",
    "identity_from_metadata_or_source_only":
        "聊天身份只取源里已声明的 chat ID 与用户给的 metadata，绝不从标题/文件名/正文猜",
    "desktop_date_timezone_unknown":
        "Desktop 的 date 是未标时区的 ISO 串（官方 schema 沉默），只作展示串保留",
    "tdl_field_spelling_unverified":
        "tdl 导出 JSON 的真实字段拼写未核对（Task 3 已记 insufficient_source）："
        "本适配器只吃仓库既有代码点名的键，其余全部申报为缺口，不静默丢弃",
    "stdlib_single_file": "标准库无流式 JSON：单文件整体读入后逐聊天出栈，一次只装一个聊天",
    "desktop_media_type_values_undocumented":
        "Desktop media_type 的取值集合官方 schema 未列出，故类别只认 photo 字段与 "
        "mime_type 前缀（IANA 顶级类型），判不出来的一律 unknown",
}


# ---------------------------------------------------------------- 通用小工具

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _norm(name):
    return re.sub(r"[_\s]", "", str(name)).lower()


# 评审 I-4：真实超级群导出是 10^5-10^6 条量级，这些「按键集归一化」的表只与常量有关，
# 因此只在模块加载时建一次；旧实现每条消息的每个键都重建一遍（_norm 约 490 次/条）。
TDL_CONSUMED_NORM_KEYS = frozenset(_norm(key) for key in TDL_CONSUMED_KEYS)
# Task 6：顶层 id 是已点名的身份键（state.TDL_TOP_ID_KEYS），不是未知缺口；
# 消息层 id 本就经 MESSAGE_ID_KEYS 消费，两层都不报 unknown_fields。
TDL_UNDECLARED_CHAT_KEYS = frozenset(set(state.TDL_TOP_ID_KEYS) | {"messages"})
ATTACHMENT_NODE_NORM_KEYS = frozenset(_norm(key) for key in ATTACHMENT_NODE_KEYS)
# Desktop 侧本模块已消费的消息键（官方 schema 的拼写，逐字对应 _normalize_desktop_one 与
# _attach_desktop 的读取点）。评审 I-4：这张表原来每条消息都重建成一个元字面量，现在模块级
# 只建一次；它与 DESKTOP_MESSAGE_KEYS 是两件事——后者是「文档列出」，前者是「本模块用了」。
DESKTOP_CONSUMED_KEYS = frozenset((
    "text", "text_entities", "date", "date_unixtime", "from", "from_id",
    "reply_to_message_id", "forwarded_from", "photo", "file", "thumbnail", "width",
    "height", "mime_type", "media_type", "duration_seconds", "title", "id",
))


def _norm_table(message):
    """一条消息的「归一化键名 -> 原拼写」表：每条消息只建一次，供本次所有取值复用。"""
    table = {}
    for name in message:
        table.setdefault(_norm(name), name)
    return table


def _lookup(message, keys, table=None):
    """按归一化键名取值（sender_name / senderName 视为同一字段，与 reading 同口径）。

    ``table`` 是调用方为这条消息预先算好的归一化键表（见 :func:`_norm_table`）；不给就
    现场算一次，语义完全一致，只是热路径不该反复重建。
    """
    if table is None:
        table = _norm_table(message)
    for key in keys:
        hit = table.get(_norm(key))
        if hit is not None:
            return message[hit], hit
    return None, None


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _positive_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 \
        else None


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _absolute(value, label):
    _require(value is not None, "%s 缺失" % label)
    path = Path(str(value))
    _require(path.is_absolute(), "%s 必须是绝对路径（不读写磁盘之外的东西）：%s" % (label, value))
    return path


def _batch_id(source_digest, identity):
    return _digest(("%s|%s|%d" % (source_digest, identity, ARCHIVE_VERSION)).encode())[:16]


# ---------------------------------------------------------------- 源载入（只读）

def _load(path, source_format):
    """读源文件并核对前后摘要：原档只读，任何一次导入都留下可比对的证据。"""
    _require(source_format in SUPPORTED_FORMATS,
             "source_format 不受支持：%r（首期只覆盖 %s；未知来源不猜、不默认）"
             % (source_format, "、".join(SUPPORTED_FORMATS)))
    path = Path(path)
    _require(path.is_file(), "来源文件不存在或不是文件：%s（sources 只能是用户明确选定的"
                             "路径，不自动递归磁盘）" % path.name)
    raw = path.read_bytes()
    digest_before = _digest(raw)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("来源文件 %s 的 JSON 解析失败（%s），本个源单独报告失败，"
                         "其余源继续处理" % (path.name, type(error).__name__))
    return path, payload, digest_before, _digest(path.read_bytes())


# ---------------------------------------------------------------- 归档骨架构造

def _skeleton(source_format, path, digest_before, digest_after, metadata):
    """归档骨架：身份必须由调用方先解析好（源声明 + 用户 metadata），这里只复核。"""
    peer_type = metadata.get("peer_type")
    chat_id = _positive_int(metadata.get("chat_id"))
    _require(peer_type in PEER_TYPES and chat_id is not None,
             "缺少聊天身份：请补 metadata.peer_type（必须是 %s 之一）与 metadata.chat_id"
             "（正整数 chat ID）。归档身份 = peer 类型 + chat ID + 消息 ID，"
             "绝不从聊天标题、文件名或正文猜身份" % ("/".join(PEER_TYPES)))
    identity = "%s:%s" % (peer_type, chat_id)
    timezone_name = _text(metadata.get("timezone")) or "unknown"
    metadata = dict(metadata, peer_type=peer_type, chat_id=chat_id)
    return {
        "kind": ARCHIVE_KIND, "version": ARCHIVE_VERSION,
        "identity": identity, "peer_type": peer_type, "chat_id": metadata["chat_id"],
        "chat": {"peer_type": peer_type, "id": metadata["chat_id"],
                 "display_name": _text(metadata.get("display_name")),
                 "display_name_basis": "metadata" if _text(metadata.get("display_name"))
                 else MISSING,
                 "locator": _text(metadata.get("locator"))},
        "identity_basis": {"peer_type": "metadata", "chat_id": None,
                           "guessed_from_name_or_title": False},
        "source": {"format": source_format, "adapter": source_format, "path": str(path),
                   "name": path.name, "sha256_before": digest_before,
                   "sha256_after": digest_after,
                   "read_only_verified": digest_before == digest_after,
                   "batch_id": None, "read_at": _now(), "size": path.stat().st_size},
        "time": {"utc_field": None, "source_time_basis": None,
                 "display_timezone": timezone_name},
        "coverage": {"message_count": 0, "id_lower": None, "id_upper": None,
                     "date_lower": None, "date_upper": None,
                     "online_coverage": ONLINE_COVERAGE, "online_complete": False,
                     "online_completion_credential": None,
                     "cannot_replace_online_export": True,
                     "claim": LOCAL_SNAPSHOT_CLAIM},
        "messages": [],
        "gaps": {"unknown_fields": [], "unknown_entities": [], "missing_fields": [],
                 "text_mismatch": [],
                 "counts": {"sender": 0, "time": 0, "text": 0, "reply_to": 0,
                            "attachments": 0, "text_mismatch": 0}, "notes": []},
        "claims": {"local_snapshot_only": True, "online_full_export_verified": False,
                   "online_coverage": ONLINE_COVERAGE, "claim": LOCAL_SNAPSHOT_CLAIM,
                   "read_only_source": True, "network_used": False, "login_used": False,
                   "media_downloaded": False},
        "limitations": copy.deepcopy(LIMITATIONS),
    }


def _finish(doc, messages, ids, utc_seconds, peer_type_source, chat_id_source):
    doc["messages"] = messages
    doc["identity_basis"]["peer_type"] = peer_type_source
    doc["identity_basis"]["chat_id"] = chat_id_source
    doc["source"]["batch_id"] = _batch_id(doc["source"]["sha256_before"], doc["identity"])
    doc["coverage"].update({
        "message_count": len(messages),
        "id_lower": min(ids) if ids else None,
        "id_upper": max(ids) if ids else None,
        "date_lower": min(utc_seconds) if utc_seconds else None,
        "date_upper": max(utc_seconds) if utc_seconds else None})
    counts = doc["gaps"]["counts"]
    if counts["sender"]:
        doc["gaps"]["missing_fields"].append("sender")
    if counts["time"]:
        doc["gaps"]["missing_fields"].append("time")
    if counts["text"]:
        doc["gaps"]["missing_fields"].append("text")
    if counts["reply_to"]:
        doc["gaps"]["missing_fields"].append("reply_to")
    if counts["attachments"]:
        doc["gaps"]["missing_fields"].append("attachments")
    doc["gaps"]["missing_fields"].sort()
    return doc


def _declare_unknown_field(doc, entry):
    gaps = doc["gaps"]["unknown_fields"]
    for item in gaps:
        if item["field"] == entry["field"] and item["scope"] == entry["scope"]:
            if entry["message_id"] not in item["message_ids"]:
                item["message_ids"].append(entry["message_id"])
            return
    gaps.append({"field": entry["field"], "scope": entry["scope"],
                 "source_format": doc["source"]["format"],
                 "documented": entry.get("documented", False),
                 "message_ids": [entry["message_id"]] if entry["message_id"] else [],
                 "note": entry.get("note", "")})


def _base_message(doc, message_id, position):
    return {"message_id": message_id,
            "identity_key": "%s#%d" % (doc["identity"], message_id),
            "date_utc": None, "date_utc_basis": None, "display_time": None,
            "time_missing": True, "text": "", "text_missing": True,
            "sender": {"name": None, "id": None, "missing": True, "basis": MISSING},
            "reply_to": {"message_id": None, "missing": True},
            "forwarded_from": {"name": None, "missing": True},
            "attachments": [], "attachments_missing": True,
            "service": False, "action": None, "gaps": [],
            "source_ref": {"batch_id": None, "path": doc["source"]["path"],
                           "index": position, "format": doc["source"]["format"]}}


def _mark_missing(doc, item, key):
    doc["gaps"]["counts"][key] += 1
    if key not in item["gaps"]:
        item["gaps"].append(key)


# ---------------------------------------------------------------- tdl 适配器

def _tdl_shape(payload):
    """tdl 侧只认两种已在 state.py 里定死的形状：消息数组 / 含 messages 数组的映射。

    含 messages 数组的顶层映射的身份键走 state.TDL_TOP_ID_KEYS（含真实 tdl
    导出的顶层 id；Task 6）；逐条消息的身份仍只认 state.DECLARED_CHAT_KEYS。
    返回 (消息列表, 来源声明的 chat ID)。未知结构一律拒绝，不默认为空聊天。
    """
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        declared = next((payload[name] for name in state.TDL_TOP_ID_KEYS
                         if payload.get(name) is not None), None)
        return payload["messages"], declared
    observed = "、".join(sorted(str(key) for key in payload)[:8]) \
        if isinstance(payload, dict) else type(payload).__name__
    raise ValueError("未知的 tdl 导出结构（不猜测、不默认为空聊天）：需要消息数组或含 "
                     "messages 数组的映射，实际看到：%s" % observed)


def _normalize_tdl(payload, path, metadata, digests, wanted=None):
    raw_messages, declared = _tdl_shape(payload)
    declared = _positive_int(declared)
    peer_type = metadata.get("peer_type")
    _require(peer_type in PEER_TYPES,
             "缺少聊天身份：请补 metadata.peer_type（必须是 %s 之一）。归档身份是 peer 类型 "
             "+ chat ID + 消息 ID，绝不从聊天标题、文件名或正文猜身份"
             % "/".join(PEER_TYPES))
    chat_id = wanted or declared
    _require(chat_id is not None,
             "缺少聊天身份：来源没有声明 chat ID，请补 metadata.chat_id（正整数），"
             "不把文件名当身份")
    if wanted is not None and declared is not None:
        _require(wanted == declared,
                 "来源声明的 chat ID %s 与 metadata.chat_id %s 不符（不代为改写身份）"
                 % (declared, wanted))
    doc = _skeleton("tdl_json", path, digests[0], digests[1],
                    dict(metadata, peer_type=peer_type, chat_id=chat_id))
    chat_id_source = "metadata" if wanted is not None else "source"
    for name in (payload if isinstance(payload, dict) else {}):
        if name not in TDL_UNDECLARED_CHAT_KEYS:
            _declare_unknown_field(doc, {"field": name, "scope": "chat", "message_id": None,
                                         "note": "tdl 顶层未点名的键：申报缺口，不使用"})
    messages, ids, utc_seconds = [], [], []
    for position, item in enumerate(raw_messages, start=1):
        _require(isinstance(item, dict), "第 %d 项不是消息映射，无法归一化" % position)
        table = _norm_table(item)          # 每条消息算一次，下面的取值全部复用它（I-4）
        message_id = _positive_int(_lookup(item, state.MESSAGE_ID_KEYS, table)[0])
        _require(message_id is not None,
                 "第 %d 项缺少正整数消息 ID（id 或 message_id）：消息 ID 不编造、不补号"
                 % position)
        row = _base_message(doc, message_id, position)
        raw_date, _ = _lookup(item, reading.DATE_KEYS, table)
        if isinstance(raw_date, (int, float)) and not isinstance(raw_date, bool):
            row["date_utc"] = int(raw_date)
            row["date_utc_basis"] = "epoch_seconds_utc"
            row["time_missing"] = False
            utc_seconds.append(row["date_utc"])
        elif raw_date is not None:
            row["display_time"] = str(raw_date)
            _mark_missing(doc, row, "time")
            doc["gaps"]["notes"].append(
                "第 %d 项 %s 是非数值日期，未核对含义，不猜成 UTC" % (position, raw_date))
        else:
            _mark_missing(doc, row, "time")
        raw_text, _ = _lookup(item, reading.TEXT_KEYS, table)
        if isinstance(raw_text, str) and raw_text.strip():
            row["text"] = raw_text
            row["text_missing"] = False
        elif raw_text is not None:
            _declare_unknown_field(doc, {"field": "text", "scope": "message",
                                         "message_id": message_id,
                                         "note": "正文不是字符串，按缺口申报不猜"})
            _mark_missing(doc, row, "text")
        else:
            _mark_missing(doc, row, "text")
        sender, sender_key = _lookup(item, reading.SENDER_KEYS, table)
        if _text(sender):
            row["sender"] = {"name": _text(sender), "id": None, "missing": False,
                             "basis": "source"}
        else:
            _mark_missing(doc, row, "sender")
        reply = _positive_int(_lookup(item, ("reply_to_message_id",), table)[0])
        if reply is not None:
            row["reply_to"] = {"message_id": reply, "missing": False}
        else:
            _mark_missing(doc, row, "reply_to")
        _attach_tdl(doc, item, row, message_id, table)
        for name in item:
            if _norm(name) not in TDL_CONSUMED_NORM_KEYS:
                _declare_unknown_field(doc, {"field": name, "scope": "message",
                                             "message_id": message_id,
                                             "note": "tdl 侧未点名的键：申报缺口，不使用"})
        messages.append(row)
        ids.append(message_id)
    doc["time"].update({"utc_field": "date", "source_time_basis":
                        "epoch_seconds_utc(数值日期按 UTC 秒处理，出处 reading._moment)"})
    return _finish(doc, messages, ids, utc_seconds, "metadata", chat_id_source)


def _attach_tdl(doc, item, row, message_id, table=None):
    """附件描述：类别一律走 media.classify（判据可指认到 work/upstream 行号）。"""
    category = media.classify(item)
    name, _ = _lookup(item, reading.FILE_KEYS, table)
    has_node = any(_lookup(item, (field,), table)[0] is not None
                   for field in ATTACHMENT_NODE_KEYS)
    if not has_node and not _text(name):
        row["attachments_missing"] = True
        _mark_missing(doc, row, "attachments")
        return
    node = {field: value for field, value in item.items()
            if _norm(field) in ATTACHMENT_NODE_NORM_KEYS}
    row["attachments"].append({
        "category": category, "category_basis": "media.classify", "path": _text(name),
        "mime_type": None, "declared_media_type": None, "width": None, "height": None,
        "duration_seconds": None, "thumbnail": None, "title": None, "node": node,
        "source_field": "tdl"})
    row["attachments_missing"] = False
    if category == media.UNRESOLVED:
        doc["gaps"]["notes"].append("消息 %s 的附件判为 unknown（不默选、不猜类别）"
                                    % message_id)


# ---------------------------------------------------------------- Desktop 适配器

def _desktop_chats(payload, wanted=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("chats"), list):
        observed = "、".join(sorted(str(key) for key in payload)[:8]) \
            if isinstance(payload, dict) else type(payload).__name__
        raise ValueError("未知的 Telegram Desktop 导出结构（不猜测、不默认为空聊天）："
                         "官方 schema 的顶层需含 chats 数组，实际看到：%s" % observed)
    chats = [item for item in payload["chats"] if isinstance(item, dict)]
    _require(len(chats) == len(payload["chats"]) and chats,
             "来源里没有任何聊天（chats 为空或含非映射条目）：不默认为空聊天，也不猜身份")
    if wanted is not None:
        picked = [chat for chat in chats if _positive_int(chat.get("id")) == wanted]
        _require(picked, "metadata.chat_id=%s 在该来源里找不到对应聊天（不代为新建身份）"
                 % wanted)
        return picked
    return chats


def _desktop_peer_type(chat, metadata):
    declared = _text(chat.get("type"))
    mapped = DESKTOP_TYPE_TO_PEER.get(declared)
    given = metadata.get("peer_type")
    _require(mapped is not None or given in PEER_TYPES,
             "缺少聊天身份：来源的 type=%r 不在官方 schema（%s）列出的取值里，请在 "
             "metadata.peer_type 里给出 peer 类型（%s），不从名字猜"
             % (declared, DESKTOP_DOC_URL, "/".join(PEER_TYPES)))
    if mapped is None:
        return str(given), "metadata", None
    if given in PEER_TYPES and given != mapped:
        return str(given), "metadata", (
            "metadata.peer_type=%s 与来源 type=%s 映射出的 %s 不一致：以 metadata 为准并"
            "申报缺口" % (given, declared, mapped))
    return mapped, "source.chats[].type(%s)" % declared, None


def _declare_text_mismatch(doc, message_id, plain, joined, kept):
    """``text`` 纯串与 ``text_entities`` 拼串不一致时的缺口申报（评审 I-2）。

    只落长度与摘要，不落敏感原文；``kept`` 记下正文最终采用了哪一份，让「保住的是能
    保全内容的那份」这件事在归档与报告里都可复核。
    """
    entry = {"message_id": message_id,
             "identity_key": "%s#%d" % (doc["identity"], message_id),
             "text_length": len(plain), "text_entities_length": len(joined),
             "text_sha256": _digest(plain.encode("utf-8"))[:16],
             "text_entities_sha256": _digest(joined.encode("utf-8"))[:16],
             "kept": kept,
             "note": "text 与 text_entities 拼出的正文不一致：以 %s 为准（保住能保全内容的"
                     "那份），另一份未被采用的部分按缺口申报，不静默丢正文" % kept}
    doc["gaps"]["text_mismatch"].append(entry)
    doc["gaps"]["counts"]["text_mismatch"] += 1
    return entry


def _join_desktop_text(doc, raw, entities, message_id):
    """结构化 text 数组按原顺序拼接**实际文本节点**，绝不把实体对象变成字典字符串。

    ``text`` 与 ``text_entities`` 同时存在时（官方 schema：无实体时 text 是字符串，有实体
    时是数组），两者说法不一致就**不整体替换**：留下能保全内容的那一份（拼接结果更长就
    用拼接，等长或更短都以权威纯串为准），并把差异计入 ``gaps``。旧实现直接整体替换，
    遇到未认得的实体节点就会吃掉纯串里的字，正文变短而缺口计数不变（评审 I-2）。
    """
    unknown_types = []

    def join(nodes):
        parts = []
        for node in nodes:
            if isinstance(node, str):
                parts.append(node)
                continue
            if isinstance(node, dict) and isinstance(node.get("text"), str):
                parts.append(node["text"])
                kind = node.get("type")
                if isinstance(kind, str) and kind not in DESKTOP_ENTITY_TYPES:
                    unknown_types.append(kind)
            else:
                doc["gaps"]["notes"].append(
                    "消息 %s 的实体节点形状未认得（%s）：该节点不并入正文"
                    % (message_id, type(node).__name__))
        return "".join(parts)

    if isinstance(raw, list):
        body = join(raw)
    elif isinstance(raw, str):
        body = raw
        if isinstance(entities, list):
            candidate = join(entities)
            if candidate and candidate != raw:
                if len(candidate) > len(raw):
                    _declare_text_mismatch(doc, message_id, raw, candidate,
                                           "text_entities")
                    body = candidate
                else:
                    _declare_text_mismatch(doc, message_id, raw, candidate, "text")
    else:
        return None, unknown_types
    for kind in sorted(set(unknown_types)):
        gaps = doc["gaps"]["unknown_entities"]
        hit = next((entry for entry in gaps if entry["type"] == kind), None)
        if hit is None:
            gaps.append({"type": kind, "message_ids": [message_id],
                         "note": "实体类型不在官方 schema 列出的取值内，文本已保留、类型未猜"})
        elif message_id not in hit["message_ids"]:
            hit["message_ids"].append(message_id)
    return body, unknown_types


def _normalize_desktop_one(chat, path, metadata, digests, chat_id_source, wanted=None):
    declared_id = _positive_int(chat.get("id"))
    _require(declared_id is not None,
             "来源聊天缺少正整数 id（官方 schema 的 chats[].id）：请在 metadata.chat_id 里"
             "补充聊天身份，不把聊天名或文件名当身份")
    if wanted is not None:
        _require(wanted == declared_id,
                 "来源聊天声明的 chat ID %s 与 metadata.chat_id %s 不符（不代为改写身份）"
                 % (declared_id, wanted))
    peer_type, peer_basis, peer_note = _desktop_peer_type(chat, metadata)
    probe = dict(metadata, peer_type=peer_type, chat_id=declared_id)
    doc = _skeleton("desktop_json", path, digests[0], digests[1], probe)
    if peer_note:
        doc["gaps"]["notes"].append(peer_note)
    doc["identity_basis"].update({"peer_type": peer_basis, "chat_id": chat_id_source,
                                  "display_name_never_used_for_identity": True})
    name = _text(metadata.get("display_name")) if wanted is not None else None
    source_name = _text(chat.get("name"))
    doc["chat"]["display_name"] = name or source_name
    doc["chat"]["display_name_basis"] = ("metadata" if name else
                                         "source" if source_name else MISSING)
    doc["source"]["batch_id"] = _batch_id(digests[0], doc["identity"])
    for key in chat:
        if key not in DESKTOP_CHAT_KEYS:
            _declare_unknown_field(doc, {"field": key, "scope": "chat", "message_id": None,
                                         "note": "官方 schema 未列出该聊天键：只申报不使用"})
    messages, ids, utc_seconds = [], [], []
    raw_messages = chat.get("messages")
    _require(isinstance(raw_messages, list),
             "来源聊天缺少 messages 数组（官方 schema 的 chats[].messages）：不猜结构")
    for position, item in enumerate(raw_messages, start=1):
        _require(isinstance(item, dict), "第 %d 项不是消息映射，无法归一化" % position)
        message_id = _positive_int(item.get("id"))
        _require(message_id is not None,
                 "第 %d 项缺少正整数消息 ID（官方 schema 的 id）：消息 ID 不编造、不补号"
                 % position)
        row = _base_message(doc, message_id, position)
        unix = item.get("date_unixtime")
        parsed = _positive_int(int(unix)) if isinstance(unix, (int, str)) and str(unix).isdigit() \
            and int(unix) > 0 else None
        if parsed is not None:
            row["date_utc"] = parsed
            row["date_utc_basis"] = "date_unixtime"
            row["time_missing"] = False
            utc_seconds.append(parsed)
        else:
            _mark_missing(doc, row, "time")
        if isinstance(item.get("date"), str):
            row["display_time"] = item["date"]      # 官方 schema 未标注时区：原样保留
        body, _kinds = _join_desktop_text(doc, item.get("text"), item.get("text_entities"),
                                          message_id)
        if body is not None and body.strip():
            row["text"] = body
            row["text_missing"] = False
        elif body is None:
            _declare_unknown_field(doc, {"field": "text", "scope": "message",
                                         "message_id": message_id, "documented": True,
                                         "note": "正文既不是字符串也不是实体数组：申报不猜"})
            _mark_missing(doc, row, "text")
        else:
            _mark_missing(doc, row, "text")
        sender = _text(item.get("from"))
        if sender:
            row["sender"] = {"name": sender, "id": _text(item.get("from_id")),
                             "missing": False, "basis": "source"}
        else:
            _mark_missing(doc, row, "sender")
        reply = _positive_int(item.get("reply_to_message_id"))
        if reply is not None:
            row["reply_to"] = {"message_id": reply, "missing": False}
        else:
            _mark_missing(doc, row, "reply_to")
        forwarded = _text(item.get("forwarded_from"))
        if forwarded:
            row["forwarded_from"] = {"name": forwarded, "missing": False}
        else:
            row["forwarded_from"]["missing"] = True
        _attach_desktop(doc, item, row, message_id)
        for key in item:
            if key in DESKTOP_CONSUMED_KEYS:      # 模块级只建一次的已消费键集（I-4）
                continue
            if key in DESKTOP_MESSAGE_KEYS:
                if key in DESKTOP_UNMAPPED_KEYS and key != "type" and key != "action":
                    _declare_unknown_field(doc, {"field": key, "scope": "message",
                                                 "message_id": message_id,
                                                 "documented": True,
                                                 "note": "官方 schema 有出处，本模块不建立映射"})
                continue
            _declare_unknown_field(doc, {"field": key, "scope": "message",
                                         "message_id": message_id, "documented": False,
                                         "note": "官方 schema 未列出该字段：只申报不使用"})
        if item.get("type") == "service":
            row["service"] = True
        if isinstance(item.get("action"), str):
            row["action"] = item["action"]
        messages.append(row)
        ids.append(message_id)
    doc["time"].update({"utc_field": "date_unixtime",
                        "source_time_basis": "date_unixtime(官方 schema：unix timestamp)"})
    return _finish(doc, messages, ids, utc_seconds, peer_basis, chat_id_source)


def _category_from_desktop(item, photo, file_path):
    """类别判据：photo 字段（官方 schema 的照片路径）或 mime_type 顶级类型；否则 unknown。

    Desktop 的 media_type 取值集合官方 schema 未公开（见 LIMITATIONS），因此**不**按它猜。
    """
    if photo:
        return "image", "desktop.photo_field"
    mime = _text(item.get("mime_type"))
    if mime:
        top, _, sub = mime.partition("/")
        top = top.strip().lower()
        table = {"image": "image", "video": "video", "audio": "audio"}
        if top in table:
            if top == "audio" and _norm(sub) in ("ogg", "oga", "opus"):
                return table[top], "desktop.mime_prefix(voice 需 TL 旗标，这里不猜)"
            return table[top], "desktop.mime_prefix"
        if top == "application":
            return "document", "desktop.mime_prefix"
    return media.UNRESOLVED, "unresolved:无 photo 字段且 mime_type 缺失或不认识"


def _attach_desktop(doc, item, row, message_id):
    photo = _text(item.get("photo"))
    file_path = _text(item.get("file"))
    if not photo and not file_path:
        _mark_missing(doc, row, "attachments")
        return
    category, basis = _category_from_desktop(item, photo, file_path)
    width = _positive_int(item.get("width"))
    height = _positive_int(item.get("height"))
    duration = item.get("duration_seconds")
    row["attachments"].append({
        "category": category, "category_basis": basis, "path": photo or file_path,
        "mime_type": _text(item.get("mime_type")),
        "declared_media_type": _text(item.get("media_type")),
        "width": width, "height": height,
        "duration_seconds": duration if isinstance(duration, (int, float))
        and not isinstance(duration, bool) else None,
        "thumbnail": _text(item.get("thumbnail")), "title": _text(item.get("title")),
        "node": None, "source_field": "photo" if photo else "file"})
    row["attachments_missing"] = False
    if category == media.UNRESOLVED:
        doc["gaps"]["notes"].append("消息 %s 的附件判为 unknown（%s）" % (message_id, basis))


# ---------------------------------------------------------------- 公开入口

def iter_normalized_sources(path, source_format, metadata, select_chat=None):
    """逐聊天产出归档：一次只处理一个来源里的一个聊天，写完就能被下一个覆盖。

    ``select_chat`` 只表达「用户点名要哪一个聊天」；批量导入不传它，身份一律取源里已声明
    的 chat ID（Desktop 的 ``chats[].id`` / tdl 的 ``chat_id``），绝不按聊天名或文件名猜。
    """
    metadata = dict(metadata or {})
    path, payload, digest_before, digest_after = _load(path, source_format)
    _require(digest_before == digest_after,
             "来源文件在读取期间发生变化（%s）：原始文件必须只读，本次不出归档" % path.name)
    wanted = _positive_int(select_chat)
    digests = (digest_before, digest_after)
    if source_format == "tdl_json":
        yield _normalize_tdl(payload, path, metadata, digests, wanted)
        return
    for chat in _desktop_chats(payload, wanted):
        yield _normalize_desktop_one(chat, path, metadata, digests,
                                     "metadata" if wanted else "source.chats[].id", wanted)


def normalize_source(path, source_format, metadata):
    """把一个用户选定的本地导出归一化成共同归档 dict（合同签名，逐字照用）。

    单来源单聊天返回归档；一个文件里含多个聊天且用户没指定 chat_id 时拒绝并说明该补什么。
    """
    metadata = dict(metadata or {})
    produced = []
    for archive in iter_normalized_sources(path, source_format, metadata,
                                           select_chat=metadata.get("chat_id")):
        produced.append(archive)
    _require(produced, "来源里没有任何聊天（不默认为空聊天，也不猜身份）：%s" % Path(path).name)
    _require(len(produced) == 1,
             "该来源含 %d 个聊天，请用 metadata.chat_id 指定其中一个，或改用 "
             "iter_normalized_sources 逐聊天处理；可选 chat_id：%s"
             % (len(produced), "、".join(str(item["chat_id"]) for item in produced)))
    return produced[0]


# ---------------------------------------------------------------- 派生存储

def _archive_name(archive):
    return "%s-archive-%s.json" % (state.identity_slug(archive["identity"]),
                                   archive["source"]["batch_id"])


def _index_name(archive):
    return "%s-index-%s.json" % (state.identity_slug(archive["identity"]),
                                 archive["source"]["batch_id"])


def _dumps(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    state.publish_text_files(path.parent, {path.name: text})
    return path


def _index_messages(archive):
    """派生文本索引：reading 的入参形状（id/date/text/sender）+ 分类需要的原始附件节点。"""
    rows = []
    for item in archive["messages"]:
        row = {"id": item["message_id"], "text": item["text"]}
        if item["date_utc"] is not None:
            row["date"] = item["date_utc"]
        if not item["sender"]["missing"]:
            row["sender"] = item["sender"]["name"]
            if item["sender"]["id"]:
                row["from_id"] = item["sender"]["id"]
        if item["reply_to"]["message_id"] is not None:
            row["reply_to_message_id"] = item["reply_to"]["message_id"]
        for attachment in item["attachments"]:
            if attachment["path"]:
                row["file"] = attachment["path"]
            if isinstance(attachment["node"], dict):
                row.update(attachment["node"])
        rows.append(row)
    return rows


def _index_payload(archive):
    return {"kind": "telegram-archive-derived-index", "version": ARCHIVE_VERSION,
            "identity": archive["identity"], "peer_type": archive["peer_type"],
            "chat_id": archive["chat_id"],
            "range": {"mode": "id", "lower": archive["coverage"]["id_lower"],
                      "upper": archive["coverage"]["id_upper"]},
            "snapshot": {"upper_bound": archive["coverage"]["id_upper"]},
            "source_batch_id": archive["source"]["batch_id"],
            "source_sha256": archive["source"]["sha256_before"],
            "online_coverage": ONLINE_COVERAGE, "count": len(archive["messages"]),
            "messages": _index_messages(archive)}


def _load_batches(path):
    if path is None or not Path(path).is_file():
        return {"kind": BATCH_KIND, "version": BATCH_VERSION, "batches": [],
                "updated_at": None}
    payload = state._read_json(path)
    _require(isinstance(payload, dict) and isinstance(payload.get("batches"), list),
             "来源批次清单 %s 形状不认识（不覆盖、不猜）" % Path(path).name)
    return payload


def _batch_record(archive, extra):
    coverage = archive["coverage"]
    source = archive["source"]
    return {"batch_id": source["batch_id"], "identity": archive["identity"],
            "chat_id": archive["chat_id"], "peer_type": archive["peer_type"],
            "source_path": source["path"], "source_name": source["name"],
            "source_format": source["format"], "source_sha256": source["sha256_before"],
            "source_sha256_after": source["sha256_after"],
            "read_only_verified": source["read_only_verified"],
            "processing_version": ARCHIVE_VERSION,
            "message_count": coverage["message_count"], "id_lower": coverage["id_lower"],
            "id_upper": coverage["id_upper"], "date_lower": coverage["date_lower"],
            "date_upper": coverage["date_upper"],
            "display_timezone": archive["time"]["display_timezone"],
            "utc_field": archive["time"]["utc_field"],
            "online_coverage": ONLINE_COVERAGE, "online_complete": False,
            "claim": LOCAL_SNAPSHOT_CLAIM,
            "missing_fields": copy.deepcopy(archive["gaps"]["missing_fields"]),
            "unknown_field_count": len(archive["gaps"]["unknown_fields"]),
            "unknown_entity_count": len(archive["gaps"]["unknown_entities"]),
            # 评审 I-2：正文与实体数组不一致的条数进台账，Task 8 对账时看得见
            "text_mismatch_count": len(archive["gaps"]["text_mismatch"]),
            "archive_path": extra.get("archive_path"),
            "text_index_path": extra.get("text_index_path"),
            "online_completion": False,
            "reading_built": bool(extra.get("reading")),
            "imported_at": extra.get("imported_at") or _now()}


# ---------------------------------------------------------------- 覆盖闸门与凭据口径

class FrozenRange(ValueError):
    """来源覆盖越过用户冻结的范围：这是配置冲突（退出码 2），不是「某个源读不动」。

    单独一个类型，好让 :func:`import_sources` 只把「坏源」归类为普通 ValueError，
    越过冻结边界这种全局冲突直接向外抛，绝不被记成单个来源的失败后照常发布清单。
    """


def _check_frozen_bounds(data, archive):
    """冻结上界优先：来源覆盖越界就拒绝，不悄悄扩大范围（合同第 87 行口径）。"""
    coverage = archive["coverage"]
    if coverage["id_upper"] is None:
        return
    snapshot = (data.get("snapshot") or {}).get("upper_bound")
    if _positive_int(snapshot) and coverage["id_upper"] > snapshot:
        raise FrozenRange("来源里出现了消息 ID %s，越过本次冻结的快照上界 %s（导入不扩大"
                          "已冻结的范围）" % (coverage["id_upper"], snapshot))
    scope = data.get("range") or {}
    if scope.get("mode") == "id":
        _require(coverage["id_lower"] >= scope["lower"]
                 and coverage["id_upper"] <= scope["upper"],
                 "来源覆盖的消息 ID 区间 [%s, %s] 不在用户冻结的 [%s, %s] 之内"
                 % (coverage["id_lower"], coverage["id_upper"], scope["lower"],
                    scope["upper"]))


def _wants_text_index(data):
    contents = list(data.get("contents") or [])
    outputs = list(data.get("outputs") or [])
    return "text" in contents and bool({"markdown", "json"} & set(outputs))


# ---------------------------------------------------------------- 阅读版（复用 Task 4）

_OFFSET_LABEL = re.compile(r"^[+-]\d{2}:?\d{2}$")


def _zone_token(data, archive):
    """展示时区：只把可核对的 UTC / ±HH:MM 交给阅读版（与 Task 4 同一口径）。

    归档里的 ``time.display_timezone`` 原样保留用户给的标签（含 ``Asia/Shanghai`` 这类
    名字）；命名时区在离线机器上没有可核对的 tzdata，所以阅读版退回 UTC 并留下申报，
    绝不猜偏移。
    """
    for value in (data.get("timezone"), archive["time"]["display_timezone"]):
        if not isinstance(value, str) or not value.strip():
            continue
        token = value.strip()
        if token.upper() in ("UTC", "Z") or _OFFSET_LABEL.match(token):
            return token, None
        if token != "unknown":
            return "UTC", ("展示时区 %s 不是可核对的固定偏移（Task 4 的同一口径）：阅读版按 "
                           "UTC 渲染，归档里的 display_timezone 仍保留原标签" % token)
    return "UTC", "用户未给展示时区：阅读版按 UTC 渲染，归档里 display_timezone 记 unknown"


def _reading_options(data, archive):
    token, note = _zone_token(data, archive)
    notes = [note] if note else []
    return {"chat": {"peer_type": archive["peer_type"], "id": archive["chat_id"],
                     "display_name": archive["chat"]["display_name"] or archive["identity"]},
            "cleaning": data.get("cleaning", "none"),
            "template_compression": bool(data.get("template_compression", False)),
            "timezone": token,
            "contents": ["text"], "outputs": ["markdown"],
            "source": {"path": archive["source"]["path"], "name": archive["source"]["name"],
                       "sha256": archive["source"]["sha256_before"]}}, notes


def build_reading(archive, target, job=None):
    """导入后走 Task 4 的既有阅读接口：只吃归一化结果，绝不登录、绝不拉历史。"""
    options, notes = _reading_options(job or {}, archive)
    report = reading.build_reading_pack(_index_messages(archive), Path(target), options)
    report["import_notes"] = notes
    report["login_used"] = False
    report["history_fetched"] = False
    return report


# ---------------------------------------------------------------- 组合层

def import_sources(sources, metadata, source_format=None, derived_dir=None,
                   batches_path=None, progress=None, per_chat=None):
    """逐个来源、逐个聊天写入派生存储；坏源单独报告，整体报告列出全部失败源。

    ``sources`` 必须是用户明确选择的路径清单（本函数不递归磁盘、不自动找文件）。
    ``per_chat(archive)`` 在写盘前调用，用于覆盖闸门与完成凭据登记，返回的映射会并进
    该聊天的报告条目。

    给了 ``batches_path`` 时，来源批次台账的读-改-写**全程持归档锁**（
    :func:`state.acquire_archive_lock`，锁名 ``archive-*``），合并结果由
    ``state.publish_text_files`` 原子发布后才放锁；锁被占用抛 :class:`state.LockBusy`
    （CLI 按能力缺口退出码 3），不静默跳过、不覆盖对方正在写的台账。加锁顺序固定
    「先归档锁、后会话锁」（``state.LOCK_ORDER``）；报告里的 ``lock`` 块记下本次持过的锁。
    """
    _require(isinstance(sources, (list, tuple)) and sources,
             "sources 必须是非空列表（离线动作只能使用用户明确选择的本地路径，"
             "不自动递归整个磁盘）")
    derived_dir = Path(derived_dir) if derived_dir else None
    batches_path = Path(batches_path) if batches_path else None
    # 台账 imported.json 是「整读 -> 内存合并 -> 整写」的读-改-写，全程持归档锁
    # （archive-* 前缀，Task 2 预留名），写完由 state.publish_text_files 原子发布，从而与
    # search/share/update 互斥（合同：search/import/share 与 update 的组合按归档锁互斥）。
    # 加锁顺序固定「先归档锁、后会话锁」（state.LOCK_ORDER）：import 是离线动作，只持
    # 归档锁、不取会话锁。锁被占用一律抛 state.LockBusy，由 CLI 归为能力缺口（退出码 3），
    # 绝不静默跳过、也不覆盖别人正在写的台账。Task 8 复用同一对 helper。
    # 归档锁的口径（评审 N-2）：锁根与被锁的资源都是**同一个状态根**（派生层目录），
    # 不是「各锁自己那个文件」——修复轮 1 只统一了 marker 算法，import 锁 imported.json、
    # 索引锁 catalog.db 时两个锁名天然不同，import 与 search/rebuild 其实并不互斥。
    handle = None
    if batches_path is not None:
        state_root = batches_path.parent           # 归档锁的状态根：<destination>/derived
        handle = state.acquire_archive_lock(state_root, resource=state_root,
                                            owner={"label": "import"})
    try:
        emit = progress or (lambda event: None)
        batches = _load_batches(batches_path)
        known = {item.get("batch_id"): item for item in batches["batches"]}
        entries, failures = [], []
        for raw_path in sources:
            path = Path(str(raw_path))
            emit({"stage": "source_started", "path": str(path),
                  "source_format": source_format})
            entry = {"path": str(path), "name": path.name, "source_format": source_format,
                     "chats": [], "status": "complete", "reason": None,
                     "sha256_before": None, "sha256_after": None,
                     "read_only_verified": None}
            pending = {}                       # 本来源的批次：整源成功后才并进清单
            try:
                for archive in iter_normalized_sources(path, source_format, metadata):
                    extra = {}
                    if entry["sha256_before"] is None:
                        entry["sha256_before"] = archive["source"]["sha256_before"]
                    emit({"stage": "chat_normalized", "path": str(path),
                          "identity": archive["identity"],
                          "message_count": archive["coverage"]["message_count"]})
                    if derived_dir is not None:
                        target = derived_dir / _archive_name(archive)
                        _write(target, _dumps(archive))
                        extra["archive_path"] = str(target)
                        # 派生文本索引：形状与导出成品一致（state.validate_export 认得），
                        # 供阅读版与后续增量使用；它不是在线完成凭据。
                        index = derived_dir / _index_name(archive)
                        _write(index, _dumps(_index_payload(archive)))
                        extra["text_index_path"] = str(index)
                    if per_chat:
                        extra.update(per_chat(archive) or {})
                    record = _batch_record(archive, extra)
                    pending[record["batch_id"]] = record
                    entry["chats"].append({
                        "identity": archive["identity"], "peer_type": archive["peer_type"],
                        "chat_id": archive["chat_id"],
                        "source_format": archive["source"]["format"],
                        "message_count": archive["coverage"]["message_count"],
                        "batch_id": record["batch_id"],
                        "archive_path": extra.get("archive_path"),
                        "text_index_path": extra.get("text_index_path"),
                        "credential": extra.get("credential"),
                        "register_reason": extra.get("register_reason"),
                        "reading": extra.get("reading"),
                        "missing_fields": copy.deepcopy(archive["gaps"]["missing_fields"]),
                        "unknown_fields": copy.deepcopy(archive["gaps"]["unknown_fields"]),
                        "unknown_entities": copy.deepcopy(archive["gaps"]["unknown_entities"]),
                        "text_mismatch": copy.deepcopy(archive["gaps"]["text_mismatch"]),
                        "notes": copy.deepcopy(archive["gaps"]["notes"])})
                    emit({"stage": "chat_written", "path": str(path),
                          "identity": archive["identity"],
                          "archive_path": extra.get("archive_path")})
                    del archive          # 一次只装一个聊天：写完就放掉，不留到下个聊天
                after = _digest(path.read_bytes())
                entry["sha256_after"] = after
                entry["read_only_verified"] = entry["sha256_before"] == after
                if not entry["read_only_verified"]:
                    entry.update({"status": "failed",
                                  "reason": "来源文件在导入期间被改动（原档必须只读），本批不采信"})
                    failures.append({"path": str(path), "reason": entry["reason"]})
                elif not entry["chats"]:
                    entry.update({"status": "failed", "reason": "来源里没有可归档的聊天"})
                    failures.append({"path": str(path), "reason": entry["reason"]})
                else:
                    known.update(pending)
                    # 批次按首次导入顺序排列；同 batch_id 复导入只替换内容，不挪位置
                    batches["batches"] = list(known.values())
            except FrozenRange:
                raise                       # 全局配置冲突：不记成单个坏源，直接向外抛
            except ValueError as error:
                entry.update({"status": "failed", "reason": str(error)})
                failures.append({"path": str(path), "name": path.name, "reason": str(error)})
            entries.append(entry)
            emit({"stage": "source_finished", "path": str(path), "status": entry["status"],
                  "reason": entry["reason"]})
        if batches_path is not None:
            batches["updated_at"] = _now()
            batches["claim"] = LOCAL_SNAPSHOT_CLAIM
            batches["processing_version"] = ARCHIVE_VERSION
            _write(batches_path, _dumps(batches))
    finally:
        if handle is not None:
            state.release_archive_lock(handle)
    lock = None
    if handle is not None:
        lock = {"kind": handle["kind"], "marker": handle["marker"],
                "root": str(handle["base"]), "order": handle["order"],
                "path": handle["token"]["path"],
                "note": "台账的读-改-写全程持归档锁，写完原子发布；加锁顺序固定"
                        "「先归档锁、后会话锁」（state.LOCK_ORDER），import 是离线动作、"
                        "不取会话锁（session-* 与 archive-* 永不同名）"}
    succeeded = [item for item in entries if item["status"] == "complete"]
    status = "complete" if len(succeeded) == len(entries) else (
        "failed" if not succeeded else "partial")
    source_batches = [{key: value for key, value in item.items()}
                      for item in batches["batches"]]
    return {"kind": "telegram-archive-import-report", "version": ARCHIVE_VERSION,
            "status": status, "sources": entries, "failed_sources": failures,
            "lock": lock,
            "source_batches": source_batches, "counts": {
                "sources": len(entries), "succeeded": len(succeeded),
                "failed": len(failures), "chats": sum(len(item["chats"]) for item in entries),
                "messages": sum(chat["message_count"] for item in entries
                                for chat in item["chats"])},
            "claims": {"local_snapshot_only": True, "online_full_export_verified": False,
                       "online_coverage": ONLINE_COVERAGE, "claim": LOCAL_SNAPSHOT_CLAIM,
                       "read_only_source": all(
                           bool(item["read_only_verified"]) for item in entries),
                       "network_used": False, "login_used": False,
                       "media_downloaded": False,
                       "download_is_separate_online_action": True},
            "limitations": copy.deepcopy(LIMITATIONS)}


def _headline(report):
    counts = report["counts"]
    if report["status"] == "complete":
        return "已完成：导入 %s 个来源、%s 个聊天、%s 条消息（用户导入的本地快照，在线覆盖未验证）" % (
            counts["sources"], counts["chats"], counts["messages"])
    if report["status"] == "partial":
        return "部分完成：%s 个来源成功、%s 个来源失败（失败源已单独列出，其余照常入库；" \
               "本地快照不代替在线导出）" % (counts["succeeded"], counts["failed"])
    return "失败：%s 个来源全部未成功（见 failed_sources 的逐条原因）" % counts["sources"]


def run_import(job):
    """``import`` 动作的组合层：离线、不登录、不联网；无 tdl/storage/namespace 也要完成。"""
    data = validate_job(job)
    _require(data.get("action") == "import",
             "run_import 只处理 action=import：%s" % data.get("action"))
    destination = _absolute(data.get("destination"), "destination")
    source_format = data.get("source_format")
    _require(source_format in SUPPORTED_FORMATS,
             "缺少或不受支持的 source_format：%r（首期只覆盖 %s，未知来源不猜）"
             % (source_format, "、".join(SUPPORTED_FORMATS)))
    derived_dir = destination / DERIVED_SUBDIR
    batches_path = derived_dir / MANIFEST_NAME
    reading_root = destination / "阅读版"          # 与 render 的默认输出目录同一处
    per_chat = lambda archive: _review(data, archive, reading_root)  # noqa: E731 - 单点回调

    report = import_sources(data["sources"], dict(data.get("metadata") or {}),
                            source_format=source_format, derived_dir=derived_dir,
                            batches_path=batches_path, per_chat=per_chat)
    report["destination"] = str(destination)
    report["derived_dir"] = str(derived_dir)
    report["reading_dir"] = str(reading_root)
    report["manifest_path"] = str(batches_path)
    report["runtime"] = {"tdl_required": False, "session_required": False,
                         "offline_action": True,
                         "note": "import 属离线动作：没有 tdl / storage / namespace 也照跑"}
    readings = [chat.get("reading") for entry in report["sources"]
                for chat in entry["chats"] if chat.get("reading")]
    report["actions"] = {"download_selection_created": False,
                         "download_selection_path": None,
                         "reading_built": bool(readings),
                         "reading_status": (readings[0]["status"] if readings else None)}
    selection = _download_selection(data, report, derived_dir)
    if selection:
        report["actions"]["download_selection_created"] = True
        report["actions"]["download_selection_path"] = selection
    report["claims"]["media_downloaded"] = False
    report["headline"] = _headline(report)
    return report


def _review(data, archive, reading_root):
    """覆盖闸门 + 阅读版（复用 Task 4 接口）+ 凭据口径申报。"""
    _check_frozen_bounds(data, archive)
    out = {"credential": {"online_completion": False,
                          "reason": "离线导入不产生在线完成凭据：render/download/export 的完成"
                                    "闸仍只认已验收导出的完成清单，导入文件不算已验证在线全量"}}
    if archive["coverage"]["message_count"] == 0:
        out["register_reason"] = "本批 0 条消息：没有可登记的区间，也不出阅读版"
        return out
    if not _wants_text_index(data):
        out["register_reason"] = ("本次未选择文字正文输出（contents/outputs 未含 text + "
                                  "markdown/json）：只写派生归档与文本索引")
        return out
    built = build_reading(archive, reading_root / state.identity_slug(archive["identity"]),
                         data)
    out["reading"] = {"status": built["status"], "target": built["target"],
                      "files": [item["path"] for item in built["files"]],
                      "counts": built["counts"], "index": built["index"],
                      "source_unchanged": built["source"]["unchanged"],
                      "display_timezone": built["display_timezone"],
                      "notes": built["import_notes"],
                      "login_used": built["login_used"],
                      "history_fetched": built["history_fetched"]}
    out["register_reason"] = None
    return out


def _download_selection(data, report, derived_dir):
    """补媒体是独立在线动作：这里只产出选择清单，绝不下载。"""
    attachments = [item for item in (data.get("contents") or [])
                   if item in media.ATTACHMENT_TYPES]
    if not attachments:
        return None
    target = None
    for entry in report["sources"]:
        for chat in entry["chats"]:
            if not chat.get("text_index_path"):
                continue
            payload = state._read_json(Path(chat["text_index_path"]))
            selection, unknown = media.select_media(payload, attachments)
            selection["chat"] = {"peer_type": chat["peer_type"], "id": chat["chat_id"]}
            selection["source_batch_id"] = chat["batch_id"]
            selection["imported_snapshot"] = True
            selection["online_coverage"] = ONLINE_COVERAGE
            selection["unknown_ids"] = sorted(set(selection["unknown_ids"]) | set(unknown))
            target = derived_dir / ("%s-download-selection.json"
                                    % state.identity_slug(chat["identity"]))
            _write(target, _dumps(selection))
    return str(target) if target else None
