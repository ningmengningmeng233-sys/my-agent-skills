#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单02-Task5：按人物优先级生成独立任务（规格 6）。

分级：P0/P1 六项；P2 正脸+全身正背（剧情需要补服装正背）；P3 仅全身正面（有背面镜头补背面）。
依赖链：人物事实卡 → 正脸 → 后脑 → 服装正面 → 服装背面 → 全身正面 → 全身背面。
每项资产独立生成、独立检查、独立返工；禁止让模型一次生成多格人物板。
"""
import sys
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_planner import (  # noqa: E402
    TASK_TEMPLATE_DIR,
    plan_visual_tasks,
    template_for_kind,
)
from scripts.engine.visual_schema import (  # noqa: E402
    VisualTask,
    load_visual_plan,
    validate_visual_plan,
    write_visual_plan,
)

STYLE = resolve_style_record("仙侠国风电影CG")


def _asset(asset_id, asset_type, name, priority, quote="林姑娘走进茶楼", **kw):
    base = {
        "asset_id": asset_id, "type": asset_type, "name": name, "priority": priority,
        "fact_level": "剧本事实",
        "evidence": [{"segment": "段01.md", "chapter": "第1章", "line": "00001", "quote": quote}],
        "dependencies": [], "relations": [], "status": "CONFIRMED",
    }
    base.update(kw)
    return base


def _index():
    return {
        "schema_version": 1,
        "source_fingerprint": "sha256:abc123",
        "assets": [
            _asset("CH_林姑娘", "人物", "林姑娘", "P0"),
            _asset("CH_掌柜", "人物", "掌柜", "P2"),
            _asset("CH_路人甲", "人物", "路人甲", "P3"),
            _asset("CO_林姑娘_青衫", "服装", "青衫", "P1"),
            _asset("SC_茶楼", "场景", "茶楼", "P1"),
            _asset("PR_琴", "道具", "琴", "P2"),
        ],
    }


def _kinds(plan, asset_id):
    return {t.kind for t in plan.tasks if t.asset_id == asset_id}


def test_p0_gets_six_independent_tasks():
    plan = plan_visual_tasks(_index(), STYLE)
    assert _kinds(plan, "CH_林姑娘") == {
        "正脸", "后脑", "服装独立正面", "服装独立背面", "人物全身正面", "人物全身背面"}


def test_p2_gets_face_and_full_body_only():
    plan = plan_visual_tasks(_index(), STYLE)
    assert _kinds(plan, "CH_掌柜") == {"正脸", "人物全身正面", "人物全身背面"}


def test_p2_costume_added_only_when_scenario_needs():
    plan = plan_visual_tasks(_index(), STYLE, extra_costume={"CH_掌柜"})
    assert {"服装独立正面", "服装独立背面"} <= _kinds(plan, "CH_掌柜")


def test_p3_gets_full_body_front_only():
    plan = plan_visual_tasks(_index(), STYLE)
    assert _kinds(plan, "CH_路人甲") == {"人物全身正面"}


def test_p3_back_added_when_back_shot_exists():
    plan = plan_visual_tasks(_index(), STYLE, extra_back={"CH_路人甲"})
    assert _kinds(plan, "CH_路人甲") == {"人物全身正面", "人物全身背面"}


def test_dependency_chain_follows_spec():
    plan = plan_visual_tasks(_index(), STYLE)
    deps = {t.kind: set(t.depends_on) for t in plan.tasks if t.asset_id == "CH_林姑娘"}
    by_kind = {t.kind: t.task_id for t in plan.tasks if t.asset_id == "CH_林姑娘"}
    assert deps["正脸"] == set()
    assert deps["后脑"] == {by_kind["正脸"]}
    assert deps["服装独立正面"] == {by_kind["后脑"]}
    assert deps["服装独立背面"] == {by_kind["服装独立正面"]}
    assert deps["人物全身正面"] == {by_kind["服装独立背面"]}
    assert deps["人物全身背面"] == {by_kind["人物全身正面"]}


def test_scene_and_prop_tasks_planned():
    plan = plan_visual_tasks(_index(), STYLE)
    assert _kinds(plan, "SC_茶楼") == {"场景母版"}
    assert _kinds(plan, "PR_琴") == {"道具"}


def test_scope_filters_assets():
    plan = plan_visual_tasks(_index(), STYLE, scope={"CH_掌柜"})
    assert {t.asset_id for t in plan.tasks} == {"CH_掌柜"}


def test_plan_is_style_pinned():
    plan = plan_visual_tasks(_index(), STYLE)
    assert plan.style_slug == "仙侠国风电影CG"
    assert plan.style_version == "1.0.0"
    assert plan.style_hash == STYLE.pack_hash
    for task in plan.tasks:
        assert task.style_hash == STYLE.pack_hash
        assert task.style_version == STYLE.pack_version


def test_plan_records_fact_source_with_quote():
    plan = plan_visual_tasks(_index(), STYLE)
    task = plan.by_id("CH_林姑娘-正脸-v01")
    assert task is not None
    assert "资产索引.json" in task.fact_source
    assert "林姑娘走进茶楼" in task.fact_source


def test_task_ids_follow_convention():
    plan = plan_visual_tasks(_index(), STYLE)
    for task in plan.tasks:
        assert task.task_id == f"{task.asset_id}-{task.kind}-v01", task.task_id


def test_plan_passes_own_validator():
    plan = plan_visual_tasks(_index(), STYLE, extra_back={"CH_路人甲"},
                             extra_costume={"CH_掌柜"})
    assert validate_visual_plan(plan, _index()) == []


def test_planner_requires_asset_index():
    with pytest.raises(Exception) as ctx:
        plan_visual_tasks({}, STYLE)
    assert "ASSET_INDEX" in str(ctx.value) or "资产索引" in str(ctx.value)


def test_planner_rejects_unpinned_style():
    class _Bare:
        id = "H7"
        name = "仙侠国风电影CG"
        pack_slug = "仙侠国风电影CG"
        pack_version = ""
        pack_hash = ""
    with pytest.raises(Exception) as ctx:
        plan_visual_tasks(_index(), _Bare())
    assert "STYLE" in str(ctx.value)


# --- Step 2：裁头反例 ------------------------------------------------------------

COSTUME_KINDS = ("服装独立正面", "服装独立背面")
FULL_BODY_KINDS = ("人物全身正面", "人物全身背面")
# 工单02-Task5 Step2 的字面禁用方案（断颈/颈桩属"必须禁止的缺陷名"，允许出现在负面清单里）
CROP_BAN_WORDS = ("裁头", "无头身体", "切掉头部", "头部裁除")


def _template_text(kind):
    return template_for_kind(kind)


@pytest.mark.parametrize("kind", COSTUME_KINDS)
def test_costume_template_forbids_crop_head_wording(kind):
    text = _template_text(kind)
    for word in CROP_BAN_WORDS:
        assert word not in text, f"服装模板不得使用「{word}」方案"


@pytest.mark.parametrize("kind", COSTUME_KINDS)
def test_costume_template_requires_invisible_support(kind):
    text = _template_text(kind)
    assert "隐形人体支撑" in text
    assert "领口" in text and "肩线" in text
    assert "皮肤残端" in text
    # 断颈/颈部残端必须被显式禁止（老罗失败反例的缺陷名）
    assert "断颈" in text or "颈部" in text


@pytest.mark.parametrize("kind", FULL_BODY_KINDS)
def test_full_body_template_requires_complete_head_and_feet(kind):
    text = _template_text(kind)
    assert "头顶" in text or "头脚" in text
    assert "脚" in text
    assert "完整" in text


@pytest.mark.parametrize("kind", ("正脸", "后脑", "服装独立正面", "服装独立背面",
                                  "人物全身正面", "人物全身背面", "场景母版", "道具"))
def test_every_template_exists_and_forbids_multi_panel(kind):
    text = _template_text(kind)
    assert text.strip()
    assert "单张" in text or "单幅" in text
    assert "多格" in text or "拼版" in text or "三格" in text


@pytest.mark.parametrize("kind", ("正脸", "后脑", "服装独立正面", "服装独立背面",
                                  "人物全身正面", "人物全身背面", "场景母版", "道具"))
def test_every_template_is_chinese_and_static(kind):
    from scripts.engine.style import find_video_leaks

    text = _template_text(kind)
    assert find_video_leaks(text) == [], f"{kind} 模板混入视频规则"
    assert not any(ch.isascii() and ch.isalpha() and False for ch in text)
    assert any("\u4e00" <= ch <= "\u9fff" for ch in text)


def test_template_dir_layout():
    assert TASK_TEMPLATE_DIR.is_dir()
    names = {p.stem for p in TASK_TEMPLATE_DIR.glob("*.md")}
    assert {"人物正脸", "人物后脑", "服装独立正面", "服装独立背面",
            "人物全身正面", "人物全身背面", "场景母版", "道具"} <= names


# --- Step 4：幂等追加与事实变化 ---------------------------------------------------

def test_rerun_is_idempotent(tmp_path):
    path = tmp_path / "计划.toml"
    first = plan_visual_tasks(_index(), STYLE)
    write_visual_plan(first, path)
    second = plan_visual_tasks(_index(), STYLE, existing=load_visual_plan(path))
    assert [t.task_id for t in first.tasks] == [t.task_id for t in second.tasks]


def test_rerun_keeps_user_locked_and_attempts(tmp_path):
    from scripts.engine.visual_schema import AttemptRecord, VisualTask

    path = tmp_path / "计划.toml"
    first = plan_visual_tasks(_index(), STYLE)
    task = first.by_id("CH_林姑娘-正脸-v01")
    edited = VisualTask(
        **{**task.__dict__,
           "status": "READY_AS_REFERENCE",
           "user_locked": ("aspect_ratio",),
           "notes": "人工微调过",
           "attempts": (AttemptRecord(round=1, image="_a/1.png",
                                      image_sha256="d" * 64),)}
    )
    write_visual_plan(first.with_task(edited), path)
    second = plan_visual_tasks(_index(), STYLE, existing=load_visual_plan(path))
    kept = second.by_id("CH_林姑娘-正脸-v01")
    assert kept.status == "READY_AS_REFERENCE"
    assert kept.user_locked == ("aspect_ratio",)
    assert kept.notes == "人工微调过"
    assert len(kept.attempts) == 1


def test_fact_change_marks_affected_task_needs_review(tmp_path):
    path = tmp_path / "计划.toml"
    first = plan_visual_tasks(_index(), STYLE)
    task = first.by_id("CH_林姑娘-正脸-v01")
    from scripts.engine.visual_schema import VisualTask

    accepted = VisualTask(**{**task.__dict__, "status": "READY_AS_REFERENCE"})
    write_visual_plan(first.with_task(accepted), path)

    changed = _index()
    for a in changed["assets"]:
        if a["asset_id"] == "CH_林姑娘":
            a["evidence"] = [{"segment": "段01.md", "chapter": "第1章", "line": "00009",
                              "quote": "林姑娘推门而入"}]
    second = plan_visual_tasks(changed, STYLE, existing=load_visual_plan(path))
    kept = second.by_id("CH_林姑娘-正脸-v01")
    assert kept.status == "NEEDS_REVIEW"
    assert "林姑娘推门而入" in kept.fact_source


def test_rerun_adds_missing_tasks_only(tmp_path):
    path = tmp_path / "计划.toml"
    first = plan_visual_tasks(_index(), STYLE, scope={"CH_林姑娘"})
    write_visual_plan(first, path)
    second = plan_visual_tasks(_index(), STYLE, existing=load_visual_plan(path))
    assert _kinds(second, "CH_掌柜") == {"正脸", "人物全身正面", "人物全身背面"}
    assert _kinds(second, "CH_林姑娘") == {
        "正脸", "后脑", "服装独立正面", "服装独立背面", "人物全身正面", "人物全身背面"}

# --- CLI ------------------------------------------------------------------------


def _run_cli(lib, *extra):
    import json

    (lib / ".process").mkdir(parents=True, exist_ok=True)
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(_index(), ensure_ascii=False), encoding="utf-8")
    return run_py(
            [str(REPO_ROOT / "scripts" / "create_visual_plan.py"), "--lib", str(lib), "--style", "仙侠国风电影CG", *extra],
    )


def test_cli_writes_plan_and_is_idempotent(tmp_path):
    lib = tmp_path / "资产库"
    first = _run_cli(lib)
    assert first.returncode == 0, first.stdout + first.stderr
    plan_path = lib / "视觉资产" / "计划.toml"
    assert plan_path.is_file()
    second = _run_cli(lib)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "本次新增: 0 项" in second.stdout
    assert load_visual_plan(plan_path).by_id("CH_林姑娘-正脸-v01") is not None


def test_cli_requires_asset_index(tmp_path):
    lib = tmp_path / "资产库"
    lib.mkdir(parents=True)

    r = run_py(
            [str(REPO_ROOT / "scripts" / "create_visual_plan.py"), "--lib", str(lib), "--style", "仙侠国风电影CG"],
    )
    assert r.returncode != 0
    assert "资产索引" in r.stdout


def test_cli_scope_and_extra_flags(tmp_path):
    lib = tmp_path / "资产库"
    r = _run_cli(lib, "--assets", "CH_掌柜", "--costume-assets", "CH_掌柜",
                 "--back-assets", "CH_掌柜")
    assert r.returncode == 0, r.stdout + r.stderr
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    # P2 分级不含后脑（规格 6.1）；服装独立图用隐形人体支撑，本就不需要后脑参考
    assert _kinds(plan, "CH_掌柜") == {
        "正脸", "服装独立正面", "服装独立背面", "人物全身正面", "人物全身背面"}


def test_cli_rejects_unknown_style(tmp_path):
    lib = tmp_path / "资产库"
    import json

    (lib / ".process").mkdir(parents=True)
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(_index(), ensure_ascii=False), encoding="utf-8")
    r = run_py(
            [str(REPO_ROOT / "scripts" / "create_visual_plan.py"), "--lib", str(lib), "--style", "并不存在的风格"],
    )
    assert r.returncode != 0
    assert "不在登记表" in r.stdout


# --- 阶段 5 工单 5.2：剧情类任务的依赖规则 ---------------------------------------

def _story_task(**kw):
    base = dict(task_id="CH_林姑娘-剧情剧照-v01", asset_id="CH_林姑娘", kind="剧情剧照",
                status="PLANNED", style_slug=STYLE.pack_slug,
                style_version=STYLE.pack_version, style_hash=STYLE.pack_hash,
                aspect_ratio="16:9", storyboard_source="分镜-001",
                fact_source=".process/资产索引.json#CH_林姑娘",
                prompt="【任务目标】剧情剧照",
                composition_path="视觉资产/构图卡/CH_林姑娘-剧情剧照-v01.toml",
                composition_sha256="c" * 64)
    base.update(kw)
    return VisualTask(**base)


def test_story_task_waits_for_all_base_tasks_of_its_asset():
    """剧情图必须等同资产的基础任务全部通过（规格第八节：等人物/服装/场景/道具通过）。"""
    plan = plan_visual_tasks(_index(), STYLE)
    story = _story_task()
    merged = plan_visual_tasks(_index(), STYLE, existing=plan.with_task(story))
    task = merged.by_id(story.task_id)
    base_ids = {t.task_id for t in merged.tasks
                if t.asset_id == "CH_林姑娘" and t.kind != "剧情剧照"}
    assert base_ids, "夹具里该人物应有多项基础任务"
    assert set(task.depends_on) == base_ids
    assert validate_visual_plan(merged, _index()) == []


def test_story_task_keeps_explicit_cross_asset_dependencies():
    """人工补充的跨资产依赖（例如场景母版）不得被依赖推导覆盖，只做并集。"""
    plan = plan_visual_tasks(_index(), STYLE)
    scene_task_id = next(t.task_id for t in plan.tasks if t.asset_id == "SC_茶楼")
    story = _story_task(depends_on=(scene_task_id,))
    merged = plan_visual_tasks(_index(), STYLE, existing=plan.with_task(story))
    task = merged.by_id(story.task_id)
    assert scene_task_id in task.depends_on
    assert len(task.depends_on) > 1, "同资产的基础任务必须并入依赖"
    assert validate_visual_plan(merged, _index()) == []


def test_base_tasks_keep_their_chain_dependencies():
    """基础任务的依赖链不得被剧情依赖规则改动。"""
    plan = plan_visual_tasks(_index(), STYLE)
    face = next(t for t in plan.tasks if t.kind == "正脸")
    assert face.depends_on == (), "链首不应凭空多出依赖"
    merged = plan_visual_tasks(_index(), STYLE, existing=plan.with_task(_story_task()))
    assert merged.by_id(face.task_id).depends_on == ()
