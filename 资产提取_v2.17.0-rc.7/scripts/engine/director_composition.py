#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导演构图推导器（阶段 3 工单 3.1）。

事实优先级固定：**原始剧本 > 资产索引 > 分镜提示词 > 导演推导**。

- 剧本写明的关系（「挡在…之间」「护住」）直接成为前后景与遮挡事实；
- 剧本没写站位而分镜写了（「画面左侧」），采用分镜位置并标记来源为分镜；
- 两层冲突时**保留剧本事实**并产出冲突记录（分镜不得改写剧本与资产身份）；
- 三层都没说明时给出可解释的导演推导，标记来源为导演推导，绝不伪装成原文事实。

默认规则（设计 §7）：人物设定图正面/正后方、平视、居中、完整站立；剧情类用正常人眼高度；
没有剧情理由不得使用极端俯仰、荷兰角、贴脸广角或夸张留白——这些一律写进禁止项。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .visual_schema import (
    SOURCE_LEVELS,
    CompositionSpec,
    PropPlacement,
    SubjectPlacement,
    VisualTask,
)

SOURCE_SCRIPT = "原始剧本"
SOURCE_ASSET = "资产索引"
SOURCE_STORYBOARD = "分镜提示词"
SOURCE_DIRECTOR = "导演推导"

COMPOSITION_DIR_REL = ("视觉资产", "构图卡")
CARD_SUFFIX = ".toml"

SETUP_KINDS = ("正脸", "后脑", "服装独立正面", "服装独立背面",
               "人物全身正面", "人物全身背面")
SCENE_KINDS = ("场景母版",)

DEFAULT_FORBIDDEN = ("禁止俯拍/仰拍等极端机位（无剧情理由时）",
                     "禁止荷兰角与倾斜构图",
                     "禁止贴脸广角与夸张留白",
                     "禁止出现剧本未写明的额外人物、文字与水印")

_PROTECT_WORDS = ("挡在", "护住", "保护", "拦在", "挡着")
_ZONE_WORDS = (("左侧", "左"), ("右边", "右"), ("右侧", "右"), ("左", "左"), ("右", "右"),
               ("中央", "中"), ("中间", "中"), ("正中", "中"))
_SHOT_WORDS = ("远景", "全景", "中景", "近景", "特写")
_ANGLE_WORDS = ("平视", "俯视", "俯拍", "仰视", "仰拍", "越肩", "侧视")


def _sentence_with(text: str, words: tuple[str, ...]) -> str:
    if not text:
        return ""
    for chunk in re.split(r"[。！？；\n]", str(text)):
        if any(word in chunk for word in words):
            return chunk.strip()
    return ""


def _zone_from(text: str) -> tuple[str, str]:
    for word, zone in _ZONE_WORDS:
        if word in text:
            return zone, word
    return "", ""


def _shot_from(text: str) -> str:
    for word in _SHOT_WORDS:
        if word in text:
            return word
    return ""


def _angle_from(text: str) -> str:
    for word in _ANGLE_WORDS:
        if word in text:
            return {"俯拍": "俯视", "仰拍": "仰视"}.get(word, word)
    return ""


def _mentions(text: str, known: tuple[str, ...]) -> list[str]:
    """按给定人物名单在文本中的出现顺序提取（去重）。"""
    found: list[tuple[int, str]] = []
    for name in known:
        index = str(text).find(name)
        if index >= 0 and name not in [item[1] for item in found]:
            found.append((index, name))
    return [name for _, name in sorted(found)]


