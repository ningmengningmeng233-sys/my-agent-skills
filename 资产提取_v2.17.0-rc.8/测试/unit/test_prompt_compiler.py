#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 6：提示词确定性编译（prompt_ir + prompt_compiler）。

覆盖：固定分区、两次编译逐字节一致、Omni 人物视图规格、无侧面、基础提示词不含动作/站位/镜头、
冲突与无证据破损校验、规则来源/输入指纹/编译器版本元数据、shadow 差异不覆盖正式、
按秩覆盖（下游不得改写上游确定值）、美术总则落盘载入、shadow 接进 CLI。
"""
import os
import sys
from pathlib import Path
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.art_constitution import ArtConstitution  # noqa: E402
from scripts.engine.asset_ledger import build_ledger, write_ledger  # noqa: E402
from scripts.engine.prompt_ir import (  # noqa: E402
    BASE_FORBIDDEN_HINTS,
    SECTION_ORDER,
    build_prompt_ir,
    omni_person_views,
)
from scripts.engine.prompt_compiler import (  # noqa: E402
    COMPILER_VERSION,
    compile_prompt,
    shadow_report,
    validate_prompt,
)

CONSTITUTION = ArtConstitution(
    medium="写实三维渲染", realism="国漫三维", proportions="写实头身比",
    materials="物理渲染材质", palette="玉清色调", lighting="体积光",
    era="仙侠古代", scene_aesthetic="空灵飘渺", costume_aesthetic="完整合体整洁",
    forbidden=("无现代元素", "无科技感"),
)


def _person(priority="P0", name="林姑娘", aid="CH_林姑娘"):
    return {
        "asset_id": aid, "type": "人物", "name": name, "priority": priority,
        "fact_level": "剧本事实", "stable_key": "CHR:00012:1",
        "narrative_importance": {"P0": "main", "P1": "supporting"}.get(priority, "minor"),
        "production_mode": "image",
        "evidence": [{"chapter": "第1章", "segment": "段01.md", "line": "00012",
                      "quote": "林姑娘走进茶楼，青衫束发"}],
    }


# --- 固定分区 -----------------------------------------------------------------

def test_fixed_section_order_is_locked():
    assert SECTION_ORDER == (
        "身份与主体", "原文事实", "造型与材质", "构图与视角", "背景",
        "光照", "风格", "禁止项", "证据元数据",
    )


def test_ir_sections_use_only_fixed_keys_and_are_ordered():
    ir = build_prompt_ir(_person(), None, CONSTITUTION)
    keys = [k for k, _ in ir.ordered_sections()]
    assert keys == list(SECTION_ORDER)


# --- 确定性（两次编译逐字节一致） ----------------------------------------------

def test_compile_is_byte_stable():
    ir = build_prompt_ir(_person(), None, CONSTITUTION)
    a = compile_prompt(ir, target="reverse")
    b = compile_prompt(ir, target="reverse")
    assert a.text == b.text and a.negative == b.negative
    assert a.metadata["compiler_version"] == COMPILER_VERSION


def test_metadata_carries_fingerprint_rule_sources_and_version():
    ir = build_prompt_ir(_person(), None, CONSTITUTION)
    meta = compile_prompt(ir, target="reverse").metadata
    assert meta["input_fingerprint"].startswith("sha256:")
    assert meta["compiler_version"] == COMPILER_VERSION
    assert "原文事实" in meta["rule_sources"] and "项目美术总则" in meta["rule_sources"]
    # 相同输入指纹稳定
    assert meta["input_fingerprint"] == compile_prompt(
        build_prompt_ir(_person(), None, CONSTITUTION), target="reverse").metadata["input_fingerprint"]


# --- Omni 人物视图规格 --------------------------------------------------------

def test_omni_person_view_counts():
    assert len(omni_person_views("P0")) == 4          # 核心四张
    assert len(omni_person_views("P1")) >= 2          # 重要配角两张起
    assert omni_person_views("P3") == omni_person_views("P2")  # 普通/群演一张
    assert len(omni_person_views("P3")) == 1


def test_no_side_view_in_any_persona_spec():
    for p in ("P0", "P1", "P2", "P3"):
        for view in omni_person_views(p):
            assert "侧面" not in view and "侧视" not in view, view


def test_each_view_is_a_separate_ir_task():
    ir = build_prompt_ir(_person("P0"), None, CONSTITUTION)
    assert len(ir.views) == 4 and len(set(ir.views)) == 4


# --- 基础提示词只描述资产，叙事图才含动作/站位/镜头 -----------------------------

def test_base_prompt_has_no_blocking_or_camera():
    ir = build_prompt_ir(_person(), None, CONSTITUTION, narrative=False)
    text = compile_prompt(ir, target="reverse").text
    for hint in BASE_FORBIDDEN_HINTS:
        assert hint not in text, hint


def test_narrative_prompt_may_carry_blocking():
    asset = _person()
    facts = [{"quote": "林姑娘与掌柜隔着柜台对峙，位于画面左前景"}]
    ir = build_prompt_ir(asset, facts, CONSTITUTION, narrative=True)
    text = compile_prompt(ir, target="reverse").text
    assert "对峙" in text or "站位" in ir.ordered_sections()[3][1]


# --- 人物/服装基础固定纯白背景 ------------------------------------------------

def test_person_base_background_is_pure_white():
    ir = build_prompt_ir(_person(), None, CONSTITUTION)
    sections = dict(ir.ordered_sections())
    assert "#FFFFFF" in sections["背景"]


def test_scene_base_background_not_forced_white():
    scene = {"asset_id": "SC_茶楼", "type": "场景", "name": "茶楼", "priority": "P1",
             "fact_level": "剧本事实", "evidence": [{"line": "00001", "quote": "街市茶楼飞檐"}]}
    ir = build_prompt_ir(scene, None, CONSTITUTION)
    assert "#FFFFFF" not in dict(ir.ordered_sections())["背景"]


# --- 校验：冲突与无证据破损 ----------------------------------------------------

def test_validate_flags_unevidenced_damage_on_base_prompt():
    asset = _person()
    asset["notes"] = "衣衫破损、赤足"      # 无原文证据支撑的破损设定
    ir = build_prompt_ir(asset, [], CONSTITUTION)   # facts 清空 → 证据段无破损出处
    text = compile_prompt(ir, target="reverse")
    codes = {i.code for i in validate_prompt(text)}
    assert "UNEVIDENCED_DAMAGE" in codes


def test_validate_allows_damage_backed_by_source_quote():
    asset = _person()
    asset["evidence"] = [{"line": "00030", "quote": "他衣衫破损，赤足走在雪地里"}]
    ir = build_prompt_ir(asset, None, CONSTITUTION)
    compiled = compile_prompt(ir, target="reverse")
    assert "UNEVIDENCED_DAMAGE" not in {i.code for i in validate_prompt(compiled)}


def test_validate_reports_style_conflict_between_sections():
    asset = _person()
    asset["forced_style_words"] = "卡通贴纸"      # 注入与写实相斥的表达
    ir = build_prompt_ir(asset, None, CONSTITUTION)
    issues = validate_prompt(compile_prompt(ir, target="reverse"))
    assert any(i.code == "STYLE_CONFLICT" for i in issues)


# --- shadow：出差异但不覆盖正式 ------------------------------------------------

def test_shadow_report_differs_without_touching_official_text():
    ir = build_prompt_ir(_person(), None, CONSTITUTION)
    new_text = compile_prompt(ir, target="reverse").text
    official = "旧的模型自由生成正文\n"
    report = shadow_report("CH_林姑娘", official_text=official, new_text=new_text)
    assert "CH_林姑娘" in report
    assert "旧" in report and "新" in report
    assert official.strip() in report
    # shadow 只产差异报告，明确声明不回写正式产物
    assert "不覆盖正式" in report or "正式产物未改写" in report
    # 正式文本本身不被 shadow 修改
    assert official == "旧的模型自由生成正文\n"


# --- 按秩覆盖：下游层不得改写上游确定值 ----------------------------------------

def test_exclusive_and_composite_sections_partition_the_nine_sections():
    from scripts.engine.prompt_ir import COMPOSITE_SECTIONS, EXCLUSIVE_SECTIONS

    assert set(EXCLUSIVE_SECTIONS) | set(COMPOSITE_SECTIONS) == set(SECTION_ORDER)
    assert not (set(EXCLUSIVE_SECTIONS) & set(COMPOSITE_SECTIONS))


def test_person_background_is_owned_by_omni_not_constitution():
    """人物基础图背景由 Omni 标准（秩 1）独占，项目美术总则（秩 3）不得改写。"""
    ir = build_prompt_ir(_person(), None, CONSTITUTION)
    sections = dict(ir.ordered_sections())
    assert "#FFFFFF" in sections["背景"]
    assert CONSTITUTION.scene_aesthetic not in sections["背景"]
    assert ir.rule_sources["背景"] == "Omni标准"


def test_person_composition_is_owned_by_omni_view_spec():
    ir = build_prompt_ir(_person(), None, CONSTITUTION)
    assert ir.rule_sources["构图与视角"] == "Omni标准"
    assert "非侧面" in dict(ir.ordered_sections())["构图与视角"]


def test_unset_exclusive_slot_falls_through_to_lower_layer():
    """上游沉默的槽位仍由美术总则填充（覆盖只发生在同一槽位被上游占用时）。"""
    ir = build_prompt_ir(_person(), None, CONSTITUTION)
    sections = dict(ir.ordered_sections())
    assert sections["光照"] == CONSTITUTION.lighting
    assert ir.rule_sources["光照"] == "项目美术总则"


def test_ir_records_precedence_rulings_for_override():
    ir = build_prompt_ir(_person(), None, CONSTITUTION)
    assert any("背景" in r for r in ir.rulings), ir.rulings


def test_no_constitution_still_yields_complete_sections():
    """无美术总则时不得出现空分区（上游缺位退回必要补全）。"""
    ir = build_prompt_ir(_person(), None, None)
    for key, value in ir.ordered_sections():
        assert value.strip(), key


# --- shadow 接进 CLI：同一总账产新旧两套、写差异报告、不覆盖正式 -----------------

def _cli_env(lib: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "ASSET_LIB_DIR": str(lib),
        "OPENAI_API_KEY": "test-key-not-real",
        "SUMMARY_API_MODEL": "test-model",
        "SUMMARY_API_URL": "http://127.0.0.1:1/v1",
        "ASSET_WORKERS": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _shadow_lib(tmp_path: Path) -> Path:
    lib = tmp_path / "资产库"
    (lib / "提示词" / "基础").mkdir(parents=True)
    (lib / "提示词" / "基础" / "基础提示词.md").write_text(
        "# 基础提示词\n\n## CH_林姑娘 林姑娘\n旧的模型自由生成正文\n", encoding="utf-8")
    index = {"assets": [{
        "asset_id": "CH_林姑娘", "stable_key": "CHR:00012:1", "type": "人物", "name": "林姑娘",
        "priority": "P0", "fact_level": "剧本事实", "status": "CONFIRMED",
        "evidence": [{"line": "00012", "quote": "林姑娘走进茶楼，青衫束发"}],
    }]}
    write_ledger(lib, build_ledger(index))
    return lib


def test_cli_help_exposes_shadow_mode(tmp_path):
    result = run_py(
            [str(REPO_ROOT / "scripts" / "generate_prompts.py"), "--help"],
            env=_cli_env(_shadow_lib(tmp_path)),
            cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "--shadow" in result.stdout


def test_shadow_run_writes_report_and_never_touches_official(tmp_path):
    from scripts.engine.prompt_compiler import run_shadow

    lib = _shadow_lib(tmp_path)
    official = lib / "提示词" / "基础" / "基础提示词.md"
    before = official.read_bytes()

    report_path = run_shadow(lib)
    report = Path(report_path).read_text(encoding="utf-8")

    assert Path(report_path).parent == lib / ".process"
    assert "CH_林姑娘" in report
    assert "不覆盖正式" in report or "正式产物未改写" in report
    # 正式产物逐字节未变，且没有额外写回提示词目录
    assert official.read_bytes() == before
    assert sorted(p.name for p in (lib / "提示词" / "基础").iterdir()) == ["基础提示词.md"]
