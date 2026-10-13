"""按权威、优先级和 Token 预算编译完整上下文块。

v4.5.0-dev.3：
- ``compile_context``：去重 + pinned/authority/priority 装配，不截断单个块；
- ``compute_prompt_budget``：普通章 1+1 / 复杂章 2+2 的参考资产配额与输入 Token 预算；
- ``PromptBudgetExceeded``：必选块（pinned）合计超预算时抛出，绝不静默截断；
- ``PromptManifest``：一次正式 prompt 运行的不可变元数据（块清单/哈希/预算），
  经 ``save_prompt_manifest`` 原子落盘 ``chapters/<id>/prompts/<run_id>.json``，
  供 checks/vNNN.json 绑定 ``prompt_run_id`` 回放审计。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .jsonio import write_json_atomic
from .context_blocks import ContextBlock


class PromptBudgetExceeded(RuntimeError):
    """必选上下文块合计超过输入 Token 预算。"""


@dataclass(frozen=True, slots=True)
class CompiledContext:
    blocks: tuple[ContextBlock, ...]
    estimated_tokens: int
    excluded_ids: tuple[str, ...]
    compiler_version: str = "1"

    @property
    def text(self) -> str:
        return "\n".join(block.content for block in self.blocks)


def compile_context(blocks: Iterable[ContextBlock], input_budget: int) -> CompiledContext:
    """去重后按 pinned/authority/priority 装配；不截断单个块。

    - pinned 块（必选）永远入选；若全部 pinned 块合计已超预算则由调用方
      （``compile_prompt``）抛 ``PromptBudgetExceeded``；
    - 非 pinned 块按 authority 升序尝试装填，装不下的进入 ``excluded_ids``。
    """
    authority_order = {
        "source": 0,
        "accepted_decision": 1,
        "event_graph": 2,
        "canon": 3,
        "character_state": 4,
        "hard_rule": 5,
        "continuity": 6,
        "style_rule": 7,
        "chapter_index": 8,
        "writing_guide": 9,
        "example": 10,
    }
    unique: dict[str, ContextBlock] = {}
    for block in blocks:
        key = block.sha256 or block.block_id
        unique.setdefault(key, block)
    ordered = sorted(
        unique.values(),
        key=lambda item: (not item.pinned, authority_order.get(item.authority, 99), item.priority, item.block_id),
    )
    selected: list[ContextBlock] = []
    excluded: list[str] = []
    used = 0
    for block in ordered:
        if block.pinned or used + block.estimated_tokens <= max(0, input_budget):
            selected.append(block)
            used += block.estimated_tokens
        else:
            excluded.append(block.block_id)
    return CompiledContext(tuple(selected), used, tuple(excluded))


def compile_prompt(blocks: Iterable[ContextBlock], input_budget: int) -> CompiledContext:
    """带必选块预算校验的编译入口：pinned 合计超预算直接失败（fail-closed）。"""
    block_list = list(blocks)
    pinned_total = sum(block.estimated_tokens for block in block_list if block.pinned)
    if pinned_total > max(0, input_budget):
        raise PromptBudgetExceeded(
            f"必选上下文块合计 {pinned_total} tokens 超过输入预算 {input_budget} tokens；"
            "拒绝静默截断，请缩小章节或提高上下文窗口"
        )
    return compile_context(block_list, input_budget)


_DEFAULT_CONTEXT_WINDOW_TOKENS = 32768
_SAFETY_MARGIN_TOKENS = 1024


@dataclass(frozen=True, slots=True)
class PromptBudget:
    """一次正式改写的 Token 与参考资产配额。"""

    input_budget: int
    output_reserve: int
    context_window: int
    style_card_limit: int
    index_chapter_limit: int
    is_complex: bool
    exemplar_limit: int = 2


def compute_prompt_budget(
    *,
    is_complex: bool,
    output_reserve: int,
    context_window: int = _DEFAULT_CONTEXT_WINDOW_TOKENS,
) -> PromptBudget:
    """普通章 1+1+2（文风卡+章节索引+范例），复杂章 2+2+3；输入预算=窗口-输出预留-安全边际。

    范例配额按 A 方案契约取 2~3 段真实正文：笔触只能从真句子里学，
    单段覆盖面不足，故普通章给 2 段、复杂章给 3 段。单段字符上限由
    ``_EXEMPLAR_MAX_CHARS`` 单独控制，不随配额放大。

    ``output_reserve`` 由调用方按 ``config.max_tokens`` 传入，因此**必须封顶**：
    端点支持 32768 输出、而默认窗口恰好也是 32768 时，不封顶就是把整个窗口
    预留成输出，``input_budget`` 塌成 1024，改写直接失败，并报出方向相反的
    指引「缩小章节或提高上下文窗口」（真实原因是 max_tokens 调得太大）。
    封顶在窗口一半：调大 max_tokens 只抬高输出上限，不反噬输入。
    """
    margin = _SAFETY_MARGIN_TOKENS
    reserve_cap = max(1024, context_window // 2)
    output_reserve = min(max(1024, output_reserve), reserve_cap)
    input_budget = max(1024, context_window - output_reserve - margin)
    if is_complex:
        return PromptBudget(
            input_budget=input_budget,
            output_reserve=output_reserve,
            context_window=context_window,
            style_card_limit=2,
            index_chapter_limit=2,
            is_complex=True,
            exemplar_limit=3,
        )
    return PromptBudget(
        input_budget=input_budget,
        output_reserve=output_reserve,
        context_window=context_window,
        style_card_limit=1,
        index_chapter_limit=1,
        is_complex=False,
        exemplar_limit=2,
    )


@dataclass(frozen=True, slots=True)
class PromptManifest:
    """一次正式 prompt 运行的不可变元数据（可序列化、可回放审计）。"""

    prompt_run_id: str
    chapter_id: str
    compiler_version: str
    input_budget: int
    output_reserve: int
    estimated_tokens: int
    # 配额本身一并记录，使 Manifest 能自证「普通章 1+1 / 复杂章 2+2」合规，
    # 而不必靠数入选参考块反推（清单 §4 核验项）。
    style_card_limit: int
    index_chapter_limit: int
    is_complex: bool
    context_window: int
    excluded_ids: tuple[str, ...]
    blocks: tuple[dict[str, Any], ...]
    paragraph_start: int
    paragraph_end: int
    system_sha256: str
    user_sha256: str
    created_at: str
    exemplar_limit: int = 2

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["excluded_ids"] = list(self.excluded_ids)
        data["blocks"] = list(self.blocks)
        return data


def new_prompt_run_id() -> str:
    return f"pr-{uuid.uuid4().hex[:12]}"


def _manifest_from_context(
    compiled: CompiledContext,
    *,
    prompt_run_id: str,
    chapter_id: str,
    budget: PromptBudget,
    paragraph_start: int,
    paragraph_end: int,
    system_text: str,
    user_text: str,
    created_at: str,
) -> PromptManifest:
    block_meta = tuple(
        {
            "block_id": block.block_id,
            "kind": block.kind,
            "authority": block.authority,
            "priority": block.priority,
            "pinned": block.pinned,
            "estimated_tokens": block.estimated_tokens,
            "sha256": block.sha256,
            "source_ids": list(block.source_ids),
        }
        for block in compiled.blocks
    )
    return PromptManifest(
        prompt_run_id=prompt_run_id,
        chapter_id=chapter_id,
        compiler_version=compiled.compiler_version,
        input_budget=budget.input_budget,
        output_reserve=budget.output_reserve,
        estimated_tokens=compiled.estimated_tokens,
        style_card_limit=budget.style_card_limit,
        index_chapter_limit=budget.index_chapter_limit,
        is_complex=budget.is_complex,
        context_window=budget.context_window,
        excluded_ids=tuple(compiled.excluded_ids),
        blocks=block_meta,
        paragraph_start=paragraph_start,
        paragraph_end=paragraph_end,
        system_sha256=hashlib.sha256(system_text.encode("utf-8")).hexdigest(),
        user_sha256=hashlib.sha256(user_text.encode("utf-8")).hexdigest(),
        created_at=created_at,
        exemplar_limit=budget.exemplar_limit,
    )


def build_manifest(
    *,
    compiled: CompiledContext,
    prompt_run_id: str,
    chapter_id: str,
    budget: PromptBudget,
    paragraph_start: int,
    paragraph_end: int,
    system_text: str,
    user_text: str,
    created_at: str,
) -> PromptManifest:
    return _manifest_from_context(
        compiled,
        prompt_run_id=prompt_run_id,
        chapter_id=chapter_id,
        budget=budget,
        paragraph_start=paragraph_start,
        paragraph_end=paragraph_end,
        system_text=system_text,
        user_text=user_text,
        created_at=created_at,
    )


def save_prompt_manifest(
    workspace: Path, chapter_id: str, manifest: PromptManifest
) -> Path:
    """把 PromptManifest 原子写入 ``chapters/<chapter_id>/prompts/<run_id>.json``。"""
    directory = workspace / "chapters" / chapter_id / "prompts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{manifest.prompt_run_id}.json"
    write_json_atomic(path, manifest.to_dict())
    return path


#: 范例原文缓存目录（外部索引库文件一律只读，全文缓存只写这里）
KNOWLEDGE_CACHE_DIRNAME = ".cache/knowledge"


def save_exemplar_cache(
    workspace: Path, chapter_id: str, prompt_run_id: str, exemplars: list[str]
) -> Path | None:
    """把本次**实际注入**的文风范例原文缓存下来，供检查侧做防抄比对。

    Manifest 只存哈希不存正文（其隔离契约不得破坏），而防抄检测必须拿到范例
    原文才能算 n-gram 重合，故单独缓存。无范例时不写文件，返回 ``None``。
    """
    if not exemplars:
        return None
    directory = workspace / KNOWLEDGE_CACHE_DIRNAME / "exemplars" / chapter_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{prompt_run_id}.json"
    write_json_atomic(
        path,
        {
            "schema_version": "v4-exemplar-cache-1",
            "chapter_id": chapter_id,
            "prompt_run_id": prompt_run_id,
            "exemplars": list(exemplars),
        },
    )
    return path


def load_exemplar_cache(
    workspace: Path, chapter_id: str, prompt_run_id: str | None = None
) -> list[str]:
    """读取范例缓存；``prompt_run_id`` 为空时取该章最新一份。"""
    directory = workspace / KNOWLEDGE_CACHE_DIRNAME / "exemplars" / chapter_id
    if not directory.is_dir():
        return []
    if prompt_run_id:
        candidates = [directory / f"{prompt_run_id}.json"]
    else:
        candidates = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for path in reversed(candidates):
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rows = doc.get("exemplars")
        if isinstance(rows, list):
            return [str(row) for row in rows if str(row).strip()]
    return []
