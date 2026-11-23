"""v4 Enhance 参考审计：向工作区 audit.jsonl 追加增强使用/不可用记录。

只写工作区 audit.jsonl（追加式，不修改历史），绝不写回外部索引库/写作指导。
增强结果只作参考，不能替代 Core 事实硬门槛。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..core.audit import append_audit

ASSET_KINDS = frozenset(
    {
        "index_chapter",     # 章节索引
        "index_original",    # 索引库原文
        "style_card",        # 文风卡/场景卡
        "style_exemplar",    # 文风范例：真实正文片段，供模型学"笔触"（配防抄门）
        "writing_module",    # 写作指导专项模块
        "corpus_excerpt",    # 写作指导语料摘录
        "corpus_original",   # 写作指导语料原文
    }
)

PURPOSES = frozenset(
    {
        "scene",            # 场景
        "dialogue",         # 对白
        "transition",       # 转场
        "chapter_ending",   # 章末
        "consistency",      # 一致性
        "anti_ai_voice",    # 去 AI 味
        "mapping",          # 映射/命名
        "structure",        # 结构/节奏提炼
        "style_texture",    # 笔触/质感范例
        "negative_example", # 反例/禁止清单
    }
)

BORROWED_LAYERS = frozenset(
    {
        "structure",         # 结构
        "rhythm",            # 节奏
        "information_flow",  # 信息投放
        "diagnostic",        # 诊断/检查框架
    }
)

_SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")

# ── 加载失败分类（追加裁决 1）────────────────────────────────────────────
#
# 为什么要这一层：`cmd_rewrite` 原先用一个 `except Exception` 把整段增强加载兜住，
# 无论「库真没有」「读不了」「文件坏了」还是「代码炸了」，最后都报同一句
# 「索引库未找到」——一次并发 I/O 抖动就被读成「用户没配库」，排查方向整个错掉。
# 分类只保留**类别 + 异常类型名**：异常原文常带私密路径、凭据形状或正文片段，
# 那些东西既不进终端也不进审计，所以这里刻意不 `str(exc)`。
LOAD_KIND_MISSING = "缺失"
LOAD_KIND_UNREADABLE = "读取失败"
LOAD_KIND_CORRUPT = "格式损坏"
LOAD_KIND_OTHER = "加载异常"

def _corrupt_types() -> tuple:
    """格式损坏的异常族。延迟取，避免为了一个归类而硬依赖可选解析器。"""
    import json
    import xml.etree.ElementTree as ET

    types: list = [json.JSONDecodeError, ET.ParseError,
                   UnicodeDecodeError, UnicodeTranslateError]
    try:  # yaml 不是硬依赖
        import yaml

        types.append(yaml.YAMLError)
    except ImportError:
        pass
    return tuple(types)


def classify_load_failure(exc: BaseException) -> str:
    """异常 → 「类别:异常类型名」。不回显异常原文。"""
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        kind = LOAD_KIND_MISSING
    elif isinstance(exc, _corrupt_types()):
        kind = LOAD_KIND_CORRUPT
    elif isinstance(exc, OSError):
        kind = LOAD_KIND_UNREADABLE
    else:
        kind = LOAD_KIND_OTHER
    return f"{kind}:{type(exc).__name__}"


def classify_validate_reason(reason: str) -> str:
    """`validate_*` 的说明文字 → 类别（说明文字里带路径，只能取类别）。"""
    if "不存在" in reason or "不是目录" in reason:
        return LOAD_KIND_MISSING
    if "缺少" in reason:
        return LOAD_KIND_CORRUPT
    return LOAD_KIND_OTHER


class EnhanceAuditError(ValueError):
    pass


def _validate_sha256(value: str) -> None:
    if not _SHA256_RE.match(value):
        raise EnhanceAuditError(f"asset_sha256 必须是完整 64 位十六进制，收到: {value!r}")


def _validate_enum(field: str, value: Any, allowed: frozenset[str]) -> None:
    if value not in allowed:
        choices = "|".join(sorted(allowed))
        raise EnhanceAuditError(f"{field} 必须是 {choices} 之一，收到: {value!r}")


def record_enhance_use(
    audit_path: Path,
    *,
    chapter_id: str,
    asset_kind: str,
    asset_path: str,
    asset_sha256: str,
    purpose: str,
    selection_reason: str,
    location: str,
    context_rechecked: bool = True,
    borrowed_layer: str = "structure",
    copied_surface_text: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """追加一条 enhance_use 审计记录。

    参数与 docs/v4-asset-integration.md §6 的字段一致。asset_path 应为
    工作区内归一化路径；asset_sha256 为完整 64 位 SHA-256。
    """
    _validate_enum("asset_kind", asset_kind, ASSET_KINDS)
    _validate_enum("purpose", purpose, PURPOSES)
    _validate_enum("borrowed_layer", borrowed_layer, BORROWED_LAYERS)
    _validate_sha256(asset_sha256)
    record = {
        "chapter_id": chapter_id,
        "asset_kind": asset_kind,
        "asset_path": asset_path,
        "asset_sha256": asset_sha256,
        "purpose": purpose,
        "selection_reason": selection_reason,
        "location": location,
        "context_rechecked": bool(context_rechecked),
        "borrowed_layer": borrowed_layer,
        "copied_surface_text": bool(copied_surface_text),
    }
    record.update(extra)
    append_audit(audit_path, "enhance_use", **record)
    return {"type": "enhance_use", **record}


def record_enhance_unavailable(
    audit_path: Path,
    *,
    chapter_id: str,
    asset_kind: str,
    asset_path: str,
    reason: str,
    **extra: Any,
) -> dict[str, Any]:
    """追加一条 enhance_unavailable 记录（路径不可用/读取失败）。

    不可用时不得写“已使用”，只能记录不可用及原因。
    """
    _validate_enum("asset_kind", asset_kind, ASSET_KINDS)
    record = {
        "chapter_id": chapter_id,
        "asset_kind": asset_kind,
        "asset_path": asset_path,
        "reason": reason,
    }
    record.update(extra)
    append_audit(audit_path, "enhance_unavailable", **record)
    return {"type": "enhance_unavailable", **record}


def record_enhance_skipped(
    audit_path: Path,
    *,
    chapter_id: str,
    reason: str,
    **extra: Any,
) -> dict[str, Any]:
    """追加一条 enhance_skipped 记录（已配置增强资产但本章未使用）。"""
    record = {"chapter_id": chapter_id, "reason": reason}
    record.update(extra)
    append_audit(audit_path, "enhance_skipped", **record)
    return {"type": "enhance_skipped", **record}
