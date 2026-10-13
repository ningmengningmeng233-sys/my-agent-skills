#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 3 工单 3.2：场景站位图与连续性检查。

场景站位图记录入口、出口、通道、固定物、可站立区域、前中后景、默认拍摄方向、行动轴线
和不可穿越区域；同一场景后续图片必须继承同一空间结构。

连续镜头检查语义关系（左中右区域、前后遮挡、距离、朝向、视线），**不做像素级比较**：
人物可以在指定区域内小幅变化，但不得无故左右互换、不得越轴、视线必须相互对应。

问题码：`POSITION_MISMATCH`、`VIEWPOINT_MISMATCH`、`EYELINE_MISMATCH`、`AXIS_CROSSING`、
`OCCLUSION_MISMATCH`、`SPACE_ANCHOR_DRIFT`。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.scene_blocking import (  # noqa: E402
    BLOCKING_DIR_REL,
    SCENE_ANCHOR_DRIFT,
    AXIS_CROSSING,
    EYELINE_MISMATCH,
    OCCLUSION_MISMATCH,
    POSITION_MISMATCH,
    VIEWPOINT_MISMATCH,
    SceneBlocking,
    ShotPlacement,
    build_scene_blocking,
    check_continuity,
    load_scene_blocking,
    write_scene_blocking,
)
from scripts.engine.visual_schema import SubjectPlacement  # noqa: E402

SCENE = SC_ = "SC_茶楼"


def _blocking() -> SceneBlocking:
    return build_scene_blocking(
        SCENE, anchors=("东侧木窗", "南侧楼梯口", "柜台"),
        entrances=("南侧楼梯口",), exits=("东侧木窗",),
        standable={"左": "靠窗区", "中": "堂中过道", "右": "柜台前"},
        camera_direction="由堂中向北拍摄，轴线沿过道",
        axis=("CH_林姑娘", "CH_老者"),
        no_cross_zones=("柜台后", "楼梯间"),
    )


def _shot(task_id="分镜-01", *, zones=(), facing=(), gaze=(), depths=(), camera="平视",
          anchors=("东侧木窗", "南侧楼梯口", "柜台")):
    subjects = []
    for index, name in enumerate(["CH_林姑娘", "CH_老者"]):
        subjects.append(SubjectPlacement(
            subject=name, zone=(zones[index] if index < len(zones) else ("左", "右")[index]),
            depth=(depths[index] if index < len(depths) else "中景"),
            facing=(facing[index] if index < len(facing) else ""),
            gaze=(gaze[index] if index < len(gaze) else "")))
    return ShotPlacement(task_id=task_id, subjects=tuple(subjects), camera_height=camera,
                         anchors=tuple(anchors))


# --- 1. 结构 ----------------------------------------------------------------------

def test_blocking_records_space_structure():
    blocking = _blocking()
    assert blocking.scene_id == SCENE
    assert "东侧木窗" in blocking.anchors
    assert blocking.entrances and blocking.exits
    assert blocking.standable["左"]
    assert blocking.default_camera_direction
    assert blocking.axis == ("CH_林姑娘", "CH_老者")
    assert "柜台后" in blocking.no_cross_zones


def test_blocking_roundtrip(tmp_path):
    blocking = _blocking()
    path = write_scene_blocking(tmp_path, blocking)
    assert path == tmp_path / BLOCKING_DIR_REL[0] / BLOCKING_DIR_REL[1] / f"{SCENE}.toml"
    loaded = load_scene_blocking(path)
    assert loaded.anchors == blocking.anchors
    assert loaded.standable == blocking.standable
    assert loaded.axis == blocking.axis


def test_blocking_write_is_atomic(tmp_path):
    blocking = _blocking()
    first = write_scene_blocking(tmp_path, blocking)
    before = first.read_bytes()
    write_scene_blocking(tmp_path, blocking)
    assert first.read_bytes() == before
    assert list(first.parent.glob("*.tmp")) == []


def test_load_missing_blocking_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_scene_blocking(tmp_path / "不存在.toml")


# --- 2. 视线对应（双人对话） ------------------------------------------------------

def test_two_person_dialogue_eyelines_match():
    blocking = _blocking()
    first = _shot("分镜-01", gaze=("看向右侧的老者", "看向左侧的林姑娘"))
    second = _shot("分镜-02", gaze=("看向右侧的老者", "看向左侧的林姑娘"))
    assert check_continuity(blocking, first, second) == []


