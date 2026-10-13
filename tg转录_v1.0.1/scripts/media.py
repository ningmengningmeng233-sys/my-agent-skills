"""媒体分类、用户所选索引、下载组合与附件验收（Task 3）。

判据只来自原始元数据，且每一条都能指到 work/upstream 的 gotdgen 源码行号（见
CLASSIFICATION_SOURCES 与 references/selection-and-types.md）。文件名、扩展名、MIME、
标题一律不参与分类：动画与视频同为 mp4、语音与音频同为 ogg，靠后缀必然串类别。

后端旗据以 work/upstream/tdl-help-0.20.4.txt 的 `tdl dl --help` 段为唯一权威：
`-f/--file`（help:70，官方客户端导出文件）、`-d/--dir`（help:68）、全局 `-n/--ns`
（help:40）、`--storage`（help:45）、`--proxy`（help:43）。该段自己写明 `-i/--include`
与 `-e/--exclude`「only judge by file name, not file MIME」（help:69、help:73），所以本
模块只走「本地按 raw 元数据分类 → 生成用户所选消息索引 → tdl dl -f <索引> -d <目录>」，
绝不带 -i/-e；`--group` 会「auto detect grouped message and download all of them」
（help:71），与「媒体不自动把相邻消息或整组相册加入选择」合同冲突，绝不启用；
`--skip-same` 按同名同尺寸跳过（help:78），会让同名附件互相覆盖，也绝不启用；`--desc`
会颠倒顺序（help:67），与「固定索引与顺序以支持续传」冲突，同样不启用。

验收三步分开记录：候选索引生成 / 所选附件下载 / 附件验收。下载进程退出码 0 单独不构成
完成凭据：按「候选消息 ↔ 输出文件」一一映射核对存在性、已知大小与摘要。摘要来自上一轮本
地验收记录——raw 元数据里没有文件内容摘要可指认（document.go:181 的 AccessHash 是
「Check sum, dependent on document ID」，不是内容摘要）。大小未知项（照片：work/upstream
未收录 photo.go，没有 Size 字段可指认）明确标出依据较弱。缺失、失败、未知类型、不可访问
分别计数；未处理完所选范围（例如还需向更早翻页）绝不报告整体完成。

unknown 不是可默选类别：既不能进 selected 入参，也绝不进下载索引，而是明确上报，把
「包含 / 跳过 / 调整条件」的选择交回用户。旗标（voice/video/round）在 --raw JSON 里的
编码形状未核对，一律经有文档的谓词 `flag_is_set` 判定：True/1/"1"/"true" 算置位，
0/"false"/None/缺键算未置位，其它形状 = 歧义，整条消息落 unknown 并单独计数
（ambiguous_flag_ids），绝不猜成任何一个具体类别（评审 B-I1 / 总控裁决 I-1）。同一
peer 身份内的重复消息 ID 在计数前按「peer + 消息 ID」去重（duplicated_ids 申报），
配额只被唯一消息消耗，跨 peer 相同 ID 不互相抵消（shared-facts 裁决 1 / 评审 B-I2）。

退出码归口（shared-facts 第 21 行 / 评审 B-I3 / 总控裁决 I-3）：能力或依赖缺口
（含后端起不来）→ unavailable → 3；运行中途 OSError → failed → 1，且照常写出 media
里程碑与补漏清单；启动前的选择/配置非法 → ValueError → 2（语义不变）。

证据边界：本模块只做本地分类、调度与验收。假后端通过只证明 argv 与索引正确，**不证明**
真实媒体下载成功；真实下载与真实 `tdl --raw` JSON 拼写在获得具体授权前均为 NOT_RUN，
对应能力以 insufficient_source 上报（见 CAPABILITIES）。

依赖：Python 标准库 + 同目录 job.py / state.py。不联网、不登录、不自行执行 tdl（只按调用
方配置做参数列表子进程调度）。
"""

import copy
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path, PurePath

import state
from job import ATTACHMENT_TYPES, chat_identity, validate_job

CATEGORIES = ("image", "video", "audio", "voice", "animation", "sticker", "document",
              "other")
UNRESOLVED = "unknown"
DOWNLOADABLE_CATEGORIES = ("image", "video", "audio", "voice", "animation", "sticker",
                           "document")
SELECTION_KIND = "telegram-archive-media-selection"
INDEX_KIND = "telegram-archive-media-index"
REPORT_KIND = "telegram-archive-media-run"
TASK_REPORT_KIND = "telegram-archive-media-task"
INDEX_VERSION = 1
MEDIA_SUBDIR = state.MEDIA_SUBDIR

# 本机 tdl 0.20.4 的默认文件名模板原文（help:80）；MessageID 在内，同名附件因此不会互相
# 覆盖，也是「附件 ↔ 消息」映射的验收锚点。
TDL_DEFAULT_TEMPLATE = "{{ .DialogID }}_{{ .MessageID }}_{{ filenamify .FileName }}"

_MESSAGE_ID_IN_NAME = re.compile(r"^(?:\d+_)?(\d+)(?:_|$)")
_TAG_KEYS = ("_", "_type", "type", "typename", "@type", "_class", "__type", "tl_type")
_ENCODING_FIELD = "flags"          # gotd 结构体都带 Flags，是编码字段，不参与判定

# 参与分类的原始字段（决定类别只用这些；后缀/MIME/标题不在其中）。
CLASSIFICATION_INPUT_FIELDS = ("media", "document", "attributes", "animated", "sticker",
                               "video", "audio", "voice", "round", "has_stickers",
                               "custom_emoji")


def _norm(value):
    """归一化名字：documentAttributeVideo 与 DocumentAttributeVideo 得到同一个键。"""
    return re.sub(r"[^0-9a-z]", "", str(value).lower())


def _table(mapping):
    return {_norm(name): value for name, value in mapping.items()}


def _field_table(mapping):
    return {_norm(name): frozenset(_norm(item) for item in fields)
            for name, fields in mapping.items()}


# ---------------------------------------------------------------- 分类判据（可指认）

# TL 构造器名 -> 类别。"container" 表示要继续看内层 document 的属性才能定类别。
_MEDIA_TYPES = {
    "messageMediaEmpty": "other",              # media.go:35 Empty constructor
    "messageMediaPhoto": "image",              # media.go:137 Attached photo
    "messageMediaDocument": "container",       # media.go:887 video/audio/voice/sticker...
    "messageMediaUnsupported": UNRESOLVED,     # media.go:784 客户端解不出，不猜
    "messageMediaPaidMedia": UNRESOLVED,       # media.go:4778 内层 ExtendedMedia 未收录
    "messageMediaGeo": "other",
    "messageMediaContact": "other",
    "messageMediaVenue": "other",
    "messageMediaGame": "other",
    "messageMediaInvoice": "other",
    "messageMediaGeoLive": "other",
    "messageMediaWebPage": "other",
    "messageMediaPoll": "other",
    "messageMediaDice": "other",
    "messageMediaStory": "other",
    "messageMediaGiveaway": "other",
    "messageMediaGiveawayResults": "other",
    "messageMediaToDo": "other",
    "messageMediaVideoStream": "other",        # media.go:5181 直播流，不是文件
}
_MESSAGE_MEDIA = _table(_MEDIA_TYPES)

# 无构造器名时的结构指纹：字段集合取自各构造器 TypeInfo 的 SchemaName 行。
_MEDIA_FIELDS = _field_table({
    "messageMediaPhoto": ("spoiler", "photo", "ttl_seconds"),                  # 245-255
    "messageMediaDocument": ("nopremium", "spoiler", "video", "round", "voice",
                             "document", "alt_documents", "video_cover",
                             "video_timestamp", "ttl_seconds"),                # 1060-1105
    "messageMediaWebPage": ("force_large_media", "force_small_media", "manual",
                            "safe", "webpage"),                                 # 1586-1606
    "messageMediaGeo": ("geo",),                                                # 478
    "messageMediaContact": ("phone_number", "first_name", "last_name", "vcard",
                            "user_id"),                                         # 646-662
    "messageMediaVenue": ("geo", "title", "address", "provider", "venue_id",
                          "venue_type"),                                        # 1887-1907
    "messageMediaGame": ("game",),                                              # 2127
    "messageMediaInvoice": ("shipping_address_requested", "test", "title",
                            "description", "photo", "receipt_msg_id", "currency",
                            "total_amount", "start_param"),                     # 2365-2401
    "messageMediaPoll": ("poll", "results"),                                    # 3068-3072
    "messageMediaDice": ("value", "emoticon", "game_outcome"),                  # 3253-3261
    "messageMediaVideoStream": ("rtmp_stream", "call"),                         # 5266-5271
    "messageMediaPaidMedia": ("stars_amount", "extended_media"),                # 4859-4863
})

_DOCUMENT_TYPES = _table({"document": "container", "documentEmpty": UNRESOLVED})
_DOCUMENT_FIELDS = _field_table({
    "document": ("id", "access_hash", "file_reference", "date", "mime_type", "size",
                 "thumbs", "video_thumbs", "dc_id", "attributes"),              # 330-368
    "documentempty": ("id",),                                                   # 111
})

# 属性 -> 类别；音频再由 voice 旗标分「语音 / 普通音频」；filename、imagesize 不是类别标志。
_ATTRIBUTE_TYPES = _table({
    "documentAttributeAnimated": "animation",          # attributes.go:197 Defines an animated GIF
    "documentAttributeSticker": "sticker",             # attributes.go:299 Defines a sticker
    "documentAttributeHasStickers": "sticker",         # attributes.go:1507（推断，见下）
    "documentAttributeVideo": "video",                 # attributes.go:570 Defines a video
    "documentAttributeAudio": "audio",                 # attributes.go:1033 audio file
    "documentAttributeFilename": None,                 # attributes.go:1372 普通文档标志
    "documentAttributeImageSize": None,
    "documentAttributeCustomEmoji": UNRESOLVED,        # attributes.go:1609 九类里没有它
})
_ATTRIBUTE_FIELDS = _field_table({
    "documentAttributeImageSize": ("w", "h"),                                  # 118-122
    "documentAttributeAnimated": (),                                           # 258 无字段
    "documentAttributeSticker": ("mask", "alt", "stickerset", "mask_coords"),  # 409-422
    "documentAttributeVideo": ("round_message", "supports_streaming", "nosound",
                               "duration", "w", "h", "preload_prefix_size",
                               "video_start_ts", "video_codec"),                # 729-766
    "documentAttributeAudio": ("voice", "duration", "title", "performer",
                              "waveform"),                                     # 1164-1183
    "documentAttributeFilename": ("file_name",),                               # 1448
    "documentAttributeHasStickers": (),                                        # 1568 无字段
    "documentAttributeCustomEmoji": ("free", "text_color", "alt", "stickerset"),
})                                                                       # 1716-1730

