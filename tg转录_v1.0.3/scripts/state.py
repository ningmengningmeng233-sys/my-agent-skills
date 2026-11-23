"""持久化分段与可信完成状态（Task 2）。

合同要点（设计第 45–47、106、110–112 行，工单第 84–89 行）：

* 每段独立落盘：导出先写临时文件，校验通过才原子替换进 segments/ 并在完成清单里
  登记 SHA-256；绝不只在内存里保存成功段，所以进程退出/重启后能复用已验收段。
* 跳过一段必须同时满足「清单在册 + JSON 可解析且结构合法 + 文件摘要一致 + 范围与
  任务身份匹配」，文件存在或 JSON 能解析都不构成完成凭据；篡改会被检出。
* 缺段绝不发布正式 final：merge_completed 抛 ValueError 且不创建目标；不齐只能 partial。
* 任务固定快照上界与配置摘要；恢复旧任务沿用原上界与原选择，计划/摘要/身份不符时
  拒绝复用而不是静默跳过。合并按消息 ID 去重只在同一 peer 身份内生效。
* status 只读聚合，绝不启动或恢复任何子进程，并把「文字索引 / 阅读版 / 媒体」三类
  完成状态分别报告；整体 complete 只在所选输出全部验收后才成立。
* 子进程一律用参数列表调用（绝不拼接 shell 字符串）；网络/限流类失败按 retries 有限
  重试，授权/会话类失败停整轮并如实报告（同一 job 的后续聊天不再启动，标
  unavailable 并点名前序身份），不进重试循环。
* 本轮进度凭据写入各聊天的状态报告（progress）：完成样本耗时只覆盖本轮实际完成
  的段，ETA 在样本不足（<2）时标记未知；time 模式按 UTC 日期窗口做本地二次校验，
  窗外消息在提交前剔除，缺日期的保留并计数。
* 会话互斥按「每任务」而非每聊天取锁：一个 job 里所有聊天共用一把锁，锁根由执行者配置
  决定（显式 lock_root > destination/locks），未配 lock_root 时互斥面不足必须在
  check/status 的能力报告里申报，不许沉默。固定加锁顺序：先归档锁、后会话锁。
* 出错时保留已验收成功段与诊断记录：本轮诊断的整体重置只在持锁后进行，且只清自己的
  记录；段文件已落盘但清单发布失败的崩溃窗口，启动时按摘要与范围复认领，不重导覆盖。

证据边界：本模块只做本地状态与调度。假后端通过不代表真实 Telegram 导出成功；真实
文字导出与媒体下载在获得本次具体授权前均为 NOT_RUN。

ID 区间端点：任务侧区间按合同为「两端包含」（ID_ENDPOINT_SEMANTICS）；后端旗标面以本机
帮助副本 work/upstream/tdl-help-0.20.4.txt 的 `tdl chat export` 段为唯一权威，该副本与
gotdgen 类型定义都没有说明端点包含性，因此记为 unknown（ID_ENDPOINT_EVIDENCE），防御方式
是按设计第 110 行让相邻段边界重叠一个 ID 并按 peer + 消息 ID 去重。
"""

import copy
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from job import chat_identity, job_fingerprint, validate_job

STATE_VERSION = 1
SEGMENT_SUBDIR = "segments"
TMP_SUBDIR = "tmp"
LOCK_SUBDIR = "locks"
STATE_DIR_NAME = "telegram-state"
TASK_NAME = "task.json"
MANIFEST_NAME = "manifest.json"
STATUS_NAME = "status.json"
FAILURE_NAME = "failures.json"
OUTPUT_NAME = "outputs.json"
FINAL_SUFFIX = "-messages.json"
# Task 3 的媒体子目录：状态根下的内部索引落在这里，附件产物落在用户所选保存目录下。
MEDIA_SUBDIR = "media"

# I5：异常路径遗留的临时文件治理。同一账号存储同时只有一个归档任务持会话锁，超过这个
# 年龄仍未被复用的 *.part 只可能是崩溃残留。
STALE_PART_MAX_AGE_SECONDS = 3600
_PART_SIDECAR = re.compile(r"^(?P<base>.+?)\.part-(?P<pid>\d+)$")
TEMP_SCAN_SUBDIRS = (TMP_SUBDIR, MEDIA_SUBDIR)
STATE_FILE_NAMES = (TASK_NAME, MANIFEST_NAME, STATUS_NAME, FAILURE_NAME, OUTPUT_NAME)

# 工单第 88 行是两把不同的锁：在线操作用「会话锁」，search/import/share/update 的组合用
# 「归档锁」。本模块只实现会话锁，文件名一律 session-*；archive-* 前缀留给 Task 5–8 的
# 归档锁，两把锁绝不同名、绝不互相顶替。
# 固定加锁顺序：先归档锁、后会话锁（Task 8 的 update 同时需要两把锁时按此顺序取，全局同序
# 才能防死锁）；释放顺序与取锁顺序相反。
LOCK_ORDER = ("archive", "session")
LOCK_PREFIXES = {"archive": "archive-", "session": "session-"}

# 互斥面申报（合同第 83 行：存储路径等运行参数由执行者配置，不成为用户内容选择）。
MUTEX_CAPABILITY = "session_mutex_across_destinations"
MUTEX_AFFECTED_ACTIONS = ("export", "download", "update")
# lock_root 指向会话存储目录时的互斥面标记：既不是 account 也不是 destination，
# 组合层据此按「配置非法＝退出码 2」处理（见 archive.py 的 _cmd_check）。
LOCK_SCOPE_REJECTED = "rejected"
MUTEX_GAP_REASON = (
    "未配置执行者运行参数 lock_root：会话锁只落在 destination/locks/ 下，同一账号存储"
    "（storage+namespace）换用不同 destination 的两个任务互相看不见，跨 destination 不"
    "互斥，「同一账号存储同时只跑一个归档任务」未在默认配置下兑现。把 lock_root 配到"
    "账号级公共可写目录（不得是 tdl 会话目录）后，互斥才覆盖该账号的所有保存目录。"
    "受影响动作：export、download、update。")
MUTEX_OK_REASON = (
    "已配置执行者运行参数 lock_root：同一 storage+namespace 的任意 destination 共用一把"
    "会话锁，跨保存目录互斥成立。受影响动作：export、download、update。")
MUTEX_SESSION_CONFLICT_REASON = (
    "执行者运行参数 lock_root 与账号存储（登录会话）目录相同或互相包含：合同禁止写入或重置"
    "登录会话目录，也禁止把该位置外泄到输出，因此本次配置按「配置非法」拒绝，不启动任何子"
    "进程。请把 lock_root 配到会话目录之外的账号级公共可写目录。受影响动作：export、"
    "download、update。")

# 任务侧区间约定与后端参数端点的证据状态（见模块 docstring 的证据边界）。
ID_ENDPOINT_SEMANTICS = "inclusive"
ID_ENDPOINT_EVIDENCE = "unknown"
ENDPOINT_NOTE = (
    "任务侧 ID 区间按合同为两端包含（lower/upper 均包含），相邻段边界重叠一个 ID 后按消息 "
    "ID 去重，所以后端无论哪一端包含都不会因边界丢消息。后端侧：本机 tdl 0.20.4 的帮助副本 "
    "work/upstream/tdl-help-0.20.4.txt 里 `tdl chat export` 的旗标面是 -c/--chat、"
    "-T/--type [time,id,last]（默认 time）、-i/--input ints、-o/--output、--all、--raw、"
    "--with-content、-f/--filter、--reply、--topic，另有全局 -n/--ns、--storage、--proxy；"
    "work/upstream/attributes.go、work/upstream/document.go、work/upstream/media.go 只是 "
    "gotdgen 生成的 MessageMedia*/Document*/DocumentAttribute* 类型定义（如 media.go:136 "
    "MessageMediaPhoto）。两份材料都没有说明 ID/时间区间的端点是否包含，故端点包含性记为 "
    "unknown，需 Task 3 依对应版本源码或小样本核对后才能宣称与后端一致；核对前不启动任何"
    "导出子进程。")

# 本地合成/规范化导出的可识别形状：消息数组，或含 messages 数组的映射。真实 tdl 导出
# 字段名由 Task 3/5 依 work/upstream 证据固定，这里不臆造 tdl schema，只按这两种形状
# 取消息 ID，并在文件自己声明 chat 身份时强制核对。
MESSAGE_ID_KEYS = ("id", "message_id")
DECLARED_CHAT_KEYS = ("chat_id", "dialog_id")
# Task 6 真实反例：活体 tdl 0.20.4 chat export 顶层身份键为 id（{"id": <chat>,
# "messages": [...]}）。只有含 messages 数组的顶层映射才认 id 为 chat 身份；
# 逐条消息自带 id=消息 ID，绝不能走这套键（见 _declared_top_chat_id 与
# _declared_chat_id 的分工），DECLARED_CHAT_KEYS 原语义不动。
TDL_TOP_ID_KEYS = ("chat_id", "dialog_id", "id")
OUTPUT_KINDS = ("text_index", "reading", "media")
STATUSES = ("pending", "running", "complete", "partial", "failed", "unavailable")
RETRYABLE_KINDS = ("network", "rate_limit")
STOPPING_KINDS = ("authorization",)
CAPABILITY_KINDS = ("insufficient_source", "lock_busy", "lock_release")
FLOOD_WAIT_CAP_SECONDS = 30
DEFAULT_SEGMENT_WIDTH = 5000
PLAN_LOWER_BOUND = 1
# W2（设计方案第 114 行）：ETA 只在完成样本≥2 时按均值×剩余段数给出，否则标记未知。
MIN_PROGRESS_SAMPLES_FOR_ETA = 2
# W4：time 窗口的日期键与 reading.DATE_KEYS 同口径（数值按 UTC 秒，字符串按 ISO）。
WINDOW_DATE_KEYS = ("date", "timestamp")
# 锁文件只允许带这些任务信息：会话目录、命名空间与凭据一律不写（marker 已是散列）。
_OWNER_KEYS = ("pid", "identity", "label")

_RATE_LIMIT_MARKER = re.compile(r"flood_wait_(\d+)", re.I)
_NETWORK_MARKERS = ("timed out", "timeout", "connection reset", "connection refused",
                    "connection aborted", "temporarily unavailable", "temporary failure",
                    "network is unreachable", "no such host", "i/o timeout", "eof",
                    "pong missed", "broken pipe", "tls handshake", "server closed")
_AUTHORIZATION_MARKERS = ("unauthorized", "auth_key_invalid", "auth_key_unregistered",
                          "auth_key_duplicated", "not authorised", "phone_number_invalid",
                          "phone_number_unregistered", "session_revoke", "session_terminated",
                          "invalid session", "invalid access token", "auth error")


class ExportError(Exception):
    """导出失败；kind 区分网络/限流（可有限重试）与授权/会话（停止任务）。"""

    def __init__(self, message, kind=None):
        super().__init__(str(message))
        self.kind = kind if kind in ("network", "rate_limit", "authorization", "unknown") \
            else classify_failure(str(message))


class LockBusy(Exception):
    """会话互斥：同一账号存储同时只允许一个归档任务。

    带锁文件路径，让「由人处理陈旧锁」这件运维动作真的可执行（报告 §9.4）。
    """

    def __init__(self, holder, marker, path=None):
        self.holder = holder
        self.marker = marker
        self.path = str(path) if path else None
        where = "（锁文件：%s）" % self.path if self.path else ""
        super().__init__("同一账号存储已有归档任务在运行，不并发写同一会话%s" % where)


