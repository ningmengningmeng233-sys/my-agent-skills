"""用户选择合同（Task 1）。

validate_job 只验证字段与组合合法性，绝不注入任何隐含业务选择：缺失的
chats/range/contents/outputs/destination 一律报错，不默认纯文字、不默认全量
历史、不默认全账号。会话目录、命名空间、代理凭据与账号号码不进入摘要或指纹。

依赖：Python 标准库。本模块不读写归档、不执行 tdl、不联网。
"""

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import PurePath
from urllib.parse import urlsplit

_UTC = timezone.utc

CLI_ACTIONS = ("check", "validate", "export", "status", "resume", "render",
               "download", "import", "search", "share", "update", "preset")
ONLINE_ACTIONS = ("export", "resume", "download", "update")
OFFLINE_ACTIONS = tuple(a for a in CLI_ACTIONS if a not in ONLINE_ACTIONS)
# 读取本地归档数据、必须给出用户明确选定路径的离线动作
OFFLINE_SOURCE_ACTIONS = ("render", "import", "search", "share")

CONTENT_TYPES = ("text", "image", "video", "audio", "voice", "animation",
                 "sticker", "document", "other")
ATTACHMENT_TYPES = CONTENT_TYPES[1:]
TEXT_OUTPUTS = ("markdown", "json")
OUTPUT_TYPES = TEXT_OUTPUTS + ("media",)
PEER_TYPES = ("user", "chat", "channel")
RANGE_MODES = ("all", "time", "id", "last")
RANGE_FIELDS = {"all": frozenset(), "time": frozenset({"start", "end"}),
                "id": frozenset({"lower", "upper"}),
                "last": frozenset({"n", "count_unit"})}
COUNT_UNITS = ("messages", "matching_messages")
CLEANING_CHOICES = ("none", "ads")
PROXY_SCHEMES = ("socks5", "socks5h", "socks4", "socks4a", "http", "https")
REQUIRED_ONLINE_SELECTION = ("chats", "range", "contents", "outputs", "destination")
# 合同只把 tdl/存储/命名空间列为必要运行配置；proxy 可选——键缺省即「不使用代理」，
# 绝不因缺 proxy 拒绝在线任务，也绝不注入任何默认代理值（本机预设不外溢）。
REQUIRED_ONLINE_RUNTIME = ("tdl", "storage", "namespace")
RUNTIME_INTEGER_MINIMUM = {"retries": 0, "concurrency": 1, "segment_width": 1}
_PATH_KEYS = ("destination", "tdl", "storage")


def _is_text(value):
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_absolute_path(value):
    if not _is_text(value):
        return False
    path = PurePath(value)
    return path.is_absolute() and len(path.parts) > 1


def chat_identity(chat):
    """归档隔离身份：peer 类型 + chat ID，不依赖显示名。"""
    if not isinstance(chat, dict):
        raise ValueError("chats 条目必须是包含 peer_type 与 id 的映射")
    peer_type = chat.get("peer_type")
    chat_id = chat.get("id")
    if not _is_text(peer_type) or not _is_positive_int(chat_id):
        raise ValueError("chats 条目缺少可隔离身份：需要 peer_type 与正整数 id")
    return f"{peer_type}:{chat_id}"


def _check_chats(data, problems):
    if "chats" not in data:
        return
    chats = data["chats"]
    if not isinstance(chats, list) or not chats:
        problems.append("chats 必须是非空列表（用户选中的当前清单项，不默认全账号）")
        return
    seen = set()
    for chat in chats:
        if not isinstance(chat, dict):
            problems.append("chats 每项必须是包含 peer_type/id/display_name/locator 的映射")
            continue
        if chat.get("peer_type") not in PEER_TYPES:
            problems.append("chats.peer_type 取值不支持（仅 user/chat/channel，不做别名猜测）")
        if not _is_positive_int(chat.get("id")):
            problems.append("chats.id 必须是正整数消息发起方内部 ID")
        if not _is_text(chat.get("display_name")):
            problems.append("chats.display_name 必须是非空字符串（仅用于人读，不参与身份隔离）")
        if not _is_text(chat.get("locator")):
            problems.append("chats.locator 必须是非空字符串（当前 tdl 可用定位值，不臆造格式）")
        if _is_text(chat.get("peer_type")) and _is_positive_int(chat.get("id")):
            identity = chat_identity(chat)
            if identity in seen:
                problems.append("chats 含重复身份（同名不同对象要各自保留，同身份不得重复）")
            seen.add(identity)


