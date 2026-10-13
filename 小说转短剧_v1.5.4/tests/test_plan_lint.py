# -*- coding: utf-8 -*-
"""T2 D 门族 plan_lint.py（工单 T2；方案 §3.1 + ToonFlow 原件红线）

硬闸（进回炉）：H1 逐场映射全覆盖（含红线 4 编号连续不漏不重）/ H2 台词双向守恒（禁全集级宽松）/
H3 统计按源重算 / H4 切场忠实（cast ⊆ 本集登记、place 锚定源场地点）/ H5 场数 ≥ 源场数（允许拆、禁止并）
告警（进报告不回炉）：W1 光影/色调/配乐违禁词，**全字段含 transitions**（原件红线 8 / :56 / :84）。
:84 的例外是创作自由度——过渡可增补剧本没写的衔接内容，故无门把过渡文本对回剧本。
"""
import io
import json
import os
import subprocess
import sys
import copy

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plan_fixtures as fx  # noqa: E402

from dramacraft.plan_lint import FORBIDDEN_LEXICON, PlanInputError, lint_plan  # noqa: E402
from conftest import run_py

SCRIPT = fx.sample_script()
EP1 = fx.get_ep(SCRIPT, 1)
GOOD = fx.make_valid_plan(EP1)


def _hard(r):
    return [h for h in r["hard"] if h.startswith("H")]


# ---------------------------------------------------------------- 基石

def test_valid_plan_is_all_green():
    r = lint_plan(copy.deepcopy(GOOD), SCRIPT)
    assert r["hard"] == [] and r["warn"] == []
    assert r["recomputed"] == fx.source_totals(EP1)


def test_plan_lint_accepts_single_ep_dict_too():
    single = fx.get_ep(SCRIPT, 1)
    assert lint_plan(copy.deepcopy(GOOD), single)["hard"] == []


# ---------------------------------------------------------------- 评审 Block 回归（T2 代码评审 2026-09-22）

def test_single_ep_script_with_mismatched_ep_is_input_error():
    """Block-1 回归：单集剧本件 + 规划件 ep 不匹配 → 输入错，绝不静默全绿。"""
    single = fx.get_ep(SCRIPT, 1)
    plan = fx.make_valid_plan(single, ep_num=9)
    with pytest.raises(PlanInputError):
        lint_plan(plan, single)


def test_cli_single_ep_mismatch_exits_one_without_report(tmp_path):
    single_path = _write(tmp_path, "ep1.json", fx.get_ep(SCRIPT, 1))
    plan = _write(tmp_path, "plan.json", fx.make_valid_plan(EP1, ep_num=9))
    out = tmp_path / "report.json"
    cp = _run_cli(plan, "--script", single_path, "--json", out)
    assert cp.returncode == 1 and not out.exists()
    assert "9" in cp.stderr


def test_report_write_failure_does_not_print_verdict(tmp_path):
    """Block-2 回归：报告落不下去时，stdout 不得先宣布「规划合格」。"""
    plan = _write(tmp_path, "plan.json", GOOD)
    script = _write(tmp_path, "script.json", SCRIPT)
    bad_out = tmp_path / "no_such_dir" / "report.json"
    cp = _run_cli(plan, "--script", script, "--json", bad_out)
    assert cp.returncode == 1
    assert "规划合格" not in cp.stdout


def test_duplicated_plan_line_is_hard():
    """Block-3 回归：同一句台词被多抄几遍 → 多抄也是违规（不只是「不少抄」）。"""
    plan = copy.deepcopy(GOOD)
    first = plan["scenes"][0]["lines"][0]
    plan["scenes"][0]["lines"] = plan["scenes"][0]["lines"] + [dict(first)] * 3
    r = lint_plan(plan, SCRIPT)
    assert any(h.startswith("H2") for h in r["hard"]), r["hard"]


def test_source_scene_without_location_warns_not_silent():
    """Block-4 回归：源场没写地点 → 地点名核对不了，必须留声（告警），不得静默放行。"""
    script = copy.deepcopy(SCRIPT)
    for scene in script["episodes"][0]["scenes"]:
        scene.pop("location", None)
    plan = copy.deepcopy(GOOD)
    plan["scenes"][0]["place"] = "龙潭别墅·火星殖民地"
    r = lint_plan(plan, script)
    assert r["hard"] == []
    assert any("地点" in w for w in r["warn"]), r["warn"]


