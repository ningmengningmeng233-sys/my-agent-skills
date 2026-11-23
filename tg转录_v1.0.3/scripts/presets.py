"""常用方案的保存与调用（Task 9）。

只在用户**主动要求**时保存方案，只在用户**主动调用**时解析方案：本模块不启动任何
任务、不创建定时或无人值守作业（不引入子进程模块、不调用任何启动入口、不读排程
接口），真实账号验证一律申报 NOT_RUN。

保存的字段只有两类：用户选择项（``PRESET_FIELDS``）与**安全的本地账号引用**
（``account.ref``/``label``）。验证码、密码、会话库/会话目录、命名空间、可执行路径、
快照上界与带 ``user:pass`` 的代理 URL 属运行配置或凭据，永不写进方案文件：能从完整
运行配置里分出来的，按申报的丢弃原因处理；偷藏进白名单字段的，整个保存动作直接拒绝，
且拒绝时不落任何文件。

范围语义：绝对日期/ID/all/last 原样保存、原样复用（存 2026-09 下次仍导出 2026-09）；
相对范围只支持 ``SUPPORTED_RELATIVE`` 里的表达式，复用时按**调用方传入的 now** 与任务
时区重新解析并展示摘要——本模块绝不读真实时钟（``now`` 缺失或无时区就回问，不猜）。

复用与不重复提问：本轮 overrides 逐字段**替换**保存值（不做并集），只覆盖用户真正重述
的字段；解析结果重新过 ``job.validate_job`` 与 ``preflight.inspect_environment``，因此
方案里偷渡的非法或越权选择一律拒绝；方案字段已把本次任务答全时 ``questions`` 为空，
不再向用户重复提问。账号或目标在本次清单里无法唯一解析（换账号、重名目标、身份冲突）
时拒绝默选，要求重新选择。

原子落盘复用 ``state`` 里唯一的一份发布实现（``state._publish_json`` →
``state.publish_text_files``，临时文件 + ``os.replace``），本模块不写第二份发布。
方案目录不得位于技能发布包内，成品包里永远不会有方案文件或真实账号引用。

依赖：Python 标准库。不联网、不登录、不执行 tdl、不读写真实归档。
"""

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePath

try:  # zoneinfo 需要系统/包内时区数据库；缺库时按「无法解析时区」回问，不猜
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover - 仅老环境
    ZoneInfo = None

    class ZoneInfoNotFoundError(Exception):
        pass

import state
from job import (CLEANING_CHOICES, CLI_ACTIONS, CONTENT_TYPES, COUNT_UNITS,
                 ONLINE_ACTIONS, OUTPUT_TYPES, PEER_TYPES, RANGE_MODES,
                 REQUIRED_ONLINE_RUNTIME, job_summary, validate_job)

EXIT_COMPLETE = 0
EXIT_REJECTED = 2
EXIT_CAPABILITY = 3

SCHEMA_VERSION = 1
STATE_FILE_SUFFIX = ".json"
MAX_NAME_LENGTH = 80

# 可保存的用户选择项（白名单一次收敛：Task 1 裁决 5 的欠账）
PRESET_FIELDS = ("action", "account", "chats", "range", "contents", "outputs",
                 "destination", "sources", "cleaning", "template_compression",
                 "provenance")
# 执行者的运行配置：本轮可用，永不落方案文件
RUNTIME_FIELDS = ("tdl", "storage", "namespace", "proxy", "retries", "concurrency",
                  "segment_width", "lock_root", "snapshot")
# 一眼可辨的凭据字段：本轮也不参与解析，出现即拒绝
CREDENTIAL_FIELDS = ("password", "passwd", "passcode", "verification_code", "otp",
                     "token", "secret", "api_key", "apikey", "private_key",
                     "credential", "credentials", "cookie", "cookies", "session_data")
# 解析用的本轮上下文：只在内存里，既不保存也不进入 job
CONTEXT_FIELDS = ("accounts", "chat_candidates")

RUNTIME_DROP_REASONS = {
    "tdl": "tdl 可执行路径属执行者配置，不保存（换机器就失效，且与凭据面同源）",
    "storage": "会话存储目录指向会话库位置，属执行者配置，不保存",
    "namespace": "命名空间可能是账号标识，属执行者配置，不保存",
    "proxy": "代理 URL 可能带认证信息（user:pass），属运行配置与凭据，不保存",
    "retries": "重试次数属执行者配置，不保存",
    "concurrency": "并发属执行者配置，不保存",
    "segment_width": "分段宽度属执行者配置，不保存",
    "lock_root": "锁根属执行者配置，不保存",
    "snapshot": "快照上界每轮重新冻结，保存它会让复用时扩大范围，不保存",
}
CREDENTIAL_DROP_REASONS = {
    "default": "凭据类字段（验证码/密码/会话内容/密钥）永不保存",
}
# 深扫：白名单字段内部出现这些键名同样拒绝（归一化精确匹配，见 _is_credential_key）
BANNED_KEY_TOKENS = ("password", "passwd", "passcode", "otp", "token", "secret",
                     "api_key", "apikey", "private_key", "credential", "cookie",
                     "session", "verification")
# C-1/I-3：归一化后的精确 token 表（verify 单列；复数只补 credential/cookie/secret，
# 不补 sessions——sessions_seen 这类正常词不得误伤；verifycode 覆盖 verify-code 变体）
_CREDENTIAL_EXTRA_TOKENS = ("verify", "verifycode", "verificationcode",
                            "credentials", "cookies", "secrets")


def _normalize_credential_token(text):
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


