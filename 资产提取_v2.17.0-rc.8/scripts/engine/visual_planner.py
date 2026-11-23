#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉任务规划器（规格 6，工单02-Task5）。

只读取**已签发或可验证的资产索引**，绝不重新调用 LLM 推断人物事实；
每项资产独立生成、独立检查、独立返工；人物按优先级分级出任务。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:  # 包导入优先；脚本式扁平导入时回退（同一文件只有一份类身份）
    from .visual_schema import (
        ASPECT_BY_KIND, MAX_AUTO_ROUNDS, STORY_TASK_KINDS, VisualPlan, VisualTask,
        VisualTaskKind, VisualTaskStatus,
    )
except ImportError:  # pragma: no cover - 脚本式运行路径
    from visual_schema import (
        ASPECT_BY_KIND, MAX_AUTO_ROUNDS, STORY_TASK_KINDS, VisualPlan, VisualTask,
        VisualTaskKind, VisualTaskStatus,
    )

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
TASK_TEMPLATE_DIR = SKILL_DIR / "templates" / "视觉任务"
TEMPLATE_NAME_BY_KIND = {
    "正脸": "人物正脸",
    "后脑": "人物后脑",
    "服装独立正面": "服装独立正面",
    "服装独立背面": "服装独立背面",
    "人物全身正面": "人物全身正面",
    "人物全身背面": "人物全身背面",
    "场景母版": "场景母版",
    "道具": "道具",
    "剧情剧照": "剧情剧照",
    "动作参考": "动作参考",
    "材质特写": "材质特写",
}

# 依赖链（规格 6.3）：正脸 → 后脑 → 服装正面 → 服装背面 → 全身正面 → 全身背面
DEPENDENCY_CHAIN = (
    VisualTaskKind.FACE.value,
    VisualTaskKind.BACK_HEAD.value,
    VisualTaskKind.COSTUME_FRONT.value,
    VisualTaskKind.COSTUME_BACK.value,
    VisualTaskKind.FULL_BODY_FRONT.value,
    VisualTaskKind.FULL_BODY_BACK.value,
)

# 分级任务集（规格 6.1）
KINDS_BY_PRIORITY = {
    "P0": DEPENDENCY_CHAIN,
    "P1": DEPENDENCY_CHAIN,
    "P2": (VisualTaskKind.FACE.value, VisualTaskKind.FULL_BODY_FRONT.value,
           VisualTaskKind.FULL_BODY_BACK.value),
    "P3": (VisualTaskKind.FULL_BODY_FRONT.value,),
}
COSTUME_KINDS = (VisualTaskKind.COSTUME_FRONT.value, VisualTaskKind.COSTUME_BACK.value)
BACK_KINDS = (VisualTaskKind.FULL_BODY_BACK.value,)
SCENE_KINDS = (VisualTaskKind.SCENE_MASTER.value,)
PROP_KINDS = (VisualTaskKind.PROP.value,)

# 源头脸规则（阶段 4 工单 4.3）：唯一身份真相必须是干净可控的证件式底图
SOURCE_FACE_RULE = ("浅灰或浅冷灰干净背景，柔和均匀光线；不出现山水、建筑、剧情环境或夸张电影光效；"
                    "单视图、无拼版、无无头身体")
# 默认衣物状态：整洁普通；只有原文明确写破损/贫困/战损时才改成破损描述
COSTUME_DAMAGE_WORDS = ("破损", "破烂", "褴褛", "血污", "战损", "破旧", "衣衫不整", "补丁")
COSTUME_DEFAULT_NOTE = "服装默认整洁普通（原文未写明破损时不得加破损、血污、战损）"
COSTUME_DAMAGED_NOTE = "原文明确写明破损/战损，按原文描述保留破损与污损"


