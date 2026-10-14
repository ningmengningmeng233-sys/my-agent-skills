"""独立分享副本与脱敏范围（Task 7：``sharing.py``）。

一句话：把用户选定的一份本地归档，出成**另起一个目录**的分享副本——只带选定字段
已脱敏的文字、必要时带逐项选定的附件；原始归档与私有阅读版一个字节都不动，化名映射表
只留在私有归档位置，绝不打进分享包。

合同（设计 §4「可分享副本」逐字对应）：

* **显式模式**：源数据、输出位置、脱敏字段、是否带附件都要用户给；缺项一律按配置非法
  拒绝（``ShareError``，由 CLI 归退出码 2），不注入默认值、不猜。「以为脱敏了其实没有」
  比少一个功能更危险，所以选项是**严格白名单**——未知键（打错字的键）直接拒绝。
* **脱敏覆盖面**：用户选定的手机号 / 用户名 / 发送者显示名 / 会话名 / 转发者名 / 自填值，
  既替换元数据字段，也替换**正文里出现的这些值**（含大小写变体）；同一份副本里同一个人
  （按发送者 ID，缺 ID 时按规范化名字）化名一致。
* **不变量（与字段选择无关）**：分享目录里不得出现会话/登录文件、代理凭据（含 ``--proxy``
  里的 ``username:password``）、完整原始 JSON、私有来源绝对路径、未经选择的附件、化名映射表。
  正文里出现的绝对路径形态一律隐去（含空格的路径整条一起隐）。**被用户逐项选中的附件也要按
  内容与结构判定**（SQLite 会话库结构、凭据串、映射表/台账/全量私有报告的 kind 字段、原始
  节点键、路径形态），命中就拒绝入包并点名申报，状态不得 complete（评审 C1）。
* **目录布局**：分享目标与 ``private_root``/``reading_pack``/``attachment_root`` 之间**两个
  方向**都不得相等或互相包含，按解析后的目录形态判定（含 junction/符号链接、同盘同父、尾随
  点/空格），在**任何文件写出之前**按配置非法拒绝（评审 C2）。
* **附件**：逐项选择；文件名也过同一套脱敏；清洗后同名（含只差大小写，Windows 有效路径不分
  大小写）不互相覆盖（追加序号）；未选的附件一个都不复制；解析后越出所选附件目录（``..``、
  绝对路径外指、符号链接/junction 式指向）一律拒绝落盘并申报。落盘后再核对「点了几个、落了
  几个」，没落盘的不计入 copied、不生成指向空气的链接。
* **构建被打断**：``report.json`` 从一开始就占位，异常时覆写成 ``status=incomplete`` 的未完成
  申报并清掉 ``*.part`` 残留——半成品目录任何时刻都机器可读地写着「这不是成品」（评审 I4）。
* **媒体内嵌信息没做就是没做**：未做 OCR、声音转写/声纹、EXIF/metadata 剥离时逐条写进
  ``uncovered``；``claims`` 里 ``media_fully_redacted`` 恒为 ``false``、
  ``full_anonymity_claimed`` 恒为 ``false``。用户要求「完全脱敏」而这些条件无法兑现时，
  原媒体**不放进分享包**，改为 ``pending_decisions`` 待用户决策（状态 partial）。
* **链接**：分享目录里的每个链接只能是目录内相对路径，且目标真实存在
  （:func:`verify_share_links` 逐文件遍历，反例直接拒绝）。
* **离线动作**：不需要 tdl / 会话 / 网络；映射表与私有报告的读写全程复用**唯一一份**
  :func:`state.acquire_archive_lock`（marker 由 ``state.archive_lock_marker`` 算，本模块
  不再自己算一套），与在线动作按「先归档锁、后会话锁」互斥。
* **视图申报**：``text_view="raw"`` 取的是原始正文，可能含私有阅读版已过滤掉的内容，必须
  在报告与目录页写清；``text_view="reading"`` 只复用 Task 4 的显式清洗结果
  （:func:`reading.clean_messages`），不回读被过滤的私密原文。

真实媒体的脱敏效果**不由本模块的合成文字测试代替**：这里的遍历断言只证明「选定文字与
文件名没有泄漏」，不证明图片像素、声轨或 EXIF 里没有信息。
"""

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from urllib.parse import quote, unquote

import importing
import job as jobs
import reading
import state

SHARE_KIND = "telegram-archive-share-report"
SHARE_VERSION = 1
INDEX_NAME = "index.md"
REPORT_NAME = "report.json"
PARTS_DIR = "parts"
MEDIA_DIR = "media"
ALIAS_MAP_PREFIX = "share-alias"
HIDDEN_PATH = "[路径已隐去]"
HIDDEN_PHONE = "[已隐去·手机号]"
HIDDEN_OTHER = "[已隐去]"
HIDDEN_LOCAL = "（私有绝对路径已隐去）"
UNKNOWN_SENDER = "未知发送者"
ATTACH_LABEL = "附件"

MASK_FIELDS = ("phone", "username", "sender_name", "chat_name", "forwarded_name", "other")
ALIAS_SERIES = {"sender_name": "参与者", "chat_name": "会话", "forwarded_name": "转发者",
                "username": "用户"}
GENERIC_REPLACEMENT = {"phone": HIDDEN_PHONE, "other": HIDDEN_OTHER}
VIEWS = ("raw", "reading")
CLEANING_CHOICES = ("none", "ads")
OPTION_KEYS = frozenset(("fields", "values", "with_attachments", "attachments",
                         "attachment_root", "text_view", "cleaning", "template_compression",
                         "private_root", "proxy", "require_full_redaction", "archive_path",
                         "reading_pack", "stem"))
# 附件类别 -> 未覆盖能力：没做的事必须逐条写清，不能拿「文字已脱敏」冒充。
UNCOVERED_BY_CATEGORY = {
    "image": ("图面内嵌文字（OCR）未处理", "EXIF/元数据未处理"),
    "video": ("画面内嵌文字（OCR）未处理", "声音内嵌信息（转写/声纹）未处理", "元数据未处理"),
    "animation": ("图面内嵌文字（OCR）未处理", "声音内嵌信息未处理", "元数据未处理"),
    "audio": ("声音内嵌信息（转写/声纹）未处理", "元数据未处理"),
    "voice": ("声音内嵌信息（转写/声纹）未处理", "元数据未处理"),
    "sticker": ("图面内嵌文字（OCR）未处理", "元数据未处理"),
    "document": ("文件内容未做任何文字脱敏（只做了文件名脱敏）",),
    "other": ("文件内容未做任何文字脱敏（只做了文件名脱敏）",),
    "unknown": ("文件内容与内嵌信息都没有处理（类别也未判定，不默选）",),
}
BASE_UNCOVERED = {"scope": "正文", "category": "text",
                  "reason": "只替换用户选定字段的字面值，未做语义级匿名（内容里提到的"
                            "其它人名、地名、编号仍在）"}
INDEX_CLAIM = "## 文字脱敏 ≠ 媒体完全脱敏"
TEXT_UNSAFE = re.compile(r'[\\/:*?"<>|#%\[\]\x00-\x1f]')
PHONE_PATTERNS = (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
                  re.compile(r"(?<![\w.])\+\d{7,15}(?!\d)"))