@dataclass(frozen=True)
class CompositionCard:
    """一张构图卡：结构化事实 + 逐字段依据 + 冲突记录。"""

    task_id: str = ""
    spec: CompositionSpec = None  # type: ignore[assignment]
    evidence: dict = None  # type: ignore[assignment]
    conflicts: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        if self.spec is None:
            object.__setattr__(self, "spec", CompositionSpec(shot_size="近景",
                                                             source_level=SOURCE_DIRECTOR))
        if self.evidence is None:
            object.__setattr__(self, "evidence", {})

    def to_toml(self) -> str:
        spec = self.spec
        lines = [
            "# 导演构图卡（阶段 3）：由 director_composition 生成，机器事实源，不要手工改。",
            f"task_id = {_q(self.task_id)}",
            f"shot_size = {_q(spec.shot_size)}",
            f"camera_angle = {_q(spec.camera_angle)}",
            f"camera_height = {_q(spec.camera_height)}",
            f"source_level = {_q(spec.source_level)}",
            f"rationale = {_q(spec.rationale)}",
            "anchors = " + _arr(spec.anchors),
            "forbidden = " + _arr(spec.forbidden),
        ]
        for row in spec.subjects:
            lines += ["", "[[subjects]]",
                      f"subject = {_q(row.subject)}", f"zone = {_q(row.zone)}",
                      f"depth = {_q(row.depth)}", f"facing = {_q(row.facing)}",
                      f"gaze = {_q(row.gaze)}", f"action = {_q(row.action)}",
                      f"occlusion = {_q(row.occlusion)}", f"distance = {_q(row.distance)}"]
        for row in spec.props:
            lines += ["", "[[props]]", f"name = {_q(row.name)}", f"zone = {_q(row.zone)}",
                      f"holder = {_q(row.holder)}", f"count = {int(row.count)}",
                      f"state = {_q(row.state)}"]
        for field, item in sorted(self.evidence.items()):
            lines += ["", "[[evidence]]", f"field = {_q(field)}",
                      f"source = {_q(str(item.get('source', '')))}",
                      f"quote = {_q(str(item.get('quote', '')))}"]
        for conflict in self.conflicts:
            lines += ["", "[[conflicts]]",
                      f"field = {_q(str(conflict.get('field', '')))}",
                      f"kept = {_q(str(conflict.get('kept', '')))}",
                      f"script = {_q(str(conflict.get('script', '')))}",
                      f"storyboard = {_q(str(conflict.get('storyboard', '')))}"]
        return "\n".join(lines).rstrip("\n") + "\n"


