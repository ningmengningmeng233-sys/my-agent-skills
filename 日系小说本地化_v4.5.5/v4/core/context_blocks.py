"""可审计的上下文块模型与保守 Token 估算。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib


def estimate_tokens(text: str) -> int:
    """无额外依赖的保守估算；中日韩文本按约 1.5 字符/Token 估计。"""
    value = str(text or "")
    cjk = sum(1 for ch in value if "\u3000" <= ch <= "\u9fff")
    latin = len(value) - cjk
    return max(1, int(cjk / 1.5 + latin / 4) + 1)


@dataclass(frozen=True, slots=True)
class ContextBlock:
    block_id: str
    kind: str
    authority: str
    priority: int
    pinned: bool
    content: str
    source_ids: tuple[str, ...] = ()
    sha256: str = ""
    estimated_tokens: int = 0

    def __post_init__(self) -> None:
        content = self.content or ""
        if not self.sha256:
            object.__setattr__(self, "sha256", hashlib.sha256(content.encode("utf-8")).hexdigest())
        if not self.estimated_tokens:
            object.__setattr__(self, "estimated_tokens", estimate_tokens(content))
