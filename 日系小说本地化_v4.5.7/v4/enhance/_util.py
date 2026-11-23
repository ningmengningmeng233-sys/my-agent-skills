"""v4 Enhance 共享只读工具：路径解析、frontmatter 解析、文本截断。

只读约束：本模块只提供读取外部资产的能力，从不写回索引库、写作指导或源语料。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

MAX_SNIPPET_CHARS = 60

_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def dotenv_values(path: Path) -> dict[str, str]:
    """读取 KEY=VALUE 形式的 .env 文件，不引入外部依赖。"""
    values: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                match = _ENV_LINE.match(line)
                if match:
                    key, value = match.group(1), match.group(2)
                    values[key] = value.strip('"').strip("'")
    except OSError:
        return {}
    return values


def merge_env(*sources: Mapping[str, str] | None) -> dict[str, str]:
    """按优先级合并环境源，后传入的源优先级更高。"""
    merged: dict[str, str] = {}
    for source in sources:
        if source:
            merged.update(source)
    return merged


def bounded(text: str, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    """截断为受限片段，避免把长句原样传入正文或审计。"""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def normalize_relpath(root: Path, path: Path) -> str:
    """返回资产相对根目录的 POSIX 归一化路径（record 字段用）。"""
    return path.relative_to(root).as_posix()


def parse_frontmatter(text: str) -> dict[str, object]:
    """解析 --- 包裹的简单 YAML 式 frontmatter。

    支持 `key: value`、`key:` 后接 `- item` 列表，以及形如
    `- 装逼打脸 | 打脸 | ...` 的管线分隔项（保留为列表项原文，由调用方解释）。
    """
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    end: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}
    meta: dict[str, object] = {}
    current_key: str | None = None
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            if current_key is None:
                continue
            items = meta.setdefault(current_key, [])
            if not isinstance(items, list):
                items = []
                meta[current_key] = items
            items.append(stripped[2:].strip())
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            current_key = key
            if value:
                meta[key] = value.strip('"').strip("'")
            else:
                meta[key] = []
        else:
            # 续行：追加到当前列表（frontmatter 中极少出现）
            if current_key and isinstance(meta.get(current_key), list):
                meta[current_key].append(stripped)  # type: ignore[union-attr]
    return meta


def looks_like_index_lib(root: Path) -> bool:
    """判断目录是否具备索引库形态（存在 章节索引 或 小说索引 或 文风蒸馏卡）。"""
    expected = ("章节索引", "小说索引", "文风蒸馏卡", "小说原文", "判官验证")
    return root.is_dir() and any((root / name).is_dir() for name in expected)


def looks_like_writing_guide(root: Path) -> bool:
    modules = root / "references" / "modules"
    return root.is_dir() and modules.is_dir()


def split_pipe_tags(raw_items: Iterable[str]) -> list[tuple[str, list[str]]]:
    """把 `- 装逼打脸 | 打脸 | ...` 拆成 (canonical, [synonyms])。

    首段为规范标签，其余为同义词；去重保序。
    """
    result: list[tuple[str, list[str]]] = []
    for raw in raw_items:
        parts = [part.strip() for part in raw.split("|") if part.strip()]
        if not parts:
            continue
        canonical = parts[0]
        synonyms: list[str] = []
        for part in parts[1:]:
            if part not in synonyms:
                synonyms.append(part)
        result.append((canonical, synonyms))
    return result


def flatten_tags(tag_pairs: Sequence[tuple[str, list[str]]]) -> list[str]:
    """展开 (canonical, synonyms) 为去重标签集合（含 canonical 与同义词）。"""
    seen: list[str] = []
    for canonical, synonyms in tag_pairs:
        for tag in (canonical, *synonyms):
            if tag and tag not in seen:
                seen.append(tag)
    return seen


def mtime_iso(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def read_frontmatter_block(path: Path, limit: int = 2000) -> str:
    """读取 frontmatter 块（首行 `---` 到闭合 `---`）。

    适用于包含元素列表很长的章节/小说索引；找不到闭合标记时回退到
    前 limit 行。始终只读。
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
            if first.strip() != "---":
                return ""
            collected: list[str] = [first.rstrip("\r\n")]
            for _ in range(limit):
                line = handle.readline()
                if not line:
                    break
                collected.append(line.rstrip("\r\n"))
                if line.strip() == "---":
                    break
            return "\n".join(collected)
    except OSError:
        return ""


def read_first_lines(path: Path, limit: int = 80) -> str:
    """读取文件前若干行（用于快速解析 frontmatter，避免读整本原文）。"""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            collected: list[str] = []
            for _ in range(limit):
                line = handle.readline()
                if not line:
                    break
                collected.append(line.rstrip("\r\n"))
        return "\n".join(collected)
    except OSError:
        return ""
