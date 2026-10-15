"""v4 Enhance · 写作指导只读适配器。

解析顺序（优先级从高到低）：
    CLI 参数 --writing-guide > 工作区配置 > 环境/`.env` WRITING_GUIDE_DIR > 默认可发现路径 > 不启用

职责：
- 吸收专项模块（consistency_review / plot_logic / character_consistency /
  anti_ai_voice / transition / dialogue / chapter_ending）的 runtime/README
  结构规则（触发信号、分层诊断、硬规则、计划字段、例库映射）；
- 检索 analysis/excerpts.csv 与模块例库：2-4 个正例 + 1-2 个反例；
- 每条引用可携带来源 SHA-256、归一化路径与回查状态；
- 全程只读，只做结构/节奏/信息投放参考，不参与 Core 事实门槛。
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ._util import (
    bounded,
    dotenv_values,
    looks_like_writing_guide,
    now_iso,
)

MODULES: tuple[str, ...] = (
    "consistency_review",
    "plot_logic",
    "character_consistency",
    "anti_ai_voice",
    "transition",
    "dialogue",
    "chapter_ending",
)

_MODULE_DIR = "references/modules"
_ANALYSIS_DIR = "analysis"
_DATA_DIR = "data/articles"

_HEADING2 = re.compile(r"^##\s+(.+)$")
_HEADING3 = re.compile(r"^###\s+(.+)$")
_BULLET = re.compile(r"^[-*]\s+(.*)$")
_LAYER = re.compile(r"^第\s*\d+\s*层[:：]?\s*(.+)$")
_GROUP = re.compile(r"^#{0,4}\s*([A-E])\s*组[:：]?\s*(.+)$")
_ITEM = re.compile(r"^#{0,4}\s*[A-Z]+-[GB]\d+\s+(.+)$")
_NUMBERED_SUB = re.compile(r"^\d+[.、．]\s*(.+)$")


@dataclass(frozen=True)
class AssetFileRecord:
    rel_path: str
    absolute_path: Path
    sha256: str
    size: int = 0


@dataclass
class ModuleRules:
    """专项模块结构规则（runtime/README 吸收结果）。"""

    module: str
    title: str
    triggers: list[str] = field(default_factory=list)
    layers: list[dict[str, Any]] = field(default_factory=list)
    hard_rules: list[str] = field(default_factory=list)
    plan_fields: list[str] = field(default_factory=list)
    example_groups: list[str] = field(default_factory=list)
    reading_order: list[str] = field(default_factory=list)
    files: dict[str, AssetFileRecord] = field(default_factory=dict)


@dataclass(frozen=True)
class ExampleItem:
    """一条正例/反例（受限片段 + 来源定位）。"""

    group: str
    title: str
    snippet: str
    lesson: str
    source_rel_path: str
    is_negative: bool
    location: str = ""


@dataclass
class ExampleSelection:
    """检索结果：正例 + 反例。"""

    positive: list[ExampleItem] = field(default_factory=list)
    negative: list[ExampleItem] = field(default_factory=list)

    def as_audit_payload(self) -> dict[str, Any]:
        return {
            "positive": [
                {"title": item.title, "group": item.group, "location": item.location}
                for item in self.positive
            ],
            "negative": [
                {"title": item.title, "group": item.group, "location": item.location}
                for item in self.negative
            ],
        }


def _env_var(key: str) -> str | None:
    import os

    return os.environ.get(key)


def _workspace_config(workspace: Path | None, key: str) -> str | None:
    if workspace is None:
        return None
    for env_name in (".enhance.env", ".env"):
        values = dotenv_values(workspace / env_name)
        if key in values:
            return values[key]
    return None


def _project_envs(key: str, base: Path) -> list[str]:
    found: list[str] = []
    for candidate in (base / "v4" / ".env", base / ".env", base.parent / ".env"):
        values = dotenv_values(candidate)
        if key in values and values[key]:
            found.append(values[key])
    return found


def resolve_writing_guide(
    cli_arg: str | Path | None = None,
    *,
    workspace: Path | None = None,
    env: Mapping[str, str] | None = None,
    base: Path | None = None,
    default_candidates: Sequence[str | Path] = (),
) -> Path | None:
    """按 `CLI > 工作区 > env/.env > 默认发现` 解析写作指导目录。"""
    if cli_arg:
        return Path(cli_arg).expanduser()
    workspace_value = _workspace_config(workspace, "WRITING_GUIDE_DIR")
    if workspace_value:
        return Path(workspace_value).expanduser()
    if env and env.get("WRITING_GUIDE_DIR"):
        return Path(env["WRITING_GUIDE_DIR"]).expanduser()
    base_dir = Path(base) if base else Path(__file__).resolve().parents[2]
    for value in _project_envs("WRITING_GUIDE_DIR", base_dir):
        if value:
            return Path(value).expanduser()
    for candidate in default_candidates:
        path = Path(candidate).expanduser()
        if looks_like_writing_guide(path):
            return path
    return None


def validate_writing_guide(root: Path) -> str | None:
    if not root.exists():
        return f"写作指导目录不存在: {root}"
    if not root.is_dir():
        return f"写作指导路径不是目录: {root}"
    if not looks_like_writing_guide(root):
        return f"目录缺少 references/modules: {root}"
    return None


class WritingGuide:
    """写作指导只读适配器。构造后 `scan()` 建立模块与语料记录。"""

    def __init__(self, root: Path | str | None, *, base: Path | None = None) -> None:
        self.root = Path(root) if isinstance(root, str) else root
        self._base = base or Path(__file__).resolve().parents[2]
        self._module_cache: dict[str, ModuleRules] = {}
        self._excerpt_rows: list[dict[str, str]] | None = None
        self._scanned_at = now_iso()

    # ---------------------------------------------------------------- 扫描

    def scan(self) -> list[AssetFileRecord]:
        """扫描专项模块文件与语料 CSV，记录 SHA-256。"""
        self._module_cache.clear()
        self._excerpt_rows = None
        records: list[AssetFileRecord] = []
        if self.root is None or not self.root.is_dir():
            return records
        modules_dir = self.root / _MODULE_DIR
        if modules_dir.is_dir():
            for module in MODULES:
                module_dir = modules_dir / module
                if not module_dir.is_dir():
                    continue
                for file_name in ("runtime.md", "README.md", "good_examples.md", "bad_examples.md", "tutorial.md", "source_index.md"):
                    path = module_dir / file_name
                    if path.is_file():
                        records.append(self._file_record(f"{_MODULE_DIR}/{module}/{file_name}", path))
        for analysis_file in ("excerpts.csv", "article_profiles.csv", "imitation_index.md"):
            path = self.root / _ANALYSIS_DIR / analysis_file
            if path.is_file():
                records.append(self._file_record(f"{_ANALYSIS_DIR}/{analysis_file}", path))
        return records

    def _file_record(self, rel_path: str, path: Path) -> AssetFileRecord:
        size = 0
        try:
            size = path.stat().st_size
        except OSError:
            pass
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            pass
        return AssetFileRecord(
            rel_path=rel_path,
            absolute_path=path,
            sha256=digest.hexdigest(),
            size=size,
        )

    # ---------------------------------------------------------------- 模块规则

    def module_rules(self, module: str) -> ModuleRules:
        """解析专项模块 runtime.md + README.md 为结构化规则（缓存）。"""
        if module in self._module_cache:
            return self._module_cache[module]
        if module not in MODULES:
            raise KeyError(f"未知写作指导模块: {module}")
        rules = ModuleRules(module=module, title=module)
        base = self.root / _MODULE_DIR / module if self.root else None
        if base and base.is_dir():
            for name in ("runtime.md", "README.md", "good_examples.md", "bad_examples.md", "tutorial.md", "source_index.md"):
                path = base / name
                if path.is_file():
                    rules.files[name] = self._file_record(f"{_MODULE_DIR}/{module}/{name}", path)
            runtime_path = base / "runtime.md"
            readme_path = base / "README.md"
            if runtime_path.is_file():
                self._absorb_runtime(rules, _read_text(runtime_path))
            if readme_path.is_file():
                self._absorb_readme(rules, _read_text(readme_path))
        self._module_cache[module] = rules
        return rules

    def all_module_rules(self) -> list[ModuleRules]:
        return [self.module_rules(module) for module in MODULES]

    def _absorb_runtime(self, rules: ModuleRules, text: str) -> None:
        section: str | None = None
        current_layer: dict[str, Any] | None = None
        current_signals: list[str] = []
        current_fixes: list[str] = []
        layer_title: str | None = None
        collect_mode: str = "signals"  # "signals" | "fixes"
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            heading2 = _HEADING2.match(stripped)
            if heading2:
                if current_layer:
                    self._close_layer(rules, current_layer, layer_title, current_signals, current_fixes)
                    current_layer, layer_title, current_signals, current_fixes = None, None, [], []
                section = heading2.group(1)
                collect_mode = "signals"
                continue
            heading3 = _HEADING3.match(stripped)
            if heading3:
                if current_layer:
                    self._close_layer(rules, current_layer, layer_title, current_signals, current_fixes)
                    current_layer, layer_title, current_signals, current_fixes = None, None, [], []
                layer_match = _LAYER.match(heading3.group(1))
                # 允许 "### 1. 剧情逻辑一致性" 这类必查项作为分层（consistency_review）
                numbered = _NUMBERED_SUB.match(heading3.group(1))
                if layer_match or (numbered and section and ("项" in section or "必查" in section)):
                    layer_title = heading3.group(1).strip()
                    current_layer = {"title": layer_title, "signals": [], "fixes": []}
                    current_signals, current_fixes = [], []
                    collect_mode = "signals"
                continue
            if section is None:
                continue
            if current_layer is not None:
                if "典型表现" in stripped:
                    collect_mode = "signals"
                    continue
                if "优先" in stripped and any(
                    marker in stripped for marker in ("修", "补", "考虑", "用")
                ):
                    collect_mode = "fixes"
                    continue
                if stripped.startswith("-"):
                    (current_fixes if collect_mode == "fixes" else current_signals).append(
                        stripped.lstrip("- ").strip()
                    )
                    continue
            if "什么时候" in section or "调用" in section:
                bullet = _BULLET.match(stripped)
                if bullet:
                    rules.triggers.append(bullet.group(1))
            elif "硬规则" in section:
                bullet = _BULLET.match(stripped)
                if bullet:
                    rules.hard_rules.append(bullet.group(1))
            elif "计划" in section and "动笔" in section:
                bullet = _BULLET.match(stripped)
                if bullet:
                    rules.plan_fields.append(bullet.group(1))
            elif "例库映射" in section or "素材" in section:
                bullet = _BULLET.match(stripped)
                if bullet and (bullet.group(1).startswith("正例") or bullet.group(1).startswith("反例") or "组" in bullet.group(1)):
                    rules.example_groups.append(bullet.group(1))
        if current_layer:
            self._close_layer(rules, current_layer, layer_title, current_signals, current_fixes)

    @staticmethod
    def _close_layer(
        rules: ModuleRules,
        layer: dict[str, Any],
        title: str | None,
        signals: list[str],
        fixes: list[str],
    ) -> None:
        layer["title"] = title or layer["title"]
        layer["signals"] = signals
        layer["fixes"] = fixes
        rules.layers.append(layer)

    def _absorb_readme(self, rules: ModuleRules, text: str) -> None:
        section: str | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            heading2 = _HEADING2.match(stripped)
            if heading2:
                section = heading2.group(1)
                continue
            if section is None:
                continue
            if "阅读顺序" in section or "入口" in section:
                bullet = _BULLET.match(stripped)
                if bullet:
                    rules.reading_order.append(bullet.group(1))
            elif "解决" in section or "什么时候" in section:
                bullet = _BULLET.match(stripped)
                if bullet and bullet.group(1) not in rules.triggers:
                    rules.triggers.append(bullet.group(1))

    # ---------------------------------------------------------------- 例库检索

    def retrieve_examples(
        self,
        module: str,
        query: str,
        *,
        positive_n: int = 4,
        negative_n: int = 2,
    ) -> ExampleSelection:
        """从专项模块 good/bad_examples.md 检索 2-4 正例与 1-2 反例。

        按关键词与组/标题/该学什么/问题文本匹配；返回受限片段。
        """
        if self.root is None:
            return ExampleSelection()
        base = self.root / _MODULE_DIR / module
        good_path = base / "good_examples.md"
        bad_path = base / "bad_examples.md"
        positive = (
            _parse_example_file(good_path, is_negative=False) if good_path.is_file() else []
        )
        negative = (
            _parse_example_file(bad_path, is_negative=True) if bad_path.is_file() else []
        )
        tokens = _tokenize(query)
        scored_pos = _score_examples(positive, tokens)
        scored_neg = _score_examples(negative, tokens)
        return ExampleSelection(
            positive=[item for _, item in scored_pos[:positive_n]],
            negative=[item for _, item in scored_neg[:negative_n]],
        )

    def retrieve_corpus(
        self,
        query: str = "",
        *,
        excerpt_type: str | None = None,
        positive_n: int = 4,
        negative_n: int = 2,
    ) -> ExampleSelection:
        """从 analysis/excerpts.csv 检索 2-4 正例与 1-2 反例。

        正例 = 标签/文本命中度最高的摘录；反例 = 同题材但类型不符或命中度最低的
        摘录（用于展示“什么不该学”）。所有 snippet 均受限截断。
        """
        rows = self._load_excerpts()
        if not rows:
            return ExampleSelection()
        tokens = _tokenize(query)
        scored: list[tuple[float, dict[str, str]]] = []
        for row in rows:
            score = _score_excerpt(row, tokens)
            if excerpt_type and row.get("excerpt_type") != excerpt_type:
                score -= 3.0
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        positive: list[ExampleItem] = []
        negative: list[ExampleItem] = []
        for score, row in scored:
            if score > 0 and len(positive) < positive_n:
                positive.append(_excerpt_to_item(row))
        # 反例优先取明确不匹配项；不足时从低分（仍可能 >0）补齐，保证 1-2 条
        negative_rows = [row for score, row in scored if score <= 0]
        if len(negative_rows) < negative_n:
            negative_rows = [row for _, row in scored[-negative_n:]]
        for row in negative_rows[:negative_n]:
            if len(negative) >= negative_n:
                break
            negative.append(_excerpt_to_item(row))
        return ExampleSelection(positive=positive, negative=negative)

    def _load_excerpts(self) -> list[dict[str, str]]:
        if self._excerpt_rows is not None:
            return self._excerpt_rows
        if self.root is None:
            return []
        path = self.root / _ANALYSIS_DIR / "excerpts.csv"
        if not path.is_file():
            return []
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            self._excerpt_rows = [dict(row) for row in reader]
        return self._excerpt_rows

    def recheck_excerpt_context(self, row: dict[str, str]) -> bool:
        """回查摘录原文：data/articles 中存在且段落范围不越界。"""
        if self.root is None:
            return False
        file_path = row.get("file_path", "")
        try:
            para_start = int(row.get("para_start", "0"))
            para_end = int(row.get("para_end", "0"))
        except ValueError:
            return False
        source = self.root / file_path
        if not source.is_file():
            return False
        count = 0
        try:
            with source.open(encoding="utf-8", errors="replace") as handle:
                for _ in handle:
                    count += 1
        except OSError:
            return False
        return 1 <= para_start <= para_end <= count

    def corpus_asset_record(self) -> AssetFileRecord | None:
        if self.root is None:
            return None
        path = self.root / _ANALYSIS_DIR / "excerpts.csv"
        if not path.is_file():
            return None
        return self._file_record(f"{_ANALYSIS_DIR}/excerpts.csv", path)


# ---------------------------------------------------------------- 内部工具


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tokenize(query: str) -> list[str]:
    return [token for token in re.split(r"[\s，。、,.;；:：/\\|]+", query) if token]


def _score_excerpt(row: dict[str, str], tokens: list[str]) -> float:
    haystack = " ".join(
        [
            row.get("tags", ""),
            row.get("title", ""),
            row.get("excerpt_type", ""),
            row.get("text", "")[:200],
        ]
    )
    score = 0.0
    for token in tokens:
        if token in row.get("tags", ""):
            score += 3.0
        if token in row.get("title", ""):
            score += 1.5
        if token in haystack:
            score += 1.0
    return score


def _score_examples(
    items: list[ExampleItem], tokens: list[str]
) -> list[tuple[float, ExampleItem]]:
    scored: list[tuple[float, ExampleItem]] = []
    for item in items:
        score = 0.0
        haystack = f"{item.group} {item.title} {item.lesson} {item.snippet}"
        for token in tokens:
            if token in haystack:
                score += 1.0
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def _parse_example_file(path: Path, *, is_negative: bool) -> list[ExampleItem]:
    text = _read_text(path)
    items: list[ExampleItem] = []
    group = ""
    current_item: dict[str, str] | None = None
    code_lines: list[str] = []
    in_code = False
    lesson_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        group_match = _GROUP.match(stripped)
        if group_match:
            group = f"{group_match.group(1)} 组"
            continue
        item_match = _ITEM.match(stripped)
        if item_match and stripped.startswith("###"):
            if current_item:
                items.append(
                    ExampleItem(
                        group=group,
                        title=current_item.get("title", ""),
                        snippet=bounded("\n".join(code_lines[:4]), 120),
                        lesson="；".join(lesson_lines),
                        source_rel_path=path.name,
                        is_negative=is_negative,
                        location=current_item.get("title", ""),
                    )
                )
            title = (item_match.group(1) or "").strip()
            current_item = {"title": title}
            code_lines = []
            lesson_lines = []
            in_code = False
            continue
        if current_item is None:
            continue
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(stripped)
            continue
        if stripped.startswith("- 该学什么"):
            lesson_lines.append(stripped.lstrip("- 该学什么：:").strip())
        elif stripped.startswith("问题"):
            lesson_lines.append(stripped.split("问题", 1)[-1].lstrip("：:").strip())
    if current_item:
        items.append(
            ExampleItem(
                group=group,
                title=current_item.get("title", ""),
                snippet=bounded("\n".join(code_lines[:4]), 120),
                lesson="；".join(lesson_lines),
                source_rel_path=path.name,
                is_negative=is_negative,
                location=current_item.get("title", ""),
            )
        )
    return items


def _excerpt_to_item(row: dict[str, str]) -> ExampleItem:
    return ExampleItem(
        group=row.get("excerpt_type", ""),
        title=row.get("title", ""),
        snippet=bounded(row.get("text", ""), 120),
        lesson=f"标签: {row.get('tags', '')}",
        source_rel_path=row.get("file_path", ""),
        is_negative=False,
        location=f"段落 {row.get('para_start', '?')}-{row.get('para_end', '?')}",
    )