_MISSING = object()
# ---------------------------------------------------------------- 基础设施

def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_json(path, default=_MISSING):
    path = Path(path)
    if not path.is_file():
        if default is _MISSING:
            raise ValueError("状态文件不存在：%s（没有清单/任务快照就不宣称完成）" % path.name)
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError("状态文件 %s 不是合法 JSON：%s" % (path.name, error))


def _mkdir_tracked(path, created):
    """建目录并记下「本次真的新建了哪几层」，失败时才能原样撤掉（不留空目录）。"""
    if path.is_dir():
        return
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    path.mkdir(parents=True, exist_ok=True)
    created.extend(reversed(missing))


def _restore_published(replaced, pending, created, problems):
    """回滚：已换出去的文件按备份复原、本次新建的文件删掉、侧车清掉、空目录撤掉。"""
    for final, old in reversed(replaced):
        try:
            if old is None:
                if final.is_file():
                    final.unlink()
                continue
            with open(final, "wb") as handle:
                handle.write(old)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            problems.append("%s 未能恢复旧内容：%s" % (final.name, error))
    for side, _final in pending:
        try:
            if side.exists():
                side.unlink()
        except OSError as error:
            problems.append("%s 未能清掉侧车：%s" % (side.name, error))
    for directory in reversed(created):
        try:
            directory.rmdir()
        except OSError:
            pass                       # 目录非空（别人的内容）就留着，绝不清空


