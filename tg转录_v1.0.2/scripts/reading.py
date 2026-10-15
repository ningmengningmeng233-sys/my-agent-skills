"""Markdown 阅读版 + 可恢复清洗（Task 4）。

接口：``build_reading_pack(data: list[dict], target: Path, options: dict) -> dict``；
CLI ``render`` 走 ``run_render``（只读「本任务完成清单已验收」的文本源，再登记阅读版
完成状态）。依赖标准库 + 包内 ``job``/``state``/``media``；**不导入 subprocess、socket
等进程或网络层**：不联网、不起子进程、不读真实归档。

四条不可让步的规矩：

1. **原档只读**。本模块只在 ``target`` 下写派生文件；给出 ``options["source"]["path"]``
   时记录构建前后的 SHA-256 并核对一致。
2. **不覆盖、不断链、发布可回滚**。分篇目录由稳定聊天身份（peer 类型 + chat ID）决定，显示名
   只用于人读；链接做 percent-encoding，发布前逐条核对目标在本次成品文件集合内或磁盘上存在。
   全部成品先在内存出齐并校验，通过后交给 ``state.publish_text_files``（全仓**唯一**一份原子
   发布实现）落盘：任何一步失败都整批回滚，上一次可用视图复原、不残留 ``.part``、不留混合视图。
3. **清洗是显式选项**。``cleaning`` 只接受 ``none``/``ads``；模板压缩单独开关、默认关闭。
   广告判定要求「显式广告信号 + 营销语境」组合命中，反诈骗/风险提示语境一律豁免。
   过滤记录逐条含规则、原消息 ID 与原文，撤销清洗按 ``cleaning=none`` 重跑即可，旧过滤记录
   文件随之清掉（不留一份还在宣称旧计数的记录）。
4. **聊天正文是不可信数据**。``escape_body`` 转义 ``\\``、方括号、``&`` ``<`` ``>`` 与裸 URL
   形态：渲染出的可见文本与原文逐字一致，但不产生链接、标签或可执行面；正文里的 Markdown
   语法一律点不动，阅读版中的链接只能由本工具自己生成。发送者显示名与群名同样是参与者自填
   内容，展示前走同一份 ``escape_body``（群名另有一层只管落盘路径的文件系统消毒）。
"""

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePath, PurePosixPath
from urllib.parse import quote, unquote

import media
import state
from job import chat_identity, validate_job

PACK_KIND = "telegram-archive-reading-pack"
RECORD_KIND = "telegram-archive-reading-cleaning"
PACK_VERSION = 1
MANIFEST_NAME = "reading-pack.json"
INDEX_NAME = "目录.md"
PARTS_SUBDIR = "parts"
RECORDS_SUBDIR = "清洗记录"
MISSING_SENDER_NOTE = "（原始导出缺少发送者字段，不编造姓名）"
UNVERIFIED_NOTE = "附件未验收，不链接为已完成"
MISSING_TIME_LABEL = "时间缺失"

# 拆分阈值（取值理由见 references/reading-and-cleaning.md）：设计第 48 行批评旧实现
# 「只按年拆、单年巨量不拆」，所以先按年月分组，月内再按条数/字节双阈值继续拆。
DEFAULT_THRESHOLDS = {"split_trigger_messages": 5000, "max_messages_per_file": 5000,
                      "max_bytes_per_file": 1000000}
COMPRESSION_MIN_CHARS = 20
COMPRESSION_MIN_REPEATS = 3
CLEANING_CHOICES = ("none", "ads")
SOURCE_TIME_BASIS = ("原始时间戳按 UTC 标准化（job.py 把时间写成 UTC Z）；展示时区另记在 "
                     "display_timezone，两者可换算核对")
CONSISTENCY_FORMULA = "original = kept + ads_filtered + template_compressed"
CONSERVATION_TEXT = "原始数 = 保留数 + 广告过滤数 + 显式模板压缩数"
ONLINE_COVERAGE_NOTE = (
    "阅读版只覆盖「本次已验收导出的文字源」，不证明该聊天的全部在线历史已导出；"
    "范围完成不等于全量，也不声称包含已删除或无权访问的消息")
FALSE_POSITIVE_NOTE = ("不声称零误删：规则命中不等于确证广告，过滤记录保留原文，"
                       "按 cleaning=none 重跑即可撤销")

OPTION_KEYS = frozenset((
    "chat", "cleaning", "template_compression", "timezone", "contents", "outputs",
    "text_output", "split_trigger_messages", "max_messages_per_file", "max_bytes_per_file",
    "compression_min_chars", "compression_min_repeats", "verified_attachments",
    "unverified_attachment_ids", "attachment_root", "source", "now"))
ID_KEYS = ("id", "messageid")
SENDER_KEYS = ("sender", "sendername", "from", "fromname", "author")
TEXT_KEYS = ("text", "message")
DATE_KEYS = ("date", "timestamp")
FILE_KEYS = ("file", "filename")

# ---- 广告规则：显式信号（A）+ 营销语境（B）组合命中；命中 C 的整条豁免（负样本）------
AD_SIGNALS = (
    ("ads:代查隐私数据", re.compile(
        r"(?:可查|代查|承接|专业查|在线查|查)[^。，,；;]{0,6}"
        r"(?:身份证|户籍|开房|开房记录|轨迹|通话记录|社工库)")),
    ("ads:接单招徕", re.compile(r"(?:接单|全天接|24\s*小时接|代理招商|招商中|诚招代理)")),
    ("ads:引流联系方式", re.compile(
        r"(?:加我?微信|加微信|加\s*QQ|联系\s*QQ|扫码加|t\.me/|telegram\.me/|点击链接|戳我链接)")),
    ("ads:广告标签", re.compile(
        r"(?:^|[\s(（【])[#＃](?:广告|推广|赞助|廣告|贊助|AD|ad|机场|機場)(?:$|[\s)）】，,。])")),
    ("ads:成人招嫖", re.compile(r"(?:同城[约約][炮砲]|上門服務|上门服务|[约約][妹女]|援交|外围女)")),
    ("ads:充值返利", re.compile(r"(?:首充|返利|充值送|免费试用|优惠券|低价代充)")),
)
AD_CONTEXTS = (
    ("价格/价目", re.compile(r"(?:价格|價格|报价|价目|收費|收费|多少钱|USDT|usdt|元/次)")),
    ("下单/客服", re.compile(r"(?:下单|订单|客服|咨询|諮詢|成交|秒回)")),
    ("促销动作", re.compile(r"(?:点我|點擊|点击|下载|注册|试用|添加|私聊|进群|扫码)")),
)
# 反诈骗/风险提示语境：出现同样的敏感词也一律保留（设计第 50 行的负样本口径）
SUPPRESSION = re.compile(r"(?:警惕|谨防|防范|防骗|诈骗|欺詐|骗局|被骗|举报|曝光|违法|被抓"
                         r"|已被抓|别信|不要信|切勿|骗子)")

