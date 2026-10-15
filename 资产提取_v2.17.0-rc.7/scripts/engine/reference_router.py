#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参考图与材质动态路由（规格 7，工单02-Task6）。

- 参考图必须**声明职责**：identity / structure / style / material / lighting / space。
- 按资产类型与实际材质选择**最小集合**；职责不得越权（皮肤材质不得覆盖身份，
  风格参考不得把其服装复制给目标人物）。
- 超出引擎参考图上限时按 身份 → 结构 → 主风格 → 关键材质 → 次要材质 裁剪，
  被省略的参考与原因显式记录，不依赖文件遍历顺序。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .visual_schema import ReferenceBinding, ReferenceRole, VisualTask, VisualTaskKind

SKILL_DIR = Path(__file__).resolve().parent.parent.parent

# 裁剪优先级（规格 7）：数字越小越先保留；次要材质排在光影/空间之后
ROLE_PRIORITY = {
    ReferenceRole.IDENTITY.value: 0,
    ReferenceRole.STRUCTURE.value: 1,
    ReferenceRole.STYLE.value: 2,
    ReferenceRole.MATERIAL.value: 3,
    ReferenceRole.LIGHTING.value: 4,
    ReferenceRole.SPACE.value: 5,
}
SECONDARY_MATERIAL_RANK = 6
DEFAULT_LIMIT = 8

# 参考来源优先级（工单 10 步骤 1）：来源（provenance）优先于角色。
# 数字越小越优先；同一来源内才比 `ROLE_PRIORITY`。
ORIGIN_USER = "user_supplied"
ORIGIN_REVIEWED_PROJECT = "reviewed_project"
ORIGIN_APPROVED_VIEW = "approved_view"
ORIGIN_STYLE = "style"
ORIGIN_PRIORITY = (ORIGIN_USER, ORIGIN_REVIEWED_PROJECT, ORIGIN_APPROVED_VIEW, ORIGIN_STYLE)

# 只有这两种判定允许把生成图当作参考
ALLOWED_REFERENCE_DECISIONS = ("ACCEPTED", "PASS_WITH_NOTES")
# 需要审核判定才可用的来源：用户提供的参考与风格包不经过生成审核
_REVIEWED_ORIGINS = (ORIGIN_REVIEWED_PROJECT, ORIGIN_APPROVED_VIEW)

REFERENCE_FROM_FAILED_REVIEW = "REFERENCE_FROM_FAILED_REVIEW"
REFERENCE_UNREVIEWED = "REFERENCE_UNREVIEWED"
REFERENCE_ORIGIN_UNKNOWN = "REFERENCE_ORIGIN_UNKNOWN"


def origin_rank(origin: str) -> int:
    """来源优先级秩；未知来源排在最后，不抢占已知来源。"""
    try:
        return ORIGIN_PRIORITY.index(str(origin or ""))
    except ValueError:
        return len(ORIGIN_PRIORITY)


def is_reference_eligible(*, origin: str, review_decision: str = "") -> tuple:
    """判断某张图能否进入参考链，返回（是否合格, 原因码）。

    硬规则：**审核失败图永不进入参考链**；生成的图没拿到通过的判定也不得当参考。
    """
    text = str(origin or "")
    if origin_rank(text) >= len(ORIGIN_PRIORITY):
        return False, REFERENCE_ORIGIN_UNKNOWN
    if text not in _REVIEWED_ORIGINS:
        return True, ""
    decision = str(review_decision or "")
    if not decision:
        return False, REFERENCE_UNREVIEWED
    if decision not in ALLOWED_REFERENCE_DECISIONS:
        return False, REFERENCE_FROM_FAILED_REVIEW
    return True, ""


def reference_sort_key(entry: Mapping) -> tuple:
    """排序键：先来源、后角色；同来源同角色保持调用方给的顺序（稳定）。"""
    origin = str(entry.get("origin", ""))
    role = str(entry.get("role", ""))
    return (origin_rank(origin), ROLE_PRIORITY.get(role, len(ROLE_PRIORITY)), role)


def select_references(entries) -> list:
    """过滤掉不合格的参考，再按「来源 → 角色」排序。"""
    kept = []
    for entry in entries or []:
        ok, _reason = is_reference_eligible(
            origin=entry.get("origin", ""), review_decision=entry.get("review_decision", ""))
        if ok:
            kept.append(entry)
    return sorted(kept, key=reference_sort_key)


