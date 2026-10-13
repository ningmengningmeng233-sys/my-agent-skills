#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单02-Task4：视觉计划 TOML 数据契约（规格 6/7/8/9）。

TOML 是视觉任务的机器事实源；Markdown 只是自动生成的阅读稿（工单02-Task7）。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.visual_schema import (  # noqa: E402
    ASPECT_BY_KIND,
    MAX_AUTO_ROUNDS,
    REVIEW_DECISIONS,
    AttemptRecord,
    CompositionSpec,
    PropPlacement,
    QcDecision,
    ReferenceBinding,
    ReferenceRole,
    ReviewBinding,
    SubjectPlacement,
    VisualPlan,
    VisualTask,
    VisualTaskKind,
    VisualTaskStatus,
    load_visual_plan,
    user_status_label,
    validate_visual_plan,
    write_visual_plan,
)

TEMPLATE = REPO_ROOT / "templates" / "视觉计划模板.toml"
ASSET_INDEX = {
    "schema_version": 1,
    "assets": [
        {"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘", "priority": "P1"},
        {"asset_id": "CH_老者", "type": "人物", "name": "老者", "priority": "P2"},
        {"asset_id": "SC_茶楼", "type": "场景", "name": "茶楼", "priority": "P1"},
        {"asset_id": "PR_琴", "type": "道具", "name": "琴", "priority": "P2"},
    ],
}


def _task(task_id="CH_林姑娘-正脸-v01", asset_id="CH_林姑娘", kind="正脸",
          status="PLANNED", **kw):
    base = dict(
        task_id=task_id, asset_id=asset_id, kind=kind, status=status,
        aspect_ratio=ASPECT_BY_KIND.get(kind, "1:1"),
        style_slug="仙侠国风电影CG", style_version="1.0.0",
        style_hash="a" * 64, engine="gemini", model="gemini-3-pro-image",
        fact_source=".process/资产索引.json#CH_林姑娘",
    )
    base.update(kw)
    return VisualTask(**base)


def _plan(tasks=(), **kw):
    base = dict(
        schema_version=1, style_name="仙侠国风电影CG",
        style_slug="仙侠国风电影CG", style_version="1.0.0",
        style_hash="a" * 64, engine_profile="gemini",
        lib=".", asset_index_hash="b" * 16, tasks=tuple(tasks),
    )
    base.update(kw)
    return VisualPlan(**base)


def test_enums_cover_required_vocabulary():
    kinds = {k.value for k in VisualTaskKind}
    for expected in ("正脸", "后脑", "服装独立正面", "服装独立背面", "人物全身正面", "人物全身背面",
                     "场景母版", "道具"):
        assert expected in kinds, expected
    assert {r.value for r in ReferenceRole} == {
        "identity", "structure", "style", "material", "lighting", "space"}
    assert {d.value for d in QcDecision} >= {"ACCEPTED", "NEEDS_REVISION", "NEEDS_HUMAN_DECISION"}
    assert MAX_AUTO_ROUNDS == 3


def test_user_status_labels_are_chinese():
    for status in VisualTaskStatus:
        label = user_status_label(status.value)
        assert label and label != status.value or status.value in ("ACCEPTED",)
        assert any("\u4e00" <= ch <= "\u9fff" for ch in label), status.value
    assert user_status_label("PLANNED") == "待生成"
    assert user_status_label("READY_AS_REFERENCE") == "可作为参考"
    assert user_status_label("NEEDS_HUMAN_DECISION") == "需要人工决定"


def test_aspect_ratio_contract():
    assert ASPECT_BY_KIND["正脸"] == "1:1"
    assert ASPECT_BY_KIND["后脑"] == "1:1"
    assert ASPECT_BY_KIND["道具"] == "1:1"
    assert ASPECT_BY_KIND["服装独立正面"] == "2:3"
    assert ASPECT_BY_KIND["服装独立背面"] == "2:3"
    assert ASPECT_BY_KIND["人物全身正面"] == "2:3"
    assert ASPECT_BY_KIND["人物全身背面"] == "2:3"
    assert ASPECT_BY_KIND["场景母版"] == "16:9"


