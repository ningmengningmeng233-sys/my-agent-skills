#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""text_source.py — 只读快照与无损文本 I/O（v1.2 Task 5）。

整本改写的一切从这里开始：本模块是**唯一**解释源文件字节的地方（读 + 编码 +
漂移复核），下游（任务工作区/断点恢复、发布与覆盖）只吃这里给出的快照字段、哈希与
字节，不再各自读盘；落盘仍由仓库既有唯一写盘通道 file_commit 承担（见下文）。

设计方案「输入快照与编码合同」的可执行口径：

  * 只支持 ``.txt`` / ``.md``。DOCX、EPUB、PDF 返回一句转换说明，绝不进入改写。
  * 解码顺序固定：**BOM → 严格 UTF-8 → 严格 GB18030**。GB18030 只有在「重新编码
    能逐字节还原原始字节」时才接受。无 BOM 的 UTF-16、坏编码、疑似二进制一律停止，
    **不猜编码**。同一串字节同时能当 UTF-8 和 GB18030 时，UTF-8 确定性胜出。
  * 原始字节哈希与解码文本哈希、以及解码本身，来自**同一次读取**。
  * 正文不做任何 Unicode 规范化（NFC/NFKC）——异体字、全半角、兼容字符与特殊标点
    必须按字节原样存活。
  * 符号链接 / 重解析点默认拒绝并报告真实目标（文件本身与**任一祖先目录**都算）；
    调用方显式交来已解析文件才继续。
  * 单一 LF / CRLF / CR 的文件输出沿用原风格；混合换行的默认另存取占比最高的一种并
    记录该事实，混合换行不得直接覆盖，除非用户明确授权统一换行。候选开头的 BOM 先
    规范化，绝不产生双 BOM。
  * 恢复、发布、覆盖前用 ``drift_fields()`` / ``assert_source_unchanged()`` 重算哈希，
    漂移即停止，不把新旧原稿混成一个任务。

失败统一抛 :class:`SourceInputError`，带机器可读 ``code`` 与 ``details``，可直接进报告
与 JSON 记录；不向调用方泄漏裸 OSError / UnicodeDecodeError。
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal, Mapping

# ---- 契约常量 --------------------------------------------------------------
SUPPORTED_EXTENSIONS: tuple[str, ...] = (".txt", ".md")
CONVERSION_REQUIRED_EXTENSIONS: tuple[str, ...] = (".docx", ".epub", ".pdf")

LF = "\n"
CRLF = "\r\n"
CR = "\r"
#: 换行统计的键，恒含三项，便于 JSON 记录与逐项比对
NEWLINE_KEYS: tuple[str, ...] = (LF, CRLF, CR)
#: 占比并列时按此优先级定「主导换行」，保证跨机确定性
NEWLINE_PRIORITY: tuple[str, ...] = (CRLF, LF, CR)
_NEWLINE_LABELS = {LF: "lf", CRLF: "crlf", CR: "cr"}

BOM_UTF8 = b"\xef\xbb\xbf"
BOM_UTF16_LE = b"\xff\xfe"
BOM_UTF16_BE = b"\xfe\xff"
#: UTF-32 的两个 BOM 必须以「先于 UTF-16」的顺序识别，否则会被误当成 UTF-16 LE/BE
BOMS_UTF32 = (b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")

_CODEC_BY_ENCODING = {"utf-8": "utf-8", "utf-8-sig": "utf-8",
                      "utf-16-le": "utf-16-le", "utf-16-be": "utf-16-be",
                      "gb18030": "gb18030"}
#: 允许的编码字面量（设计方案第 29 行），与编解码表同源
SNAPSHOT_ENCODINGS: tuple[str, ...] = tuple(_CODEC_BY_ENCODING)
_BOM_BY_ENCODING = {"utf-8": b"", "utf-8-sig": BOM_UTF8, "utf-16-le": BOM_UTF16_LE,
                    "utf-16-be": BOM_UTF16_BE, "gb18030": b""}

CHANNEL_SAVE_AS = "save_as"
CHANNEL_OVERWRITE = "overwrite"

#: Windows MAX_PATH；超过就走「明确停止」而不是撞裸 OSError
WINDOWS_MAX_PATH = 260

#: 保留设备名（含 COM10/ LPT10 这类**不**保留的写法，故用整段匹配）
_RESERVED_RE = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)