# 任务种类 → 内置包主体路由（后脑刻意不用 face：避免继承正面构图）
SUBJECT_ROUTE_BY_KIND = {
    "正脸": "face",
    "后脑": "full_body",
    "服装独立正面": "full_body",
    "服装独立背面": "full_body",
    "人物全身正面": "full_body",
    "人物全身背面": "full_body",
    "场景母版": "environment",
    "道具": "object",
}
# 主体路由参考的职责：场景母版吃空间结构，道具吃形制结构，其余作为主风格视图基准
SUBJECT_ROLE_BY_KIND = {
    "场景母版": ReferenceRole.SPACE.value,
    "道具": ReferenceRole.STRUCTURE.value,
}
SUBJECT_ROLE_DEFAULT = ReferenceRole.STYLE.value

SUBJECT_NOTE = "主风格视图基准：只提供介质、材质、光色与构图基线，不得复制其服装与人物身份"
SPACE_NOTE = "空间结构参考：只锁定空间布局与体量关系，不改变本场景的原文事实"
OBJECT_NOTE = "物件形制参考：只锁定比例与构造关系，不覆盖本道具的原文材质与状态"

# 上游任务种类 → 作为下游参考时的职责与备注（职责不得越权）
UPSTREAM_REFERENCE = {
    "正脸": (ReferenceRole.IDENTITY.value, "身份唯一真相：锁定骨相与五官"),
    "后脑": (ReferenceRole.STRUCTURE.value, "发型体量与后脑结构；不得继承正面构图"),
    "服装独立正面": (ReferenceRole.STRUCTURE.value,
               "同一件服装的正面结构与材质分区；本任务不得继承头部与面部"),
    "服装独立背面": (ReferenceRole.STRUCTURE.value,
               "同一件服装的背面结构；本任务不得继承头部与面部"),
    "人物全身正面": (ReferenceRole.STRUCTURE.value, "同一人物的全身比例与站姿"),
    "人物全身背面": (ReferenceRole.STRUCTURE.value, "同一人物的背面轮廓；不得出现面部"),
    "场景母版": (ReferenceRole.SPACE.value, "空间结构与固定锚点基线"),
    "道具": (ReferenceRole.STRUCTURE.value, "物件形制与比例"),
}

# 绝不可作为参考的任务种类（阶段 4 工单 4.3）：拼版只是预览，
# 由两张已通过图片本地合成、不携带新事实，更不能成为身份参考。
NEVER_REFERENCE_KINDS = ("拼版",)

# 中文材质名 → 内置包材质路由
MATERIAL_ROUTE_BY_NAME = {
    "皮肤": "skin", "头发": "hair", "布料": "fabric", "皮革": "leather", "金属": "metal",
    "玻璃": "glass", "塑料": "plastic", "木材": "wood", "石材": "stone", "陶瓷": "ceramic",
    "纸": "paper", "液体": "liquid", "自发光": "emissive", "橡胶": "rubber",
    "妆面": "makeup", "植被": "foliage",
}
# 未声明材质时按资产类型兜底（仅用内置包里存在的材质）
DEFAULT_MATERIALS_BY_PREFIX = {
    "CH_": ("皮肤", "头发", "布料"),
    "SC_": ("石材", "木材", "植被"),
    "PR_": ("金属", "木材"),
    "CO_": ("布料", "皮革"),
}


class ReferenceRoutingError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


@dataclass(frozen=True)
class StylePack:
    """内置风格包（版本化）的主体/材质路由表。"""

    root: Path
    subject_routes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    material_routes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 路由表里的相对路径是相对 router.md 所在目录（内置包 references/）解析的
    router_dir: str = "references"

    def _to_root_relative(self, rel: str) -> str:
        base = (self.router_dir or "").strip("/")
        return f"{base}/{rel}" if base else rel

    def subject_image(self, route: str) -> str:
        for candidate in self.subject_routes.get(route, ()):
            if candidate.lower().endswith(".png"):
                return self._to_root_relative(candidate)
        return ""

    def material_image(self, route: str) -> str:
        for candidate in self.material_routes.get(route, ()):
            if candidate.lower().endswith(".png"):
                return self._to_root_relative(candidate)
        return ""

    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()


@dataclass(frozen=True)
class RouteResult:
    bindings: tuple[ReferenceBinding, ...] = ()
    omitted: tuple[dict, ...] = ()
    # 路由命中但**文件实际缺失**的候选（如权威包被删图）：调用方必须据此拒绝，
    # 不得因为「路由里查不到」而静默退化成无参考。
    missing: tuple[dict, ...] = ()