def publish_text_files(directory, files):
    """本仓库**唯一一份**原子发布实现：先写侧车，全部写成功后再逐个 ``os.replace``。

    ``Task 4 评审 I-2/I-7`` 的根因是 ``reading._publish`` 另写了一份同名不同语义的发布：
    侧车登记晚于写入（写失败就漏在盘上）、replace 失败没有回滚（新旧混成一套视图）。
    这里一次做对，state 自己的状态文件与阅读版的分篇/目录/清单都走这一份：

    * 侧车在 ``open`` **之前**登记，写入阶段失败也清得掉；
    * 每条 replace 前先留旧字节备份，失败时整批回滚——旧文件复原、本次新建的文件删掉、
      残留侧车清掉、本次新建的空目录撤掉，绝不留下「新分篇 + 旧目录」的混合视图；
    * 回滚自身失败不掩盖原始异常：问题清单挂在 ``error.publish_rollback_problems`` 上，
      由调用方（``run_render`` 的里程碑）如实申报；
    * 一律 UTF-8 + 显式 LF（``newline="\\n"``），与「登记摘要/体积只看实际落盘字节」同口径。

    ``files`` 是 ``{相对路径: 文本内容}``；返回 ``{"published", "restored", "created"}``。
    """
    base = Path(directory)
    staged = []
    created = []
    replaced = []
    for name in files:
        raw = str(name)
        relative = PurePosixPath(raw)
        windows = PureWindowsPath(raw)
        if relative.is_absolute() or windows.is_absolute() or ".." in relative.parts:
            raise ValueError("发布清单只收根目录内的相对路径，收到：%s" % raw)
    try:
        for name, content in files.items():
            final = base / PurePosixPath(str(name))
            _mkdir_tracked(final.parent, created)
            side = final.with_name("%s.part-%d" % (final.name, os.getpid()))
            staged.append((side, final))          # 先登记再写：写一半也清得掉
            with open(side, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        for side, final in staged:
            old = final.read_bytes() if final.is_file() else None
            os.replace(side, final)
            replaced.append((final, old))
    except OSError as error:
        problems = []
        _restore_published(replaced, staged[len(replaced):], created, problems)
        if problems:
            error.publish_rollback_problems = problems
        raise
    return {"published": [str(name) for name in files], "restored": len(replaced),
            "created": [str(item) for item in created]}


def _publish_json(path, payload):
    """临时文件 + os.replace：清单/任务/状态要么是旧版本要么是新版本。

    显式 LF 落盘（newline="\\n"）：跨任务缺陷裁决要求全仓一套口径——登记摘要/体积的
    地方绝不出现「平台文本换行」与「另一套文本重算」两份字节。原子写与回滚一律委托
    ``publish_text_files``（唯一实现），本函数只负责序列化。
    """
    path = Path(path)
    document = json.dumps(payload, ensure_ascii=False, indent=2)
    publish_text_files(path.parent, {path.name: document})
    return path


def identity_slug(identity):
    """把 peer 身份变成安全的目录/文件名片段；显示名不参与路径。"""
    return re.sub(r"[^0-9A-Za-z_.-]+", "-", str(identity)).strip("-") or "chat"


def task_state_root(destination, identity):
    """归档身份（peer 类型 + chat ID）决定的状态根目录。"""
    return Path(destination) / STATE_DIR_NAME / identity_slug(identity)


def _absolute_path(value):
    """路径入参闸门：非空绝对路径才接受（与 job.py 的 _PATH_KEYS 同口径）。

    配置里读到的 lock_root 只会是字符串（JSON），程序化调用可传 PathLike；其它类型一律拒。
    """
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not _text(value):
        return None
    path = PurePath(value)
    return path if path.is_absolute() and len(path.parts) > 1 else None


def default_lock_root(destination):
    """未配置 lock_root 时的会话锁基准目录：用户所选归档根目录本身。

    锁实际落在 `<destination>/locks/` 下（由 acquire_lock 追加 LOCK_SUBDIR）。基准目录
    一律来自配置的 destination，绝不从状态根做几何倒推——倒推会把锁 mkdir 到不相干的
    目录甚至盘根，而且换保存目录就互相看不见。
    """
    return Path(destination)


def _trimmable_by_windows(value):
    """原始字符串结尾的点或空格会被 Windows 静默剥掉，必须先按原样校验再落盘。

    不校验就会让「看起来是 A、实际写到 A 的另一个形状」这类路径进入闸门；这里只判定
    形状，不回显路径本身。
    """
    try:
        parts = PurePath(value).parts
    except (TypeError, ValueError):
        return True
    return any(part != part.rstrip(" .") for part in parts)


def inside_session_storage(lock_root, storage):
    """lock_root 与账号存储（登录会话）目录相同、在其之内或包住它——三种都算冲突。

    判定用 resolve() + relative_to，绝不用字符串前缀：前缀会把「C:\\备份」说成
    「C\\备份-别的」的同族，也会因大小写与分隔符差异误判合法锁根。
    """
    first, second = _absolute_path(lock_root), _absolute_path(storage)
    if first is None or second is None:
        return False
    resolved = (Path(str(first)).resolve(), Path(str(second)).resolve())
    for outer, inner in ((resolved[0], resolved[1]), (resolved[1], resolved[0])):
        try:
            inner.relative_to(outer)
            return True
        except ValueError:
            continue
    return False


def _segment_id(chat_id, lower, upper):
    return "%012d-%012d-%012d" % (chat_id, lower, upper)


def _segment_identity(segment):
    peer_type = segment.get("peer_type")
    if peer_type is None:
        return None
    return chat_identity({"peer_type": peer_type, "id": segment.get("chat_id")})


def _require_task(root):
    task = _read_json(Path(root) / TASK_NAME, None)
    if not isinstance(task, dict) or "identity" not in task:
        raise ValueError("任务快照未初始化：%s 缺少 %s（先建立冻结上界与身份，再写状态）"
                         % (Path(root).name, TASK_NAME))
    return task


def _task_manifest(root):
    manifest = _read_json(Path(root) / MANIFEST_NAME, None)
    if manifest is None:
        return {"version": STATE_VERSION, "updated_at": None, "identity": None,
                "segments": {}, "final": None}
    if not isinstance(manifest, dict) or not isinstance(manifest.get("segments"), dict):
        raise ValueError("完成清单结构不合法，无法判定完成状态")
    return manifest


def _failures(root):
    items = _read_json(Path(root) / FAILURE_NAME, None)
    return items if isinstance(items, list) else []


def _failure_owner(record):
    """诊断归属：段级记录按段名，任务级记录按 kind；写入与重置都按它归并。"""
    segment_id = record.get("segment_id")
    owner = record.get("owner")
    if _text(owner):
        return owner
    return "segment:%s" % segment_id if segment_id else "task:%s" % record.get("kind")


def _record_failure(root, record):
    """归并写诊断：同名 owner 覆盖自己上一条，绝不清掉别人的记录（合同第 89 行）。"""
    path = Path(root) / FAILURE_NAME
    record = dict(record, owner=_failure_owner(record))
    items = [item for item in _failures(root) if _failure_owner(item) != record["owner"]]
    items.append(record)
    _publish_json(path, items)
    return items


def _drop_failure(root, segment_id):
    owner = "segment:%s" % segment_id
    items = [item for item in _failures(root) if _failure_owner(item) != owner]
    _publish_json(Path(root) / FAILURE_NAME, items)
    return items


# 任务级（不挂段）诊断：本轮重跑时由自己名下的旧记录让位给新结论。
_OWNED_TASK_KINDS = ("insufficient_source", "lock_busy", "lock_release", "unknown")


def _reset_run_diagnostics(root, segments):
    """重置本轮诊断：只清本任务名下的记录，其它任务的诊断一条都不动。

    必须在持有会话锁之后调用；被锁拒绝的运行绝不写这里（否则排障者看不到真凶）。
    """
    owned = {"segment:%s" % item["segment_id"] for item in segments}
    owned.update("task:%s" % kind for kind in _OWNED_TASK_KINDS)
    items = [item for item in _failures(root) if _failure_owner(item) not in owned]
    _publish_json(Path(root) / FAILURE_NAME, items)
    return items


# ---------------------------------------------------------------- 任务身份与计划

def _as_export_config(data):
    probe = dict(data)
    probe["action"] = "export"
    return probe


def export_fingerprint(job):
    """任务身份摘要：按导出动作归一，使 export 与 resume 指向同一个任务。"""
    return job_fingerprint(_as_export_config(validate_job(job)))


def account_marker(job):
    """账号存储的互斥标记：只落散列，会话目录与命名空间原值不进锁文件名或锁内容。"""
    data = validate_job(job)
    seed = "%s|%s" % (data.get("storage") or "", data.get("namespace") or "")
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _single_chat(data):
    chats = data["chats"]
    if len(chats) != 1:
        raise ValueError("状态根按单个 peer+chat 建档（多聊天由组合层逐个建档，不合并身份）")
    entry = chats[0]
    return chat_identity(entry), entry["peer_type"], entry["id"]


def _declared_bounds(data):
    """(分段下界, 冻结上界)：只取本次配置里已冻结的值，绝不现场探测。"""
    scope = data["range"]
    snapshot = data.get("snapshot") or {}
    upper = snapshot.get("upper_bound")
    if upper is None and scope["mode"] == "id":
        upper = scope["upper"]
    if not _positive_int(upper):
        raise ValueError("无法确定冻结的快照上界：非 ID 范围必须显式给出 snapshot.upper_bound"
                         "（恢复与导出都不得重新探测上界）")
    lower = scope["lower"] if scope["mode"] == "id" else PLAN_LOWER_BOUND
    if scope["mode"] == "id" and scope["upper"] > upper:
        raise ValueError("range.upper %s 越过本次冻结的快照上界 %s（不得扩大范围）"
                         % (scope["upper"], upper))
    return lower, upper


def _peer_plan(chat_id, peer_type, lower, upper, width):
    return [dict(segment, peer_type=peer_type)
            for segment in plan_segments(chat_id, lower, upper, width)]


def plan_segments(chat_id, lower, upper, width):
    """把 [lower, upper]（两端包含）按宽度 width 切成相邻段，边界重叠一个 ID。

    返回的段只带 chat_id 与区间：peer 类型由组合层按用户选中的清单条目填入
    （plan 不臆造 peer 类型），segment_id 用零填充数值，保证文件名稳定可排序。
    """
    if not _positive_int(chat_id):
        raise ValueError("chat_id 必须是正整数（归档身份的一部分，不由显示名代替）")
    if not _positive_int(lower):
        raise ValueError("lower 必须是包含起点的正整数消息 ID")
    if not _positive_int(upper):
        raise ValueError("upper 必须是包含终点的正整数消息 ID")
    if lower > upper:
        raise ValueError("lower 不得大于 upper（ID 区间两端均包含）")
    if not _positive_int(width):
        raise ValueError("segment_width 必须是正整数（分段宽度，最小 1）")
    segments, start, index = [], lower, 0
    while True:
        end = min(start + width, upper)
        segments.append({"index": index, "chat_id": chat_id, "peer_type": None,
                         "lower": start, "upper": end,
                         "segment_id": _segment_id(chat_id, start, end),
                         "endpoint": ID_ENDPOINT_SEMANTICS})
        index += 1
        if end >= upper:
            break
        start = end
    return segments


def init_task(root, job):
    """建立或复用任务快照：身份 -> 冻结上界 -> 配置摘要，任一不符即拒绝复用。"""
    data = validate_job(job)
    identity, peer_type, chat_id = _single_chat(data)
    lower, upper = _declared_bounds(data)
    width = data.get("segment_width", DEFAULT_SEGMENT_WIDTH)
    fingerprint = export_fingerprint(data)
    root = Path(root)
    existing = _read_json(root / TASK_NAME, None)
    if existing is None:
        task = {"version": STATE_VERSION, "identity": identity, "peer_type": peer_type,
                "chat_id": chat_id, "fingerprint": fingerprint,
                "range": copy.deepcopy(data["range"]), "contents": list(data["contents"]),
                "selected_outputs": list(data["outputs"]), "cleaning": data.get("cleaning"),
                "destination": data["destination"], "segment_width": width,
                "retries": data.get("retries", 0), "plan_lower": lower,
                "lock_root": data.get("lock_root"),
                "snapshot_upper_bound": upper, "endpoint": ID_ENDPOINT_SEMANTICS,
                "endpoint_evidence": ID_ENDPOINT_EVIDENCE,
                "plan": _peer_plan(chat_id, peer_type, lower, upper, width),
                "created_at": _utc_now(), "updated_at": _utc_now()}
        (root / SEGMENT_SUBDIR).mkdir(parents=True, exist_ok=True)
        (root / TMP_SUBDIR).mkdir(parents=True, exist_ok=True)
        _publish_json(root / TASK_NAME, task)
        if not (root / MANIFEST_NAME).is_file():
            _publish_json(root / MANIFEST_NAME,
                          {"version": STATE_VERSION, "updated_at": task["created_at"],
                           "identity": identity, "segments": {}, "final": None})
        return copy.deepcopy(task)
    if existing.get("identity") != identity:
        raise ValueError("状态根记录的归档身份是 %s，本次是 %s：拒绝复用他人状态"
                         % (existing.get("identity"), identity))
    if existing.get("snapshot_upper_bound") != upper:
        raise ValueError("任务已冻结快照上界 %s，本次带来 %s：恢复旧任务沿用原上界，"
                         "不重新探测扩大范围" % (existing.get("snapshot_upper_bound"), upper))
    if existing.get("fingerprint") != fingerprint:
        raise ValueError("配置摘要与任务记录不一致（业务选择或运行配置已变化）："
                         "请建立新任务，不复用旧完成状态")
    return copy.deepcopy(existing)


def load_task(root):
    return copy.deepcopy(_require_task(root))


def plan_task_segments(root, job):
    """返回任务冻结的分段计划；本次配置算出的计划不一致时拒绝复用。"""
    data = validate_job(job)
    task = init_task(root, data)
    _lower, upper = _declared_bounds(data)
    if upper != task["snapshot_upper_bound"]:
        raise ValueError("本次上界 %s 与任务冻结的 %s 不一致（不扩大范围）"
                         % (upper, task["snapshot_upper_bound"]))
    width = data.get("segment_width", DEFAULT_SEGMENT_WIDTH)
    recomputed = _peer_plan(task["chat_id"], task["peer_type"], task["plan_lower"],
                            task["snapshot_upper_bound"], width)
    if recomputed != task["plan"]:
        raise ValueError("分段计划与任务冻结的计划不一致（宽度或范围已变化），拒绝复用旧完成状态")
    return copy.deepcopy(task["plan"])


# ---------------------------------------------------------------- 导出校验与逐段提交

def _message_id(item, position):
    for key in MESSAGE_ID_KEYS:
        if key in item:
            value = item[key]
            if not _positive_int(value):
                raise ValueError("第 %d 条消息的 %s 必须是正整数消息 ID" % (position, key))
            return value
    raise ValueError("第 %d 条消息缺少正整数消息 ID（id 或 message_id）" % position)


def _declared_chat_id(payload):
    for key in DECLARED_CHAT_KEYS:
        if payload.get(key) is not None:
            return payload.get(key)
    return None


def _declared_top_chat_id(payload):
    """顶层载荷（含 messages 数组的映射）声明的 chat 身份：只在这里认 id。

    逐条消息的身份检查仍用 _declared_chat_id（消息自带 id 是消息 ID，误判会把
    每条消息的身份都读错）；非正整数的声明值原样返回，由调用方的身份核对
    （declared != chat_id）拒绝，不静默当成「未声明」放过。
    """
    for key in TDL_TOP_ID_KEYS:
        if payload.get(key) is not None:
            return payload.get(key)
    return None


def validate_export(path, chat_id, lower, upper):
    """校验导出文件：存在、JSON 合法、结构可识别、消息 ID 均在包含端点区间内。

    只按两种可指认形状取消息（消息数组 / 含 messages 数组的映射），文件若自带 chat
    身份则必须与任务一致；零条消息是合法结果（空范围），但完成凭据由 commit_segment
    写入的清单提供，不是由「文件存在」提供。
    """
    path = Path(path)
    if not path.is_file():
        raise ValueError("导出文件不存在或不是文件：%s" % path.name)
    raw = path.read_bytes()
    if not raw.strip():
        raise ValueError("导出文件为空，不是合法的 JSON 导出：%s" % path.name)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("导出文件 JSON 解析失败（%s）：%s" % (type(error).__name__, path.name))
    if isinstance(payload, list):
        messages, declared = payload, None
    elif isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        messages, declared = payload["messages"], _declared_top_chat_id(payload)
    else:
        raise ValueError("无法识别的导出结构：需要消息数组或含 messages 数组的映射"
                         "（不臆造 tdl 导出 schema）")
    if declared is not None and declared != chat_id:
        raise ValueError("导出文件声明的 chat 身份 %s 与任务身份 %s 不符" % (declared, chat_id))
    found = []
    for position, item in enumerate(messages, start=1):
        if not isinstance(item, dict):
            raise ValueError("第 %d 条消息必须是映射(dict)，不能是标量或列表" % position)
        message_id = _message_id(item, position)
        if not lower <= message_id <= upper:
            raise ValueError("第 %d 条消息 ID %s 超出段区间 [%s, %s]（两端包含）"
                             % (position, message_id, lower, upper))
        inner = _declared_chat_id(item)
        if inner is not None and inner != chat_id:
            raise ValueError("第 %d 条消息声明的 chat 身份 %s 与任务身份 %s 不符"
                             % (position, inner, chat_id))
        found.append(message_id)
    return {"path": str(path), "name": path.name, "chat_id": chat_id,
            "requested": {"lower": lower, "upper": upper},
            "endpoint": ID_ENDPOINT_SEMANTICS,
            "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
            "count": len(messages), "ids": ({"lower": min(found), "upper": max(found)}
                                            if found else None),
            "empty": not messages, "declared_chat_id": declared, "messages": messages}


def segment_file(root, segment):
    return Path(root) / SEGMENT_SUBDIR / ("%s.json" % segment["segment_id"])


def _segment_record(segment, identity, target, report):
    """完成清单里的一条段记录（commit_segment 与崩溃复认领共用同一形状）。"""
    return {"segment_id": segment["segment_id"], "identity": identity,
            "chat_id": segment["chat_id"], "lower": segment["lower"],
            "upper": segment["upper"], "endpoint": ID_ENDPOINT_SEMANTICS,
            "path": target.name, "sha256": report["sha256"], "size": report["size"],
            "count": report["count"], "verified_at": _utc_now()}


def commit_segment(root, segment, temporary):
    """校验临时文件 -> 原子替换进 segments/ -> 原子发布完成清单（每段独立提交）。"""
    root = Path(root)
    task = _require_task(root)
    for key in ("segment_id", "lower", "upper", "chat_id"):
        if key not in segment:
            raise ValueError("段计划缺少字段：%s（不能提交没有范围凭据的段）" % key)
    identity = _segment_identity(segment) or task["identity"]
    if identity != task["identity"]:
        raise ValueError("段身份 %s 与任务身份 %s 不符（拒绝写入他人状态）"
                         % (identity, task["identity"]))
    if segment["chat_id"] != task["chat_id"]:
        raise ValueError("段的 chat ID %s 与任务不符" % segment["chat_id"])
    if segment["upper"] > task["snapshot_upper_bound"]:
        raise ValueError("段上界 %s 越过任务冻结的快照上界 %s（不得扩大范围）"
                         % (segment["upper"], task["snapshot_upper_bound"]))
    report = validate_export(temporary, task["chat_id"], segment["lower"], segment["upper"])
    temporary, target = Path(temporary), segment_file(root, segment)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 写入顺序保持「先替换段文件、后发布清单」：中间崩溃只会留下未登记的合法段，
    # 由 reclaim_orphan_segments 在下次运行时复认领，绝不会把没验完的文件当成完成段。
    os.replace(temporary, target)
    record = _segment_record(segment, identity, target, report)
    manifest = _task_manifest(root)
    manifest["segments"][segment["segment_id"]] = record
    manifest["identity"] = task["identity"]
    manifest["updated_at"] = record["verified_at"]
    _publish_json(root / MANIFEST_NAME, manifest)
    _drop_failure(root, segment["segment_id"])
    return copy.deepcopy(record)


def load_manifest(root):
    path = Path(root) / MANIFEST_NAME
    if not path.is_file():
        raise ValueError("状态根 %s 没有完成清单 %s（无清单不得宣称完成）"
                         % (Path(root).name, MANIFEST_NAME))
    return _task_manifest(path.parent)


def _rejection_reason(root, manifest, segment):
    """完成凭据检查（清单在册、身份与范围一致、结构合法、摘要一致）；返回 None 表示可复用。"""
    segment_id = segment.get("segment_id")
    record = manifest["segments"].get(segment_id)
    if record is None:
        return "完成清单未登记该段（文件存在不等于完成）"
    expected = _segment_identity(segment) or manifest.get("identity")
    if expected is None or record.get("identity") != expected:
        return "任务身份不符：清单记录 %s，本次要求 %s" % (record.get("identity"), expected)
    if record.get("identity") != manifest.get("identity"):
        return "段记录身份 %s 与清单身份 %s 不一致" % (record.get("identity"),
                                                       manifest.get("identity"))
    if (record.get("lower"), record.get("upper")) != (segment.get("lower"), segment.get("upper")):
        return "段范围与清单记录不一致"
    if record.get("chat_id") != segment.get("chat_id"):
        return "段的 chat ID 与清单记录不一致"
    path = root / SEGMENT_SUBDIR / record["path"]
    if not path.is_file():
        return "已完成段文件缺失：%s" % record["path"]
    try:
        report = validate_export(path, record["chat_id"], record["lower"], record["upper"])
    except ValueError as error:
        return "已完成段不再合法：%s" % error
    if report["sha256"] != record["sha256"]:
        return "文件摘要与完成清单不一致（SHA-256）"
    if report["count"] != record["count"]:
        return "消息条数与完成清单不一致"
    return None


def scan_completed(root, segments):
    """逐段判定能否复用；pending 条目带上原因，供拒绝复用时如实报告。"""
    root = Path(root)
    manifest = _task_manifest(root)
    verified, pending = [], []
    for segment in segments:
        reason = _rejection_reason(root, manifest, segment)
        if reason is None:
            verified.append(copy.deepcopy(segment))
        else:
            pending.append(dict(copy.deepcopy(segment), reason=reason))
    return {"verified": verified, "pending": pending, "planned": len(segments)}


def reclaim_orphan_segments(root, task=None):
    """复认领「段文件已落盘且合法、但没登记进完成清单」的段（commit_segment 的崩溃窗口）。

    判定一项不放松：结构仍要过 validate_export、消息 ID 全在段区间内、身份与 chat 与任务
    一致；不合格的孤儿文件不复认领，照旧走重新导出。复认领只补清单记录，绝不改段文件字节，
    所以已验好的原文不会被重导覆盖（合同第 89 行「保留已验收成功段」的实效）。
    """
    root = Path(root)
    task = task if isinstance(task, dict) else _require_task(root)
    manifest = _task_manifest(root)
    reclaimed = []
    for segment in task["plan"]:
        segment_id = segment["segment_id"]
        if segment_id in manifest["segments"]:
            continue
        target = segment_file(root, segment)
        if not target.is_file():
            continue
        identity = _segment_identity(segment) or task["identity"]
        if identity != task["identity"] or segment.get("chat_id") != task["chat_id"]:
            continue
        if segment["upper"] > task["snapshot_upper_bound"]:
            continue
        try:
            report = validate_export(target, task["chat_id"], segment["lower"],
                                     segment["upper"])
        except ValueError:
            continue
        record = dict(_segment_record(segment, identity, target, report), reclaimed_at=_utc_now())
        manifest["segments"][segment_id] = record
        reclaimed.append(record)
    if reclaimed:
        manifest["identity"] = task["identity"]
        manifest["updated_at"] = reclaimed[-1]["verified_at"]
        _publish_json(root / MANIFEST_NAME, manifest)
    return reclaimed


def final_target(root):
    """本任务最终成品的路径：按 peer 身份命名，不使用显示名。"""
    task = _require_task(root)
    return Path(root).parent / ("%s%s" % (identity_slug(task["identity"]), FINAL_SUFFIX))


def _final_body(payload):
    """成品的**内容**读数：去掉发布时刻（merged_at）后按稳定顺序序列化。

    只用来判「两次发布的成品内容是否一样」，不参与落盘字节的计算；元组/列表这类
    JSON 里同形的差别在这一步被抹平，所以比较结果与读回来的旧成品也能对上。
    """
    return json.dumps({key: value for key, value in payload.items() if key != "merged_at"},
                      ensure_ascii=False, sort_keys=True)


def merge_completed(root, segments, target):
    """只在所有段齐全且全部验收时原子发布最终文件；否则抛错且不创建目标。

    final 清单记录带 peer 身份与计划 lower/upper（B-M2：验收闸凭据③要求区间在册），
    size/摘要按实际落盘字节登记（跨任务缺陷裁决：与 validate_export 同一口径）。
    """
    root = Path(root)
    task = _require_task(root)
    manifest = load_manifest(root)
    plan_by_id = {item["segment_id"]: item for item in task["plan"]}
    given = [item.get("segment_id") for item in segments]
    wanted = set(given)
    unknown = sorted({item for item in given if item not in plan_by_id})
    if unknown:
        raise ValueError("段 %s 不在任务冻结计划内，计划不一致时拒绝合并复用" % "、".join(unknown))
    missing = [item for item in plan_by_id if item not in wanted]
    if missing:
        raise ValueError("合并请求缺少计划段：%s（计划不完整，不能发布成品）" % "、".join(missing))
    identities = {(_segment_identity(item) or task["identity"]) for item in segments}
    if len(identities) > 1 or task["identity"] not in identities:
        raise ValueError("一次合并只允许一个 peer 身份（去重只在同一 peer 内生效）：%s"
                         % "、".join(sorted(identities)))
    scanned = scan_completed(root, segments)
    if scanned["pending"]:
        detail = "、".join("%s：%s" % (item["segment_id"], item["reason"])
                           for item in scanned["pending"])
        raise ValueError("存在未验收段，不得发布正式 final：%s" % detail)

    merged, seen, duplicates = [], set(), 0
    for segment in sorted(segments, key=lambda item: item["index"]):
        record = manifest["segments"][segment["segment_id"]]
        report = validate_export(root / SEGMENT_SUBDIR / record["path"], record["chat_id"],
                                 record["lower"], record["upper"])
        for item in report["messages"]:
            key = "%s#%d" % (task["identity"], _message_id(item, 1))
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            merged.append(item)
    merged.sort(key=lambda item: _message_id(item, 1))
    payload = {"kind": "telegram-archive-messages", "version": STATE_VERSION,
               "identity": task["identity"], "peer_type": task["peer_type"],
               "chat_id": task["chat_id"],
               "range": {"lower": min(item["lower"] for item in segments),
                         "upper": max(item["upper"] for item in segments)},
               "snapshot_upper_bound": task["snapshot_upper_bound"],
               "fingerprint": task["fingerprint"], "endpoint": ID_ENDPOINT_SEMANTICS,
               "endpoint_evidence": ID_ENDPOINT_EVIDENCE,
               "segments": [item["segment_id"] for item in sorted(
                   segments, key=lambda entry: entry["index"])],
               "count": len(merged), "messages": merged, "merged_at": _utc_now()}

    target = Path(target)
    if not target.parent.is_dir():
        raise ValueError("最终成品的保存目录不存在：%s（发布不自动创建目录）" % target.parent.name)
    published = manifest.get("final")
    if target.is_file():
        current = _digest(target)
        same_task = (published and published.get("sha256") == current
                     and Path(published.get("path", "")) == target)
        if not same_task:
            raise ValueError("目标 %s 已存在且不是本任务已验收发布的成品，不覆盖用户文件"
                             % target.name)
        # 复发布同一条成品时**逐字节幂等**：merged_at 是发布时刻，不是内容，内容没变就沿用
        # 原来那一份。否则「再合并一次」会因两次调用跨了一秒而产出不同的摘要——评审要的
        # idempotent 性质在字面上不成立，任何按摘要对账的后续任务（Task 8）都会把自家
        # 成品判成「被改过」。既有成品读不出（不是合法 JSON）时按新时刻走原路径重写，
        # 不静默复用读不到的时间戳。
        try:
            previous = _read_json(target, None)
        except ValueError:
            previous = None
        if (isinstance(previous, dict) and previous.get("merged_at")
                and _final_body(previous) == _final_body(payload)):
            payload["merged_at"] = previous["merged_at"]
    document = json.dumps(payload, ensure_ascii=False, indent=2)
    temporary = target.with_name("%s.part-%d" % (target.name, os.getpid()))
    # 跨任务缺陷裁决：成品按**实际落盘字节**记 size 与 SHA-256——显式二进制写入，
    # 摘要与体积都从同一份字节算，绝不事后用另一套文本口径重算（Windows 上文本模式
    # 会转 CRLF，让验收闸拒收我们自己刚发布的 final）。
    raw = document.encode("utf-8")
    with open(temporary, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    digest = hashlib.sha256(raw).hexdigest()
    manifest["final"] = {"path": str(target), "identity": task["identity"],
                         "chat_id": task["chat_id"],
                         "lower": payload["range"]["lower"],
                         "upper": payload["range"]["upper"],
                         "endpoint": ID_ENDPOINT_SEMANTICS,
                         "sha256": digest, "size": len(raw),
                         "count": len(merged), "segments": payload["segments"],
                         "published_at": _utc_now()}
    manifest["updated_at"] = manifest["final"]["published_at"]
    _publish_json(root / MANIFEST_NAME, manifest)
    return {"target": str(target), "identity": task["identity"], "count": len(merged),
            "deduplicated": duplicates, "sha256": digest, "size": manifest["final"]["size"],
            "range": payload["range"], "segments": payload["segments"]}


# ---------------------------------------------------- 异常路径遗留的临时文件治理（I5）

def _looks_temporary(name):
    return name.endswith(".part") or bool(_PART_SIDECAR.match(name))


def prune_stale_temp_files(root, *, now=None, extra_dirs=(),
                           max_age_seconds=STALE_PART_MAX_AGE_SECONDS):
    """清理异常路径遗留的 `*.part` / `*.part-<pid>`，每个动作都上报，绝不静默。

    只扫状态根本身、`tmp/`、`media/` 三处与 ``extra_dirs`` 里点名的目录的顶层条目：
    绝不递归到归档目录，绝不删非临时文件。状态文件（manifest/task/…）的 sidecar 只有在
    「正式文件已在」时才算残留；正式文件不在时保留并申报 needs_attention——那可能是唯一的
    清单记录。取锁成功后才调用，被拒的运行不碰别人的半成品。

    ``extra_dirs`` 是给阅读版目录用的（Task 4 评审 M-10）：全仓只有这一套临时文件治理，
    别的模块要回收硬崩溃遗留的 `.part-*` 就扩展这里的扫描面，不再新造第三套清理。
    """
    root = Path(root)
    moment = time.time() if now is None else float(now)
    report = {"scope": str(root), "removed": [], "kept": [], "skipped": [],
              "needs_attention": [], "ignored": []}

    def note(bucket, name, reason, age=None):
        item = {"name": name, "reason": reason}
        if age is not None:
            item["age_seconds"] = round(age, 3)
        report[bucket].append(item)

    scan = [root] + [root / item for item in TEMP_SCAN_SUBDIRS]
    scan.extend(Path(item) for item in extra_dirs)
    for directory in scan:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            name = entry.name
            if not _looks_temporary(name):
                if entry.is_file():
                    note("ignored", name, "不是临时文件，永不删除")
                continue
            if entry.is_dir():
                note("skipped", name, "同名目录：不递归、不删除用户内容")
                continue
            if name in STATE_FILE_NAMES:
                note("skipped", name, "状态文件名，不删")
                continue
            base = _PART_SIDECAR.match(name)
            if base and not (directory / base.group("base")).is_file():
                note("needs_attention", name,
                     "正式状态文件 %s 不在，这份残留可能是唯一记录，保留待人工确认"
                     % base.group("base"))
                continue
            try:
                age = moment - entry.stat().st_mtime
            except OSError as error:
                note("skipped", name, "读不到时间戳：%s" % error)
                continue
            if age < max_age_seconds:
                note("kept", name, "年龄未过闸门，可能仍有任务在写", age)
                continue
            try:
                entry.unlink()
            except OSError as error:
                note("skipped", name, "删除失败：%s" % error, age)
                continue
            note("removed", name, "异常路径遗留的临时文件，已过年龄闸门", age)
    report["counts"] = {key: len(report[key]) for key in
                        ("removed", "kept", "skipped", "needs_attention", "ignored")}
    report["max_age_seconds"] = max_age_seconds
    return report


# TG-01：tdl 子进程单次调用超时（秒）。导出/下载 Runner 在此时间内无退出即
# 判挂起：subprocess.run 自动 kill 并回收子进程，调用方按 network 有限重试，
# 绝不在独占会话锁内无限阻塞。
TDL_SUBPROCESS_TIMEOUT_SECONDS = 600
# TG-01：会话/归档锁租约（秒）。持有者在推进中逐段/逐聊天刷新心跳
# （refresh_lock）；超过租约无刷新，或持有者进程已死，后续任务可安全自动破锁，
# 杜绝崩溃残留锁导致的永久 LockBusy。
LOCK_LEASE_SECONDS = 7200


# ---------------------------------------------------------------- 会话互斥

def _lock_path(root, marker, kind="session"):
    if kind not in LOCK_PREFIXES:
        raise ValueError("互斥锁类型不受支持（仅 %s）：%s"
                         % ("、".join(sorted(LOCK_PREFIXES)), kind))
    return Path(root) / LOCK_SUBDIR / ("%s%s.lock" % (LOCK_PREFIXES[kind], marker))


def _peek_holder(path):
    """读取占用者信息：并发抢锁时文件可能刚被创建还没写完，此时也只报告「已被占」。"""
    try:
        payload = _read_json(path, None)
    except ValueError:
        return {"pid": None, "acquired_at": None, "note": "锁文件正在写入"}
    return payload if isinstance(payload, dict) else {"pid": None, "acquired_at": None}


def _pid_alive(pid):
    """持有者进程是否存活：True 存活 / False 已死 / None 无法判定。

    posix 用 kill(pid, 0)；Windows 上 sig 0 不校验存活，改用 ctypes
    OpenProcess + GetExitCodeProcess（STILL_ACTIVE=259 表示仍在跑）。任何探测
    异常都按未知处理（租约路径兜底），绝不因探测失败而误破活锁。
    """
    if not _positive_int(pid):
        return None
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = kernel32.GetLastError()
            if error == 87:  # ERROR_INVALID_PARAMETER：pid 取值非法，必无此进程
                return False
            if error == 5:  # ERROR_ACCESS_DENIED：进程存在但无权查询
                return True
            return None
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _parse_utc(value):
    """解析锁文件里的 UTC 时间戳；缺失/非法返回 None（调用方按未知处理）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _lock_stale_reason(path, payload, lease_seconds):
    """锁是否已失效：返回 (True, "dead_pid"|"lease_expired") 或 (False, None)。

    两个独立触发条件（任一成立即失效）：持有者进程已死；自最后心跳起超过租约。
    时间戳取 acquired_at / refreshed_at / 文件 mtime 三者之中的最新者；全部缺失
    或不可解析时不按过期处理（fail-safe：未知年龄的锁只认 pid 死亡）。
    """
    holder_pid = payload.get("process_pid", payload.get("pid")) \
        if isinstance(payload, dict) else None
    if _pid_alive(holder_pid) is False:
        return True, "dead_pid"
    moments = []
    if isinstance(payload, dict):
        for key in ("acquired_at", "refreshed_at"):
            moment = _parse_utc(payload.get(key))
            if moment is not None:
                moments.append(moment)
    try:
        moments.append(datetime.fromtimestamp(Path(path).stat().st_mtime,
                                              tz=timezone.utc))
    except OSError:
        pass
    if not moments:
        return False, None
    age = (datetime.now(timezone.utc) - max(moments)).total_seconds()
    if age > lease_seconds:
        return True, "lease_expired"
    return False, None


def _break_stale_lock(path, seen_nonce):
    """破除已失效的锁：复核 nonce 未易主才删；删不动/已易主返回 False。"""
    try:
        current = _read_json(Path(path), None)
    except ValueError:
        current = None
    if (seen_nonce is not None and isinstance(current, dict)
            and current.get("nonce") != seen_nonce):
        return False  # 锁已易主（活锁），绝不碰
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass  # 已被释放，同样可以重试占锁
    except OSError:
        return False
    return True


def acquire_lock(root, *, marker, owner=None, kind="session", lease_seconds=None):
    """O_CREAT|O_EXCL 原子占锁；不写会话目录。

    TG-01 租约：已存在的锁若判定失效（持有者进程已死，或自最后心跳起超过
    lease_seconds 无刷新，默认 LOCK_LEASE_SECONDS），则安全自动破锁后重试一次
    占锁，token 里如实携带 recovered_from（被破锁的 nonce/时间/原因）；新鲜锁
    仍抛 LockBusy。破锁前复核 nonce 未易主，且只删这一个锁文件；破锁/重试失败
    一律按 LockBusy 处理，绝不与活锁并发。

    固定加锁顺序：需要两把锁的动作先取归档锁（kind="archive"，Task 5–8），再取会话锁
    （kind="session"）；本模块 own 的只有会话锁。锁名前缀按 LOCK_PREFIXES 分开，两把锁
    永远不会同名。
    """
    root = Path(root)
    if not _text(marker):
        raise ValueError("互斥标记必须是非空字符串（账号存储的散列，不含会话路径）")
    (root / LOCK_SUBDIR).mkdir(parents=True, exist_ok=True)
    path = _lock_path(root, marker, kind)
    nonce = "%d-%s" % (os.getpid(), _utc_now())
    holder = {"marker": marker, "nonce": nonce, "acquired_at": _utc_now(),
              "process_pid": os.getpid()}
    for key in _OWNER_KEYS:
        value = (owner or {}).get(key)
        if (isinstance(value, str) and value.strip()) or (key == "pid" and _positive_int(value)):
            holder[key] = value
    lease = LOCK_LEASE_SECONDS if lease_seconds is None else lease_seconds
    recovered = None
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            seen = _read_json(path, None)
        except ValueError:
            seen = None
        stale, reason = _lock_stale_reason(
            path, seen if isinstance(seen, dict) else None, lease)
        seen_nonce = seen.get("nonce") if isinstance(seen, dict) else None
        if stale and _break_stale_lock(path, seen_nonce):
            try:
                descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                raise LockBusy(_peek_holder(path), marker, path)
            recovered = {"nonce": seen_nonce,
                         "acquired_at": seen.get("acquired_at") if isinstance(seen, dict) else None,
                         "reason": reason}
        else:
            raise LockBusy(_peek_holder(path), marker, path)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(holder, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    token = {"path": str(path), "marker": marker, "nonce": nonce, "kind": kind}
    if recovered is not None:
        token["recovered_from"] = recovered
    return token


def refresh_lock(root, token):
    """心跳：持有者刷新租约（nonce 须匹配，否则拒绝续他人的锁）。

    只更新 refreshed_at（附带刷新文件 mtime），不改变 nonce/acquired_at；
    锁已丢失/易主时抛 ValueError，调用方按尽力而为处理（不中断任务）。
    """
    if not isinstance(token, dict) or not token.get("path") or not token.get("nonce"):
        raise ValueError("互斥凭据不完整，拒绝刷新")
    path = Path(token["path"])
    current = _read_json(path, None)
    if current is None or current.get("nonce") != token.get("nonce"):
        raise ValueError("互斥凭据与锁文件不一致，拒绝刷新他人的任务锁")
    current["refreshed_at"] = _utc_now()
    _publish_json(path, current)
    return {"refreshed": str(path), "refreshed_at": current["refreshed_at"]}


def release_lock(root, token):
    """只释放自己持有的锁；凭据不符就保留他人的锁，不越权解锁。"""
    if not isinstance(token, dict) or not token.get("path"):
        raise ValueError("互斥凭据不完整，拒绝释放")
    path = Path(token["path"])
    current = _read_json(path, None)
    if current is not None and current.get("nonce") != token.get("nonce"):
        raise ValueError("互斥凭据与锁文件不一致，拒绝释放他人的任务锁")
    for attempt in range(10):
        try:
            path.unlink()
            break
        except FileNotFoundError:
            break
        except PermissionError:
            # Windows 上并发读取会让删除短暂被拒；有限重试，不假装已释放。
            time.sleep(0.01 * (attempt + 1))
    else:
        raise RuntimeError("任务锁 %s 仍被其他进程占用，无法删除：不伪装已释放，"
                           "请检查仍在读取锁文件的归档任务" % path.name)
    return {"released": str(path)}


def _lock_root_choice(data, override=None):
    """本次运行使用的 lock_root 原值：显式参数 > 执行者配置 lock_root > 未配置(None)。

    lock_root 是运行参数（合同第 83 行），不是用户内容选择：绝不注入默认值，配了才用，
    配得非法就按「配置非法」拒绝（ValueError），不静默退回 destination。非法有三型：
    非绝对路径、结尾带会被 Windows 静默剥掉的点或空格、与账号存储（登录会话）目录相同或
    互相包含。第三型的错误文字不回显任何路径，免得会话目录位置进到输出里。
    """
    chosen = override if override is not None else data.get("lock_root")
    if chosen is None:
        return None
    if _absolute_path(chosen) is None:
        raise ValueError("lock_root 必须是非空绝对路径字符串（执行者运行参数，不注入默认锁根，"
                         "也不得指向 tdl 会话目录）：%r" % (chosen,))
    if _trimmable_by_windows(str(chosen)):
        raise ValueError("lock_root 结尾的点或空格会被 Windows 静默剥掉，实际落点与所写不是同"
                         "一路径：请按最终形状书写（配置非法，不启动任何子进程）")
    if inside_session_storage(chosen, data.get("storage")):
        raise ValueError(MUTEX_SESSION_CONFLICT_REASON)
    return str(_absolute_path(chosen))


def resolve_lock_root(config, *, lock_root=None):
    """会话锁落点：{"path": 基准目录, "lock_root": 生效的显式配置, "scope": 互斥面}。"""
    data = validate_job(config)
    chosen = _lock_root_choice(data, lock_root)
    if chosen is None:
        return {"path": default_lock_root(data["destination"]), "lock_root": None,
                "scope": "destination"}
    return {"path": Path(chosen), "lock_root": chosen,
            "scope": mutex_declaration(chosen, data["destination"],
                                       data.get("storage"))["lock_scope"]}


def mutex_declaration(lock_root, destination, storage=None):
    """互斥面申报（可进 check，也可进 status）：跨 destination 是否真的互斥，只讲事实。

    指向会话存储目录的 lock_root 是配置非法：既不当互斥已兑现来申报，也不把该路径的
    原值写进输出（会话/凭据位置一律不外泄）。
    """
    absolute = _absolute_path(lock_root)
    target = _absolute_path(destination)
    conflict = absolute is not None and inside_session_storage(absolute, storage)
    across = absolute is not None and (target is None or absolute != target)
    if conflict:
        return {"capability": MUTEX_CAPABILITY, "status": "unavailable",
                "lock_scope": LOCK_SCOPE_REJECTED, "lock_root": None,
                "across_destinations": False,
                "affected_actions": list(MUTEX_AFFECTED_ACTIONS),
                "reason": MUTEX_SESSION_CONFLICT_REASON}
    return {"capability": MUTEX_CAPABILITY,
            "status": "available" if across else "unavailable",
            "lock_scope": "account" if across else "destination",
            "lock_root": str(absolute) if absolute is not None else None,
            "across_destinations": across,
            "affected_actions": list(MUTEX_AFFECTED_ACTIONS),
            "reason": MUTEX_OK_REASON if across else MUTEX_GAP_REASON}


def session_mutex_report(config):
    """不做完整合同校验的互斥面申报，供 check 在配置本身非法时也能出报告。"""
    data = config if isinstance(config, dict) else {}
    return mutex_declaration(data.get("lock_root"), data.get("destination"),
                             data.get("storage"))


def acquire_session_lock(config, *, owner=None, lock_root=None, lease_seconds=None):
    """按账号存储取一把会话锁：同一 storage+namespace 的任意任务共用它。

    任务级入口（Task 3 的 download、Task 8 的 update 也要用）：一把锁覆盖整个 job，
    不按聊天重复取。取归档锁的动作必须先取归档锁，再调这里。
    TG-01：失效锁（持有者已死/租约过期）自动破锁恢复，token 携带 recovered_from。
    """
    data = validate_job(config)
    base = resolve_lock_root(data, lock_root=lock_root)
    marker = account_marker(data)
    identity = None
    if len(data.get("chats") or []) == 1:
        identity = chat_identity(data["chats"][0])
    holder = owner if owner is not None else {"pid": os.getpid(), "identity": identity}
    token = acquire_lock(base["path"], marker=marker, owner=holder,
                         lease_seconds=lease_seconds)
    return {"token": token, "base": base["path"], "marker": marker,
            "lock_root": base["lock_root"],
            "mutex": mutex_declaration(base["lock_root"], data.get("destination"))}


def release_session_lock(handle):
    """释放 acquire_session_lock 取得的锁；锁名不符就保留，不越权解锁。"""
    if not isinstance(handle, dict) or "token" not in handle:
        raise ValueError("会话锁句柄不完整，拒绝释放")
    return release_lock(handle["base"], handle["token"])


def archive_lock_marker(resource):
    """归档锁标记：取被保护资源（来源批次清单、派生索引库）绝对路径的散列。

    只落散列，不含用户名、不含会话目录；与 Task 6 的 ``catalog.archive_lock_marker``
    同一算法（同一份资源在导入侧与索引侧算出同一个标记），所以后续任务不必再造第二份。
    """
    path = Path(str(resource)).absolute()
    seed = str(path).replace("\\", "/").lower()
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def acquire_archive_lock(root, *, resource=None, marker=None, owner=None,
                         lease_seconds=None):
    """取一把**归档锁**（锁名 ``archive-*``，Task 2 预留给了 Task 5–8）。

    互斥单位是「同一份派生归档」，不是账号存储：所以离线动作（import/search/share）没有
    storage / namespace 也照样取得——这正是它与会话锁的分工。实现口径与
    :func:`acquire_session_lock` 一致：``O_CREAT|O_EXCL`` 原子占锁、失效锁按租约自动破锁、
    :class:`LockBusy` 随异常带出锁文件路径。

    加锁顺序固定「**先归档锁、后会话锁**」（``LOCK_ORDER``），释放顺序相反：需要两把锁的
    动作（Task 8 的 update 这类）先调本函数、再调 :func:`acquire_session_lock`。Task 5 的
    import 与 Task 6 的 search 都是纯离线动作，只持归档锁。锁的取放只此一份实现，
    后续任务复用本 helper 与 :func:`release_archive_lock`，不要再内联三步。
    """
    if not marker and not resource:
        raise ValueError("归档锁必须给出被保护的资源（resource）或标记（marker），不默认、不猜")
    name = marker or archive_lock_marker(resource)
    holder = owner if owner is not None else {"pid": os.getpid()}
    token = acquire_lock(root, marker=name, owner=holder, kind="archive",
                         lease_seconds=lease_seconds)
    return {"token": token, "base": Path(root), "marker": name, "kind": "archive",
            "order": list(LOCK_ORDER)}


def release_archive_lock(handle):
    """释放 acquire_archive_lock 取得的锁；锁名不符就保留，不越权解锁。"""
    if not isinstance(handle, dict) or "token" not in handle:
        raise ValueError("归档锁句柄不完整，拒绝释放")
    return release_lock(handle["base"], handle["token"])


# ---------------------------------------------------------------- 三类完成状态

def record_output(root, kind, status, reason=None, evidence=None):
    """Task 3/4 的验收入口：把媒体/阅读版的完成状态登记进 outputs.json。

    complete 必须带验收凭据（非空 evidence），failed/partial 必须带原因，避免后面
    的任务用「文件存在」冒充完成。
    """
    if kind not in OUTPUT_KINDS:
        raise ValueError("输出类别不受支持（仅 text_index/reading/media）：" + str(kind))
    if status not in STATUSES:
        raise ValueError("状态取值不受支持（仅六种任务状态）：" + str(status))
    if status in ("failed", "partial") and not _text(reason):
        raise ValueError("失败或部分完成必须给出原因")
    if status == "complete" and not (isinstance(evidence, dict) and evidence):
        raise ValueError("complete 必须有验收凭据（evidence），不能凭空登记完成")
    payload = _read_json(Path(root) / OUTPUT_NAME, None) or {}
    payload[kind] = {"status": status, "reason": reason, "evidence": evidence,
                     "recorded_at": _utc_now()}
    _publish_json(Path(root) / OUTPUT_NAME, payload)
    return copy.deepcopy(payload[kind])


def _selection(task, job):
    if task is not None:
        return list(task["selected_outputs"]), list(task["contents"])
    if job is None:
        return [], []
    return list(job.get("outputs") or []), list(job.get("contents") or [])


def _output_entries(outputs_selection, contents, milestones, index_entry):
    reading_selected = "markdown" in outputs_selection and "text" in contents
    media_selected = "media" in outputs_selection
    entries = {"text_index": index_entry,
               "reading": _milestone("reading", reading_selected, milestones.get("reading"),
                                     "所选阅读版尚未生成或未验收（阅读版由 Task 4 产出）"),
               "media": _milestone("media", media_selected, milestones.get("media"),
                                   "所选媒体尚未下载或未验收（媒体下载由 Task 3 产出）")}
    entries["text_index"]["selected"] = "json" in outputs_selection
    return entries


def _milestone(kind, selected, recorded, pending_reason):
    entry = {"kind": kind, "selected": selected, "counts_toward_completion": selected,
             "evidence": None, "reason": None}
    if not selected:
        entry["status"] = "unavailable"
        entry["counts_toward_completion"] = False
        entry["reason"] = "本次选择未包含该输出（%s），不生成也不计入完成" % kind
        return entry
    if recorded is None:
        entry["status"] = "pending"
        entry["reason"] = pending_reason
        return entry
    entry["status"] = recorded.get("status", "pending")
    entry["reason"] = recorded.get("reason")
    entry["evidence"] = recorded.get("evidence")
    if entry["status"] == "complete" and not entry["evidence"]:
        entry["status"] = "partial"
        entry["reason"] = "登记的完成状态缺少验收凭据，按 partial 处理"
    return entry


def _index_entry(scanned, failures, json_selected, plan):
    entry = {"kind": "text_index", "selected": json_selected,
             "counts_toward_completion": json_selected, "evidence": None}
    capability = [item for item in failures if item.get("kind") in CAPABILITY_KINDS]
    errors = [item for item in failures if item.get("kind") not in CAPABILITY_KINDS]
    verified = len(scanned["verified"])
    entry["evidence"] = {"planned": len(plan), "verified": verified,
                         "missing": len(plan) - verified,
                         "segment_reasons": {item["segment_id"]: item["reason"]
                                             for item in scanned["pending"]}}
    if capability:
        entry["status"] = "unavailable"
        entry["reason"] = capability[-1].get("message") or "依赖或参数未就绪"
    elif errors:
        entry["status"] = "failed"
        entry["reason"] = errors[-1].get("message") or "导出失败"
    elif not plan:
        entry["status"] = "pending"
        entry["reason"] = "没有分段计划，未做任何导出"
    elif verified == len(plan):
        entry["status"] = "complete"
        entry["reason"] = ("分段全部验收，最终成品已发布" if json_selected
                           else "分段索引全部验收完成（本次未选 JSON 成品）")
    elif verified:
        entry["status"] = "partial"
        entry["reason"] = "已验收 %d/%d 段，缺段不发布正式成品" % (verified, len(plan))
    else:
        entry["status"] = "pending"
        entry["reason"] = "还没有通过验收的段"
    return entry


def _final_entry(manifest, plan, scanned, json_selected):
    published = manifest.get("final")
    entry = {"selected": json_selected, "published": False, "trusted": False,
             "path": None, "sha256": None, "count": None, "reason": None}
    if not published:
        entry["reason"] = "没有已发布的最终成品（缺段或尚未导出）"
        return entry
    path = Path(published.get("path") or "")
    entry["path"] = str(path)
    entry["published"] = path.is_file()
    if not entry["published"]:
        entry["reason"] = "清单登记的最终成品文件缺失"
        return entry
    if not plan or len(scanned["verified"]) != len(plan):
        entry["reason"] = "存在未验收段，最终成品不可信（final 存在不等于全量完成）"
        return entry
    if _digest(path) != published.get("sha256"):
        entry["reason"] = "最终成品摘要与清单不一致（文件被修改）"
        return entry
    entry["trusted"] = True
    entry["sha256"] = published["sha256"]
    entry["count"] = published.get("count")
    entry["reason"] = "最终成品已通过验收"
    return entry


def _mutex_for(task, job):
    """状态报告里的互斥面申报：本次配置优先，其次任务快照；两者都没有就如实报缺口。"""
    sources = [item for item in (job, task) if isinstance(item, dict)]
    lock_root = next((item["lock_root"] for item in sources if "lock_root" in item), None)
    destination = next((item.get("destination") for item in sources
                        if item.get("destination")), None)
    storage = next((item.get("storage") for item in sources if item.get("storage")), None)
    return mutex_declaration(lock_root, destination, storage)


def build_status(root, job=None):
    """只读汇总三类完成状态；绝不启动或恢复任何子进程，也不写任何状态文件。"""
    root = Path(root)
    task = _read_json(root / TASK_NAME, None)
    manifest = _read_json(root / MANIFEST_NAME, None) or {
        "version": STATE_VERSION, "updated_at": None, "identity": None, "segments": {},
        "final": None}
    failures = _failures(root)
    milestones = _read_json(root / OUTPUT_NAME, None) or {}
    plan = list(task["plan"]) if isinstance(task, dict) else []
    scanned = scan_completed(root, plan) if plan else {
        "verified": [], "pending": [], "planned": 0}
    outputs_selection, contents = _selection(task, job)
    json_selected = "json" in outputs_selection
    entries = _output_entries(outputs_selection, contents, milestones,
                              _index_entry(scanned, failures, json_selected, plan))
    final = _final_entry(manifest, plan, scanned, json_selected)
    if json_selected:
        index = entries["text_index"]
        if index["status"] == "unavailable":
            # 能力/依赖缺口（导出参数未核对、会话被占用、锁未能释放）优先于「成品已验收」：
            # 降级成 partial 会把「本轮什么都没启动/没收口」伪装成跑坏了可续。
            pass
        elif index["status"] == "complete" and not final["trusted"]:
            index["status"] = "partial"
            index["reason"] = final["reason"]
        elif index["status"] != "complete" and final["trusted"]:
            index["status"] = "partial"
            index["reason"] = "最终成品已验收，但仍有计划段未验收"
        if final["trusted"]:
            index["evidence"] = dict(index["evidence"] or {}, final_sha256=final["sha256"],
                                     final_count=final["count"])
    counting = [item for item in entries.values() if item["counts_toward_completion"]]
    complete = bool(counting) and all(item["status"] == "complete" for item in counting)
    if task is None:
        headline = "unavailable"
    elif any(item["status"] == "failed" for item in entries.values()):
        headline = "failed"
    elif complete:
        headline = "complete"
    elif any(item["status"] in ("partial", "pending", "running") for item in counting):
        headline = "partial"
    elif any(item["status"] == "unavailable" for item in counting):
        headline = "unavailable"
    else:
        headline = "pending"
    if task is None:
        for entry in entries.values():
            entry["status"] = "unavailable"
            entry["counts_toward_completion"] = False
            entry["reason"] = "没有任务快照 %s：不猜本次选择了哪些输出，也不猜完成状态" % TASK_NAME
        headline = "unavailable"
    persisted = _persisted_progress(root)
    persisted["remaining"] = len(scanned["pending"])
    return {"state_root": str(root), "no_child_process_started": True,
            "session_mutex": _mutex_for(task, job),
            "task": None if task is None else {
                "identity": task["identity"], "peer_type": task["peer_type"],
                "chat_id": task["chat_id"], "fingerprint": task["fingerprint"],
                "snapshot_upper_bound": task["snapshot_upper_bound"],
                "segment_width": task["segment_width"], "range": task["range"],
                "contents": task["contents"], "selected_outputs": task["selected_outputs"],
                "endpoint": task["endpoint"], "endpoint_evidence": task["endpoint_evidence"]},
            "segments": {"planned": len(plan), "verified": len(scanned["verified"]),
                         "pending": len(scanned["pending"]),
                         "items": [{"segment_id": item["segment_id"],
                                    "verified": item["segment_id"] in
                                    {entry["segment_id"] for entry in scanned["verified"]},
                                    "lower": item["lower"], "upper": item["upper"],
                                    "reason": next((entry["reason"] for entry in
                                                   scanned["pending"]
                                                   if entry["segment_id"] == item["segment_id"]),
                                                   None)}
                                   for item in plan]},
            "final": final, "outputs": entries, "complete": complete, "status": headline,
            "diagnostics": failures, "progress": persisted}


def _write_status_file(root, report):
    document = dict(copy.deepcopy(report), written_at=_utc_now())
    _publish_json(Path(root) / STATUS_NAME, document)
    return document


# ---------------------------------------------------------------- 在线导出组合

def classify_failure(message):
    """把失败文本分成网络/限流/授权/未知，只有前两类参与有限重试。"""
    text = str(message).lower()
    if _RATE_LIMIT_MARKER.search(text) or "flood_wait" in text:
        return "rate_limit"
    if any(marker in text for marker in _AUTHORIZATION_MARKERS):
        return "authorization"
    if any(marker in text for marker in _NETWORK_MARKERS):
        return "network"
    return "unknown"


def flood_wait_seconds(message):
    """按后端反馈等待，单次上限 FLOOD_WAIT_CAP_SECONDS，绝不无界等待。"""
    match = _RATE_LIMIT_MARKER.search(str(message))
    if not match:
        return 1
    return min(int(match.group(1)), FLOOD_WAIT_CAP_SECONDS)


def _argument_list(argv):
    if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)):
        raise ValueError("导出命令必须是参数列表（list[str]），绝不拼接 shell 命令字符串")
    items = list(argv)
    if not items or not all(_text(item) for item in items):
        raise ValueError("导出命令参数必须全部是非空字符串（用参数列表调用，不走 shell）")
    return items


def _parse_utc_epoch(value):
    """数值（UTC 秒）或 ISO 日期 -> UTC epoch 秒；取不到就 None（不猜时间）。

    与 reading._moment 同口径：数值按 UTC 秒，无时区标记的字符串按 UTC 处理；
    布尔、空串、非法形状一律 None。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc).timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            return parsed.astimezone(timezone.utc).timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _message_epoch(item):
    """一条导出消息的 UTC epoch 秒；没有可解析日期就 None（由调用方保留并计数）。"""
    for key in WINDOW_DATE_KEYS:
        epoch = _parse_utc_epoch(item.get(key))
        if epoch is not None:
            return epoch
    return None


def _time_window(data):
    """time 模式的 UTC 窗口（起点包含、终点不包含）；其它模式没有日期窗口。

    配置经过 validate_job 规范化，time 的起止是合法的 UTC Zulu 串；这里只做
    解析，不重新校验——解析不出就返回 None（不设闸），绝不静默放宽窗口。
    """
    scope = data.get("range") or {}
    if scope.get("mode") != "time":
        return None
    start = _parse_utc_epoch(scope.get("start"))
    end = _parse_utc_epoch(scope.get("end"))
    if start is None or end is None or not start < end:
        return None
    return {"mode": "time", "start": scope.get("start"), "end": scope.get("end"),
            "start_epoch": start, "end_epoch": end}


def _enforce_time_window(temporary, window, stats=None):
    """W4：time 窗口的本地二次校验——窗外消息剔除，缺日期的保留并计数。

    在 commit_segment 之前对临时文件动手：只在确实剔掉窗外消息时才重写文件
    （形状原样保留：数组仍是数组，映射仍是映射），窗内与无日期消息一个不动。
    文件形状非法时抛 ValueError，调用方按段校验未通过记失败（与 commit 同口）。
    """
    path = Path(temporary)
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("导出文件 JSON 解析失败（%s）：%s"
                         % (type(error).__name__, path.name))
    if isinstance(payload, list):
        messages, is_list = payload, True
    elif isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        messages, is_list = payload["messages"], False
    else:
        raise ValueError("无法识别的导出结构：需要消息数组或含 messages 数组的映射"
                         "（不臆造 tdl 导出 schema）")
    kept, dropped, unverifiable = [], 0, 0
    for item in messages:
        if not isinstance(item, dict):
            kept.append(item)      # 形状问题留给 validate_export 判，不在这里吞掉
            continue
        epoch = _message_epoch(item)
        if epoch is None:
            unverifiable += 1
            kept.append(item)
        elif not window["start_epoch"] <= epoch < window["end_epoch"]:
            dropped += 1
        else:
            kept.append(item)
    if stats is not None:
        stats["filtered_out"] = stats.get("filtered_out", 0) + dropped
        stats["unverifiable"] = stats.get("unverifiable", 0) + unverifiable
    if dropped:
        trimmed = kept if is_list else dict(payload, messages=kept)
        path.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8",
                        newline="\n")


