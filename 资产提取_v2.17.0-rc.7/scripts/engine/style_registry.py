#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语义风格登记（规格 5）：用户面向中文正式名称 + 兼容别名 + 生命周期（工单02-Task1）。

设计要点：
- 数据在 `references/styles/registry.toml`（由 `references/画风库.md` 全量派生，
  一致性由测试钉死）；卡片正文仍以画风库为兼容内容源，本层只负责治理与命名。
- 用户使用中文正式名称选择风格；`A1`/`H7` 等内部编号只在兼容映射与诊断中出现。
- `resolve_style_record` 只做**精确**解析（编号 / 正式名 / 别名），命中多个即抛
  `STYLE_ALIAS_CONFLICT`，禁止「第一个命中」。
- 前缀/自由描述等模糊匹配仍由 `engine.style.find_card_by_name` 承担（兼容层，
  沿用其既有优先级），本层不重复实现，避免与既有简写规则冲突。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10 回退（requirements 中的条件依赖）
    import tomli as tomllib

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
STYLES_DIR = SKILL_DIR / "references" / "styles"
REGISTRY_PATH = STYLES_DIR / "registry.toml"
CONTENT_SUBDIR = "private"


def version_dir_for(style_name: str, version: str) -> Path:
    """版本目录：references/styles/<中文正式名>/<版本>/。"""
    return STYLES_DIR / style_name / version


def pack_root(style_name: str, version: str) -> Path:
    """风格包内容根：<版本>/private/（清单路径相对此处解析）。"""
    return version_dir_for(style_name, version) / CONTENT_SUBDIR


# 验证等级（规格 5）：只有用视觉母版重建并可回溯的风格才是 omni_verified
OMNI_VERIFIED = "omni_verified"
LEGACY_UNVERIFIED = "legacy_unverified"

# 生命周期（规格 10）：active → deprecated → disabled → removable
LIFECYCLE = ("active", "deprecated", "disabled", "removable")
DEFAULT_VISIBLE = ("active",)
LEGACY_VISIBLE = ("deprecated",)      # include_legacy=True 时才纳入

KIND_STYLE = "style"

STYLE_ALIAS_CONFLICT = "STYLE_ALIAS_CONFLICT"