def _utc_moment(value):
    """带时区 ISO 日期 -> UTC aware datetime（微秒原样保留）。

    返回 None 表示不合法（解析失败、缺时区、超出可转换范围），调用方报错；
    绝不自行补全时区，也绝不让 OverflowError 一类的转换异常逃出校验。
    """
    if not _is_text(value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(_UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _format_utc_zulu(moment):
    """aware datetime -> UTC Z 形式；非零微秒保留，整秒不虚增小数段。

    秒级格式（strftime/%M:%S 截断）会把亚秒抹平，使「终点不包含」边界理论上前移
    不足 1 秒，故这里始终按微秒精度输出。
    """
    naive = moment.astimezone(_UTC).replace(tzinfo=None)
    if naive.microsecond:
        return naive.isoformat(timespec="microseconds") + "Z"
    return naive.isoformat(timespec="seconds") + "Z"


def _check_range(data, problems):
    if "range" not in data:
        return
    scope = data["range"]
    if not isinstance(scope, dict) or not scope:
        problems.append("range 必须是非空映射（范围必须由用户选择，不默认全量）")
        return
    mode = scope.get("mode")
    if mode not in RANGE_MODES:
        problems.append("range.mode 取值不支持（仅 all/time/id/last）")
        return
    extra = set(scope) - RANGE_FIELDS[mode] - {"mode"}
    if extra:
        problems.append("range 含与所选 mode 无关的字段：" + "、".join(sorted(extra)))
        return
    missing = RANGE_FIELDS[mode] - set(scope)
    if missing:
        if missing == {"count_unit"}:
            problems.append("range.count_unit 缺失（messages 与 matching_messages 计数对象不同，不猜默认值）")
        else:
            problems.append("range 缺少字段：" + "、".join(sorted(missing)))
        return
    if mode == "all":
        data["range"] = {"mode": "all"}
        return
    if mode == "id":
        lower, upper = scope.get("lower"), scope.get("upper")
        if not _is_positive_int(lower) or not _is_positive_int(upper):
            problems.append("range.lower/upper 必须是包含两端的正整数消息 ID")
            return
        if lower > upper:
            problems.append("range.lower 不得大于 range.upper")
            return
        data["range"] = {"mode": "id", "lower": lower, "upper": upper}
        return
    if mode == "time":
        start_at, end_at = _utc_moment(scope.get("start")), _utc_moment(scope.get("end"))
        if start_at is None or end_at is None:
            problems.append("range.start/end 必须是带时区的 ISO 日期（时区缺失或格式非法不自行补全）")
            return
        # 按真实时刻（微秒精度）比较，避免混用整秒/亚秒写法时误判区间方向
        if start_at >= end_at:
            problems.append("range 时间区间必须起点早于终点（起点包含、终点不包含）")
            return
        data["range"] = {"mode": "time", "start": _format_utc_zulu(start_at),
                         "end": _format_utc_zulu(end_at)}
        return
    n, unit = scope.get("n"), scope.get("count_unit")
    if not _is_positive_int(n):
        problems.append("range.n 必须是正整数条数")
        return
    if unit not in COUNT_UNITS:
        problems.append("range.count_unit 取值不支持（仅 messages 或 matching_messages）")
        return
    data["range"] = {"mode": "last", "n": n, "count_unit": unit}


def _check_selection_lists(data, problems):
    contents, outputs = None, None
    if "contents" in data:
        value = data["contents"]
        if not isinstance(value, list) or not value:
            problems.append("contents 必须是非空列表（内容类别由用户选择，不默认文字或全部媒体）")
        elif not all(_is_text(item) for item in value):
            problems.append("contents 每项必须是字符串类别名")
        elif len(set(value)) != len(value):
            problems.append("contents 含重复类别")
        else:
            unknown = [item for item in value if item not in CONTENT_TYPES]
            if unknown:
                problems.append("contents 含不支持的类别：" + "、".join(unknown)
                               + "（unknown/all 不是可默选类别）")
            else:
                contents = value
    if "outputs" in data:
        value = data["outputs"]
        if not isinstance(value, list) or not value:
            problems.append("outputs 必须是非空列表（输出形式由用户选择，不自动追加全文格式）")
        elif not all(_is_text(item) for item in value):
            problems.append("outputs 每项必须是字符串输出名")
        elif len(set(value)) != len(value):
            problems.append("outputs 含重复输出名")
        else:
            unknown = [item for item in value if item not in OUTPUT_TYPES]
            if unknown:
                problems.append("outputs 含不支持的输出：" + "、".join(unknown))
            else:
                outputs = value
    return contents, outputs


def _check_content_combinations(data, contents, outputs, problems):
    if contents is None or outputs is None:
        return
    has_text = "text" in contents
    attachments = [item for item in contents if item in ATTACHMENT_TYPES]
    has_media_output = "media" in outputs
    text_outputs = [item for item in outputs if item in TEXT_OUTPUTS]
    if attachments and not has_media_output:
        problems.append("contents 选择附件类别时 outputs 必须包含 media")
    if has_media_output and not attachments:
        problems.append("outputs 含 media 但 contents 未选择任何附件类别（不自动下载媒体）")
    if has_text and not text_outputs:
        problems.append("contents 含 text 时 outputs 必须包含 markdown 或 json")
    if not has_text and text_outputs:
        problems.append("contents 未选择 text 时 outputs 不得包含 markdown/json 全文输出")
    if has_text and "cleaning" not in data:
        problems.append("cleaning 必须显式选择 none 或 ads（有文字选择时不默认清洗方式）")
    elif "cleaning" in data and not has_text:
        problems.append("cleaning 仅适用于所选文字输出：contents 未选 text 时必须跳过整理选择")


def _check_cleaning_and_compression(data, problems):
    if "cleaning" in data and data["cleaning"] not in CLEANING_CHOICES:
        problems.append("cleaning 取值不支持（仅 none 或 ads）")
    if "template_compression" in data and not isinstance(data["template_compression"], bool):
        problems.append("template_compression 必须是布尔值（未选择时保持关闭，不注入模板）")


def _check_paths_and_runtime(data, online, problems):
    for key in REQUIRED_ONLINE_RUNTIME:
        if online and key not in data:
            problems.append(f"{key} 缺失（在线动作的运行配置必须显式给出，不使用本机预设）")
    for key in _PATH_KEYS:
        if key in data and not _is_absolute_path(data[key]):
            problems.append(f"{key} 必须是绝对路径（不读写磁盘、不创建目录）")
    if "namespace" in data and not _is_text(data["namespace"]):
        problems.append("namespace 必须是非空字符串（tdl 会话命名空间，不默认）")
    if "proxy" in data and data["proxy"] is not None:
        proxy = data["proxy"]
        if not _is_text(proxy):
            problems.append("proxy 必须是 URL 或显式 None（不静默沿用环境变量）")
        else:
            parts = urlsplit(proxy)
            if parts.scheme not in PROXY_SCHEMES or not parts.netloc or not parts.hostname:
                problems.append("proxy 必须是受支持方案的 URL（socks5/socks4/http/https）")
    for key, minimum in RUNTIME_INTEGER_MINIMUM.items():
        if key in data:
            value = data[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                problems.append(f"{key} 必须是不小于 {minimum} 的整数（运行参数不生成新的内容选择）")


def _check_sources(data, action, problems):
    if action in OFFLINE_SOURCE_ACTIONS and "sources" not in data:
        problems.append("sources 缺失（离线动作只能使用用户明确选择的本地路径）")
    if "sources" not in data:
        return
    value = data["sources"]
    if not isinstance(value, list) or not value:
        problems.append("sources 必须是非空列表（不自动递归整个磁盘）")
        return
    for item in value:
        if not _is_absolute_path(item):
            problems.append("sources 每项必须是绝对路径字符串（不接受相对路径或空值）")
            continue
        if any("*" in part or "?" in part for part in PurePath(item).parts):
            problems.append("sources 不接受通配路径（只能是用户明确选定的文件）")
    if len(set(value)) != len(value):
        problems.append("sources 含重复路径")


def _check_optional_maps(data, problems):
    if "provenance" in data:
        value = data["provenance"]
        if not isinstance(value, dict) or not all(
                _is_text(key) and _is_text(item) for key, item in value.items()):
            problems.append("provenance 必须是「字段名 -> 用户原话」的非空字符串映射")
    if "source_format" in data and not _is_text(data["source_format"]):
        problems.append("source_format 必须是非空字符串")
    if "metadata" in data:
        value = data["metadata"]
        if not isinstance(value, dict) or not all(
                _is_text(key) and isinstance(item, (str, int, float, bool, type(None)))
                for key, item in value.items()):
            problems.append("metadata 必须是字符串键到标量值的映射")
    if "snapshot" in data:
        value = data["snapshot"]
        if not isinstance(value, dict) or set(value) - {"upper_bound", "frozen_at"} or \
                "upper_bound" not in value or not _is_positive_int(value.get("upper_bound")):
            problems.append("snapshot 必须是包含正整数 upper_bound 的映射（固定快照上界，不重新探测）")
        elif "frozen_at" in value and _utc_moment(value["frozen_at"]) is None:
            problems.append("snapshot.frozen_at 必须是带时区的 ISO 日期")


def validate_job(value):
    """校验用户选择合同；缺失或非法时 ValueError，且不注入任何隐含业务选择。"""
    if not isinstance(value, dict):
        raise ValueError("job 必须是映射(dict)：任务配置由用户选择与运行参数组成")
    data = copy.deepcopy(value)
    problems = []

    action = data.get("action")
    unknown_action = not isinstance(action, str) or action not in CLI_ACTIONS
    if unknown_action:
        problems.append("action 缺失或不是受支持的子命令（任务配置必须带 action）")
    # action 未知时按最严格的在线导出合同校验，缺失的业务选择必须被点名，
    # 同时绝不写回 data，避免注入默认动作。
    online = action in ONLINE_ACTIONS or unknown_action

    for field in REQUIRED_ONLINE_SELECTION:
        if online and field not in data:
            problems.append(f"{field} 缺失（必选项，不使用推荐值或沉默代替选择）")

    _check_chats(data, problems)
    contents, outputs = _check_selection_lists(data, problems)
    _check_range(data, problems)
    _check_content_combinations(data, contents, outputs, problems)
    _check_cleaning_and_compression(data, problems)
    _check_paths_and_runtime(data, online, problems)
    _check_sources(data, action, problems)
    _check_optional_maps(data, problems)

    if problems:
        raise ValueError("任务配置校验失败：" + "；".join(problems))
    return data


def _safe_proxy(value):
    """去掉代理 URL 中的认证信息，凭据不进入摘要或指纹输入。"""
    if not isinstance(value, str) or not value:
        return value
    parts = urlsplit(value)
    if not parts.scheme or not parts.hostname:
        return "invalid"
    host = parts.hostname
    netloc = f"{host}:{parts.port}" if parts.port else host
    return f"{parts.scheme}://{netloc}"


def _digest_payload(data):
    chats = sorted((chat_identity(chat), str(chat.get("locator")))
                   for chat in data.get("chats", []))
    snapshot = data.get("snapshot") or {}
    return {
        "action": data.get("action"),
        "chats": chats,
        "range": data.get("range"),
        "contents": sorted(data.get("contents", [])),
        "outputs": sorted(data.get("outputs", [])),
        "cleaning": data.get("cleaning"),
        "template_compression": bool(data.get("template_compression", False)),
        "destination": data.get("destination"),
        "sources": sorted(data.get("sources", [])),
        "snapshot_upper_bound": snapshot.get("upper_bound"),
        "tdl": data.get("tdl"),
        "storage": data.get("storage"),
        "namespace": data.get("namespace"),
        "proxy": _safe_proxy(data.get("proxy")),
    }


def job_fingerprint(value):
    """配置摘要（固定快照上界 + 业务选择）的 sha256；不输出任何配置原值。"""
    data = validate_job(value)
    canonical = json.dumps(_digest_payload(data), sort_keys=True, ensure_ascii=False,
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def job_summary(value):
    """可阅读任务摘要：保留业务选择，剔除会话目录、命名空间与代理配置值。"""
    data = validate_job(value)
    contents = list(data.get("contents", []))
    chats = copy.deepcopy(data.get("chats", []))
    summary = {
        "action": data["action"],
        "chat_identities": [chat_identity(chat) for chat in chats],
        "chats": chats,
        "range": copy.deepcopy(data.get("range")),
        "contents": contents,
        "outputs": list(data.get("outputs", [])),
        "sources": list(data.get("sources", [])),
        "destination": data.get("destination"),
        "snapshot_upper_bound": (data.get("snapshot") or {}).get("upper_bound"),
        "template_compression": bool(data.get("template_compression", False)),
        "runtime": {
            "tdl_configured": bool(data.get("tdl")),
            "session_configured": bool(data.get("storage") and data.get("namespace")),
            "proxy_configured": data.get("proxy") is not None,
        },
        "fingerprint": job_fingerprint(data),
    }
    if "text" in contents:
        summary["cleaning"] = data["cleaning"]
    return summary