_WINDOWS_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})")
_LINK = re.compile(r"(?<!\\)\[([^\]\n]*)\]\(([^)\n]+)\)")
# 会触发自动链接的裸 URL 形态（CommonMark 的 <scheme:> 之外，GFM 一类渲染器还认裸 http:// 与
# www.）：正文里出现就得打断，见 escape_body。
_AUTOLIKE = re.compile(r"(?i)\b(?:https?|ftp|file|ws|wss|mailto)://|\bwww\.")
WEEKDAYS = "一二三四五六日"

# 对用户可见的限制申报（评审 I-5：限制只写在 gitignore 的台账里等于没有）
LIMITATIONS = {
    "one_chat_per_invocation": (
        "render 一次只吃一个聊天：只读一份已验收的文本源、只出该聊天的阅读版。配置里有多"
        "个 chats 时逐个调用（阅读版清单跨聊天合并、parts/<身份>/ 按身份隔离，不会互相覆盖）"),
    "chat_body_is_untrusted": (
        "聊天正文、发送者显示名与群名一律按不可信内容转义（\\、[、]、&、<、> 与裸 URL 形态），"
        "渲染出的可见文本与原文逐字一致，但不产生任何链接、标签或可执行面；阅读版里的"
        " Markdown 链接只由本工具自己生成"),
    "attachment_dir_is_task_scoped": (
        "render 的附件目录与本任务状态目录同源（<任务 destination>/media/<身份>）；"
        "--media-dir 指向别处按配置非法拒绝（退出码 2），不静默把已验收附件降级为未验收"),
}


def _claims():
    """全模块统一的申报：派生视图、原档未改、不声称零误删、正文未执行、真实数据未验证。"""
    return {"derived_view": True, "original_files_modified": False,
            "zero_false_positive": False, "false_positive_note": FALSE_POSITIVE_NOTE,
            "chat_body_executed": False, "real_data_verified": False,
            "online_coverage_verified": False, "online_coverage_note": ONLINE_COVERAGE_NOTE,
            "synthetic_only": True}


# ---------------------------------------------------------------------- 入参校验

def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _display_timezone(value):
    """只接受 UTC 或 ±HH:MM 固定偏移：命名时区在离线机器上不可核对，直接拒绝而不猜。"""
    token = "UTC" if value is None else value
    _require(isinstance(token, str), "展示时区必须是字符串（UTC 或 ±HH:MM）：%r" % (value,))
    token = token.strip()
    if token.upper() in ("UTC", "Z"):
        return timezone.utc, "+00:00"
    match = _OFFSET.fullmatch(token)
    _require(match, "展示时区只接受 UTC 或 ±HH:MM 固定偏移，不接受时区数据库名（不猜机器上"
                    "有没有 tzdata）：%s" % token)
    hours, minutes = int(match.group(2)), int(match.group(3))
    _require(hours <= 23 and minutes <= 59,
             "展示时区偏移不合法（小时 00-23、分钟 00-59）：%s" % token)
    delta = timedelta(hours=hours, minutes=minutes)
    if match.group(1) == "-":
        delta = -delta
    return (timezone.utc if delta == timedelta(0) else timezone(delta)), token


def _positive_int(options, key, fallback):
    value = options.get(key, fallback)
    _require(isinstance(value, int) and not isinstance(value, bool) and value > 0,
             "%s 必须是大于 0 的整数（阈值与压缩条件不猜默认值）：%r" % (key, value))
    return value


def _identity(chat):
    _require(isinstance(chat, dict),
             "阅读版命名需要稳定聊天身份（peer_type + 正整数 id），显示名不参与消歧：%s"
             % type(chat).__name__)
    try:
        return chat_identity(chat)
    except ValueError as error:
        raise ValueError("阅读版命名需要稳定聊天身份（peer_type + 正整数 id），显示名不参与"
                         "消歧：%s" % error)


def _norm(key):
    return re.sub(r"[_\s]", "", str(key)).lower()


def _fields(message):
    """按归一化键名建索引（sender_name 与 senderName 视为同一个字段）。"""
    lookup = {}
    for name in message:
        lookup.setdefault(_norm(name), name)
    return lookup


def _field(message, keys):
    lookup = _fields(message)
    for key in keys:
        if key in lookup:
            return message[lookup[key]]
    return None


def _clean_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return "" if value is None else str(value)


def _sender(message):
    value = _field(message, SENDER_KEYS)
    return value.strip() if isinstance(value, str) else ""