def _progress_record(elapsed, remaining, window, stats, started):
    """W2：本轮进度凭据——完成样本耗时 + ETA（样本不足标未知）+ 窗口过滤计数。"""
    samples = [float(item) for item in elapsed]
    if len(samples) >= MIN_PROGRESS_SAMPLES_FOR_ETA:
        eta = round(sum(samples) / len(samples) * remaining, 3) if remaining > 0 else 0
        basis = "本轮完成样本均值×剩余段数" if remaining > 0 else "本轮已无剩余段"
    else:
        eta = "unknown"
        basis = ("完成样本不足（<%d），标记未知；不得用文件体积或最大消息 ID 作确定结论"
                 % MIN_PROGRESS_SAMPLES_FOR_ETA)
    window_report = None
    if window is not None:
        window_report = {"mode": "time", "start": window["start"], "end": window["end"],
                         "applied": True,
                         "filtered_out": (stats or {}).get("filtered_out", 0),
                         "unverifiable": (stats or {}).get("unverifiable", 0)}
    return {"completed_samples": len(samples), "completed_sample_seconds": samples,
            "remaining": remaining, "eta_seconds": eta, "eta_basis": basis,
            "window": window_report, "started_this_run": started}


def _persisted_progress(root):
    """上一次 run 写下的 progress（读 status.json，不写任何文件）；没有就按无样本申报。"""
    fallback = {"completed_samples": 0, "completed_sample_seconds": [],
                "remaining": 0, "eta_seconds": "unknown",
                "eta_basis": "本轮尚无耗时样本，标记未知",
                "window": None, "started_this_run": False}
    try:
        raw = Path(root) / STATUS_NAME
        payload = json.loads(raw.read_text(encoding="utf-8")) if raw.is_file() else None
    except (ValueError, OSError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("progress"), dict):
        record = copy.deepcopy(payload["progress"])
        record["started_this_run"] = False    # 这是上一轮的读数，不是本轮新样本
        return record
    return fallback


