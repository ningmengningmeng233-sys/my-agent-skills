#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入规范化（规格 4）：叙事文本 → 带来源定位的 UTF-8 Markdown。

契约：
- `normalize_sources(paths, order_file=None) -> NormalizedSource` 是唯一入口。
- 每个 SourceUnit 可追溯到原文件、文件哈希、顺序、卷/章/集/场与段落序号、原文摘句。
- 编码只做显式探测（UTF-8 BOM / UTF-8 / GB18030）；无法解码即 SOURCE_DECODE_FAILED，
  绝不 errors="ignore" 静默丢字。
- PDF 与未知后缀在读取内容前拒绝（UNSUPPORTED_SOURCE_FORMAT，规格 4）。
- 多文件：显式顺序清单优先；未提供清单时自然排序并把排序来源写入 manifest。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

# 正式支持的输入（规格 2.2）；PDF 明确不支持，也不建设 OCR（规格 12）
TEXT_SUFFIXES = (".txt", ".md", ".markdown")
DOCX_SUFFIXES = (".docx",)
EPUB_SUFFIXES = (".epub",)
JSON_SUFFIXES = (".json",)
SUPPORTED_SUFFIXES = TEXT_SUFFIXES + DOCX_SUFFIXES + EPUB_SUFFIXES + JSON_SUFFIXES
REJECTED_SUFFIXES = (".pdf",)

# 显式探测顺序（规格 4：不得依赖 locale 或 errors="ignore"）
BOM_UTF8 = b"\xef\xbb\xbf"
ENCODING_PROBE = ("utf-8", "gb18030")

_TXT_REFORMAT_HINT = "请先转为 TXT、Markdown、DOCX 或 EPUB 后重试"
_ORDER_COMMENT_PREFIX = "#"


class SourceNormalizeError(ValueError):
    """输入规范化失败：code 为稳定原因码，detail 说明修复入口。

    继承 ValueError：调用方既可按 `except ValueError` 粗拦，也可读 code 精确分派。
    """

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


@dataclass(frozen=True)
class SourceUnit:
    """一个可追溯的规范化段落。"""

    source_path: str
    source_sha256: str
    order: int
    locator: str
    text: str


@dataclass
class NormalizedSource:
    """规范化结果：Markdown 正文 + 段落清单 + 结构化来源清单。

    unit_lines 为每个单元在 markdown 中的 1-based 行区间（与 units 一一对应），
    供拆段时把段落区间映射回原始来源定位。
    """

    markdown: str
    units: list[SourceUnit] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    unit_lines: list[tuple[int, int]] = field(default_factory=list)


def _natural_key(path: Path) -> tuple:
    """自然排序键：卷2 先于 卷10（未提供顺序清单时的确定性兜底）。"""
    parts = re.split(r"(\d+)", path.name)
    return tuple(int(p) if p.isdigit() else p for p in parts) + (str(path.parent),)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def decode_bytes(raw: bytes, path: Path) -> tuple[str, str]:
    """显式解码：UTF-8 BOM → UTF-8 → GB18030。返回 (文本, 编码名)。"""
    if raw.startswith(BOM_UTF8):
        try:
            return raw[len(BOM_UTF8):].decode("utf-8"), "utf-8-sig"
        except UnicodeDecodeError as e:
            raise SourceNormalizeError(
                "SOURCE_DECODE_FAILED", f"{path.name} 声明 UTF-8 BOM 但内容不是合法 UTF-8：{e}"
            ) from e
    for enc in ENCODING_PROBE:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise SourceNormalizeError(
        "SOURCE_DECODE_FAILED",
        f"{path.name} 不是 UTF-8 或 GB18030 编码（只支持这两种显式探测，不做忽略式解码）"
        f"；请转码为 UTF-8 后重试",
    )