def test_eyeline_mismatch_is_reported():
    blocking = _blocking()
    first = _shot("分镜-01", gaze=("看向右侧的老者", "看向左侧的林姑娘"))
    broken = _shot("分镜-02", gaze=("看向画面外", "看向左侧的林姑娘"))
    codes = [code for code, _ in check_continuity(blocking, first, broken)]
    assert EYELINE_MISMATCH in codes


# --- 3. 轴线（三人对峙不越轴） ----------------------------------------------------

def test_three_way_standoff_keeps_axis():
    blocking = _blocking()
    first = _shot("分镜-01")
    second = _shot("分镜-02")
    assert AXIS_CROSSING not in [code for code, _ in check_continuity(blocking, first, second)]


def test_axis_crossing_is_reported_without_explanation():
    blocking = _blocking()
    first = _shot("分镜-01")
    crossed = ShotPlacement(task_id="分镜-02", subjects=_shot("分镜-02").subjects,
                            camera_height="平视", anchors=("东侧木窗", "南侧楼梯口", "柜台"),
                            axis_side="opposite")
    codes = [code for code, _ in check_continuity(blocking, first, crossed)]
    assert AXIS_CROSSING in codes


def test_explained_axis_crossing_is_allowed():
    blocking = _blocking()
    first = _shot("分镜-01")
    crossed = ShotPlacement(task_id="分镜-02", subjects=_shot("分镜-02").subjects,
                            camera_height="平视", anchors=("东侧木窗", "南侧楼梯口", "柜台"),
                            axis_side="opposite")
    assert check_continuity(blocking, first, crossed, axis_cross_explained=True) == []


# --- 4. 站位互换与语义容差 --------------------------------------------------------

def test_position_flip_without_movement_is_reported():
    blocking = _blocking()
    first = _shot("分镜-01", zones=("左", "右"))
    flipped = _shot("分镜-02", zones=("右", "左"))
    codes = [code for code, _ in check_continuity(blocking, first, flipped)]
    assert POSITION_MISMATCH in codes


def test_position_flip_with_recorded_movement_is_allowed():
    blocking = _blocking()
    first = _shot("分镜-01", zones=("左", "右"))
    flipped = _shot("分镜-02", zones=("右", "左"))
    assert check_continuity(blocking, first, flipped,
                            moved=("CH_林姑娘", "CH_老者")) == []


def test_minor_movement_within_zone_is_allowed():
    """语义容差：区域不变（同区间小幅移动）不算站位漂移。"""
    blocking = _blocking()
    first = _shot("分镜-01", zones=("左中", "右"))
    second = _shot("分镜-02", zones=("左", "右"))
    codes = [code for code, _ in check_continuity(blocking, first, second)]
    assert POSITION_MISMATCH not in codes


# --- 5. 遮挡关系 ------------------------------------------------------------------

def test_occlusion_change_without_movement_is_reported():
    blocking = _blocking()
    first = _shot("分镜-01", depths=("前景", "中景"))
    second = _shot("分镜-02", depths=("中景", "前景"))
    codes = [code for code, _ in check_continuity(blocking, first, second)]
    assert OCCLUSION_MISMATCH in codes


# --- 6. 机位与场景锚点 ------------------------------------------------------------

def test_viewpoint_change_is_reported():
    blocking = _blocking()
    first = _shot("分镜-01", camera="平视")
    second = _shot("分镜-02", camera="俯视")
    codes = [code for code, _ in check_continuity(blocking, first, second)]
    assert VIEWPOINT_MISMATCH in codes


def test_viewpoint_change_with_reason_is_allowed():
    blocking = _blocking()
    first = _shot("分镜-01", camera="平视")
    second = _shot("分镜-02", camera="俯视")
    assert check_continuity(blocking, first, second, viewpoint_explained=True) == []


def test_scene_anchor_drift_is_reported():
    blocking = _blocking()
    first = _shot("分镜-01")
    drifted = _shot("分镜-02", anchors=("东侧木窗", "南侧楼梯口"))
    codes = [code for code, _ in check_continuity(blocking, first, drifted)]
    assert SCENE_ANCHOR_DRIFT in codes


def test_first_shot_only_checks_self_consistency():
    blocking = _blocking()
    first = _shot("分镜-01")
    assert check_continuity(blocking, None, first) == []