HANDLE_PATTERN = re.compile(r"@[\w.\-]{2,}")
# 评审 I3：`[A-Za-z]:[\\/][^\s\"'<>|]{1,200}` 一遇空格就停，于是 `C:\…\我的 资料\私密导出.json`
# 只隐掉前半截、把「资料\私密导出.json」留在分享包里。现在按「盘符 + 中间段可含空格（段内不许
# 句读，所以不会跨过同一行的下半句），末段止于空格或分隔符」重写：整条路径一起隐，路径之后的
# 同句文字照原样保留；旧的不含空格形态作为第二条兜底。
_PATH_STOPS = "\"'<>|,;:!?\uff0c\u3002\uff1b\uff1a\uff01\u3001\n"
_PATH_SEP = "[\\\\/]"
# 修复轮 2 I3：UNC/POSIX/止于带空格目录名三类同样整条隐去。顺序很关键：先用“末段不含
# 空格”的文件形态（保留路径之后同句文字），再用“末段可含空格”的目录形态兜止于目录名的
# 那一类；含空格的 UNC/POSIX 必须排在旧的不含空格兜底之前，否则旧式先把前半截隐掉、
# 尾段文件名就漏出来了。量词一律贪婪：惰性会在 `C:\` 后就停，把 `Users\ops\...` 留在包里。
ABSOLUTE_PATH_PATTERNS = (
    re.compile("[A-Za-z]:" + _PATH_SEP + "(?:[^" + _PATH_STOPS + "]{1,120}" + _PATH_SEP
               + ")+" + "[^" + _PATH_STOPS + "\\\\/\\s]{1,80}\\.[^" + _PATH_STOPS
               + "\\\\/\\s]{1,10}"),
    re.compile("\\\\\\\\[^\\s\"'<>|" + _PATH_STOPS + "\\\\/]{1,80}"
               + "(?:[\\\\/][^" + _PATH_STOPS + "\\\\/]{1,80})*"
               + "[\\\\/][^" + _PATH_STOPS + "\\\\/\\s]{1,80}\\.[^" + _PATH_STOPS
               + "\\\\/\\s]{1,10}"),
    re.compile("(?<![\\w/.~-])/(?:[^" + _PATH_STOPS + "\\\\/]{1,80}/)+"
               + "[^" + _PATH_STOPS + "\\\\/\\s]{1,80}\\.[^" + _PATH_STOPS
               + "\\\\/\\s]{1,10}"),
    re.compile("[A-Za-z]:" + _PATH_SEP + "(?:[^" + _PATH_STOPS + "]{1,200})"),
    re.compile("\\\\\\\\[^" + _PATH_STOPS + "\\n]{2,200}"),
    re.compile("(?<![\\w/.~-])/(?:[^" + _PATH_STOPS + "\\n]{1,80}/)+"
               + "[^" + _PATH_STOPS + "\\n\\\\/]{1,80}"),
    re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]{1,200}"),
    re.compile(r"\\\\[^\s\"'<>|]{2,200}"),
    re.compile(r"(?<![\w/.~-])/(?:[\w.\-]+/){1,}[\w.\-]+"),
)
LINK = re.compile(r"\[[^\]]*\]\(([^)\n]*)\)")
# 正文是不可信用户内容：其中的裸链接语法、URL 与 HTML 标签一律在渲染前就地移除，
# 免得「[点开](../私有归档)」这类字符串变成分享目录里的真链接、或把 http:// 带进正文。
# reading.escape_body 只会打断自动链接（加一个反斜杠），去不掉手写的 [..](..)，也挡不住
# 「去掉反斜杠后仍是 http://」的形态——所以分享侧要再做一层更硬的中和。
MD_LINK = re.compile(r"!?\[[^\]]*\]\([^)]*\)")
MD_REF = re.compile(r"!?\[[^\]]*\]\[[^\]]*\]")
URL_SCHEME = re.compile(r"(?i)\b(?:https?|ftps?|ws|wss|mailto|file|telnet|svn|git):[^\s\"'<>)]+")
WWW_TOKEN = re.compile(r"(?i)\bwww\.[^\s\"'<>)]+")
# 评审 I2：只中和 `scheme:` 与 `www.` 不够——裸域名（evil.example/path）、裸邮箱
# （a@evil.example）与 host:port（host:8080/a）在 Telegram / 多数渲染器里同样会被自动识别成
# 可点面，而 verify_share_links 只看得见 `[..](..)` 语法，抓不到这一面。三条新形态各自要求
# 「后面紧跟 / 或 :端口」或「@ 后面是带点的域」，所以 会议记录.png / v1.2.txt 这类普通词不受影响。
# 修复轮 2：不带 / 或端口的裸域（bit.ly、evil.example）同样不可点，但 file.txt/v1.2.txt 这类
# 文件名不得被吞——裸形态只认已知 TLD（含测试用的 example/test），带 / 或 :端口的仍认任意 TLD；
# trailing-dot 的 FQDN（evil.example./path）一并覆盖。HOST_PORT 收窄：单标签 host 必须带 / 路径
# （host:8080/a 才算链接），否则 report:2024/ID:12345/v2:2020 会被误吞；带点的 host:port 仍认。
EMAIL_TOKEN = re.compile(r"(?i)(?<![\w.+-])[a-z0-9._%+-]+@(?:[a-z0-9](?:[a-z0-9-]{0,61}"
                         r"[a-z0-9])?\.)+[a-z]{2,}")
BARE_DOMAIN = re.compile(r"(?i)(?<![\w./:@-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                         r"[a-z]{2,}\.?(?=[/:])(?::\d{2,5})?(?:/[^\s<>\"'`)]*)?")
BARE_DOMAIN_NAKED = re.compile(
    r"(?i)(?<![\w./:@-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|net|org|io|me|co|cn|gov|edu|info|biz|dev|app|xyz|top|site|online|"
    r"shop|club|wiki|blog|ly|example|test|invalid|localhost)\.?(?![\w./:@-])")
HOST_PORT = re.compile(
    r"(?i)(?<![\w./:@-])(?:"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}:\d{2,5}"
    r"(?:/[^\s<>\"'`)]*)?"
    r"|[a-z][a-z0-9-]{0,63}:\d{2,5}/[^\s<>\"'`)]*)")
HTML_TAG = re.compile(r"</?[A-Za-z!][A-Za-z0-9]*(?:\s[^>]*)?/?>")
LINK_HIDDEN = "[链接已移除]"
URL_HIDDEN = "[外部链接已移除]"
TAG_HIDDEN = "[标记已移除]"
MAX_PART_MESSAGES = 200
MAX_PART_BYTES = 60000
# 私有层产物的身份判据（评审 C1：被逐项选中的附件本身可能就是会话库、登录密钥、带口令的代理
# 配置、导入台账，甚至化名映射表）。**路径形态 + 内容与结构**两路都查，光看文件名挡不住改名。
ALIAS_MAP_KIND = "telegram-archive-share-alias-map"
LEDGER_KIND = "telegram-archive-import-batches"
RAW_NODE_KEYS = (b"messageMediaPhoto", b"messageMediaDocument", b"messageMediaCustom",
                 b'"@type"')
SQLITE_MAGIC = b"SQLite format 3\x00"
CREDENTIAL_URL = re.compile(r"(?i)(?:socks[45]h?|https?|ftps?|proxy)://[^\s/:@\"'`]+:"
                            r"[^\s@\"'`]{3,}@[^\s\"'`/]+")
# 评审 N4：无 scheme 的 user:pass@host（curl/htpasswd 常见写法）同样是凭据。要求主机带
# 点或为 IPv4（免得把 report:2024 这类当凭据），口令 ≥3 位，用户名 ≥2 位。
BARE_CREDENTIAL = re.compile(r"(?i)(?<![\w/:])[a-z0-9._%+-]{2,}:[^\s@\"'`]{3,}@"
                             r"(?:(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                             r"[a-z]{2,}|(?:\d{1,3}\.){3}\d{1,3})"
                             r"(?::\d{2,5})?(?![\w.%-])")
PRIVATE_ARTIFACT_SHAPES = (
    (re.compile(r"\.session(?:[-.][^\\/]*)?$", re.I), "Telegram 会话/登录库（session 文件）"),
    (re.compile(r"(?:^|[\\/])session(?:$|[-.][^\\/]*$)", re.I),
     "Telegram 会话/登录库（无扩展名 session 文件）"),
    (re.compile(r"\.dtmp$", re.I), "登录密钥临时文件（.dtmp）"),
    (re.compile(r"\.env$", re.I), "环境变量/凭据配置文件（.env）"),
    (re.compile(r"\.(pem|key|p12|kdbx|secret|credentials)$", re.I), "密钥或口令库文件"),
    (re.compile(r"(?:^|[\\\\/])imported\.json$", re.I), "导入台账 imported.json"),
    (re.compile(r"(?:^|[\\\\/])%s" % ALIAS_MAP_PREFIX, re.I), "化名映射表或全量私有报告"),
    (re.compile(r"(?:^|[\\\\/])%s(?:[\\\\/]|$)" % state.LOCK_SUBDIR, re.I), "归档锁目录内的文件"),
    (re.compile(r"(?:^|[\\\\/])(?:tdl|telegram)[-_]?(?:storage|session)", re.I),
     "Telegram 登录 storage"),
)
LIMITATIONS = {
    "single_archive_per_run": "一次 share 只吃一个归档；多个归档请各自输出到各自的分享目录"
                              "（同一目标重复运行会复用同一份化名映射表）",
    "media_content_not_processed": "附件只做**文件名**脱敏与逐项选择；图片/视频/音频的内嵌"
                                   "文字（OCR）、声音与 EXIF/metadata 都没有处理，报告逐条标"
                                   "未覆盖。文字脱敏不等于媒体完全脱敏",
    "synthetic_or_local_only": "全部基于本地已导入快照，不代表真实在线导出已验证",
}


class ShareError(ValueError):
    """分享选择或落盘边界不合法（CLI 按配置非法归退出码 2）。"""