def split_paragraphs(text: str) -> list[str]:
    """按空行切段，保留块内换行（章节标题因此仍是独立行，供拆段正则识别）。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", normalized)
    return [b.strip() for b in blocks if b.strip()]


def _check_suffix(path: Path) -> str:
    """校验后缀并返回格式名；PDF 与未知格式在此拒绝（读取内容之前）。"""
    suffix = path.suffix.lower()
    if suffix in REJECTED_SUFFIXES:
        raise SourceNormalizeError(
            "UNSUPPORTED_SOURCE_FORMAT", f"{path.name} 为 PDF：不支持 PDF，也不建设文字识别；{_TXT_REFORMAT_HINT}"
        )
    if suffix not in SUPPORTED_SUFFIXES:
        raise SourceNormalizeError(
            "UNSUPPORTED_SOURCE_FORMAT", f"{path.name} 后缀 {suffix or '（无）'} 不受支持；{_TXT_REFORMAT_HINT}"
        )
    if suffix in DOCX_SUFFIXES:
        return "docx"
    if suffix in EPUB_SUFFIXES:
        return "epub"
    if suffix in JSON_SUFFIXES:
        return "json"
    return "md" if suffix in (".md", ".markdown") else "txt"


def _read_order_file(order_file: Path, paths: Sequence[Path]) -> list[Path]:
    """解析显式顺序清单：每行一个相对路径，相对清单所在目录解析。

    清单必须与提供的源文件一一对应；重复、缺失、越界分别报稳定原因码。
    """
    root = order_file.resolve().parent
    try:
        raw = order_file.read_text(encoding="utf-8")
    except OSError as e:
        raise SourceNormalizeError("SOURCE_ORDER_UNREADABLE", f"顺序清单不可读：{e}") from e
    entries: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith(_ORDER_COMMENT_PREFIX):
            continue
        entries.append(s)
    if not entries:
        raise SourceNormalizeError("SOURCE_ORDER_MISSING", f"顺序清单为空：{order_file.name}")

    available = {p.resolve(): p for p in paths}
    seen: set[Path] = set()
    ordered: list[Path] = []
    for entry in entries:
        candidate = Path(entry)
        resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise SourceNormalizeError(
                "SOURCE_ORDER_OUTSIDE_ROOT",
                f"顺序清单条目越出清单所在目录：{entry}（根目录 {root}）",
            ) from None
        if resolved in seen:
            raise SourceNormalizeError("SOURCE_ORDER_DUPLICATE", f"顺序清单重复条目：{entry}")
        seen.add(resolved)
        if resolved not in available:
            raise SourceNormalizeError(
                "SOURCE_ORDER_MISSING", f"顺序清单条目未对应任何输入源：{entry}"
            )
        ordered.append(available[resolved])

    uncovered = [p.name for r, p in available.items() if r not in seen]
    if uncovered:
        raise SourceNormalizeError(
            "SOURCE_ORDER_MISSING", f"以下输入源未出现在顺序清单中：{'、'.join(sorted(uncovered))}"
        )
    return ordered


def resolve_order(paths: Sequence[Path], order_file: Path | None) -> tuple[list[Path], str]:
    """确定处理顺序：显式清单优先，否则自然排序。返回 (有序路径, 顺序来源)。"""
    if order_file is not None:
        if not order_file.is_file():
            raise SourceNormalizeError("SOURCE_ORDER_UNREADABLE", f"顺序清单不存在：{order_file}")
        return _read_order_file(order_file, paths), "explicit"
    return sorted(paths, key=_natural_key), "natural"


def _units_from_text(path: Path, order: int, digest: str, text: str, prefix: str = "") -> list[SourceUnit]:
    """文本来源 → 段落单元；locator 前缀用于区分 DOCX 标题/EPUB 章节等定位轴。"""
    units: list[SourceUnit] = []
    for i, block in enumerate(split_paragraphs(text), 1):
        locator = f"{prefix}{i}" if prefix else f"文件{order}/段落{i}"
        units.append(SourceUnit(
            source_path=path.resolve().as_posix(),
            source_sha256=digest,
            order=order,
            locator=locator,
            text=block,
        ))
    return units


# --- DOCX（规格 4：读取正文段落、标题层级和表格文本；忽略页眉页脚） -----------------

DOCX_HEADING_MARKERS = ("heading", "标题", "title")


def _docx_block_iterator(doc):
    """按文档顺序遍历 body 直接子元素（页眉页脚不在 body 内，天然被排除）。"""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield "p", Paragraph(child, doc)
        elif tag == "tbl":
            yield "tbl", Table(child, doc)


def _is_docx_heading(paragraph) -> bool:
    style = getattr(paragraph, "style", None)
    name = (getattr(style, "name", "") or "").lower()
    style_id = (getattr(style, "style_id", "") or "").lower()
    return any(m in name or m in style_id for m in DOCX_HEADING_MARKERS)


def _read_docx(path: Path, order: int, digest: str) -> list[SourceUnit]:
    """DOCX → 段落单元：标题、正文段落、表格行按文档顺序进入规范化文本。

    locator 轴：标题自身 `{标题}/标题`，标题下段落 `{标题}/段落N`，表格行 `表格N/行M`；
    尚无标题时退化为 `文件{order}/段落N`。
    """
    from docx import Document

    doc = Document(str(path))
    resolved = path.resolve().as_posix()
    units: list[SourceUnit] = []
    heading = ""
    para_no = 0
    table_no = 0

    def add(locator: str, text: str) -> None:
        units.append(SourceUnit(source_path=resolved, source_sha256=digest,
                                order=order, locator=locator, text=text))

    for kind, block in _docx_block_iterator(doc):
        if kind == "p":
            text = (block.text or "").strip()
            if not text:
                continue
            if _is_docx_heading(block):
                heading = text
                para_no = 0
                add(f"{heading}/标题", text)
                continue
            para_no += 1
            add(f"{heading}/段落{para_no}" if heading else f"文件{order}/段落{para_no}", text)
        else:
            table_no += 1
            for row_no, row in enumerate(block.rows, 1):
                cells = [(c.text or "").strip() for c in row.cells]
                line = " | ".join(c for c in cells if c)
                if line:
                    add(f"表格{table_no}/行{row_no}", line)
    return units


# --- EPUB（规格 4：按 spine 顺序读取章节，剔除导航页与样式脚本） --------------------

# 块级元素：正文按这些粒度成段
EPUB_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre", "td")


def _epub_spine_documents(book, item_type: int):
    """按 spine 顺序产出正文文档；导航页（nav）在此剔除。"""
    for entry in book.spine:
        idref = entry[0] if isinstance(entry, tuple) else entry
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != item_type:
            continue
        if not item.is_chapter():
            continue
        yield item


def _read_epub(path: Path, order: int, digest: str) -> list[SourceUnit]:
    """EPUB → 段落单元：按 spine 顺序读章节，剔除 nav/script/style 后逐块成段。

    locator 轴：`{章节文件}#段落N`。
    """
    import ebooklib
    from bs4 import BeautifulSoup
    from ebooklib import epub

    book = epub.read_epub(str(path), options={"ignore_ncx": True})
    resolved = path.resolve().as_posix()
    units: list[SourceUnit] = []
    for item in _epub_spine_documents(book, ebooklib.ITEM_DOCUMENT):
        text, _encoding = decode_bytes(item.get_content(), path)
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "nav"]):
            tag.decompose()
        chapter = Path(item.get_name()).name
        n = 0
        for block in soup.find_all(EPUB_BLOCK_TAGS):
            if block.find(EPUB_BLOCK_TAGS):
                continue  # 交给更细的内层块，避免父子重复计数
            content = block.get_text(" ", strip=True)
            if not content:
                continue
            n += 1
            units.append(SourceUnit(source_path=resolved, source_sha256=digest, order=order,
                                    locator=f"{chapter}#段落{n}", text=content))
    if not units:
        raise SourceNormalizeError(
            "SOURCE_EMPTY_CONTENT", f"{path.name} 未解析出任何正文（可能只有导航页或空章节）"
        )
    return units


def _read_source(path: Path, order: int, digest: str) -> tuple[list[SourceUnit], str]:
    """按格式分派读取；返回 (单元, 编码标识)。"""
    fmt = _check_suffix(path)
    if fmt == "docx":
        return _read_docx(path, order, digest), "binary"
    if fmt == "epub":
        return _read_epub(path, order, digest), "binary"
    if fmt == "json":
        from engine.script_adapter import script_to_markdown
        raw_text = path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
        md_text = script_to_markdown(data)
        return _units_from_text(path, order, digest, md_text, prefix=f"剧本{order}/"), "utf-8"
    text, encoding = decode_bytes(path.read_bytes(), path)
    return _units_from_text(path, order, digest, text), encoding


def _render_markdown(blocks: list[tuple[str, str, int, list[str]]]) -> tuple[str, list[tuple[int, int]]]:
    """按顺序拼装规范化 Markdown，并记录每个单元的行区间。

    每个来源前写不可变来源注释（规格 4）；单元之间以空行分隔，保证拆分语义
    与 split_paragraphs 一致。返回 (markdown, 单元行区间列表)。
    """
    lines: list[str] = []
    spans: list[tuple[int, int]] = []
    for name, digest, order, unit_texts in blocks:
        if lines:
            lines.append("")
        lines.append(f"<!-- source: {name} sha256:{digest} order:{order} -->")
        for text in unit_texts:
            lines.append("")
            start = len(lines) + 1
            lines.extend(text.split("\n"))
            spans.append((start, len(lines)))
    return "\n".join(lines).rstrip("\n") + "\n", spans


def locator_span_for_lines(result: NormalizedSource, start_line: int, end_line: int) -> str:
    """把规范化 Markdown 的行区间映射回原始来源定位（规格 4 可追溯性）。

    返回形如 `卷1.txt: 文件1/段落2~文件1/段落5`；跨来源时给出两个来源各自的端点。
    与任何单元都不相交时返回空串。
    """
    hit = [i for i, (s, e) in enumerate(result.unit_lines) if s <= end_line and e >= start_line]
    if not hit:
        return ""
    first, last = result.units[hit[0]], result.units[hit[-1]]
    head = f"{Path(first.source_path).name}: {first.locator}"
    tail = "~" + last.locator if last.locator != first.locator else ""
    return head + tail


def normalize_sources(paths: Sequence[Path], order_file: Path | None = None) -> NormalizedSource:
    """把若干叙事文本源规范化为单一 UTF-8 Markdown，并返回可追溯来源清单。"""
    resolved = [Path(p) for p in paths]
    if not resolved:
        raise SourceNormalizeError("SOURCE_LIST_EMPTY", "未提供任何输入源")
    for p in resolved:
        if not p.is_file():
            raise SourceNormalizeError("SOURCE_NOT_FOUND", f"输入源不存在：{p}")
        _check_suffix(p)

    ordered, order_source = resolve_order(resolved, order_file)

    units: list[SourceUnit] = []
    blocks: list[tuple[str, str, int, list[str]]] = []
    sources_meta: list[dict[str, Any]] = []
    for order, path in enumerate(ordered, 1):
        raw = path.read_bytes()
        digest = _sha256_bytes(raw)
        fmt = _check_suffix(path)
        file_units, encoding = _read_source(path, order, digest)
        units.extend(file_units)
        blocks.append((path.name, digest, order, [u.text for u in file_units]))
        sources_meta.append({
            "order": order,
            "name": path.name,
            "source_path": path.resolve().as_posix(),
            "sha256": digest,
            "bytes": len(raw),
            "format": fmt,
            "encoding": encoding,
            "unit_count": len(file_units),
        })

    markdown, unit_lines = _render_markdown(blocks)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "order_source": order_source,
        "order_file": order_file.resolve().as_posix() if order_file else "",
        "sources": sources_meta,
        "unit_count": len(units),
        "markdown_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
    }
    return NormalizedSource(markdown=markdown, units=units, manifest=manifest, unit_lines=unit_lines)


def needs_normalization(paths: Sequence[Path], order_file: Path | None = None) -> bool:
    """是否需要先走规范化层（规格 4）。

    单文件 TXT/Markdown 且无顺序清单时走既有直读路径（保持旧行为不变）；
    非 TXT/MD（DOCX/EPUB）、多文件项目或显式顺序清单都必须先规范化。
    """
    if order_file is not None or len(paths) != 1:
        return True
    return Path(paths[0]).suffix.lower() not in TEXT_SUFFIXES


def normalized_dir(lib: Path) -> Path:
    """规范化中间产物目录（规格 4：只写资产库 .process，原文件只读）。"""
    return Path(lib) / ".process" / "normalized_sources"


def write_normalized(result: NormalizedSource, out_dir: Path) -> dict[str, str]:
    """把规范化结果原子写入资产库 `.process/normalized_sources/`，返回产物路径字典。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "规范化原文.md"
    manifest_path = out_dir / "来源清单.json"
    for target, content in (
        (md_path, result.markdown),
        (manifest_path, json.dumps(result.manifest, ensure_ascii=False, indent=2) + "\n"),
    ):
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    return {"markdown": md_path.as_posix(), "manifest": manifest_path.as_posix(), "dir": out_dir.as_posix()}