def _moment(value, zone):
    """epoch 秒或 ISO 字符串 -> 展示时区的 aware datetime；取不到就 None（不猜时间）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc).astimezone(zone)
        except (OverflowError, OSError, ValueError):
            return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # job.py 已把时间标准化成 UTC Z；没有时区标记的戳按 UTC 处理并照样写出，不猜别的偏移
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(zone)
    except (OverflowError, OSError, ValueError):
        return None


def _read_messages(data):
    _require(isinstance(data, list), "阅读版输入必须是消息映射列表（list[dict]）：%s"
             % type(data).__name__)
    seen, rows, duplicates = set(), [], 0
    for position, item in enumerate(data, start=1):
        _require(isinstance(item, dict), "第 %d 项不是消息映射，无法生成阅读版" % position)
        message_id = _field(item, ID_KEYS)
        _require(isinstance(message_id, int) and not isinstance(message_id, bool)
                 and message_id > 0,
                 "第 %d 项缺少正整数消息 ID（id 或 message_id）：消息 ID 不编造、不补号"
                 % position)
        if message_id in seen:
            duplicates += 1        # 边界重叠去重：同 peer + 消息 ID 只算一条（shared-facts 裁决 1）
            continue
        seen.add(message_id)
        rows.append((message_id, item))
    return rows, duplicates


def _text_selected(options):
    if "text_output" in options:
        _require(isinstance(options["text_output"], bool), "text_output 必须是布尔值")
        return bool(options["text_output"])
    contents = options.get("contents")
    outputs = options.get("outputs")
    if contents is not None and "text" not in contents:
        return False
    if outputs is not None and "markdown" not in outputs:
        return False
    return True


# ------------------------------------------------------------------ 正文与清洗

def _break_autolink(match):
    """把裸 URL 形态打断一个字符：渲染出的可见文本不变，自动链接器不再认它是链接。"""
    token = match.group(0)
    if token.endswith("://"):
        return token[:-3] + "\\://"          # http:// -> http\:（渲染后仍是 http://）
    return token[:-1] + "\\."                # www.    -> www\.（渲染后仍是 www.）


def escape_body(text):
    """聊天正文一律按**不可信用户内容**处理，转义后不产生任何链接、标签或可执行面。

    Task 4 评审 C-1：只转义方括号时，正文里的 ``<https://evil…>``（CommonMark 自动链接）、
    ``<a href=…>``（裸 HTML 链接）与 ``<script>…</script>``（raw HTML）会原样进入阅读版，
    而本模块与交付文档都申报「不可点」——那是假申报。现在：

    * ``\\`` 先转成 ``\\\\``，再转义 ``[`` ``]``：正文里的 Markdown 链接/图片语法不成其为链接；
    * ``&`` ``<`` ``>`` 走 HTML 实体（``&amp;`` ``&lt;`` ``&gt;``）：不成标签、不成自动链接，
      渲染出的**可见文本仍逐字等于原文**；
    * ``http(s)://`` ``ftp://`` ``file://`` ``mailto:`` ``ws(s)://`` 与 ``www.`` 这类**裸 URL
      形态**（会触发 GFM 式自动链接）在冒号/点前打断一个反斜杠，可见文本不变而链接不成立。

    阅读版里出现的每一个 Markdown 链接都只能由我们自己生成（分篇互链、返回目录、附件链）。
    """
    out = text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    out = _AUTOLIKE.sub(_break_autolink, out)
    return out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ad_hits(text):
    """返回 (信号, 语境)：两者都命中才算广告；反诈骗/风险提示语境直接豁免。"""
    if not text.strip() or SUPPRESSION.search(text):
        return [], []
    signals = [name for name, pattern in AD_SIGNALS if pattern.search(text)]
    if not signals:
        return [], []
    contexts = [name for name, pattern in AD_CONTEXTS if pattern.search(text)]
    return (signals, contexts) if contexts else ([], [])


def clean_messages(rows, cleaning, compression, min_chars=COMPRESSION_MIN_CHARS,
                   min_repeats=COMPRESSION_MIN_REPEATS):
    """显式清洗：先按组合规则过滤广告，再按需压缩重复模板；逐条留下可恢复的记录。"""
    kept, records = [], []
    if cleaning == "ads":
        for message_id, item in rows:
            text = _clean_text(_field(item, TEXT_KEYS))
            signals, contexts = ad_hits(text)
            if signals:
                records.append({"message_id": message_id, "rule": signals[0],
                                "matched_signals": signals, "matched_context": contexts,
                                "original_text": text})
                continue
            kept.append((message_id, item))
    else:
        kept = list(rows)
    if compression:
        tally = {}
        for _message_id, item in kept:
            norm = re.sub(r"\s+", "", _clean_text(_field(item, TEXT_KEYS)))
            if len(norm) > min_chars:
                tally[norm] = tally.get(norm, 0) + 1
        seen, survivors = set(), []
        for message_id, item in kept:
            text = _clean_text(_field(item, TEXT_KEYS))
            norm = re.sub(r"\s+", "", text)
            if len(norm) > min_chars and tally.get(norm, 0) >= min_repeats:
                if norm in seen:
                    records.append({"message_id": message_id,
                                    "rule": "template_compression",
                                    "matched_signals": ["同聊天内重复 %d 次的长文本"
                                                        % tally[norm]],
                                    "matched_context": [], "original_text": text})
                    continue
                seen.add(norm)
            survivors.append((message_id, item))
        kept = survivors
    return kept, records


def _attachment_name(message):
    name = _field(message, FILE_KEYS)
    return name.strip() if isinstance(name, str) and name.strip() else None


def _has_attachment(message):
    return bool(_attachment_name(message)) or media.classify(message) != media.UNRESOLVED


# ------------------------------------------------------------------ 命名与链接

def _safe_title(display_name, identity):
    """显示名可读优先：只替换文件系统非法字符，Windows 会静默剥掉的尾随点/空格先处理掉。"""
    cleaned = _WINDOWS_ILLEGAL.sub("_", str(display_name or ""))
    cleaned = _CONTROLS.sub("", cleaned).strip().rstrip(" .")
    cleaned = cleaned[:60].rstrip(" .")
    return cleaned or ("未命名-%s" % state.identity_slug(identity))


def _url(relative):
    """链接编码：空格 -> %20、井号 -> %23、括号 -> %28/%29；目标文件名本身不改写。"""
    return "/".join(quote(part, safe="") for part in PurePosixPath(relative).parts)


def _relative(from_dir, to):
    return PurePosixPath(os.path.relpath(str(to), str(from_dir)).replace("\\", "/"))


# ------------------------------------------------------------------ 分篇渲染

def _message_lines(message_id, item, ctx):
    moment = _moment(_field(item, DATE_KEYS), ctx["zone"])
    raw = _clean_text(_field(item, TEXT_KEYS)).strip()
    # 发送者显示名与正文一样是聊天参与者自填的不可信内容（Telegram 里谁都能把
    # 自己的名字改成 <a href=…>），走同一份 escape_body，绝不让它成链或成标签。
    who = escape_body(_sender(item)) or MISSING_SENDER_NOTE
    stamp = "%02d:%02d:%02d" % (moment.hour, moment.minute, moment.second) if moment \
        else MISSING_TIME_LABEL
    body = escape_body(raw) if raw else "（本条无文字内容）"
    lines = body.splitlines() or ["（本条无文字内容）"]
    out = ["- **%s** #%d %s：%s" % (stamp, message_id, who, _CONTROLS.sub("", lines[0]))]
    for extra in lines[1:]:
        out.append("  %s" % _CONTROLS.sub("", extra) if extra.strip() else "")
    if _has_attachment(item):
        shown = escape_body(_attachment_name(item) or "附件（原始导出未给文件名）")
        path = ctx["attachments"].get(message_id)
        if path:
            out.append("  📎 附件：[%s](%s)" % (shown,
                                               _url(_relative(ctx["part_dir"], path))))
        else:
            out.append("  📎 附件：%s（%s）" % (shown, UNVERIFIED_NOTE))
    return out


def _line_weight(lines):
    return len(("\n".join(lines) + "\n\n").encode("utf-8"))


def _chunk(months, max_messages, max_bytes):
    """按月分组的结果在月内按条数/字节双阈值滚动分篇；单条超字节上限也绝不丢弃。"""
    parts = []
    for period, rows in months:
        current, size = [], 0
        for row in rows:
            weight = _line_weight(row[2])
            if current and (len(current) >= max_messages or size + weight > max_bytes):
                parts.append((period, current))
                current, size = [], 0
            current.append(row)
            size += weight
        if current:
            parts.append((period, current))
    return parts


def _group_by_month(rows, ctx):
    buckets, order = {}, []
    for message_id, item in rows:
        moment = _moment(_field(item, DATE_KEYS), ctx["zone"])
        period = ("%04d-%02d" % (moment.year, moment.month)) if moment else MISSING_TIME_LABEL
        if period not in buckets:
            buckets[period] = []
            order.append(period)
        buckets[period].append((message_id, item))
    order.sort(key=lambda key: (key == MISSING_TIME_LABEL, key))
    return [(period, sorted(buckets[period], key=lambda pair: pair[0])) for period in order]


def _name_parts(chunks, ctx, title, slug):
    """给分篇命名并挂上落盘路径（拆分次数变化时名字会重排，所以每轮重算）。"""
    totals = {}
    for period, _rows in chunks:
        totals[period] = totals.get(period, 0) + 1
    planned, seen = [], {}
    directory = ctx["target"] / PARTS_SUBDIR / slug
    for period, rows in chunks:
        seen[period] = seen.get(period, 0) + 1
        label = period or "全篇"
        name = ("%s__%s.md" % (title, label) if totals[period] == 1
                else "%s__%s-p%d.md" % (title, label, seen[period]))
        planned.append({"period": label, "period_raw": period, "part": seen[period],
                        "parts": totals[period], "rows": rows, "messages": len(rows),
                        "dir": directory, "path": "%s/%s/%s" % (PARTS_SUBDIR, slug, name)})
    return planned


def _plan(kept, ctx, title, slug, thresholds):
    """本次聊天的分篇计划（纯内存，不含落盘）。

    拆分条件是「条数超 ``split_trigger_messages`` **OR** 正文体积超 ``max_bytes_per_file``
    **OR** 跨年月边界」——任一超限即拆（评审 I-1：旧实现只看条数，默认阈值下 200 条长文
    实测产出 12 MB 单文件，与文档承诺的「谁先到谁生效」相反）。

    页眉、按日分节与同聊天互链也占字节，所以最后一遍按**渲染后的整篇体积**收敛：任何一篇
    超上限且不止一条就二分，直到全部收进上限。只有一条却仍超限的那种（单条超长正文）
    保留成独占一篇、只申报 ``counts.oversized_parts``，绝不截断或丢消息。
    """
    if not kept:
        return []
    months = [(period, [(message_id, item, _message_lines(message_id, item, ctx))
                        for message_id, item in rows])
              for period, rows in _group_by_month(kept, ctx)]
    body_bytes = sum(_line_weight(row[2]) for _period, rows in months for row in rows)
    if (len(kept) > thresholds["split_trigger_messages"]
            or body_bytes > thresholds["max_bytes_per_file"] or len(months) > 1):
        chunks = _chunk(months, thresholds["max_messages_per_file"],
                        thresholds["max_bytes_per_file"])
    else:
        chunks = [(None, [row for _period, rows in months for row in rows])]
    limit = thresholds["max_bytes_per_file"]
    while True:
        planned = _name_parts(chunks, ctx, title, slug)
        ctx["planned"] = planned
        grown, split = [], False
        for item in planned:
            item["text"] = _part_text(item, ctx, title, ctx["identity"])
            item["bytes"] = len(item["text"].encode("utf-8"))
            if item["bytes"] > limit and len(item["rows"]) > 1:
                middle = len(item["rows"]) // 2
                grown += [(item["period_raw"], item["rows"][:middle]),
                          (item["period_raw"], item["rows"][middle:])]
                split = True
            else:
                grown.append((item["period_raw"], item["rows"]))
        if not split:
            return planned
        chunks = grown


def _part_text(item, ctx, title, identity):
    directory = item["dir"]
    counts = ctx["counts"]
    # 群名同样是参与者可改的不可信内容：落盘路径用它（已过 _safe_title 消毒），
    # 页眉与正文里的**展示**再走一遍 escape_body，不成链、不成标签。
    shown = escape_body(title)
    lines = ["# %s（%s）— %s 第 %d/%d 篇" % (shown, identity, item["period"], item["part"],
                                            item["parts"]), "",
             "- 聊天身份: %s（显示名只用于人读，路径与消歧按身份）" % identity,
             "- 展示时区: %s（%s）" % (ctx["display_timezone"], SOURCE_TIME_BASIS),
             "- 本篇: 第 %d/%d 篇，消息 %d 条" % (item["part"], item["parts"],
                                                 item["messages"]),
             "- %s: 原始数 %d = 保留数 %d + 广告过滤数 %d + 显式模板压缩数 %d"
             % (CONSERVATION_TEXT, counts["original"], counts["kept"],
                counts["ads_filtered"], counts["template_compressed"]),
             "- 清洗: %s | 模板压缩: %s" % (ctx["cleaning"],
                                        "开启" if ctx["template_compression"] else "关闭"),
             "- 本阅读版是派生产物，不修改、不删除原始 JSON 导出"]
    if ctx["records_path"]:
        lines.append("- 过滤记录: `%s`（含规则、原消息 ID 与原文，可撤销）"
                     % ctx["records_path"])
    if ctx["source_name"]:
        lines.append("- 文本源: `%s`" % ctx["source_name"])
    others = [other for other in ctx["planned"] if other["path"] != item["path"]]
    if others:
        lines.append("- 同聊天其它分篇: " + " · ".join(
            "[%s 第%d/%d篇](%s)" % (other["period"], other["part"], other["parts"],
                                    _url(_relative(directory,
                                                   other["dir"] / PurePosixPath(
                                                       other["path"]).name)))
            for other in others))
    lines.append("- [返回目录](%s)" % _url(_relative(directory, ctx["target"] / INDEX_NAME)))
    lines.append("")
    current_day = None
    for message_id, _item, body in item["rows"]:
        moment = _moment(_field(_item, DATE_KEYS), ctx["zone"])
        day = ("%04d-%02d-%02d 周%s" % (moment.year, moment.month, moment.day,
                                       WEEKDAYS[moment.weekday()])
               if moment else "%s（原始导出没有时间戳字段）" % MISSING_TIME_LABEL)
        if day != current_day:
            lines += ["## %s" % day, ""]
            current_day = day
        lines += body
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# ------------------------------------------------------------------ 附件（只链接已验收）

def _inside(path, base):
    try:
        Path(path).relative_to(base)
        return True
    except ValueError:
        return False


def _attachments(target, options, present_ids):
    """只链接「已验收且磁盘上真在、且确实挂在本条消息上」的附件。

    越出所选附件目录的路径直接拒绝构建（也正因为这道校验发生在写盘之前，旧视图不会被
    半成品覆盖）。缺文件的记入 missing_on_disk，不链接、不冒充已完成。
    """
    verified = options.get("verified_attachments") or {}
    _require(isinstance(verified, dict), "verified_attachments 必须是 消息ID -> 路径 的映射")
    unverified = list(options.get("unverified_attachment_ids") or [])
    _require(all(isinstance(item, int) and not isinstance(item, bool) for item in unverified),
             "unverified_attachment_ids 只能是整数消息 ID")
    root = options.get("attachment_root")
    base = (Path(root) if root else Path(target)).resolve()
    by_id, linked, missing = {}, [], []
    for key, value in sorted(verified.items(), key=lambda pair: str(pair[0])):
        try:
            message_id = int(key)
        except (TypeError, ValueError):
            raise ValueError("verified_attachments 的键必须是消息 ID（整数）：%r" % (key,))
        raw = Path(value)
        resolved = (raw if raw.is_absolute() else base / raw).resolve()
        _require(_inside(resolved, base),
                 "附件 %s 解析后越出了所选附件目录，拒绝链接：%s" % (resolved.name, base))
        if not resolved.is_file():
            missing.append(message_id)
            continue
        linked.append({"message_id": message_id, "path": str(resolved), "exists": True})
        if message_id in present_ids:
            by_id[message_id] = resolved
    return {"by_id": by_id, "linked": linked,
            "not_linked": sorted(set(unverified) | set(missing)),
            "missing_on_disk": sorted(missing),
            "orphan_verified": sorted(item["message_id"] for item in linked
                                      if item["message_id"] not in by_id),
            "root": str(base)}


# ------------------------------------------------------------------ 清单与发布

# 清单里每个分篇只记「不随正文变化」的稳定字段。原因：重建某个聊天时，总目录与清单要能
# 逐字节不变，别处（其它聊天的分篇）才不会被牵连；逐文件的 bytes/sha256 属于本次构建报告
# （report["files"]），由调用方核对，不写进共享清单。
MANIFEST_FILE_KEYS = ("path", "identity", "title", "period", "part", "parts", "messages")


def _manifest_file_entry(entry):
    return {key: entry[key] for key in MANIFEST_FILE_KEYS}


def _read_manifest(target):
    path = target / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    chats = payload.get("chats")
    return dict(chats) if isinstance(chats, dict) else {}


def _index_text(chats, ctx, options):
    keys = ("original", "kept", "ads_filtered", "template_compressed")
    totals = {key: sum(item["counts"].get(key, 0) for item in chats.values()) for key in keys}
    scope = "、".join(sorted("%s=%s/%s" % (name, item["cleaning"],
                                          "压缩开" if item["template_compression"] else "压缩关")
                             for name, item in chats.items())) or "无聊天"
    lines = ["# Telegram 归档 · 阅读版目录", "",
             "- 生成时间: %s" % (options.get("now") or _now()),
             "- 展示时区: %s（%s）" % (ctx["display_timezone"], SOURCE_TIME_BASIS),
             "- 聊天数: %d" % len(chats),
             "- %s: 原始数 %d = 保留数 %d + 广告过滤数 %d + 显式模板压缩数 %d"
             % (CONSERVATION_TEXT, totals["original"], totals["kept"],
                totals["ads_filtered"], totals["template_compressed"]),
             "- 清洗与模板压缩（逐聊天）: %s" % scope,
             "- 本阅读版是派生产物：不修改、不删除原始 JSON 导出；构建前后原档 SHA-256 一致。",
             "- %s" % FALSE_POSITIVE_NOTE, ""]
    for key in sorted(chats):
        item = chats[key]
        counts = item["counts"]
        lines += ["## %s（%s）" % (escape_body(item["title"]), key),
                  "- %s: 原始 %s = 保留 %s + 广告过滤 %s + 模板压缩 %s"
                  % (CONSERVATION_TEXT, counts["original"], counts["kept"],
                     counts["ads_filtered"], counts["template_compressed"]),
                  "- 清洗 %s｜模板压缩 %s｜缺发送者 %s 条｜缺时间戳 %s 条｜已链接附件 %s 个"
                  % (item["cleaning"], "开启" if item["template_compression"] else "关闭",
                     counts["missing_sender"], counts["missing_time"],
                     len(counts.get("attachments_linked_ids", [])))]
        if item.get("records"):
            lines.append("- 过滤记录: `%s`" % item["records"])
        for part in item["files"]:
            lines.append("- [%s 第 %d/%d 篇](%s)" % (part["period"], part["part"],
                                                    part["parts"], _url(part["path"])))
        if not item["files"]:
            lines.append("- （本次选择下没有可阅读的文字分篇）")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _verify_links(target, plan, extra):
    """发布前遍历本次成品的每个链接：解码后目标要么在本次计划文件集合里，要么磁盘上真存在。"""
    known = {os.path.normpath(str(target / name)) for name in plan}
    for relative, content in plan.items():
        if not relative.endswith(".md"):
            continue
        directory = target / PurePosixPath(relative).parent
        for _label, raw in _LINK.findall(content):
            _require("://" not in raw, "阅读版不该出现外部链接：%s（在 %s）" % (raw, relative))
            decoded = unquote(raw)
            resolved = os.path.normpath(str(directory / PurePosixPath(decoded).as_posix()
                                             .replace("/", os.sep)))
            _require(resolved in known or resolved in extra or Path(resolved).is_file(),
                     "链接目标不存在或越出本次成品：%s -> %s" % (relative, raw))


def _drop_superseded(target, identity, previous, keep):
    """只清理本聊天自己分篇目录里被取代的文件；别处一律不碰。"""
    own = os.path.normpath(str(target / PARTS_SUBDIR / state.identity_slug(identity)))
    removed = []
    for name in previous:
        stale = os.path.normpath(str(target / PurePosixPath(name)))
        if stale in keep or not os.path.isfile(stale) or not stale.startswith(own + os.sep):
            continue
        os.unlink(stale)
        removed.append(name)
    return removed


def _drop_stale_records(target, existing, identity, keep_path):
    """本轮没有过滤记录了，就把本聊天上一轮的记录文件删掉（评审 I-6③）。

    留着它，用户打开读到的是一份还在宣称旧「原始数/广告过滤数」的过滤声明，与当前视图
    不符。只碰 <本身份>.json 这一份，别的聊天的记录一律不动。
    """
    previous = (existing.get(identity) or {}).get("records")
    if not previous or previous == keep_path:
        return []
    own = "%s/%s.json" % (RECORDS_SUBDIR, state.identity_slug(identity))
    stale = os.path.normpath(str(target / PurePosixPath(previous)))
    if stale != os.path.normpath(str(target / PurePosixPath(own))) or not os.path.isfile(stale):
        return []
    os.unlink(stale)
    return [previous]


# --------------------------------------------------------------------- 主入口

def build_reading_pack(data, target, options):
    """生成/更新 Markdown 阅读版；返回可核对的构建报告。原档只读，产物全是派生视图。"""
    _require(isinstance(options, dict), "options 必须是映射(dict)：%s"
             % type(options).__name__)
    unknown = sorted(str(key) for key in options if key not in OPTION_KEYS)
    _require(not unknown, "未知选项：%s（可用：%s）"
             % ("、".join(unknown), "、".join(sorted(OPTION_KEYS))))
    _require(target is not None, "target 必须是阅读版输出目录，不能是 None")
    _require(isinstance(target, PurePath), "target 必须是路径，收到 %s" % type(target).__name__)
    _require(str(target).strip(), "target 必须是非空路径")
    target = Path(target)
    cleaning = options.get("cleaning", "none")
    _require(cleaning in CLEANING_CHOICES,
             "cleaning 必须显式选择 none 或 ads（有文字时不默认清洗方式）：%r" % (cleaning,))
    compression = options.get("template_compression", False)
    _require(isinstance(compression, bool),
             "template_compression 必须是布尔值（未选择时保持关闭）：%r" % (compression,))
    zone, display_timezone = _display_timezone(options.get("timezone"))
    thresholds = {key: _positive_int(options, key, DEFAULT_THRESHOLDS[key])
                  for key in DEFAULT_THRESHOLDS}
    min_chars = _positive_int(options, "compression_min_chars", COMPRESSION_MIN_CHARS)
    min_repeats = _positive_int(options, "compression_min_repeats", COMPRESSION_MIN_REPEATS)
    rows, duplicates = _read_messages(data)

    if not _text_selected(options):
        counts = _zero_counts(duplicates)
        return _report(target, None, None, cleaning, compression, thresholds,
                       display_timezone, options, status="skipped",
                       reason="纯媒体或未选 markdown 文字输出：按合同跳过阅读版，不生成全文"
                              "阅读文档", counts=counts, files=[], records=[], kept_ids=[])

    chat = copy.deepcopy(options.get("chat"))
    identity = _identity(chat)
    rows.sort(key=lambda pair: pair[0])
    kept, records = clean_messages(rows, cleaning, compression, min_chars, min_repeats)
    title = _safe_title(chat.get("display_name"), identity)
    slug = state.identity_slug(identity)
    part_dir = target / PARTS_SUBDIR / slug
    attachments = _attachments(target, options, {message_id for message_id, _item in kept})
    counts = {"original": len(rows), "kept": len(kept),
              "ads_filtered": sum(1 for item in records
                                  if item["rule"] != "template_compression"),
              "template_compressed": sum(1 for item in records
                                         if item["rule"] == "template_compression"),
              "duplicated_ids": duplicates,
              "missing_sender": sum(1 for _id, item in kept if not _sender(item)),
              "missing_time": sum(1 for _id, item in kept
                                  if _moment(_field(item, DATE_KEYS), zone) is None),
              "attachments_linked": len(attachments["by_id"]),
              "attachments_unverified": len(attachments["not_linked"]),
              "oversized_parts": 0}
    ctx = {"zone": zone, "display_timezone": display_timezone, "cleaning": cleaning,
           "template_compression": compression, "target": target, "part_dir": part_dir,
           "attachments": attachments["by_id"], "counts": counts, "planned": [],
           "identity": identity,
           "source_name": (options.get("source") or {}).get("name"), "records_path": None}
    records_path = "%s/%s.json" % (RECORDS_SUBDIR, slug) if records else None
    ctx["records_path"] = records_path

    planned = _plan(kept, ctx, title, slug, thresholds)
    ctx["planned"] = planned
    files = []
    for item in planned:
        # 正文与体积在 _plan 的收敛循环里已按「渲染后的整篇」算过，这里只出回执。
        blob = item["text"].encode("utf-8")
        files.append({"path": item["path"], "identity": identity, "title": title,
                      "period": item["period"], "part": item["part"],
                      "parts": item["parts"], "messages": item["messages"],
                      "bytes": item["bytes"], "sha256": hashlib.sha256(blob).hexdigest()})
    counts["oversized_parts"] = sum(1 for item in planned if item["bytes"]
                                    > thresholds["max_bytes_per_file"])

    payload_records = [_record_meta(item, rows, zone, identity) for item in records]
    existing = _read_manifest(target)
    previous = [item["path"] for item in (existing.get(identity) or {}).get("files", [])]
    merged = dict(existing)
    merged[identity] = {"identity": identity, "chat": chat, "title": title,
                        "files": [_manifest_file_entry(item) for item in files],
                        "counts": dict(counts, attachments_linked_ids=sorted(
                            attachments["by_id"])),
                        "cleaning": cleaning, "template_compression": compression,
                        "records": records_path,
                        "attachments": {"linked": attachments["linked"],
                                        "not_linked": attachments["not_linked"],
                                        "missing_on_disk": attachments["missing_on_disk"],
                                        "orphan_verified": attachments["orphan_verified"]},
                        "thresholds": thresholds, "display_timezone": display_timezone,
                        "source": copy.deepcopy(options.get("source"))}
    manifest = {"kind": PACK_KIND, "version": PACK_VERSION,
                "generated_at": options.get("now") or _now(), "index": INDEX_NAME,
                "display_timezone": display_timezone, "source_time_basis": SOURCE_TIME_BASIS,
                "thresholds": thresholds, "conservation": CONSERVATION_TEXT, "chats": merged,
                "claims": _claims()}
    plan = {}
    for item in planned:
        plan[item["path"]] = item["text"]
    plan[INDEX_NAME] = _index_text(merged, ctx, options)
    plan[MANIFEST_NAME] = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    if records_path:
        plan[records_path] = json.dumps(
            {"kind": RECORD_KIND, "version": PACK_VERSION, "identity": identity,
             "chat": chat, "cleaning": cleaning, "template_compression": compression,
             "display_timezone": display_timezone,
             "counts": {"original": counts["original"], "kept": counts["kept"],
                        "ads_filtered": counts["ads_filtered"],
                        "template_compressed": counts["template_compressed"]},
             "conservation_formula": CONSISTENCY_FORMULA,
             "conservation_text": CONSERVATION_TEXT,
             "false_positive_note": FALSE_POSITIVE_NOTE,
             "undo": "按 cleaning=none（并关掉 template_compression）重跑 build_reading_pack；"
                     "records 里的 original_text 就是底档原文，可逐条覆盖回去",
             "records": payload_records}, ensure_ascii=False, indent=2, sort_keys=True)
    extra = {os.path.normpath(str(Path(item["path"]))): True for item in
             attachments["linked"]}
    _verify_links(target, plan, extra)
    # 全仓唯一一份原子发布实现（state.publish_text_files）：写失败或 replace 失败都整批
    # 回滚，旧视图复原、不残留 .part、不留「新分篇 + 旧目录」的混合视图。
    state.publish_text_files(target, plan)
    removed = _drop_superseded(target, identity, previous,
                               {os.path.normpath(str(target / name)) for name in plan})
    removed += _drop_stale_records(target, existing, identity, records_path)
    return _report(target, identity, chat, cleaning, compression, thresholds,
                   display_timezone, options, status="complete", reason=None, counts=counts,
                   files=files, records=payload_records,
                   kept_ids=[message_id for message_id, _item in kept],
                   duplicates=duplicates,
                   missing_sender=sorted(message_id for message_id, item in kept
                                         if not _sender(item)),
                   attachments=attachments, records_path=records_path, removed=removed,
                   linked=[item["path"] for item in files])


def _zero_counts(duplicates):
    return {"original": 0, "kept": 0, "ads_filtered": 0, "template_compressed": 0,
            "duplicated_ids": duplicates, "missing_sender": 0, "missing_time": 0,
            "oversized_parts": 0, "attachments_linked": 0, "attachments_unverified": 0}


def _record_meta(record, rows, zone, identity):
    by_id = dict(rows)
    item = by_id.get(record["message_id"], {})
    moment = _moment(_field(item, DATE_KEYS), zone)
    out = dict(record)
    out["chat_identity"] = identity
    out["date_utc"] = (moment.astimezone(timezone.utc).isoformat(timespec="seconds")
                       if moment else None)
    out["display_time"] = moment.isoformat(timespec="seconds") if moment else None
    out["undo"] = ("按 cleaning=none 重跑 build_reading_pack，或用 original_text 逐条覆盖回"
                   "该条正文（原档从未被改写）")
    return out


def _report(target, identity, chat, cleaning, compression, thresholds, display_timezone,
            options, status, reason, counts, files, records, kept_ids, duplicates=None,
            missing_sender=None, attachments=None, records_path=None, removed=None,
            linked=None):
    source = copy.deepcopy(options.get("source")) or {"path": None}
    path = source.get("path")
    if path:
        source.setdefault("name", Path(path).name)
        source["sha256_before"] = source.get("sha256") or _digest(path)
        source["sha256_after"] = _digest(path)
        source["unchanged"] = source["sha256_before"] == source["sha256_after"]
    else:
        source.update({"name": None, "sha256_before": None, "sha256_after": None,
                       "unchanged": True})
    counts = dict(counts)
    return {"kind": PACK_KIND, "version": PACK_VERSION, "status": status, "reason": reason,
            "text_selected": status != "skipped", "scope": "markdown_text_output",
            "target": str(target), "identity": identity, "chat": copy.deepcopy(chat),
            "display_timezone": display_timezone, "source_time_basis": SOURCE_TIME_BASIS,
            "cleaning": cleaning, "template_compression": compression,
            "thresholds": dict(thresholds), "counts": counts,
            "conservation": dict(counts, formula=CONSISTENCY_FORMULA,
                                 text=CONSERVATION_TEXT,
                                 holds=counts["original"] == counts["kept"]
                                 + counts["ads_filtered"] + counts["template_compressed"]),
            "index": INDEX_NAME, "files": files, "records_path": records_path,
            "filtered": records, "kept_ids": kept_ids,
            "missing_sender_ids": sorted(missing_sender or []),
            "attachments": ({"linked": attachments["linked"],
                             "not_linked": attachments["not_linked"],
                             "missing_on_disk": attachments["missing_on_disk"],
                             "orphan_verified": attachments["orphan_verified"]}
                            if attachments else
                            {"linked": [], "not_linked": [], "missing_on_disk": [],
                             "orphan_verified": []}),
            "source": source,
            "claims": _claims(),
            "limitations": dict(LIMITATIONS),
            "generated_at": options.get("now") or _now(), "removed": removed or [],
            "linked_index_entries": linked or []}


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------- CLI 组合层

def run_render(job, *, state_root=None, target=None, media_dir=None):
    """``render``：只读「本任务完成清单验收过」的文本源出阅读版，并登记完成状态。

    未验收、身份不符、范围不符、没有任务快照、文本源被删/不可读——一律如实报 unavailable
    （CLI 归退出码 3），绝不因为「文件存在且能解析」就出成品；发布中途失败报 failed
    （退出码 1）并把原因写进阅读里程碑，旧可用视图由 ``state.publish_text_files`` 回滚保留。
    附件目录与本任务状态目录**同源**，显式传入不同源的 ``--media-dir`` 按配置非法拒绝
    （退出码 2）。附件链接复用 ``media.verify_attachments``：只有 verified 的消息 ID 才被
    链接成附件，其余在正文里标注未验收。一次只吃一个聊天（见 ``LIMITATIONS``）。
    """
    data = validate_job(job)
    _require(data.get("destination"), "render 需要 destination（阅读版保存位置由用户选定）")
    chats = data.get("chats") or []
    _require(len(chats) <= 1,
             "render 一次只吃一个聊天：配置里给了 %d 个 chats，请逐个调用（阅读版清单跨聊天"
             "合并、parts/<身份>/ 按身份隔离，不会互相覆盖）" % len(chats))
    out = Path(target) if target else Path(data["destination"]) / "阅读版"
    contents = list(data.get("contents") or [])
    outputs = list(data.get("outputs") or [])
    if not ("text" in contents and "markdown" in outputs):
        return build_reading_pack([], out, {"cleaning": "none", "contents": contents,
                                            "outputs": outputs,
                                            "timezone": data.get("timezone") or "UTC"})
    sources = [str(item) for item in (data.get("sources") or [])]
    _require(len(sources) == 1,
             "render 一次只读一份已验收的导出（sources 必须是用户明确选定的单个路径）：%d 项"
             % len(sources))
    source = Path(sources[0])
    _require(source.is_file(), "文本源不存在或不是文件：%s（不自动找文件、不递归磁盘）"
             % source.name)
    root = _state_root_for(source, data, state_root)
    if root is None:
        return _refused(data, _default_state_root(data) or source.parent, source, out,
                        "文本源没有在任何任务状态目录的完成清单里登记过（完成清单是唯一凭据："
                        "文件存在、能解析、看起来像导出都不算）：%s" % source.name)
    try:
        task = state.load_task(root)
    except ValueError as error:
        return _refused(data, root, source, out, str(error))
    if data.get("chats") and chat_identity(data["chats"][0]) != task["identity"]:
        return _refused(data, root, source, out,
                        "本次聊天身份 %s 与任务快照 %s 不符（不代为改写身份）"
                        % (chat_identity(data["chats"][0]), task["identity"]))
    try:
        # 与 download 完全同一道凭据闸（摘要+身份+区间+条数）。跨任务缺陷裁决已把
        # 成品 size/摘要统一为实际落盘字节，这里不再存在、也不允许再出现
        # 「LF/CRLF 口径调和」的绕行分支。
        accepted = media.accepted_export(source, root, task)
    except (ValueError, OSError) as error:
        # 预检后被删/被占的文本源、读不出的完成清单都是**凭据不足**（3），不是配置非法（2）。
        return _refused(data, root, source, out, str(error))
    attachments = _rendered_attachments(root, task, media_dir)
    stale_temp = _prune_reading_temp(out)
    try:
        report = build_reading_pack(
            accepted["messages"], out,
            {"chat": _chat_of(data, task), "cleaning": data.get("cleaning", "none"),
             "template_compression": bool(data.get("template_compression", False)),
             "timezone": data.get("timezone") or "UTC", "contents": contents,
             "outputs": outputs,
             "verified_attachments": attachments["verified"],
             "unverified_attachment_ids": attachments["unverified"],
             "attachment_root": attachments["root"],
             "source": {"path": str(source), "name": accepted["name"],
                        "sha256": accepted["sha256"], "size": accepted["size"],
                        "count": accepted["count"]}})
    except OSError as error:
        # 发布失败：旧视图已由 state.publish_text_files 整批回滚，这里只申报原因并登记
        # 失败里程碑（CLI 归退出码 1），绝不静默吞掉，也不谎报完成。
        return _publish_failed(data, root, source, out, error, attachments)
    report["state_root"] = str(root)
    report["source_accepted"] = True
    report["attachment_index"] = attachments["index"]
    report["attachment_root"] = str(attachments["root"])
    if attachments["note"]:
        report["attachment_note"] = attachments["note"]
    report["temp_files"] = stale_temp
    report["milestone"] = state.record_output(
        root, "reading", "complete",
        evidence={"index": report["index"], "target": report["target"],
                  "files": len(report["files"]), "counts": report["counts"],
                  "records": report["records_path"], "source_sha256": accepted["sha256"],
                  "attachment_links": len(report["attachments"]["linked"]),
                  "attachment_index": attachments["index"],
                  "attachment_note": attachments["note"],
                  "limitations": dict(LIMITATIONS),
                  "return_code_alone_is_not_proof": True, "real_data_verified": False})
    return report


def _publish_failed(data, root, source, out, error, attachments):
    """发布失败的可核对回执：状态里申报原因 + 回滚是否彻底，全部照常写出。"""
    problems = list(getattr(error, "publish_rollback_problems", []) or [])
    rollback = ("回滚全部成功，旧视图逐字节可用" if not problems
                else "回滚仍有遗留：%s" % "、".join(problems))
    reason = ("阅读版发布失败（%s）：%s；%s" % (type(error).__name__, error, rollback))
    report = _refused(data, root, source, out, reason, status="failed")
    report["source_accepted"] = True
    report["attachment_index"] = attachments["index"]
    report["rollback_problems"] = problems
    report["milestone"] = state.record_output(
        root, "reading", "failed", reason=reason,
        evidence={"target": str(out), "index": INDEX_NAME, "files": 0,
                  "attachment_index": attachments["index"],
                  "rollback_problems": problems, "partial_publish_attempted": True,
                  "return_code_alone_is_not_proof": True, "real_data_verified": False})
    return report


def _chat_of(data, task):
    chats = data.get("chats") or []
    if chats:
        return dict(chats[0])
    return {"peer_type": task["peer_type"], "id": task["chat_id"], "display_name": None,
            "locator": None}


def _state_root(data):
    chats = data.get("chats") or []
    _require(len(chats) == 1, "render 需要 --root 状态目录，或配置里唯一的 chat 以推定状态目录")
    return state.task_state_root(data["destination"], chat_identity(chats[0]))


def _default_state_root(data):
    """按配置推定状态目录；推不出来（多聊天/缺字段）就返回 None，不猜。"""
    try:
        return _state_root(data)
    except (ValueError, KeyError):
        return None


def _task_roots_beside(source):
    """从用户选定的文本源反查任务状态目录。

    只看它自己所在或相邻的 ``telegram-state`` 这一层，不递归磁盘、不自动找文件：这里定位的
    只是「该读哪份完成清单」，凭据仍然是清单本身（由 ``media.accepted_export`` 复核摘要）。
    """
    resolved = Path(source).resolve()
    bases = []
    for directory in [resolved.parent, *resolved.parents]:
        if directory.name == state.STATE_DIR_NAME:
            bases.append(directory)
            break
        candidate = directory / state.STATE_DIR_NAME
        if candidate.is_dir():
            bases.append(candidate)
            break
    roots = []
    for base in bases:
        for child in sorted(base.iterdir()):
            if (child / state.TASK_NAME).is_file():
                roots.append(child)
    return roots


def _same_path(left, right):
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


def _accepts_source(root, source):
    """该任务状态目录登记的最终成品是否就是这份文本源。"""
    try:
        return _same_path(state.final_target(root), source)
    except ValueError:
        return False


def _state_root_for(source, data, explicit):
    if explicit:
        return Path(explicit)
    for root in _task_roots_beside(source):
        if _accepts_source(root, source):
            return root
    default = _default_state_root(data)
    if default is not None and (default / state.TASK_NAME).is_file():
        return default
    return None


def _task_destination(root):
    """从任务状态目录反推它自己的 destination：``<destination>/telegram-state/<身份>``。

    评审 I-3 的根因是附件目录取自 render 作业自己的 ``destination``、状态根却取自文本源
    路径，两条线不一致时**已验收**的附件被静默标成未验收。这里让两者同源。
    """
    root = Path(root)
    if root.parent.name != state.STATE_DIR_NAME:
        return None
    return root.parent.parent


def _rendered_attachments(root, task, media_dir):
    """附件目录与状态根同源；显式传入不同源的目录按配置非法拒绝（退出码 2）。

    找不到本任务自己的媒体索引时如实写进 ``note`` 申报——「没找到索引」不等于「附件未验收」，
    更不能悄悄把已验收的东西说成未验收还照样报 complete。
    """
    slug = state.identity_slug(task["identity"])
    destination = _task_destination(root)
    derived = (destination / state.MEDIA_SUBDIR / slug) if destination else None
    if media_dir:
        given = Path(media_dir)
        if derived is not None and not _same_path(given, derived):
            raise ValueError(
                "--media-dir 与本任务状态目录里的附件位置不同源：%s != %s（附件目录与状态根"
                "必须同源，不静默把已验收附件降级成未验收；请去掉 --media-dir，或改用本任务"
                "自己的媒体目录）" % (given, derived))
        directory = given
    elif derived is not None:
        directory = derived
    else:
        raise ValueError(
            "--root 指向的不是 <destination>/%s/<身份> 形状的任务状态目录，无法同源推定附件"
            "目录，请显式给出与本任务同源的 --media-dir：%s" % (state.STATE_DIR_NAME, root))
    index_path = Path(root) / state.MEDIA_SUBDIR / ("index-%s.json" % slug)
    empty = {"verified": {}, "unverified": [], "root": directory, "index": None, "note": None}
    if not index_path.is_file():
        empty["note"] = ("本任务没有媒体候选索引（%s 不存在）：本次没有任何「已验收附件」可"
                         "链接，这是「没有下载记录」而不是「附件未验收」" % index_path)
        return empty
    if not directory.is_dir():
        empty["note"] = ("候选索引在，但附件目录 %s 不存在：附件按未落盘处理，不链接为已完成"
                         % directory)
        return empty
    index = json.loads(index_path.read_text(encoding="utf-8"))
    acceptance = media.verify_attachments(index, directory)
    known = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        message_id = media.message_id_from_name(path.name)
        if message_id:
            known.setdefault(message_id, str(path))
    return {"verified": {item: known[item] for item in acceptance["verified"]
                        if item in known},
            "unverified": sorted(acceptance["outstanding"]),
            "root": directory, "index": str(index_path),
            "note": None if acceptance["verified"] else
            "候选索引里没有任何通过验收的附件（缺文件/大小或摘要不符），一律不链接为已完成"}


def _prune_reading_temp(target):
    """回收阅读版目录里硬崩溃遗留的 ``*.part-<pid>``（复用 state 那一套治理，不新造）。"""
    target = Path(target)
    if not target.is_dir():
        return None
    parts = target / PARTS_SUBDIR
    extra = [item for item in sorted(parts.iterdir()) if item.is_dir()] if parts.is_dir() else []
    return state.prune_stale_temp_files(target, extra_dirs=extra)


def _refused(data, root, source, out, reason, status="unavailable"):
    counts = _zero_counts(0)
    report = {"kind": PACK_KIND, "version": PACK_VERSION, "status": "unavailable",
            "reason": reason, "text_selected": True, "scope": "markdown_text_output",
            "target": str(out), "identity": None, "chat": None, "display_timezone": None,
            "source_time_basis": SOURCE_TIME_BASIS, "cleaning": data.get("cleaning"),
            "template_compression": bool(data.get("template_compression", False)),
            "thresholds": dict(DEFAULT_THRESHOLDS), "counts": counts,
            "conservation": dict(counts, formula=CONSISTENCY_FORMULA,
                                 text=CONSERVATION_TEXT, holds=True),
            "index": INDEX_NAME, "files": [], "records_path": None, "filtered": [],
            "kept_ids": [], "missing_sender_ids": [],
            "attachments": {"linked": [], "not_linked": [], "missing_on_disk": [],
                            "orphan_verified": []},
            "source": {"path": str(source), "name": source.name, "sha256_before": None,
                       "sha256_after": None, "unchanged": True},
            "claims": _claims(),
            "limitations": dict(LIMITATIONS),
            "generated_at": _now(), "removed": [], "linked_index_entries": [],
            "state_root": str(root), "source_accepted": False}
    report["status"] = status
    return report