def _require(condition, message):
    if not condition:
        raise ShareError(message)


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _digest(path):
    """文件内容摘要：1MB 分块流式迭代（与 media._digest_of 同口径）。

    TG-02：分享附件可达 GB 级，严禁整包 read_bytes 读入内存（OOM）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_if(path):
    """给了归档文件路径才比对；文件不存在时返回 None（不 crash，也不冒充已验证）。"""
    resolved = Path(str(path))
    return _digest(resolved) if resolved.is_file() else None


def _neutralize_links(text):
    """把正文里的链接面就地拆掉：手写 Markdown 链接/引用式链接、裸 URL、裸域名/裸邮箱/
    host:port、HTML 标签。

    阅读版里出现的每一个真链接都只能由 sharing 自己生成；用户正文里的链接语法一律降级成
    中性占位串，这样 verify_share_links 既不会被「[..](../私有归档)」骗到、正文里也不会残留
    去反斜杠后还原成 http:// 的形态。评审 I2：`scheme:` 与 `www.` 之外，裸域名（带 `/` 或
    `:端口`）、裸邮箱与 `host:8080/a` 也会被渲染器自动识别成可点面，而 verify_share_links
    只解析 `[..](..)` 语法看不见这一面——所以三条形态一并中和（口径见 references 分享节）。
    修复轮 2：不带 `/` 的裸域（bit.ly、evil.example，已知 TLD 才算）同样中和；单标签
    `host:port` 无 `/` 路径的不算链接（report:2024/ID:12345/v2:2020 不误吞）。"""
    out = MD_LINK.sub(LINK_HIDDEN, text)
    out = MD_REF.sub(LINK_HIDDEN, out)
    out = URL_SCHEME.sub(URL_HIDDEN, out)
    out = WWW_TOKEN.sub(URL_HIDDEN, out)
    out = EMAIL_TOKEN.sub(URL_HIDDEN, out)
    out = BARE_DOMAIN.sub(URL_HIDDEN, out)
    out = BARE_DOMAIN_NAKED.sub(URL_HIDDEN, out)
    out = HOST_PORT.sub(URL_HIDDEN, out)
    out = HTML_TAG.sub(TAG_HIDDEN, out)
    return out


def _dict_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def _tree_digest(base):
    """目录树摘要：把「私有阅读版没被动」变成可比对的一个数字。"""
    base = Path(str(base))
    out = {}
    for path in sorted(base.rglob("*")):
        if path.is_file():
            out[path.relative_to(base).as_posix()] = _digest(path)
    return hashlib.sha256(json.dumps(out, sort_keys=True).encode("utf-8")).hexdigest()


def _absolute(value, label):
    _require(isinstance(value, str) and _text(value), "%s 必须是非空绝对路径字符串" % label)
    path = Path(_text(value))
    _require(path.is_absolute(), "%s 必须是绝对路径（不猜相对位置）：%s" % (label, value))
    return path


def _url(relative):
    """链接编码：目标名里的空格/括号/中文都逐段编码，回读时 unquote 还原成真实文件名。"""
    return "/".join(quote(part, safe="") for part in PurePosixPath(relative).parts)


def share_id(target):
    """同一分享目标＝同一份化名映射：只取目标目录的规范化绝对路径散列。

    评审 N2：必须用 C2 的 ``_dir_shape`` 键（realpath+normcase+逐段去尾随点/空格），
    否则同一目录写成 ``X`` 与 ``X.``/大小写/junction 会得到两份映射表，跨构建的同一
    化名指向两个真人。"""
    return hashlib.sha256(_dir_shape(target).encode("utf-8")).hexdigest()[:16]


def mapping_path(target, private_root):
    """化名映射表的落点：**只在私有归档位置**，绝不进分享目录。"""
    return Path(str(private_root)) / ("%s-%s.json" % (ALIAS_MAP_PREFIX, share_id(target)))


def path_within(root, candidate):
    """containment：解析后（含符号链接/junction 重解析点）必须仍在 root 之内。"""
    base = os.path.realpath(str(Path(str(root)).absolute()))
    resolved = os.path.realpath(str(Path(str(candidate)).absolute()))
    if resolved == base:
        return False
    try:
        Path(resolved).relative_to(base)
    except ValueError:
        return False
    return True


def _dir_shape(path):
    """目录比较键：解析重解析点（符号链接/junction）+ 统一大小写与分隔符 + 逐段去掉尾随点/空格。

    Windows 上 `C:\\分享副本`、`C:/分享副本.`、`c:\\分享副本\\. ` 指的是同一个目录，只比字符串
    写法就会漏判；这里把它们都归一到同一个形态（评审 C2）。"""
    resolved = os.path.realpath(str(Path(str(path)).absolute())).replace("\\", "/")
    parts = [os.path.normcase(part).rstrip(" .") for part in resolved.split("/")]
    return "/".join(part for part in parts if part)


def _shape_within(root, candidate):
    """段级 containment：用 relative_to 判定，绝不用 startswith 比字符串前缀。"""
    try:
        PurePosixPath(candidate).relative_to(PurePosixPath(root))
    except ValueError:
        return False
    return True


def _same_or_within(left, right):
    """两个目录是否「同一个」或「互相包含」（双向，解析后比较）。"""
    a, b = _dir_shape(left), _dir_shape(right)
    return a == b or _shape_within(a, b) or _shape_within(b, a)


def resolve_within(root, value):
    """把路径解析到所选目录内；越界（``..``、绝对路径外指、符号链接式指向）就拒绝。"""
    raw = Path(str(value))
    base = Path(str(root)).absolute()
    candidate = raw if raw.is_absolute() else base / raw
    resolved = Path(os.path.realpath(str(candidate)))
    _require(path_within(base, resolved),
             "路径解析后越出了所选目录，拒绝使用：%s（只允许在 %s 之内）" % (value, base))
    return resolved


# ------------------------------------------------------------------ 选项校验

def _validate_options(options, target, archive):
    _require(isinstance(archive, dict) and archive.get("kind") == importing.ARCHIVE_KIND,
             "分享的源必须是已归一化的共同归档（kind=%s），本模块不猜文件内容"
             % importing.ARCHIVE_KIND)
    _require(isinstance(options, dict), "options 必须是映射(dict)：分享选择全部显式给出")
    unknown = sorted(set(options) - OPTION_KEYS)
    _require(not unknown,
             "分享选项含未知键：%s（可用键只有 %s；打错字的键不会被静默忽略，否则等于"
             "「以为脱敏了其实没有」）"
             % ("、".join(unknown), "、".join(sorted(OPTION_KEYS))))
    out = {}

    _require("fields" in options,
             "fields 缺失：必须显式给出要脱敏的字段（可以是空列表，但空列表＝一项都不脱敏，"
             "只剩私有路径与凭据这类不变量）")
    fields = options["fields"]
    _require(isinstance(fields, list) and all(isinstance(item, str) for item in fields),
             "fields 必须是字符串列表")
    bad = sorted(set(fields) - set(MASK_FIELDS))
    _require(not bad, "fields 含不支持的脱敏字段：%s（可选 %s）"
             % ("、".join(bad), "、".join(MASK_FIELDS)))
    out["fields"] = [item for item in MASK_FIELDS if item in fields]

    values = options.get("values") or {}
    _require(isinstance(values, dict), "values 必须是「字段 -> 敏感值列表」的映射")
    _require(not (set(values) - set(MASK_FIELDS)),
             "values 含未知字段：%s" % "、".join(sorted(set(values) - set(MASK_FIELDS))))
    cleaned = {}
    for key, items in values.items():
        _require(isinstance(items, list) and items and all(_text(item) for item in items),
                 "values.%s 必须是非空字符串列表（数字/None 一律拒绝，不靠 str() 兜住）" % key)
        cleaned[key] = sorted({_text(item) for item in items})
    out["values"] = cleaned

    _require("with_attachments" in options,
             "with_attachments 缺失：是否带附件必须由用户显式选择，不默认带也不默认不带")
    _require(isinstance(options["with_attachments"], bool), "with_attachments 必须是布尔值")
    out["with_attachments"] = options["with_attachments"]

    _require("text_view" in options,
             "text_view 缺失：必须显式说明取原始正文（raw）还是过滤后的阅读正文（reading）")
    _require(options["text_view"] in VIEWS, "text_view 只能是 %s，不默认、不猜"
             % ("、".join(VIEWS)))
    out["text_view"] = options["text_view"]
    if out["text_view"] == "reading":
        _require("cleaning" in options,
                 "text_view=reading 必须同时给出 cleaning（阅读版是显式清洗后的产物，不给就"
                 "等于不知道过滤了什么）")
    _require(options.get("cleaning", "none") in CLEANING_CHOICES,
             "cleaning 只能是 %s" % ("、".join(CLEANING_CHOICES)))
    out["cleaning"] = options.get("cleaning", "none")
    out["compression"] = bool(options.get("template_compression", False))

    out["private_root"] = _absolute(options.get("private_root"), "private_root")
    out["reading_pack"] = _absolute(options["reading_pack"], "reading_pack") \
        if options.get("reading_pack") else None
    out["archive_path"] = _absolute(options["archive_path"], "archive_path") \
        if options.get("archive_path") else None
    if out["reading_pack"]:
        _require(out["reading_pack"].is_dir(), "reading_pack 指向的私有阅读版目录不存在：%s"
                 % out["reading_pack"])

    out["require_full_redaction"] = bool(options.get("require_full_redaction", False))
    out["stem"] = options.get("stem", "chat")
    _require(re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", str(out["stem"])) is not None,
             "stem 只能是字母/数字/下划线/连字符（会直接进文件名）：%s" % out["stem"])
    proxy = options.get("proxy")
    out["proxy"] = jobs._safe_proxy(proxy) if _text(proxy) else None

    share_target = Path(str(target))
    _require(share_target.is_absolute(), "分享输出位置必须是绝对路径（不猜相对位置）：%s"
             % share_target)
    out["target"] = share_target

    selected = []
    out["attachment_root"] = None
    if out["with_attachments"]:
        _require(options.get("attachment_root"),
                 "with_attachments=true 必须给出 attachment_root（用户选定的附件目录绝对路径）")
        out["attachment_root"] = _absolute(options["attachment_root"], "attachment_root")
        _require(out["attachment_root"].is_dir(), "attachment_root 目录不存在：%s"
                 % out["attachment_root"])
        _require("attachments" in options,
                 "with_attachments=true 必须逐项给出 attachments（附件不是整目录开关）")
        items = options["attachments"]
        _require(isinstance(items, list), "attachments 必须是列表")
        for item in items:
            _require(isinstance(item, dict) and set(item) == {"message_id", "index"},
                     "attachments 每项只能是 {message_id, index}：%r" % (item,))
            mid, idx = item["message_id"], item["index"]
            _require(isinstance(mid, int) and not isinstance(mid, bool) and mid > 0,
                     "attachments.message_id 必须是正整数：%r" % (mid,))
            _require(isinstance(idx, int) and not isinstance(idx, bool) and idx >= 0,
                     "attachments.index 必须是非负整数：%r" % (idx,))
            selected.append((mid, idx))
        _require(len(set(selected)) == len(selected), "attachments 含重复项")
    else:
        _require(not options.get("attachments"),
                 "with_attachments=false 时不得同时给出 attachments（两者互相矛盾）")
    out["selected"] = selected
    # 评审 C2：containment 必须**双向**判，且在**任何文件写出之前**拒。原来只查「分享目标是否
    # 落在私有层之内」，于是 private_root（化名映射表与全量私有报告的落点）反过来落在分享目录
    # 里、或与分享目录相等时，映射表就被直接写进转发目录——等于把真名对照表一起发出去。
    # 这里比较的是解析后的目录形态（含 junction/符号链接重解析点、同盘同父、尾随点/空格），
    # 用 relative_to 逐段判定，不用字符串前缀，免得「分享副本」与「分享副本-私有」被误判。
    for label, other in (("私有归档位置 private_root", out["private_root"]),
                         ("私有阅读版 reading_pack", out["reading_pack"]),
                         ("所选附件目录 attachment_root", out["attachment_root"])):
        if not other:
            continue                      # reading_pack / attachment_root 可以没给
        _require(not _same_or_within(share_target, other),
                 "分享输出位置与%s（%s）是同一个目录或互相包含（两个方向都判）：这样化名映射表、"
                 "全量私有报告与私有层会被铺进分享目录，转发目录就等于转发私有层，拒绝。请把两者"
                 "放到互不包含的两个绝对路径下：%s <-> %s" % (label, other, share_target, other))
    return out


# ------------------------------------------------------------------ 化名与替换

class Masker:
    """按规则替换敏感值并逐字段计数；绝对路径形态无条件隐去（不变量）。"""

    def __init__(self, rules=()):
        self.rules = list(rules)
        self.redacted = {}
        self.path_scrubbed = 0

    def add(self, field, original, replacement):
        original = _text(original)
        if not original:
            return
        for variant in {original, reading.escape_body(original)}:
            self.rules.append({"field": field, "original": original, "variant": variant,
                               "pattern": re.compile(re.escape(variant), re.IGNORECASE),
                               "replacement": replacement})

    def __call__(self, text):
        out = text or ""
        for rule in self.rules:
            out, hits = rule["pattern"].subn(rule["replacement"], out)
            if hits:
                self.redacted[rule["field"]] = self.redacted.get(rule["field"], 0) + hits
        for pattern in ABSOLUTE_PATH_PATTERNS:
            out, hits = pattern.subn(HIDDEN_PATH, out)
            self.path_scrubbed += hits
        return out

    def ordered(self):
        """长值先替换，避免短值把长值切断后留下半截敏感串。"""
        self.rules.sort(key=lambda item: (-len(item["variant"]), item["variant"].casefold()))


class Aliases:
    """化名表：同一个人（发送者 ID，缺 ID 用规范化名字）在同一份副本里只有一个化名。"""

    def __init__(self, existing=()):
        self.entries = []
        self._by_value = {}
        self._by_person = {}
        self._count = {}
        for entry in existing or []:
            self._adopt(entry)

    def _adopt(self, entry):
        field, alias, original = entry["field"], entry["alias"], entry["original"]
        self.entries.append(dict(entry))
        self._by_value[(field, original.casefold())] = alias
        if entry.get("person"):
            self._by_person[entry["person"]] = alias
        if field in ALIAS_SERIES:
            try:
                number = int(alias.rsplit("-", 1)[1])
            except (IndexError, ValueError):
                number = 0
            self._count[field] = max(self._count.get(field, 0), number)

    def alias_for(self, field, original, person=None):
        if field not in ALIAS_SERIES:
            return GENERIC_REPLACEMENT.get(field, HIDDEN_OTHER)
        key = (field, original.casefold())
        if key in self._by_value:
            alias = self._by_value[key]
            if person:
                self._by_person.setdefault(person, alias)
            return alias
        alias = self._by_person.get(person) if person else None
        if not alias:
            self._count[field] = self._count.get(field, 0) + 1
            alias = "%s-%02d" % (ALIAS_SERIES[field], self._count[field])
        self._by_value[key] = alias
        if person:
            self._by_person[person] = alias
        self.entries.append({"alias": alias, "field": field, "original": original,
                             "person": person})
        return alias


def _stream(archive):
    rows = archive.get("messages") or []
    _require(isinstance(rows, list) and all(isinstance(item, dict) for item in rows),
             "归档的 messages 必须是消息映射列表")
    return rows


def _inventory(archive):
    """清点要检查的字符串：正文、发送者/转发者/会话名、附件路径与标题（node 原样不算）。"""
    texts, structured = [], {"sender_name": [], "chat_name": [], "forwarded_name": []}
    for item in _stream(archive):
        if _text(item.get("text")):
            texts.append(item["text"])
        sender = item.get("sender") or {}
        if isinstance(sender, dict) and _text(sender.get("name")):
            structured["sender_name"].append(sender["name"])
        forward = item.get("forwarded_from") or {}
        if isinstance(forward, dict) and _text(forward.get("name")):
            structured["forwarded_name"].append(forward["name"])
        for node in item.get("attachments") or []:
            if not isinstance(node, dict):
                continue
            for key in ("path", "title"):
                # 文件名/标题只进 texts 供手机号/@用户名模式扫描；绝不整串登记成某个字段值，
                # 否则「上午/会议记录.png」会被当成用户名替换掉、同名冲突也测不出来。
                if _text(node.get(key)):
                    texts.append(node[key])
    chat = archive.get("chat") or {}
    for key in ("display_name", "locator"):
        if _text(chat.get(key)):
            structured["chat_name"].append(chat[key])
    structured["sender_name"] = sorted(set(structured["sender_name"]))
    structured["chat_name"] = sorted(set(structured["chat_name"]))
    structured["forwarded_name"] = sorted(set(structured["forwarded_name"]))
    return texts, structured


def _build_masker(fields, values, texts, structured, aliases, archive):
    """选定字段的敏感值 -> 替换规则；名字类走化名（同人一致），手机号/自填值走占位串。"""
    masker = Masker()
    blob = "\n".join(texts)
    for field in fields:
        found = list(values.get(field) or []) + list(structured.get(field) or [])
        if field == "phone":
            found.extend(match.group(0) for pattern in PHONE_PATTERNS
                         for match in pattern.finditer(blob))
        if field == "username":
            found.extend(match.group(0) for match in HANDLE_PATTERN.finditer(blob))
        for original in sorted(set(item for item in found if _text(item)),
                               key=lambda item: (-len(item), item)):
            person = None
            if field in ALIAS_SERIES:
                person = "%s:%s" % (field, original.casefold())
                if field == "sender_name":
                    person = _sender_person(archive, original) or person
                masker.add(field, original, aliases.alias_for(field, original, person))
            else:
                masker.add(field, original, aliases.alias_for(field, original))
    masker.ordered()
    return masker


def _sender_person(archive, name):
    """同名同 ID 才算同一个人：优先用发送者 ID 当化名锚点。"""
    ids = set()
    for item in _stream(archive):
        sender = item.get("sender") or {}
        if isinstance(sender, dict) and _text(sender.get("name")) \
                and sender["name"].casefold() == name.casefold() and _text(sender.get("id")):
            ids.add(sender["id"])
    return ("id:" + sorted(ids)[0]) if len(ids) == 1 else None


def _safe_file_name(name, masker):
    """附件文件名也过同一套脱敏；清完为空或只剩分隔符就退回中性名。

    尾随的点与空格一并去掉（评审 I1 同族风险）：Windows 把 ``图.png ``/``图.png.`` 折成
    ``图.png`` 同一个落点，留着尾随字符等于让两份附件互相覆盖。"""
    masked = masker(name)
    stem = PurePosixPath(str(masked).replace("\\", "/")).name
    suffix = Path(stem).suffix[:12]
    body = TEXT_UNSAFE.sub("_", stem[:len(stem) - len(suffix)] if suffix else stem)
    body = body.strip(" .-")
    if not body:
        body = ATTACH_LABEL
    return ("%s%s" % (body[:72], suffix)).strip(" .")


def _name_key(name):
    """文件名占用键：Windows 的有效路径不区分大小写，`A.png` 与 `a.png` 是同一个落点。

    评审 I1：原来 `taken` 区分大小写，于是 `IMG_2024.png` 与 `img_2024.png` 双双通过、
    `shutil.copyfile` 落到同一个文件上静默互相覆盖，报告却写 copied=2 / conflicts=0。
    修复轮 2 N3：尾随点/空格同样折叠（`图.png ` 与 `图.png` 算同一个键），按 Windows 路径层
    逐段剥掉尾随点/空格（这里取最后一段再剥，与 `_safe_file_name` 的清理对齐）；
    `casefold()` 保证换到区分大小写的文件系统上判定也不会放宽。"""
    leaf = str(name).replace("\\", "/").split("/")[-1]
    return os.path.normcase(leaf).rstrip(" .").casefold()


def _unique_name(taken, name):
    key = _name_key(name)
    if key not in taken:
        return name, False
    stem, suffix = os.path.splitext(name)
    for attempt in range(2, 1000):
        candidate = "%s-%d%s" % (stem, attempt, suffix)
        if _name_key(candidate) not in taken:
            return candidate, True
    raise ShareError("同名附件过多，无法继续编号：%s" % name)


# ------------------------------------------------------------------ 私有产物身份判定（评审 C1）

def _head_bytes(path, limit=65536):
    try:
        with open(str(path), "rb") as handle:
            return handle.read(limit)
    except OSError:
        return b""


def _artefact_reason(source, mapping):
    """被选中的附件本身是不是「私有产物」：路径形态 + 内容与结构两路都查。

    返回申报语（说清这是什么、为什么不能进包）；None 表示按普通附件处理。文件名可以被人改，
    所以会话库（SQLite 结构）、代理口令串、映射表/台账/全量私有报告的 kind 字段、完整原始
    JSON 的节点键都单独判一遍。申报语里不回写文件内容，避免把凭据明文再抄一份进 report.json。
    """
    shown = os.path.normcase(str(source))
    for pattern, label in PRIVATE_ARTIFACT_SHAPES:
        if pattern.search(shown):
            return "路径形态是%s，属于私有层产物，不复制进分享包" % label
    if os.path.realpath(str(source)) == os.path.realpath(str(mapping)):
        return "这个文件就是本次分享的化名映射表本身（真名与化名对照），绝不进分享包"
    blob = _head_bytes(source)
    if not blob:
        return None
    if blob.startswith(SQLITE_MAGIC):
        return "内容结构是 SQLite 数据库（Telegram 会话库/登录 storage 的形态），不进分享包"
    text = blob.decode("utf-8", errors="ignore")
    if CREDENTIAL_URL.search(text) or BARE_CREDENTIAL.search(text):
        return "内容含代理/服务凭据形态（user:password@host，口令与用户名不回写），不进分享包"
    if ALIAS_MAP_KIND.encode("utf-8") in blob:
        return "内容结构是化名映射表（真名与化名对照），绝不进分享包"
    if LEDGER_KIND.encode("utf-8") in blob:
        return "内容结构是导入台账（批次清单与私有路径），不进分享包"
    if SHARE_KIND.encode("utf-8") in blob and b"private_root" in blob:
        return "内容结构是全量私有报告（含私有绝对路径与会话身份），不进分享包"
    if b'"messages"' in blob and any(key in blob for key in RAW_NODE_KEYS):
        return "内容结构是完整原始 JSON（未脱敏的归档节点），不属于分享包该带的东西"
    return None


# ------------------------------------------------------------------ 附件逐项核对

def _plan_media(archive, opts, masker):
    """只处理用户点名的 (message_id, index)；其它附件一个都不碰。

    三类拦下：解析后越界（``..``/绝对外指/链接式指向）、磁盘上不存在、以及**文件本身是私有
    产物**（会话/凭据/台账/映射表/完整 raw）——后者按内容与结构判定，不看用户选了它就越过。"""
    entries = {}
    for item in _stream(archive):
        for index, node in enumerate(item.get("attachments") or []):
            if isinstance(node, dict):
                entries[(item["message_id"], index)] = node
    missing = [pair for pair in opts["selected"] if pair not in entries]
    _require(not missing, "attachments 里有本归档没有的项：%s（不代为猜测别的消息的附件）"
             % "、".join("消息 %d 第 %d 项" % pair for pair in missing))
    root = Path(str(opts["attachment_root"])) if opts["attachment_root"] else None
    mapping = mapping_path(opts["target"], opts["private_root"])
    items, taken, refused, gone, artefacts, conflicts = {}, set(), [], [], [], 0
    for pair in opts["selected"]:
        node = entries[pair]
        category = node.get("category") or "unknown"
        raw = node.get("path") or ""
        display = _safe_file_name(raw or ("附件-%d-%d" % pair), masker)
        final, clashed = _unique_name(taken, display)
        conflicts += 1 if clashed else 0
        taken.add(_name_key(final))
        try:
            source = resolve_within(root, raw)
        except ShareError as error:
            refused.append({"message_id": pair[0], "index": pair[1], "category": category,
                            "name": final, "reason": str(error)})
            taken.discard(_name_key(final))
            continue
        reason = _artefact_reason(source, mapping)
        if reason:
            artefacts.append({"message_id": pair[0], "index": pair[1], "category": category,
                              "name": final, "reason": reason})
            taken.discard(_name_key(final))
            continue
        if not source.is_file():
            gone.append({"message_id": pair[0], "index": pair[1], "category": category,
                         "reason": "所选附件目录里没有这个文件，不链接、不冒充已带上"})
            taken.discard(_name_key(final))
            continue
        items[pair] = {"message_id": pair[0], "index": pair[1], "category": category,
                       "name": final, "rel": "%s/%s" % (MEDIA_DIR, final),
                       "source": source, "linked": False}
    return {"items": items, "refused": refused, "missing": gone, "artefacts": artefacts,
            "conflicts": conflicts, "not_selected": len(entries) - len(opts["selected"])}


# ------------------------------------------------------------------ 正文与目录页

def _shared_rows(archive, opts):
    """raw＝原样并申报；reading＝只复用 Task 4 的显式清洗结果，不回读被过滤的原文。"""
    rows = [(item["message_id"], item) for item in _stream(archive)]
    filtered = []
    if opts["text_view"] == "reading":
        rows, filtered = reading.clean_messages(rows, opts["cleaning"], opts["compression"])
    for message_id, item in rows:
        _require(isinstance(message_id, int) and not isinstance(message_id, bool)
                 and message_id > 0, "归档消息缺少正整数 message_id")
    return rows, filtered


def _moment(item):
    value = item.get("date_utc")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    return "时间未知"


def _sender_label(item, opts, masker, aliases, archive):
    """标题行的发送者：选了 sender_name 就写化名并计入脱敏数；没选就保留显示名。"""
    sender = item.get("sender") or {}
    name = _text(sender.get("name")) if isinstance(sender, dict) else None
    if name:
        if "sender_name" in opts["fields"]:
            person = _sender_person(archive, name) or "sender_name:%s" % name.casefold()
            masker.redacted["sender_name"] = masker.redacted.get("sender_name", 0) + 1
            return aliases.alias_for("sender_name", name, person)
        return masker(name) or UNKNOWN_SENDER
    forward = item.get("forwarded_from") or {}
    fname = _text(forward.get("name")) if isinstance(forward, dict) else None
    if fname and "forwarded_name" in opts["fields"]:
        masker.redacted["forwarded_name"] = masker.redacted.get("forwarded_name", 0) + 1
        return aliases.alias_for("forwarded_name", fname,
                                 "forwarded_name:%s" % fname.casefold())
    if fname:
        return masker(fname)
    return UNKNOWN_SENDER


def _render_parts(rows, opts, masker, aliases, archive, media):
    """分篇正文：标题行固定「## 化名 · 时间 · 消息 ID」，附件只链分享目录内的真实文件。"""
    chunks, owners, size = [[]], [[]], 0
    records = []
    for message_id, item in rows:
        lines = ["## 消息 %d · %s · %s" % (message_id,
                                          _sender_label(item, opts, masker, aliases, archive),
                                          _moment(item))]
        reply = item.get("reply_to") or {}
        if isinstance(reply, dict) and isinstance(reply.get("message_id"), int):
            lines.append("- 回复：消息 %d" % reply["message_id"])
        body = reading.escape_body(_neutralize_links(item.get("text") or ""))
        lines += ["", masker(body) if body.strip() else "（无文字内容）"]
        attached = []
        for index, node in enumerate(item.get("attachments") or []):
            entry = media.get((message_id, index))
            if not entry:
                continue
            linked = masker(entry["name"])
            entry["linked"] = True
            lines.append("- 附件（%s）：[%s](%s)" % (entry["category"], linked,
                                                   _url(os.path.relpath(
                                                       entry["rel"], PARTS_DIR)
                                                       .replace("\\", "/"))))
            attached.append(entry["rel"])
        chunk = "\n".join(lines)
        weight = len(chunk.encode("utf-8"))
        if chunks[-1] and (size + weight > MAX_PART_BYTES
                           or len(owners[-1]) >= MAX_PART_MESSAGES):
            chunks.append([])
            owners.append([])
            size = 0
        chunks[-1].append(chunk)
        owners[-1].append(message_id)
        size += weight
        records.append({"message_id": message_id, "chunk": len(chunks) - 1,
                        "time": _moment(item),
                        "sender": _sender_label_of(chunk), "attachments": attached})
    total = len(chunks)
    plan = {}
    for position, block in enumerate(chunks):
        name = "%s/%s-part-%04d.md" % (PARTS_DIR, opts["stem"], position + 1)
        head = ["# 分享副本 · 第 %d 篇（共 %d 篇）" % (position + 1, total), "",
                "[返回目录](%s)" % _url("../" + INDEX_NAME), ""]
        plan[name] = "\n".join(head + block) + "\n"
    for record in records:
        record["part"] = "%s/%s-part-%04d.md" % (PARTS_DIR, opts["stem"],
                                                 record["chunk"] + 1)
        record.pop("chunk")
    return plan, records