def test_valid_plan_has_no_issues(tmp_path):
    plan = _plan([_task()])
    assert validate_visual_plan(plan, ASSET_INDEX) == []


def test_toml_roundtrip_preserves_everything(tmp_path):
    plan = _plan([
        _task(references=(ReferenceBinding(path="_refs/face.png", role="identity",
                                           sha256="c" * 64, note="身份参考"),)),
        _task(task_id="SC_茶楼-场景母版-v01", asset_id="SC_茶楼", kind="场景母版",
              depends_on=()),
    ])
    path = tmp_path / "计划.toml"
    write_visual_plan(plan, path)
    loaded = load_visual_plan(path)
    assert loaded == plan


def test_write_is_byte_deterministic(tmp_path):
    plan = _plan([_task(), _task(task_id="PR_琴-道具-v01", asset_id="PR_琴", kind="道具")])
    a, b = tmp_path / "a.toml", tmp_path / "b.toml"
    write_visual_plan(plan, a)
    write_visual_plan(plan, b)
    assert a.read_bytes() == b.read_bytes()


def test_task_order_is_sorted_deterministically(tmp_path):
    plan = _plan([_task(task_id="PR_琴-道具-v01", asset_id="PR_琴", kind="道具"),
                  _task()])
    path = tmp_path / "计划.toml"
    write_visual_plan(plan, path)
    loaded = load_visual_plan(path)
    ids = [t.task_id for t in loaded.tasks]
    assert ids == sorted(ids)


def test_chinese_is_not_escaped(tmp_path):
    path = tmp_path / "计划.toml"
    write_visual_plan(_plan([_task()]), path)
    text = path.read_text(encoding="utf-8")
    assert "仙侠国风电影CG" in text
    assert "待生成" not in text  # TOML 存英文枚举；中文只出现在阅读稿
    assert "\\u" not in text


def test_backslash_paths_are_normalized(tmp_path):
    plan = _plan([_task(references=(
        ReferenceBinding(path="_refs\\face.png", role="identity", sha256="c" * 64),))])
    path = tmp_path / "计划.toml"
    write_visual_plan(plan, path)
    loaded = load_visual_plan(path)
    assert loaded.tasks[0].references[0].path == "_refs/face.png"


def test_absolute_paths_rejected():
    plan = _plan([_task(references=(
        ReferenceBinding(path="C:/素材/face.png", role="identity"),))])
    issues = validate_visual_plan(plan, ASSET_INDEX)
    assert any(i.code == "ABSOLUTE_PATH" for i in issues), [i.code for i in issues]


def test_parent_traversal_rejected():
    plan = _plan([_task(references=(
        ReferenceBinding(path="../外部/face.png", role="identity"),))])
    assert any(i.code == "PATH_OUTSIDE_LIB" for i in validate_visual_plan(plan, ASSET_INDEX))


def test_unknown_asset_rejected():
    plan = _plan([_task(asset_id="CH_不存在", task_id="CH_不存在-正脸-v01")])
    issues = validate_visual_plan(plan, ASSET_INDEX)
    assert any(i.code == "UNKNOWN_ASSET" for i in issues), [i.code for i in issues]


def test_task_id_format_enforced():
    plan = _plan([_task(task_id="林姑娘_正脸")])
    assert any(i.code == "TASK_ID_FORMAT" for i in validate_visual_plan(plan, ASSET_INDEX))


def test_duplicate_task_id_rejected():
    plan = _plan([_task(), _task()])
    assert any(i.code == "DUPLICATE_TASK_ID" for i in validate_visual_plan(plan, ASSET_INDEX))


def test_self_dependency_rejected():
    plan = _plan([_task(depends_on=("CH_林姑娘-正脸-v01",))])
    assert any(i.code == "SELF_DEPENDENCY" for i in validate_visual_plan(plan, ASSET_INDEX))


def test_dependency_cycle_rejected():
    plan = _plan([
        _task(task_id="CH_林姑娘-正脸-v01", asset_id="CH_林姑娘", kind="正脸",
              depends_on=("CH_林姑娘-后脑-v01",)),
        _task(task_id="CH_林姑娘-后脑-v01", asset_id="CH_林姑娘", kind="后脑",
              depends_on=("CH_林姑娘-正脸-v01",)),
    ])
    assert any(i.code == "DEPENDENCY_CYCLE" for i in validate_visual_plan(plan, ASSET_INDEX))


