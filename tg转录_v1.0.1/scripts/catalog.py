"""Task 6：可重建本地索引与检索（Python 标准库 SQLite，纯离线）。

设计口径（设计第 142–146 行、工单第 87–89 行）：

* **只用标准库** ``sqlite3``：不装分词包、不接外部搜索服务、不上传任何内容；索引库
  是**可删可重建的派生层**，原始归档与阅读版永远是底档，换索引库实例不影响归档可读。
  唯一的仓内依赖是归档锁：marker 与取放一律复用 ``state`` 的那一份实现（评审 I-4），
  本模块不再自带第二份口径。
* **关键词一律作字面值**：匹配走 ``instr()`` 的参数化比较，``%``、``_``、单引号、
  反斜杠、中文全部按普通字符处理，绝不把用户关键词当 LIKE 模式或代码执行；
  大小写折叠只对 ASCII 生效并如实申报（``case_folding="ascii-only"``）。
* **FTS 未验证就不声称**：本模块不建 FTS 虚表、不承诺中文分词能力，报告里显式写
  ``fts_enabled=False``、``chinese_tokenizer_installed=False``。
* **raw 与 reading 两个视图物理同库、逻辑分离**：行的主键含 ``view``，
  reading 视图的成员严格取自阅读版实际保留清单（分篇里出现过的消息 ID），
  被阅读版过滤掉的原文**不进入** reading 视图，查 reading 绝不返回它、也不泄漏其片段。
* **身份隔离**：主键是 ``(view, identity, message_id)``，``identity`` 是
  ``<peer_type>:<chat_id>``；两个同名聊天持有相同 message ID 也绝不互串。
* **事务与可见性**：写入走 ``BEGIN IMMEDIATE`` 事务，先进 staging 再整批替换当前视图；
  读侧是另一个连接，只看得见**已提交**视图（WAL 可用时读者不阻塞）。事务失败或索引
  版本不认识时维持旧的可用视图并申报「从原档重建」，不半修、不猜。

供 Task 8（增量）使用的接口（本模块只保证形状可用，不含增量判断）：
``connect / begin / stage_archive / publish_staged / commit / abort``、
``revisions`` 表、``source_batches`` 表、``database_status``、``list_revisions``。
"""

import base64
import datetime
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

# 归档锁只此一份实现（state.py 的 helper）：marker 与取放都从 state 转入，本模块不再自带
# 第二份口径，也不内联加锁三步。用 from-import 只取这四个名字，不引 state 的其他内容。
from state import (LockBusy, acquire_archive_lock, archive_lock_marker,
                   release_archive_lock)

CATALOG_KIND = "telegram-archive-catalog"
SEARCH_KIND = "telegram-archive-search-result"
STATUS_KIND = "telegram-archive-catalog-status"
CATALOG_VERSION = 2                       # 本索引 schema 版本（Task 8 比对迁移用）
# v2 的两处 DDL 变动（v1 库不再被拒绝读写：版本不符自动从原档整批重建，见 ensure_schema）：
#   * revisions.new_batch_id 改为可空——消息从批次里消失时记 NULL，不再为了填 NOT NULL
#     而去取「本次 staging 的第一条」，那正是评审 C1 崩溃的根因；
#   * idx_reading_paths_target 以 view 起头——阅读入口查询带 view 谓词（评审 I-3）。
ARCHIVE_KIND = "telegram-archive"         # Task 5 冻结的归档 dict kind
ARCHIVE_VERSION = 1                       # Task 5 冻结的归档 schema 版本
LOCAL_SNAPSHOT_CLAIM = "用户导入的本地快照，在线覆盖未验证"
VIEWS = ("raw", "reading")
PEER_TYPES = ("user", "chat", "channel")
SNIPPET_MAX_CHARS = 160                   # 结果片段的长度上限（不含省略号外的加工）
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
TIME_SENTINEL = -1                        # 时间缺失的排序哨兵：真实 UTC 秒恒 >= 0

STDLIB_MODULES = frozenset({"base64", "datetime", "hashlib", "json", "os", "re", "sqlite3",
                            "pathlib"})
TABLES = ("meta", "chats", "messages", "revisions", "attachments", "source_batches",
          "reading_paths")
# staging 表与正式表共用同一份表体（一份定义、两套名字），Task 8 的事务写入靠它做缓冲。
_STAGE_SOURCES = ("chats", "messages", "attachments", "source_batches", "reading_paths")
STAGE_TABLES = tuple("stage_%s" % name for name in _STAGE_SOURCES)
QUERY_KEYS = frozenset(("view", "keyword", "case_sensitive", "chats", "since", "until",
                        "id_lower", "id_upper", "message_ids", "order", "limit", "cursor"))
READING_FILE_LINE = re.compile(r"^- \*\*[^*\n]*\*\* #(?P<id>\d+) ", re.MULTILINE)

_META_KEYS = ("schema_version", "catalog_kind", "archive_version", "claim", "backend",
              "literal_matching", "fts_enabled", "updated_at")

# 表体一份两用：正式表与 staging 表结构逐字一致，避免「两套 schema」漂移。
_TABLE_BODIES = {
    "meta": """
        key TEXT NOT NULL PRIMARY KEY,
        value TEXT NOT NULL""",
    "chats": """
        identity TEXT NOT NULL PRIMARY KEY,
        peer_type TEXT NOT NULL CHECK (peer_type IN ('user','chat','channel')),
        chat_id INTEGER NOT NULL,
        display_name TEXT,
        display_name_basis TEXT,
        identity_basis TEXT,
        source_format TEXT NOT NULL,
        source_path TEXT,
        locator TEXT,
        batch_id TEXT NOT NULL,
        archive_version INTEGER NOT NULL,
        first_indexed_at TEXT NOT NULL,
        updated_at TEXT NOT NULL""",
    "messages": """
        view TEXT NOT NULL CHECK (view IN ('raw','reading')),
        identity TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        identity_key TEXT NOT NULL,
        date_utc INTEGER,
        date_sort INTEGER NOT NULL,
        time_missing INTEGER NOT NULL CHECK (time_missing IN (0,1)),
        display_time TEXT,
        text TEXT,
        has_text INTEGER NOT NULL CHECK (has_text IN (0,1)),
        text_sha256 TEXT NOT NULL,
        sender_name TEXT,
        sender_id INTEGER,
        sender_missing INTEGER NOT NULL CHECK (sender_missing IN (0,1)),
        reply_to_message_id INTEGER,
        attachment_count INTEGER NOT NULL,
        service INTEGER NOT NULL CHECK (service IN (0,1)),
        action TEXT,
        batch_id TEXT NOT NULL,
        archive_path TEXT,
        source_format TEXT NOT NULL,
        source_sha256 TEXT,
        indexed_at TEXT NOT NULL,
        PRIMARY KEY (view, identity, message_id)""",
    # 历史版本：当前视图被不同内容取代时才追加一行；Task 8 的增量对账靠它回溯。
    # new_batch_id 为 NULL 表示这条消息在本次发布里**从批次中消失**（没有取代它的那一批）：
    # 收紧/删除路径因此不再需要「凑一个批次 ID」，也就不会因为 staged 为空而崩；
    # 完整的删除证据语义由 Task 8 落地。
    "revisions": """
        revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
        view TEXT NOT NULL CHECK (view IN ('raw','reading')),
        identity TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        previous_batch_id TEXT NOT NULL,
        new_batch_id TEXT,
        old_text TEXT,
        old_date_utc INTEGER,
        old_sender_name TEXT,
        old_text_sha256 TEXT,
        old_archive_path TEXT,
        reason TEXT NOT NULL,
        superseded_at TEXT NOT NULL""",
    "attachments": """
        attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        view TEXT NOT NULL CHECK (view IN ('raw','reading')),
        identity TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        category TEXT NOT NULL,
        path TEXT,
        mime_type TEXT,
        declared_media_type TEXT,
        width INTEGER,
        height INTEGER,
        duration_seconds REAL,
        title TEXT,
        UNIQUE (view, identity, message_id, position)""",
    "source_batches": """
        view TEXT NOT NULL CHECK (view IN ('raw','reading')),
        batch_id TEXT NOT NULL,
        identity TEXT NOT NULL,
        peer_type TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        display_name TEXT,
        source_path TEXT,
        source_name TEXT,
        source_format TEXT NOT NULL,
        source_sha256 TEXT,
        processing_version INTEGER NOT NULL,
        archive_version INTEGER NOT NULL,
        message_count INTEGER NOT NULL,
        id_lower INTEGER,
        id_upper INTEGER,
        date_lower INTEGER,
        date_upper INTEGER,
        display_timezone TEXT,
        utc_field TEXT,
        online_coverage TEXT NOT NULL,
        online_complete INTEGER NOT NULL CHECK (online_complete IN (0,1)),
        claim TEXT NOT NULL,
        archive_path TEXT,
        text_index_path TEXT,
        content_sha256 TEXT NOT NULL,
        indexed_at TEXT NOT NULL,
        PRIMARY KEY (view, batch_id)""",
    # 阅读入口：只有阅读版里真实存在的位置才登记；查不到就留空，绝不编链接。
    "reading_paths": """
        view TEXT NOT NULL CHECK (view IN ('raw','reading')),
        identity TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        path TEXT NOT NULL,
        target TEXT,
        kind TEXT NOT NULL CHECK (kind IN ('part','index')),
        PRIMARY KEY (view, identity, message_id, path)""",
}

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_messages_sort ON messages"
    " (view, date_sort, identity, message_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_identity ON messages (view, identity)",
    "CREATE INDEX IF NOT EXISTS idx_revisions_target ON revisions"
    " (view, identity, message_id)",
    "CREATE INDEX IF NOT EXISTS idx_attachments_target ON attachments"
    " (view, identity, message_id)",
    "CREATE INDEX IF NOT EXISTS idx_reading_paths_target ON reading_paths"
    " (view, identity, message_id)",
    "CREATE INDEX IF NOT EXISTS idx_batches_identity ON source_batches (view, identity)",
)


