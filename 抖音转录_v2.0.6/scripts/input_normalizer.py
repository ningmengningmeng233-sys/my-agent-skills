"""输入导入与 URL 规范化。

用户可能提交聊天原文、TXT/Markdown 或 CSV。无论来源如何，交给上游的
永远是规范化后的抖音作品链接：`modal_id` 一律转成标准作品页，
相似域名一律拒绝，跟踪参数一律剥离。
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from scripts.models import NormalizedInput

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_URL_CANDIDATES = 10_000

_ALLOWED_HOST = "douyin.com"
_SHORT_HOST = "v.douyin.com"
_VIDEO_PATH_PATTERN = re.compile(r"/video/(\d{6,})")
_MODAL_ID_PATTERN = re.compile(r"^\d{6,}$")
_URL_PATTERN = re.compile(r"https?://[^\s\u3000\u4e00-\u9fff\"'<>（）【】]+",
                          re.IGNORECASE)
_TRAILING_PUNCTUATION = "\"'.,;:!?，。；：！？、）】》」』…·"
_CSV_URL_COLUMNS = ("url", "link", "douyin_url", "分享链接")


class InputError(Exception):
    """导入阶段的稳定错误：`code` 直接作为 JSON 的 error.code。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_douyin_host(host: str) -> bool:
    host = (host or "").lower().strip(".")
    return host == _ALLOWED_HOST or host.endswith("." + _ALLOWED_HOST)


def _clean_url(raw: str) -> str:
    return raw.rstrip(_TRAILING_PUNCTUATION)


def _modal_id(query: str) -> str:
    for value in parse_qs(query, keep_blank_values=False).get("modal_id", []):
        if _MODAL_ID_PATTERN.match(value or ""):
            return value
    return ""


def _video_id(path: str) -> str:
    match = _VIDEO_PATH_PATTERN.search(path or "")
    return match.group(1) if match else ""


def _strip_tracking(url: str) -> str:
    """只移除查询串（跟踪参数），绝不改动路径中的作品 ID。"""
    parts = urlsplit(url)
    if not parts.query:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _to_input(url: str, raw_input: str) -> NormalizedInput | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not _is_douyin_host(host):
        return None

    modal = _modal_id(parts.query)
    if modal:
        return NormalizedInput(
            raw_input=raw_input,
            url=f"https://www.douyin.com/video/{modal}",
            candidate_aweme_id=modal, kind="video")

    video_id = _video_id(parts.path)
    if video_id:
        return NormalizedInput(
            raw_input=raw_input,
            url=f"https://www.douyin.com/video/{video_id}",
            candidate_aweme_id=video_id, kind="video")

    if host == _SHORT_HOST:
        # 短链交给上游展开；展开后以真实 aweme_id 二次去重
        return NormalizedInput(raw_input=raw_input, url=_clean_url(url),
                               candidate_aweme_id="", kind="short_link")

    return NormalizedInput(raw_input=raw_input, url=_strip_tracking(url),
                           candidate_aweme_id="", kind="other")


def _extract(text: str, raw_input: str) -> list[NormalizedInput]:
    items: list[NormalizedInput] = []
    for match in _URL_PATTERN.finditer(text or ""):
        item = _to_input(_clean_url(match.group(0)), raw_input)
        if item is not None:
            items.append(item)
    return items


def normalize_inputs(text: str) -> list[NormalizedInput]:
    """从任意文本中按出现顺序提取抖音链接（不去重，去重是队列的职责）。"""
    return _guard_limit(_extract(text or "", text or ""))


def strip_query_parameters(text: str) -> str:
    """去掉文本中所有 URL 的查询串，用于长期留存前的脱敏（设计文档第 20 节）。"""
    def _replace(match: re.Match) -> str:
        return _strip_tracking(match.group(0))

    return _URL_PATTERN.sub(_replace, str(text or ""))


def _decode(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(
            "input_encoding_error",
            "文件不是 UTF-8 编码；请另存为 UTF-8 或 UTF-8 BOM 后重试") from exc


def _load_text(text: str) -> list[NormalizedInput]:
    items: list[NormalizedInput] = []
    for line in text.splitlines():
        if line.strip():
            items.extend(_extract(line, line))
    return _guard_limit(items)


def _load_csv(text: str) -> list[NormalizedInput]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = [cell.strip().lower() for cell in rows[0]]
    column = next((header.index(name) for name in _CSV_URL_COLUMNS
                   if name in header), None)
    items: list[NormalizedInput] = []
    for row in rows[1:]:
        if not row:
            continue
        # raw_input 保留整行，作为该条 job 的可追溯原始输入
        label = ",".join(row)
        cells = ([row[column]] if column < len(row) else []) \
            if column is not None else row
        for cell in cells:
            items.extend(_extract(cell, label))
    return _guard_limit(items)


def _guard_limit(items: list[NormalizedInput]) -> list[NormalizedInput]:
    if len(items) > MAX_URL_CANDIDATES:
        raise InputError(
            "invalid_input",
            f"URL 候选超过 {MAX_URL_CANDIDATES} 条上限，请拆分后再导入")
    return items


def load_input_file(path: Path) -> list[NormalizedInput]:
    """读取 TXT / Markdown / CSV 列表；超限或编码错误都抛 `InputError`。"""
    path = Path(path)
    if not path.is_file():
        raise InputError("invalid_input", f"文件不存在：{path.name}")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise InputError(
            "invalid_input",
            f"文件超过 10 MiB 上限（当前 {size} 字节），请拆分后再导入")
    text = _decode(path)
    if path.suffix.lower() == ".csv":
        return _load_csv(text)
    return _load_text(text)