_NEWLINE_RE = re.compile(r"\r\n|\r|\n")

# 错误码（下游按 code 分支，不解析中文消息）
CODE_UNSUPPORTED_EXTENSION = "unsupported_extension"
CODE_CONVERSION_REQUIRED = "conversion_required"
CODE_RESERVED_DEVICE_NAME = "reserved_device_name"
CODE_REPARSE_REFUSED = "reparse_refused"
CODE_MISSING = "missing"
CODE_NOT_A_FILE = "not_a_file"
CODE_PATH_TOO_LONG = "path_too_long"
CODE_UNAVAILABLE = "unavailable"
CODE_BINARY_SUSPECTED = "binary_suspected"
CODE_UNDECODABLE = "undecodable"
CODE_EMPTY_CANDIDATE = "empty_candidate"
CODE_MIXED_NEWLINE_OVERWRITE = "mixed_newline_overwrite"
CODE_UNENCODABLE = "unencodable"
CODE_BAD_SNAPSHOT = "bad_snapshot"
CODE_BAD_CHANNEL = "bad_channel"
CODE_SOURCE_DRIFT = "source_drift"


class SourceInputError(RuntimeError):
    """输入/输出合同的违反。``code`` 是给机器看的，``message`` 是给人看的。"""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details)

    def to_dict(self) -> dict:
        """JSON 安全的记录形状，直接进任务报告与断点状态。"""
        return {"code": self.code, "message": self.message, "details": self.details}

    def __str__(self) -> str:  # pragma: no cover - 只影响打印形状
        return f"[{self.code}] {self.message}"