class CatalogError(Exception):
    """索引不可用（不存在、版本不认识、结构被破坏）：申报从原档重建，不半修、不猜。"""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _digest(*parts):
    return hashlib.sha256("\u241f".join("" if item is None else str(item)
                                         for item in parts).encode("utf-8")).hexdigest()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _checked_view(view):
    _require(view in VIEWS,
             "视图必须是明确选择的 %s（原始正文与阅读版不共用一个视图，不默认、不猜）：%r"
             % ("、".join(VIEWS), view))
    return view


def _flag(value):
    return 1 if value else 0


def acquire_index_lock(state_root, owner=None):
    """索引写入前取归档锁：锁根与被锁的资源都是**同一个状态根**（评审 N-2）。

    修复轮 1 只把 marker 算法统一成 ``state.archive_lock_marker`` 那一份，但锁根与锁资源
    仍不同源：import 锁 ``imported.json``（根＝它的父目录）、索引锁 ``catalog.db``
    （根＝索引库父目录），两个锁名天然不同 → import 与 search/rebuild 之间**并不互斥**。
    现在按总控裁决：两侧都以**同一个状态根**（派生层目录 ``<destination>/derived``）
    作为锁根，也把同一个状态根作为被锁资源，于是锁文件完整路径逐字相同，合同第 88 行
    要求的「search/import/share 与 update 按归档锁互斥」才真的成立。取放与 marker 仍只用
    ``state`` 那一份实现（``state.acquire_archive_lock`` / ``state.archive_lock_marker``），
    本模块不内联加锁三步、也不新造第二套路径规则。

    调用方（同一进程里先取了这把锁的任务，例如 Task 8 的 update）已经持有同一把归档锁时
    不重复占用——锁文件里的 ``process_pid`` 就是本进程，说明这把锁本来就是我们自己的，
    直接沿用（同进程重入），绝不另起一把同名锁把自己锁死。别的进程持有时原样抛
    :class:`state.LockBusy`（CLI 归为能力缺口退出码 3），不静默跳过、不覆盖对方正在写的状态根。
    """
    root = Path(str(state_root))
    try:
        handle = acquire_archive_lock(root, resource=root,
                                      owner=owner or {"label": "index"})
    except LockBusy as busy:
        holder = busy.holder if isinstance(busy.holder, dict) else {}
        if holder.get("process_pid") == os.getpid():
            return {"token": None, "base": root,
                    "marker": archive_lock_marker(root), "reused": True}
        raise
    handle["reused"] = False
    return handle


def release_index_lock(handle):
    """释放在 :func:`acquire_index_lock` 里取得的锁；沿用了调用方的锁时不替对方解锁。"""
    if handle.get("token") is None:
        return {"released": None, "reused": bool(handle.get("reused"))}
    return release_archive_lock(handle)



# ---------------------------------------------------------------- 连接与 schema

def connect(database):
    """开一条连接：行工厂 + WAL（可用则用）+ 自动事务关闭，由调用方 commit/abort。"""
    _require(database is not None, "索引库路径不能是 None")
    path = Path(str(database))
    _require(str(path).strip(), "索引库路径必须是非空路径")
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None                     # 事务由 begin/commit/abort 显式管
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        # 已经是 WAL 时不再重设：读者在写事务进行中改模式会被拒（database is locked）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _journal_mode(conn):
    """当前连接的实际日志模式（WAL 可用则读者不阻塞写者，读侧只看已提交视图）。"""
    return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def _table_names(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}


def begin(conn):
    conn.execute("BEGIN IMMEDIATE")


def commit(conn):
    conn.execute("COMMIT")


def abort(conn):
    conn.rollback()


def _ddl(name, base=None):
    """正式表与 staging 表共用同一份表体定义（base 是表体名，name 是实际表名）。"""
    return "CREATE TABLE IF NOT EXISTS %s (%s)" % (name, _TABLE_BODIES[base or name])


def _drop_derived_tables(conn, names):
    """把派生层的表整批丢掉（索引随表一起消失）：只用于版本不符的自动重建。"""
    for name in names:
        conn.execute("DROP TABLE IF EXISTS %s" % name)


def ensure_schema(conn):
    """建表并做版本闸门。

    三种形状分开处理（不混成一句「删库重建」）：

    * 表都不在 / 版本行都没有却已有同名表 → 结构被破坏：抛 :class:`CatalogError`，
      不动一个字节（拿半套字段建索引才是真危险）。
    * 结构完整、只是 ``schema_version`` 与当前实现不符 → **自动就地重建**：派生层的表
      整批丢掉后按当前版本重建，紧接着的这次事务会把用户选定范围的内容重新发布回去。
      索引是可重建派生层、原始归档才是底档，所以「篡改/落后一个版本号就永久不可查」
      这种自伤路径不留进产品（评审 I-1 申报的偏差，按总控裁决改掉）。
    * 库是空的或本版本 → 直接建表写元数据。
    """
    names = _table_names(conn)
    missing = [name for name in TABLES if name not in names]
    stored = None
    rebuilt_from = None
    if "meta" in names:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        stored = None if row is None else str(row[0])
        if missing:
            raise CatalogError("索引库缺少表 %s：结构已损坏，不做半修。请删掉索引库后从原始归档"
                               "重建（索引是派生层，原档不受影响）"
                               % "、".join(sorted(missing)))
        if stored is not None and stored != str(CATALOG_VERSION):
            rebuilt_from = stored
            _drop_derived_tables(conn, TABLES + STAGE_TABLES)
    for name in TABLES:
        conn.execute(_ddl(name))
    for name in _STAGE_SOURCES:
        conn.execute(_ddl("stage_%s" % name, name))
    for statement in _INDEXES:
        conn.execute(statement)
    for key, value in (("schema_version", str(CATALOG_VERSION)),
                       ("catalog_kind", CATALOG_KIND),
                       ("archive_version", str(ARCHIVE_VERSION)),
                       ("claim", LOCAL_SNAPSHOT_CLAIM),
                       ("backend", "sqlite3(标准库)"),
                       ("literal_matching", "instr 字面子串；SQL 通配符按字面"),
                       ("fts_enabled", "false")):
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    return {"created": stored is None, "previous_version": stored,
            "rebuilt_from": rebuilt_from}


def _open_read(database, allow_stale_version=False):
    path = Path(str(database))
    if not path.is_file():
        raise CatalogError("索引库不存在：%s——先按用户选定的范围建立索引（派生层，"
                           "随时可从原始归档重建）" % path)
    conn = connect(path)
    try:
        names = _table_names(conn)
        missing = [name for name in TABLES if name not in names]
        if missing:
            raise CatalogError("索引库 %s 缺少表 %s：结构已损坏，请删掉索引库后从原始归档重建"
                               % (path.name, "、".join(sorted(missing))))
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            raise CatalogError("索引库 %s 没有版本记录：无法确认它属于本 schema，"
                               "请删掉索引库后从原始归档重建" % path.name)
        if str(row[0]) != str(CATALOG_VERSION):
            if allow_stale_version:
                # 重建判定用：版本不符不再拒绝，交给调用方按原档整批重建（派生层口径）
                return conn
            raise CatalogError("本地索引版本 %s 与当前实现 %s 不一致：%s 只读已提交视图、"
                               "不做半修；本函数手上没有原始归档，无法自动重建——按用户选定的"
                               "范围调用 catalog.index_archive（或 CLI 的 search）从原档重建"
                               "即可恢复检索（索引是派生层，原档不受影响，不必手工删库）"
                               % (row[0], CATALOG_VERSION, path.name))
        return conn
    except CatalogError:
        conn.close()
        raise
    except Exception:
        conn.close()
        raise


