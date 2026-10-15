#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务事实 → 参考路由 → 路径/哈希校验 → 请求编译（工单 P0-1 / P0-2 共同入口）。

背景（终验工单）：旧实现里 `run` 与 `export` 各自读 `task.references`，
而计划创建时把 `references` 固定写成空，导致真实生产链**根本没有参考图**。
本模块把这条链收敛成**唯一实现**，自动 Gemini 与手工导出都只能经此编译：

    资产索引事实 ─┐
                  ├─→ route_references_detailed ─→ resolve_references ─→ build_generation_request
    权威风格包   ─┘         (选哪些/职责)            (逐文件校验)          (最小发送编译)

关键纪律：
- 参考图**永远从权威风格包路由得出**，不读计划里可能过期的 `task.references`
  （`task.references` 只作为可选的人工追加项参与路由）。
- 任何参考必须通过存在性/非空/哈希/MIME 校验，失败即稳定码中断，**绝不降级为空图**。
- 风格包缺失或不完整一律 `STYLEPACK_UNAVAILABLE` 拒绝视觉任务。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    from .image_executor import GenerationRequest
    from .image_request_builder import RequestBuildError, build_generation_request
    from .reference_resolver import (
        REFERENCE_HASH_MISMATCH,
        STYLEPACK_UNAVAILABLE,
        ResolvedReference,
        ReferenceResolveError,
        resolve_references,
    )
    from .reference_router import (
        DEFAULT_LIMIT,
        ReferenceRoutingError,
        RouteResult,
        StylePack,
        load_omni_stylepack,
        route_references_detailed,
    )
    from .style_registry import resolve_style_record
    from .visual_schema import ReferenceBinding, VisualTask
except ImportError:  # 扁平导入回退（单文件直跑）
    from image_executor import GenerationRequest  # type: ignore
    from image_request_builder import RequestBuildError, build_generation_request  # type: ignore
    from reference_resolver import (  # type: ignore
        REFERENCE_HASH_MISMATCH,
        STYLEPACK_UNAVAILABLE,
        ResolvedReference,
        ReferenceResolveError,
        resolve_references,
    )
    from reference_router import (  # type: ignore
        DEFAULT_LIMIT,
        ReferenceRoutingError,
        RouteResult,
        StylePack,
        load_omni_stylepack,
        route_references_detailed,
    )
    from style_registry import resolve_style_record  # type: ignore
    from visual_schema import ReferenceBinding, VisualTask  # type: ignore

# 参考图引擎上限（供应商侧可接受张数；裁剪由路由器按职责优先级负责）
REFERENCE_LIMIT = DEFAULT_LIMIT

ACCEPTED_REFERENCE_STATUSES = ("ACCEPTED", "READY_AS_REFERENCE")