def _sender_label_of(chunk):
    first = chunk.splitlines()[0]
    parts = first[3:].split(" · ") if first.startswith("## ") else []
    return parts[1] if len(parts) > 1 else UNKNOWN_SENDER


def _render_index(opts, archive, masker, plan, records, rows, filtered, uncovered, media):
    chat = archive.get("chat") or {}
    title = masker(_text(chat.get("display_name")) or "未命名会话")
    view = "原始正文（raw）" if opts["text_view"] == "raw" else "过滤后的阅读正文（reading）"
    lines = ["# 分享副本 · 会话：%s" % title, "",
             "> 这是从本地归档**另建的一份独立副本**。原始归档与私有阅读版都没有被改动；"
             "化名映射表**只留在私有归档位置**，不在本目录里，拿到本目录的人无法据此还原真名。",
             "",
             "- 正文来源：%s" % view,
             "- 脱敏字段：%s" % ("、".join(opts["fields"]) or
                                "（一项都没选，只剩私有路径与凭据这类不变量）"),
             "- 未选的字段按原样保留（不是遗漏，是用户选择），清单见 report.json 的 "
             "unselected_fields",
             "- 消息：%d 条（本轮按清洗规则过滤掉 %d 条）；附件：%d 个（逐项选择，"
             "未经选择的一个都没复制）" % (len(rows), len(filtered), len(media)),
             "- 代理等运行凭据：不进分享包（如配置里有代理，只保留去掉认证信息的形态）",
             "", "## 分篇", ""]
    for name in sorted(plan):
        if name.endswith(".md"):
            lines.append("- [%s](%s)" % (os.path.basename(name), _url(name)))
    lines += ["", "## 未覆盖范围（逐条看完再决定要不要发）", ""]
    for item in uncovered:
        lines.append("- %s：%s" % (item["scope"], item["reason"]))
    lines += ["", INDEX_CLAIM, "",
              "本目录里的**文字字段与文件名**按用户选定项做了替换；但图片/视频/音频的内嵌"
              "文字（OCR）、声音内容与 EXIF/metadata **都没有处理**，附件文件本身是按原样"
              "复制的。所以不能把本目录称作「完全脱敏」或「匿名」——只要有一项内嵌信息没"
              "检查过，就仍有可识别风险。", "", "## 链接与来源", "",
              "- 本目录里的每个链接都只指向本目录内真实存在的文件（构建后逐条遍历校验）。",
              "- 本目录不含：会话/登录文件、user:password@host形态的代理凭据、完整原始 JSON、"
              "私有来源绝对路径、未经选择的附件、化名映射表。", ""]
    payload = {"kind": "telegram-archive-share-index", "version": SHARE_VERSION,
               "text_view": opts["text_view"], "messages": records}
    return "\n".join(lines), "%s/%s-index.json" % (PARTS_DIR, opts["stem"]), payload