def costume_condition_note(asset: dict | None) -> tuple[str, str]:
    """由资产事实判断衣物状态：返回（备注, 依据摘句）。

    默认「整洁普通」；只有原文摘句里明确出现破损类词才允许破损描述——
    不允许模型凭风格想象加战损。
    """
    quotes = []
    for item in (asset or {}).get("evidence") or ():
        if isinstance(item, dict):
            quotes.append(str(item.get("quote", "")))
    text = "；".join(quotes)
    for word in COSTUME_DAMAGE_WORDS:
        if word in text:
            return COSTUME_DAMAGED_NOTE, f"原文依据：{word}"
    return COSTUME_DEFAULT_NOTE, "原文未写明破损"


PRIORITY_FALLBACK = "P3"


class PlannerError(ValueError):
    """规划失败：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def template_for_kind(kind: str) -> str:
    """取任务种类的构图模板正文（缺模板即显式失败，不用空模板冒充）。"""
    name = TEMPLATE_NAME_BY_KIND.get(kind)
    if not name:
        raise PlannerError("TEMPLATE_UNKNOWN_KIND", f"任务种类 {kind!r} 没有对应模板")
    path = TASK_TEMPLATE_DIR / f"{name}.md"
    if not path.is_file():
        raise PlannerError("TEMPLATE_MISSING", f"缺少视觉任务模板 {path}")
    return path.read_text(encoding="utf-8")


def _first_quote(asset: Mapping) -> str:
    evidence = asset.get("evidence") or []
    for item in evidence:
        if isinstance(item, dict) and str(item.get("quote") or "").strip():
            line = str(item.get("line") or "").strip()
            return f"{item['quote'].strip()}" + (f"（行 {line}）" if line else "")
    return ""


def _fact_source(asset: Mapping, index_rel: str) -> str:
    quote = _first_quote(asset) or "（索引未附摘句）"
    return f"{index_rel}#{asset.get('asset_id')}｜摘句: {quote}"


def _kinds_for(asset: Mapping, extra_costume: set[str], extra_back: set[str]) -> tuple[str, ...]:
    asset_type = str(asset.get("type") or "")
    asset_id = str(asset.get("asset_id") or "")
    priority = str(asset.get("priority") or PRIORITY_FALLBACK)
    if asset_type == "人物":
        kinds = list(KINDS_BY_PRIORITY.get(priority, KINDS_BY_PRIORITY[PRIORITY_FALLBACK]))
        if asset_id in extra_costume:
            kinds += [k for k in COSTUME_KINDS if k not in kinds]
            kinds.sort(key=DEPENDENCY_CHAIN.index)
        if asset_id in extra_back:
            kinds += [k for k in BACK_KINDS if k not in kinds]
            kinds.sort(key=DEPENDENCY_CHAIN.index)
        return tuple(kinds)
    if asset_type == "场景":
        return SCENE_KINDS
    if asset_type == "道具":
        return PROP_KINDS
    # 服装资产不单独出任务：服装独立图按"人物穿着的那一套"生成（规格 6.1/6.3 依赖链）
    return ()


def _plan_id(asset_id: str, kind: str, version: int = 1) -> str:
    return f"{asset_id}-{kind}-v{version:02d}"


def _build_task(asset: Mapping, kind: str, style, index_rel: str,
                dep_ids: Sequence[str]) -> VisualTask:
    asset_id = str(asset.get("asset_id") or "")
    quote = _first_quote(asset) or "（索引未附摘句）"
    identity_lock = f"{asset.get('name')}：{quote}"
    costume_note, costume_evidence = costume_condition_note(dict(asset))
    notes = identity_lock
    purpose = _purpose_for(asset, kind)
    if kind in (VisualTaskKind.FACE.value, VisualTaskKind.BACK_HEAD.value):
        notes = f"{identity_lock}｜源头脸底图规则：{SOURCE_FACE_RULE}｜{costume_note}"
    elif kind in COSTUME_KINDS or kind.startswith("人物全身"):
        notes = f"{identity_lock}｜{costume_note}"
    return VisualTask(
        task_id=_plan_id(asset_id, kind),
        asset_id=asset_id,
        kind=kind,
        status=VisualTaskStatus.PLANNED.value,
        aspect_ratio=ASPECT_BY_KIND.get(kind, "1:1"),
        style_slug=style.pack_slug,
        style_version=style.pack_version,
        style_hash=style.pack_hash,
        engine="gemini",
        model="",
        depends_on=tuple(dep_ids),
        references=(),
        attempts=(),
        max_rounds=MAX_AUTO_ROUNDS,
        fact_source=_fact_source(asset, index_rel),
        user_locked=(),
        notes=notes,
        purpose=purpose,
        script_source=quote,
        asset_index_key=str(asset.get("stable_key") or ""),
        negative_prompt=(f"{SOURCE_FACE_RULE}" if kind == VisualTaskKind.FACE.value else ""),
    )


def _purpose_for(asset: Mapping, kind: str) -> str:
    name = str(asset.get("name") or asset.get("asset_id") or "")
    return {
        VisualTaskKind.FACE.value: f"{name} 正脸身份近景（源头脸，唯一身份真相）",
        VisualTaskKind.BACK_HEAD.value: f"{name} 后脑与发型背面",
        VisualTaskKind.COSTUME_FRONT.value: f"{name} 服装独立正面",
        VisualTaskKind.COSTUME_BACK.value: f"{name} 服装独立背面",
        VisualTaskKind.FULL_BODY_FRONT.value: f"{name} 全身正面立绘",
        VisualTaskKind.FULL_BODY_BACK.value: f"{name} 全身背面立绘",
        VisualTaskKind.SCENE_MASTER.value: f"{name} 场景母版",
        VisualTaskKind.PROP.value: f"{name} 道具参考",
    }.get(kind, f"{name} {kind}")


def plan_visual_tasks(asset_index: dict, style, scope: set[str] | None = None,
                      existing: VisualPlan | None = None,
                      extra_costume: Iterable[str] | None = None,
                      extra_back: Iterable[str] | None = None,
                      index_rel: str = ".process/资产索引.json") -> VisualPlan:
    """按资产索引生成视觉计划（幂等：传入 existing 时只新增缺失任务）。

    - P0/P1 人物：正脸、后脑、服装独立正面、服装独立背面、全身正面、全身背面。
    - P2 人物：正脸、全身正面、全身背面；`extra_costume` 里的资产补服装正背面（剧情需要）。
    - P3 人物：全身正面；`extra_back` 里的资产补全身背面（存在背面镜头）。
    - 场景：场景母版；道具：道具。
    已完成记录、用户锁定字段与人工备注一律保留；事实变化时受影响任务标记
    `NEEDS_REVIEW`（旧图保留，但不再作为下游参考）。
    """
    if not isinstance(asset_index, dict) or not isinstance(asset_index.get("assets"), list) \
            or not asset_index.get("assets"):
        raise PlannerError("ASSET_INDEX_REQUIRED",
                           "视觉计划必须基于已签发或可验证的资产索引（assets 非空）")
    for field in ("pack_slug", "pack_version", "pack_hash"):
        if not str(getattr(style, field, "") or "").strip():
            raise PlannerError("STYLE_UNPINNED",
                               f"风格必须固定 {field}（先同步风格包并登记内容哈希）")

    extra_costume = set(extra_costume or ())
    extra_back = set(extra_back or ())
    wanted = set(scope) if scope else None

    previous = {t.task_id: t for t in (existing.tasks if existing else ())}
    planned: list[VisualTask] = []
    kept_ids: set[str] = set()

    for asset in asset_index["assets"]:
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("asset_id") or "")
        if not asset_id or (wanted is not None and asset_id not in wanted):
            continue
        kinds = _kinds_for(asset, extra_costume, extra_back)
        dep_by_kind: dict[str, str] = {}
        for kind in kinds:
            dep_ids: list[str] = []
            if kind in DEPENDENCY_CHAIN:
                pos = DEPENDENCY_CHAIN.index(kind)
                # 依赖链上"最近"的前置（P2/P3 缺中间环节时自动下沉到已有的最近前置）
                for prev_kind in reversed(DEPENDENCY_CHAIN[:pos]):
                    if prev_kind in dep_by_kind:
                        dep_ids = [dep_by_kind[prev_kind]]
                        break
                else:
                    dep_ids = []
            task_id = _plan_id(asset_id, kind)
            dep_by_kind[kind] = task_id
            fresh = _build_task(asset, kind, style, index_rel, dep_ids)
            old = previous.get(task_id)
            planned.append(_merge_task(old, fresh))
            kept_ids.add(task_id)

    # 已有计划里的人工任务（不在本次 scope/索引范围内）原样保留
    for task_id, old in previous.items():
        if task_id not in kept_ids:
            planned.append(old)

    planned = _apply_story_dependencies(planned)

    return VisualPlan(
        schema_version=existing.schema_version if existing else 1,
        style_name=style.name,
        style_slug=style.pack_slug,
        style_version=style.pack_version,
        style_hash=style.pack_hash,
        engine_profile=existing.engine_profile if existing else "gemini",
        lib=existing.lib if existing else ".",
        asset_index_hash=str(asset_index.get("source_fingerprint") or ""),
        updated_at=existing.updated_at if existing else "",
        tasks=tuple(sorted(planned, key=lambda t: t.task_id)),
    )


def _apply_story_dependencies(tasks: Sequence[VisualTask]) -> list[VisualTask]:
    """剧情类任务的依赖规则（工单 5.2）：等同一资产的全部基础任务正式通过才允许生成。

    规格第十一节：剧情剧照、动作参考与关键帧必须依据分镜/构图卡，不得从摘要直接跳到生图；
    第八节：剧情图片必须等待相关人物、服装、场景和关键道具通过。剧情任务按资产归属
    （人物／场景／道具）落到同一 `asset_id` 的基础任务上，故依赖就是该资产的全部基础任务。
    已有依赖一律保留（人工补充的跨资产依赖不被覆盖），只做并集。
    """
    base_ids_by_asset: dict[str, list[str]] = {}
    for task in tasks:
        if task.kind in STORY_TASK_KINDS:
            continue
        base_ids_by_asset.setdefault(task.asset_id, []).append(task.task_id)
    if not base_ids_by_asset:
        return list(tasks)

    updated: list[VisualTask] = []
    for task in tasks:
        if task.kind not in STORY_TASK_KINDS:
            updated.append(task)
            continue
        needed = [tid for tid in sorted(base_ids_by_asset.get(task.asset_id, []))
                  if tid != task.task_id]
        merged = list(task.depends_on) + [tid for tid in needed
                                          if tid not in task.depends_on]
        updated.append(task if tuple(merged) == task.depends_on
                       else replace(task, depends_on=tuple(merged)))
    return updated


def _merge_task(old: VisualTask | None, fresh: VisualTask) -> VisualTask:
    """幂等合并：保留用户锁定字段、已完成记录与人工备注；事实变化标 NEEDS_REVIEW。"""
    if old is None:
        return fresh
    fact_changed = old.fact_source != fresh.fact_source
    status = old.status
    if fact_changed and old.is_accepted:
        status = VisualTaskStatus.NEEDS_REVIEW.value
    elif fact_changed and old.status == VisualTaskStatus.NEEDS_HUMAN_DECISION.value:
        status = VisualTaskStatus.NEEDS_REVIEW.value
    merged = dict(old.__dict__)
    merged.update({
        "kind": fresh.kind,
        "status": status,
        "aspect_ratio": fresh.aspect_ratio,
        "style_slug": fresh.style_slug,
        "style_version": fresh.style_version,
        "style_hash": fresh.style_hash,
        "depends_on": old.depends_on if old.depends_on else fresh.depends_on,
        "fact_source": fresh.fact_source,
        "notes": old.notes or fresh.notes,
    })
    if old.user_locked:
        # 用户锁定字段：不被本次规划覆盖（锁定的是字段名，值取自旧记录）
        for field in old.user_locked:
            if field in old.__dict__:
                merged[field] = getattr(old, field)
    return VisualTask(**merged)