def test_place_using_source_name_as_cover_is_hard():
    """W-1 回归：拿源地名当掩护另起一个地点（顾家客厅隔壁的王家别墅）→ 硬伤。"""
    plan = copy.deepcopy(GOOD)
    plan["scenes"][0]["place"] = "顾家客厅隔壁的王家别墅·深夜密谋"
    assert any(h.startswith("H4") for h in lint_plan(plan, SCRIPT)["hard"])


def test_forbidden_word_after_anchor_is_still_warned():
    """W-2 回归：只有「·之前的锚定段」继承源地名豁免，概况段照查违禁词。"""
    plan = copy.deepcopy(GOOD)
    loc = plan["scenes"][0]["place"].split("·")[0]
    plan["scenes"][0]["place"] = f"{loc}·顶光冷蓝的深夜"
    assert any("场景地点" in w and "顶光" in w for w in lint_plan(plan, SCRIPT)["warn"])


def test_emotion_level_out_of_range_is_hard():
    """W-3 回归：情绪浓度必须 0~10、基调非空（原件 :73-75 + 红线 7）。"""
    plan = copy.deepcopy(GOOD)
    plan["scenes"][0]["emotion"] = {"level": 99, "tone": "", "shift": None}
    r = lint_plan(plan, SCRIPT)
    assert any(h.startswith("H6") for h in r["hard"]), r["hard"]


def test_empty_dialogue_fields_are_hard_on_both_sides():
    """W-4 回归：剧本侧或规划侧出现空说话人/空台词，都必须落成硬伤而不是静默守恒。"""
    script = copy.deepcopy(SCRIPT)
    beat = next(b for b in script["episodes"][0]["scenes"][0]["flow"] if b.get("type") == "dialogue")
    beat["line"] = ""
    r = lint_plan(copy.deepcopy(GOOD), script)
    assert any(h.startswith("H2") and "剧本" in h for h in r["hard"]), r["hard"]

    plan = copy.deepcopy(GOOD)
    plan["scenes"][0]["lines"] = [{"speaker": "", "text": ""}] + plan["scenes"][0]["lines"]
    assert any(h.startswith("H2") for h in lint_plan(plan, SCRIPT)["hard"])


# ---------------------------------------------------------------- H1 覆盖

def test_missing_source_scene_is_hard():
    r = lint_plan(fx.drop_scene(GOOD, "S02"), SCRIPT)
    assert any(h.startswith("H1") and "S02" in h for h in r["hard"])


def test_ghost_source_scene_is_hard():
    r = lint_plan(fx.reference_ghost_scene(GOOD), SCRIPT)
    assert any(h.startswith("H1") and "S99" in h for h in r["hard"])


def test_non_contiguous_numbering_is_hard():
    # 红线 4：分场按序连续编号，不漏不重
    r = lint_plan(fx.break_numbering(GOOD), SCRIPT)
    assert any(h.startswith("H1") and "编号" in h for h in r["hard"])


# ---------------------------------------------------------------- H2 台词守恒

def test_wrong_speaker_in_scene_is_hard():
    r = lint_plan(fx.wrong_speaker(GOOD), SCRIPT)
    assert any(h.startswith("H2") for h in r["hard"])


def test_dropped_script_line_is_hard():
    r = lint_plan(fx.drop_line(GOOD), SCRIPT)
    assert any(h.startswith("H2") for h in r["hard"])


def test_invented_line_in_scene_is_hard():
    # 创作只许在「场间过渡」（:84）；场内编台词 = 违反「只拆分、不创作」
    r = lint_plan(fx.invent_line(GOOD), SCRIPT)
    assert any(h.startswith("H2") for h in r["hard"])


def test_none_line_does_not_crash():
    plan = copy.deepcopy(GOOD)
    plan["scenes"][0]["lines"] = [{"speaker": None, "text": None}]
    r = lint_plan(plan, SCRIPT)
    assert _hard(r), "空/缺字段必须落成硬伤而不是崩掉"


# ---------------------------------------------------------------- H3 统计

def test_lying_stats_is_hard():
    r = lint_plan(fx.lie_stats(GOOD), SCRIPT)
    assert any(h.startswith("H3") and "字数" in h for h in r["hard"])


def test_wrong_scene_count_in_stats_is_hard():
    plan = copy.deepcopy(GOOD)
    plan["stats"]["scenes"] = plan["stats"]["scenes"] + 2
    assert any(h.startswith("H3") for h in lint_plan(plan, SCRIPT)["hard"])