def _latest_failure_message(root):
    """本任务最近一条诊断的消息；没有就给授权/会话失效的兜底说法。"""
    items = _failures(root)
    if items:
        message = items[-1].get("message")
        if _text(message):
            return message
    return "授权/会话失效"


def _untouched_temp_record(root):
    """整轮停止后未启动聊天的 temp_files 占位：如实说没清理，不伪装扫过。"""
    return {"scope": str(root), "removed": [], "kept": [], "skipped": [],
            "needs_attention": [], "ignored": [],
            "counts": {"removed": 0, "kept": 0, "skipped": 0, "needs_attention": 0,
                       "ignored": 0},
            "note": "本轮未启动（整轮已停止），未做任何清理"}


def _subprocess_runner(argv, temporary, timeout=None):
    """默认后端：只按参数列表启动子进程，失败文本用于分类；不拼 shell、不改全局环境。

    TG-01：timeout 秒无退出即判挂起（默认 TDL_SUBPROCESS_TIMEOUT_SECONDS）：
    subprocess.run 自动 kill 并回收子进程，这里转成 kind="network" 的 ExportError
    进入有限重试，绝不在锁内无限阻塞。
    """
    limit = TDL_SUBPROCESS_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=limit)
    except subprocess.TimeoutExpired as error:
        raise ExportError("导出子进程超时（timeout=%ss）：%ss 未退出，已终止；"
                          "按网络类有限重试" % (limit, limit), kind="network") from error
    if completed.returncode != 0:
        detail = ("%s %s" % (completed.stderr or "", completed.stdout or "")).strip()
        raise ExportError("导出进程退出码 %s：%s" % (completed.returncode, detail[:400]))
    return completed.returncode