# ---------------------------------------------------------------- 归档校验与拆行

def _identity_of(doc, label):
    identity = doc.get("identity")
    peer_type, chat_id = doc.get("peer_type"), doc.get("chat_id")
    _require(isinstance(identity, str) and identity.strip(),
             "归档 %s 缺少身份 identity：请补 metadata（peer_type / chat_id），"
             "不把文件名或显示名猜成账号/聊天身份" % label)
    _require(":" in identity, "归档 %s 的 identity %r 形状不认识（应为 peer_type:chat_id）"
             % (label, identity))
    kind, _, tail = identity.partition(":")
    _require(kind in PEER_TYPES,
             "归档 %s 的 peer_type %r 不受支持（可选 %s）：不猜身份"
             % (label, kind, "、".join(PEER_TYPES)))
    _require(tail.strip().lstrip("-").isdigit(),
             "归档 %s 的 chat_id 部分 %r 不是数字：请补 metadata，不把显示名当身份"
             % (label, tail))
    if peer_type is not None:
        _require(str(peer_type) == kind,
                 "归档 %s 的 peer_type=%s 与 identity=%s 不一致：不代为改写身份"
                 % (label, peer_type, identity))
    if chat_id is not None:
        _require(str(chat_id) == tail.lstrip("-") or str(chat_id) == tail,
                 "归档 %s 的 chat_id=%s 与 identity=%s 不一致：不代为改写身份"
                 % (label, chat_id, identity))
    return identity, kind, int(tail)


def _validated(doc, view, index):
    """把一份冻结归档 dict 校验并拆成可写行；只读输入，绝不改写调用方的对象。"""
    _require(isinstance(doc, dict), "第 %d 份索引来源不是映射(dict)：只接受已归一化的归档"
             % (index + 1))
    label = doc.get("identity") or doc.get("source", {}).get("name") or "第 %d 份" % (index + 1)
    _require(doc.get("kind") == ARCHIVE_KIND,
             "第 %d 份来源的 kind=%r 不是已归一化归档（%s）：不猜文件内容"
             % (index + 1, doc.get("kind"), ARCHIVE_KIND))
    version = doc.get("version")
    _require(version == ARCHIVE_VERSION,
             "归档 schema 版本 %r 与本索引实现支持的 %s 不一致：请重新导入或先做版本迁移，"
             "不按猜的字段建索引" % (version, ARCHIVE_VERSION))
    identity, peer_type, chat_id = _identity_of(doc, label)
    source = doc.get("source") or {}
    _require(isinstance(source, dict) and source.get("batch_id"),
             "归档 %s 缺少 source.batch_id：来源批次必须随事务记录，不能事后靠文件名猜" % label)
    messages = doc.get("messages")
    _require(isinstance(messages, list), "归档 %s 的 messages 必须是列表" % label)
    paths = doc.get("paths") if isinstance(doc.get("paths"), dict) else {}
    archive_path = (doc.get("archive_path") or paths.get("archive_path")
                    or source.get("archive_path"))
    text_index_path = (doc.get("text_index_path") or paths.get("text_index_path")
                       or source.get("text_index_path"))
    coverage = doc.get("coverage") or {}
    chat = doc.get("chat") or {}
    time_block = doc.get("time") or {}
    rows, attachments = [], []
    for item in messages:
        message_id = item.get("message_id")
        _require(isinstance(message_id, int) and not isinstance(message_id, bool),
                 "归档 %s 有消息缺少整数 message_id：不猜归属" % label)
        _require(item.get("identity_key") == "%s#%s" % (identity, message_id),
                 "归档 %s 第 %s 条的 identity_key %r 与身份不符：跨聊天归属不可信，拒绝建索引"
                 % (label, message_id, item.get("identity_key")))
        text = item.get("text")
        _require(text is None or isinstance(text, str),
                 "归档 %s 第 %s 条正文不是字符串：不猜内容" % (label, message_id))
        date_utc = item.get("date_utc")
        _require(date_utc is None or (isinstance(date_utc, int)
                                      and not isinstance(date_utc, bool)),
                 "归档 %s 第 %s 条的 date_utc 不是 UTC 秒整数：不猜时间" % (label, message_id))
        sender = item.get("sender") or {}
        listing = item.get("attachments") or []
        rows.append({"view": view, "identity": identity, "message_id": message_id,
                     "identity_key": item["identity_key"], "date_utc": date_utc,
                     "date_sort": TIME_SENTINEL if date_utc is None else date_utc,
                     "time_missing": _flag(item.get("time_missing")),
                     "display_time": item.get("display_time"), "text": text,
                     "has_text": _flag(text), "text_sha256": _digest(text),
                     "sender_name": sender.get("name"), "sender_id": sender.get("id"),
                     "sender_missing": _flag(sender.get("missing", True)),
                     "reply_to_message_id": (item.get("reply_to") or {}).get("message_id"),
                     "attachment_count": len(listing),
                     "service": _flag(item.get("service")),
                     "action": item.get("action"), "batch_id": source["batch_id"],
                     "archive_path": archive_path,
                     "source_format": source.get("format") or "unknown",
                     "source_sha256": source.get("sha256_before"),
                     "indexed_at": _now()})
        for position, attachment in enumerate(listing):
            attachments.append({"view": view, "identity": identity,
                                "message_id": message_id, "position": position,
                                "category": attachment.get("category") or "unknown",
                                "path": attachment.get("path"),
                                "mime_type": attachment.get("mime_type"),
                                "declared_media_type": attachment.get("declared_media_type"),
                                "width": attachment.get("width"),
                                "height": attachment.get("height"),
                                "duration_seconds": attachment.get("duration_seconds"),
                                "title": attachment.get("title")})
    batch = {"view": view, "batch_id": source["batch_id"], "identity": identity,
             "peer_type": peer_type, "chat_id": chat_id,
             "display_name": chat.get("display_name"),
             "source_path": source.get("path"), "source_name": source.get("name"),
             "source_format": source.get("format") or "unknown",
             "source_sha256": source.get("sha256_before"),
             "processing_version": ARCHIVE_VERSION, "archive_version": ARCHIVE_VERSION,
             "message_count": len(rows),
             "id_lower": coverage.get("id_lower"), "id_upper": coverage.get("id_upper"),
             "date_lower": coverage.get("date_lower"), "date_upper": coverage.get("date_upper"),
             "display_timezone": time_block.get("display_timezone"),
             "utc_field": time_block.get("utc_field"),
             "online_coverage": (doc.get("coverage") or {}).get("online_coverage")
             or "unverified",
             "online_complete": _flag((doc.get("coverage") or {}).get("online_complete")),
             "claim": (doc.get("coverage") or {}).get("claim") or LOCAL_SNAPSHOT_CLAIM,
             "archive_path": archive_path, "text_index_path": text_index_path,
             "content_sha256": None, "indexed_at": _now()}
    chat_row = {"identity": identity, "peer_type": peer_type, "chat_id": chat_id,
                "display_name": chat.get("display_name"),
                "display_name_basis": chat.get("display_name_basis"),
                "identity_basis": json.dumps(doc.get("identity_basis"), ensure_ascii=False)
                if isinstance(doc.get("identity_basis"), dict) else doc.get("identity_basis"),
                "source_format": batch["source_format"], "source_path": batch["source_path"],
                "locator": chat.get("locator"), "batch_id": batch["batch_id"],
                "archive_version": ARCHIVE_VERSION,
                "first_indexed_at": batch["indexed_at"], "updated_at": batch["indexed_at"]}
    return _finalize({"identity": identity, "label": label, "rows": rows,
                      "attachments": attachments,
                      "batch": batch, "chat": chat_row, "reading": doc.get("reading"),
                      "text_missing": sum(1 for item in messages if not item.get("text"))})


def _reading_paths(view, identity, reading, wanted):
    """从阅读版**真实存在的分篇文件**里取消息归属（不解析目录、不编路径）。"""
    entries, unmapped = {}, []
    target = reading.get("target")
    files = reading.get("files") or []
    base = Path(str(target)) if target else None
    for raw in files:
        path = Path(str(raw))
        if not path.is_absolute() and base is not None:
            path = base / path
        if not path.is_file():
            unmapped.append(str(raw))
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unmapped.append(str(raw))
            continue
        for match in READING_FILE_LINE.finditer(body):
            message_id = int(match.group("id"))
            if wanted is not None and message_id not in wanted:
                continue
            entries.setdefault(message_id, path.resolve().as_posix())
    return entries, unmapped


