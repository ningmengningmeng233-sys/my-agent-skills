#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小发送请求编译器（规格 2.14 / 7，工单03-Task3）。

- **最小发送**：只发当前任务事实、选中参考图与风格约束；严禁整部原稿、完整风格包、
  其他角色事实与任何绝对本机路径。
- 提示词分块固定为：任务目标 → 不可变事实 → 构图 → 风格 → 参考职责 → 材质 → 禁止项。
- Gemini / Codex 两个 profile 只允许调整块的**表达顺序**，事实集合必须完全相同。
- 发送清单只记录相对路径、职责、SHA-256 与字节数，**绝不记录密钥、认证头或原文全文**。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .art_constitution import (
    ArtConflictError,
    check_prompt_conflicts,
    enforce_pure_white_background,
)
from .image_executor import GenerationRequest, GenerationResult  # noqa: F401
from .reference_router import MATERIAL_ROUTE_BY_NAME
from .visual_planner import template_for_kind
from .visual_schema import (
    STORY_TASK_KINDS,
    ReferenceBinding,
    VisualTask,
    VisualTaskKind,
    normalize_rel_path,
)

PROMPT_BLOCKS = ("任务目标", "不可变事实", "构图", "风格", "参考职责", "材质", "禁止项")
PROFILES = ("gemini", "codex")
# profile 只影响块的排列顺序（表达顺序），不影响任何事实
BLOCK_ORDER_BY_PROFILE = {
    "gemini": PROMPT_BLOCKS,
    "codex": ("不可变事实", "任务目标", "构图", "风格", "材质", "参考职责", "禁止项"),
}
TASK_GOAL_BY_KIND = {
    "正脸": "输出该角色正脸特写单张画面，作为此后所有造型的身份唯一真相",
    "后脑": "输出该角色后脑与发型背面单张画面，不得出现面部",
    "服装独立正面": "输出服装正面独立展示单张画面，服装是主体",
    "服装独立背面": "输出服装背面独立展示单张画面，服装是主体",
    "人物全身正面": "输出穿好这身服装的全身正面单张画面，头脚完整入画",
    "人物全身背面": "输出穿好这身服装的全身背面单张画面，头脚完整入画，不得出现面部",
    "场景母版": "输出该场景空间母版单张画面，作为所有变体的空间基线",
    "道具": "输出该道具单张独立展示画面，轮廓与材质身份清晰",
}
ROLE_CN = {"identity": "身份", "structure": "结构", "style": "风格",
           "material": "材质", "lighting": "光影", "space": "空间"}
MAX_QUOTE_CHARS = 60
MAX_PROMPT_CHARS = 6000


class RequestBuildError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def _first_quote(asset: Mapping) -> str:
    for item in asset.get("evidence") or ():
        if isinstance(item, dict) and str(item.get("quote") or "").strip():
            return str(item["quote"]).strip()[:MAX_QUOTE_CHARS]
    return ""


def extract_private_facts(task: VisualTask, asset: Mapping) -> dict:
    """抽取**本任务**需要发送的最小事实（其他资产、整章原文一律不取）。"""
    if not isinstance(asset, Mapping):
        raise RequestBuildError("ASSET_REQUIRED", "缺少当前资产事实")
    return {
        "asset_id": str(asset.get("asset_id") or task.asset_id),
        "name": str(asset.get("name") or ""),
        "quote": _first_quote(asset),
        "identity_lock": str(task.notes or "").strip(),
    }


