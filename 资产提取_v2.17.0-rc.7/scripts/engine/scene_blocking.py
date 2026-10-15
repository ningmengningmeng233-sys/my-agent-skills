#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""场景站位图与连续性检查（阶段 3 工单 3.2）。

一张场景站位图固定一件空间事实：入口/出口/通道、固定物（锚点）、可站立区域、前中后景、
默认拍摄方向、行动轴线与不可穿越区域。同一场景的后续图片都继承这份结构，
因此画面之间的左右位置、前后遮挡与视线关系可以被机械检查。

检查只比较**语义关系**（左中右、前中后、朝向、视线、轴线），不做像素级坐标比较：
人物可以在指定区域内小幅移动，但不能无故左右互换。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .visual_schema import DEPTH_VALUES, SubjectPlacement, ZONE_VALUES

BLOCKING_DIR_REL = ("视觉资产", "场景站位图")
BLOCKING_SUFFIX = ".toml"

POSITION_MISMATCH = "POSITION_MISMATCH"
VIEWPOINT_MISMATCH = "VIEWPOINT_MISMATCH"
EYELINE_MISMATCH = "EYELINE_MISMATCH"
AXIS_CROSSING = "AXIS_CROSSING"
OCCLUSION_MISMATCH = "OCCLUSION_MISMATCH"
SCENE_ANCHOR_DRIFT = "SPACE_ANCHOR_DRIFT"

DEFAULT_CAMERA_HEIGHTS = ("平视",)
# 同一「语义区域」的等价写法：区域内小幅移动不算漂移
ZONE_FAMILY = {"左": "左中", "左中": "左中", "中": "左中", "右": "右", "中右": "右"}


def _zone_family(zone: str) -> str:
    return ZONE_FAMILY.get(str(zone or ""), str(zone or ""))


@dataclass(frozen=True)
class SceneBlocking:
    """一个场景的空间结构（不可变；同一场景只有一份）。"""

    scene_id: str = ""
    anchors: tuple[str, ...] = ()
    entrances: tuple[str, ...] = ()
    exits: tuple[str, ...] = ()
    standable: dict = field(default_factory=dict)
    default_camera_direction: str = ""
    axis: tuple[str, ...] = ()
    no_cross_zones: tuple[str, ...] = ()
    notes: str = ""

    def to_toml(self) -> str:
        lines = [
            "# 场景站位图（阶段 3 工单 3.2）：同一场景的后续图片必须继承本结构。",
            f"scene_id = {_q(self.scene_id)}",
            "anchors = " + _arr(self.anchors),
            "entrances = " + _arr(self.entrances),
            "exits = " + _arr(self.exits),
            f"default_camera_direction = {_q(self.default_camera_direction)}",
            "axis = " + _arr(self.axis),
            "no_cross_zones = " + _arr(self.no_cross_zones),
            f"notes = {_q(self.notes)}",
            "",
            "[standable]",
        ]
        for zone, desc in sorted(self.standable.items()):
            lines.append(f'"{zone}" = {_q(str(desc))}')
        return "\n".join(lines).rstrip("\n") + "\n"


@dataclass(frozen=True)
class ShotPlacement:
    """一个镜头里的人物站位快照（由构图卡派生）。"""

    task_id: str = ""
    subjects: tuple[SubjectPlacement, ...] = ()
    camera_height: str = "平视"
    anchors: tuple[str, ...] = ()
    axis_side: str = "same"

    def zone_of(self, subject: str) -> str:
        for row in self.subjects:
            if row.subject == subject:
                return str(row.zone)
        return ""

    def depth_of(self, subject: str) -> str:
        for row in self.subjects:
            if row.subject == subject:
                return str(row.depth)
        return ""


