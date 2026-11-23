#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 4 工单 4.3：源头脸规则、正背分开审核、拼版不入参考、默认整洁服装。

- 源头脸固定浅灰/浅冷灰干净背景与柔和均匀光；正面与背面分开任务、分开审核；
- 拼版只由两个已通过版本程序合成，**永不进入参考链**（尤其不得成为身份参考）；
- 人物衣物默认整洁普通，只有原文明确写破损/战损时才按原文保留破损。
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.reference_router import (  # noqa: E402
    NEVER_REFERENCE_KINDS,
    route_references_detailed,
)
from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_planner import (  # noqa: E402
    COSTUME_DAMAGED_NOTE,
    COSTUME_DEFAULT_NOTE,
    SOURCE_FACE_RULE,
    costume_condition_note,
    plan_visual_tasks,
)
from scripts.engine.visual_schema import (  # noqa: E402
    AttemptRecord,
    ReferenceRole,
    VisualPlan,
    VisualTask,
)

STYLE = resolve_style_record("仙侠国风电影CG")


def _index(quote="林姑娘走进茶楼"):
    return {
        "schema_version": 1, "source_fingerprint": "sha256:demo",
        "assets": [{"asset_id": "CH_林姑娘", "stable_key": "CHR:00003:1", "type": "人物",
                    "name": "林姑娘", "priority": "P1", "fact_level": "剧本事实",
                    "evidence": [{"segment": "段01.md", "line": "00001", "quote": quote}],
                    "relations": [], "dependencies": [], "status": "CONFIRMED"}],
    }


# --- 1. 源头脸规则 -----------------------------------------------------------------

def test_face_task_carries_source_face_rules():
    plan = plan_visual_tasks(_index(), STYLE)
    face = plan.by_id("CH_林姑娘-正脸-v01")
    assert "浅灰" in face.notes and "柔和均匀" in face.notes
    assert "单视图" in face.notes
    assert face.negative_prompt, "禁止项必须独立成字段"
    assert "浅灰" in face.negative_prompt


def test_face_and_back_are_separate_tasks_and_reviews():
    plan = plan_visual_tasks(_index(), STYLE)
    face = plan.by_id("CH_林姑娘-正脸-v01")
    back = plan.by_id("CH_林姑娘-后脑-v01")
    assert face.task_id != back.task_id
    assert face.status == back.status == "PLANNED", "两个任务各自独立审核"
    assert back.task_id in [t.task_id for t in plan.tasks if face.task_id in t.depends_on]


def test_purpose_and_script_source_are_recorded():
    plan = plan_visual_tasks(_index(), STYLE)
    face = plan.by_id("CH_林姑娘-正脸-v01")
    assert "正脸" in face.purpose
    assert face.script_source.startswith("林姑娘走进茶楼")
    assert "00001" in face.script_source, "剧本出处必须带行号"
    assert face.asset_index_key == "CHR:00003:1"


# --- 2. 拼版永不进入参考链 ---------------------------------------------------------

def test_board_kind_is_declared_never_reference():
    assert "拼版" in NEVER_REFERENCE_KINDS


def test_board_upstream_is_not_routed_as_reference():
    board = VisualTask(task_id="CH_林姑娘-拼版-v01", asset_id="CH_林姑娘", kind="拼版",
                       aspect_ratio="16:9", style_slug=STYLE.pack_slug,
                       style_version=STYLE.pack_version, style_hash=STYLE.pack_hash,
                       fact_source="x", attempts=(AttemptRecord(round=1, image="拼版.png"),))
    task = VisualTask(task_id="CH_林姑娘-人物全身正面-v01", asset_id="CH_林姑娘",
                      kind="人物全身正面", aspect_ratio="2:3", style_slug=STYLE.pack_slug,
                      style_version=STYLE.pack_version, style_hash=STYLE.pack_hash,
                      fact_source="x", depends_on=(board.task_id,))
    plan = VisualPlan(schema_version=1, style_slug=STYLE.pack_slug,
                      style_version=STYLE.pack_version, style_hash=STYLE.pack_hash,
                      tasks=(board, task))
    from scripts.engine.reference_router import load_omni_stylepack

    pack = load_omni_stylepack(STYLE)
    accepted = {board.task_id: Path("视觉资产/拼版/板.png")}
    result = route_references_detailed(task, pack, accepted)
    assert all(binding.path != "视觉资产/拼版/板.png" for binding in result.bindings), \
        "拼版不得作为任何职责的参考"
    assert not any(binding.role == ReferenceRole.IDENTITY.value
                   for binding in result.bindings), "不得把拼版当身份参考"
    _ = plan


# --- 3. 默认整洁服装 ---------------------------------------------------------------

def test_costume_defaults_to_clean():
    note, evidence = costume_condition_note(
        {"evidence": [{"quote": "林姑娘走进茶楼，白衣如雪"}]})
    assert note == COSTUME_DEFAULT_NOTE
    assert "破损" in note and "未写明" in evidence


def test_costume_damage_requires_script_evidence():
    note, evidence = costume_condition_note(
        {"evidence": [{"quote": "他衣衫破烂，满身血污地爬上岸"}]})
    assert note == COSTUME_DAMAGED_NOTE
    assert "原文依据" in evidence


def test_costume_note_enters_task_notes():
    plan = plan_visual_tasks(_index("老罗衣衫破烂地倒在河边"), STYLE)
    task = plan.by_id("CH_林姑娘-正脸-v01")
    assert COSTUME_DAMAGED_NOTE in task.notes or COSTUME_DEFAULT_NOTE in task.notes


def test_source_face_rule_text_is_specific():
    assert "浅灰或浅冷灰" in SOURCE_FACE_RULE
    assert "无拼版" in SOURCE_FACE_RULE