def _reading_membership(reading, identity, label):
    """阅读版成员：优先用分篇里实际出现的消息 ID；其次用调用方给的 kept_ids。"""
    _require(isinstance(reading, dict),
             "聊天 %s 的阅读版信息缺失：view=reading 必须给出该聊天已建好的阅读版"
             "（kept_ids 或 target+files），不把原始正文当成阅读版" % label)
    kept = reading.get("kept_ids")
    files = [str(item) for item in (reading.get("files") or [])]
    _require(kept or files,
             "聊天 %s 的阅读版没有给出保留清单（kept_ids）也没有给出分篇文件（files）："
             "不猜阅读版成员，请补阅读版构建结果" % label)
    wanted = None
    if kept:
        _require(isinstance(kept, (list, tuple))
                 and all(isinstance(item, int) and not isinstance(item, bool) for item in kept),
                 "聊天 %s 的 kept_ids 必须是整数列表" % label)
        wanted = set(kept)
    entries, unmapped = _reading_paths("reading", identity, reading, wanted)
    members = wanted if wanted is not None else set(entries)
    _require(members, "聊天 %s 的阅读版保留清单为空：没有可建的成员（不回落原始正文）" % label)
    return members, entries, unmapped, reading


# ---------------------------------------------------------------- 内容摘要与短路判定
#
# 评审 I-1：「每次 search 都全量重索引 + 独占归档锁」把检索成本顶成 O(选定范围)，
# 还把并发检索顶成退出码 3。这里的口径是：**检索路径默认只读已提交视图**，只有下面
# 三种情况才重建（并只在重建时取归档锁、走事务发布）：
#   1. 本次归档算出来的内容摘要与 `source_batches.content_sha256` 不一致（含范围/视图变化）；
#   2. 索引库缺失或 schema 版本不认识（版本不符照旧抛 CatalogError 申报重建）；
#   3. 调用方显式 `rebuild=True`。
# 判定本身不写一字节、不取锁；报告里的 `reindexed` 与 `reindex_reason` 就是这条短路的
# 可读凭据，`database_status` 也把每批的 `content_sha256` 一起交出来供核对。
#
# `_CONTENT_COLUMNS` 列出每张表里真正承载内容的列；每次必然变化的时间戳列
# （indexed_at / first_indexed_at / updated_at）一律排除，否则「没改内容也永远算不一致」。

_VOLATILE_COLUMNS = ("indexed_at", "first_indexed_at", "updated_at")
_CONTENT_COLUMNS = {
    "messages": ("message_id", "identity_key", "date_utc", "date_sort", "time_missing",
                 "display_time", "text_sha256", "has_text", "sender_name", "sender_id",
                 "sender_missing", "reply_to_message_id", "attachment_count", "service",
                 "action", "batch_id", "archive_path", "source_format", "source_sha256"),
    "attachments": ("message_id", "position", "category", "path", "mime_type",
                    "declared_media_type", "width", "height", "duration_seconds", "title"),
    "reading_paths": ("message_id", "path", "target", "kind"),
    "chats": ("peer_type", "chat_id", "display_name", "display_name_basis", "identity_basis",
              "source_format", "source_path", "locator", "batch_id", "archive_version"),
    # content_sha256 本身不进摘要（它就是这份摘要）；其余登记值都算内容。
    "source_batches": ("batch_id", "peer_type", "chat_id", "display_name", "source_path",
                       "source_name", "source_format", "source_sha256", "processing_version",
                       "archive_version", "message_count", "id_lower", "id_upper",
                       "date_lower", "date_upper", "display_timezone", "utc_field",
                       "online_coverage", "online_complete", "claim", "archive_path",
                       "text_index_path"),
}