_CREDENTIAL_NORM_TOKENS = frozenset(
    _normalize_credential_token(item)
    for item in BANNED_KEY_TOKENS + _CREDENTIAL_EXTRA_TOKENS
)
# 中文凭据词（值侧）：命中即整次拒绝，拒绝原因只点名命名字段、不回显值本身
_CHINESE_CREDENTIALS = ("密码", "验证码", "口令", "密钥", "令牌")
# 手机号形态：+8613800001111 / 138 0000 1111 / 8613800001111 / 138-0000-1111，
# 前后必须是非数字边界；-1001789123456（-100 开头、13 位）不在此形，不误伤
_PHONE_SHAPE = re.compile(
    r"(?<!\d)(?:\+?86[\s\-]*)?1[3-9]\d[\s\-]?\d{4}[\s\-]?\d{4}(?!\d)")
# 会话库文件形态：要求前导点（*.session），普通含 session 子串的合法词不判死
_SESSION_SHAPE = re.compile(r"\.session(?![A-Za-z0-9_])", re.IGNORECASE)
# 无 scheme 的 user:pass@host（如 mg:secRet@10.0.0.1）；合法 URL/时间/中文全角冒号不命中
_SCHEMELESS_USERPASS = re.compile(
    r"(?<![A-Za-z0-9_\-.\/:])[^\/\s:@]{1,64}:[^\/\s:@]{1,64}@[^\/\s@]+")


def _is_credential_key(key):
    """键名判定：归一化去分隔符后精确比对 + 按词切分后精确比对。

    pass_word -> password（归一化命中）；verify-code -> verify（切词命中）；
    sessions_seen -> sessionsseen/['sessions','seen']（均不等于 session，不误伤）。
    """
    text = str(key)
    for word in _CHINESE_CREDENTIALS:
        if word in text:
            return True
    lowered = text.lower()
    normalized = re.sub(r"[^a-z0-9]+", "", lowered)
    if normalized and normalized in _CREDENTIAL_NORM_TOKENS:
        return True
    for part in re.split(r"[^a-z0-9]+", lowered):
        if part and part in _CREDENTIAL_NORM_TOKENS:
            return True
    return False


def _value_has_credential_token(text):
    """自由文本里的英文凭据词：按候选词归一化精确比对，不做子串包含。"""
    lowered = str(text).lower()
    for match in re.finditer(r"[a-z0-9]+(?:[_\-\.][a-z0-9]+)*", lowered):
        candidate = match.group(0)
        normalized = re.sub(r"[^a-z0-9]+", "", candidate)
        if normalized and normalized in _CREDENTIAL_NORM_TOKENS:
            return True
        for subword in re.split(r"[_\-\.]+", candidate):
            if subword and subword in _CREDENTIAL_NORM_TOKENS:
                return True
    return False

RELATIVE_MODE = "relative"
_RELATIVE_DAYS = re.compile(r"^last_(\d{1,4})_days$")
RELATIVE_FIXED_EXPRESSIONS = ("today", "yesterday", "this_month", "last_month")
SUPPORTED_RELATIVE = RELATIVE_FIXED_EXPRESSIONS + ("last_N_days（如 last_7_days）",)
_RELATIVE_HELP = "、".join(SUPPORTED_RELATIVE)

_URL_CREDENTIALS = re.compile(r"://[^/\s@]*:[^/\s@]+@")
_NAME_FORBIDDEN = re.compile(r"[\x00-\x1f<>:\"/\\|?*]")

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_ONLINE_REQUIRED = tuple(REQUIRED_ONLINE_RUNTIME)


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _digest_name(name):
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]


def _checked_directory(directory):
    base = Path(directory)
    if not _text(str(directory)):
        raise ValueError("方案目录必须是非空路径（不默认写到当前目录）")
    resolved = base.resolve() if base.exists() else Path(os.path.abspath(str(base)))
    if str(resolved) == str(PACKAGE_ROOT):
        raise ValueError("方案目录不能是技能发布包根目录：%s" % PACKAGE_ROOT)
    try:
        common = os.path.commonpath([str(resolved), str(PACKAGE_ROOT)])
    except ValueError:      # 不同盘符（Windows）时没有公共路径，必然不在包内
        common = None
    if common is not None and Path(common) == PACKAGE_ROOT:
        raise ValueError("方案目录不得位于技能发布包内（成品包不得带上用户方案）：%s" % resolved)
    return resolved


def _checked_name(name):
    if not isinstance(name, str):
        raise ValueError("方案名必须是字符串")
    if name != name.strip() or not name.strip():
        raise ValueError("方案名不能为空或带首尾空白")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError("方案名过长（最多 %d 个字符）" % MAX_NAME_LENGTH)
    if _NAME_FORBIDDEN.search(name) or name in (".", "..") or "\u202e" in name:
        raise ValueError("方案名含有路径或控制字符，不能用来定位方案：%s" % name)
    return name


def _preset_filename(name):
    return "%s--%s.json" % (state.identity_slug(name), _digest_name(name))


# ------------------------------------------------------------------ 凭据卫生