def _q(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _arr(values) -> str:
    return "[" + ", ".join(_q(v) for v in values) + "]"


def build_scene_blocking(scene_id: str, *, anchors=(), entrances=(), exits=(),
                         standable: dict | None = None, camera_direction: str = "",
                         axis=(), no_cross_zones=(), notes: str = "") -> SceneBlocking:
    if not str(scene_id or "").strip():
        raise ValueError("场景站位图必须指明场景 ID")
    for zone in (standable or {}):
        if zone not in ZONE_VALUES:
            raise ValueError(f"可站立区域只允许 {ZONE_VALUES}，当前 {zone!r}")
    return SceneBlocking(scene_id=str(scene_id), anchors=tuple(anchors),
                         entrances=tuple(entrances), exits=tuple(exits),
                         standable=dict(standable or {}), default_camera_direction=str(camera_direction),
                         axis=tuple(axis), no_cross_zones=tuple(no_cross_zones), notes=str(notes))


def blocking_path(lib: Path, scene_id: str) -> Path:
    return Path(lib) / BLOCKING_DIR_REL[0] / BLOCKING_DIR_REL[1] / f"{scene_id}{BLOCKING_SUFFIX}"


def write_scene_blocking(lib: Path, blocking: SceneBlocking) -> Path:
    target = blocking_path(lib, blocking.scene_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(blocking.to_toml(), encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return target


def load_scene_blocking(path: Path) -> SceneBlocking:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"场景站位图不存在: {target}")
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 回退
        import tomli as tomllib  # type: ignore

    with open(target, "rb") as fh:
        data = tomllib.load(fh)
    return build_scene_blocking(
        str(data.get("scene_id", "")),
        anchors=tuple(str(x) for x in data.get("anchors", ())),
        entrances=tuple(str(x) for x in data.get("entrances", ())),
        exits=tuple(str(x) for x in data.get("exits", ())),
        standable={str(k): str(v) for k, v in (data.get("standable") or {}).items()},
        camera_direction=str(data.get("default_camera_direction", "")),
        axis=tuple(str(x) for x in data.get("axis", ())),
        no_cross_zones=tuple(str(x) for x in data.get("no_cross_zones", ())),
        notes=str(data.get("notes", "")),
    )


def _eyeline_pairs(shot: ShotPlacement) -> dict[str, str]:
    """从「看向…」文本里解析出视线指向的语义目标（左/右/中或人物名）。"""
    out: dict[str, str] = {}
    for row in shot.subjects:
        gaze = str(row.gaze or "")
        if not gaze:
            continue
        for token, key in (("画面外", "画面外"), ("镜头", "镜头"), ("左", "左"), ("右", "右"),
                           ("中", "中")):
            if token in gaze:
                out[row.subject] = key
                break
        for other in shot.subjects:
            if other.subject != row.subject and other.subject in gaze:
                out[row.subject] = other.subject
                break
    return out


def check_continuity(blocking: SceneBlocking, previous: ShotPlacement | None,
                     current: ShotPlacement, *, moved=(), axis_cross_explained: bool = False,
                     viewpoint_explained: bool = False) -> list[tuple[str, str]]:
    """比较相邻镜头的站位关系；返回 (问题码, 说明) 列表，空列表=通过。"""
    issues: list[tuple[str, str]] = []

    # 场景锚点：同一场景的每个镜头都必须继承固定物
    missing = [item for item in blocking.anchors if item not in (current.anchors or ())]
    if current.anchors and missing:
        issues.append((SCENE_ANCHOR_DRIFT,
                       f"镜头 {current.task_id} 缺少场景锚点：{'、'.join(missing)}"))

    # 机位高度：默认平视，改变必须有理由
    if current.camera_height and current.camera_height not in DEFAULT_CAMERA_HEIGHTS \
            and not viewpoint_explained:
        issues.append((VIEWPOINT_MISMATCH,
                       f"镜头 {current.task_id} 机位 {current.camera_height} 与默认平视不一致，"
                       "且未给出剧情理由"))

    # 越轴：机位换到轴线另一侧必须显式说明（明确穿越轴线的镜头允许）
    if previous is not None and str(current.axis_side) != str(previous.axis_side) \
            and not axis_cross_explained:
        issues.append((AXIS_CROSSING,
                       f"镜头 {current.task_id} 换到行动轴线另一侧，未说明穿越理由"))

    # 视线对应：两人互相看向对方时，方向必须彼此指向
    if previous is not None:
        pairs = _eyeline_pairs(previous)
        now = _eyeline_pairs(current)
        for subject, target in pairs.items():
            if target in ("镜头", "画面外"):
                continue
            after = now.get(subject)
            if after and after != target:
                issues.append((EYELINE_MISMATCH,
                               f"{subject} 的视线从 {target} 变成 {after}，未记录移动事件"))
            elif not after:
                issues.append((EYELINE_MISMATCH, f"{subject} 的视线在后续镜头中缺失"))

    if previous is None:
        return issues

    moved_set = set(moved)
    for row in current.subjects:
        before = previous.zone_of(row.subject)
        after = str(row.zone)
        if not before or not after or before == after:
            continue
        if _zone_family(before) == _zone_family(after):
            continue  # 语义容差：同一区域内的小幅移动
        if row.subject not in moved_set:
            issues.append((POSITION_MISMATCH,
                           f"{row.subject} 从 {before} 变到 {after}，但没有记录移动事件"))

    for row in current.subjects:
        before_depth = previous.depth_of(row.subject)
        after_depth = str(row.depth)
        if not before_depth or not after_depth or before_depth == after_depth:
            continue
        if row.subject not in moved_set:
            issues.append((OCCLUSION_MISMATCH,
                           f"{row.subject} 的前后关系从 {before_depth} 变到 {after_depth}，"
                           "但没有记录移动事件"))

    return issues


def blocking_codes(issues) -> list[str]:
    return [code for code, _ in issues]


_ = DEPTH_VALUES  # 保持与 schema 取值一致（导入即校验词表存在）