def _attempt_segment(root, data, segment, command_builder, runner, retries, sleeper,
                     *, window=None, stats=None, subprocess_timeout=None):
    """单段：临时文件 -> 子进程 -> 窗口二次校验 -> 校验 -> 原子提交；返回 ok/failed/stopped。

    ``window`` 是 _time_window 算出的 time 窗口（其它模式为 None，不过滤）；
    ``stats`` 是调用方给的累加器（filtered_out / unverifiable），只在给了窗口时记数。
    耗时样本由调用方在外层计时（覆盖重试等待），本函数只返回结果词。
    TG-01：``subprocess_timeout`` 只透传给默认 Runner；注入式 Runner 保持
    ``(argv, temporary)`` 二元形状，不受影响。
    """
    temporary = Path(root) / TMP_SUBDIR / ("%s.json.part" % segment["segment_id"])
    if temporary.exists():
        temporary.unlink()
    attempts = 0
    while True:
        attempts += 1
        try:
            argv = _argument_list(command_builder(data, segment, temporary))
            if runner is None:
                code = _subprocess_runner(argv, temporary, timeout=subprocess_timeout)
            else:
                code = runner(argv, temporary)
        except ExportError as error:
            kind = error.kind
            if kind in STOPPING_KINDS:
                _record_failure(root, {"segment_id": segment["segment_id"], "kind": kind,
                                       "message": "授权/会话失效：%s；停止本任务，不进重试循环"
                                       % error, "attempts": attempts,
                                       "retryable": False, "failed_at": _utc_now()})
                return "stopped"
            if kind in RETRYABLE_KINDS and attempts <= retries:
                if kind == "rate_limit":
                    sleeper(flood_wait_seconds(error))
                continue
            _record_failure(root, {"segment_id": segment["segment_id"], "kind": kind,
                                   "message": str(error), "attempts": attempts,
                                   "retryable": kind in RETRYABLE_KINDS,
                                   "failed_at": _utc_now()})
            return "failed"
        except FileNotFoundError as error:
            # 依赖缺口：后端可执行文件起不来（路径不存在/不是可执行文件），本轮没有真正
            # 导出任何东西，按能力缺口申报（组合层退出码 3），不伪装成跑坏了一段。
            _record_failure(root, {"segment_id": segment["segment_id"],
                                   "kind": "insufficient_source",
                                   "message": "后端可执行文件未能启动：%s（不猜路径、"
                                              "不自动安装、不搜索 PATH）" % error,
                                   "attempts": attempts, "retryable": False,
                                   "failed_at": _utc_now()})
            return "stopped"
        except (TypeError, ValueError) as error:
            _record_failure(root, {"segment_id": segment["segment_id"], "kind": "unknown",
                                   "message": str(error), "attempts": attempts,
                                   "retryable": False, "failed_at": _utc_now()})
            return "failed"
        if code != 0:
            _record_failure(root, {"segment_id": segment["segment_id"], "kind": "unknown",
                                   "message": "导出进程返回非零退出码 %s" % code,
                                   "attempts": attempts, "retryable": False,
                                   "failed_at": _utc_now()})
            return "failed"
        if not temporary.is_file():
            _record_failure(root, {"segment_id": segment["segment_id"], "kind": "unknown",
                                   "message": "退出码 0 但后端没有写出导出文件，"
                                              "不作为完成凭据", "attempts": attempts,
                                   "retryable": False, "failed_at": _utc_now()})
            return "failed"
        try:
            if window is not None:
                _enforce_time_window(temporary, window, stats)
            commit_segment(root, segment, temporary)
        except ValueError as error:
            _record_failure(root, {"segment_id": segment["segment_id"], "kind": "unknown",
                                   "message": "段校验未通过，不重试也不提交：%s" % error,
                                   "attempts": attempts, "retryable": False,
                                   "failed_at": _utc_now()})
            return "failed"
        return "ok"