class StyleAliasConflict(ValueError):
    """同一取值命中多个风格登记项，禁止自动取第一个。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def _norm(value: str | None) -> str:
    """归一化查询键：小写、去空格（含全角空格）、去括号后缀的写法差异。"""
    return re.sub(r"[\s\u3000]+", "", (value or "").strip()).lower()


@dataclass(frozen=True)
class StyleRecord:
    """一个风格的登记项（不可变，防止治理信息被就地改写）。"""

    id: str
    name: str
    aliases: tuple[str, ...] = ()
    status: str = "active"
    validation_level: str = LEGACY_UNVERIFIED
    pack_slug: str = ""
    pack_version: str = ""
    pack_hash: str = ""
    shareable: bool = True
    kind: str = KIND_STYLE

    @property
    def enabled(self) -> bool:
        """deprecated 仍可用（仅提示），disabled/removable 视为不可用。"""
        return self.status in DEFAULT_VISIBLE + LEGACY_VISIBLE

    @property
    def user_label(self) -> str:
        """用户可见标签：中文正式名；旧版未验证的显式标注，绝不显示内部编号。"""
        if self.validation_level == LEGACY_UNVERIFIED:
            return f"{self.name}（旧版未验证）"
        return self.name


@dataclass(frozen=True)
class StyleRegistry:
    """风格登记表：查询、生命周期过滤与别名冲突判定。"""

    records: tuple[StyleRecord, ...] = ()
    schema_version: int = 1
    path: str = ""

    def by_id(self, style_id: str) -> StyleRecord | None:
        key = _norm(style_id)
        for record in self.records:
            if _norm(record.id) == key:
                return record
        return None

    def all_styles(self) -> list[StyleRecord]:
        return list(self.records)

    def user_styles(self, include_legacy: bool = False) -> list[StyleRecord]:
        """用户可选风格：滤掉速查表与停用/可删除项，保留中文正式名。

        include_legacy=True 时才纳入已弃用（deprecated）风格；disabled/removable
        永不出现在用户列表（只能通过管理命令查看）。
        """
        allowed = DEFAULT_VISIBLE + (LEGACY_VISIBLE if include_legacy else ())
        return [r for r in self.records if r.kind == KIND_STYLE and r.status in allowed]

    def resolve(self, value: str) -> StyleRecord | None:
        """精确解析：编号 → 正式名 → 别名。命中多个同名/同别名即抛冲突。"""
        key = _norm(value)
        if not key:
            return None
        for tier, getter in (("编号", lambda r: (r.id,)),
                             ("正式名", lambda r: (r.name,)),
                             ("别名", lambda r: tuple(r.aliases))):
            hits = [r for r in self.records if any(_norm(c) == key for c in getter(r) if c)]
            if len(hits) > 1:
                detail = f"{value!r} 同时命中多个风格{tier}：" + "、".join(
                    f"{r.id}（{r.name}）" for r in hits)
                raise StyleAliasConflict(STYLE_ALIAS_CONFLICT, detail)
            if hits:
                return hits[0]
        return None


def _record_from_row(row: dict) -> StyleRecord:
    aliases = row.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    status = str(row.get("status") or "active").strip()
    if status not in LIFECYCLE:
        raise ValueError(f"登记表生命周期非法: {row.get('id')!r} → {status!r}（只允许 {list(LIFECYCLE)}）")
    return StyleRecord(
        id=str(row.get("id") or "").strip(),
        name=str(row.get("name") or "").strip(),
        aliases=tuple(str(a) for a in aliases if str(a).strip()),
        status=status,
        validation_level=str(row.get("validation_level") or LEGACY_UNVERIFIED).strip(),
        pack_slug=str(row.get("pack_slug") or "").strip(),
        pack_version=str(row.get("pack_version") or "").strip(),
        pack_hash=str(row.get("pack_hash") or "").strip(),
        shareable=bool(row.get("shareable", True)),
        kind=str(row.get("kind") or KIND_STYLE).strip(),
    )


def load_registry(path: Path | None = None) -> StyleRegistry:
    """读取登记表；缺文件返回空登记表（调用方按需降级，不伪造记录）。"""
    target = Path(path) if path else REGISTRY_PATH
    if not target.is_file():
        return StyleRegistry(records=(), path=str(target))
    with open(target, "rb") as fh:
        data = tomllib.load(fh)
    rows = data.get("styles") or []
    return StyleRegistry(
        records=tuple(_record_from_row(r) for r in rows if isinstance(r, dict)),
        schema_version=int(data.get("schema_version") or 1),
        path=str(target),
    )


def resolve_style_record(value: str, registry: StyleRegistry | None = None) -> StyleRecord | None:
    """按编号/中文正式名/别名精确解析风格；未命中返回 None，冲突抛 StyleAliasConflict。"""
    reg = registry if registry is not None else load_registry()
    return reg.resolve(value)


def list_user_styles(include_legacy: bool = False,
                     registry: StyleRegistry | None = None) -> list[StyleRecord]:
    """用户可选风格列表（中文正式名；含旧版未验证但排除停用与速查表）。"""
    reg = registry if registry is not None else load_registry()
    return reg.user_styles(include_legacy=include_legacy)


def list_all_styles(registry: StyleRegistry | None = None) -> list[StyleRecord]:
    """全部登记项（含速查表与停用项，供管理命令与诊断使用）。"""
    reg = registry if registry is not None else load_registry()
    return reg.all_styles()


def style_name_report(registry: StyleRegistry | None = None) -> list[dict]:
    """全库中文映射报告（规格 5）：正式名 / 旧编号 / 验证等级 / 生命周期。"""
    reg = registry if registry is not None else load_registry()
    return [
        {
            "正式名称": r.name,
            "旧编号": r.id,
            "别名": list(r.aliases),
            "验证等级": r.validation_level,
            "生命周期": r.status,
            "类型": r.kind,
            "风格包": f"{r.pack_slug}@{r.pack_version}" if r.pack_slug else "",
            "可分享": r.shareable,
        }
        for r in iter_records(reg.all_styles())
    ]


def iter_records(records: Iterable[StyleRecord]) -> Sequence[StyleRecord]:
    """稳定排序的登记项序列（按编号族与序号），供报告与清单使用。"""
    def key(record: StyleRecord):
        m = re.match(r"^([A-Z])(\d+)$", record.id)
        return (m.group(1), int(m.group(2))) if m else (record.id, 0)

    return tuple(sorted(records, key=key))


# ---------------------------------------------------------------------------
# 生命周期与引用扫描（规格 10，工单04-Task2）
# ---------------------------------------------------------------------------

class LifecycleError(ValueError):
    """风格生命周期操作被拒：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def set_style_status(registry_path: Path, style_name: str, target: str) -> str:
    """推进风格生命周期（单向：active→deprecated→disabled→removable）。

    只改登记表本身，**不触碰任何项目文件**；越级跳转一律拒绝。
    """
    path = Path(registry_path)
    if target not in LIFECYCLE:
        raise LifecycleError("ILLEGAL_STATUS", f"未知生命周期 {target!r}")
    registry = load_registry(path)
    record = registry.resolve(style_name)
    if record is None:
        raise LifecycleError("STYLE_UNKNOWN", f"风格 {style_name!r} 不在登记表中")
    current_index = LIFECYCLE.index(record.status)
    target_index = LIFECYCLE.index(target)
    if target_index <= current_index:
        raise LifecycleError("STATUS_NOT_FORWARD",
                             f"{record.name} 当前 {record.status}，不能回到 {target}")
    if target_index != current_index + 1:
        raise LifecycleError("ILLEGAL_LIFECYCLE_JUMP",
                             f"{record.name} 当前 {record.status}，只能前进到 "
                             f"{LIFECYCLE[current_index + 1]}，不能直接到 {target}")
    text = path.read_text(encoding="utf-8")
    blocks = text.split("[[styles]]")
    out = [blocks[0]]
    for block in blocks[1:]:
        if f'id = "{record.id}"' in block:
            block = re.sub(r'status = "[^"]*"', f'status = "{target}"', block, count=1)
        out.append(block)
    path.write_text("[[styles]]".join(out), encoding="utf-8", newline="\n")
    return target