# ---------------------------------------------------------------- H4 切场忠实

def test_unknown_cast_code_is_hard():
    r = lint_plan(fx.unknown_cast(GOOD), SCRIPT)
    assert any(h.startswith("H4") and "C99" in h for h in r["hard"])


def test_unknown_place_is_hard():
    r = lint_plan(fx.unknown_place(GOOD), SCRIPT)
    assert any(h.startswith("H4") and "龙潭别墅" in h for h in r["hard"])


def test_anchored_place_with_summary_is_legal():
    # 原件输出结构就是「地点·概况」，拆场补充说明不得被误杀
    assert lint_plan(fx.anchored_place_with_summary(GOOD), SCRIPT)["hard"] == []


def _script_with_dotted_location():
    """源场 location 自带 U+00B7（合同内写法，references/WORKFLOW.md:36 示例「顾家大厅·夜」）。"""
    doc = copy.deepcopy(SCRIPT)
    ep = doc["episodes"][0]
    ep["scenes"][0]["location"] = "东吴大学·校门内林荫道·日"
    ep["scenes"][1]["location"] = "教学楼后墙根·日"
    return doc


def _plan_for(script_doc, ep_num=1):
    ep = fx.get_ep(script_doc, ep_num)
    plan = fx.make_valid_plan(ep, ep_num)
    for sc in plan["scenes"]:
        for sid in sc["srcSceneIds"]:
            src = next(s for s in ep["scenes"] if s["sceneId"] == sid)
            sc["place"] = src["location"]
    return plan, ep


def test_place_matching_source_location_that_contains_dot_is_legal():
    """源地点自己带 · 时，照抄它必须过门（旧实现按 · 切首段比对，这种源地点永远比不中）。"""
    script_doc = _script_with_dotted_location()
    plan, _ep = _plan_for(script_doc)
    r = lint_plan(plan, script_doc)
    assert [h for h in r["hard"] if h.startswith("H4")] == []


def test_place_extending_dotted_source_location_is_legal():
    """源地点带 · 时，在它后面接「·概况」也必须过门。"""
    script_doc = _script_with_dotted_location()
    plan, _ep = _plan_for(script_doc)
    plan["scenes"][0]["place"] = "东吴大学·校门内林荫道·日·同学绕道与刘远传警"
    r = lint_plan(plan, script_doc)
    assert [h for h in r["hard"] if h.startswith("H4")] == []


def test_place_not_prefix_of_dotted_source_location_is_still_hard():
    """放宽成「前缀匹配」后，前缀不对的照样要拦（错的地点名不许蒙混）。"""
    script_doc = _script_with_dotted_location()
    plan, _ep = _plan_for(script_doc)
    plan["scenes"][0]["place"] = "东吴大学·校门外马路·日"
    r = lint_plan(plan, script_doc)
    assert any(h.startswith("H4") and "东吴大学·校门内林荫道·日" in h for h in r["hard"])


# ---------------------------------------------------------------- H5 拆并

def test_merged_scenes_are_hard():
    r = lint_plan(fx.merge_two_scenes(GOOD), SCRIPT)
    assert any(h.startswith("H5") for h in r["hard"])


def test_split_scene_is_legal():
    r = lint_plan(fx.split_first_scene(GOOD), SCRIPT)
    assert r["hard"] == [], r["hard"]


# ---------------------------------------------------------------- W1 违禁词（全字段，含过渡）

def test_forbidden_word_in_notes_warns():
    r = lint_plan(fx.with_notes(GOOD, "本场以冷蓝月光打底，色温偏冷"), SCRIPT)
    assert r["hard"] == [] and any("色温" in w for w in r["warn"])


def test_forbidden_word_in_line_warns():
    plan = copy.deepcopy(GOOD)
    plan["scenes"][0]["lines"][0]["text"] = plan["scenes"][0]["lines"][0]["text"] + "（逆光剪影）"
    assert any("逆光" in w for w in lint_plan(plan, SCRIPT)["warn"])


def test_forbidden_word_in_transitions_also_warns():
    # 原件红线 8 / :84 末句：过渡也「不规划光影 / 配乐」——词表全字段生效
    r = lint_plan(fx.with_transition(GOOD, "空镜：明暗对比渐强，配乐起 → 淡入下一场"), SCRIPT)
    assert any("场间过渡" in w for w in r["warn"]), r["warn"]
    assert any("明暗" in w and "配乐" in w for w in r["warn"]), r["warn"]