_FINGERPRINTS = {"media": _MEDIA_FIELDS, "document": _DOCUMENT_FIELDS,
                "attribute": _ATTRIBUTE_FIELDS}

# ---------------------------------------------------------------- 出处对照（可指认）

CLASSIFICATION_SOURCES = (
    {"category": "image", "file": "media.go", "line": 137, "contains": "// Attached photo."},
    {"category": "image", "file": "media.go", "line": 140,
     "contains": "type MessageMediaPhoto struct {"},
    {"category": "image", "file": "media.go", "line": 229,
     "contains": 'return "messageMediaPhoto"'},
    {"category": "image", "file": "media.go", "line": 250, "contains": 'SchemaName: "photo"'},
    {"category": "video", "file": "media.go", "line": 887,
     "contains": "// Document (video, audio, voice, sticker, any media type except photo)"},
    {"category": "video", "file": "media.go", "line": 890,
     "contains": "type MessageMediaDocument struct {"},
    {"category": "video", "file": "attributes.go", "line": 570,
     "contains": "// Defines a video"},
    {"category": "video", "file": "attributes.go", "line": 573,
     "contains": "type DocumentAttributeVideo struct {"},
    {"category": "video", "file": "attributes.go", "line": 713,
     "contains": 'return "documentAttributeVideo"'},
    {"category": "video", "file": "attributes.go", "line": 744,
     "contains": 'SchemaName: "duration"'},
    {"category": "video", "file": "media.go", "line": 901,
     "contains": "// Whether this is a video."},
    {"category": "audio", "file": "attributes.go", "line": 1033,
     "contains": "// Represents an audio file"},
    {"category": "audio", "file": "attributes.go", "line": 1036,
     "contains": "type DocumentAttributeAudio struct {"},
    {"category": "audio", "file": "attributes.go", "line": 1148,
     "contains": 'return "documentAttributeAudio"'},
    {"category": "voice", "file": "attributes.go", "line": 1042,
     "contains": "// Whether this is a voice message"},
    {"category": "voice", "file": "attributes.go", "line": 1164,
     "contains": 'SchemaName: "voice"'},
    {"category": "voice", "file": "attributes.go", "line": 1258,
     "contains": "d.Voice = d.Flags.Has(10)"},
    {"category": "voice", "file": "media.go", "line": 905,
     "contains": "// Whether this is a voice message."},
    {"category": "animation", "file": "attributes.go", "line": 197,
     "contains": "// Defines an animated GIF"},
    {"category": "animation", "file": "attributes.go", "line": 200,
     "contains": "type DocumentAttributeAnimated struct {"},
    {"category": "animation", "file": "attributes.go", "line": 245,
     "contains": 'return "documentAttributeAnimated"'},
    {"category": "animation", "file": "attributes.go", "line": 258,
     "contains": "typ.Fields = []tdp.Field{}"},
    {"category": "sticker", "file": "attributes.go", "line": 299,
     "contains": "// Defines a sticker"},
    {"category": "sticker", "file": "attributes.go", "line": 302,
     "contains": "type DocumentAttributeSticker struct {"},
    {"category": "sticker", "file": "attributes.go", "line": 393,
     "contains": 'return "documentAttributeSticker"'},
    {"category": "sticker", "file": "attributes.go", "line": 409,
     "contains": 'SchemaName: "mask"'},
    {"category": "sticker", "file": "attributes.go", "line": 1507,
     "contains": "// Whether the current document has stickers attached"},
    {"category": "document", "file": "attributes.go", "line": 1372,
     "contains": "// A simple document with a file name"},
    {"category": "document", "file": "attributes.go", "line": 1375,
     "contains": "type DocumentAttributeFilename struct {"},
    {"category": "document", "file": "attributes.go", "line": 1432,
     "contains": 'return "documentAttributeFilename"'},
    {"category": "document", "file": "document.go", "line": 169,
     "contains": "// Document represents TL type"},
    {"category": "document", "file": "document.go", "line": 350,
     "contains": 'SchemaName: "size"'},
    {"category": "other", "file": "media.go", "line": 35, "contains": "// Empty constructor."},
    {"category": "other", "file": "media.go", "line": 38,
     "contains": "type MessageMediaEmpty struct {"},
    {"category": "other", "file": "media.go", "line": 5373,
     "contains": "// MessageMediaClass represents MessageMedia generic type."},
    {"category": UNRESOLVED, "file": "media.go", "line": 784,
     "contains": "// MessageMediaUnsupported represents TL type"},
    {"category": UNRESOLVED, "file": "media.go", "line": 788,
     "contains": "type MessageMediaUnsupported struct {"},
    {"category": UNRESOLVED, "file": "media.go", "line": 4778,
     "contains": "// Either the paid-for media, or super low resolution media previews if the media wasn't"},
    {"category": UNRESOLVED, "file": "attributes.go", "line": 1609,
     "contains": "// Info about a custom emoji"},
    {"category": UNRESOLVED, "file": "document.go", "line": 181,
     "contains": "// Check sum, dependent on document ID"},
)

# 后端旗标出处（同一行核对机制，见 BACKEND_FLAG_SOURCES 的 contains）。
BACKEND_FLAG_SOURCES = (
    {"category": "dl", "file": "tdl-help-0.20.4.txt", "line": 60,
     "contains": "tdl download [flags]"},
    {"category": "dl", "file": "tdl-help-0.20.4.txt", "line": 63, "contains": "download, dl"},
    {"category": "file", "file": "tdl-help-0.20.4.txt", "line": 70,
     "contains": "-f, --file strings      official client exported files"},
    {"category": "dir", "file": "tdl-help-0.20.4.txt", "line": 68,
     "contains": "-d, --dir string"},
    {"category": "include", "file": "tdl-help-0.20.4.txt", "line": 73,
     "contains": "only judge by file name, not file MIME"},
    {"category": "exclude", "file": "tdl-help-0.20.4.txt", "line": 69,
     "contains": "only judge by file name, not file MIME"},
    {"category": "group", "file": "tdl-help-0.20.4.txt", "line": 71,
     "contains": "--group             auto detect grouped message and download all of them"},
    {"category": "skip-same", "file": "tdl-help-0.20.4.txt", "line": 78,
     "contains": "--skip-same         skip files with the same name(without extension) and size"},
    {"category": "template", "file": "tdl-help-0.20.4.txt", "line": 80,
     "contains": "{{ .DialogID }}_{{ .MessageID }}_{{ filenamify .FileName }}"},
    {"category": "desc", "file": "tdl-help-0.20.4.txt", "line": 67,
     "contains": "--desc              download files from the newest to the oldest ones"},
    {"category": "namespace", "file": "tdl-help-0.20.4.txt", "line": 40,
     "contains": "-n, --ns string"},
    {"category": "storage", "file": "tdl-help-0.20.4.txt", "line": 45,
     "contains": "--storage stringToString"},
    {"category": "storage", "file": "tdl-help-0.20.4.txt", "line": 45,
     "contains": "format: type=driver,key1=value1,key2=value2"},
    {"category": "proxy", "file": "tdl-help-0.20.4.txt", "line": 43,
     "contains": "--proxy string"},
)

# 已验证事实 vs 合理推断：以下判定只能算推断，必须在 references 里点名。
INFERRED_JUDGEMENTS = {
    "documentAttributeHasStickers":
        "sticker 推断：attributes.go:1507 只说「the current document has stickers "
        "attached」，把它当贴纸族文档是推断，不是原文直述",
    "messageMediaDocument.video":
        "video 推断：document 属性缺失时退回消息级旗标 media.go:901「Whether this is a "
        "video」，属第二判据",
    "messageMediaDocument.round":
        "video 推断：media.go:903「round video」按视频处理",
    "messageMediaDocument.voice":
        "voice 推断：media.go:905「Whether this is a voice message」作第二判据",
    "raw JSON 类型拼写":
        "work/upstream 只含 gotdgen 类型定义（TypeName()/SchemaName），未收录 tdl 自身的 "
        "JSON 编码器，因此同时接受 TL 构造器名与 Go 字段名两种拼写；真实 --raw 拼写未核对",
}

CAPABILITIES = (
    {"capability": "tdl_dl_export_file_schema", "status": "insufficient_source",
     "reason": "help:70 只说 -f/--file 接受「official client exported files」，没说明本地"
              "按用户所选类别过滤后的索引文件是否被同样接受（是否需要 tdl chat export 的完整"
              "容器字段）。未做真实下载核对，索引按原导出形状保留消息并只删非定位字段"
              "（嵌套形状的 raw.Media 会提升到投影顶层，raw 包裹层不保留；见 _project）。"
              "受影响动作：download。"},
    {"capability": "raw_json_type_names", "status": "insufficient_source",
     "reason": "work/upstream 只收录 gotdgen 类型定义（media.go/document.go/attributes.go 的 "
              "TypeName()/SchemaName），未收录 tdl 自身的 JSON 编码器，真实 `tdl chat export "
              "--raw` 的键名与类型判别字段拼写未核对；分类同时接受 TL 构造器名与 Go 字段名两"
              "种拼写，认不出的一律 unknown，不猜。受影响动作：download。"},
    {"capability": "tdl_storage_option_mapping", "status": "available",
     "reason": "help:45 自证 --storage 的格式「type=driver,key1=value1,key2=value2」与驱动"
              "清单 [legacy,bolt,file]，且本机默认值就是 [path=...,type=bolt]；"
              "`type=bolt,path=...` 因此是**已文档化的拼法**，不是推断（评审 B-M5 据实上修）。"
              "真实连接行为仍未核对（NOT_RUN），旗标形状本身已核对。受影响动作：download。"},
    {"capability": "media_content_digest_source", "status": "insufficient_source",
     "reason": "raw 元数据不含附件内容摘要（document.go:181 的 AccessHash 是「Check sum, "
              "dependent on document ID」，不是内容摘要），摘要核对只能与上一轮本地验收记录比，"
              "不能证明与 Telegram 上的原件一致。受影响动作：download。"},
    {"capability": "id_range_endpoint_semantics", "status": "insufficient_source",
     "reason": state.ENDPOINT_NOTE + " 媒体侧同样只在已冻结上界内工作，不重新探测。"},
)