def _parse_routes(text: str) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """解析 router.md 中的 yaml 代码块（确定性：按声明顺序）。"""
    subject: dict[str, list[str]] = {}
    material: dict[str, list[str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip() == "subject_routes:":
            current = subject
            continue
        if line.strip() == "material_routes:":
            current = material
            continue
        if current is None:
            continue
        m = re.match(r"^\s{2}([A-Za-z_]+):\s*\[(.*)\]\s*$", line)
        if not m:
            continue
        names = tuple(x.strip() for x in m.group(2).split(",") if x.strip())
        current[m.group(1)] = list(names)
    return ({k: tuple(v) for k, v in subject.items()},
            {k: tuple(v) for k, v in material.items()})


def load_omni_stylepack(record) -> StylePack:
    """加载内置 Omni 风格包的路由表；缺包或缺路由器显式失败。"""
    from .style_registry import pack_root

    slug = str(getattr(record, "pack_slug", "") or "")
    version = str(getattr(record, "pack_version", "") or "")
    if not slug or not version:
        raise ReferenceRoutingError("STYLE_UNPINNED", "风格未固定包版本，无法路由参考图")
    root = pack_root(slug, version)
    router = root / "references" / "router.md"
    if not router.is_file():
        raise ReferenceRoutingError("STYLEPACK_ROUTER_MISSING", f"内置包缺少路由器：{router}")
    subject, material = _parse_routes(router.read_text(encoding="utf-8"))
    if not subject or not material:
        raise ReferenceRoutingError("STYLEPACK_ROUTER_MALFORMED",
                                    f"路由器解析为空：{router}")
    return StylePack(root=root, subject_routes=subject, material_routes=material,
                     router_dir=_posix_rel(router.parent, root))


def _posix_rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return ""


def _kind_of_task_id(task_id: str) -> str:
    """从 `<资产ID>-<任务种类>-vNN` 解析任务种类（解析不出返回空串）。"""
    m = re.match(r"^[A-Za-z]{2}_[^-]+-(.+)-v\d{2}$", task_id or "")
    return m.group(1) if m else ""


def _library_relative(path: Path | str) -> str:
    """归一化为**库内相对路径**（正斜杠）。

    绑定与清单绝不保存机器绝对路径；调用方若传入绝对路径说明接线有误，
    直接以稳定码失败，而不是把本机路径漏进产物。
    """
    from .visual_schema import is_relative_safe, normalize_rel_path

    rel = normalize_rel_path(str(path))
    if not is_relative_safe(rel):
        raise ReferenceRoutingError(
            "REFERENCE_PATH_UNSAFE",
            f"上游参考必须使用资产库内相对路径，收到绝对或越界路径：{rel!r}")
    return rel


def _accepted_face_of(task: VisualTask,
                      accepted: Mapping[str, Path]) -> tuple[str, Path] | None:
    """同一资产已通过的正脸任务（身份唯一真相）；没有则返回 None。"""
    prefix = f"{task.asset_id}-{VisualTaskKind.FACE.value}-"
    for dep in sorted(accepted):
        if str(dep).startswith(prefix):
            return str(dep), accepted[dep]
    return None


def _materials_for(task: VisualTask) -> list[str]:
    declared = [str(m).strip() for m in task.materials if str(m).strip()]
    if declared:
        return declared
    for prefix, defaults in DEFAULT_MATERIALS_BY_PREFIX.items():
        if str(task.asset_id).startswith(prefix):
            return list(defaults)
    return []


def route_references_detailed(task: VisualTask, stylepack: StylePack,
                              accepted_assets: Mapping[str, Path] | None = None,
                              limit: int = DEFAULT_LIMIT) -> RouteResult:
    """路由参考图并返回 (绑定, 被省略项)。limit<=0 时全部省略。"""
    accepted = dict(accepted_assets or {})
    candidates: list[tuple[int, int, ReferenceBinding]] = []
    missing: list[dict] = []
    order = 0

    def add(rank: int, binding: ReferenceBinding) -> None:
        nonlocal order
        if not binding.path:
            return
        candidates.append((rank, order, binding))
        order += 1

    # 1) 上游已通过图片：身份 / 结构 / 空间
    #    accepted 的取值既可是绝对路径也可是库内相对路径：
    #    绑定一律保存**库内相对路径**（清单与计划绝不写机器绝对路径）。
    for dep in task.depends_on:
        path = accepted.get(dep)
        if path is None:
            continue
        rel = _library_relative(path)
        kind_of_dep = _kind_of_task_id(dep)
        if kind_of_dep in NEVER_REFERENCE_KINDS:
            # 拼版等预览产物永不进入参考链（尤其不得成为身份参考）
            continue
        role, note = UPSTREAM_REFERENCE.get(
            kind_of_dep, (ReferenceRole.STRUCTURE.value, "上游结构参考"))
        add(ROLE_PRIORITY[role], ReferenceBinding(path=rel, role=role, note=note))

    # 1b) 身份唯一真相：同一资产**已通过的正脸**始终作为身份参考。
    # 依赖链只表达生成顺序（全身正面的最近前置是服装背面），若只看 depends_on，
    # 全身/服装任务会丢掉身份锁 → 画风与五官漂移。身份必须独立于生成顺序路由。
    if task.kind != VisualTaskKind.FACE.value:
        face = _accepted_face_of(task, accepted)
        if face is not None and not any(
                b.role == ReferenceRole.IDENTITY.value for _r, _o, b in candidates):
            add(ROLE_PRIORITY[ReferenceRole.IDENTITY.value],
                ReferenceBinding(path=_library_relative(face[1]),
                                 role=ReferenceRole.IDENTITY.value,
                                 note="身份唯一真相（同资产已通过正脸）：锁定骨相与五官"))

    # 2) 内置包主体路由：主风格 / 空间 / 形制
    route = SUBJECT_ROUTE_BY_KIND.get(task.kind, "")
    if route:
        subject_path = stylepack.subject_image(route)
        if subject_path and stylepack.exists(subject_path):
            role = SUBJECT_ROLE_BY_KIND.get(task.kind, SUBJECT_ROLE_DEFAULT)
            note = {ReferenceRole.SPACE.value: SPACE_NOTE,
                    ReferenceRole.STRUCTURE.value: OBJECT_NOTE}.get(role, SUBJECT_NOTE)
            add(ROLE_PRIORITY[role],
                ReferenceBinding(path=subject_path, role=role, note=note))
        else:
            # 命中路由却取不到图：显式记录，绝不静默退化为「本任务无参考图」
            missing.append({
                "path": subject_path,
                "role": SUBJECT_ROLE_BY_KIND.get(task.kind, SUBJECT_ROLE_DEFAULT),
                "reason": f"权威风格包缺少主体路由图 {route!r}（风格包不完整，须先修复风格包）",
            })

    # 3) 实际材质：只取画面中真实存在且内置包里有对应参考的材质
    material_index = 0
    for name in _materials_for(task):
        material_route = MATERIAL_ROUTE_BY_NAME.get(name)
        if not material_route:
            continue
        path = stylepack.material_image(material_route)
        if not path or not stylepack.exists(path):
            missing.append({
                "path": path,
                "role": ReferenceRole.MATERIAL.value,
                "reason": f"权威风格包缺少材质路由图 {material_route!r}（材质 {name}）",
            })
            continue
        rank = ROLE_PRIORITY[ReferenceRole.MATERIAL.value] if material_index == 0 \
            else SECONDARY_MATERIAL_RANK
        material_index += 1
        add(rank, ReferenceBinding(
            path=path, role=ReferenceRole.MATERIAL.value,
            note=f"{name}材质参考：只决定表面反应，不得覆盖资产身份与其他材质事实"))

    candidates.sort(key=lambda item: (item[0], item[1]))
    keep = max(0, int(limit))
    kept = tuple(b for _rank, _order, b in candidates[:keep])
    omitted = tuple({
        "path": b.path,
        "role": b.role,
        "reason": f"超出参考图上限 {keep} 张（优先级：身份→结构→主风格→关键材质→次要材质）",
    } for _rank, _order, b in candidates[keep:])
    return RouteResult(bindings=kept, omitted=omitted, missing=tuple(missing))


def route_references(task: VisualTask, stylepack: StylePack,
                     accepted_assets: Mapping[str, Path] | None = None,
                     limit: int = DEFAULT_LIMIT) -> list[ReferenceBinding]:
    """按职责选择最小参考集（被省略的参考见 route_references_detailed）。"""
    return list(route_references_detailed(task, stylepack, accepted_assets, limit).bindings)