def test_created_transition_content_is_legal():
    # :84 创作例外：过渡可增补剧本没写的衔接内容，无门对回剧本 ⇒ 硬闸不得误杀
    r = lint_plan(fx.with_transition(GOOD, "角色起身推门往外走 → 接下一场步入新场景"), SCRIPT)
    assert r["hard"] == [] and r["warn"] == []


def test_lexicon_is_single_source_constant():
    for word in ("光影", "色温", "明暗", "色调", "配乐", "乐器"):
        assert word in FORBIDDEN_LEXICON


def test_source_mandated_dialogue_wording_is_not_warned():
    """剧本原句带「月光」→ H2 强制逐字照抄；词表只告导演自己写的话，不得反咬忠实件。"""
    script = copy.deepcopy(SCRIPT)
    flow = script["episodes"][0]["scenes"][0]["flow"]
    dialogue = next(b for b in flow if b.get("type") == "dialogue")
    dialogue["line"] = dialogue["line"].rstrip("。！？!?") + "，月光底下我看得一清二楚。"
    plan = fx.make_valid_plan(fx.get_ep(script, 1))
    r = lint_plan(plan, script)
    assert r["hard"] == []
    assert [w for w in r["warn"] if " lines " in w] == [], r["warn"]


def test_source_location_name_is_not_the_directors_fault():
    """源场地点就叫「月光码头」→ 规划件 place 锚定复用它，不该被词表算成规划件的违规。"""
    script = copy.deepcopy(SCRIPT)
    script["episodes"][0]["scenes"][0]["location"] = "月光码头"
    ep = fx.get_ep(script, 1)
    plan = fx.make_valid_plan(ep)
    plan["scenes"][0]["place"] = "月光码头·深夜枪战"
    r = lint_plan(plan, script)
    assert r["hard"] == []
    assert r["warn"] == [], r["warn"]  # 导演自己补的概况「深夜枪战」不带违禁词就整条安静


# ---------------------------------------------------------------- CLI 双判定

def _run_cli(*args):
    return run_py(["-m", "dramacraft.plan_lint", *map(str, args)], cwd=ROOT)


def _write(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_cli_green_writes_report_and_exits_zero(tmp_path):
    plan = _write(tmp_path, "plan.json", GOOD)
    script = _write(tmp_path, "script.json", SCRIPT)
    out = tmp_path / "report.json"
    cp = _run_cli(plan, "--script", script, "--json", out)
    assert cp.returncode == 0
    payload = json.loads(out.read_text("utf-8"))
    assert payload["hard"] == [] and payload["ep"] == 1


def test_cli_hard_violation_exits_two(tmp_path):
    plan = _write(tmp_path, "plan.json", fx.drop_scene(GOOD, "S02"))
    script = _write(tmp_path, "script.json", SCRIPT)
    out = tmp_path / "report.json"
    cp = _run_cli(plan, "--script", script, "--json", out)
    assert cp.returncode == 2
    assert json.loads(out.read_text("utf-8"))["hard"]


def test_cli_missing_json_path_is_usage_error(tmp_path):
    plan = _write(tmp_path, "plan.json", GOOD)
    script = _write(tmp_path, "script.json", SCRIPT)
    cp = _run_cli(plan, "--script", script, "--json")
    assert cp.returncode == 1  # 缺路径 = 用法错硬失败，绝不 exit0 静默


def test_cli_missing_input_file_is_hard_error_no_report(tmp_path):
    script = _write(tmp_path, "script.json", SCRIPT)
    out = tmp_path / "report.json"
    cp = _run_cli(tmp_path / "nope.json", "--script", script, "--json", out)
    assert cp.returncode == 1 and not out.exists()
    assert "nope.json" in cp.stderr


def test_cli_plan_for_missing_episode_is_hard_error(tmp_path):
    plan = _write(tmp_path, "plan.json", fx.make_valid_plan(EP1, ep_num=9))
    script = _write(tmp_path, "script.json", SCRIPT)
    out = tmp_path / "report.json"
    cp = _run_cli(plan, "--script", script, "--json", out)
    assert cp.returncode == 1 and not out.exists()
    assert "9" in cp.stderr