_UNKNOWN_CHOICES = ("包含（改成明确类别后重跑）", "跳过（保持当前选择）",
                   "调整条件（缩小范围或换类别）")


# ---------------------------------------------------------------------- 分类实现

def _keys(node):
    """节点上参与结构判定的键（去掉编码用 Flags 与类型名判别键）。"""
    tag_keys = {_norm(name) for name in _TAG_KEYS}
    return {key for key in (_norm(name) for name in node)
            if key and key != _ENCODING_FIELD and key not in tag_keys}


def _tag_of(node):
    for name in _TAG_KEYS:
        value = node.get(name)
        if isinstance(value, str) and value.strip():
            return _norm(value)
    return None


def _structural_name(node, kind, names):
    keys = _keys(node)
    if not keys:
        # 空字段对象（如 Animated 与 HasStickers 都无 TL 字段）无法区分，一律不猜。
        return None
    table = _FINGERPRINTS[kind]
    hits = [name for name in names if keys <= table.get(name, frozenset())]
    if len(hits) == 1:
        return hits[0]
    if kind == "attribute" and len(hits) > 1:
        # 无标签裸结构（真实 `tdl --raw`：大写键、无构造器标签）的并列消解。
        # 只动无标签路径：有标签形状走 _tag_of，本函数碰不到，行为原样保留。
        resolved = _untagged_attribute_tiebreak(keys, hits, table)
        if resolved is not None:
            return resolved
    return None


def _untagged_attribute_tiebreak(keys, hits, table):
    """无标签属性节点出现多个结构指纹并列时的消解（Task 6b 真实反例，Task 6c(1) 修正）。

    只保留一条：精确形状优先。节点键集与某候选指纹完全相等时取它。{W,H} 恰为
    DocumentAttributeImageSize 的全部 TL 字段（attributes.go:118-122），
    它同时落在 video 指纹里只是子集判定的副产品；ImageSize 本身不决定类别
    （_ATTRIBUTE_TYPES 里是 None），只阻止中性节点毒化整条判定。

    Task 6c(1) 删掉的规则（曾为 6b 的“贴纸载荷优先”）：键集恰为 {alt,stickerset}
    时按 sticker 计。该规则不成立，已验证：sticker 的 mask/mask_coords 与
    custom-emoji 的 free/text_color 都是 TL 条件字段（attributes.go:409-422
    与 1716-1730 的 TypeInfo 均标 Null: !Flags.Has(…)；两边的 SetFlags/EncodeBare
    只在字段非零时置位），全缺时两边的序列化形状完全重合为 {flags,alt,stickerset}，
    而 flags 本是编码字段（本模块 _ENCODING_FIELD，不参与判定），且即便可见，
    位含义也随类型而变（bit0 = mask_coords 或 free），不用类型解不开类型，属循环。
    真实 raw 里这些判别键的存在性依据也没有（本任务可用的已验证真实观测只有
    6b 的那条贴纸样本，它恰恰缺了全部四个键；upstream 只证明 TL 可能，
    不证明 raw 拼写，见 CAPABILITIES.raw_json_type_names）。
    所以精确两键形状无法唯一判定，一律返回 None → 上游照常落 unknown
    （工单 Task 6：不把 unknown 强制归类）。带判别键的形状不受影响：
    它们走单命中路径（_structural_name 的 len(hits)==1），本来就到不了这里。
    FileName/MimeType 全程不参与本判定。
    两条都不命中（{Alt} 单键、{Alt,Stickerset} 精确两键、空节点等）仍返回 None；
    旗标歧义、Unsupported/Paid（media 层）与有标签形状一律不受影响。
    """
    exact = [name for name in hits if keys == table.get(name, frozenset())]
    if len(exact) == 1:
        return exact[0]
    return None


def _resolve(value, kind, mapping):
    """按构造器名解析，其次按结构指纹；解析不出来返回 (None, 节点)。"""
    if not isinstance(value, dict):
        return None, None
    tag = _tag_of(value)
    if tag is not None:
        return (tag if tag in mapping else None), value
    return _structural_name(value, kind, list(mapping)), value


def _child(node, wanted):
    for name, value in (node or {}).items():
        if _norm(name) == wanted:
            return value
    return None


