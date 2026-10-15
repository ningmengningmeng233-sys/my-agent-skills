#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 P0-2：Omni 权威风格包必须是新任务视觉规则的唯一真相源。

红测目标（修复前必须失败）：
- `stylepack` 参数在旧实现里被接收却从未使用；新任务实际仍从旧 `画风库.md`/H7 拼装；
- 每种任务类型必须注入**权威模板**的结构规则（正脸裁切、全身头脚完整等）；
- 权威包任一核心文件变更必须使旧计划过期（哈希变化）；
- 新请求提示词中旧 H7 独有标记必须为 0。
"""
import base64
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_planner import plan_visual_tasks  # noqa: E402
from scripts.engine.visual_request_chain import compile_for_library  # noqa: E402
from scripts.engine.visual_schema import write_visual_plan  # noqa: E402

STYLE = resolve_style_record("仙侠国风电影CG")
VERSION_DIR = REPO_ROOT / "references" / "styles" / "仙侠国风电影CG" / "1.0.0"
PACK_ROOT = VERSION_DIR / "private"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")

# 旧 H7 独有标记（权威包与新版画风库都不含）：出现即说明新任务仍在吃旧规则源
H7_ONLY_MARKERS = ("斗罗", "玄机", "瓷感", "瓷亮", "高饱和", "镜面高光", "八K超高清")


def _person_index():
    return {
        "schema_version": 1, "source_fingerprint": "sha256:demo",
        "assets": [
            {"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘", "priority": "P0",
             "fact_level": "剧本事实", "materials": ["皮肤", "头发", "布料"],
             "evidence": [{"segment": "段01.md", "line": "00001", "quote": "林姑娘走进茶楼"}],
             "relations": [], "dependencies": [], "status": "CONFIRMED"},
            {"asset_id": "SC_茶楼", "type": "场景", "name": "茶楼", "priority": "P1",
             "fact_level": "剧本事实",
             "evidence": [{"segment": "段01.md", "line": "00001", "quote": "林姑娘走进茶楼"}],
             "relations": [], "dependencies": [], "status": "CONFIRMED"},
            {"asset_id": "PR_算盘", "type": "道具", "name": "算盘", "priority": "P1",
             "fact_level": "剧本事实",
             "evidence": [{"segment": "段01.md", "line": "00002", "quote": "掌柜拨算盘"}],
             "relations": [], "dependencies": [], "status": "CONFIRMED"},
        ],
    }


def _lib_with_plan(tmp_path):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    index = _person_index()
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")
    write_visual_plan(plan_visual_tasks(index, STYLE), lib / "视觉资产" / "计划.toml")
    return lib


def _compiled(lib, task_id):
    from scripts.engine.visual_schema import load_visual_plan

    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    return compile_for_library(plan.by_id(task_id), lib, plan)


def _accept_chain(lib, task_ids, image_bytes=PNG):
    """把链上一批任务置为已通过（带真实图片与哈希），使下游可编译。"""
    import hashlib

    from scripts.engine.visual_schema import AttemptRecord, VisualTask, load_visual_plan
    from scripts.engine.visual_store import attempt_dir

    plan_path = lib / "视觉资产" / "计划.toml"
    for task_id in task_ids:
        plan = load_visual_plan(plan_path)
        task = plan.by_id(task_id)
        rel = f"{attempt_dir(task_id, 1)}/image.png"
        target = lib / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(image_bytes)
        attempt = AttemptRecord(round=1, image=rel,
                                image_sha256=hashlib.sha256(image_bytes).hexdigest(),
                                model="manual", provider="manual")
        write_visual_plan(plan.with_task(
            VisualTask(**{**task.__dict__, "status": "READY_AS_REFERENCE", "attempts": (attempt,)})),
            plan_path)


# --- 权威包成为唯一真相源 ---------------------------------------------------------

def test_stylepack_is_actually_used(tmp_path):
    """stylepack 参数不得再是未使用参数：提示词必须含权威包内容。"""
    lib = _lib_with_plan(tmp_path)
    c = _compiled(lib, "CH_林姑娘-正脸-v01")
    prompt = c.request.prompt
    assert "pure 3D" in prompt or "纯三维" in prompt, "缺少权威包生成公式的介质基线"
    assert c.style_name == "仙侠国风电影CG"


def test_negative_prompt_comes_from_authoritative_pack(tmp_path):
    """负面提示词必须来自权威包的 negative_prompt.md（含其特征项）。"""
    lib = _lib_with_plan(tmp_path)
    c = _compiled(lib, "CH_林姑娘-正脸-v01")
    negative = c.request.negative_prompt
    assert negative.strip()
    assert any(marker in negative for marker in
               ("真人", "相机", "噪点", "塑料", "蜡质")), negative[:200]


def test_generation_formula_blocks_present(tmp_path):
    """生成公式的分块（BASE_STYLE / LIGHT_COLOR / COMPOSITION）必须进入编译。"""
    lib = _lib_with_plan(tmp_path)
    c = _compiled(lib, "CH_林姑娘-正脸-v01")
    prompt = c.request.prompt
    for token in ("BASE_STYLE", "LIGHT_COLOR", "COMPOSITION"):
        assert token in prompt, f"生成公式缺少 {token} 块"


def test_shared_invariants_entered(tmp_path):
    """共同风格约束（shared_style_invariants）必须进入编译。"""
    lib = _lib_with_plan(tmp_path)
    c = _compiled(lib, "CH_林姑娘-正脸-v01")
    assert "pure_3d_render" in c.request.prompt


# --- 每种任务类型都必须带权威模板规则 ---------------------------------------------

def test_face_task_has_clean_crop_and_safety_margins(tmp_path):
    lib = _lib_with_plan(tmp_path)
    prompt = _compiled(lib, "CH_林姑娘-正脸-v01").request.prompt
    for token in ("头顶", "下巴", "裁切"):
        assert token in prompt, f"正脸缺少安全边距规则：{token}"
    assert "正交" in prompt or "弱透视" in prompt, "正脸缺少正交/弱透视要求"
    for forbidden in ("拼版", "多视图", "无头身体"):
        assert forbidden in prompt, f"正脸缺少禁用项：{forbidden}"


def test_full_body_task_has_head_to_toe_rules(tmp_path):
    lib = _lib_with_plan(tmp_path)
    _accept_chain(lib, [
        "CH_林姑娘-正脸-v01", "CH_林姑娘-后脑-v01",
        "CH_林姑娘-服装独立正面-v01", "CH_林姑娘-服装独立背面-v01"])
    prompt = _compiled(lib, "CH_林姑娘-人物全身正面-v01").request.prompt
    for token in ("头脚", "比例", "相机高度", "安全边距"):
        assert token in prompt, f"全身缺少规则：{token}"


def test_full_body_does_not_inherit_face_crop_rule(tmp_path):
    """全身图不得继承正脸裁切与正脸基线（否则会出现裁头/半身/脸部特写基线）。"""
    lib = _lib_with_plan(tmp_path)
    _accept_chain(lib, [
        "CH_林姑娘-正脸-v01", "CH_林姑娘-后脑-v01",
        "CH_林姑娘-服装独立正面-v01", "CH_林姑娘-服装独立背面-v01"])
    prompt = _compiled(lib, "CH_林姑娘-人物全身正面-v01").request.prompt
    # 取景行必须是头脚完整，不得把「头顶到锁骨」当作取景口径
    composition = prompt.split("【构图】", 1)[1].split("【风格】", 1)[0]
    framing = next(line for line in composition.splitlines() if line.strip().startswith("- 取景"))
    assert "头脚完整" in framing, framing
    assert "头顶到锁骨" not in framing, framing
    # 必须注入全身基线而非正脸基线
    assert "Believable adult proportions" in prompt, "全身任务必须注入权威包全身基线"
    assert "Refined East Asian facial anatomy" not in prompt, \
        "全身任务不得把正脸基线当主体基线"


def test_back_head_task_forbids_face(tmp_path):
    lib = _lib_with_plan(tmp_path)
    _accept_chain(lib, ["CH_林姑娘-正脸-v01"])
    prompt = _compiled(lib, "CH_林姑娘-后脑-v01").request.prompt
    assert "面部" in prompt or "脸" in prompt, "后脑必须显式禁止面部"


def test_costume_task_forbids_head_crop_and_neck_stump(tmp_path):
    lib = _lib_with_plan(tmp_path)
    _accept_chain(lib, ["CH_林姑娘-正脸-v01", "CH_林姑娘-后脑-v01"])
    prompt = _compiled(lib, "CH_林姑娘-服装独立正面-v01").request.prompt
    assert "无头" in prompt or "隐形支撑" in prompt or "人台" in prompt, \
        "服装独立图必须说明无头身体支撑"


def test_scene_task_has_no_people(tmp_path):
    lib = _lib_with_plan(tmp_path)
    prompt = _compiled(lib, "SC_茶楼-场景母版-v01").request.prompt
    assert "无人物" in prompt or "画面中无任何人物" in prompt, "场景母版必须禁止人物"


def test_prop_task_has_three_view_policy(tmp_path):
    lib = _lib_with_plan(tmp_path)
    prompt = _compiled(lib, "PR_算盘-道具-v01").request.prompt
    assert "三视图" in prompt or "正面图" in prompt, "道具必须声明视图保底"


# --- 旧 H7 标记必须为 0 ------------------------------------------------------------

def test_no_legacy_h7_markers_in_new_prompt(tmp_path):
    lib = _lib_with_plan(tmp_path)
    for task_id in ("CH_林姑娘-正脸-v01", "SC_茶楼-场景母版-v01", "PR_算盘-道具-v01"):
        c = _compiled(lib, task_id)
        blob = c.request.prompt + "\n" + c.request.negative_prompt
        hits = [m for m in H7_ONLY_MARKERS if m in blob]
        assert not hits, f"{task_id} 提示词含旧 H7 独有标记：{hits}"


def test_no_video_rules_in_static_prompt(tmp_path):
    """静态图不得注入视频维度规则（E1 运镜/时长/节奏）。"""
    lib = _lib_with_plan(tmp_path)
    prompt = _compiled(lib, "CH_林姑娘-正脸-v01").request.prompt
    for token in ("运镜", "时长", "节奏", "镜头运动", "推拉摇移"):
        assert token not in prompt, f"静态提示词混入视频规则：{token}"


# --- 权威包变更必须使计划过期 ------------------------------------------------------

def test_stylepack_change_invalidates_pin(tmp_path):
    """权威包任一核心文件变更后，其内容哈希必须变化。"""
    import shutil

    from scripts.engine.stylepack_verify import pack_content_hash

    copy = tmp_path / "版本"
    shutil.copytree(VERSION_DIR, copy)
    pack = copy / "private"
    before = pack_content_hash(pack)
    (pack / "generation_formula.md").write_text("被改写", encoding="utf-8")
    after = pack_content_hash(pack)
    assert before and after and before != after, "核心文件变更必须改变内容哈希"


def test_deleting_pack_file_blocks_task(tmp_path):
    """删除权威包文件后，编译必须拒绝（不得用不完整风格源继续）。"""
    import shutil

    from scripts.engine.reference_router import load_omni_stylepack
    from scripts.engine.visual_request_chain import VisualCompileError, compile_visual_request
    from scripts.engine.visual_schema import load_visual_plan

    lib = _lib_with_plan(tmp_path)
    copy = tmp_path / "版本"
    shutil.copytree(VERSION_DIR, copy)
    pack = copy / "private"
    (pack / "references" / "face_base_style.md").unlink()

    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    index = json.loads((lib / ".process" / "资产索引.json").read_text(encoding="utf-8"))
    real = load_omni_stylepack(STYLE)
    stand_in = type(real)(root=pack, subject_routes=real.subject_routes,
                          material_routes=real.material_routes, router_dir=real.router_dir)
    with pytest.raises(VisualCompileError) as ctx:
        compile_visual_request(plan.by_id("CH_林姑娘-正脸-v01"), index["assets"][0], plan, lib,
                               stylepack_override=(STYLE, stand_in, pack))
    assert ctx.value.code == "STYLEPACK_UNAVAILABLE"


# --- 模板不得凭空添加事实 -----------------------------------------------------------

def test_template_does_not_invent_facts(tmp_path):
    """模板不得凭空添加服装、年龄、身份与道具：仅原文出现的事实可入提示词。"""
    lib = _lib_with_plan(tmp_path)
    prompt = _compiled(lib, "CH_林姑娘-正脸-v01").request.prompt
    for invented in ("锦衣", "华服", "凤冠", "少年", "老者", "佩剑"):
        assert invented not in prompt, f"提示词凭空出现未在原文声明的事实：{invented}"


def test_user_locked_facts_win_over_template(tmp_path):
    """用户锁定的事实优先于模板可变项。"""
    from scripts.engine.visual_schema import VisualTask, load_visual_plan

    lib = _lib_with_plan(tmp_path)
    plan_path = lib / "视觉资产" / "计划.toml"
    plan = load_visual_plan(plan_path)
    task = plan.by_id("CH_林姑娘-正脸-v01")
    locked = VisualTask(**{**task.__dict__, "notes": "身份锁：左眉有一道旧疤，发色纯黑。"})
    write_visual_plan(plan.with_task(locked), plan_path)
    prompt = _compiled(lib, "CH_林姑娘-正脸-v01").request.prompt
    assert "左眉有一道旧疤" in prompt, "用户锁定事实必须出现在不可变事实块"


def test_stylepack_absence_blocks_task(tmp_path, monkeypatch):
    """风格包不可用时必须以 STYLEPACK_UNAVAILABLE 拒绝，而不是退回旧规则源。"""
    from scripts.engine import visual_request_chain as chain
    from scripts.engine.visual_request_chain import VisualCompileError

    lib = _lib_with_plan(tmp_path)

    def _boom(task):
        raise chain.VisualCompileError("STYLEPACK_UNAVAILABLE", "权威包不可用（测试注入）")

    monkeypatch.setattr(chain, "_stylepack_of", _boom)
    from scripts.engine.visual_schema import load_visual_plan

    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    with pytest.raises(VisualCompileError) as ctx:
        chain.compile_for_library(plan.by_id("CH_林姑娘-正脸-v01"), lib, plan)
    assert ctx.value.code == "STYLEPACK_UNAVAILABLE"