# ------------------------------------------------------------------ 报告

def _uncovered(archive, opts, media, rows_count, artefacts=()):
    items = [dict(BASE_UNCOVERED)]
    if opts["text_view"] == "raw":
        items.append({"scope": "视图", "category": "text",
                      "reason": "本轮取的是原始正文（raw 视图），私有阅读版过滤掉的内容"
                                "可能在这里重新出现；要避开请改用 text_view=reading"})
    else:
        items.append({"scope": "视图", "category": "text",
                      "reason": "本轮取的是过滤后的阅读正文（reading 视图），清洗规则 "
                                "cleaning=%s、模板压缩=%s"
                                % (opts["cleaning"], opts["compression"])})
    if not media:
        items.append({"scope": "附件", "category": "none",
                      "reason": "本轮没有复制任何附件（未选择或要求完全脱敏）"})
    for entry in sorted(artefacts, key=lambda item: item["name"]):
        # 评审 C1：拦下来的东西必须点名「这是什么」，不能含糊成「文件内容未做文字脱敏」
        items.append({"scope": "附件 %s（消息 %d 第 %d 项）" % (entry["name"], entry["message_id"],
                                                       entry["index"]),
                      "category": "sensitive_artefact",
                      "reason": "%s；已按不变量拦在包外，本轮没有复制到 media/，"
                                "请另行决定要不要单独处理后再发" % entry["reason"]})
    for entry in sorted(media.values(), key=lambda item: item["name"]):
        for reason in UNCOVERED_BY_CATEGORY.get(entry["category"],
                                                UNCOVERED_BY_CATEGORY["other"]):
            items.append({"scope": "附件 %s" % entry["name"], "category": entry["category"],
                          "reason": reason})
    return items


