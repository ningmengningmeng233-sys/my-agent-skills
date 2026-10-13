# -*- coding: utf-8 -*-
"""共享文件工具 v4.1（单源真相）：read_text_auto / parse_frontmatter / 书名键字面量匹配。

split_segments.py 与 build_segment_index.py 此前各自复制同一份
read_text_auto + parse_frontmatter，改一处漏一处——v4.1 抽取到本文件。
修改编码回退链或 frontmatter 解析规则时只改本文件。

R05-D03 新增（机制来源=本地同族成人索引 v4.6.3 等价实现，非 54Lynnn 作者代码）：
delete_novel / rename_novel 共用的 path_has_book_key / path_replace_book_key /
prefixed_files——书名含 `[`、`%`、`_` 等元字符时只做字面量匹配，不拼进 glob 或 SQL LIKE。
"""
import os
import re
from pathlib import Path

# frontmatter 行格式：键值行 / 列表项 / 注释 / 空行（防正文 `---` 误判的严格性校验）
_FM_ITEM_RE = re.compile(r"^[^:#][^:]{0,40}:\s*.*$|^-\s+.*$|^#.*$|^\s*$")
_LIST_KEYS = {"包含元素"}


def resolve_under(root, relative):
    """将索引库内相对路径安全解析为绝对路径。

    返回 ``None`` 表示路径为空、跨盘符或试图逃出索引库根目录；调用方应将其
    视为校验失败，而不是继续读取宿主文件系统的任意路径。
    """
    if not relative:
        return None
    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root_abs, str(relative).replace("\\", os.sep)))
    try:
        common = os.path.commonpath([os.path.normcase(root_abs), os.path.normcase(candidate)])
    except ValueError:
        return None
    return candidate if common == os.path.normcase(root_abs) else None


def read_text_auto(path):
    """读取文本文件：UTF-8（含 BOM）优先，回退 GB18030/GBK。

    网文 txt 常见 GBK 编码；UTF-8 BOM 若不剥离会让首个章节标题变成
    ``\\ufeff第一章``，章节正则无法匹配并静默丢掉第一章，因此首选
    ``utf-8-sig``。
    """
    last_err = None
    for enc in ("utf-8-sig", "gb18030", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError as e:
            last_err = e
    raise last_err


def parse_frontmatter(text):
    """严格 frontmatter 解析。返回 (meta, body)。

    v4.1 严格化（此前 `text.find("\\n---", 3)` 只找第一处 `---` 行——源文件为 md 且
    正文含 Markdown 分隔线时，frontmatter 被误判结束、正文被吞）：
    - 仅当文本以 `---` 开头且其后存在**独立 `---` 行**闭合
    - frontmatter 区每行必须是 键值 / 列表项 / 注释 / 空行 格式，否则视为无 frontmatter
    - 连续 `- ` 列表项归入上一 key（列表值）；键值行可含 `《》` 等全角字符
    解析失败一律返回 ( {}, text )，绝不吞正文。
    """
    if text.startswith("﻿"):
        text = text[1:]
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    fm_lines = lines[1:end]
    # lstrip 后校验：兼容 LLM 生成的缩进列表项（`  - 标签`）
    if not all(_FM_ITEM_RE.match(ln.lstrip()) for ln in fm_lines):
        return {}, text
    meta, cur_key = {}, None
    for ln in fm_lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- "):
            item = s[2:].strip()
            if cur_key in _LIST_KEYS and item:
                if not isinstance(meta.get(cur_key), list):
                    meta[cur_key] = []
                meta[cur_key].append(item)
            continue
        if ":" in ln:
            k, v = ln.split(":", 1)
            cur_key = k.strip()
            meta[cur_key] = v.strip().strip("'\"")
    return meta, "\n".join(lines[end + 1:])


_SEP_SPLIT_RE = re.compile(r"([\\/]+)")


def _is_book_key_segment(segment, key):
    """路径段等于 `key` 或以 `key-` 开头（字面量，不解释任何通配语义）。"""
    return segment == key or segment.startswith(key + "-")


def _split_key_segments(path):
    """把路径切成 (段, 分隔符, 段, ...)；返回的列表奇数位是分隔符，原样可回拼。"""
    return _SEP_SPLIT_RE.split(str(path))


def path_has_book_key(path, key):
    """路径（任意分隔符写法）是否属于该书——纯字面量逐段比对。

    书名当 glob / SQL LIKE 模式处理会误伤他书：`[1]` 是字符类、`%` 与 `_` 是通配。
    """
    if not path or not key:
        return False
    return any(_is_book_key_segment(seg, key)
               for i, seg in enumerate(_split_key_segments(path)) if i % 2 == 0 and seg)


def path_replace_book_key(path, old_key, new_key):
    """逐段字面量把 `old_key` 前缀改写为 `new_key`，分隔符风格原样保留。

    只改路径段开头命中的片段（不是任意子串、也不是无限次 replace），
    无命中返回 None，调用方可据此判断该行/该字段是否属于本书。
    """
    if not path or not old_key:
        return None
    chunks = _split_key_segments(path)
    changed = False
    for i, seg in enumerate(chunks):
        if i % 2 == 1 or not _is_book_key_segment(seg, old_key):
            continue
        chunks[i] = new_key if seg == old_key else new_key + seg[len(old_key):]
        changed = True
    return "".join(chunks) if changed else None


def prefixed_files(directory, key, suffix=None):
    """列出目录内属于该书的书名键文件（字面量前缀匹配，替代 glob）。"""
    directory = Path(str(directory))
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and _is_book_key_segment(p.name, key)
        and (suffix is None or p.name.endswith(suffix))
    )