def _q(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _arr(values) -> str:
    return "[" + ", ".join(_q(v) for v in values) + "]"


def composition_path(lib: Path, task_id: str) -> Path:
    return Path(lib) / COMPOSITION_DIR_REL[0] / COMPOSITION_DIR_REL[1] / f"{task_id}{CARD_SUFFIX}"


def write_composition_card(lib: Path, card: CompositionCard) -> Path:
    """原子写出构图卡（同值重写字节稳定）。"""
    target = composition_path(lib, card.task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(card.to_toml(), encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return target


def load_composition_card(path: Path) -> "CompositionCard":
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"构图卡不存在: {target}")
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 回退
        import tomli as tomllib  # type: ignore

    with open(target, "rb") as fh:
        data = tomllib.load(fh)
    subjects = tuple(SubjectPlacement(
        subject=str(row.get("subject", "")), zone=str(row.get("zone", "中")),
        depth=str(row.get("depth", "中景")), facing=str(row.get("facing", "")),
        gaze=str(row.get("gaze", "")), action=str(row.get("action", "")),
        occlusion=str(row.get("occlusion", "")), distance=str(row.get("distance", "")),
    ) for row in data.get("subjects", ()) if isinstance(row, dict))
    props = tuple(PropPlacement(
        name=str(row.get("name", "")), zone=str(row.get("zone", "中")),
        holder=str(row.get("holder", "")), count=int(row.get("count", 1) or 1),
        state=str(row.get("state", "")),
    ) for row in data.get("props", ()) if isinstance(row, dict))
    spec = CompositionSpec(
        shot_size=str(data.get("shot_size", "")), camera_angle=str(data.get("camera_angle", "")),
        camera_height=str(data.get("camera_height", "")), subjects=subjects, props=props,
        anchors=tuple(str(x) for x in data.get("anchors", ())),
        forbidden=tuple(str(x) for x in data.get("forbidden", ())),
        rationale=str(data.get("rationale", "")),
        source_level=str(data.get("source_level", SOURCE_DIRECTOR)),
    )
    evidence = {str(row.get("field", "")): {"source": str(row.get("source", "")),
                                            "quote": str(row.get("quote", ""))}
                for row in data.get("evidence", ()) if isinstance(row, dict)}
    conflicts = tuple({"field": str(row.get("field", "")), "kept": str(row.get("kept", "")),
                       "script": str(row.get("script", "")),
                       "storyboard": str(row.get("storyboard", ""))}
                      for row in data.get("conflicts", ()) if isinstance(row, dict))
    return CompositionCard(task_id=str(data.get("task_id", "")), spec=spec,
                           evidence=evidence, conflicts=conflicts)


def _defaults_for(kind: str) -> dict:
    """按任务种类给出设定图/剧情图的默认镜头（来源一律标记为导演推导）。"""
    if kind == "正脸":
        return dict(shot_size="近景", camera_angle="平视", camera_height="与人物面部同高",
                    depth="中景", facing="朝向镜头", gaze="看向镜头", action="自然站立")
    if kind == "后脑":
        return dict(shot_size="近景", camera_angle="平视", camera_height="与人物面部同高",
                    depth="中景", facing="背对镜头", gaze="朝向画面深处", action="自然站立")
    if kind in ("服装独立正面", "服装独立背面"):
        return dict(shot_size="全景", camera_angle="平视", camera_height="与人物胸口同高",
                    depth="中景",
                    facing="朝向镜头" if kind.endswith("正面") else "背对镜头",
                    gaze="朝向镜头" if kind.endswith("正面") else "朝向画面深处",
                    action="自然站立，服装完整可读")
    if kind in ("人物全身正面", "人物全身背面"):
        return dict(shot_size="全景", camera_angle="平视", camera_height="与人物胸口同高",
                    depth="中景",
                    facing="朝向镜头" if kind.endswith("正面") else "背对镜头",
                    gaze="朝向镜头" if kind.endswith("正面") else "朝向画面深处",
                    action="自然站立，头顶到脚完整入画")
    if kind in SCENE_KINDS:
        return dict(shot_size="远景", camera_angle="平视", camera_height="正常人眼高度",
                    depth="中景", facing="", gaze="", action="")
    if kind == "道具":
        return dict(shot_size="特写", camera_angle="平视", camera_height="与道具同高",
                    depth="中景", facing="", gaze="", action="静置")
    return dict(shot_size="中景", camera_angle="平视", camera_height="正常人眼高度",
                depth="中景", facing="", gaze="", action="")


def derive_composition(task: VisualTask, asset: dict | None = None, *,
                       script_text: str = "", storyboard_text: str = "",
                       known_subjects: tuple[str, ...] = (),
                       scene_anchors: tuple[str, ...] = ()) -> CompositionCard:
    """推导构图卡：剧本 > 资产 > 分镜 > 导演推导，逐字段记录来源并留冲突记录。"""
    asset = dict(asset or {})
    defaults = _defaults_for(str(task.kind))
    evidence: dict[str, dict] = {}
    conflicts: list[dict] = []

    def mark(field: str, source: str, quote: str) -> None:
        evidence[field] = {"source": source, "quote": quote}

    script = str(script_text or "")
    board = str(storyboard_text or "")

    # --- 景别 / 角度：剧本不写景别，分镜可定；否则用默认
    shot_size = _shot_from(board) or defaults["shot_size"]
    mark("shot_size", SOURCE_STORYBOARD if _shot_from(board) else SOURCE_DIRECTOR,
         _sentence_with(board, ("景",)) or "默认规则：设定图与剧情图的默认景别")

    angle = _angle_from(board) or defaults["camera_angle"]
    mark("camera_angle", SOURCE_STORYBOARD if _angle_from(board) else SOURCE_DIRECTOR,
         _sentence_with(board, _ANGLE_WORDS) or "默认规则：平视，不擅自使用极端机位")

    # --- 人物：名单来自剧本/分镜提及，与资产事实核对
    names = _mentions(script, known_subjects) or _mentions(board, known_subjects)
    primary = str(asset.get("name") or task.asset_id or "").strip()
    if not names:
        names = [primary or task.asset_id or "主体"]
    if primary and primary not in names:
        names = [primary] + names
    script_protect = _sentence_with(script, _PROTECT_WORDS)
    script_zone, script_zone_word = _zone_from(script)
    board_zone, board_zone_word = _zone_from(board)

    subjects: list[SubjectPlacement] = []
    for index, name in enumerate(names):
        is_primary = index == 0
        zone = None
        if is_primary:
            if script_zone:
                zone = script_zone
                mark("zone", SOURCE_SCRIPT, _sentence_with(script, (script_zone_word,)))
                if board_zone and board_zone != script_zone:
                    conflicts.append({"field": "zone", "kept": SOURCE_SCRIPT,
                                      "script": _sentence_with(script, (script_zone_word,)),
                                      "storyboard": _sentence_with(board, (board_zone_word,))})
            elif board_zone:
                zone = board_zone
                mark("zone", SOURCE_STORYBOARD, _sentence_with(board, (board_zone_word,)))
            else:
                zone = "中"
                mark("zone", SOURCE_DIRECTOR, "默认规则：设定图居中，剧情图按保护/对峙关系居中处理")
        else:
            zone = "左" if zone != "左" else "右"
        depth = "中景"
        occlusion = ""
        if is_primary and script_protect:
            depth = "前景"
            occlusion = script_protect
            mark("occlusion", SOURCE_SCRIPT, script_protect)
            mark("depth", SOURCE_SCRIPT, script_protect)
        elif is_primary:
            mark("depth", SOURCE_DIRECTOR, "默认规则：主体位于中景，前后关系按剧情补足")
        subjects.append(SubjectPlacement(
            subject=name, zone=zone, depth=depth,
            facing=(defaults.get("facing") or "") if is_primary else "",
            gaze=(defaults.get("gaze") or "") if is_primary else "",
            action=(defaults.get("action") or "") if is_primary else "",
            occlusion=occlusion,
            distance="" if is_primary else "与主体保持可辨识距离"))

    if board and ("改为" in board or "换成" in board):
        conflicts.append({"field": "identity", "kept": SOURCE_ASSET,
                          "script": primary, "storyboard": _sentence_with(board, ("改为", "换成"))})

    # --- 道具：持有关系只认资产索引
    props: list[PropPlacement] = []
    for relation in asset.get("relations") or ():
        if not isinstance(relation, dict) or str(relation.get("type", "")).upper() != "HOLDS":
            continue
        props.append(PropPlacement(name=str(relation.get("target", "")), zone="中",
                                   holder=str(asset.get("name") or task.asset_id),
                                   count=int(relation.get("count", 1) or 1)))
    if props:
        mark("props", SOURCE_ASSET, "资产索引 HOLDS 关系")

    anchors = tuple(scene_anchors) or tuple(
        str(item) for item in (asset.get("anchors") or ()) if str(item))
    if anchors:
        mark("anchors", SOURCE_ASSET, "资产索引锚点")
    else:
        anchors = ("场景母版未建立时先留空，建立后必须继承同一空间结构",)
        mark("anchors", SOURCE_DIRECTOR, "默认规则：场景锚点随场景母版建立后继承")

    forbidden = list(DEFAULT_FORBIDDEN)
    if task.kind in SETUP_KINDS:
        forbidden.append("禁止多视图/拼版/无头身体（设定图只出单张单幅）")
    if task.kind in ("人物全身正面", "人物全身背面"):
        forbidden.append("禁止裁切头脚（必须完整站立入画）")

    # 顶层来源只由**构图决策字段**决定（站位/景别/机位/前后景/遮挡）；
    # 道具与锚点本来就来自资产索引，不参与判定，否则任何带道具的图都会被标成「资产索引」。
    decision_fields = ("shot_size", "camera_angle", "camera_height", "zone", "depth", "occlusion")
    sources = {evidence[field]["source"] for field in decision_fields if field in evidence}
    top = next((level for level in SOURCE_LEVELS if level in sources), SOURCE_DIRECTOR)

    rationale = _rationale(task, script_protect, script, board)

    spec = CompositionSpec(
        shot_size=shot_size, camera_angle=angle, camera_height=defaults["camera_height"],
        subjects=tuple(subjects), props=tuple(props), anchors=tuple(anchors),
        forbidden=tuple(forbidden), rationale=rationale, source_level=top,
    )
    return CompositionCard(task_id=task.task_id, spec=spec, evidence=evidence,
                           conflicts=tuple(conflicts))


def _rationale(task: VisualTask, script_protect: str, script: str, board: str) -> str:
    if script_protect:
        return f"依据剧本事实「{script_protect}」确定前后景与遮挡关系"
    if script:
        return f"依据剧本事实「{_sentence_with(script, (task.asset_id,)) or script[:40]}」" \
               "确定人物与景别，其余按默认规则"
    if board:
        return f"剧本未说明站位，采用分镜提示词「{board[:40]}」；其余按默认规则"
    return "三层事实均未说明，按默认规则给出可解释的导演推导（标记为导演推导，不冒充原文）"


def composition_issues(card: CompositionCard, asset: dict | None = None,
                       expected_subjects: tuple[str, ...] = ()) -> list[str]:
    """构图事实与资产关系的一致性检查（返回问题码列表，空列表=通过）。"""
    issues: list[str] = []
    subjects = {row.subject for row in card.spec.subjects}
    expected = set(expected_subjects)
    if expected and subjects != expected:
        missing = "、".join(sorted(expected - subjects))
        extra = "、".join(sorted(subjects - expected))
        issues.append(f"COMPOSITION_SUBJECT_MISMATCH：人物集合与剧本/资产不一致"
                      f"（缺 {missing or '无'}；多 {extra or '无'}）")
    holders = {row.holder for row in card.spec.props if row.holder}
    known = {str((asset or {}).get("name") or "")}
    for holder in holders:
        if holder and holder not in known:
            issues.append(f"COMPOSITION_PROP_HOLDER_MISMATCH：道具持有者 {holder} 不在资产事实中")
    return issues


# --- 构图预览模式：批准与失效（阶段 3 工单 3.4） ------------------------------------

COMPOSITION_NOT_APPROVED = "COMPOSITION_NOT_APPROVED"
COMPOSITION_CARD_MISSING = "COMPOSITION_CARD_MISSING"


def approval_path(lib: Path, task_id: str) -> Path:
    return Path(lib) / COMPOSITION_DIR_REL[0] / COMPOSITION_DIR_REL[1] / f"{task_id}.approved.json"


def approve_composition(lib: Path, task_id: str, *, approved_by: str = "宿主") -> Path:
    """批准某张构图卡：记录**当前卡内容的哈希**，之后改卡即自动失效。"""
    import hashlib
    import json

    card = composition_path(lib, task_id)
    if not card.is_file():
        raise FileNotFoundError(f"{COMPOSITION_CARD_MISSING}：构图卡不存在，无法批准（{card.name}）")
    digest = hashlib.sha256(card.read_bytes()).hexdigest()
    target = approval_path(lib, task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"task_id": task_id, "composition_sha256": digest,
                          "approved_by": approved_by}, ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return target


def is_composition_approved(lib: Path, task) -> bool:
    """构图卡是否已批准且**未被改动**（哈希现算比对）。"""
    import hashlib
    import json

    task_id = getattr(task, "task_id", "")
    card_rel = str(getattr(task, "composition_path", "") or "")
    if not card_rel:
        return False
    card = Path(lib) / card_rel
    if not card.is_file():
        return False
    approval = approval_path(lib, task_id)
    if not approval.is_file():
        return False
    try:
        data = json.loads(approval.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return str(data.get("composition_sha256", "")) == hashlib.sha256(card.read_bytes()).hexdigest()


def require_composition_approval(lib: Path, task, composition_mode: str) -> None:
    """preview 模式下未批准（或卡已改）即拒绝进入生成/导出。"""
    if str(composition_mode or "auto") != "preview":
        return
    if not is_composition_approved(lib, task):
        raise PermissionError(
            f"{COMPOSITION_NOT_APPROVED}：构图预览模式下必须先看构图卡并批准"
            f"（python scripts/visual_production.py approve-composition "
            f"--lib <资产库> --task {getattr(task, 'task_id', '')}）")