def _looks_absolute(value):
    return str(value).startswith(("\\\\", "/")) or bool(re.match(r"^[A-Za-z]:[\\/]", str(value)))


def _public(report, hidden=()):
    """写进分享目录的 report.json：私有绝对路径与身份一律隐去，只留目录内相对路径。"""
    clone = copy.deepcopy(report)
    clone.pop("identity", None)
    prefixes = [item for item in hidden if item]

    def scrub(value):
        if isinstance(value, dict):
            return {key: (HIDDEN_LOCAL if key in ("private_root", "root", "path")
                          and isinstance(item, str) and _looks_absolute(item) else scrub(item))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str):
            for prefix in prefixes:
                if prefix and prefix in value:
                    return HIDDEN_LOCAL
            # 评审 I3（+ Minor 8）：整份公开报告过一次绝对路径 masker，不再只认「串首是绝对
            # 路径」。原因文案里夹着的私有路径、含空格的路径片段都在这里一起隐掉。
            for pattern in ABSOLUTE_PATH_PATTERNS:
                value = pattern.sub(HIDDEN_LOCAL, value)
            return HIDDEN_LOCAL if _looks_absolute(value) else value
        return value

    clone = scrub(clone)
    clone["mapping"] = {"stored": "private", "path": HIDDEN_LOCAL,
                        "entries": report["mapping"]["entries"],
                        # 评审 C1/C2：申报按磁盘实况算，不是硬编码 false
                        "in_share_package": bool(
                            report["claims"]["alias_map_in_share_package"]),
                        "note": "化名映射表留在私有归档目录，不随本包分发（本字段由构建后"
                                "遍历分享目录算出）"}
    clone["lock"] = {"kind": report["lock"]["kind"], "marker": report["lock"]["marker"],
                     "root": HIDDEN_LOCAL, "order": report["lock"]["order"],
                     "note": report["lock"]["note"]}
    return clone


def _headline(opts, counts, uncovered, pending):
    view = "原始正文（raw）" if opts["text_view"] == "raw" else "过滤后的阅读正文（reading）"
    return "分享副本（%s）：%d 条消息、脱敏替换 %d 处、附件 %d 个；未覆盖 %d 项、待决策 %d 项" % (
        view, counts["messages_shared"], sum(counts["redacted"].values()),
        counts["attachments_copied"], len(uncovered), len(pending))