def scan_style_usage(root: Path, style_id: str, pack_hash: str = "") -> list[dict]:
    """扫描项目对某风格的引用（只读）：启动状态、交付清单、视觉计划与视觉交付清单。

    pack_hash 非空时只统计哈希一致的项目（母版已升级的项目不算旧风格引用）。
    """
    import json

    root = Path(root)
    hits: list[dict] = []
    candidates = [
        (".start-state.json", "启动状态", lambda d: str(d.get("style") or "")),
        ("交付清单.json", "交付清单", lambda d: str(d.get("style") or "")),
        ("视觉资产/计划.toml", "视觉计划", None),
        ("视觉资产/视觉交付清单.json", "视觉交付清单", lambda d: str(d.get("style_slug") or "")),
    ]
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        name = Path(rel).name
        for suffix, source, getter in candidates:
            if not rel.endswith(suffix):
                continue
            text = ""
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if name.endswith(".toml"):
                if (f'style_slug = "{style_id}"' not in text
                        and f'style_name = "{style_id}"' not in text):
                    continue
                digest = _field(text, "style_hash")
                if pack_hash and digest != pack_hash:
                    continue
                project = rel.split("/" + suffix.split("/")[0])[0] or "."
                hits.append({"project": project, "source": source, "path": rel,
                             "style_id": style_id,
                             "style_version": _field(text, "style_version"),
                             "pack_hash": digest, "last_used": _mtime(path)})
                continue
            try:
                data = json.loads(text)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            value = getter(data) if getter else ""
            if value != style_id:
                continue
            digest = str(data.get("style_fingerprint") or data.get("style_hash") or "")
            if pack_hash and digest and digest != pack_hash:
                continue
            project = rel[:-len(suffix)] or "."
            hits.append({"project": project.rstrip("/") or ".", "source": source, "path": rel,
                         "style_id": style_id, "style_version": str(data.get("style_version") or ""),
                         "pack_hash": digest, "last_used": _mtime(path)})
    return hits


def _field(text: str, key: str) -> str:
    m = re.search(rf'^{key} = "([^"]*)"', text, re.M)
    return m.group(1) if m else ""


def _mtime(path: Path) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""