def _plain(value):
    """压成「写进 SQLite 再读回来形状不变」的规范值（列亲和性会把 800.0 存成 800 等）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _project(table, row):
    return [_plain(row[key]) for key in _CONTENT_COLUMNS[table]]


def _blocks_digest(blocks):
    """把「每张表要写哪些行」规范化成一个 SHA-256（行的先后不参与比较）。"""
    normalized = {name: sorted([list(row) for row in blocks[name]],
                               key=lambda row: json.dumps(row, ensure_ascii=False))
                  for name in sorted(blocks)}
    return _digest(json.dumps(normalized, sort_keys=True, ensure_ascii=False))


def _prepared_blocks(item):
    entries = item.get("reading_entries") or {}
    return {"messages": [_project("messages", row) for row in item["rows"]],
            "attachments": [_project("attachments", row) for row in item["attachments"]],
            "reading_paths": [[message_id, path, item.get("reading_target"), "part"]
                              for message_id, path in sorted(entries.items())],
            "chats": [_project("chats", item["chat"])],
            "source_batches": [_project("source_batches", item["batch"])]}


def _finalize(item):
    """盖上内容摘要：这一批**实际会写进正式表的全部内容**（含阅读入口与聊天登记）。

    v2 起这份摘要覆盖消息行、附件、阅读入口、聊天登记与批次登记值，所以「阅读版成员被
    收紧」也必然算作内容变了——摘要短路绝不能把隐私收紧吃掉。
    """
    item["batch"]["content_sha256"] = _blocks_digest(_prepared_blocks(item))
    return item


def _already_published(conn, view, item):
    """这一批是否已经原样躺在当前视图里（三条索引化查询，不逐行重扫内容）。

    推理链：发布是「同一事务里整批替换该聊天的行 + 盖这条批次台账」，二者不会分开；
    所以「台账里这批的摘要 == 本次摘要」+「该聊天当前的行全部来自这批」+「行数与批次归属
    相符」三者同时成立时，视图里的内容就是本次要写的内容，发布是纯 no-op。
    把旧批次再灌一次（当前行其实来自另一批）会被第二条查询抓出来重建。
    """
    batch_id = item["batch"]["batch_id"]
    ledger = conn.execute("SELECT identity, content_sha256, message_count FROM source_batches"
                          " WHERE view = ? AND batch_id = ?", (view, batch_id)).fetchone()
    if ledger is None:
        return False, "来源批次台账里没有这一批：%s（内容摘要尚未登记）" % batch_id
    if ledger["identity"] != item["identity"]:
        return False, ("批次 %s 的台账归属是 %s，与本次的 %s 不同"
                       % (batch_id, ledger["identity"], item["identity"]))
    if str(ledger["content_sha256"]) != item["batch"]["content_sha256"]:
        return False, ("source_batches.content_sha256 与本次归档的内容摘要不一致（批次 %s）"
                       % batch_id)
    rows = conn.execute("SELECT count(*) AS total, sum(batch_id = ?) AS mine FROM messages"
                        " WHERE view = ? AND identity = ?",
                        (batch_id, view, item["identity"])).fetchone()
    total = int(rows["total"] or 0)
    mine = int(rows["mine"] or 0)
    if total != len(item["rows"]) or mine != total:
        return False, ("该聊天当前的行不是这一批的（视图 %s、聊天 %s：已提交 %s 行，其中来自"
                       "批次 %s 的 %s 行，本次要写 %s 行）"
                       % (view, item["identity"], total, batch_id, mine, len(item["rows"])))
    if int(ledger["message_count"]) != len(item["rows"]):
        return False, "批次台账记的消息数与本次不符（批次 %s）" % batch_id
    return True, ""


def _reindex_decision(conn, view, items):
    """只读判定：返回 (是否要重建, 原因)。判定本身不写一字节、不取锁。"""
    if not items:
        return True, "本次选定范围内没有任何要写的内容：按用户选定的范围建立"
    stored = {row[0] for row in conn.execute(
        "SELECT DISTINCT identity FROM source_batches WHERE view = ?", (view,))}
    wanted = {item["identity"] for item in items}
    if not stored:
        return True, "视图 %s 还没有任何已提交内容：按用户选定的范围建立" % view
    if stored != wanted:
        return True, ("本次选定的聊天范围与已提交清单不同（已提交：%s；本次：%s）"
                      % ("、".join(sorted(stored)), "、".join(sorted(wanted))))
    for item in items:
        published, reason = _already_published(conn, view, item)
        if not published:
            return True, reason
    return False, ("内容摘要与已提交视图逐批一致（source_batches.content_sha256 全部对上）："
                   "未重写任何行，也未取归档锁")


def _needs_rebuild(database, view, items, rebuild):
    """检索默认只读已提交视图；只有这里判定要重建时才走事务发布（评审 I-1 裁决①②）。

    版本不符的形状按总控裁决改成**自动重建**：索引是可重建派生层，只要本次手上有原档
    （``items`` 已经过 ``_validated`` 校验），落后/被篡改的 schema 版本就整批重建，不再
    拒绝读写。真正拒绝的是「手上没有原档」的读路径（``search_archive``/``database_status``）
    与结构损坏（缺表）——那些地方重建无从下手，只能如实申报原因。
    """
    if rebuild:
        return (True, "调用方显式要求重建（rebuild=True）：整批重写已选定范围", "unknown")
    if not database.is_file() or database.stat().st_size == 0:
        return (True, "索引库不存在或还没有内容：按用户选定的范围建立", "unknown")
    conn = _open_read(database, allow_stale_version=True)
    try:
        stored = str(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'"
                                  ).fetchone()[0])
        if stored != str(CATALOG_VERSION):
            return (True, "索引库记的 schema 版本 %s 与当前实现 %s 不一致：索引是可重建派生层，"
                          "本次自动从原档整批重建（原始归档只读、未被改动，不必手工删库）"
                          % (stored, CATALOG_VERSION), _journal_mode(conn))
        reindexed, reason = _reindex_decision(conn, view, items)
        return reindexed, reason, _journal_mode(conn)
    finally:
        conn.close()


def _skipped_counts(items):
    """短路时报告的就是已提交视图里已有的内容数——只有逐行核对一致才允许走这条路。"""
    return {"chats": len({item["identity"] for item in items}),
            "messages": sum(len(item["rows"]) for item in items),
            "batches": len({item["batch"]["batch_id"] for item in items}),
            "attachments": sum(len(item["attachments"]) for item in items),
            "reading_entries": sum(len(item.get("reading_entries") or {}) for item in items),
            "revisions_added": 0, "unchanged_rows": sum(len(item["rows"]) for item in items),
            "removed_rows": 0, "duplicate_rows_ignored": 0}


# ---------------------------------------------------------------- 写入（staging → 发布）

def _insert(conn, table, row):
    columns = ", ".join(row.keys())
    marks = ", ".join("?" for _item in row)
    conn.execute("INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (table, columns, marks),
                 list(row.values()))


def _stage_one(conn, prepared, stats):
    identity = prepared["identity"]
    view = prepared["batch"]["view"]
    for row in prepared["rows"]:
        existing = conn.execute("SELECT text_sha256, batch_id FROM stage_messages "
                                "WHERE view = ? AND identity = ? AND message_id = ?",
                                (view, identity, row["message_id"])).fetchone()
        if existing is not None:
            if existing["text_sha256"] == row["text_sha256"]:
                stats["duplicate_rows_ignored"] += 1
            else:
                bucket = stats["conflicts"].setdefault(identity, {"message_ids": [],
                                                                  "kept_batch_id":
                                                                  existing["batch_id"]})
                bucket["message_ids"].append(row["message_id"])
                stats["duplicate_rows_ignored"] += 1
            continue                                  # 先给的来源为准，不静默覆盖
        _insert(conn, "stage_messages", row)
        stats["rows_staged"] += 1
    for row in prepared["attachments"]:
        _insert(conn, "stage_attachments", row)
    _insert(conn, "stage_chats", prepared["chat"])
    _insert(conn, "stage_source_batches", prepared["batch"])
    for message_id, path in sorted(prepared.get("reading_entries", {}).items()):
        _insert(conn, "stage_reading_paths",
                {"view": view, "identity": identity, "message_id": message_id,
                 "path": path, "target": prepared.get("reading_target"), "kind": "part"})


def _apply_reading(item):
    """把阅读版成员/入口套到已校验的拆分结果上（view=reading 专用，绝不回落原始正文）。"""
    members, entries, unmapped, reading = _reading_membership(
        item["reading"], item["identity"], item["label"])
    item["rows"] = [row for row in item["rows"] if row["message_id"] in members]
    item["attachments"] = [row for row in item["attachments"] if row["message_id"] in members]
    item["batch"]["message_count"] = len(item["rows"])
    item["reading_entries"] = entries
    item["reading_target"] = reading.get("target")
    item["reading_unmapped"] = unmapped
    # 摘要必须在成员过滤**之后**重算：阅读版收紧（kept_ids 变小）要能被判成内容变了，
    # 否则短路会把被收紧掉的正文继续留在 reading 视图里（隐私面）。
    # 评审 N-1：这份摘要覆盖的是**过滤后的成员行集合本身**（含消息身份、正文散列、阅读
    # 入口与批次登记值），所以「同 batch_id、同条数、把某条成员换成另一条」也算内容变了；
    # 少了这一行，隐私收紧就会被短路吃掉（用例
    # test_equal_size_membership_swap_in_one_batch_still_rebuilds）。
    return _finalize(item)


def stage_archive(conn, view, archive):
    """Task 8 的事务写入入口：把**一份**归档校验后写进 staging（不提交、不动当前视图）。

    调用方负责事务边界（``begin`` / ``commit`` / ``abort``）与 ``publish_staged``。
    返回本次写入的统计；校验失败抛 ValueError，调用方回滚即可维持旧的已提交视图。
    """
    prepared = _validated(archive, _checked_view(view), 0)
    if prepared["batch"]["view"] == "reading":
        _apply_reading(prepared)
    stats = {"identity": prepared["identity"], "rows_staged": 0, "duplicate_rows_ignored": 0,
             "conflicts": {}, "reading_unmapped": prepared.get("reading_unmapped", [])}
    _stage_one(conn, prepared, stats)
    return stats


def publish_staged(conn, view):
    """把 staging 整批换成当前视图（同事务内）：内容变了才追加 revisions。

    只影响 ``view`` 指定的那一侧视图；本次未出现的聊天保持原样。发布后清空该视图的
    staging 行，Task 8 可以先 stage 多份、再一次发布。
    """
    view = _checked_view(view)
    identities = [row["identity"] for row in conn.execute(
        "SELECT DISTINCT identity FROM stage_source_batches WHERE view = ? ORDER BY identity",
        (view,))]
    counts = {"chats": 0, "messages": 0, "batches": 0, "attachments": 0,
              "reading_entries": 0, "revisions_added": 0, "unchanged_rows": 0,
              "removed_rows": 0}
    for identity in identities:
        staged = [dict(row) for row in conn.execute(
            "SELECT * FROM stage_messages WHERE view = ? AND identity = ? "
            "ORDER BY message_id", (view, identity))]
        current = {row["message_id"]: dict(row) for row in conn.execute(
            "SELECT * FROM messages WHERE view = ? AND identity = ?", (view, identity))}
        staged_by_id = {item["message_id"]: item for item in staged}
        stamp = _now()
        for message_id, before in sorted(current.items()):
            after = staged_by_id.get(message_id)
            if after is None:
                reason = "removed_from_batch"
            elif after["text_sha256"] != before["text_sha256"]:
                reason = "content_changed"
            else:
                counts["unchanged_rows"] += 1
                continue
            conn.execute("INSERT INTO revisions (view, identity, message_id, "
                         "previous_batch_id, new_batch_id, old_text, old_date_utc, "
                         "old_sender_name, old_text_sha256, old_archive_path, reason, "
                         "superseded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (view, identity, message_id, before["batch_id"],
                          # 变更行记**实际取代它的那一批**；被移除的行（after is None）没有
                          # 取代它的那一批，按 NULL 记「删除证据」——该语义的完整台账归 Task 8。
                          after["batch_id"] if after is not None else None,
                          before["text"], before["date_utc"], before["sender_name"],
                          before["text_sha256"], before["archive_path"], reason, stamp))
            counts["revisions_added"] += 1
            if after is None:
                counts["removed_rows"] += 1
        conn.execute("DELETE FROM messages WHERE view = ? AND identity = ?", (view, identity))
        conn.execute("DELETE FROM attachments WHERE view = ? AND identity = ?",
                     (view, identity))
        conn.execute("DELETE FROM reading_paths WHERE view = ? AND identity = ?",
                     (view, identity))
        for row in staged:
            _insert(conn, "messages", row)
        counts["messages"] += len(staged)
        for row in conn.execute("SELECT * FROM stage_attachments WHERE view = ? AND identity = ?",
                                (view, identity)):
            _insert(conn, "attachments", dict(row))
            counts["attachments"] += 1
        for row in conn.execute(
                "SELECT * FROM stage_reading_paths WHERE view = ? AND identity = ?",
                (view, identity)):
            _insert(conn, "reading_paths", dict(row))
            counts["reading_entries"] += 1
        chat = conn.execute("SELECT * FROM stage_chats WHERE identity = ?", (identity,)).fetchone()
        if chat is not None:
            existing = conn.execute("SELECT first_indexed_at FROM chats WHERE identity = ?",
                                    (identity,)).fetchone()
            payload = dict(chat)
            payload["first_indexed_at"] = (existing["first_indexed_at"] if existing
                                           else payload["first_indexed_at"])
            _insert(conn, "chats", payload)
            counts["chats"] += 1
    for row in conn.execute("SELECT * FROM stage_source_batches WHERE view = ? ORDER BY batch_id",
                            (view,)):
        _insert(conn, "source_batches", dict(row))
        counts["batches"] += 1
    for name in ("messages", "attachments", "source_batches", "reading_paths"):
        conn.execute("DELETE FROM stage_%s WHERE view = ?" % name, (view,))
    if identities:
        conn.execute("DELETE FROM stage_chats WHERE identity IN (%s)"
                     % ", ".join("?" for _i in identities), identities)
    conn.execute("INSERT INTO meta (key, value) VALUES ('updated_at', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (_now(),))
    return counts


def _write_index(database, view, items):
    """真正重建时的那一次事务：stage → publish → commit，任何一步失败都整体回滚。"""
    conn = connect(database)
    unmapped, mode = [], "unknown"
    rebuilt_from = None
    totals = {"rows_staged": 0, "duplicate_rows_ignored": 0}
    conflicts = {}
    try:
        # 结构损坏 / 版本不符都会在这里如实处理：版本不符走自动重建（见 ensure_schema），
        # 缺表之类结构损坏才抛 CatalogError，且此时未写一字节。
        rebuilt_from = ensure_schema(conn)["rebuilt_from"]
        mode = _journal_mode(conn)
        begin(conn)
        for item in items:
            stats = {"identity": item["identity"], "rows_staged": 0,
                     "duplicate_rows_ignored": 0, "conflicts": {},
                     "reading_unmapped": item.get("reading_unmapped", [])}
            _stage_one(conn, item, stats)
            totals["rows_staged"] += stats["rows_staged"]
            totals["duplicate_rows_ignored"] += stats["duplicate_rows_ignored"]
            for identity, bucket in stats["conflicts"].items():
                conflicts.setdefault(identity, bucket)
            unmapped.extend(stats["reading_unmapped"])
        counts = publish_staged(conn, view)
        commit(conn)
    except CatalogError:
        abort(conn)
        raise
    except sqlite3.Error as error:
        abort(conn)
        raise CatalogError("索引写入失败，事务已整体回滚：旧的已提交视图仍然可用（%s: %s）。"
                           "原始归档未被改动，可从原档重建索引" % (type(error).__name__, error))
    except Exception:
        abort(conn)
        raise
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    counts["duplicate_rows_ignored"] = totals["duplicate_rows_ignored"]
    return counts, conflicts, unmapped, mode, rebuilt_from


def index_archive(database, archives, view, rebuild=False, lock_root=None):
    """按用户选定的归档建立/刷新指定视图的本地索引；整批一次事务，失败回滚留旧视图。

    ``archives`` 是 Task 5 冻结的归档 dict 列表（可带 ``archive_path`` /
    ``text_index_path``，``view="reading"`` 时还需带该聊天已建好的 ``reading``）。
    视图必须显式选择；空清单直接拒绝——索引范围是用户的选择，不做无条件全量重建。

    评审 I-1 的裁决：**默认只读已提交视图**。只有下面三种情况才重建（并只在这时候取归档锁）：
    ``source_batches.content_sha256`` 与本次归档的内容摘要不一致（含范围/视图/行数变化，
    以及**同批次等量成员替换**——摘要覆盖成员集合本身，见 :func:`_apply_reading`）、
    索引库缺失或 ``schema_version`` 与当前实现不符（版本不符**自动从原档整批重建**，
    报告里 ``schema.rebuilt_from`` 记下它是从哪个版本重建的；手上没有原档的读路径才拒绝
    并如实申报原因）、或调用方显式 ``rebuild=True``。短路成立时一个字节都不写、也不取锁
    ——报告里的 ``reindexed`` 与 ``reindex_reason`` 就是这条判定的可读凭据。真正的增量
    合并策略仍归 Task 8。

    ``lock_root`` 是归档锁的**状态根**：锁根与被锁的资源都是它，所以导入侧
    （``importing.import_sources`` 锁同一个派生层目录）与索引/重建侧算出的锁文件完整路径
    逐字相同（评审 N-2）。CLI 传 ``<destination>/derived``；不给就退化成索引库所在目录，
    只与同样传该目录的一方互斥。调用方已经持有同一把归档锁时 :func:`acquire_index_lock`
    沿用它的锁，不重复占用。
    """
    view = _checked_view(view)
    docs = list(archives) if isinstance(archives, (list, tuple)) else []
    _require(docs, "索引范围必须是非空的归档清单：只建用户选定的部分，不自动全建、"
                   "也不递归磁盘")
    prepared = [_validated(item, view, position) for position, item in enumerate(docs)]
    excluded, gaps = [], []
    for item in prepared:
        if view != "reading":
            continue
        if item["reading"] is None:
            excluded.append(item["identity"])
            gaps.append({"identity": item["identity"],
                         "reason": "未选定该聊天的阅读版：不拿原始正文冒充阅读版，本次跳过"})
            continue
        _apply_reading(item)
    if len(excluded) == len(prepared):
        raise ValueError("view=reading 但选定的归档里没有任何聊天带阅读版构建结果（%s）："
                         "不猜阅读版成员，请先建阅读版或改查 raw 视图"
                         % "、".join(item["identity"] for item in prepared))

    database = Path(str(database))
    items = [item for item in prepared if item["identity"] not in excluded]
    reindexed, reason, mode = _needs_rebuild(database, view, items, rebuild)
    if reindexed:
        handle = acquire_index_lock(lock_root or database.parent)
        try:
            counts, conflicts, unmapped, mode, rebuilt_from = _write_index(database, view,
                                                                           items)
        finally:
            release_index_lock(handle)
    else:
        counts, conflicts, unmapped = _skipped_counts(items), {}, []
        rebuilt_from = None
    seen, ordered = set(), []
    for item in prepared:
        if item["identity"] not in excluded and item["identity"] not in seen:
            seen.add(item["identity"])
            ordered.append(item["identity"])
    return {"kind": CATALOG_KIND, "version": CATALOG_VERSION, "status": "complete",
            "view": view, "database": str(database),
            "reindexed": reindexed, "reindex_reason": reason,
            "schema": {"version": CATALOG_VERSION, "archive_version": ARCHIVE_VERSION,
                       "rebuilt_from": rebuilt_from,
                       "tables": list(TABLES), "staging_tables": list(STAGE_TABLES),
                       "identity_key": "<peer_type>:<chat_id>#<message_id>",
                       "primary_key": "(view, identity, message_id)"},
            "scope": {"identities": ordered, "excluded": excluded,
                      "batches": sorted({item["batch"]["batch_id"] for item in prepared}),
                      "selected_sources": len(docs),
                      "note": "索引范围来自用户选定的归档清单，不无条件全建"},
            "counts": counts,
            "gaps": {"overlap_conflicts": [{"identity": key,
                                            "message_ids": sorted(value["message_ids"]),
                                            "kept_batch_id": value["kept_batch_id"],
                                            "reason": "同一 (视图, 聊天, 消息 ID) 出现内容不同的"
                                                      "两份来源：以先给的来源为准并申报，"
                                                      "不静默覆盖"}
                                           for key, value in sorted(conflicts.items())],
                     "missing_reading_entry": gaps,
                     "missing_reading_files": sorted(unmapped),
                     "text_missing": sum(item["text_missing"] for item in prepared)},
            "claims": {"derived_layer_rebuildable": True, "archive_read_only": True,
                       "network_used": False, "login_used": False, "tdl_invoked": False,
                       "local_snapshot_only": True, "online_full_export_verified": False,
                       "claim": LOCAL_SNAPSHOT_CLAIM, "fts_enabled": False,
                       "chinese_tokenizer_installed": False,
                       "wildcards_treated_as_literal": True,
                       "case_folding": "ascii-only",
                       "note": "合成/本地导入的索引与检索通过，不代表真实在线导出已验证"},
            "runtime": {"backend": "sqlite3(标准库)", "journal_mode": mode,
                        "transaction": ("BEGIN IMMEDIATE + staging + 整批替换" if reindexed
                                        else "只读短路：未开写事务、未取归档锁"),
                        "archive_lock": ("本次重建取归档锁（state.acquire_archive_lock 那一份"
                                         "实现；调用方已持有时沿用同一把，不重复占用）"
                                         if reindexed else "本次没有写入，不取归档锁"),
                        "tdl_required": False, "session_required": False,
                        "offline_action": True},
            "limitations": {"literal_matching": "关键词走 instr 字面子串，未启用全文分词",
                            "reading_membership": "阅读版成员取自分篇里实际出现的消息 ID；"
                                                  "视图里的正文取自原始归档，不是阅读版逐字排版",
                            "schema_version_mismatch": "版本不符时旧版本派生表整批丢弃、"
                                                       "按本次选定的原档重建；只读路径"
                                                       "（search_archive/database_status）手上"
                                                       "没有原档，仍拒绝并申报重建动作",
                            "single_writer": "同一索引库的写入按归档锁串行，读侧只看已提交视图",
                            "incremental_merge": "本轮只消除「每次全量重建」：判定用内容摘要，"
                                                 "真正的增量合并与水位对账归 Task 8",
                            "digest_short_circuit": "短路时不再复检同一次调用内的重复来源"
                                                    "（因为一个字节都不写），也不推进 indexed_at",
                            "no_online_verification": LOCAL_SNAPSHOT_CLAIM}}


# ---------------------------------------------------------------- 查询


# ---------------------------------------------------------------- 查询

def _epoch(value, label):
    if isinstance(value, bool) or value is None:
        raise ValueError("%s 必须是 UTC 秒整数或带时区的 ISO 日期：%r" % (label, value))
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        _require(value.is_integer(), "%s 的秒值不接受小数部分：%r" % (label, value))
        return int(value)
    _require(isinstance(value, str), "%s 类型不认识：%r" % (label, value))
    try:
        moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("%s 不是可解析的 ISO 日期：%s（日期必须带时区，不猜本地时区）"
                         % (label, value)) from None
    _require(moment.tzinfo is not None,
             "%s 必须带时区（避免把用户日期猜成某个时区）：%s" % (label, value))
    return int(moment.timestamp())


def _identity_token(value, label):
    if isinstance(value, str):
        _require(":" in value, "%s 里的身份字符串应为 peer_type:chat_id：%r" % (label, value))
        return value
    if isinstance(value, dict):
        if value.get("identity"):
            return str(value["identity"])
        peer_type, chat_id = value.get("peer_type"), value.get("chat_id")
        _require(peer_type in PEER_TYPES and isinstance(chat_id, int),
                 "%s 里的聊天必须给 identity 或 peer_type + chat_id：%r（不按显示名筛，"
                 "同名聊天会互串）" % (label, value))
        return "%s:%s" % (peer_type, chat_id)
    raise ValueError("%s 的聊天项类型不认识：%r" % (label, value))


def _normalize_query(query):
    _require(isinstance(query, dict), "查询条件必须是映射(dict)：视图/关键词/聊天/日期/ID")
    unknown = sorted(str(key) for key in query if key not in QUERY_KEYS)
    _require(not unknown,
             "查询含未知条件：%s（可用：%s）——不接受自由 SQL，也不猜默认"
             % ("、".join(unknown), "、".join(sorted(QUERY_KEYS))))
    view = _checked_view(query.get("view"))
    normalized = {"view": view}
    keyword = query.get("keyword")
    if keyword is not None:
        _require(isinstance(keyword, str) and keyword != "",
                 "keyword 必须是非空字符串（按字面匹配，不作为模式解释）")
        normalized["keyword"] = keyword
    normalized["case_sensitive"] = bool(query.get("case_sensitive", False))
    chats = query.get("chats")
    if chats is not None:
        _require(isinstance(chats, (list, tuple)) and chats,
                 "chats 必须是非空列表（按 peer_type + chat_id 筛，不按显示名筛）")
        normalized["chats"] = [_identity_token(item, "chats") for item in chats]
    for key in ("since", "until"):
        if query.get(key) is not None:
            normalized[key] = _epoch(query[key], key)
    for key in ("id_lower", "id_upper"):
        if query.get(key) is not None:
            value = query[key]
            _require(isinstance(value, int) and not isinstance(value, bool) and value >= 0,
                     "%s 必须是非负整数消息 ID：%r" % (key, value))
            normalized[key] = value
    if "message_ids" in query:
        ids = query["message_ids"]
        _require(isinstance(ids, (list, tuple)) and ids
                 and all(isinstance(item, int) and not isinstance(item, bool) for item in ids),
                 "message_ids 必须是非空整数列表")
        normalized["message_ids"] = sorted(set(ids))
    order = query.get("order", "asc")
    _require(order in ("asc", "desc"), "order 只能是 asc 或 desc：%r" % (order,))
    normalized["order"] = order
    limit = query.get("limit", DEFAULT_LIMIT)
    _require(isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= MAX_LIMIT,
             "limit 必须是 1 到 %s 的整数：%r" % (MAX_LIMIT, limit))
    normalized["limit"] = limit
    if "id_lower" in normalized and "id_upper" in normalized:
        _require(normalized["id_lower"] <= normalized["id_upper"],
                 "id_lower=%s 不能大于 id_upper=%s（区间写反了一律拒绝，不静默交换）"
                 % (normalized["id_lower"], normalized["id_upper"]))
    return normalized


def _fingerprint(normalized):
    return _digest(json.dumps(normalized, sort_keys=True, ensure_ascii=False))


def _encode_cursor(normalized, row):
    payload = {"v": CATALOG_VERSION, "q": _fingerprint(normalized),
               "key": [row["date_sort"], row["identity"], row["message_id"]]}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor, normalized):
    _require(isinstance(cursor, str) and cursor.strip(), "cursor 必须是字符串游标：%r"
             % (cursor,))
    raw = cursor.strip().encode("ascii", "ignore")
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
                             .decode("utf-8"))
    except (ValueError, UnicodeDecodeError, TypeError):
        raise ValueError("游标无法解析：它必须是上一次同一次查询返回的 next_cursor") from None
    _require(isinstance(payload, dict) and payload.get("q") == _fingerprint(normalized)
             and isinstance(payload.get("key"), list) and len(payload["key"]) == 3,
             "游标不属于本次查询条件（换了关键词/聊天/日期/排序就要从头再查）")
    return payload["key"]


def _where(normalized, cursor_key):
    clauses, params = ["messages.view = ?"], [normalized["view"]]
    keyword = normalized.get("keyword")
    if keyword is not None:
        if normalized["case_sensitive"]:
            clauses.append("instr(messages.text, ?) > 0")
            params.append(keyword)
        else:
            # 两条比较都走 instr + 绑定参数：通配符与引号都是普通字符，永不被解释
            clauses.append("(instr(messages.text, ?) > 0 OR instr(lower(messages.text), "
                           "lower(?)) > 0)")
            params += [keyword, keyword]
    if normalized.get("chats"):
        clauses.append("messages.identity IN (%s)" % ", ".join("?" for _i in normalized["chats"]))
        params += normalized["chats"]
    if normalized.get("since") is not None:
        clauses.append("messages.date_utc >= ?")
        params.append(normalized["since"])
    if normalized.get("until") is not None:
        clauses.append("messages.date_utc < ?")
        params.append(normalized["until"])
    if normalized.get("id_lower") is not None:
        clauses.append("messages.message_id >= ?")
        params.append(normalized["id_lower"])
    if normalized.get("id_upper") is not None:
        clauses.append("messages.message_id <= ?")
        params.append(normalized["id_upper"])
    if normalized.get("message_ids"):
        clauses.append("messages.message_id IN (%s)"
                       % ", ".join("?" for _i in normalized["message_ids"]))
        params += normalized["message_ids"]
    if cursor_key is not None:
        direction = ">" if normalized["order"] == "asc" else "<"
        sort_key, identity, message_id = cursor_key
        clauses.append("(messages.date_sort %s ?"
                       " OR (messages.date_sort = ? AND messages.identity %s ?)"
                       " OR (messages.date_sort = ? AND messages.identity = ?"
                       " AND messages.message_id %s ?))" % (direction, direction, direction))
        params += [sort_key, sort_key, identity, sort_key, identity, message_id]
    return " AND ".join(clauses), params


def _snippet(text, keyword, case_sensitive, limit):
    if not text:
        return "", False
    position = -1
    if keyword:
        needle = text if case_sensitive else text.lower()
        hay = keyword if case_sensitive else keyword.lower()
        position = needle.find(hay)
    start = 0 if position < 1 else max(0, position - (limit // 3))
    body = text[start:]
    if len(body) > limit:
        return body[:limit - 1] + "…", True
    return body, False


def search_archive(database, query):
    """只读已提交视图的本地检索：关键词按字面、过滤聊天/日期/消息 ID、稳定分页。"""
    normalized = _normalize_query(query)
    conn = _open_read(database)
    try:
        cursor_key = _decode_cursor(query["cursor"], normalized) if query.get("cursor") else None
        count_where, count_params = _where(normalized, None)
        total = conn.execute("SELECT count(*) FROM messages WHERE %s" % count_where,
                             list(count_params)).fetchone()[0]
        page_where, page_params = _where(normalized, cursor_key)
        remaining = conn.execute("SELECT count(*) FROM messages WHERE %s" % page_where,
                                 list(page_params)).fetchone()[0]
        order = "ASC" if normalized["order"] == "asc" else "DESC"
        rows = conn.execute("SELECT * FROM messages WHERE %s ORDER BY date_sort %s, "
                            "identity %s, message_id %s LIMIT ?"
                            % (page_where, order, order, order),
                            list(page_params) + [normalized["limit"]]).fetchall()
        results = [_row_to_result(conn, row, normalized) for row in rows]
        absent = []
        for identity in normalized.get("chats") or []:
            found = conn.execute("SELECT 1 FROM messages WHERE view = ? AND identity = ? LIMIT 1",
                                 (normalized["view"], identity)).fetchone()
            if found is None:
                absent.append(identity)
        views = [str(row[0]) for row in conn.execute(
            "SELECT DISTINCT view FROM source_batches ORDER BY view")]
        mode = _journal_mode(conn)
        next_cursor = (_encode_cursor(normalized, rows[-1])
                       if len(rows) == normalized["limit"] and remaining > len(rows) else None)
    finally:
        conn.close()
    keyword = normalized.get("keyword")
    return {"kind": SEARCH_KIND, "version": CATALOG_VERSION, "status": "complete",
            "database": str(database), "view": normalized["view"], "total": total,
            "results": results,
            "page": {"limit": normalized["limit"], "has_more": next_cursor is not None,
                     "next_cursor": next_cursor,
                     "sort": ["date_sort", "identity", "message_id"], "order":
                     normalized["order"],
                     "note": "排序键含身份与消息 ID，同值也稳定；游标与查询条件绑定"},
            "query_echo": {"view": normalized["view"], "keyword": keyword,
                           "keyword_length": len(keyword or ""),
                           "case_sensitive": normalized["case_sensitive"],
                           "chats": normalized.get("chats") or [], "absent_chats": absent,
                           "since": normalized.get("since"), "until": normalized.get("until"),
                           "id_lower": normalized.get("id_lower"),
                           "id_upper": normalized.get("id_upper"),
                           "message_ids": normalized.get("message_ids")},
            "views_indexed": views,
            "claims": {"literal_matching": True, "wildcards_treated_as_literal": True,
                       "query_executed_as_code": False, "fts_enabled": False,
                       "chinese_tokenizer_installed": False,
                       "external_search_service": False, "network_used": False,
                       "upload_performed": False, "case_folding": "ascii-only",
                       "view_isolation_enforced": True,
                       "reading_excludes_filtered_original": True,
                       "no_fabricated_links": True,
                       "snippet_max_chars": SNIPPET_MAX_CHARS,
                       "committed_view_only": True},
            "runtime": {"backend": "sqlite3(标准库)", "offline_action": True,
                        "tdl_required": False, "session_required": False,
                        "journal_mode": mode,
                        "note": "search 是离线动作：没有 tdl / storage / namespace 也能用"}}


def _row_to_result(conn, row, normalized):
    text = row["text"]
    snippet, truncated = _snippet(text, normalized.get("keyword"),
                                  normalized["case_sensitive"], SNIPPET_MAX_CHARS)
    attachments = [{"category": item["category"], "path": item["path"],
                    "mime_type": item["mime_type"], "width": item["width"],
                    "height": item["height"]} for item in conn.execute(
        "SELECT * FROM attachments WHERE view = ? AND identity = ? AND message_id = ? "
        "ORDER BY position", (row["view"], row["identity"], row["message_id"]))]
    # 阅读入口只认 view='reading' 登记的那批行（评审 I-3：查询必须带视图谓词，否则别的
    # 视图塞进来的同键路径会跨视图串进结果）。raw 的结果**允许**指向真实存在的阅读入口——
    # 那是定位特性，不是内容泄漏：入口本身不含正文，正文仍按各自视图的行返回。
    reading_row = conn.execute(
        "SELECT path FROM reading_paths WHERE view = 'reading' AND identity = ?"
        " AND message_id = ? AND kind = 'part' ORDER BY path LIMIT 1",
        (row["identity"], row["message_id"])).fetchone()
    chat = conn.execute("SELECT display_name, peer_type, chat_id FROM chats WHERE identity = ?",
                        (row["identity"],)).fetchone()
    batch = conn.execute("SELECT text_index_path, source_path FROM source_batches WHERE view = ?"
                         " AND batch_id = ?", (row["view"], row["batch_id"])).fetchone()
    return {"view": row["view"], "identity": row["identity"],
            "peer_type": row["identity"].split(":")[0], "chat_id": chat["chat_id"] if chat
            else None, "display_name": (chat["display_name"] if chat else None),
            "message_id": row["message_id"], "identity_key": row["identity_key"],
            "date_utc": row["date_utc"], "display_time": row["display_time"],
            "time_missing": bool(row["time_missing"]),
            "sender": None if row["sender_missing"] else row["sender_name"],
            "sender_missing": bool(row["sender_missing"]),
            "snippet": snippet, "snippet_truncated": truncated,
            "snippet_max_chars": SNIPPET_MAX_CHARS, "text_length": len(text or ""),
            "has_text": bool(row["has_text"]), "service": bool(row["service"]),
            "action": row["action"], "reply_to_message_id": row["reply_to_message_id"],
            "attachments": attachments, "attachment_count": row["attachment_count"],
            "batch_id": row["batch_id"], "source_format": row["source_format"],
            "source_sha256": row["source_sha256"],
            "entries": {"archive": row["archive_path"],
                        "text_index": batch["text_index_path"] if batch else None,
                        "source": batch["source_path"] if batch else None,
                        "reading": reading_row["path"] if reading_row else None},
            "locator": "%s / 第 %d 条" % (row["identity"], row["message_id"])}


def database_status(database):
    """索引库自检：版本、视图成员数、批次与修订条数；不可用时申报重建。"""
    conn = _open_read(database)
    try:
        views = {}
        for view in VIEWS:
            counts = {}
            for label, sql in (("messages", "messages"), ("chats", "messages"),
                               ("batches", "source_batches"), ("attachments", "attachments"),
                               ("revisions", "revisions"), ("reading_entries",
                                                             "reading_paths")):
                if label == "chats":
                    row = conn.execute("SELECT count(DISTINCT identity) FROM messages "
                                       "WHERE view = ?", (view,)).fetchone()
                else:
                    row = conn.execute("SELECT count(*) FROM %s WHERE view = ?" % sql,
                                       (view,)).fetchone()
                counts[label] = row[0]
            counts["coverage"] = [dict(row) for row in conn.execute(
                "SELECT batch_id, identity, message_count, id_lower, id_upper, online_coverage"
                ", claim, content_sha256 FROM source_batches WHERE view = ?"
                " ORDER BY identity, batch_id",
                (view,))]
            views[view] = counts
        meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()
    return {"kind": STATUS_KIND, "version": CATALOG_VERSION, "database": str(database),
            "schema_version": int(meta.get("schema_version", CATALOG_VERSION)),
            "archive_version": int(meta.get("archive_version", ARCHIVE_VERSION)),
            "claim": meta.get("claim"), "derived_layer": True,
            "journal_mode": mode, "tables": list(TABLES) + list(STAGE_TABLES),
            "views": views, "updated_at": meta.get("updated_at"),
            "reindex": {"digest_field": "source_batches.content_sha256",
                        "coverage_carries_digest": True,
                        "note": "再建同一范围时先比这份摘要：一致就只读短路（报告里 "
                                "reindexed=false + reindex_reason），不一致或范围/视图变了才"
                                "重建并取归档锁；显式 rebuild=True 强制重建"},
            "claims": {"network_used": False, "archive_read_only": True,
                       "note": "索引是派生层：删掉后可从原始归档重建，不代表在线全量已验证"}}


def list_revisions(database, identity=None, message_id=None, view=None, limit=DEFAULT_LIMIT):
    """Task 8 的回溯入口：读某个 (视图, 聊天, 消息) 的历史版本，只读已提交内容。"""
    conn = _open_read(database)
    try:
        clauses, params = ["1=1"], []
        if view is not None:
            clauses.append("view = ?")
            params.append(_checked_view(view))
        if identity is not None:
            clauses.append("identity = ?")
            params.append(_identity_token(identity, "identity"))
        if message_id is not None:
            _require(isinstance(message_id, int) and not isinstance(message_id, bool),
                     "message_id 必须是整数：%r" % (message_id,))
            clauses.append("message_id = ?")
            params.append(message_id)
        _require(isinstance(limit, int) and not isinstance(limit, bool)
                 and 1 <= limit <= MAX_LIMIT, "limit 必须是 1 到 %s 的整数" % MAX_LIMIT)
        rows = conn.execute("SELECT * FROM revisions WHERE %s ORDER BY revision_id DESC LIMIT ?"
                            % " AND ".join(clauses), list(params) + [limit]).fetchall()
    finally:
        conn.close()
    return {"kind": "telegram-archive-revisions", "version": CATALOG_VERSION,
            "filters": {"view": view, "identity": identity, "message_id": message_id},
            "revisions": [{"view": row["view"], "identity": row["identity"],
                           "message_id": row["message_id"],
                           "previous_batch_id": row["previous_batch_id"],
                           "new_batch_id": row["new_batch_id"], "old_text": row["old_text"],
                           "old_date_utc": row["old_date_utc"],
                           "old_sender_name": row["old_sender_name"],
                           "old_text_sha256": row["old_text_sha256"],
                           "reason": row["reason"], "superseded_at": row["superseded_at"],
                           "revision_id": row["revision_id"]} for row in rows]}