class VisualCompileError(RuntimeError):
    """编译视觉请求失败：code 为稳定原因码，绝不带密钥。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


@dataclass(frozen=True)
class CompiledVisualRequest:
    """一次已校验、可发送/可导出的视觉请求及其清单。"""

    request: GenerationRequest
    resolved: tuple[ResolvedReference, ...]
    omitted: tuple[dict, ...]
    routing: RouteResult
    style_name: str
    style_version: str
    stylepack_root: Path | None

    def prompt_facts(self) -> dict:
        from .image_request_builder import prompt_fact_set

        return prompt_fact_set(self.request.prompt)


def _stylepack_of(task: VisualTask) -> tuple[Any, StylePack, Path]:
    """解析任务风格 → (登记记录, 已加载风格包, 风格包根)；缺包即稳定码失败。"""
    record = resolve_style_record(task.style_slug)
    if record is None:
        raise VisualCompileError(
            "STYLE_UNKNOWN", f"风格 {task.style_slug!r} 不在登记表中（新任务只能用权威中文风格名）")
    try:
        pack = load_omni_stylepack(record)
    except ReferenceRoutingError as e:
        code = STYLEPACK_UNAVAILABLE if e.code in (
            "STYLEPACK_ROUTER_MISSING", "STYLEPACK_MALFORMED", "STYLE_UNPINNED") else e.code
        raise VisualCompileError(code, e.detail or str(e)) from e
    return record, pack, Path(pack.root)


def accepted_asset_paths(plan, library_root: Path) -> dict[str, Path]:
    """计划内**已通过且文件与哈希可验证**的上游产物，值为**库内相对路径**。

    只有 ACCEPTED / READY_AS_REFERENCE、最新 attempt 图片真实存在、
    且**文件哈希与 attempt 记录一致**时才算可用。哈希不符（文件被换过）一律剔除，
    绝不让「与验收时不是同一张」的图片成为下游参考。
    值为相对路径：绑定与清单只能保存相对路径，绝不泄露机器绝对路径。
    """
    import hashlib

    out: dict[str, Path] = {}
    for task in plan.tasks:
        if task.status not in ACCEPTED_REFERENCE_STATUSES:
            continue
        attempt = task.latest_attempt
        if attempt is None:
            continue
        rel = str(attempt.image or "").strip()
        if not rel:
            continue
        image = Path(library_root) / rel
        if not image.is_file():
            continue
        expected = str(attempt.image_sha256 or "").strip().lower()
        if expected:
            try:
                actual = hashlib.sha256(image.read_bytes()).hexdigest()
            except OSError:
                continue
            if actual != expected:
                continue
        out[task.task_id] = Path(rel.replace("\\", "/"))
    return out


def _extra_bindings(task: VisualTask, library_root: Path) -> list[ReferenceBinding]:
    """计划里显式声明的人工追加参考（可选）：路径仍按相对资产库解析。"""
    return [b for b in task.references if str(b.path or "").strip()]


def _verify_declared_hashes(task: VisualTask, library_root: Path,
                            accepted: Mapping[str, Path]) -> None:
    """计划里带哈希声明的参考必须与真实内容一致（防止「用旧图当新参考」）。

    路由器按职责重建绑定，不携带计划中的人工声明哈希；此处补上这层校验，
    使「声明哈希 ≠ 真实内容」在生成前就以 `REFERENCE_HASH_MISMATCH` 中断。
    """
    import hashlib

    for binding in task.references:
        declared = str(binding.sha256 or "").strip()
        if not declared or not str(binding.path or "").strip():
            continue
        rel = str(binding.path).replace("\\", "/")
        candidate = Path(library_root) / rel
        if not candidate.is_file():
            # 可能落在风格包里，交由 resolve_references 统一判定
            continue
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual.lower() != declared.lower():
            raise VisualCompileError(
                REFERENCE_HASH_MISMATCH,
                f"参考图哈希与计划声明不符：{rel}"
                f"（声明 {declared[:12]}… 实际 {actual[:12]}…）")


def stylepack_integrity_issues(pack_root: Path, require_images: bool = True) -> list[str]:
    """权威包与自身 manifest 的一致性检查（缺图/漂移/多出文件都算问题）。

    新任务只能建立在**运行必需**内容齐备的权威包上：少一张**运行参考图**就必须失败，
    否则会退化成「无参考图生成」——正是 rc.1 的 P0 缺陷形态。

    来源图（`references/sources/`，授权未确认、`share_allowed=false`）不参与运行，
    分享包按策略排除它们属设计如此，因此**不算不完整**。

    校验实现只有一份（`stylepack_verify`），同步、发布与运行期共用。
    """
    from .stylepack_verify import runtime_image_issues, verify_pack

    root = Path(pack_root)
    issues = verify_pack(root, require_images=False)   # 文本层：必需文件与漂移
    if require_images:
        issues += runtime_image_issues(root)           # 运行必需参考图
    return issues


def unverifiable_accepted(plan, library_root: Path) -> list[tuple[str, str, str]]:
    """已判定通过但**产物无法核验**的任务：(task_id, 原因码, 说明)。

    已通过的图就是后续所有参考的身份/结构真相。若其文件缺失、被换或哈希漂移，
    绝不能「静默从参考集里消失」——那会让下游在没有身份锁的情况下生成，
    正是画风与五官漂移的成因。此处如实报出，由编译层拒绝。
    """
    import hashlib

    problems: list[tuple[str, str, str]] = []
    for task in plan.tasks:
        if task.status not in ACCEPTED_REFERENCE_STATUSES:
            continue
        attempt = task.latest_attempt
        if attempt is None:
            problems.append((task.task_id, "REFERENCE_MISSING", "标记已通过但没有产物记录"))
            continue
        rel = str(attempt.image or "").strip()
        if not rel:
            problems.append((task.task_id, "REFERENCE_MISSING", "产物记录缺少图片路径"))
            continue
        image = Path(library_root) / rel
        if not image.is_file():
            problems.append((task.task_id, "REFERENCE_MISSING", f"产物文件不存在：{rel}"))
            continue
        expected = str(attempt.image_sha256 or "").strip().lower()
        if not expected:
            continue
        try:
            actual = hashlib.sha256(image.read_bytes()).hexdigest()
        except OSError as e:
            problems.append((task.task_id, "REFERENCE_MISSING", f"产物不可读：{type(e).__name__}"))
            continue
        if actual != expected:
            problems.append((task.task_id, "REFERENCE_HASH_MISMATCH",
                             f"产物内容与验收时不一致：{rel}"))
    return problems


def compile_visual_request(task: VisualTask, asset: Mapping, plan, library_root: Path,
                           engine_profile: str = "", limit: int = REFERENCE_LIMIT,
                           stylepack_override: tuple[Any, StylePack, Path] | None = None
                           ) -> CompiledVisualRequest:
    """编译一次视觉请求：路由 → 校验 → 最小发送编译（run 与 export 的唯一入口）。

    `stylepack_override` 供测试注入替身风格包（生产路径一律取权威登记包）。
    """
    library_root = Path(library_root)
    if stylepack_override is not None:
        record, stylepack, pack_root = stylepack_override
        pack_root = Path(pack_root)
    else:
        record, stylepack, pack_root = _stylepack_of(task)
    # 权威包必须先自证完整（缺图/漂移会让路由缺项 → 退化成少发参考）
    issues = stylepack_integrity_issues(pack_root, require_images=True)
    if issues:
        raise VisualCompileError(
            STYLEPACK_UNAVAILABLE,
            f"权威风格包与清单不一致（{len(issues)} 项）：" + "；".join(issues[:3]))
    # 已通过的图是下游的身份/结构真相：其产物无法核验时必须拒绝，不得静默丢参考
    problems = unverifiable_accepted(plan, library_root)
    if problems:
        task_id, code, detail = problems[0]
        raise VisualCompileError(
            code,
            f"已通过任务 {task_id} 的产物无法核验（{detail}）；"
            f"共 {len(problems)} 项。修复后方可继续，不得在缺少身份参考的情况下生成。")

    accepted = accepted_asset_paths(plan, library_root)
    _verify_declared_hashes(task, library_root, accepted)

    # 计划内显式参考并入上游集合（相对资产库根解析），使人工追加项也能被路由与校验
    for binding in _extra_bindings(task, library_root):
        accepted.setdefault(f"__extra__:{binding.path}", library_root / binding.path)

    try:
        routing = route_references_detailed(task, stylepack, accepted, limit=limit)
    except ReferenceRoutingError as e:
        raise VisualCompileError(e.code, e.detail or str(e)) from e

    # 权威包不完整（少图/漂移）时，路由会缺项；此时必须拒绝而不是少发参考
    if routing.missing:
        detail = "；".join(str(item.get("reason") or item.get("path")) for item in routing.missing)
        raise VisualCompileError(
            STYLEPACK_UNAVAILABLE,
            f"权威风格包不完整，缺少 {len(routing.missing)} 项路由参考：{detail}")

    # 人工追加项：路由器不认识其角色，直接以声明职责追加到绑定尾部后统一校验
    bindings = list(routing.bindings)
    known_paths = {b.path for b in bindings}
    declared_by_path = {str(b.path): str(b.sha256 or "") for b in task.references
                        if str(b.path or "").strip()}
    for binding in _extra_bindings(task, library_root):
        if binding.path not in known_paths:
            bindings.append(binding)
    # 计划里对某张图声明过哈希时，把声明带进校验（内容被换过即在生成前中断）
    if declared_by_path:
        bindings = [
            b if not declared_by_path.get(b.path) or b.sha256
            else ReferenceBinding(path=b.path, role=b.role,
                                  sha256=declared_by_path[b.path], note=b.note)
            for b in bindings
        ]

    try:
        resolved = resolve_references(bindings, stylepack_root=pack_root,
                                      library_root=library_root)
    except ReferenceResolveError as e:
        raise VisualCompileError(e.code, e.detail or str(e)) from e

    try:
        request = build_generation_request(task, asset, stylepack,
                                           tuple(r.as_binding() for r in resolved),
                                           engine_profile or task.engine or "gemini")
    except RequestBuildError as e:
        raise VisualCompileError(e.code, e.detail or str(e)) from e

    # 把「已校验、可发送」的参考对象挂到请求上：执行器只发这些真实字节
    from dataclasses import replace as _replace

    request = _replace(request, resolved=tuple(resolved))

    return CompiledVisualRequest(
        request=request, resolved=resolved, omitted=tuple(routing.omitted),
        routing=routing, style_name=record.name, style_version=task.style_version,
        stylepack_root=pack_root)


def compile_for_library(task: VisualTask, library_root: Path, plan,
                        engine_profile: str = "", limit: int = REFERENCE_LIMIT
                        ) -> CompiledVisualRequest:
    """从资产库读取当前资产事实后编译（CLI 的便捷包装）。"""
    return compile_visual_request(task, load_asset_facts(library_root, task.asset_id), plan,
                                  library_root, engine_profile=engine_profile, limit=limit)


def load_asset_facts(library_root: Path, asset_id: str) -> dict:
    """从资产索引取当前资产事实；索引缺失时显式失败（不得凭空生成事实）。"""
    import json

    index_path = Path(library_root) / ".process" / "资产索引.json"
    if not index_path.is_file():
        raise VisualCompileError("ASSET_INDEX_MISSING", f"资产索引不存在：{index_path.name}")
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise VisualCompileError("ASSET_INDEX_UNREADABLE", f"资产索引不可解析：{e}") from e
    for item in data.get("assets", []):
        if isinstance(item, dict) and item.get("asset_id") == asset_id:
            return item
    raise VisualCompileError("ASSET_NOT_FOUND", f"资产索引中没有 {asset_id}")