def _task_targets(root, data):
    """本任务要处理的状态根清单：[(状态根, 单聊天配置), ...]。

    单聊天用调用方给定的状态根；多聊天按配置里的 destination 与 peer 身份逐个建档，
    整 job 只取一把会话锁（锁作用域＝每任务，不是每聊天），此时入参 root 不再生效。
    """
    chats = data["chats"]
    if len(chats) == 1:
        return [(Path(root), data)]
    return [(task_state_root(data["destination"], chat_identity(entry)),
             dict(data, chats=[entry])) for entry in chats]


def _refused_report(root, data, kind, message):
    """不启动任何子进程的拒绝路径：只归并写本任务名下的一条诊断，再落状态快照。"""
    _record_failure(root, {"segment_id": None, "kind": kind, "message": message,
                           "attempts": 0, "retryable": False, "failed_at": _utc_now()})
    return _write_status_file(root, build_status(root, data))


def _aggregate_reports(reports, lock=None):
    """把每聊天的状态合成任务级状态；单聊天保持原来的单状态形状不变。

    退出码口径由组合层按 status 判定：unavailable=能力/依赖缺口（3），
    complete=0，其余 partial/failed=1。
    """
    for report in reports:
        if lock is not None:
            report["lock"] = lock
    if len(reports) == 1:
        return reports[0]
    statuses = [item["status"] for item in reports]
    if "unavailable" in statuses:
        headline = "unavailable"
    elif "failed" in statuses:
        headline = "failed"
    elif all(item["complete"] for item in reports):
        headline = "complete"
    else:
        headline = "partial"
    return {"kind": "telegram-archive-task", "status": headline,
            "complete": all(item["complete"] for item in reports),
            "session_mutex": reports[0]["session_mutex"],
            "state_roots": [item["state_root"] for item in reports], "tasks": reports}