def _scan_value_blobs(value, trail):
    """深扫白名单字段：凭据键名与凭据值形态都拒绝，报错不回显值本身。

    值侧覆盖：归一化去分隔符后比 token 表、无 scheme 的 user:pass@host、
    手机号形态、会话库文件形态（*.session）、中文凭据词。命中即整次拒绝。
    """
    problems = []
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            path = "%s.%s" % (trail, key)
            if _is_credential_key(key):
                problems.append("字段 %s 里出现凭据类键名 %s" % (path, key))
            else:
                problems.extend(_scan_value_blobs(item, path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            problems.extend(_scan_value_blobs(item, "%s[%d]" % (trail, index)))
    elif isinstance(value, str):
        if _URL_CREDENTIALS.search(value):
            problems.append("字段 %s 里的 URL 带认证信息（user:pass）" % trail)
        elif _SCHEMELESS_USERPASS.search(value):
            problems.append("字段 %s 里的值出现 user:pass@host 形态" % trail)
        elif _SESSION_SHAPE.search(value):
            problems.append("字段 %s 里的值出现会话库文件形态" % trail)
        elif _PHONE_SHAPE.search(value):
            problems.append("字段 %s 里的值出现号码形态" % trail)
        elif any(word in value for word in _CHINESE_CREDENTIALS):
            problems.append("字段 %s 里的值出现凭据类字样" % trail)
        elif _value_has_credential_token(value):
            problems.append("字段 %s 里的值出现凭据类字样" % trail)
    return problems


def _check_account_block(account):
    if not isinstance(account, dict):
        raise ValueError("account 必须是映射（本地账号引用：ref/label）")
    extra = set(account) - {"ref", "label"}
    if extra:
        raise ValueError("account 只允许 ref/label，多出：" + "、".join(sorted(extra))
                         + "（会话库、命名空间、手机号等账号标识不保存）")
    ref = account.get("ref")
    if not _text(ref):
        raise ValueError("account.ref 缺失：方案需要安全的本地账号引用")
    if len(str(ref)) > 64 or _NAME_FORBIDDEN.search(str(ref)) or "@" in str(ref) \
            or re.search(r"\+\d", str(ref)) or any(sep in str(ref) for sep in ("/", "\\")):
        raise ValueError("account.ref 不是安全的本地引用（不得含路径、分隔符或号码形态）")
    if "label" in account and not _text(account["label"]):
        raise ValueError("account.label 需要是非空短字符串")
    if "label" in account and len(str(account["label"])) > 64:
        raise ValueError("account.label 过长（最多 64 个字符）")


# ------------------------------------------------------------------ 选择项形状

def _check_chats_field(chats):
    if not isinstance(chats, list) or not chats:
        raise ValueError("chats 必须是非空列表（用户选中的当前清单项，不默认全账号）")
    for index, chat in enumerate(chats):
        if not isinstance(chat, dict):
            raise ValueError("chats 第 %d 项必须是映射" % (index + 1))
        extra = set(chat) - {"peer_type", "id", "display_name", "locator"}
        if extra:
            raise ValueError("chats 第 %d 项含方案不保存的键：%s"
                             % (index + 1, "、".join(sorted(map(str, extra)))))
        if chat.get("peer_type") not in PEER_TYPES:
            raise ValueError("chats 第 %d 项 peer_type 不受支持（仅 user/chat/channel）"
                             % (index + 1))
        if not _positive_int(chat.get("id")):
            raise ValueError("chats 第 %d 项 id 必须是正整数" % (index + 1))
        for key in ("display_name", "locator"):
            if not _text(chat.get(key)):
                raise ValueError("chats 第 %d 项缺少 %s" % (index + 1, key))


def _check_relative_range(scope):
    expression = scope.get("expression")
    if not _text(expression):
        raise ValueError("range.expression 缺失：相对范围只能用受支持表达式")
    days = _RELATIVE_DAYS.match(str(expression))
    supported = str(expression) in RELATIVE_FIXED_EXPRESSIONS or (
        days is not None and int(days.group(1)) >= 1)
    if not supported:
        raise ValueError("相对范围表达式不受支持：%s；只支持 %s，其他自然语言请由代理澄清后"
                         "再保存（不自行猜日期）" % (expression, _RELATIVE_HELP))
    if not _text(scope.get("timezone")):
        raise ValueError("range.timezone 缺失：相对范围必须写清用哪个时区解析")


def _check_range_field(scope):
    if not isinstance(scope, dict) or not scope:
        raise ValueError("range 必须是非空映射（范围由用户选择，不默认全量）")
    mode = scope.get("mode")
    if mode == RELATIVE_MODE:
        if set(scope) - {"mode", "expression", "timezone"}:
            raise ValueError("相对范围只包含 mode/expression/timezone，收到："
                             + "、".join(sorted(k for k in scope
                                                if k not in ("mode", "expression", "timezone"))))
        _check_relative_range(scope)
        return
    if mode not in RANGE_MODES:
        raise ValueError("range.mode 不受支持：%s（可用 %s，另加 relative）"
                         % (mode, "、".join(RANGE_MODES)))
    allowed = {"all": set(), "time": {"start", "end"}, "id": {"lower", "upper"},
               "last": {"n", "count_unit"}}[mode]
    extra = set(scope) - allowed - {"mode"}
    if extra:
        raise ValueError("range 含与 mode=%s 无关的字段：%s"
                         % (mode, "、".join(sorted(map(str, extra)))))
    if mode == "time":
        for key in ("start", "end"):
            if not _text(scope.get(key)):
                raise ValueError("range.%s 必须是带时区的 ISO 日期" % key)
            if _zone_of(scope[key]) is None:
                raise ValueError("range.%s 不是可解析的带时区 ISO 日期（不自行补时区）" % key)
    elif mode == "id":
        for key in ("lower", "upper"):
            if not _positive_int(scope.get(key)):
                raise ValueError("range.%s 必须是正整数消息 ID" % key)
    elif mode == "last":
        if not _positive_int(scope.get("n")):
            raise ValueError("range.n 必须是正整数")
        if scope.get("count_unit") not in COUNT_UNITS:
            raise ValueError("range.count_unit 不受支持（仅 messages/matching_messages）")


def _check_selection_shapes(stored):
    action = stored.get("action")
    if action not in CLI_ACTIONS:
        raise ValueError("action 缺失或不是受支持的子命令，方案保存不下来")
    if action == "preset":
        raise ValueError("方案不能保存 action=preset（保存动作本身不是被保存的任务）")
    if "account" in stored:
        _check_account_block(stored["account"])
    if "chats" in stored:
        _check_chats_field(stored["chats"])
    if "range" in stored:
        _check_range_field(stored["range"])
    if "contents" in stored:
        value = stored["contents"]
        if not isinstance(value, list) or not value:
            raise ValueError("contents 必须是非空列表（不默认纯文字或全部媒体）")
        unknown = [item for item in value if item not in CONTENT_TYPES]
        if unknown:
            raise ValueError("contents 含不支持的类别：" + "、".join(map(str, unknown))
                             + "（unknown 不是可默选类别）")
    if "outputs" in stored:
        value = stored["outputs"]
        if not isinstance(value, list) or not value:
            raise ValueError("outputs 必须是非空列表")
        unknown = [item for item in value if item not in OUTPUT_TYPES]
        if unknown:
            raise ValueError("outputs 含不支持的输出：" + "、".join(map(str, unknown)))
    if "cleaning" in stored and stored["cleaning"] not in CLEANING_CHOICES:
        raise ValueError("cleaning 取值不受支持（仅 none/ads）")
    if "template_compression" in stored and not isinstance(
            stored["template_compression"], bool):
        raise ValueError("template_compression 必须是布尔值")
    if "provenance" in stored:
        value = stored["provenance"]
        if not isinstance(value, dict) or not all(
                _text(key) and _text(item) for key, item in value.items()):
            raise ValueError("provenance 必须是「字段名 -> 用户原话」的字符串映射")
    if "sources" in stored:
        value = stored["sources"]
        if not isinstance(value, list) or not value:
            raise ValueError("sources 必须是非空列表（只放用户明确选定的路径）")
        for item in value:
            if not _text(item) or not PurePath(item).is_absolute():
                raise ValueError("sources 每项必须是绝对路径字符串")
            if any("*" in part or "?" in part for part in PurePath(item).parts):
                raise ValueError("sources 不接受通配路径")
    if "destination" in stored and not _text(stored["destination"]):
        raise ValueError("destination 需要是非空绝对路径字符串")


def _split_fields(selection):
    stored, dropped = {}, []
    for key, value in selection.items():
        name = str(key)
        if name in PRESET_FIELDS:
            stored[name] = copy.deepcopy(value)
        elif name in RUNTIME_FIELDS:
            dropped.append({"field": name, "reason": RUNTIME_DROP_REASONS[name]})
        elif name in CREDENTIAL_FIELDS:
            dropped.append({"field": name, "reason": CREDENTIAL_DROP_REASONS["default"]})
        else:
            raise ValueError("常用方案不接受字段：%s（可保存的选择字段只有：%s）"
                             % (name, "、".join(PRESET_FIELDS)))
    return stored, sorted(dropped, key=lambda item: item["field"])


# ------------------------------------------------------------------ 时区与相对范围

def _zone_of(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _resolve_timezone(name):
    spec = str(name)
    if ZoneInfo is not None:
        try:
            return ZoneInfo(spec)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    if spec in ("UTC", "Z"):
        return timezone.utc
    match = re.match(r"^[+-](\d{2}):(\d{2})$", spec)
    if match:
        sign = -1 if match.group(1).startswith("-") else 1
        return timezone(sign * timedelta(hours=int(match.group(1)),
                                         minutes=int(match.group(2))))
    return None


def _month_start(moment):
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start(moment):
    if moment.month == 12:
        return moment.replace(year=moment.year + 1, month=1, day=1,
                              hour=0, minute=0, second=0, microsecond=0)
    return _month_start(moment).replace(month=moment.month + 1)


def _relative_window(expression, now, zone):
    """受支持相对表达式 -> 任务时区里的 [起点包含, 终点不包含)。"""
    local = now.astimezone(zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    days = _RELATIVE_DAYS.match(expression)
    if expression == "today":
        start, end = midnight, midnight + timedelta(days=1)
    elif expression == "yesterday":
        start, end = midnight - timedelta(days=1), midnight
    elif expression == "this_month":
        start, end = _month_start(local), _next_month_start(local)
    elif expression == "last_month":
        end = _month_start(local)
        start = _month_start(end - timedelta(days=1))
    elif days is not None:
        start, end = midnight - timedelta(days=int(days.group(1))), midnight
    else:
        return None
    return start, end


def _utc_iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


# ------------------------------------------------------------------ 合同复验（不落盘）

def _synthetic_runtime(base):
    """只为把「缺运行配置」与「选择项本身非法」分开而构造，永不保存、永不返回。"""
    return {"tdl": str(base / "__validate_only_tdl__"),
            "storage": str(base / "__validate_only_session__"),
            "namespace": "validate-only"}


# 相对范围在方案层是表达式，交给 validate_job 前只换一个「合法时间窗」占位进去做组合
# 复验（表达式本身由 _check_relative_range 严格把关）；这个窗口永不保存、永不返回。
_VALIDATION_ONLY_WINDOW = {"mode": "time", "start": "2000-01-01T00:00:00+00:00",
                           "end": "2000-01-02T00:00:00+00:00"}


def _selection_problems(selection, base):
    probe = {key: copy.deepcopy(value) for key, value in selection.items()
             if key != "account"}
    scope = probe.get("range")
    if isinstance(scope, dict) and scope.get("mode") == RELATIVE_MODE:
        probe["range"] = dict(_VALIDATION_ONLY_WINDOW)
    for key, value in _synthetic_runtime(base).items():
        probe.setdefault(key, value)
    try:
        validate_job(probe)
    except ValueError as error:
        return [str(error)]
    return []


def _missing_runtime_fields(selection):
    if selection.get("action") not in ONLINE_ACTIONS:
        return []
    return [key for key in _ONLINE_REQUIRED if key not in selection]


# ------------------------------------------------------------------ 保存

def save_preset(directory, name, selection, *, overwrite=False):
    """原子保存命名方案；只有调用方明确要求保存时才写盘，同名冲突需显式要求覆盖。"""
    base = _checked_directory(directory)
    safe_name = _checked_name(name)
    if not isinstance(selection, dict):
        raise ValueError("selection 必须是映射(dict)：方案保存的是本次选择项")
    stored, dropped = _split_fields(selection)
    problems = _scan_value_blobs(stored, "selection")
    if problems:
        raise ValueError("方案保存被拒绝（凭据不保存）：" + "；".join(problems))
    _check_selection_shapes(stored)
    invalid = _selection_problems(stored, base)
    if invalid:
        raise ValueError("方案内容不满足选择合同，未保存：" + "；".join(invalid))

    path = base / _preset_filename(safe_name)
    existed = path.is_file()
    if existed and not overwrite:
        return {"status": "exists", "name": safe_name, "path": str(path),
                "stored_fields": sorted(stored), "dropped": dropped,
                "questions": ["已存在同名方案「%s」。要顶掉旧方案请明确说「覆盖保存」，"
                              "否则请换个方案名（本模块不自动改名、不静默覆盖）" % safe_name],
                "reasons": [{"code": "name_exists", "field": "name",
                             "detail": "同名方案已存在，覆盖需要用户明确要求"}],
                "replaced": False, "started_task": False, "scheduled": False,
                "executed": False, "real_account_verified": "NOT_RUN"}

    payload = {"schema_version": SCHEMA_VERSION, "name": safe_name,
               "selection": stored, "dropped": dropped,
               "notes": {"saved_only_on_request": True, "started_task": False,
                         "scheduled": False, "real_account_verified": "NOT_RUN",
                         "relative_range_kept_as_expression": True}}
    state._publish_json(path, payload)      # 唯一的一份原子发布实现（临时文件 + replace）
    return {"status": "saved", "name": safe_name, "path": str(path),
            "stored_fields": sorted(stored), "dropped": dropped,
            "replaced": bool(existed), "questions": [], "reasons": [],
            "runtime_required_missing": _missing_runtime_fields(stored),
            "started_task": False, "scheduled": False, "executed": False,
            "real_account_verified": "NOT_RUN"}


def list_presets(directory):
    """只读列出方案：不解析、不校验、更不启动任何任务。"""
    base = _checked_directory(directory)
    items, unreadable = [], []
    for record, problem in _scan_directory(base):
        if problem:
            unreadable.append(problem)
            continue
        selection = record["selection"]
        scope = selection.get("range") or {}
        items.append({"name": record["name"], "path": record["path"],
                      "action": selection.get("action"),
                      "stored_fields": sorted(selection),
                      "relative_expression": scope.get("expression")
                      if scope.get("mode") == RELATIVE_MODE else None})
    return {"directory": str(base), "presets": sorted(items, key=lambda item: item["name"]),
            "unreadable": unreadable, "started_task": False, "scheduled": False}


def _scan_directory(base):
    records = []
    if not base.is_dir():
        return records
    for path in sorted(base.glob("*" + STATE_FILE_SUFFIX)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            records.append((None, {"file": path.name,
                                   "reason": "方案文件读不出来（%s）" % type(error).__name__}))
            continue
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION \
                or not _text(payload.get("name")) or not isinstance(
                payload.get("selection"), dict):
            version = payload.get("schema_version") if isinstance(payload, dict) else None
            records.append((None, {"file": path.name,
                                   "reason": "方案文件结构不受支持（schema_version=%s）" % version}))
            continue
        records.append(({"name": payload["name"], "selection": payload["selection"],
                         "dropped": payload.get("dropped") or [], "path": str(path)}, None))
    return records


# ------------------------------------------------------------------ 调用

def _refuse(status, wanted, reasons, questions, extra=None):
    result = {"status": status, "name": wanted, "path": None, "job": None,
              "summary": None, "range_resolution": None, "applied_overrides": [],
              "questions": questions, "reasons": reasons, "capabilities": None,
              "resolved_account": None, "account_ref": None, "stored_fields": [],
              "missing_runtime": [], "started_task": False, "scheduled": False,
              "executed": False, "real_account_verified": "NOT_RUN"}
    if extra:
        result.update(extra)
    return result


def _reason(code, field, detail):
    return {"code": code, "field": field, "detail": detail}


def _find_preset(base, wanted):
    matches, unreadable = [], []
    for record, problem in _scan_directory(base):
        if problem:
            unreadable.append(problem["file"])
            continue
        if record["name"] == wanted:
            matches.append(record)
    if len(matches) == 1:
        return matches[0], unreadable, None
    if len(matches) > 1:
        return None, unreadable, _reason("duplicate_preset_name", "name",
                                         "有 %d 个方案都叫「%s」，不静默挑一个"
                                         % (len(matches), wanted))
    return None, unreadable, None


def _resolve_account(stored, overrides, applied):
    """账号引用解析：换账号或无法唯一确定时拒绝默选。

    C-2：本轮重述了 account 但只给 label（无 ref）时，不用 label 兜底匹配到
    另一个账号并沿用旧目标，直接走无法唯一解析 -> 回问；成功时 account_ref
    取 resolved_account 的 ref，保证两者同源。
    """
    preset_account = stored.get("account") or {}
    account_restated = "account" in applied
    current_account = (overrides.get("account") or {}) if account_restated else {}
    preset_ref = preset_account.get("ref")
    current_ref = current_account.get("ref") if isinstance(current_account, dict) else None
    if account_restated and not current_ref:
        label = current_account.get("label") if isinstance(current_account, dict) else None
        detail = ("本轮只给了账号名称%s，未给本地引用 ref，无法唯一确定账号："
                  "换账号时不沿用旧目标，请重新选择账号与聊天对象"
                  % ("「%s」" % label if _text(label) else ""))
        return {"account_ref": None, "resolved_account": None,
                "problems": [("account_unavailable", "account", detail)]}
    effective_ref = current_ref or preset_ref
    restated_targets = "chats" in applied
    result = {"account_ref": effective_ref, "resolved_account": None, "problems": []}
    if effective_ref and preset_ref and effective_ref != preset_ref and not restated_targets:
        result["problems"].append(
            ("account_changed", "account",
             "本轮账号是「%s」，方案「%s」里的目标属于账号「%s」：换账号时不沿用旧目标，"
             "请重新选择账号与聊天对象" % (effective_ref, preset_ref, preset_ref)))
        return result
    accounts = overrides.get("accounts")
    if not effective_ref or not isinstance(accounts, list) or not accounts:
        return result
    matches = [item for item in accounts if isinstance(item, dict)
               and item.get("ref") == effective_ref]
    if len(matches) > 1:
        result["problems"].append(
            ("account_ambiguous", "account",
             "本次可用账号里有 %d 个都能对上方案记录的账号「%s」，无法唯一确定，"
             "请重新选择账号" % (len(matches), effective_ref)))
    elif not matches:
        result["problems"].append(
            ("account_unavailable", "account",
             "方案记录的账号「%s」不在本次可用账号里（换账号或账号丢失），"
             "请重新选择账号与聊天对象" % effective_ref))
    else:
        result["resolved_account"] = copy.deepcopy(matches[0])
        result["account_ref"] = matches[0].get("ref")
    return result


def _resolve_targets(stored, overrides, applied):
    """目标解析：本轮重述优先；本次清单里重名或身份冲突时拒绝默选。"""
    if "chats" in applied:
        return []
    candidates = overrides.get("chat_candidates")
    if not isinstance(candidates, list) or not candidates:
        return []          # 本轮没有清单：按方案里已固化的身份与定位值复用，不猜新目标
    problems = []
    for target in stored.get("chats", []):
        same_id = [item for item in candidates if isinstance(item, dict)
                   and (item.get("peer_type"), item.get("id"))
                   == (target.get("peer_type"), target.get("id"))]
        if len(same_id) > 1:
            problems.append(("duplicate_target", "chats",
                            "本次清单里有 %d 个条目标着同一个身份 %s:%s，无法唯一确定，"
                            "请重新选择目标" % (len(same_id), target.get("peer_type"),
                                                target.get("id"))))
            continue
        if len(same_id) == 1:
            continue
        same_name = [item for item in candidates if isinstance(item, dict)
                     and item.get("display_name") == target.get("display_name")]
        if len(same_name) > 1:
            problems.append(("duplicate_target", "chats",
                            "本次清单里有 %d 个重名目标「%s」，无法唯一确定，"
                            "请按 peer 类型 + ID 重新选择" % (len(same_name),
                                                              target.get("display_name"))))
        elif not same_name:
            problems.append(("target_unresolved", "chats",
                            "方案里的目标「%s」在本次清单中找不到，请重新选择目标"
                            % target.get("display_name")))
    return problems


def _adopt_candidates(stored, overrides, applied):
    if "chats" in applied or "chats" not in stored:
        return None          # 本轮重述过的目标直接生效，不再拿方案旧目标覆盖回去
    candidates = overrides.get("chat_candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    adopted = []
    for target in stored["chats"]:
        same_id = [item for item in candidates if isinstance(item, dict)
                   and (item.get("peer_type"), item.get("id"))
                   == (target.get("peer_type"), target.get("id"))]
        if len(same_id) == 1:
            adopted.append(copy.deepcopy(same_id[0]))
            continue
        same_name = [item for item in candidates if isinstance(item, dict)
                     and item.get("display_name") == target.get("display_name")]
        adopted.append(copy.deepcopy(same_name[0]) if len(same_name) == 1
                       else copy.deepcopy(target))
    return adopted


def _resolve_range(stored, now):
    """绝对范围原样返回；相对范围只按传入 now + 任务时区解析。"""
    scope = stored.get("range")
    if not isinstance(scope, dict) or scope.get("mode") != RELATIVE_MODE:
        return scope, None, []
    if now is None:
        return scope, None, [("now_required", "range",
                              "相对范围需要本次调用时间（now）才能解析：本模块不读真实时钟，"
                              "请把本次时间传进来再由用户确认")]
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return scope, None, [("now_naive", "now",
                             "传入的 now 没有时区：不猜时区，请按任务时区给出带时区的时刻")]
    zone = _resolve_timezone(scope.get("timezone"))
    if zone is None:
        return scope, None, [("timezone_unsupported", "range",
                             "方案记录的时区「%s」无法解析（不退回本机时区），"
                             "请重新选择时区" % scope.get("timezone"))]
    try:
        expression = str(scope.get("expression"))
        window = _relative_window(expression, now, zone)
    except (ValueError, OverflowError, OSError):
        window = None
    if window is None:
        return scope, None, [("expression_unsupported", "range",
                             "相对表达式「%s」不受支持，只按 %s 解析，其他说法请回问用户"
                             % (scope.get("expression"), _RELATIVE_HELP))]
    start, end = window
    resolved = {"mode": "time", "start": _utc_iso(start), "end": _utc_iso(end)}
    detail = {"expression": scope.get("expression"), "timezone": str(scope.get("timezone")),
              "now": now.isoformat(timespec="seconds"),
              "start": start.isoformat(), "end": end.isoformat()}
    return resolved, detail, []


def resolve_preset(directory, name, overrides, now):
    """按本轮原话解析方案：逐字段替换、重新过合同与能力检查，歧义一律回问。"""
    base = _checked_directory(directory)
    wanted = _checked_name(name)
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ValueError("overrides 必须是映射（本轮用户重述的字段与本次上下文）")
    bad = [key for key in overrides if key not in
           PRESET_FIELDS + RUNTIME_FIELDS + CONTEXT_FIELDS]
    if bad:
        raise ValueError("overrides 含不参与方案解析的字段：%s（可重述的选择字段：%s；"
                         "凭据与运行配置请按运行配置给出）"
                         % ("、".join(sorted(map(str, bad))), "、".join(PRESET_FIELDS)))
    credential_keys = [key for key in overrides if key in CREDENTIAL_FIELDS]
    if credential_keys:
        raise ValueError("凭据类字段不参与方案解析：" + "、".join(sorted(credential_keys)))
    applied_preview = sorted(key for key in overrides if key in PRESET_FIELDS)
    override_problems = []
    for key in applied_preview:
        override_problems.extend(
            _scan_value_blobs(overrides[key], "overrides.%s" % key))
    if override_problems:
        raise ValueError("本轮重述被拒绝（凭据不保存）：" + "；".join(override_problems))

    record, unreadable, ambiguity = _find_preset(base, wanted)
    if ambiguity:
        return _refuse("needs_choice", wanted, [ambiguity],
                       ["磁盘上有多个都叫「%s」的方案，请让用户点名或改名" % wanted],
                       {"path": str(base)})
    if record is None:
        questions = ["没有找到名为「%s」的常用方案；请让用户重新选择范围，"
                     "或明确要求保存为新方案" % wanted]
        if unreadable:
            questions.append("以下方案文件读不出来，不能被当作可用方案：%s"
                             % "、".join(unreadable))
        return _refuse("missing", wanted,
                       [_reason("preset_not_found", "name", "方案不存在")], questions,
                       {"path": str(base), "unreadable": unreadable})

    stored = record["selection"]
    smuggled = sorted(set(stored) - set(PRESET_FIELDS))
    if smuggled:
        return _refuse("needs_choice", wanted,
                       [_reason("corrupt_preset", "、".join(smuggled),
                                "方案文件里带着不该保存的字段，拒绝使用（不代为丢弃、"
                                "也不采用其中的凭据）")],
                       ["方案「%s」被手工改动过，含非选择字段：%s；请让用户重新确认并保存"
                        % (wanted, "、".join(smuggled))],
                       {"path": record["path"]})
    if _scan_value_blobs(stored, "selection"):
        return _refuse("needs_choice", wanted,
                       [_reason("corrupt_preset", "selection",
                                "方案文件里出现凭据形态的内容，拒绝使用")],
                       ["方案「%s」里出现凭据形态的内容，请重新保存一份干净方案" % wanted],
                       {"path": record["path"]})
    try:
        _check_selection_shapes(stored)
    except ValueError as error:
        return _refuse("needs_choice", wanted,
                       [_reason("corrupt_preset", "selection", str(error))],
                       ["方案「%s」内容不合法：%s" % (wanted, error)],
                       {"path": record["path"]})

    applied = sorted(key for key in overrides if key in PRESET_FIELDS)
    merged = copy.deepcopy(stored)
    for key in applied:
        merged[key] = copy.deepcopy(overrides[key])

    account = _resolve_account(stored, overrides, applied)
    if account["problems"]:
        code, field, detail = account["problems"][0]
        return _refuse("needs_choice", wanted, [_reason(code, field, detail)],
                       [detail + "（本轮已重述的字段：%s）" % ("、".join(applied) or "无")],
                       {"path": record["path"], "applied_overrides": applied,
                        "account_ref": account["account_ref"]})
    target_problems = _resolve_targets(stored, overrides, applied)
    if target_problems:
        code, field, detail = target_problems[0]
        return _refuse("needs_choice", wanted, [_reason(code, field, detail)],
                       [detail], {"path": record["path"],
                                  "applied_overrides": applied,
                                  "account_ref": account["account_ref"]})
    adopted = _adopt_candidates(stored, overrides, applied)
    if adopted is not None:
        merged["chats"] = adopted

    scope, range_detail, range_problems = _resolve_range(merged, now)
    if range_problems:
        code, field, detail = range_problems[0]
        return _refuse("needs_choice", wanted, [_reason(code, field, detail)],
                       [detail], {"path": record["path"], "applied_overrides": applied,
                                  "account_ref": account["account_ref"],
                                  "stored_fields": sorted(stored)})
    job_fields = {key: value for key, value in merged.items() if key != "account"}
    if "range" in job_fields:
        job_fields["range"] = copy.deepcopy(scope)
    for key in RUNTIME_FIELDS:
        if key in overrides:
            job_fields[key] = copy.deepcopy(overrides[key])

    invalid = _selection_problems(job_fields, base)
    if invalid:
        return _refuse("needs_choice", wanted,
                       [_reason("invalid_selection", "job", " ".join(invalid))],
                       ["解析后的选择不满足合同，请让用户补齐或改口：%s" % " ".join(invalid),
                        "方案不会替用户补默认值（不自动加 markdown/json、不自动选清洗方式）"],
                       {"path": record["path"], "applied_overrides": applied,
                        "account_ref": account["account_ref"],
                        "stored_fields": sorted(stored)})

    missing_runtime = _missing_runtime_fields(job_fields)
    capabilities = inspect_capabilities(job_fields, base)
    if missing_runtime or capabilities["blocking"]:
        questions = []
        if missing_runtime:
            questions.append("本轮缺少在线运行配置：%s（属执行者配置，方案按设计不保存，"
                             "请本轮给出后再调用）" % "、".join(missing_runtime))
        for entry in capabilities["blocking"]:
            questions.append("能力缺口：%s＝%s（%s）" % (entry["capability"],
                                                       entry["status"], entry["reason"]))
        return _refuse("capability_gap", wanted,
                       [_reason("capability_gap", "、".join(missing_runtime) or "capability",
                                "本轮运行配置或环境能力不足，不假装任务能跑")],
                       questions, {"path": record["path"], "applied_overrides": applied,
                                   "account_ref": account["account_ref"],
                                   "stored_fields": sorted(stored),
                                   "capabilities": capabilities["entries"],
                                   "missing_runtime": missing_runtime})

    job = validate_job(job_fields)
    summary = job_summary(job)
    summary["preset"] = {"name": wanted, "relative_range": range_detail,
                         "account_ref": account["account_ref"]}
    summary["overrides_applied"] = applied
    return {"status": "resolved", "name": wanted, "path": record["path"],
            "job": job, "summary": summary, "range_resolution": range_detail,
            "applied_overrides": applied, "questions": [], "reasons": [],
            "capabilities": capabilities["entries"],
            "resolved_account": account["resolved_account"],
            "account_ref": account["account_ref"],
            "stored_fields": sorted(stored), "missing_runtime": [],
            "started_task": False, "scheduled": False, "executed": False,
            "real_account_verified": "NOT_RUN"}


def inspect_capabilities(job_fields, base):
    """解析后重新做能力检查：方案不能偷渡越权或环境不支持的选择。"""
    from preflight import inspect_environment
    action = job_fields.get("action")
    probe = {key: copy.deepcopy(value) for key, value in job_fields.items()
             if key != "account"}
    for key, value in _synthetic_runtime(base).items():
        probe.setdefault(key, value)
    report = inspect_environment(probe, action, False)
    blocking = [entry for entry in report["capabilities"]
                if entry["status"] in ("unavailable", "unsupported")
                and entry["affected_action"] == action]
    return {"entries": report["capabilities"], "blocking": blocking}


# ------------------------------------------------------------------ CLI 入口
# archive.py 里的 `preset` 子命令注册由总控统一改（本模块自带 main，先把行为跑通）。

def _read_json_file(path, label, expect=dict):
    if not path:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if expect is not None and not isinstance(payload, expect):
        raise ValueError("%s 必须是 %s" % (label, "JSON 映射" if expect is dict
                                          else "JSON 列表"))
    return payload


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="presets.py",
        description="常用方案：仅在用户要求时保存、仅在用户调用时解析；不启动任务、"
                    "不建定时作业。",
    )
    parser.add_argument("command", nargs="?", help="save / list / show / use")
    parser.add_argument("--preset-dir", dest="preset_dir", help="方案目录（不得在技能包内）")
    parser.add_argument("--name", help="方案名")
    parser.add_argument("--job", help="save：本次选择与运行配置的 JSON（运行配置只丢弃不落盘）")
    parser.add_argument("--overrides", help="本轮用户重述的字段 JSON（逐字段替换，不做并集）")
    parser.add_argument("--accounts", help="use：本次可用账号清单 JSON（本地引用）")
    parser.add_argument("--candidates", help="use：本次聊天清单 JSON（用于唯一解析目标）")
    parser.add_argument("--now", help="use：本次调用时刻（带时区 ISO；本模块不读真实时钟）")
    parser.add_argument("--overwrite", action="store_true", help="覆盖同名方案（需用户要求）")
    return parser


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _status_exit(status):
    return {"resolved": EXIT_COMPLETE, "saved": EXIT_COMPLETE,
            "capability_gap": EXIT_CAPABILITY}.get(status, EXIT_REJECTED)


def _public(result):
    """CLI 只打可展示视图：job 里带着本轮运行配置，绝不打印。"""
    return {key: value for key, value in result.items() if key != "job"}


def main(argv=None):
    parser = _build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as signal:
        if signal.code in (0, None):
            return EXIT_COMPLETE
        print("方案命令参数不合法（%s）" % signal.code)
        return EXIT_REJECTED
    command = args.command
    if not command:
        parser.print_help()
        return EXIT_REJECTED
    if command not in ("save", "list", "show", "use"):
        print("未知子命令：%s；方案只支持 save / list / show / use，"
              "不会代为启动任何任务" % command)
        return EXIT_REJECTED
    if not args.preset_dir:
        print("缺少 --preset-dir：方案目录由使用者给出，本模块不默认写进技能包")
        return EXIT_REJECTED
    try:
        if command == "list":
            _emit(list_presets(Path(args.preset_dir)))
            return EXIT_COMPLETE
        if not args.name:
            print("缺少 --name：方案动作必须点名方案")
            return EXIT_REJECTED
        if command == "save":
            selection = _read_json_file(args.job, "--job")
            if selection is None:
                print("缺少 --job：保存方案需要本次的选择项")
                return EXIT_REJECTED
            result = save_preset(Path(args.preset_dir), args.name, selection,
                                 overwrite=bool(args.overwrite))
            _emit(result)
            return _status_exit(result["status"])
        if command == "show":
            record, unreadable, ambiguity = _find_preset(
                _checked_directory(args.preset_dir), _checked_name(args.name))
            if record is None:
                _emit({"status": "missing", "name": args.name,
                       "questions": [ambiguity["detail"] if ambiguity
                                     else "没有找到名为「%s」的方案" % args.name],
                       "unreadable": unreadable, "started_task": False,
                       "scheduled": False})
                return EXIT_REJECTED
            _emit({"status": "shown", "name": record["name"], "path": record["path"],
                   "selection": record["selection"], "dropped": record["dropped"],
                   "started_task": False, "scheduled": False,
                   "real_account_verified": "NOT_RUN"})
            return EXIT_COMPLETE
        # use
        if not args.now:
            print("缺少 --now：相对范围只能按本次调用时刻解析，"
                  "本模块不读真实时钟（不无人值守跑）")
            return EXIT_REJECTED
        moment = _zone_of(args.now)
        if moment is None:
            print("--now 必须是带时区的 ISO 日期（收到：%s）" % args.now)
            return EXIT_REJECTED
        overrides = _read_json_file(args.overrides, "--overrides") or {}
        accounts = _read_json_file(args.accounts, "--accounts", list)
        candidates = _read_json_file(args.candidates, "--candidates", list)
        if accounts is not None:
            overrides["accounts"] = accounts
        if candidates is not None:
            overrides["chat_candidates"] = candidates
        result = resolve_preset(Path(args.preset_dir), args.name, overrides, moment)
        _emit(_public(result))
        return _status_exit(result["status"])
    except (ValueError, OSError) as error:
        print("方案命令被拒绝：%s" % error)
        return EXIT_REJECTED


if __name__ == "__main__":   # pragma: no cover - CLI 入口
    raise SystemExit(main())