@dataclass(frozen=True)
class SourceSnapshot:
    """一次读取得出的只读快照（字段与设计方案第 35 行逐项对应）。"""

    requested_path: Path
    resolved_path: Path
    extension: Literal[".txt", ".md"]
    size: int
    mtime_ns: int
    sha256: str
    text_sha256: str
    encoding: Literal["utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "gb18030"]
    bom: bytes
    newline_counts: Mapping[str, int]
    ends_with_newline: bool
    file_mode: int

    def to_dict(self) -> dict:
        return {
            "requested_path": str(self.requested_path),
            "resolved_path": str(self.resolved_path),
            "extension": self.extension,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "text_sha256": self.text_sha256,
            "encoding": self.encoding,
            "bom_hex": self.bom.hex(),
            "newline_counts": {key: int(self.newline_counts.get(key, 0)) for key in NEWLINE_KEYS},
            "ends_with_newline": self.ends_with_newline,
            "file_mode": self.file_mode,
        }

    @classmethod
    def from_dict(cls, record: Mapping) -> "SourceSnapshot":
        """断点恢复时把 JSON 记录还原成快照；缺字段就是记录不可信，直接拒绝。"""
        needed = ({f.name for f in fields(cls)} - {"bom"}) | {"bom_hex"}
        missing = sorted(needed - set(record))
        if missing:
            raise SourceInputError(CODE_BAD_SNAPSHOT,
                                   f"快照记录缺字段，拒绝据此恢复: {missing}",
                                   missing=missing)
        if record["encoding"] not in SNAPSHOT_ENCODINGS:
            raise SourceInputError(
                CODE_BAD_SNAPSHOT,
                f"快照记录里的编码 {record['encoding']!r} 不在支持集合 "
                f"{' / '.join(SNAPSHOT_ENCODINGS)} 内，拒绝据此恢复或写盘",
                encoding=str(record["encoding"]), supported=list(SNAPSHOT_ENCODINGS))
        counts = dict(record["newline_counts"])
        return cls(
            requested_path=Path(record["requested_path"]),
            resolved_path=Path(record["resolved_path"]),
            extension=record["extension"],
            size=int(record["size"]),
            mtime_ns=int(record["mtime_ns"]),
            sha256=record["sha256"],
            text_sha256=record["text_sha256"],
            encoding=record["encoding"],
            bom=bytes.fromhex(record["bom_hex"] or ""),
            newline_counts={key: int(counts.get(key, 0)) for key in NEWLINE_KEYS},
            ends_with_newline=bool(record["ends_with_newline"]),
            file_mode=int(record["file_mode"]),
        )


def conversion_instruction(extension: str) -> str:
    """DOCX/EPUB/PDF 的转换说明：不进改写，先转成受支持的纯文本。"""
    ext = str(extension).lower()
    label = {".docx": "DOCX", ".epub": "EPUB", ".pdf": "PDF"}.get(ext, ext.upper())
    return (f"{label} 不进入改写：请先把它转换成 TXT 或 Markdown"
            f"（例如用 Word 另存为纯文本、或 pandoc -o book.md 源文件），"
            f"再把转换后的 .txt / .md 文件交给改写流程。转换环节不改写正文，只换承载格式。")


# ---- 路径与命名门 ----------------------------------------------------------
def _extension_gate(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in SUPPORTED_EXTENSIONS:
        return ext
    if ext in CONVERSION_REQUIRED_EXTENSIONS:
        raise SourceInputError(CODE_CONVERSION_REQUIRED, conversion_instruction(ext),
                               path=str(path), extension=ext,
                               instruction=conversion_instruction(ext),
                               supported=list(SUPPORTED_EXTENSIONS))
    raise SourceInputError(
        CODE_UNSUPPORTED_EXTENSION,
        f"不支持的文件类型 {ext or '（无扩展名）'}：只支持 {' / '.join(SUPPORTED_EXTENSIONS)}，"
        f"其余格式请先转换成 TXT 或 Markdown。路径: {path}",
        path=str(path), extension=ext, supported=list(SUPPORTED_EXTENSIONS))


def _reserved_component(path: Path):
    for part in Path(path).parts:
        if _RESERVED_RE.match(part):
            return part
    return None


def _name_gate(path: Path) -> None:
    reserved = _reserved_component(path)
    if reserved:
        raise SourceInputError(
            CODE_RESERVED_DEVICE_NAME,
            f"路径含 Windows 保留设备名 {reserved.upper()}，不能作为源文件或输出文件。路径: {path}",
            path=str(path), device=reserved.split(".")[0].upper())


def _resolve(path: Path) -> Path:
    """真实路径；解析失败时退回绝对路径，由后续门给出明确错误。"""
    try:
        return Path(os.path.realpath(str(path)))
    except OSError:  # pragma: no cover - 极端路径形态
        return Path(os.path.abspath(str(path)))


def _reparse_component(path: Path):
    """整条路径（文件本身 + **全部**祖先目录）里第一个链接/重解析点，没有则 None。

    只看文件和它的直接父目录会漏掉挂在更上层的 junction：正文照读不误，「默认拒绝并
    报告真实目标」这条闸就形同虚设。这里坚持用 ``lstat`` 而不是比较
    ``resolved != requested``——后者在 Windows 的 8.3 短名与大小写规范化下会误拒正常路径。
    """
    for candidate in (path, *path.parents):
        try:
            st = os.lstat(candidate)
        except OSError:
            continue  # 祖先不存在：交给后面的 missing / path_too_long 门去报
        if os.path.islink(candidate) or getattr(st, "st_reparse_tag", 0):
            return Path(candidate)
    return None


def _lstat_or_raise(path: Path, resolved: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except FileNotFoundError as exc:
        if len(str(resolved)) > WINDOWS_MAX_PATH:
            raise SourceInputError(
                CODE_PATH_TOO_LONG,
                f"路径长度 {len(str(resolved))} 超过 Windows MAX_PATH {WINDOWS_MAX_PATH}，"
                f"系统已无法访问（也可能是文件确实不存在）。请先把文件放到更浅的目录再改写: {path}",
                path=str(path), length=len(str(resolved)), max_path=WINDOWS_MAX_PATH) from exc
        raise SourceInputError(CODE_MISSING, f"源文件不存在: {path}", path=str(path)) from exc
    except OSError as exc:
        if len(str(resolved)) > WINDOWS_MAX_PATH:
            raise SourceInputError(
                CODE_PATH_TOO_LONG,
                f"路径长度 {len(str(resolved))} 超过 Windows MAX_PATH {WINDOWS_MAX_PATH}，"
                f"系统拒绝访问。请先把文件放到更浅的目录再改写: {path}",
                path=str(path), length=len(str(resolved)), max_path=WINDOWS_MAX_PATH) from exc
        raise SourceInputError(CODE_UNAVAILABLE,
                               f"无法读取源文件: {path}（{exc}）", path=str(path)) from exc


# ---- 解码 ------------------------------------------------------------------
def _reject_control_characters(text: str, raw: bytes) -> None:
    """正文里的 C0/C1 控制字符＝疑似二进制或截断的流，停止而不是猜。"""
    for index, char in enumerate(text):
        code = ord(char)
        if (code < 0x20 and char not in "\t\n\r") or 0x7F <= code <= 0x9F:
            raise SourceInputError(
                CODE_BINARY_SUSPECTED,
                f"第 {index} 个字符是控制字符 U+{code:04X}，疑似二进制内容或被截断的流，"
                f"拒绝按文本改写。",
                offset=index, codepoint=code, size=len(raw))


def _looks_like_bomless_utf16(raw: bytes) -> bool:
    """无 BOM UTF-16 的结构特征：高字节位上成片出现 0x00（ASCII 为主的稿子）。"""
    if len(raw) < 8 or len(raw) % 2:
        return False
    half = len(raw) // 2
    le_zeros = sum(1 for i in range(1, len(raw), 2) if raw[i] == 0)
    be_zeros = sum(1 for i in range(0, len(raw), 2) if raw[i] == 0)
    return le_zeros * 4 >= half or be_zeros * 4 >= half


_UNDECODABLE_HINT = (
    "已按 BOM → 严格 UTF-8 → 严格 GB18030（且要求逐字节还原）顺序尝试，全部不通过；"
    "无 BOM 的 UTF-16 一律拒绝。本工具不猜编码：请先确认源文件编码，"
    "另存为 UTF-8 或 GB18030 后再改写。")


def decode_source_bytes(raw: bytes) -> tuple[str, str, bytes]:
    """按固定顺序解码，返回 (文本, 编码, BOM 字节)；不接受就抛错。

    BOM 不进正文（解码文本以快照的 ``bom`` 字段单独承载），下游拿到的一定是干净正文。
    """
    if raw.startswith(BOMS_UTF32):
        raise SourceInputError(CODE_UNDECODABLE,
                               f"检测到 UTF-32 BOM，UTF-32 不在支持范围内。{_UNDECODABLE_HINT}",
                               size=len(raw))
    for bom, encoding in ((BOM_UTF8, "utf-8-sig"),
                          (BOM_UTF16_LE, "utf-16-le"),
                          (BOM_UTF16_BE, "utf-16-be")):
        if raw.startswith(bom):
            codec = _CODEC_BY_ENCODING[encoding]
            try:
                text = raw[len(bom):].decode(codec)
            except UnicodeDecodeError as exc:
                raise SourceInputError(
                    CODE_UNDECODABLE,
                    f"BOM 声明为 {encoding}，但正文按 {encoding} 严格解码失败（{exc}）。"
                    f"{_UNDECODABLE_HINT}",
                    encoding=encoding, offset=exc.start, size=len(raw)) from exc
            _reject_control_characters(text, raw)
            return text, encoding, bom

    if b"\x00" in raw:
        raise SourceInputError(
            CODE_BINARY_SUSPECTED,
            "原始字节含 NUL：疑似二进制内容，或无 BOM 的 UTF-16。"
            "本工具只接受 UTF-8、UTF-8 BOM、带 BOM 的 UTF-16 LE/BE 与严格往返 GB18030，"
            "不猜编码。",
            size=len(raw), nul_count=raw.count(b"\x00"))
    if _looks_like_bomless_utf16(raw):
        raise SourceInputError(
            CODE_BINARY_SUSPECTED,
            "字节分布呈无 BOM 的 UTF-16 形态（高字节位成片为 0）。"
            f"{_UNDECODABLE_HINT}",
            size=len(raw))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as utf8_exc:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as gb_exc:
            raise SourceInputError(
                CODE_UNDECODABLE,
                f"严格 UTF-8 失败（{utf8_exc.reason}），严格 GB18030 也失败"
                f"（{gb_exc.reason}）。{_UNDECODABLE_HINT}",
                size=len(raw)) from gb_exc
        if text.encode("gb18030") != raw:
            raise SourceInputError(
                CODE_UNDECODABLE,
                f"字节可按 GB18030 解码，但重新编码无法逐字节还原原始字节"
                f"（差 {len(text.encode('gb18030')) - len(raw)} 字节），"
                f"说明它不是干净的 GB18030 文本。{_UNDECODABLE_HINT}",
                size=len(raw))
        _reject_control_characters(text, raw)
        return text, "gb18030", b""
    # UTF-8 成功即定案：同一串字节即使也能被 GB18030 解释，也绝不回退——顺序就是确定性。
    _reject_control_characters(text, raw)
    return text, "utf-8", b""


def _newline_counts(text: str) -> dict:
    counts = {key: 0 for key in NEWLINE_KEYS}
    for match in _NEWLINE_RE.findall(text):
        counts[match] += 1
    return counts


# ---- 公开入口：读 ----------------------------------------------------------
def read_source(path, allow_resolved_reparse: bool = False) -> tuple[SourceSnapshot, str]:
    """只读地载入一个 TXT/MD 源文件，返回 (快照, 正文)。

    ``allow_resolved_reparse=True`` 表示调用方已把用户选定的**真实文件**交过来，
    允许穿过符号链接/重解析点继续；默认一律拒绝并报告真实目标。
    """
    requested = Path(path)
    # 设备名先判：`CON` / `nul` 这类路径连扩展名都没有，报「不支持格式」会把真正的
    # 危险（Windows 保留名）掩盖掉。
    _name_gate(requested)
    extension = _extension_gate(requested)

    resolved = _resolve(requested)
    if not allow_resolved_reparse:
        offending = _reparse_component(requested)
        if offending is not None:
            raise SourceInputError(
                CODE_REPARSE_REFUSED,
                f"源文件路径经过符号链接或重解析点：{offending}，真实目标是 {resolved}。"
                f"默认拒绝——请让用户显式选择真实文件后重新提交，"
                f"或在调用方明确传 allow_resolved_reparse=True。",
                path=str(offending), resolved_path=str(resolved))

    st = _lstat_or_raise(requested, resolved)
    if not stat.S_ISREG(st.st_mode):
        raise SourceInputError(CODE_NOT_A_FILE,
                               f"源文件不是普通文件，拒绝改写: {requested}",
                               path=str(requested))

    raw = _read_bytes_once(requested, resolved)
    text, encoding, bom = decode_source_bytes(raw)
    counts = _newline_counts(text)
    snapshot = SourceSnapshot(
        requested_path=requested,
        resolved_path=resolved,
        extension=extension,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        sha256=hashlib.sha256(raw).hexdigest(),
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        encoding=encoding,
        bom=bom,
        newline_counts=counts,
        ends_with_newline=bool(text) and text[-1] in (LF, CR),
        file_mode=st.st_mode,
    )
    return snapshot, text


def _read_bytes_once(path: Path, resolved: Path) -> bytes:
    """一次读取同时供哈希与解码使用；失败给出明确错误而不是裸 OSError。"""
    try:
        return path.read_bytes()
    except OSError as exc:
        if len(str(resolved)) > WINDOWS_MAX_PATH:
            raise SourceInputError(
                CODE_PATH_TOO_LONG,
                f"路径长度超过 Windows MAX_PATH {WINDOWS_MAX_PATH}，读取失败: {path}",
                path=str(path)) from exc
        raise SourceInputError(CODE_UNAVAILABLE, f"读取源文件失败: {path}（{exc}）",
                               path=str(path)) from exc


# ---- 公开入口：换行与编码合同 ---------------------------------------------
def newline_kind(snapshot: SourceSnapshot) -> str:
    present = [key for key in NEWLINE_KEYS if snapshot.newline_counts.get(key)]
    if not present:
        return "none"
    if len(present) == 1:
        return _NEWLINE_LABELS[present[0]]
    return "mixed"


def dominant_newline(snapshot: SourceSnapshot) -> str:
    """输出要用的换行：单一风格沿用原风格；混合取占比最高者；并列按 CRLF > LF > CR。"""
    counts = snapshot.newline_counts
    present = [key for key in NEWLINE_PRIORITY if counts.get(key)]
    if not present:
        return LF
    return max(present, key=lambda key: counts[key])


def describe_newlines(snapshot: SourceSnapshot) -> dict:
    """交付报告要记录的事实：混合换行被统一这一条不能悄悄过去。"""
    kind = newline_kind(snapshot)
    dominant = dominant_newline(snapshot)
    counts = {key: int(snapshot.newline_counts.get(key, 0)) for key in NEWLINE_KEYS}
    unified = kind == "mixed"
    notice = ""
    if unified:
        notice = (f"混合换行源文件：原始分布 CRLF×{counts[CRLF]} / LF×{counts[LF]} / "
                  f"CR×{counts[CR]}，输出统一改用占比最高的 {_NEWLINE_LABELS[dominant]}。")
    return {"kind": kind, "counts": counts, "dominant": dominant,
            "unifies_newlines": unified, "notice": notice}


def encode_text(text, snapshot: SourceSnapshot, *,
                channel: str = CHANNEL_SAVE_AS,
                mixed_newline_authorization: str | None = None) -> bytes:
    """把候选正文按快照的编码、BOM 与换行风格编码成要落盘的字节。

    * 候选开头的 BOM **全部**剥掉再按快照重编码，绝不出现双 BOM；源文件没有 BOM 就
      不会凭空长出一个。
    * 单一 LF / CRLF / CR 源文件一律沿用原风格；混合换行的默认另存取占比最高者。
    * ``channel`` 只认 ``save_as`` / ``overwrite`` 两个字面量，其余（含拼错的
      ``overwrite``）直接拒绝——退到宽松的另存通道会让覆盖闸整道失效。
    * ``channel="overwrite"`` 且源文件是混合换行时，必须有 ``mixed_newline_authorization``
      （用户授权统一换行的原话），否则拒绝。
    * 非空源文件不允许被静默写成空文件或纯空白文件（逐字复制原稿除外）。
    """
    if channel not in (CHANNEL_SAVE_AS, CHANNEL_OVERWRITE):
        raise SourceInputError(
            CODE_BAD_CHANNEL,
            f"未知的写出通道 channel={channel!r}：只接受 "
            f"{CHANNEL_SAVE_AS!r} 与 {CHANNEL_OVERWRITE!r}。"
            f"拼错的通道不得退成默认的另存，否则混合换行的覆盖闸会被绕过。",
            channel=str(channel), supported=[CHANNEL_SAVE_AS, CHANNEL_OVERWRITE])
    if snapshot.encoding not in _CODEC_BY_ENCODING:
        raise SourceInputError(CODE_BAD_SNAPSHOT,
                               f"快照编码 {snapshot.encoding!r} 不在支持集合里，拒绝据此写盘",
                               encoding=str(snapshot.encoding))
    info = describe_newlines(snapshot)
    if info["kind"] == "mixed" and channel == CHANNEL_OVERWRITE \
            and not (mixed_newline_authorization or "").strip():
        raise SourceInputError(
            CODE_MIXED_NEWLINE_OVERWRITE,
            "源文件是混合换行，直接覆盖会改写其中一部分行的分隔符。"
            "默认只允许另存；确需覆盖并统一换行时，请出具用户明确授权的原话。",
            newline_counts=info["counts"], dominant=info["dominant"])
    if not text.strip() and hashlib.sha256(text.encode("utf-8")).hexdigest() != snapshot.text_sha256:
        raise SourceInputError(
            CODE_EMPTY_CANDIDATE,
            "候选正文是空的（或只剩空白），而源文件有内容：拒绝据此产出一份空文件。",
            size=snapshot.size, text_sha256=snapshot.text_sha256)

    # 解码后的文本只可能以 U+FEFF 开头当且仅当源文件带了 ≥2 个 BOM（第一个已被解码器吃掉），
    # 所以开头这些 U+FEFF 一律不是正文：全剥干净再按快照重编码，双 BOM 与「无 BOM 源长出
    # 一个 BOM」都从源头消失。中间的 U+FEFF 是正文字符，原样保留。
    body = text.lstrip("\ufeff")
    payload = info["dominant"].join(_NEWLINE_RE.split(body))
    codec = _CODEC_BY_ENCODING[snapshot.encoding]
    try:
        encoded = payload.encode(codec)
    except UnicodeEncodeError as exc:
        raise SourceInputError(
            CODE_UNENCODABLE,
            f"候选正文无法按源文件编码 {snapshot.encoding} 编码（{exc}）："
            f"请改用能表达的字符，或先转换源文件编码，不要静默丢字。",
            encoding=snapshot.encoding) from exc
    bom = snapshot.bom if snapshot.bom else _BOM_BY_ENCODING[snapshot.encoding]
    return bom + encoded


# ---- 落盘不在本层 ----------------------------------------------------------
# 本模块**不写盘**：读与编码都是纯函数，任何一步失败都不会在磁盘上留下半个文件。
# 全仓唯一的写盘通道是 ``scripts/file_commit.py`` 的 SafeFileCommitEngine（状态机不变量
# 「除 file_commit.py 外 scripts 内不得存在其他写盘实现」把它钉住了），它负责同目录临时
# 文件 → fsync → 原子替换 → 落盘复核 → 失败回滚。发布/覆盖通道（Task 12）要交给它的东西
# 就是 ``encode_text(...)`` 的字节，并用 ``expected_hash=snapshot.sha256`` 复核对源文件的
# 竞争；换行是否允许统一由本模块的 ``channel`` 参数先给出结论，写盘方只照结论执行。


# ---- 公开入口：漂移复核 ----------------------------------------------------
def drift_fields(path, snapshot: SourceSnapshot) -> tuple:
    """重算并比对，返回对不上的字段名（空元组 = 源文件自快照以来未变）。"""
    current = Path(path)
    resolved = _resolve(current)
    st = _lstat_or_raise(current, resolved)
    raw = _read_bytes_once(current, resolved)
    drift = []
    if st.st_size != snapshot.size:
        drift.append("size")
    if st.st_mtime_ns != snapshot.mtime_ns:
        drift.append("mtime_ns")
    if st.st_mode != snapshot.file_mode:
        drift.append("file_mode")
    if hashlib.sha256(raw).hexdigest() != snapshot.sha256:
        drift.append("sha256")
    return tuple(drift)


def assert_source_unchanged(path, snapshot: SourceSnapshot) -> None:
    """恢复 / 发布 / 覆盖前的硬闸：源文件一旦变过就停，绝不把新旧原稿混成一个任务。"""
    fields_drifted = drift_fields(path, snapshot)
    if fields_drifted:
        raise SourceInputError(
            CODE_SOURCE_DRIFT,
            f"源文件在快照之后已发生变化（{', '.join(fields_drifted)}），任务必须停止并重新建立快照，"
            f"不能把新旧原稿混成一次改写: {path}",
            path=str(path), fields=list(fields_drifted))


if __name__ == "__main__":  # pragma: no cover - 手工自检出口
    import sys

    for argument in sys.argv[1:]:
        try:
            snap, body = read_source(argument)
        except SourceInputError as error:
            print(f"[STOP] {error}")
            continue
        raw = snap.resolved_path.read_bytes()
        print(f"[OK] {snap.resolved_path} {snap.encoding} bom={snap.bom.hex() or '-'} "
              f"size={snap.size} sha256={snap.sha256[:12]}… "
              f"newlines={dict(snap.newline_counts)} 末尾换行={snap.ends_with_newline} "
              f"逐字节往返={'是' if encode_text(body, snap) == raw else '否'}")