def flag_is_set(value):
    """有文档的判据谓词：判定 TL 旗标在 --raw JSON 里的编码形状是否算「置位」。

    - 置位：``True``、``1``、``"1"``，以及字符串 ``"true"``（去首尾空白、大小写不敏感）。
    - 未置位：``False``、``0``、``"0"``、字符串 ``"false"``、``None``，以及**缺键**。
    - 其它任何类型或取值（``{}``、``2``、``"yes"``、``1.5``……）：返回 ``None``，即**歧义**。

    旗标在 TL 里是位旗标展开（media.go:1225 ``m.Voice = m.Flags.Has(8)``、
    attributes.go:1258 ``d.Voice = d.Flags.Has(10)``），而 ``tdl --raw`` 的真实 JSON 编码
    形状未核对（CAPABILITIES.raw_json_type_names = insufficient_source）。把非 bool 的
    形状硬判成 False 会让语音串进普通音频（评审 B-I1 的实测事故）；硬猜成某个类别同样
    危险。因此歧义时调用方必须把整条消息落 unknown 并单独计数上报，交回用户决定，
    绝不猜成任何一个具体类别（总控裁决 I-1）。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, int):
        return bool(value) if value in (0, 1) else None
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ("1", "true"):
            return True
        if token in ("0", "false"):
            return False
    return None


def _flag_state(node, wanted):
    """True/False = 已判定；None = 旗标存在但形状不可判（歧义）。缺键算未置位。"""
    if isinstance(node, dict):
        for name, value in node.items():
            if _norm(name) == wanted:
                return flag_is_set(value)
    return False


def _attribute_markers(attributes):
    """attributes 列表折成 (类别标志, 是否含无法判定项, 是否有文件名属性, 歧义旗标清单)。"""
    if attributes is None:
        return [], False, False, []
    if not isinstance(attributes, list):
        return [], True, False, []
    categories, blocked, has_file_name, ambiguous = [], False, False, []
    for item in attributes:
        name, _node = _resolve(item, "attribute", _ATTRIBUTE_TYPES)
        if name is None:
            blocked = True                 # 认不出的属性不能当作「没有类别」
            continue
        category = _ATTRIBUTE_TYPES[name]
        if category == UNRESOLVED:
            blocked = True                 # 自定义 emoji 等九类里没有它
            continue
        if category is None:
            has_file_name = has_file_name or name == _norm("documentAttributeFilename")
            continue
        if name == _norm("documentAttributeAudio"):
            voice = _flag_state(item, "voice")
            if voice is None:
                ambiguous.append("documentAttributeAudio.voice")
                continue
            categories.append("voice" if voice else "audio")
        else:
            categories.append(category)
    return categories, blocked, has_file_name, ambiguous


def _document_detail(media_node):
    """文档容器的 (类别, 歧义旗标)。属性旗标优先；消息级旗标只做第二判据，
    置位形态（1/"true"/True）不改变属性已定的类别，歧义形状则整条落 unknown。"""
    ambiguous = [name for name in ("voice", "video", "round")
                 if _flag_state(media_node, name) is None]
    document_node = _child(media_node, "document")
    name, _node = _resolve(document_node, "document", _DOCUMENT_TYPES)
    if name is None or _DOCUMENT_TYPES.get(name) != "container":
        return UNRESOLVED, ambiguous          # 没有 document / documentEmpty / 认不出：不猜
    categories, blocked, has_file_name, attr_ambiguous = _attribute_markers(
        _child(document_node, "attributes"))
    ambiguous = ambiguous + attr_ambiguous
    if blocked or ambiguous:
        return UNRESOLVED, ambiguous
    distinct = set(categories)
    if len(distinct) > 1:
        return UNRESOLVED, ambiguous          # 属性互相矛盾（如 Animated + Video）
    if distinct:
        return next(iter(distinct)), ambiguous
    if _flag_state(media_node, "voice"):
        return "voice", ambiguous
    if _flag_state(media_node, "video") or _flag_state(media_node, "round"):
        return "video", ambiguous
    if has_file_name:
        return "document", ambiguous
    return UNRESOLVED, ambiguous


def _media_node(message):
    if not isinstance(message, dict):
        return None
    for name, value in message.items():
        if _norm(name) == "media" and isinstance(value, dict):
            return value
    # 真实 `tdl --raw` 把 MTProto 结构包在 message["raw"] 下（顶层键为
    # date/file/id/raw/text/type）：顶层缺失时仅下降一层，只读 raw.Media。
    # raw 缺失或非映射时行为与原来一致（返回 None，上游照常落 unknown）。
    raw = None
    for name, value in message.items():
        if _norm(name) == "raw" and isinstance(value, dict):
            raw = value
            break
    if raw is not None:
        for name, value in raw.items():
            if _norm(name) == "media" and isinstance(value, dict):
                return value
    return None


def _classify_detail(message):
    """内部判定：返回 (类别, 歧义旗标清单)。公开合同只看类别，计数层需要旗标去向。"""
    if not isinstance(message, dict):
        return UNRESOLVED, []
    node = _media_node(message)
    if node is None:
        tag = _tag_of(message)
        if tag not in _MESSAGE_MEDIA:
            return UNRESOLVED, []
        node, category = message, tag
    else:
        category, node = _resolve(node, "media", _MESSAGE_MEDIA)
    if category is None:
        return UNRESOLVED, []
    resolved = _MESSAGE_MEDIA.get(category)
    if resolved == "container":
        return _document_detail(node)
    return resolved or UNRESOLVED, []


def classify(message):
    """按 raw 元数据判定媒体类别；判不出来（含旗标形状歧义）一律 unknown。"""
    return _classify_detail(message)[0]


# ---------------------------------------------------------------- 用户所选索引

def _validated_selection(selected):
    if isinstance(selected, (str, bytes)) or not isinstance(selected, (list, tuple)):
        raise ValueError("selected 必须是类别名列表（不按后缀、不按 MIME 选择）")
    if not selected:
        raise ValueError("selected 不能为空：没有用户所选类别就不生成下载索引")
    items = list(selected)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ValueError("selected 每项必须是非空字符串类别名")
    lowered = [item.strip().lower() for item in items]
    if any(item != exact for item, exact in zip(lowered, items)):
        raise ValueError("selected 类别名区分大小写，须逐字用合同里的类别名："
                         + "、".join(sorted({exact for item, exact in zip(lowered, items)
                                            if item != exact})))
    if len(set(items)) != len(items):
        raise ValueError("selected 含重复类别")
    bad = sorted({item for item in lowered if item not in CATEGORIES})
    if bad:
        if UNRESOLVED in bad:
            raise ValueError("unknown 不是可默选类别：无法判定的类型须交回用户决定")
        if "text" in bad:
            raise ValueError("text 不是附件类别：download 只下载用户所选附件")
        raise ValueError("selected 含不支持的类别：" + "、".join(bad)
                        + "（可选：" + "、".join(CATEGORIES) + "）")
    return lowered


def _payload_messages(data):
    if isinstance(data, list):
        return list(data)
    if isinstance(data, dict):
        for name in ("messages", "Messages"):
            value = data.get(name)
            if isinstance(value, list):
                return list(value)
        raise ValueError("导出载荷需要 messages 数组（不臆造 tdl 导出 schema）")
    raise ValueError("导出载荷必须是消息列表或含 messages 数组的映射")


def _payload_identity(data):
    if not isinstance(data, dict):
        return None
    value = data.get("identity")
    if isinstance(value, str) and value.strip():
        return value
    chat = data.get("chat")
    if isinstance(chat, dict):
        try:
            return chat_identity(chat)
        except ValueError:
            return None
    return None


def _scope(data):
    """(下界, 冻结上界, n, count_unit, history_complete)：只取本次已冻结的值。"""
    scope = data.get("range")
    snapshot = data.get("snapshot")
    upper = snapshot.get("upper_bound") if isinstance(snapshot, dict) else None
    mode, lower, n, unit = None, None, None, None
    if scope is not None:
        if not isinstance(scope, dict):
            raise ValueError("range 必须是映射（范围由用户选择，不现场探测）")
        mode = scope.get("mode")
        if mode not in ("all", "time", "id", "last"):
            raise ValueError("range.mode 取值不支持（仅 all/time/id/last）")
        if mode == "id":
            lower = scope.get("lower")
            if upper is None:
                upper = scope.get("upper")
        elif mode == "last":
            n, unit = scope.get("n"), scope.get("count_unit")
            if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                raise ValueError("range.n 必须是正整数（不猜默认条数）")
            if unit not in ("messages", "matching_messages"):
                raise ValueError("range.count_unit 必须是 messages 或 matching_messages"
                                "（两者计数对象不同）")
    declared = data.get("history_complete")
    complete = bool(declared) if declared is not None else mode in ("all", "id")
    return {"mode": mode, "lower": lower, "upper": upper, "n": n, "unit": unit,
            "history_complete": bool(complete)}


def _message_id(item):
    for name, value in item.items():
        if _norm(name) in ("id", "messageid"):
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
            return None
    return None


def _project(message):
    """索引只保留下载定位所需字段，正文一律不写入（内部索引 ≠ 全文阅读文档）。

    下游合同（Task 6c(3) 追踪锁定）：本函数的唯一调用方是 write_download_index，
    产出的 messages 数组随索引文件交给 ``tdl dl -f``（download_argv 只传文件
    路径）；仓内没有第二处程序化消费。合同字段 = 身份（payload 级
    identity/chat_id，不在本函数）＋消息 ID（_message_id 消费：id/messageid）
    ＋media 定位（_media_node/_record 重推导消费）＋chat 定位
    （chatid/dialogid/peerid）＋date。正文/file/type 一律不写入。

    嵌套形状（Media 包在 message["raw"] 下）时，media 本身是定位字段：按
    “只删非定位字段”的约定把它提升到投影顶层；raw 包裹层本身不保留
    （不盲目保留整个 raw）。旧顶层形状（Media 直接在顶层）原样保留原键拼写。
    tdl 侧 -f 是否接受该索引形状仍是 insufficient_source（help:70 一句话，
    未做真实下载核对），本函数只保证仓内合同，不承诺后端接受度。
    """
    projected = {name: value for name, value in message.items()
                 if _norm(name) in ("id", "messageid", "media", "chatid", "dialogid",
                                   "peerid", "date")}
    if _child(projected, "media") is None and isinstance(message, dict):
        raw = _child(message, "raw")
        promoted = _child(raw, "media") if isinstance(raw, dict) else None
        if isinstance(promoted, dict):
            projected["media"] = promoted
    return projected


def _trail(media_node):
    """这次判定用到的构造器名（供 references 对账；不含文件名/MIME 依据）。"""
    if not isinstance(media_node, dict):
        return []
    trail = [_tag_of(media_node) or _structural_name(media_node, "media",
                                                    list(_MESSAGE_MEDIA))]
    document_node = _child(media_node, "document")
    for item in (_child(document_node, "attributes") or []):
        if isinstance(item, dict):
            trail.append(_tag_of(item) or _structural_name(item, "attribute",
                                                          list(_ATTRIBUTE_TYPES)))
    return [name for name in trail if name]


def _record(item, message_id, category):
    media_node = _media_node(item)
    document_node = _child(media_node, "document")
    size, basis, file_name = None, "unavailable:no_size_field", None
    if isinstance(document_node, dict):
        raw_size = _child(document_node, "size")
        if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size >= 0:
            size, basis = raw_size, "document.size"
        for attribute in (_child(document_node, "attributes") or []):
            if isinstance(attribute, dict):
                name = _child(attribute, "filename")
                if isinstance(name, str) and name:
                    file_name = name
    elif isinstance(media_node, dict) and (_tag_of(media_node)
                                          == _norm("messageMediaPhoto")):
        # work/upstream 未收录 photo.go，照片没有可指认的 Size 字段：大小未知、依据较弱。
        basis = "unavailable:photo.size_not_captured"
    downloadable = category in DOWNLOADABLE_CATEGORIES
    return {"message_id": message_id, "category": category,
            "downloadable": downloadable, "file_name": file_name,
            "expected_size": size, "size_basis": basis,
            "weak_basis": bool(downloadable and size is None),
            "document_id": _child(document_node, "id") if isinstance(document_node, dict)
            else None,
            "has_access_hash": isinstance(document_node, dict)
            and _child(document_node, "access_hash") is not None,
            "classification": _trail(media_node),
            "_message": item}


def _tally(counts, category):
    counts[category] = counts.get(category, 0) + 1


def _item_peer(item, fallback):
    """消息的合同身份（peer 侧）：优先消息自带的 peer 定位字段，其次载荷声明的身份。

    去重键 = peer 身份 + 消息 ID（shared-facts 裁决 1：跨 peer 的相同 ID 不互相抵消）。
    """
    for wanted in ("chatid", "peerid", "dialogid"):
        value = _child(item, wanted)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, str)):
            return "peer:%s:%s" % (wanted, value)
        if isinstance(value, dict):
            for inner in ("id", "channelid", "userid", "chatid"):
                hit = _child(value, inner)
                if isinstance(hit, (int, str)) and not isinstance(hit, bool):
                    return "peer:%s:%s" % (wanted, hit)
            return "peer:%s:opaque" % wanted
    return fallback


def select_media(data, selected):
    """返回（用户所选类别的索引载荷, unknown 消息 ID 清单）。

    未选类别绝不进入结果；unknown 绝不进入下载载荷。已确认无附件的纯文字
    （顶层与 raw 里都没有 media 键：Task 6c(2)）另计 text_ids 并在 counts 里记
    "text"，不进 unknown_ids——unknown 只点名“有媒体结构但解不出”的消息。
    同一 peer 身份内的重复消息 ID
    在计数**之前**去重（配额只被唯一消息消耗；候选索引、下载映射、验收计数三处都用
    去重后的集合），跨 peer 相同 ID 不互相抵消；旗标形状歧义落 unknown 并单独计数
    （ambiguous_flag_ids）。range.mode=last 按**固定上界由新到旧**扫描：
    count_unit=matching_messages 只在类型确认后计数，不够就申报「仍需向更早历史翻页」
    （绝不重新探测上界）；count_unit=messages 先取最近 n 条消息再在其中筛类别。
    """
    categories = _validated_selection(selected)
    messages = _payload_messages(data)
    container = data if isinstance(data, dict) else {}
    scope = _scope(container)
    ordered, unidentified, duplicated = [], 0, 0
    fallback_peer = _payload_identity(container)
    seen_peers = set()
    for item in messages:
        if not isinstance(item, dict):
            unidentified += 1
            continue
        message_id = _message_id(item)
        if message_id is None:
            unidentified += 1
            continue
        key = (_item_peer(item, fallback_peer), message_id)
        if key in seen_peers:
            duplicated += 1
            continue
        seen_peers.add(key)
        ordered.append((message_id, item))
    ordered.sort(key=lambda pair: pair[0])
    upper, lower = scope["upper"], scope["lower"]
    beyond = sorted(message_id for message_id, _item in ordered
                   if upper is not None and message_id > upper)
    in_scope = [(message_id, item) for message_id, item in ordered
               if (upper is None or message_id <= upper)
               and (lower is None or message_id >= lower)]

    counts, records, unknown_ids, ambiguous_ids, not_downloadable, scanned = \
        {}, [], [], [], [], 0
    text_ids = []

    def consider(message_id, item):
        category, ambiguous = _classify_detail(item)
        if (category == UNRESOLVED and _media_node(item) is None
                and _tag_of(item) not in _MESSAGE_MEDIA):
            # Task 6c(2)：已确认无附件的纯文字（顶层与 raw 里都没有任何 media 键，
            # 消息级标签也不是媒体构造器）。classify 返 unknown 只是内部哨兵，
            # 候选统计不得把它计入 unknown_ids，否则纯文字区间永远 partial。
            # 有 media 节点但解不出的（Unsupported/Paid/歧义属性）仍走下面的
            # unknown 点名路径，不受影响；messageMediaEmpty 这类“已识别的空媒体”
            # 走 "other"（not_downloadable），本来就不在这里。
            _tally(counts, "text")
            text_ids.append(message_id)
            return None
        _tally(counts, category)
        if ambiguous:
            ambiguous_ids.append(message_id)   # 旗标形状歧义：单独计数上报，不猜类别
        if category == UNRESOLVED:
            unknown_ids.append(message_id)
            return None
        if category not in categories:
            return None
        row = _record(item, message_id, category)
        records.append(row)
        if not row["downloadable"]:
            not_downloadable.append(message_id)
        return row

    matching = (scope["mode"] == "last" and scope["unit"] == "matching_messages")
    if matching:
        # 由新到旧，类型确认后才计数，攒够 n 就停；更早的历史不再扫。
        wanted = scope["n"]
        for message_id, item in reversed(in_scope):
            scanned += 1
            row = consider(message_id, item)
            if row and row["downloadable"] and len(
                    [done for done in records if done["downloadable"]]) >= wanted:
                break
    elif scope["mode"] == "last":
        window = in_scope[-scope["n"]:]
        scanned = len(window)
        for message_id, item in window:
            consider(message_id, item)
    else:
        scanned = len(in_scope)
        for message_id, item in in_scope:
            consider(message_id, item)

    picked = sorted((row for row in records if row["downloadable"]),
                   key=lambda row: row["message_id"])
    matched = sorted(records, key=lambda row: row["message_id"])
    unknown_ids.sort()
    not_downloadable.sort()
    found = len(picked)
    wanted = scope["n"] if scope["mode"] == "last" else None
    exhausted = (wanted is None or found >= wanted or scope["history_complete"])
    selection = {
        "kind": SELECTION_KIND,
        "contains_full_text": False,
        "messages": [row["_message"] for row in picked],
        "attachments": [{key: value for key, value in row.items() if key != "_message"}
                       for row in matched],
        "counts": counts,
        "scanned": scanned,
        "found": found,
        "wanted": wanted,
        "count_unit": scope["unit"],
        "range": copy.deepcopy(scope),
        "snapshot_upper_bound": upper,
        "beyond_upper_bound_ids": beyond,
        "unknown_ids": unknown_ids,
        "ambiguous_flag_ids": sorted(set(ambiguous_ids)),
        "text_ids": sorted(text_ids),
        "duplicated_ids": duplicated,
        "not_downloadable_ids": not_downloadable,
        "unidentified": unidentified,
        "history_exhausted": bool(exhausted),
        "needs_earlier_pages": bool(not exhausted),
        "selected_categories": categories,
        "identity": _payload_identity(container),
    }
    return selection, unknown_ids


# ---------------------------------------------------------------------- 索引文件

def _as_path(value, label):
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s 必须是非空路径（参数列表调用，绝不拼 shell 字符串）：%r"
                         % (label, value))
    return Path(value)


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_download_index(selection, directory, *, identity, chat_id=None,
                        source_path=None, fingerprint=None, now=None):
    """写「用户所选消息」的下载索引：固定升序、只含定位字段、绝不含全文阅读。"""
    entries = [copy.deepcopy(row) for row in selection.get("attachments", [])
               if row.get("downloadable")]
    if not entries:
        raise ValueError("所选类别里没有可下载附件，不生成空索引、不启动下载")
    directory = _as_path(directory, "索引目录")
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("索引必须带归档身份（peer 类型 + chat ID），不能只靠显示名")
    messages = []
    raw_by_id = {}
    for item in selection.get("messages") or []:
        if isinstance(item, dict):
            key = _message_id(item)
            if key is not None:
                raw_by_id[key] = item
    for row in entries:
        raw = raw_by_id.get(row["message_id"])
        if isinstance(raw, dict):
            messages.append(_project(raw))
    if not messages:
        raise ValueError("索引缺少原始消息载荷（不写只有清单没有载荷的空索引）")
    source = None
    if source_path is not None:
        path = _as_path(source_path, "导出文件")
        if path.is_file():
            raw = path.read_bytes()
            source = {"path": str(path), "name": path.name,
                      "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
    payload = {
        "kind": INDEX_KIND, "version": INDEX_VERSION,
        "purpose": "tdl dl -f 的输入：只包含本次用户所选类别的消息附件",
        "contains_full_text": False,
        "identity": identity, "chat_id": chat_id, "fingerprint": fingerprint,
        "backend_command": "dl", "file_name_template": TDL_DEFAULT_TEMPLATE,
        "created_at": (now or _utc_now()), "order": "ascending_message_id",
        "entries": [{key: value for key, value in row.items()
                     if not key.startswith("_")} for row in entries],
        "messages": messages,
        "skipped": {"unknown_ids": list(selection.get("unknown_ids", [])),
                    "ambiguous_flag_ids": list(selection.get("ambiguous_flag_ids", [])),
                    "not_downloadable_ids": list(
                        selection.get("not_downloadable_ids", [])),
                    "beyond_upper_bound_ids": list(
                        selection.get("beyond_upper_bound_ids", []))},
        "source": source,
    }
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("index-%s.json" % state.identity_slug(identity))
    temporary = target.with_name("%s.part-%d" % (target.name, os.getpid()))
    document = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    # 显式 LF 落盘（与 state.py 同一口径）：平台换行不再制造第二套字节口径。
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(document)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return {"path": str(target), "identity": identity, "entries": len(entries),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "payload": payload}


# ---------------------------------------------------------------------- 下载命令

def _argument_list(argv):
    if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)):
        raise ValueError("下载命令必须是参数列表（list[str]），绝不拼接 shell 命令字符串")
    items = list(argv)
    if not items or not all(isinstance(item, str) and item.strip() for item in items):
        raise ValueError("下载命令参数必须全部是非空字符串（用参数列表调用，不走 shell）")
    return items


def _require_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s 必须是非空字符串（运行配置由执行者给出，不猜、不搜索 PATH）"
                         % label)
    return value


def download_argv(config, index_file, directory):
    """`tdl dl -f <所选索引> -d <目录>`：绝不带 -i/-e/--group/--skip-same/--desc。

    类别筛选已在本地按 raw 元数据完成并固化进索引；-i/-e 只看文件名不看 MIME（help:69、
    help:73），用了就会把动画混进视频、把语音混进音频。
    """
    data = config if isinstance(config, dict) else {}
    argv = [_require_text(data.get("tdl"), "tdl"), "dl",
            "-f", str(_as_path(index_file, "媒体索引文件")),
            "-d", str(_as_path(directory, "媒体保存目录")),
            "--template", TDL_DEFAULT_TEMPLATE,
            "-n", _require_text(data.get("namespace"), "namespace"),
            "--storage", "type=bolt,path=%s" % _require_text(data.get("storage"),
                                                             "storage")]
    if data.get("proxy"):
        argv += ["--proxy", _require_text(data.get("proxy"), "proxy")]
    return _argument_list(argv)


def _subprocess_runner(argv):
    """默认后端：只按参数列表启动子进程（不拼 shell、不改全局环境）。"""
    completed = subprocess.run(list(argv), capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = ("%s %s" % (completed.stderr or "", completed.stdout or "")).strip()
        raise DownloadError("下载进程退出码 %s：%s" % (completed.returncode, detail[:400]))
    return completed.returncode


class DownloadError(Exception):
    """下载子进程失败；kind 沿用 state.classify_failure 的口径。"""

    def __init__(self, message, kind=None):
        super().__init__(str(message))
        self.kind = kind if kind in ("network", "rate_limit", "authorization", "unknown")\
            else state.classify_failure(str(message))


class InsufficientSource(OSError):
    """文字源/完成清单缺失或不可读 = **凭据不足**（CLI 退出码 3），不是运行期 IO 失败（1）。

    是 OSError 的子类，因此任何按 OSError 兜底的旧代码都不会漏；组合层在它之前先接住它，
    才能把「源没了」与「跑到一半磁盘出错」分开口径（Task 3 复审 I-3 剩余半条）。
    """


def _redact(text, secrets):
    result = str(text)
    for value in secrets:
        if value and str(value) in result:
            result = result.replace(str(value), "***")
    return result[:400]


def _fail(started, attempts, flags, kind, reason, secrets, stopped=False, code=None):
    return {"started": started, "skipped": False, "returncode": code,
            "attempts": attempts, "failed": True, "stopped": stopped, "kind": kind,
            "flags": flags, "reason": _redact(reason, secrets)}


def _attempt_download(config, index_path, directory, runner, retries,
                     sleeper, secrets):
    flags, attempts = [], 0
    while True:
        attempts += 1
        try:
            argv = _argument_list(download_argv(config, index_path, directory))
            flags = sorted({item for item in argv if item.startswith("-")})
            code = (runner or _subprocess_runner)(argv)
        except (DownloadError, state.ExportError) as error:
            kind = error.kind
            if kind in state.STOPPING_KINDS:
                return _fail(True, attempts, flags, kind,
                             "授权/会话失效：%s；停止本任务，不进重试循环" % error,
                             secrets, stopped=True)
            if kind in state.RETRYABLE_KINDS and attempts <= retries:
                if kind == "rate_limit":
                    sleeper(state.flood_wait_seconds(error))
                continue
            return _fail(True, attempts, flags, kind, error, secrets)
        except FileNotFoundError as error:
            # 依赖缺口：后端起不来，本轮什么都没下载，按能力缺口申报。
            return _fail(True, attempts, flags, "insufficient_source",
                         "后端可执行文件未能启动：%s（不猜路径、不自动安装、不搜索 PATH）"
                         % error, secrets, stopped=True)
        except (TypeError, ValueError) as error:
            return _fail(True, attempts, flags, "unknown", error, secrets, stopped=True)
        if code != 0:
            return _fail(True, attempts, flags, "unknown",
                         "下载进程返回非零退出码 %s" % code, secrets, code=code)
        return {"started": True, "skipped": False, "returncode": code,
                "attempts": attempts, "failed": False, "stopped": False, "kind": None,
                "flags": flags, "reason": None}


# ---------------------------------------------------------------------- 附件验收

def message_id_from_name(name):
    """公开访问器：从 ``tdl dl --template`` 的输出文件名里取回消息 ID（取不到给 None）。

    Task 4 评审 I-7：阅读层要认文件名里的消息 ID，但绝不该摸私有名 —— 这一份是全仓
    唯一的实现，``_message_id_from_name`` 只是它的内部别名。
    """
    match = _MESSAGE_ID_IN_NAME.match(name)
    return int(match.group(1)) if match else None


_message_id_from_name = message_id_from_name


def _list_media(directory):
    if directory is None:
        return False, []
    target = Path(directory)
    if not target.is_dir():
        return False, []
    try:
        names = sorted(item.name for item in os.scandir(target))
    except OSError:
        return True, []
    return True, [target / name for name in names]


def _digest_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lookup(digests, message_id):
    if not isinstance(digests, dict):
        return None
    for key in (str(message_id), message_id):
        value = digests.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def verify_attachments(index, directory, *, expected_digests=None, download_failed=0):
    """按「候选消息 ↔ 输出文件」一一映射验收；退出码 0 单独不算完成凭据。

    缺失 / 大小不符 / 摘要不符 / 歧义 / 不可访问 / 未知类型 / 非所选文件分别计数；
    验收本身只读，绝不删用户已有文件。
    """
    entries = [item for item in (index.get("entries") or [])
               if item.get("downloadable", True)]
    skipped = index.get("skipped") or {}
    exists, files = _list_media(directory)
    mapped = {}
    for path in files:
        mapped.setdefault(_message_id_from_name(path.name), []).append(path)
    result = {key: [] for key in ("verified", "missing", "size_mismatch",
                                 "digest_mismatch", "ambiguous", "inaccessible",
                                 "size_unknown", "weak_basis", "digest_unknown")}
    digests = {}
    for entry in sorted(entries, key=lambda item: item["message_id"]):
        message_id = entry["message_id"]
        matches = mapped.get(message_id) or []
        if not matches:
            result["missing"].append(message_id)
            continue
        if len(matches) > 1:
            result["ambiguous"].append(message_id)
            continue
        path = matches[0]
        try:
            size = os.stat(path).st_size
        except OSError:
            result["inaccessible"].append(message_id)
            continue
        ok = True
        expected_size = entry.get("expected_size")
        if isinstance(expected_size, int) and not isinstance(expected_size, bool):
            if expected_size != size:
                result["size_mismatch"].append(message_id)
                ok = False
        else:
            result["size_unknown"].append(message_id)
            result["weak_basis"].append(message_id)
        try:
            digest = _digest_of(path)
        except OSError:
            result["inaccessible"].append(message_id)
            continue
        digests[str(message_id)] = digest
        expected_digest = _lookup(expected_digests, message_id)
        if expected_digest is None:
            result["digest_unknown"].append(message_id)
        elif expected_digest != digest:
            result["digest_mismatch"].append(message_id)
            ok = False
        if ok:
            result["verified"].append(message_id)
    known = {int(entry["message_id"]) for entry in entries}
    unexpected = sorted(path.name for path in files
                       if _message_id_from_name(path.name) not in known)
    counts = {"expected": len(entries), "verified": len(result["verified"]),
             "missing": len(result["missing"]),
             "size_mismatch": len(result["size_mismatch"]),
             "digest_mismatch": len(result["digest_mismatch"]),
             "ambiguous": len(result["ambiguous"]),
             "inaccessible": len(result["inaccessible"]),
             "unexpected": len(unexpected),
             "size_unknown": len(result["size_unknown"]),
             "not_downloadable": len(skipped.get("not_downloadable_ids") or []),
             "unknown_type": len(skipped.get("unknown_ids") or []),
             "download_failed": int(download_failed or 0)}
    outstanding = sorted(set(result["missing"] + result["size_mismatch"]
                            + result["digest_mismatch"] + result["ambiguous"]
                            + result["inaccessible"]))
    complete = bool(entries) and not outstanding and not counts["download_failed"]
    return {"directory": str(directory), "directory_exists": exists,
            "expected": len(entries), "verified": sorted(result["verified"]),
            "missing": result["missing"], "size_mismatch": result["size_mismatch"],
            "digest_mismatch": result["digest_mismatch"],
            "ambiguous": result["ambiguous"], "inaccessible": result["inaccessible"],
            "size_unknown": result["size_unknown"], "weak_basis": result["weak_basis"],
            "digest_unknown": result["digest_unknown"], "unexpected": unexpected,
            "outstanding": outstanding, "counts": counts, "digests": digests,
            "complete": complete,
            "basis_note": "大小未知的附件只靠存在性与消息 ID 映射验收，依据较弱",
            "return_code_alone_is_not_proof": True}


# ---------------------------------------------------- 下载源：必须是已验收的导出（F3）

def accepted_export(path, root, task):
    """`--export-file` 的验收闸：只认本任务完成清单里登记过、摘要一致的那份导出。

    「文件存在」「JSON 能解析」都不是凭据（那是被明令禁止的老根因换个入口重来）。凭据有三件：

    1. 本任务状态根的完成清单里有一条记录的 sha256/size/count 与该文件完全一致（段记录由
       `state.commit_segment` 写入，final 记录由 `state.merge_completed` 写入；两者登记的
       size/摘要都来自**实际落盘字节**，与这里 `read_bytes()` 同一口径）；
    2. 那条记录的身份就是本任务身份（peer 类型 + chat ID），不消费他人任务验收过的导出；
    3. 记录必须声明区间 `lower/upper`，且区间落在任务已冻结的范围内；缺区间字段的旧记录
       = 凭据不足，**拒绝并申报原因，不静默跳过该凭据**。随后再由 `state.validate_export`
       按任务边界逐条复核消息 ID。

    任一件不满足就 ValueError，由组合层归为退出码 3（能力/凭据不足），且绝不启动子进程。
    """
    path = _as_path(path, "导出文件")
    manifest = state.load_manifest(root)
    records = list(manifest["segments"].values())
    if isinstance(manifest.get("final"), dict):
        records.append(manifest["final"])
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    matched = [item for item in records if isinstance(item, dict)
               and item.get("sha256") == digest]
    if not matched:
        raise ValueError("导出文件没有在本任务的完成清单里验收过（没有任何登记记录的摘要与它"
                         "一致）：文件存在或能解析都不算凭据")
    record = next((item for item in matched
                   if item.get("identity") == task["identity"]), None)
    if record is None:
        raise ValueError("导出文件虽登记在清单里，但没有一条记录的身份与本任务 %s 相符（不消费"
                         "其他任务验收过的导出）" % task["identity"])
    if record.get("chat_id") not in (None, task["chat_id"]):
        raise ValueError("导出文件登记的 chat ID %s 与本任务 %s 不符"
                         % (record.get("chat_id"), task["chat_id"]))
    if "lower" not in record or "upper" not in record:
        raise ValueError("完成清单里与该文件同摘要的记录未声明区间 lower/upper：凭据不足，"
                         "拒绝采信（旧版记录缺区间字段时须重新发布成品，不静默跳过该凭据）")
    if not (task["plan_lower"] <= record["lower"]
            and record["upper"] <= task["snapshot_upper_bound"]):
        raise ValueError("导出文件登记的区间 [%s, %s] 不在任务冻结范围 [%s, %s] 之内"
                         "（不因下载扩大已冻结的范围）"
                         % (record["lower"], record["upper"], task["plan_lower"],
                            task["snapshot_upper_bound"]))
    report = state.validate_export(path, task["chat_id"], task["plan_lower"],
                                   task["snapshot_upper_bound"])
    for key, label in (("sha256", "摘要 sha256"), ("size", "体积 size"),
                       ("count", "条数 count")):
        if record.get(key) is not None and record[key] != report[key]:
            raise ValueError("导出文件与完成清单登记不一致（%s 不符，登记 %s、实际 %s）："
                             "不采信" % (label, record.get(key), report[key]))
    return report


# ------------------------------------------------------------------------ 组合层

def _download_targets(data, state_root):
    chats = data["chats"]
    if len(chats) == 1:
        entry = chats[0]
        root = (Path(state_root) if state_root
               else state.task_state_root(data["destination"], chat_identity(entry)))
        return [(root, data)]
    return [(state.task_state_root(data["destination"], chat_identity(entry)),
             dict(data, chats=[entry])) for entry in chats]


def _read_source(source, task, root):
    if isinstance(source, (str, PurePath, os.PathLike)):
        # 文件入口一律过验收闸：未在本任务完成清单里登记过的 JSON 不采信。
        # Task 3 复审 I-3 剩余半条：源文件在这一步被删/被占，或完成清单读不出来，
        # 都属**凭据不足**（退出码 3），不是运行期 IO 失败（1）——单独归类，别让它
        # 掉进 CLI 的兜底文案。
        try:
            report = accepted_export(source, root, task)
        except OSError as error:
            raise InsufficientSource(
                "下载/阅读的文字源或其完成清单不可用（%s）：%s —— 凭据不足，未启动任何下载"
                % (type(error).__name__, error))
        return report["messages"], {"path": report["path"], "name": report["name"],
                                   "sha256": report["sha256"], "size": report["size"],
                                   "count": report["count"]}
    if isinstance(source, list):
        return list(source), None
    if isinstance(source, dict):
        return _payload_messages(source), None
    raise ValueError("媒体来源必须是导出 JSON 路径、消息数组或含 messages 的映射")


def _previous_digests(root):
    """上一轮验收记录里的摘要：raw 元数据没有内容摘要，只能与自己比。"""
    path = Path(root) / state.OUTPUT_NAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    evidence = (payload.get("media") or {}).get("evidence") or {}
    digests = evidence.get("digests")
    return digests if isinstance(digests, dict) else {}


def _empty_acceptance(directory, unknown_ids, not_downloadable):
    acceptance = verify_attachments({"entries": [], "skipped": {
        "unknown_ids": list(unknown_ids), "not_downloadable_ids": list(not_downloadable)}},
        directory)
    acceptance["complete"] = True
    acceptance["basis_note"] = "所选范围内没有可下载附件，无需启动下载"
    return acceptance


def _skeleton_steps(reason):
    return {"candidates": {"generated": False, "reason": reason, "index_path": None,
                          "entries": 0, "unknown_ids": [], "counts": {},
                          "needs_earlier_pages": False},
            "download": {"started": False, "skipped": True, "returncode": None,
                        "attempts": 0, "failed": False, "stopped": False, "kind": None,
                        "flags": [], "reason": reason},
            "acceptance": _empty_acceptance(None, [], [])}


def _run_one(root, config, task, source, directory, runner, sleeper, progress=None):
    identity = task["identity"]
    progress = {} if progress is None else progress
    secrets = (task.get("lock_root"), config.get("storage"), config.get("namespace"),
               config.get("proxy"))
    messages, source_info = _read_source(source, task, root)
    payload = {"messages": messages, "range": config["range"],
               "snapshot": config.get("snapshot") or {},
               "chat": config["chats"][0]}
    selection, unknown_ids = select_media(
        payload, [item for item in config["contents"] if item in ATTACHMENT_TYPES])
    entries = [row for row in selection["attachments"] if row["downloadable"]]
    # 内部定位索引留在状态目录，附件产物留在用户所选保存目录，两者不混在一处。
    directory = (Path(directory) if directory
                else Path(config["destination"]) / MEDIA_SUBDIR
                / state.identity_slug(identity))
    progress.update({"selection": selection, "directory": directory})
    candidates = {"generated": False, "index_path": None, "entries": len(entries),
                 "unknown_ids": list(unknown_ids),
                 "ambiguous_flag_ids": list(selection["ambiguous_flag_ids"]),
                 "duplicated_ids": selection["duplicated_ids"],
                 "not_downloadable_ids": list(selection["not_downloadable_ids"]),
                 "beyond_upper_bound_ids": list(selection["beyond_upper_bound_ids"]),
                 "counts": dict(selection["counts"]),
                 "scanned": selection["scanned"], "found": selection["found"],
                 "wanted": selection["wanted"],
                 "count_unit": selection["count_unit"],
                 "history_exhausted": selection["history_exhausted"],
                 "needs_earlier_pages": selection["needs_earlier_pages"],
                 "selected_categories": list(selection["selected_categories"]),
                 "reason": None}
    download = {"started": False, "skipped": False, "returncode": None, "attempts": 0,
               "failed": False, "stopped": False, "kind": None, "flags": [],
               "reason": None}
    if not entries:
        # F-B（总控裁决，设计第 98、104 行）：候选索引为空分两态——区分必须读得出自状态，不靠文案。
        # (a) 合法零条：所选范围确实不存在该类消息，且扫描带完成凭据（区间已扫尽、无未判定候选）
        #     -> 允许 complete，但把该凭据写进状态（empty_scope_credential）。
        # (b) 不可判定：有候选却全落 unknown/旗标歧义，或扫描未能确认扫尽 -> 不得 complete，
        #     判 partial，并把「包含 / 跳过 / 调整条件」的决策清单交回用户（unknown 块）。
        unresolved = sorted(set(unknown_ids) | set(selection["ambiguous_flag_ids"]))
        credential = {
            "history_exhausted": bool(selection["history_exhausted"]),
            "needs_earlier_pages": bool(selection["needs_earlier_pages"]),
            "reached_upper_bound": bool(selection["snapshot_upper_bound"] is not None
                                        and not selection["needs_earlier_pages"]),
            "scanned": selection["scanned"], "wanted": selection["wanted"],
            "count_unit": selection["count_unit"],
            "unresolved_candidate_ids": unresolved,
        }
        credential["proves_zero"] = bool(
            credential["history_exhausted"]
            and not credential["needs_earlier_pages"] and not unresolved)
        candidates["empty_scope_credential"] = credential
        acceptance = _empty_acceptance(directory, unknown_ids,
                                      selection["not_downloadable_ids"])
        if unresolved or not credential["history_exhausted"]:
            acceptance["complete"] = False
            acceptance["basis_note"] = ("空索引但不可判定：有候选落 unknown/旗标歧义，或扫描"
                                        "未确认区间扫尽，不擅自判完成，交回用户决定")
            candidates["reason"] = ("所选类别没有可下载附件，但有 %s 条无法判定的候选或扫描"
                                    "未扫尽，不生成索引、不启动下载，交回用户决定"
                                    "（包含/跳过/调整条件）" % len(unresolved))
        else:
            candidates["reason"] = ("所选类别里没有可下载附件（所选 %s），且扫描已证明区间扫尽、"
                                    "无未判定候选，属合法零条，不生成索引、不启动下载"
                                    % "、".join(candidates["selected_categories"]))
    else:
        candidates["empty_scope_credential"] = None
        report = write_download_index(selection, root / MEDIA_SUBDIR, identity=identity,
                                    chat_id=task["chat_id"],
                                    source_path=(source_info or {}).get("path"),
                                    fingerprint=task["fingerprint"])
        candidates.update({"generated": True, "index_path": report["path"],
                          "index_sha256": report["sha256"]})
        progress["index_path"] = report["path"]
        download = _attempt_download(config, Path(report["path"]), directory, runner,
                                    int(config.get("retries") or 0), sleeper, secrets)
        progress["download"] = download
        acceptance = verify_attachments(report["payload"], directory,
                                       expected_digests=_previous_digests(root),
                                       download_failed=(len(entries)
                                                       if download["failed"] else 0))
        progress["acceptance"] = acceptance
    if download["kind"] == "insufficient_source":
        # I-3①（shared-facts 第 21 行）：后端起不来/能力缺口一律 3，不混进运行期失败（1）。
        status = "unavailable"
    elif download["failed"]:
        status = "failed"
    elif not acceptance["complete"]:
        status = "partial"
    elif candidates["needs_earlier_pages"]:
        status = "partial"
    else:
        status = "complete"
    reason = None
    if status == "failed":
        reason = download["reason"] or "下载步骤失败"
    elif status == "unavailable":
        reason = download["reason"] or "能力或依赖缺口，未启动有效下载"
    elif status == "partial":
        credential = candidates.get("empty_scope_credential") or {}
        if not entries and credential.get("unresolved_candidate_ids"):
            reason = ("空索引不可判定：所选类别有 %s 条候选落 unknown/旗标歧义（%s）却全无"
                      "可下载项，未擅自下载、不报告整体完成，交回用户决定"
                      "（包含/跳过/调整条件）"
                      % (len(credential["unresolved_candidate_ids"]),
                         credential["unresolved_candidate_ids"]))
        elif not entries and not credential.get("history_exhausted", True):
            reason = ("空索引且扫描未确认区间扫尽（仍需向更早翻页），未处理完所选范围，"
                      "不报告整体完成，交回用户决定（包含/跳过/调整条件）")
        elif candidates["needs_earlier_pages"]:
            reason = ("所选类别还需向更早的历史翻页才能凑够 %s 条（已得 %s 条），未处理完所选"
                      "范围，不报告整体完成" % (candidates["wanted"], candidates["found"]))
        else:
            reason = ("附件验收未过：缺 %s、大小不符 %s、摘要不符 %s、歧义 %s、不可访问 %s"
                      % (acceptance["missing"], acceptance["size_mismatch"],
                         acceptance["digest_mismatch"], acceptance["ambiguous"],
                         acceptance["inaccessible"]))
    evidence = {"index_path": candidates["index_path"], "identity": identity,
               "counters": acceptance["counts"], "digests": acceptance["digests"],
               "outstanding": acceptance["outstanding"],
               "weak_basis": acceptance["weak_basis"],
               "unknown_ids": list(unknown_ids),
               "needs_earlier_pages": candidates["needs_earlier_pages"],
               "empty_scope_credential": candidates["empty_scope_credential"],
               "return_code_alone_is_not_proof": True,
               "real_media_download_verified": False}
    media_state = state.record_output(root, "media", status, reason=reason,
                                     evidence=evidence)
    return {"kind": REPORT_KIND, "action": "download", "identity": identity,
           "chat_id": task["chat_id"], "state_root": str(root),
           "media_dir": str(directory), "selected_outputs": list(config["outputs"]),
           "contents": list(config["contents"]),
           "snapshot_upper_bound": task["snapshot_upper_bound"],
           "frozen_range": copy.deepcopy(task["range"]),
           "steps": {"candidates": candidates, "download": download,
                     "acceptance": acceptance},
           "counters": acceptance["counts"],
           "outstanding": acceptance["outstanding"],
           "empty_scope_credential": candidates["empty_scope_credential"],
           "unknown": {"ids": list(unknown_ids), "downloaded": False,
                      "user_decision_required": bool(unknown_ids)
                      or bool(candidates["ambiguous_flag_ids"]),
                      "ambiguous_flag_ids": list(candidates["ambiguous_flag_ids"]),
                      "choices": list(_UNKNOWN_CHOICES),
                      "reason": "无法判定类别的消息不擅自下载，交回用户决定"
                                "（含旗标形状歧义，已单独计数）"},
           "status": status, "complete": status == "complete", "reason": reason,
           "media_output": media_state, "readable_document_produced": False,
           "real_media_download_verified": False, "source": source_info,
           "session_mutex": state.session_mutex_report(config),
           "capabilities": copy.deepcopy(CAPABILITIES)}


def _runtime_failure(root, config, task, error, secrets, progress):
    """I-3②：运行中途的 OSError 属运行期失败（退出码 1），不是「配置非法」（2）。

    仍必须写出 media 里程碑与补漏清单：候选已知时对实际目录做一次只读验收，
    未验证明细全部进 outstanding；候选都还没算出来时如实申报「无法确定补漏明细，
    需整体重跑本任务」。绝不伪装已下载，也绝不让异常穿到 CLI 顶层。
    """
    identity = task["identity"]
    reason = ("运行期读写失败（%s）：%s；本轮下载/验收中断，归为运行期失败（退出码 1），"
              "不当作配置非法；补漏清单见 outstanding"
              % (type(error).__name__, _redact(error, secrets)))
    steps = _skeleton_steps(reason)
    # 失败上下文在进入任何可能抛错的步骤**之前**绑定：Task 3 复审 I-3 的剩余半条就是
    # 「候选都没算出来」这条分支引用了从未赋值的 acceptance -> UnboundLocalError，
    # 承诺写出的里程碑与补漏清单一个都没落盘。
    acceptance = steps["acceptance"]
    selection = progress.get("selection")
    if selection is not None:
        if progress.get("acceptance") is not None:
            acceptance = progress["acceptance"]
        else:
            entries = [{"message_id": row["message_id"],
                        "expected_size": row["expected_size"], "downloadable": True}
                       for row in selection["attachments"] if row["downloadable"]]
            acceptance = verify_attachments(
                {"entries": entries,
                 "skipped": {"unknown_ids": list(selection["unknown_ids"]),
                             "not_downloadable_ids": list(
                                 selection["not_downloadable_ids"])}},
                progress.get("directory"))
        steps["candidates"].update({
            "generated": bool(progress.get("index_path")),
            "index_path": progress.get("index_path"),
            "entries": len([row for row in selection["attachments"]
                            if row["downloadable"]]),
            "unknown_ids": list(selection["unknown_ids"]),
            "ambiguous_flag_ids": list(selection["ambiguous_flag_ids"]),
            "duplicated_ids": selection["duplicated_ids"],
            "not_downloadable_ids": list(selection["not_downloadable_ids"]),
            "beyond_upper_bound_ids": list(selection["beyond_upper_bound_ids"]),
            "counts": dict(selection["counts"]), "scanned": selection["scanned"],
            "found": selection["found"], "wanted": selection["wanted"],
            "count_unit": selection["count_unit"],
            "history_exhausted": selection["history_exhausted"],
            "needs_earlier_pages": selection["needs_earlier_pages"],
            "selected_categories": list(selection["selected_categories"]),
            "reason": reason})
        steps["acceptance"] = acceptance
    else:
        acceptance["basis_note"] = ("中断发生在候选生成之前，无法确定补漏明细：需整体重跑"
                                    "本任务后再按清单补漏")
        acceptance["complete"] = False
        acceptance["outstanding_unresolved"] = True
    if progress.get("download") is not None:
        steps["download"] = progress["download"]
    else:
        steps["download"].update({"skipped": False, "reason": reason})
    media_state = state.record_output(root, "media", "failed", reason=reason,
                                      evidence={"outstanding": acceptance["outstanding"],
                                                "counters": acceptance["counts"],
                                                "digests": acceptance["digests"],
                                                "interrupted": True,
                                                "unknown_ids": list(
                                                    selection["unknown_ids"])
                                                if selection else [],
                                                "return_code_alone_is_not_proof": True,
                                                "real_media_download_verified": False})
    return {"kind": REPORT_KIND, "action": "download", "identity": identity,
            "chat_id": task["chat_id"], "state_root": str(root),
            "media_dir": str(progress.get("directory")) if progress.get("directory")
            else None,
            "selected_outputs": list(config.get("outputs") or []),
            "contents": list(config.get("contents") or []),
            "snapshot_upper_bound": task["snapshot_upper_bound"],
            "frozen_range": copy.deepcopy(task["range"]),
            "steps": steps, "counters": acceptance["counts"],
            "outstanding": acceptance["outstanding"],
            "unknown": {"ids": list(selection["unknown_ids"]) if selection else [],
                        "downloaded": False, "user_decision_required": False,
                        "ambiguous_flag_ids": list(
                            selection["ambiguous_flag_ids"]) if selection else [],
                        "choices": list(_UNKNOWN_CHOICES),
                        "reason": "运行期中断；候选集若已生成，unknown 仍按原样上报"},
            "status": "failed", "complete": False, "reason": reason,
            "refused": "runtime_io", "media_output": media_state,
            "readable_document_produced": False,
            "real_media_download_verified": False, "source": None,
            "session_mutex": state.session_mutex_report(config),
            "capabilities": copy.deepcopy(CAPABILITIES)}


def _refused(root, config, task, kind, message):
    report = {"kind": REPORT_KIND, "action": "download",
             "identity": task["identity"] if task else None,
             "chat_id": task["chat_id"] if task else None,
             "state_root": str(root) if root else None,
             "media_dir": None, "selected_outputs": list(config.get("outputs") or []),
             "contents": list(config.get("contents") or []),
             "snapshot_upper_bound": task["snapshot_upper_bound"] if task else None,
             "frozen_range": copy.deepcopy(task["range"]) if task else None,
             "steps": _skeleton_steps(message), "counters": {"expected": 0},
             "outstanding": [],
             "unknown": {"ids": [], "downloaded": False,
                        "user_decision_required": False,
                        "choices": list(_UNKNOWN_CHOICES), "reason": None},
             "status": "unavailable", "complete": False, "reason": message,
             "refused": kind, "readable_document_produced": False,
             "real_media_download_verified": False, "source": None,
             "session_mutex": state.session_mutex_report(config),
             "capabilities": copy.deepcopy(CAPABILITIES)}
    if root is not None:
        # 被拒也要写出 media 里程碑与补漏清单（哪怕是空的），后面的任务才不会面对
        # 「什么都没有」的 outputs.json 猜发生过什么。
        steps = report["steps"]
        state.record_output(root, "media", "unavailable", reason=message,
                            evidence={"outstanding": list(steps["acceptance"]["outstanding"]),
                                      "counters": steps["acceptance"]["counts"],
                                      "basis_note": steps["acceptance"]["basis_note"],
                                      "candidates_generated": steps["candidates"]["generated"],
                                      "refused": kind,
                                      "return_code_alone_is_not_proof": True,
                                      "real_media_download_verified": False})
    return report


def run_download(job, source, *, state_root=None, media_dir=None, runner=None,
                sleeper=None):
    """候选索引生成 -> 所选附件下载 -> 附件验收；整任务一把会话锁，逐聊天分别建档。

    source 可以是导出 JSON 路径、消息数组或含 messages 的映射。下载只消费本任务所选索引；
    未选类别与 unknown 一律不进索引。退出码口径与 shared-facts 一致：启动前的选择/配置
    非法抛 ValueError（CLI 退出码 2）；能力或依赖缺口（含后端起不来、**文字源被删/不可读**
    这类凭据不足、验收凭据不符、锁被占用）收成 unavailable 状态（3）；运行中途的其它
    OSError 收成 failed 状态（1）。**任何一条失败路径都先写出 media 里程碑与补漏清单再返回**，
    绝不让异常穿到 CLI 顶层的「未归类」兜底。不启动任何越权子进程。
    """
    data = validate_job(job)
    contents = [item for item in data["contents"] if item in ATTACHMENT_TYPES]
    if not contents:
        raise ValueError("contents 未选择任何附件类别：download 不下载未选择的媒体")
    if "media" not in data["outputs"]:
        raise ValueError("outputs 未包含 media：不启动媒体下载")
    if isinstance(source, (str, PurePath, os.PathLike)):
        path = _as_path(source, "导出文件")
        if not path.is_file():
            raise ValueError("导出文件不存在或不是文件：%s（不自动找文件、不递归磁盘、"
                             "不启动任何子进程）" % path.name)
    targets = _download_targets(data, state_root)
    prepared = [(root, one, state.init_task(root, one)) for root, one in targets]
    sleeper = sleeper or time.sleep
    secrets = (data.get("storage"), data.get("namespace"), data.get("proxy"))
    try:
        handle = state.acquire_session_lock(data, owner={"pid": os.getpid(),
                                                        "label": "download"})
    except state.LockBusy as error:
        message = "%s（占用者：%s）" % (error, error.holder)
        return _aggregate([_refused(root, one, task, "lock_busy", message)
                          for root, one, task in prepared])
    lock = {"released": True, "path": handle["token"]["path"], "marker": handle["marker"]}
    reports = []
    try:
        for root, one, task in prepared:
            report = {"temp_files": None}
            progress = {}
            try:
                # 取锁后的第一步也可能抛 OSError（例如 media/ 索引目录被别的文件占住）：
                # 它必须在 try 里面，否则会被 CLI 归成「配置非法 2」。
                progress["temp_files"] = state.prune_stale_temp_files(root)
                directory = None
                if media_dir:
                    directory = (Path(media_dir) if len(prepared) == 1
                                else Path(media_dir) / state.identity_slug(task["identity"]))
                report.update(_run_one(root, one, task, source, directory, runner,
                                      sleeper, progress))
            except InsufficientSource as error:
                # ②：源文件被删/被占 = 凭据不足 -> 3，里程碑与补漏清单照常写出。
                report.update(_refused(root, one, task, "insufficient_source",
                                       _redact(error, secrets)))
            except ValueError as error:
                report.update(_refused(root, one, task, "invalid",
                                      _redact(error, secrets)))
            except OSError as error:
                # I-3②：启动都完成、跑在中途的 IO 失败是运行期失败（1），不是配置非法（2）；
                # 里程碑与补漏清单照常写出，异常绝不穿到 CLI 顶层变 traceback。
                report.update(_runtime_failure(root, one, task, error, secrets,
                                               progress))
            report["temp_files"] = progress.get("temp_files")
            reports.append(report)
    finally:
        try:
            state.release_session_lock(handle)
        except (OSError, RuntimeError, ValueError) as error:
            lock = {"released": False, "path": handle["token"]["path"],
                   "marker": handle["marker"], "reason": _redact(error, secrets)}
    return _aggregate(reports, lock=lock, mutex=handle["mutex"])


def _aggregate(reports, *, lock=None, mutex=None):
    """多聊天聚合成任务级报告；单聊天保持原来的单报告形状（与 state 同一口径）。"""
    for report in reports:
        if lock is not None:
            report["lock"] = lock
        if mutex is not None:
            report["session_mutex"] = mutex
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
    return {"kind": TASK_REPORT_KIND, "action": "download", "status": headline,
           "complete": all(item["complete"] for item in reports),
           "session_mutex": mutex, "chats": reports, "lock": lock,
           "capabilities": copy.deepcopy(CAPABILITIES),
           "readable_document_produced": False, "real_media_download_verified": False}
