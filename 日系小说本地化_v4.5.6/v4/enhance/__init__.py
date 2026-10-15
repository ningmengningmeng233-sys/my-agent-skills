"""v4 可选增强层：只读参考资产适配器。

分层：
- index_lib.py      用户索引库只读适配（章节索引/小说索引/文风卡/原文/判官验证）
- writing_guide.py  写作指导只读适配（专项模块结构规则 + 语料正反例）
- reference_audit.py 增强引用审计（enhance_use / enhance_unavailable）

增强层只提供风格/结构/节奏参考，不参与 Core 事实硬门槛。
"""

from . import index_lib, reference_audit, writing_guide, providers, sqlite_fts_provider
from .reference_audit import (
    ASSET_KINDS,
    BORROWED_LAYERS,
    PURPOSES,
    EnhanceAuditError,
    record_enhance_skipped,
    record_enhance_unavailable,
    record_enhance_use,
)

__all__ = [
    "index_lib",
    "writing_guide",
    "reference_audit",
    "providers",
    "sqlite_fts_provider",
    "record_enhance_use",
    "record_enhance_unavailable",
    "record_enhance_skipped",
    "EnhanceAuditError",
    "ASSET_KINDS",
    "PURPOSES",
    "BORROWED_LAYERS",
]