def run_export(root, job, *, command_builder=None, runner=None, lock_root=None,
               sleeper=None, subprocess_timeout=None):
    """分段导出组合：整任务一把会话锁 -> 逐聊天逐段落盘验收 -> 齐段才发布 final。

    command_builder 必须由调用方提供（tdl 参数名与端点包含性未由本机帮助副本确认，见
    ENDPOINT_NOTE）；缺它就不启动任何子进程，只如实报 unavailable。sleeper 只在
    FLOOD_WAIT 反馈时使用，测试注入即可避免真实等待。
    TG-01：subprocess_timeout 透传给默认 Runner（单次 tdl 调用超时）；逐段刷新锁心跳，
    锁恢复事件进报告的 lock.recovered_from。

    写入次序（本轮裁决）：
    1. 配置与任务身份先验：validate_job / init_task / plan_task_segments 的 ValueError
       一律在启动任何子进程之前抛出（组合层按「配置非法＝退出码 2」归类）；
    2. 会话锁只在整个任务上取一次，取放一律经公开 helper acquire_session_lock /
       release_session_lock（全仓只此一份实现），锁根由 resolve_lock_root 决定（显式
       lock_root > destination/locks；lock_root 与账号存储目录相同或互相包含＝配置非法，
       在启动任何子进程之前拒绝；未配 lock_root 时互斥面不足，由状态/能力报告申报）；
    3. 取锁成功后才重置本轮诊断，并且只重置本任务名下的记录；同一时点清理上一轮崩溃
       遗留的超龄 *.part，逐条上报，被拒的运行不做任何清理；
    4. 复认领崩溃窗口留下的合法孤儿段，再补齐未验收的段；
    5. 合并发布 final 也在锁内完成；释放锁失败如实记为 lock_release 诊断，不伪装已释放。
    6. 授权/会话失效（stopped）停整轮：同一 job 的后续聊天不再启动，标 unavailable
       并点名前序身份（聚合报告另带 stopped_early.after/skipped 清单）；本轮各聊天
       的完成样本耗时、ETA（样本<2 标 unknown）与 time 窗口过滤计数写入 progress。
    """
    data = validate_job(job)
    chosen = _lock_root_choice(data, lock_root)
    data = dict(data, lock_root=chosen)
    targets = _task_targets(root, data)
    prepared = []
    for item_root, one in targets:
        task = init_task(item_root, one)
        prepared.append((item_root, one, task, plan_task_segments(item_root, one)))
    if command_builder is None:
        return _aggregate_reports([
            _refused_report(item_root, one, "insufficient_source",
                            "未提供导出参数构造器：%s（不启动任何子进程）" % ENDPOINT_NOTE)
            for item_root, one, _task, _plan in prepared])
    retries = int(data.get("retries") or 0)
    sleeper = sleeper or time.sleep
    # 会话锁的取放只有 acquire_session_lock / release_session_lock 这一份实现：产品路径
    # 直接调这两个公开 helper，不再内联 resolve_lock_root + account_marker + acquire_lock
    # 三步（两份实现会漂移，漂移了互斥就形同虚设）。
    try:
        handle = acquire_session_lock(data, lock_root=chosen)
    except LockBusy as error:
        return _aggregate_reports([
            _refused_report(item_root, one, "lock_busy",
                            "%s（占用者：%s）" % (error, error.holder))
            for item_root, one, _task, _plan in prepared])
    token = handle["token"]
    lock = {"released": True, "path": token["path"], "marker": handle["marker"]}
    if token.get("recovered_from"):
        lock["recovered_from"] = token["recovered_from"]
    pruned = {}
    samples = {}
    halted = None
    try:
        for item_root, one, task, plan in prepared:
            if halted is not None:
                # W3：前序聊天已因授权/会话失效停整轮——后续聊天一个都不启动，标
                # unavailable 并点名前序身份，不逐个刷同样的失败。
                _record_failure(item_root, {
                    "segment_id": None, "kind": "insufficient_source",
                    "message": "同一在线任务的前序聊天 %s 在本轮遇到授权/会话失效"
                               "（%s），整轮已停止；本聊天未启动，标 unavailable。"
                               "请先恢复会话/授权后重跑" % (halted["after"], halted["reason"]),
                    "attempts": 0, "retryable": False, "failed_at": _utc_now()})
                halted["skipped"].append(chat_identity(one["chats"][0]))
                samples[str(item_root)] = {"elapsed": [], "ok": 0,
                                           "pending_total": len(plan), "stats": None,
                                           "window": None, "started": False}
                continue
            # I5：取锁成功后才清理上一轮崩溃遗留的超龄 *.part（复认领语义不变：sidecar
            # 缺正式文件时保留并申报）。被拒的运行走不到这里，绝不删别人正在写的半成品。
            pruned[str(item_root)] = prune_stale_temp_files(item_root)
            _reset_run_diagnostics(item_root, plan)
            reclaim_orphan_segments(item_root, task)
            window = _time_window(one)
            stats = {"applied": window is not None, "filtered_out": 0, "unverifiable": 0}
            pending = scan_completed(item_root, plan)["pending"]
            elapsed, ok, stopped = [], 0, False
            for item in pending:
                try:
                    refresh_lock(handle["base"], handle["token"])
                except (OSError, ValueError):
                    pass  # 心跳尽力而为：续不上只影响租约，不中断任务
                moment = time.monotonic()
                outcome = _attempt_segment(item_root, one, item, command_builder, runner,
                                           retries, sleeper, window=window, stats=stats,
                                           subprocess_timeout=subprocess_timeout)
                if outcome == "ok":
                    ok += 1
                    elapsed.append(round(time.monotonic() - moment, 3))
                elif outcome == "stopped":
                    stopped = True
                    break
            if "json" in one["outputs"] and not scan_completed(item_root, plan)["pending"]:
                try:
                    merge_completed(item_root, plan, final_target(item_root))
                except ValueError as error:
                    _record_failure(item_root, {"segment_id": None, "kind": "unknown",
                                                "message": "合并发布未完成：%s" % error,
                                                "attempts": 0, "retryable": False,
                                                "failed_at": _utc_now()})
            samples[str(item_root)] = {"elapsed": elapsed, "ok": ok,
                                       "pending_total": len(pending), "stats": stats,
                                       "window": window, "started": True}
            if stopped and halted is None:
                halted = {"after": chat_identity(one["chats"][0]),
                          "reason": _latest_failure_message(item_root), "skipped": []}
    finally:
        try:
            release_session_lock(handle)
        except (OSError, RuntimeError, ValueError) as error:
            lock = {"released": False, "path": token["path"], "marker": handle["marker"],
                    "reason": str(error)}
            if token.get("recovered_from"):
                lock["recovered_from"] = token["recovered_from"]
            for item_root, one, _task, _plan in prepared:
                _record_failure(item_root, {
                    "segment_id": None, "kind": "lock_release",
                    "message": "%s：不伪装已释放，锁文件 %s 需人工确认后处理"
                               % (error, token["path"]), "attempts": 0, "retryable": False,
                    "failed_at": _utc_now()})
    reports = []
    for item_root, one, _task, _plan in prepared:
        base = build_status(item_root, one)
        info = samples.get(str(item_root)) or {"elapsed": [], "ok": 0, "pending_total": 0,
                                                "stats": None, "window": None,
                                                "started": False}
        base["progress"] = _progress_record(info["elapsed"],
                                            info["pending_total"] - info["ok"],
                                            info["window"], info["stats"], info["started"])
        report = _write_status_file(item_root, base)
        report["temp_files"] = pruned.get(str(item_root)) \
            or _untouched_temp_record(item_root)
        reports.append(report)
    result = _aggregate_reports(reports, lock)
    if halted is not None and len(reports) > 1:
        result["stopped_early"] = halted
    return result