def _section(text: str, title: str) -> str:
    """取模板中某个二级小节的正文（缺节返回空串）。"""
    m = re.search(rf"^##\s+{re.escape(title)}\s*$\n(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


# 权威风格包内的固定文件（新任务的视觉规则唯一真相源）
PACK_SHARED_INVARIANTS = "references/shared_style_invariants.md"
PACK_SUBJECT_BASE = {
    "人物": "references/face_base_style.md",
    "全身": "references/full_body_base_style.md",
    "场景": "references/environment_base_style.md",
    "道具": "references/object_base_style.md",
    "服装": "references/full_body_base_style.md",
}
PACK_MATERIAL_BASE = "references/materials/{}_base_style.md"
# 生成公式里必须整段带入的块（介质/光色/构图三段共同定义风格）
FORMULA_BLOCKS = ("BASE_STYLE", "LIGHT_COLOR", "COMPOSITION")

# 任务种类 → 权威包主体基线：全身/服装类必须用全身基线，**不得继承正脸基线**
FULL_BODY_KINDS = (
    VisualTaskKind.FULL_BODY_FRONT.value,
    VisualTaskKind.FULL_BODY_BACK.value,
    VisualTaskKind.COSTUME_FRONT.value,
    VisualTaskKind.COSTUME_BACK.value,
    VisualTaskKind.BACK_HEAD.value,
)


def _subject_for_kind(kind: str) -> str:
    """任务种类 → 权威包主体基线键（全身/服装/后脑一律用全身基线）。"""
    if kind.startswith("场景"):
        return "场景"
    if kind.startswith("道具"):
        return "道具"
    if kind in FULL_BODY_KINDS:
        return "全身"
    return "人物"


def _pack_text(stylepack: Any, rel: str) -> str:
    """读取权威包内文件正文；缺失即失败（风格源不完整不得继续）。"""
    root = getattr(stylepack, "root", None)
    if root is None:
        raise RequestBuildError("STYLEPACK_UNAVAILABLE", "未提供权威风格包（新任务只允许权威包）")
    path = Path(root) / rel
    if not path.is_file():
        raise RequestBuildError("STYLEPACK_INCOMPLETE", f"权威包缺少必需文件：{rel}")
    return path.read_text(encoding="utf-8").strip()


def _formula_block(stylepack: Any) -> str:
    """从生成公式提取介质/光色/构图三段（只取这三段，不夹带整包其他内容）。"""
    text = _pack_text(stylepack, "generation_formula.md")
    out: list[str] = []
    for name in FORMULA_BLOCKS:
        m = re.search(rf"^\[{name}\]\s*$\n(.+?)(?=^\[|\Z)", text, re.M | re.S)
        if m:
            out.append(f"[{name}]\n{m.group(1).strip()}")
    if not out:
        raise RequestBuildError("STYLEPACK_INCOMPLETE",
                                "生成公式缺少 [BASE_STYLE]/[LIGHT_COLOR]/[COMPOSITION] 段")
    return "\n".join(out)


def _style_blocks(stylepack: Any, record: Any, task: VisualTask) -> tuple[str, str, str]:
    """返回 (风格块, 材质块, 禁止项块)，全部来自**权威 Omni 包**。

    旧 `画风库.md`/H7 卡片**不再参与**新任务编译：只有权威包的共同约束、
    生成公式、主体基线与负面提示词构成视觉规则。
    """
    subject = _subject_for_kind(task.kind)
    name = getattr(record, "name", "") or task.style_slug
    header = f"{name}@{task.style_version}（权威 Omni 包；内容哈希 {task.style_hash[:12]}…）"
    style_block = "\n\n".join([
        header,
        _pack_text(stylepack, PACK_SHARED_INVARIANTS),
        _formula_block(stylepack),
        _pack_text(stylepack, PACK_SUBJECT_BASE[subject]),
    ])

    material_lines: list[str] = []
    for material_name in task.materials:
        route = MATERIAL_ROUTE_BY_NAME.get(str(material_name).strip())
        if not route:
            continue
        rel = PACK_MATERIAL_BASE.format(route)
        if (Path(stylepack.root) / rel).is_file():
            material_lines.append(f"- {material_name}：{_pack_text(stylepack, rel)}")
    material_block = ("仅使用画面中真实存在的材质，材质反应以权威包基线为准：\n"
                      + "\n".join(material_lines)) if material_lines else ""

    negative = _pack_text(stylepack, "negative_prompt.md")
    task_bans = _section(template_for_kind(task.kind), "禁止项")
    if task_bans:
        negative = negative + "\n\n任务专属禁止项：\n" + task_bans
    return style_block, material_block, negative


def _composition_block(task: VisualTask, stylepack: Any, composition: Any = None) -> str:
    """构图块：导演构图卡事实 + 任务模板结构规则 + 权威包主体基线。

    构图卡必须排在画风包之前（阶段 3 工单 3.3 的固定编译顺序；
    全身任务不得继承正脸裁切规则）。
    """
    template = template_for_kind(task.kind)
    body = _section(template, "构图")
    subject = _subject_for_kind(task.kind)
    subject_base = _pack_text(stylepack, PACK_SUBJECT_BASE[subject])
    parts = [part for part in (body, f"主体基线（权威包 {subject}）：{subject_base}") if part]
    director = _director_facts(task, composition)
    if director:
        parts.insert(0, director)
    return ("\n\n".join(parts) + f"\n（画幅 {task.aspect_ratio}）").strip()


def _director_facts(task: VisualTask, composition: Any) -> str:
    """把构图卡渲染成可审核的结构化条目（无构图卡时返回空串，不凭空编造）。"""
    if composition is None:
        return ""
    spec = getattr(composition, "spec", composition)
    lines = ["导演构图卡（事实来源：%s）：" % getattr(spec, "source_level", "") or "导演构图卡："]
    lines[0] = f"导演构图卡（事实来源：{getattr(spec, 'source_level', '')}）："
    for label, value in (("景别", getattr(spec, "shot_size", "")),
                         ("视角", getattr(spec, "camera_angle", "")),
                         ("机位高度", getattr(spec, "camera_height", "")),
                         ("构图理由", getattr(spec, "rationale", ""))):
        if value:
            lines.append(f"- {label}：{value}")
    for row in getattr(spec, "subjects", ()) or ():
        bits = [f"{row.subject}位于{row.zone}区（{row.depth}）"]
        for label, value in (("朝向", row.facing), ("视线", row.gaze), ("动作", row.action),
                             ("遮挡", row.occlusion), ("距离", row.distance)):
            if value:
                bits.append(f"{label}{value}")
        lines.append("- 人物：" + "，".join(bits))
    for row in getattr(spec, "props", ()) or ():
        bits = [f"{row.name}位于{row.zone}区", f"数量 {row.count}"]
        if row.holder:
            bits.append(f"持有者 {row.holder}")
        if row.state:
            bits.append(f"状态 {row.state}")
        lines.append("- 道具：" + "，".join(bits))
    if getattr(spec, "anchors", ()):
        lines.append("- 场景锚点：" + "；".join(spec.anchors))
    if getattr(spec, "forbidden", ()):
        lines.append("- 构图禁止项：" + "；".join(spec.forbidden))
    return "\n".join(lines)


def _facts_block(task: VisualTask, facts: Mapping[str, str]) -> str:
    lines = [f"- 资产：{facts['name']}（{facts['asset_id']}）"]
    if facts["quote"]:
        lines.append(f"- 原文依据：{facts['quote']}")
    if facts["identity_lock"]:
        lines.append(f"- 身份/结构锁：{facts['identity_lock']}")
    if task.depends_on:
        lines.append("- 依赖（须先通过）：" + "、".join(task.depends_on))
    return "\n".join(lines)


def _reference_block(refs: Sequence[ReferenceBinding]) -> str:
    if not refs:
        return "本任务无参考图，仅按上述事实与风格生成。"
    lines = ["参考图按职责使用，职责不得越权："]
    for binding in refs:
        role_cn = ROLE_CN.get(binding.role, binding.role)
        line = f"- {role_cn}：{normalize_rel_path(binding.path)}"
        if binding.note:
            line += f"｜{binding.note}"
        lines.append(line)
    return "\n".join(lines)


def build_generation_request(task: VisualTask, asset: Mapping, stylepack: Any,
                             refs: Sequence[ReferenceBinding],
                             engine_profile: str = "gemini",
                             composition: Any = None) -> GenerationRequest:
    """编译最小发送请求（块顺序随 profile，事实集合不变）。

    视觉规则**只能**来自权威 Omni 风格包：`stylepack` 为空即拒绝任务，
    绝不回退到旧 `画风库.md`/H7 卡片（否则会出现画风漂移与裁切规则错配）。

    剧情类任务（剧情剧照/动作参考/材质特写）必须有分镜出处与导演构图卡：
    不允许从剧情摘要直接跳到生图（设计 §11）。
    """
    profile = str(engine_profile or "gemini").strip().lower()
    if profile not in PROFILES:
        raise RequestBuildError(
            "UNKNOWN_PROFILE", f"未知引擎 profile {engine_profile!r}（只允许 {list(PROFILES)}）")
    if task.kind in STORY_TASK_KINDS:
        if not str(task.storyboard_source or "").strip():
            raise RequestBuildError(
                "STORYBOARD_REQUIRED", f"{task.kind} 必须记录分镜提示词出处，不得从剧情摘要直接生图")
        if composition is None and not str(task.composition_path or "").strip():
            raise RequestBuildError(
                "COMPOSITION_REQUIRED", f"{task.kind} 生成前必须有导演构图卡")
    if not task.style_hash or not task.style_version:
        raise RequestBuildError("STYLE_UNPINNED", "任务未固定风格包版本与内容哈希")
    if stylepack is None or getattr(stylepack, "root", None) is None:
        raise RequestBuildError(
            "STYLEPACK_UNAVAILABLE",
            "缺少权威 Omni 风格包：新任务只允许权威包作为视觉规则源，不得回退旧画风库")

    from .style_registry import resolve_style_record

    record = resolve_style_record(task.style_slug)
    if record is None:
        raise RequestBuildError("STYLE_UNKNOWN", f"风格 {task.style_slug!r} 不在登记表中")

    facts = extract_private_facts(task, asset)
    style_block, material_block, negative = _style_blocks(stylepack, record, task)
    # 人物/服装基础资产固定纯白背景，风格包中的环境/棚拍底不得覆盖（工单 5）
    style_block = enforce_pure_white_background(style_block, task.kind)
    negatives = [part.strip() for part in (negative, task.negative_prompt) if str(part or "").strip()]
    blocks = {
        "任务目标": TASK_GOAL_BY_KIND.get(task.kind, f"输出 {task.kind} 单张画面"),
        "不可变事实": _facts_block(task, facts),
        "构图": _composition_block(task, stylepack, composition),
        "风格": style_block,
        "参考职责": _reference_block(refs),
        "材质": material_block or "（本任务未声明材质参考）",
        "禁止项": "见负面提示词字段；另须遵守：" + (negative or "无额外禁用项"),
    }
    order = BLOCK_ORDER_BY_PROFILE[profile]
    prompt = "\n\n".join(f"【{name}】\n{blocks[name]}".rstrip() for name in order)
    try:
        check_prompt_conflicts(prompt)
    except ArtConflictError as exc:
        raise RequestBuildError("ART_CONFLICT", str(exc)) from exc
    if len(prompt) > MAX_PROMPT_CHARS:
        raise RequestBuildError(
            "PROMPT_TOO_LARGE",
            f"提示词 {len(prompt)} 字超过上限 {MAX_PROMPT_CHARS}（最小发送原则不允许夹带全文）")

    return GenerationRequest(
        task_id=task.task_id,
        prompt=prompt,
        negative_prompt="\n".join(negatives),
        references=tuple(refs),
        aspect_ratio=task.aspect_ratio,
        image_size="2K",
        output_mime="image/png",
        style_name=record.name,
        style_version=task.style_version,
        reference_roles=tuple(r.role for r in refs),
        extra={"engine_profile": profile, "model": task.model},
    )


def prompt_fact_set(prompt: str) -> dict:
    """把提示词拆成 {块名: 归一化正文}，用于跨引擎一致性比较。"""
    parts = re.split(r"^【([^】]+)】\s*$", prompt, flags=re.M)
    out: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        name = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        # 归一化空白与块内条目顺序：只比较事实内容
        items = sorted(line.strip() for line in body.splitlines() if line.strip())
        out[name] = "|".join(items)
    return out


def build_send_manifest(request: GenerationRequest) -> dict:
    """可审查的发送清单：只有相对路径、职责、哈希、尺寸与规模，绝无密钥与全文。"""
    manifest = request.to_manifest()
    manifest["prompt_chars"] = len(request.prompt)
    manifest["negative_chars"] = len(request.negative_prompt)
    manifest["reference_count"] = len(request.references)
    manifest["reference_roles"] = list(request.reference_roles)
    manifest["prompt_blocks"] = list(prompt_fact_set(request.prompt))
    manifest["engine_profile"] = str(request.extra.get("engine_profile", ""))
    return manifest