def test_unknown_dependency_rejected():
    plan = _plan([_task(depends_on=("CH_林姑娘-后脑-v01",))])
    assert any(i.code == "UNKNOWN_DEPENDENCY" for i in validate_visual_plan(plan, ASSET_INDEX))


def test_unaccepted_task_cannot_be_a_reference():
    plan = _plan([
        _task(),
        _task(task_id="CH_林姑娘-后脑-v01", asset_id="CH_林姑娘", kind="后脑",
              status="NEEDS_REVISION", depends_on=("CH_林姑娘-正脸-v01",)),
    ])
    issues = validate_visual_plan(plan, ASSET_INDEX)
    assert any(i.code == "REFERENCE_NOT_ACCEPTED" for i in issues), [i.code for i in issues]


def test_accepted_task_may_be_a_reference():
    plan = _plan([
        _task(status="READY_AS_REFERENCE"),
        _task(task_id="CH_林姑娘-后脑-v01", asset_id="CH_林姑娘", kind="后脑",
              depends_on=("CH_林姑娘-正脸-v01",)),
    ])
    codes = [i.code for i in validate_visual_plan(plan, ASSET_INDEX)]
    assert "REFERENCE_NOT_ACCEPTED" not in codes


def test_round_limit_enforced():
    attempts = tuple(
        AttemptRecord(round=n, image=f"_attempts/r{n}.png", image_sha256=str(n) * 64)
        for n in range(1, 5)
    )
    plan = _plan([_task(attempts=attempts)])
    issues = validate_visual_plan(plan, ASSET_INDEX)
    assert any(i.code == "ROUND_LIMIT_EXCEEDED" for i in issues), [i.code for i in issues]


def test_max_rounds_cannot_exceed_three():
    plan = _plan([_task(max_rounds=4)])
    assert any(i.code == "MAX_ROUNDS_OVERFLOW" for i in validate_visual_plan(plan, ASSET_INDEX))


def test_aspect_ratio_mismatch_rejected():
    plan = _plan([_task(aspect_ratio="16:9")])
    assert any(i.code == "ASPECT_RATIO_MISMATCH" for i in validate_visual_plan(plan, ASSET_INDEX))


def test_reference_role_must_be_legal():
    plan = _plan([_task(references=(
        ReferenceBinding(path="_refs/face.png", role="随手参考"),))])
    assert any(i.code == "ILLEGAL_REFERENCE_ROLE" for i in validate_visual_plan(plan, ASSET_INDEX))


def test_style_pin_required():
    plan = _plan(style_hash="", tasks=(_task(),))
    assert any(i.code == "STYLE_UNPINNED" for i in validate_visual_plan(plan, ASSET_INDEX))


def test_task_style_pin_must_match_plan():
    plan = _plan([_task(style_version="9.9.9")])
    assert any(i.code == "STYLE_PIN_MISMATCH" for i in validate_visual_plan(plan, ASSET_INDEX))


def test_fact_source_required():
    plan = _plan([_task(fact_source="")])
    assert any(i.code == "FACT_SOURCE_MISSING" for i in validate_visual_plan(plan, ASSET_INDEX))