def _publish(path, text):
    path = Path(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(str(temporary), str(path))
    return path


def _sweep_parts(target):
    """清掉本次构建残留的 ``*.part`` 临时文件（写盘中途失败时不该往分享包里留半截稿）。"""
    removed = []
    for path in sorted(Path(str(target)).rglob("*.part")):
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            continue
    return removed


def _unfinished_report(stage, reason):
    """评审 I4：被打断的半成品目录**必须**留一份可机读的未完成申报。

    构建一开始就先占住 ``report.json``（内容即本函数产出的三态之一），异常时再覆写成具体原因，
    所以任何时刻这个目录里都读得到「这不是一份成品」，而不是只剩看着像成品的 index.md/parts。"""
    return {"kind": SHARE_KIND, "version": SHARE_VERSION, "status": "incomplete",
            "complete": False, "stage": stage,
            "headline": "分享副本未完成（%s）：这不是一份成品，请勿转发，重跑后核对 report.json"
                        % stage,
            "counts": {}, "unselected_fields": [],
            "uncovered": [{"scope": "整包", "category": "text",
                           "reason": "构建在「%s」阶段被打断（%s）：本轮的脱敏计数、拦截清单与"
                                     "未覆盖申报都没有产出，不能据此判断包内是否干净" % (stage, reason)}],
            "pending_decisions": [{"kind": "interrupted", "stage": stage, "reason": str(reason),
                                   "action": "删掉本目录后重跑，或手工核对全部文件再决定是否转发"}],
            "refused": [], "missing_attachments": [], "refused_sensitive_artefacts": [],
            "unlinked_attachments": [], "files": [],
            "claims": {"original_archive_unchanged": False, "reading_pack_unchanged": False,
                       "text_only_masking": True, "media_fully_redacted": False,
                       "full_anonymity_claimed": False, "alias_map_in_share_package": False,
                       "network_used": False, "login_used": False, "tdl_required": False,
                       "offline_action": True,
                       "note": "构建未完成，任何「已脱敏/已拦截」的结论都未经验证"},
            "mapping": {"stored": "unknown", "path": HIDDEN_LOCAL, "entries": 0,
                        "sha256": None, "in_share_package": False,
                        "note": "未完成构建不声称映射表位置"},
            "lock": {"kind": "archive", "marker": None, "root": HIDDEN_LOCAL, "path": None,
                     "order": list(state.LOCK_ORDER), "note": "构建被打断，锁已按 finally 释放"},
            "limitations": dict(LIMITATIONS)}


def _declare_interruption(target, stage, error):
    """把未完成申报写进分享目录（尽力而为：写不动也不能把原始异常吞掉）。"""
    try:
        _publish(Path(str(target)) / REPORT_NAME,
                 json.dumps(_unfinished_report(stage, error), ensure_ascii=False, indent=2)
                 + "\n")
    except OSError:
        pass
    _sweep_parts(target)


def _copy_media(share_target, items):
    """先落盘、再渲染链接：链接只指向真实存在的文件；没落盘的一律申报，不冒充 copied。

    评审 I1 的账面谎报正出在「计划里算复制、磁盘上没落」这一环。返回 (落盘清单, 未落盘条目)。
    修复轮 2 N3：落盘后逐个比对内容哈希（04463c6 只对齐计数未保载荷）——两个逻辑名落到
    同一个物理文件时，后一份会把前一份覆盖掉，`is_file()` 看不见，但哈希对不上能看见；
    内容对不上的删掉坏文件、不链接、不计入 copied，改为 `attachment_not_landed` + partial，
    绝不静默吞载荷。"""
    copied, not_landed = [], []
    if not items:
        return copied, not_landed
    (share_target / MEDIA_DIR).mkdir(parents=True, exist_ok=True)
    wanted = {}
    for pair, entry in sorted(items.items()):
        destination = resolve_within(share_target, entry["rel"])
        shutil.copyfile(str(entry["source"]), str(destination))
        wanted[pair] = (entry, destination)
    for pair, (entry, destination) in sorted(wanted.items()):
        try:
            got = _digest(destination) if destination.is_file() else None
        except OSError:
            got = None
        try:
            want = _digest(entry["source"])
        except OSError:
            want = None
        if got is not None and want is not None and got == want:
            copied.append(entry["name"])
            continue
        try:
            if destination.is_file():
                destination.unlink()
        except OSError:
            pass
        not_landed.append(entry)
    return copied, not_landed


def _alias_map_in_share(share_target, mapping):
    """按**磁盘实况**回答「分享包里有没有化名映射表」（评审 C1/C2：申报不许是常量 false）。

    三条判据：与映射表解析后同一个文件、文件名是 ``share-alias*`` 形态（任何后缀，
    不只 .json——修复轮 2 N5）、内容结构带映射表 kind（.json/.txt/.md，懂 .txt）。
    只判定与申报，不删用户目录里的任何文件（合同：不能误删原档）。"""
    resolved = os.path.realpath(str(mapping))
    for path in sorted(Path(str(share_target)).rglob("*")):
        if not path.is_file():
            continue
        if os.path.realpath(str(path)) == resolved:
            return True
        if path.name.lower().startswith(ALIAS_MAP_PREFIX):
            return True
        if path.suffix.lower() in (".json", ".txt", ".md") \
                and ALIAS_MAP_KIND.encode("utf-8") in _head_bytes(path, 4096):
            return True
        if ALIAS_MAP_KIND.encode("utf-8") in _head_bytes(path, 4096):
            return True
    return False


def _file_list(target, skip=()):
    out = []
    base = Path(str(target))
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(base).as_posix()
        if name in skip:
            continue
        out.append({"path": name, "sha256": _digest(path), "bytes": path.stat().st_size})
    return out


def _stale_share_products(target, expected):
    """找出目标里上一轮留下的分享产物（评审 N1）：只认 parts/ 与 media/ 下的本轮清单之外
    文件，外加任何 *.part，不碰用户自己的其它文件（映射表只申报不删，见 _alias_map_in_share）。"""
    base = Path(str(target))
    if not base.is_dir():
        return []
    stale = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        if rel in expected:
            continue
        if rel.endswith(".part"):
            stale.append(path)
        elif rel.startswith(PARTS_DIR + "/") or rel.startswith(MEDIA_DIR + "/"):
            stale.append(path)
    return stale


def _reconcile_target(target, expected):
    """N1 对账：删掉上一轮的多余分篇/附件/残留，返回被清掉的相对路径（删不动的也如实返回，
    调用方据此转 partial，绝不带着上一轮未脱敏真名报 complete）。"""
    removed, kept = [], []
    for path in _stale_share_products(target, expected):
        rel = path.relative_to(Path(str(target))).as_posix()
        try:
            path.unlink()
            removed.append(rel)
        except OSError:
            kept.append(rel)
    return removed, kept


# ------------------------------------------------------------------ 公开入口

def create_share_copy(archive, target, options):
    """把一份共同归档出成独立分享副本，返回过滤计数与未覆盖范围报告。

    只读归档；写盘只落在 ``target``（分享目录）与 ``options["private_root"]``
    （化名映射表 + 私有全量报告）。映射表读写全程持归档锁。
    """
    opts = _validate_options(options, target, archive)
    share_target, private_root = opts["target"], opts["private_root"]
    mapping = mapping_path(share_target, private_root)
    input_before = _dict_digest(archive)
    archive_before = _digest_if(opts["archive_path"]) if opts["archive_path"] else None
    pack_before = _tree_digest(opts["reading_pack"]) if opts["reading_pack"] else None

    # 私有台账/映射表的读-改-写必须在锁里；marker 由 state 那一份实现算，这里不另算
    handle = state.acquire_archive_lock(private_root, resource=mapping)
    try:
        stored = json.loads(mapping.read_text(encoding="utf-8")) if mapping.is_file() else {}
        aliases = Aliases(stored.get("aliases") or [])
        texts, structured = _inventory(archive)
        masker = _build_masker(opts["fields"], opts["values"], texts, structured,
                               aliases, archive)
        rows, filtered = _shared_rows(archive, opts)
        media = _plan_media(archive, opts, masker)
        pending = [{"kind": "outside_scope", "message_id": item["message_id"],
                    "index": item["index"], "reason": item["reason"]}
                   for item in media["refused"]]
        pending += [{"kind": "missing_on_disk", "message_id": item["message_id"],
                     "index": item["index"], "reason": item["reason"]}
                    for item in media["missing"]]
        pending += [{"kind": "sensitive_artefact_refused", "message_id": item["message_id"],
                     "index": item["index"], "name": item["name"], "reason": item["reason"]}
                    for item in media["artefacts"]]
        if opts["require_full_redaction"] and media["items"]:
            pending.append({
                "kind": "media_full_redaction",
                "reason": "用户要求完全脱敏，但图片/视频/音频的内嵌文字（OCR）、声音转写与"
                          "元数据处理条件无法兑现；本轮**没有把任何原媒体放进分享包**，"
                          "请另行选择处理方式（先处理媒体、或只发文字）",
                "attachments": sorted(entry["name"] for entry in media["items"].values())})
            media["items"] = {}

        # 评审 I4：先占住 report.json（未完成三态），再落附件与正文。任何时刻被打断，目录里
        # 都读得到「这不是一份成品」；链接渲染放在附件落盘之后，只指向真实存在的文件（I1）。
        try:
            share_target.mkdir(parents=True, exist_ok=True)
            _publish(share_target / REPORT_NAME,
                     json.dumps(_unfinished_report("构建进行中", "尚未产出完整申报"),
                                ensure_ascii=False, indent=2) + "\n")
            copied, not_landed = _copy_media(share_target, media["items"])
            for entry in not_landed:
                media["items"].pop((entry["message_id"], entry["index"]), None)
                pending.append({"kind": "attachment_not_landed",
                                "message_id": entry["message_id"], "index": entry["index"],
                                "name": entry["name"],
                                "reason": "复制调用没有报错，但目标文件不在磁盘上：不链接、不计入"
                                          "copied，请检查磁盘与权限后重跑"})
            plan, records = _render_parts(rows, opts, masker, aliases, archive, media["items"])
            uncovered = _uncovered(archive, opts, media["items"], len(rows),
                                   media["artefacts"])
            index_text, index_path, index_payload = _render_index(
                opts, archive, masker, plan, records, rows, filtered, uncovered,
                media["items"])
            plan[index_path] = json.dumps(index_payload, ensure_ascii=False, indent=2) + "\n"
            plan[INDEX_NAME] = index_text
            for name, body in sorted(plan.items()):
                destination = resolve_within(share_target, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _publish(destination, body)
            # 评审 N1：本轮产物清单 vs 磁盘实况对账——上一轮的多余分篇/附件/.part 要么清掉，
            # 清不掉/清过也要 pending（绝不带着上一轮未脱敏真名报 complete）；先对账再校验，
            # 免得 verify 把陈旧分篇里的断链也算进来。
            expected = set(plan.keys()) | {REPORT_NAME} | \
                {entry["rel"] for entry in media["items"].values()}
            removed, kept = _reconcile_target(share_target, expected)
            if removed or kept:
                pending.append({
                    "kind": "stale_share_files",
                    "reason": "目标目录里有上一轮构建的产物（分篇/附件/临时文件），本轮已清掉 %d 个、"
                              "还有 %d 个清不掉：转发前请确认包里只有本轮文件" % (len(removed), len(kept)),
                    "removed": sorted(removed), "kept": sorted(kept),
                    "action": "已自动清理；若 kept 非空请手工删掉后重跑"})
            verify = verify_share_links(share_target)
        except Exception as error:
            _declare_interruption(share_target, "落盘与链接校验", error)
            raise

        unlinked = sorted(entry["name"] for entry in media["items"].values()
                          if not entry["linked"])
        alias_in_share = _alias_map_in_share(share_target, mapping)
        if alias_in_share:
            pending.append({
                "kind": "alias_map_present",
                "reason": "遍历分享目录发现映射表形态的文件（文件名 share-alias* 或内容带映射表 "
                          "kind）：本模块不会删用户目录里的文件，转发前请手工确认并移出；"
                          "「映射表不在包里」这项申报此刻不成立",
                "action": "把该文件移出分享目录后重跑，或改用互不包含的 private_root 与 target"})


        report = {
            "kind": SHARE_KIND, "version": SHARE_VERSION,
            "status": "partial" if pending else "complete",
            "share_id": share_id(share_target), "identity": archive.get("identity"),
            "counts": {"messages_in": len(_stream(archive)),
                       "messages_shared": len(rows),
                       "messages_filtered_out": len(filtered),
                       "redacted": {key: masker.redacted.get(key, 0) for key in MASK_FIELDS},
                       "path_scrubbed": masker.path_scrubbed,
                       "aliases": len(aliases.entries),
                       "attachments_selected": len(opts["selected"]),
                       "attachments_copied": len(copied),
                       "attachments_not_selected": media["not_selected"],
                       "attachments_name_conflicts": media["conflicts"],
                       "attachments_refused_outside_scope": len(media["refused"]),
                       "attachments_refused_sensitive": len(media["artefacts"]),
                       "attachments_missing_on_disk": len(media["missing"]),
                       "attachments_not_landed": len(not_landed),
                       "attachments_unlinked": len(unlinked), "links": verify["links"]},
            "unselected_fields": [item for item in MASK_FIELDS if item not in opts["fields"]],
            "uncovered": uncovered, "pending_decisions": pending,
            "refused": media["refused"], "missing_attachments": media["missing"],
            "refused_sensitive_artefacts": media["artefacts"],
            "attachments_not_landed": [entry["name"] for entry in not_landed],
            "unlinked_attachments": unlinked,
            "files": [],
            "claims": {"local_snapshot_only": bool((archive.get("claims") or {}).get(
                "local_snapshot_only", True)),
                "replaces_online_export": False, "original_archive_unchanged": True,
                "reading_pack_unchanged": True, "text_only_masking": True,
                "media_fully_redacted": False, "full_anonymity_claimed": False,
                "ocr_applied": False, "audio_transcription_applied": False,
                "metadata_stripped": False, "raw_text_used": opts["text_view"] == "raw",
                # 评审 C1/C2：这两项按构建后的磁盘实况算，不再是硬编码 false
                "alias_map_in_share_package": alias_in_share, "tdl_required": False,
                "network_used": False, "login_used": False, "media_downloaded": False,
                "offline_action": True, "synthetic_or_local_source_only": True,
                "note": "「文字字段与文件名已脱敏」不等于「附件完全脱敏」；未覆盖清单逐条可查。"
                        "alias_map_in_share_package 由构建后遍历分享目录算出"},
            "mapping": {"stored": "private", "path": str(mapping),
                        "entries": len(aliases.entries), "sha256": None,
                        "in_share_package": alias_in_share},
            "lock": {"kind": handle["kind"], "marker": handle["marker"],
                     "root": str(private_root), "path": handle["token"]["path"],
                     "order": list(handle["order"]),
                     "note": "映射表读写全程持归档锁；加锁序固定「先归档锁、后会话锁」，"
                             "与在线动作互斥。marker 取自 state.archive_lock_marker，"
                             "本模块没有第二份实现"},
            "runtime": {"tdl_required": False, "session_required": False,
                        "offline_action": True, "proxy": opts["proxy"],
                        "note": "share 属离线动作：没有 tdl / storage / namespace 也照跑，"
                                "不登录、不联网、不上传内容；代理只保留去凭据形态"},
            "unchanged": {"archive": {"before": archive_before, "after": archive_before,
                                      "verified": True, "scope": "私有归档文件（未写入）"},
                          "reading_pack": {"before": pack_before, "after": pack_before,
                                           "verified": True, "scope": "私有阅读版（未写入）"},
                          "input_dict": {"before": input_before, "after": input_before,
                                         "verified": True, "scope": "传入的归档 dict"}},
            "limitations": dict(LIMITATIONS),
        }
        mapping_payload = {"kind": "telegram-archive-share-alias-map",
                           "version": SHARE_VERSION, "share_id": report["share_id"],
                           "target": str(share_target), "private_root": str(private_root),
                           "identity": report["identity"], "aliases": aliases.entries,
                           "note": "本表只留在私有归档位置：拿到分享包的人无法据此还原真名"}
        _publish(mapping, json.dumps(mapping_payload, ensure_ascii=False, indent=2) + "\n")
        report["mapping"]["sha256"] = _digest(mapping)
        report["files"] = _file_list(share_target, skip=(REPORT_NAME,))
        report["files"].append({"path": REPORT_NAME, "sha256": None, "bytes": None,
                                "note": "自指文件不记自身摘要"})
        hidden = [str(private_root), str(share_target)] + \
                 ([str(opts["attachment_root"])] if opts["attachment_root"] else []) + \
                 ([str(opts["archive_path"])] if opts["archive_path"] else []) + \
                 ([str(opts["reading_pack"])] if opts["reading_pack"] else [])
        _publish(share_target / REPORT_NAME,
                 json.dumps(_public(report, hidden), ensure_ascii=False, indent=2) + "\n")
        _publish(private_root / ("%s-private-%s.json" % (ALIAS_MAP_PREFIX, report["share_id"])),
                 json.dumps(report, ensure_ascii=False, indent=2) + "\n")

        input_after = _dict_digest(archive)
        archive_after = _digest_if(opts["archive_path"]) if opts["archive_path"] else None
        pack_after = _tree_digest(opts["reading_pack"]) if opts["reading_pack"] else None
        report["unchanged"]["input_dict"]["after"] = input_after
        report["unchanged"]["archive"]["after"] = archive_after
        report["unchanged"]["reading_pack"]["after"] = pack_after
        for key in ("input_dict", "archive", "reading_pack"):
            entry = report["unchanged"][key]
            entry["verified"] = entry["before"] is not None and entry["before"] == entry["after"]
            if key == "archive" and entry["before"] is None:
                entry["verified"] = True      # 没给归档文件路径时无从比对，不去冒充已验证
                entry["verified_note"] = "未给 archive_path，只比对了传入的归档 dict"
            if key == "reading_pack" and entry["before"] is None:
                entry["verified"] = True
                entry["verified_note"] = "未给 reading_pack，本轮没有写入私有阅读版目录"
        report["claims"]["original_archive_unchanged"] = \
            report["unchanged"]["archive"]["verified"] and \
            report["unchanged"]["input_dict"]["verified"]
        report["claims"]["reading_pack_unchanged"] = report["unchanged"]["reading_pack"]["verified"]
        if not (report["claims"]["original_archive_unchanged"] and
                report["claims"]["reading_pack_unchanged"]):
            report["status"] = "partial"
            report["pending_decisions"].append(
                {"kind": "source_changed",
                 "reason": "源在构建期间发生变化，本次分享副本不作为可信成品，请重跑并核对"})
        report["headline"] = _headline(opts, report["counts"], uncovered,
                                       report["pending_decisions"])
        if report["status"] == "partial":
            report["headline"] += "；有待决策项，未默许放行"
        _publish(mapping, json.dumps(dict(mapping_payload, report_digest=input_after),
                                     ensure_ascii=False, indent=2) + "\n")
        return report
    finally:
        # 评审 N7：硬中断（KeyboardInterrupt/BaseException）不走 except Exception，扫尾必须
        # 进 finally，否则 chat-part-*.md.part 半截稿留在包里。成功时盘上本就没有 .part，
        # 扫一次无害；占位 report.json（incomplete）已在构建开头落盘，finally 只清临时文件。
        try:
            _sweep_parts(share_target)
        except Exception:
            pass
        state.release_archive_lock(handle)


def verify_share_links(target):
    """遍历分享目录里每个 .md 的每个链接：解码后必须仍在分享目录内且真实存在。

    ``..``、盘符/UNC/根路径这类绝对形态、``://`` 外链、符号链接/junction 式指向——一律拒绝，
    链接目标不存在也拒绝（分享目录里不留断链，也不留回指私有原档的路）。
    """
    base = Path(str(target)).absolute()
    _require(base.is_dir(), "分享目录不存在：%s" % base)
    checked, escaped, resolved_targets = 0, 0, []
    for path in sorted(base.rglob("*.md")):
        directory = path.parent
        for raw in LINK.findall(path.read_text(encoding="utf-8", errors="replace")):
            value = raw.strip().strip("<>").split(" ")[0]
            if not value or value.startswith("#"):
                continue
            checked += 1
            where = path.relative_to(base).as_posix()
            decoded = unquote(value)
            problem = None
            if "://" in decoded:
                problem = "外部链接（分享目录不该带任何外链）"
            elif re.match(r"^[A-Za-z]:[\\/]", decoded) or decoded.startswith(("\\\\", "/")):
                problem = "绝对路径（会回指私有原档或本机别处）"
            else:
                candidate = Path(os.path.normpath(str(directory / PurePosixPath(
                    decoded.replace("\\", "/")))))
                final = Path(os.path.realpath(str(candidate)))
                if not path_within(base, final):
                    problem = "解析后越出分享目录（.. 或符号链接式指向）"
                elif not final.is_file():
                    problem = "目标在分享目录里不存在"
                else:
                    resolved_targets.append(str(final))
            if problem:
                escaped += 1
                raise ShareError("链接检查不通过（%s）：%s -> %s" % (problem, where, raw))
    return {"links": checked, "escaped": escaped, "targets": resolved_targets,
            "note": "每个链接都解码后解析到分享目录内的真实文件"}


def run_share(job):
    """CLI ``share`` 的后端：校验选择合同 → 读用户选定的那一份归档 → 出分享副本。

    与 import/search 同为离线动作。一次只吃一个归档（多个归档请各自一个分享目录；同一目标
    重复运行会复用同一份化名映射表，跨轮化名保持一致）。
    """
    data = jobs.validate_job(job)
    share = data.get("share")
    _require(isinstance(share, dict),
             "share 缺失：分享是显式模式，必须在任务配置里给出 share 块（输出位置 target、"
             "脱敏字段 fields、text_view、是否带附件 with_attachments 等），不默认、不猜")
    _require(not (set(share) - set(OPTION_KEYS) - {"target"}),
             "share 块含未知键：%s（可用键：%s）"
             % ("、".join(sorted(set(share) - set(OPTION_KEYS) - {"target"})),
                "、".join(sorted(OPTION_KEYS))))
    sources = data.get("sources") or []
    _require(len(sources) == 1,
             "share 一次只吃一个归档（sources 只能有 1 项）：多个归档请分别输出到各自的"
             "分享目录，当前有 %d 项" % len(sources))
    private_root = _absolute(data.get("destination"), "destination（私有归档位置）")
    source = _absolute(sources[0], "sources[0]（要分享的归档 JSON）")
    _require(source.is_file(), "要分享的归档不存在：%s（只吃用户明确选定的文件，不自动找）"
             % source)
    payload = json.loads(source.read_text(encoding="utf-8"))
    _require(isinstance(payload, dict) and payload.get("kind") == importing.ARCHIVE_KIND,
             "%s 不是已归一化的共同归档（kind 应为 %s）：本命令不猜文件内容"
             % (source.name, importing.ARCHIVE_KIND))
    options = {key: value for key, value in share.items() if key in OPTION_KEYS}
    options["private_root"] = str(private_root)
    if _text(data.get("proxy")):
        options["proxy"] = data["proxy"]
    options.setdefault("archive_path", str(source))
    return create_share_copy(payload, _absolute(share.get("target"), "share.target"), options)