def test_template_exists_and_parses():
    from engine.common import toml_module

    assert TEMPLATE.is_file(), f"缺少模板 {TEMPLATE}"
    data = toml_module().loads(TEMPLATE.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["style_version"]
    assert isinstance(data.get("tasks"), list)


def test_load_missing_file_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_visual_plan(tmp_path / "不存在.toml")


def test_write_is_atomic(tmp_path):
    path = tmp_path / "计划.toml"
    write_visual_plan(_plan([_task()]), path)
    before = path.read_bytes()
    write_visual_plan(
        _plan([_task(), _task(task_id="PR_琴-道具-v01", asset_id="PR_琴", kind="道具")]), path)
    assert path.read_bytes() != before
    assert list(tmp_path.glob("*.tmp")) == [], "原子写入不得留下临时文件"


# --- 阶段 2 工单 2.1：新数据结构、六哈希审核绑定与状态规范化 ------------------------

def _composition():
    return CompositionSpec(
        shot_size="中景", camera_angle="平视", camera_height="正常人眼高度",
        subjects=(SubjectPlacement(subject="CH_林姑娘", zone="中", depth="前景",
                                   facing="朝向画面右侧", gaze="看向画面左侧的对手",
                                   action="抬手护住身后的人", occlusion="遮挡身后的路人",
                                   distance="与对手约一步半"),
                  SubjectPlacement(subject="CH_老者", zone="右", depth="中景",
                                   facing="朝向画面左侧", gaze="看向林姑娘",
                                   action="后退半步", occlusion="", distance="与林姑娘约两步")),
        props=(PropPlacement(name="PR_黑匣", zone="左", holder="CH_林姑娘", count=1,
                             state="未开启"),),
        anchors=("场景母版：村口井台；井栏在画面左侧固定",),
        forbidden=("禁止越轴", "禁止出现第三个人"),
        rationale="剧本写明林姑娘挡在老者与敌人之间，故取中景平视保护关系",
        source_level="原始剧本",
    )


def test_composition_spec_keeps_four_layer_sources():
    spec = _composition()
    assert spec.source_level == "原始剧本"
    assert spec.subjects[0].zone == "中"
    assert spec.props[0].holder == "CH_林姑娘"
    assert spec.anchors and spec.forbidden and spec.rationale


def test_composition_spec_requires_source_level():
    with pytest.raises(ValueError):
        CompositionSpec(shot_size="中景", source_level="")


def test_subject_placement_zone_vocabulary():
    placement = SubjectPlacement(subject="CH_林姑娘", zone="左")
    assert placement.zone == "左"
    with pytest.raises(ValueError):
        SubjectPlacement(subject="CH_林姑娘", zone="左上角第三格")


def test_review_binding_carries_six_hashes():
    binding = ReviewBinding(task_id="CH_林姑娘-正脸-v01", image_sha256="a" * 64,
                            prompt_sha256="b" * 64, reference_set_sha256="c" * 64,
                            style_sha256="d" * 64, composition_sha256="e" * 64)
    assert binding.task_id == "CH_林姑娘-正脸-v01"
    assert len(binding.as_dict()) == 6
    for value in binding.as_dict().values():
        assert value


def test_review_binding_rejects_short_hash():
    with pytest.raises(ValueError):
        ReviewBinding(task_id="CH_林姑娘-正脸-v01", image_sha256="abc",
                      prompt_sha256="b" * 64, reference_set_sha256="c" * 64,
                      style_sha256="d" * 64, composition_sha256="e" * 64)


def test_review_binding_blank_hashes_allowed_for_legacy():
    """旧计划没有六哈希；空值可读（是否闭环由审核契约与发布器判定）。"""
    binding = ReviewBinding(task_id="CH_林姑娘-正脸-v01")
    assert binding.image_sha256 == ""


def test_task_new_fields_roundtrip(tmp_path):
    task = _task(purpose="正脸身份近景", script_source="段01.md#00012｜「林姑娘走进茶楼」",
                 asset_index_key="CHR:00003:1", storyboard_source="分镜-EP01-014",
                 prompt="【任务目标】正脸身份近景", negative_prompt="禁止多视图；禁止文字水印",
                 composition_path="视觉资产/构图卡/CH_林姑娘-正脸-v01.toml",
                 composition_sha256="f" * 64,
                 blocking_path="视觉资产/场景站位图/SC_茶楼.toml",
                 blocking_sha256="0" * 64, current_version="attempt-02")
    path = tmp_path / "计划.toml"
    write_visual_plan(_plan([task]), path)
    loaded = load_visual_plan(path).by_id(task.task_id)
    assert loaded.purpose == task.purpose
    assert loaded.script_source == task.script_source
    assert loaded.asset_index_key == task.asset_index_key
    assert loaded.storyboard_source == task.storyboard_source
    assert loaded.prompt == task.prompt
    assert loaded.negative_prompt == task.negative_prompt
    assert loaded.composition_path == task.composition_path
    assert loaded.composition_sha256 == task.composition_sha256
    assert loaded.blocking_path == task.blocking_path
    assert loaded.blocking_sha256 == task.blocking_sha256
    assert loaded.current_version == task.current_version


def test_task_new_fields_are_deterministic(tmp_path):
    task = _task(prompt="正向描述", negative_prompt="禁止项")
    first = tmp_path / "a.toml"
    second = tmp_path / "b.toml"
    write_visual_plan(_plan([task]), first)
    write_visual_plan(_plan([task]), second)
    assert first.read_bytes() == second.read_bytes()


def test_legacy_rc2_plan_still_loads(tmp_path):
    """rc.2 时代写出的计划（无新字段）必须照常加载，新字段取空默认值。"""
    legacy = "\n".join((
        "# 旧计划（rc.2 形态）",
        "schema_version = 1",
        'style_name = "仙侠国风电影CG"',
        'style_slug = "仙侠国风电影CG"',
        'style_version = "1.0.0"',
        f'style_hash = "{"a" * 64}"',
        'engine_profile = "gemini"',
        'lib = "."',
        'asset_index_hash = "b"',
        'updated_at = ""',
        "",
        "[[tasks]]",
        'task_id = "CH_林姑娘-正脸-v01"',
        'asset_id = "CH_林姑娘"',
        'kind = "正脸"',
        'status = "PLANNED"',
        'aspect_ratio = "1:1"',
        'style_slug = "仙侠国风电影CG"',
        'style_version = "1.0.0"',
        f'style_hash = "{"a" * 64}"',
        'engine = "gemini"',
        'model = ""',
        'max_rounds = 3',
        'fact_source = ".process/资产索引.json#CH_林姑娘"',
        "depends_on = []",
        "materials = []",
        "user_locked = []",
        'notes = ""',
        "",
    ))
    path = tmp_path / "计划.toml"
    path.write_text(legacy, encoding="utf-8", newline="\n")
    plan = load_visual_plan(path)
    task = plan.by_id("CH_林姑娘-正脸-v01")
    assert task.purpose == ""
    assert task.composition_sha256 == ""
    assert task.current_version == ""
    assert validate_visual_plan(plan, ASSET_INDEX) == []


def test_composition_path_must_be_library_relative():
    bad_abs = _task(composition_path="C:/别的库/构图卡.toml")
    issues = validate_visual_plan(_plan([bad_abs]), ASSET_INDEX)
    assert any(i.code == "ABSOLUTE_PATH" for i in issues), [str(i) for i in issues]

    bad_escape = _task(blocking_path="../../外面/站位图.toml")
    issues = validate_visual_plan(_plan([bad_escape]), ASSET_INDEX)
    assert any(i.code == "PATH_OUTSIDE_LIB" for i in issues)


def test_task_hash_fields_must_be_64_hex():
    issues = validate_visual_plan(_plan([_task(composition_sha256="abc")]), ASSET_INDEX)
    assert any(i.code == "HASH_LENGTH" for i in issues)
    issues = validate_visual_plan(_plan([_task(blocking_sha256="z" * 64)]), ASSET_INDEX)
    assert any(i.code == "HASH_LENGTH" for i in issues)


def test_new_statuses_exist_and_are_localized():
    values = VisualTaskStatus.valid_values()
    assert "WAITING_REVIEW" in values
    assert "BLOCKED" in values
    assert user_status_label("WAITING_REVIEW") == "等待审核"
    assert user_status_label("BLOCKED") == "已阻断"


def test_pass_with_notes_is_a_review_decision_not_a_task_status():
    """「通过但有备注」只作为审核判定；任务发布后仍是可作为参考。"""
    assert "PASS_WITH_NOTES" in REVIEW_DECISIONS
    assert "PASS_WITH_NOTES" not in VisualTaskStatus.valid_values()
    assert REVIEW_DECISIONS == ("ACCEPTED", "PASS_WITH_NOTES", "NEEDS_REVISION",
                                "NEEDS_HUMAN_DECISION")
