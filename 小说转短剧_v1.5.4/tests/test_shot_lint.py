# -*- coding: utf-8 -*-
"""T3 S 门族 shot_lint.py（工单 T3；方案 §3.2 + Omni 剧本转镜头组卡 v1.2.0）

硬闸（进回炉）：SG1 编号连续无洞无重 / SG2 单组时长 4-15s（禁绝对时间段）/ SG3 Σ中值∈目标±15% /
SG4 规划场全覆盖承载 / SG5 人物不越台 / SG6 五字段齐（人物可空=无人物）/ SG7 编号令牌可解析
告警（进报告不回炉）：抽象心理词表命中「内容」字段（Omni 卡 :139-146 反面写法）
导出：机读 JSON → Omni 极简五字段 MD（映射信息留在机读件，MD 不掺 boilerplate）
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
import shot_fixtures as sfx  # noqa: E402

from dramacraft.continuity_lint import emit_event  # noqa: E402
from dramacraft.exporter import export_shot_groups_md  # noqa: E402
from dramacraft.shot_lint import (  # noqa: E402
    ABSTRACT_PSY_LEXICON,
    ShotInputError,
    lint_shot_groups,
)
from conftest import run_py

SCRIPT = fx.sample_script()
EP1 = fx.get_ep(SCRIPT, 1)
PLAN = fx.make_valid_plan(EP1)
SHOTS = sfx.make_valid_shots(EP1, PLAN, 90)
TARGET = 90


def _lint(shots, plan=None, workdir=None):
    return lint_shot_groups(shots, plan or PLAN, SCRIPT, TARGET, workdir=workdir)


def test_valid_shots_all_green():
    r = _lint(copy.deepcopy(SHOTS))
    assert r["hard"] == [] and r["warn"] == []
    assert r["totalSeconds"] > 0


def test_secs_midpoint_of_range_is_seven():
    r = _lint(copy.deepcopy(SHOTS))
    assert r["groupMids"][0] == 12.0  # "11-13" → 中值 12.0，口径钉在测试里


# ---------------------------------------------------------------- SG1 编号

def test_gid_hole_is_hard():
    r = _lint(sfx.gid_hole(SHOTS))
    assert any(h.startswith("SG1") for h in r["hard"])


def test_duplicate_gid_is_hard():
    r = _lint(sfx.duplicate_gid(SHOTS))
    assert any(h.startswith("SG1") for h in r["hard"])


# ---------------------------------------------------------------- SG2 时长

def test_absolute_time_range_rejected():
    # Omni 卡 :50 原文：不要写 "00:00-00:25" 这种绝对时间段
    r = _lint(sfx.absolute_secs(SHOTS))
    assert any(h.startswith("SG2") for h in r["hard"])


def test_out_of_range_secs_is_hard():
    r = _lint(sfx.out_of_range_secs(SHOTS, "18-22"))
    assert any(h.startswith("SG2") for h in r["hard"])


# ---------------------------------------------------------------- SG3 总时长对账

def test_eight_seconds_against_ninety_is_hard():
    r = _lint(sfx.thin(SHOTS, keep=2, secs="6-10"))
    assert any(h.startswith("SG3") for h in r["hard"])


# ---------------------------------------------------------------- SG4 承载

def test_plan_scene_without_group_is_hard():
    r = _lint(sfx.gap_on_plan_scene(SHOTS, 1))
    assert any(h.startswith("SG4") for h in r["hard"])


def test_ghost_plan_scene_is_hard():
    r = _lint(sfx.ghost_plan_scene(SHOTS, "Sc99"))
    assert any(h.startswith("SG4") and "Sc99" in h for h in r["hard"])


# ---------------------------------------------------------------- SG5 人物

def test_unknown_cast_is_hard():
    r = _lint(sfx.unknown_cast(SHOTS, "C99"))
    assert any(h.startswith("SG5") and "C99" in h for h in r["hard"])


def test_no_character_group_is_legal():
    # Omni 卡：无人物写「无」——空镜/环境组不得被判违规
    assert _lint(sfx.no_character_group(SHOTS))["hard"] == []


# ---------------------------------------------------------------- SG6 五字段

def test_empty_content_is_hard():
    r = _lint(sfx.empty_field(SHOTS, "content"))
    assert any(h.startswith("SG6") for h in r["hard"])


def test_empty_secs_is_hard():
    r = _lint(sfx.empty_field(SHOTS, "secs"))
    assert any(h.startswith("SG6") for h in r["hard"])


# ---------------------------------------------------------------- SG7 令牌可解析

def test_registered_foreshadow_resolves(tmp_path):
    emit_event(tmp_path, "plant", ep=1, id="F001", text="碎瓷片上的血迹",
               payoffPlannedEp=3, registeredBy="outline")
    r = _lint(sfx.foreshadow_ref(SHOTS, "F001"), workdir=str(tmp_path))
    assert r["hard"] == []


def test_ghost_foreshadow_with_ledger_is_hard(tmp_path):
    emit_event(tmp_path, "plant", ep=1, id="F001", text="碎瓷片上的血迹",
               payoffPlannedEp=3, registeredBy="outline")
    r = _lint(sfx.foreshadow_ref(SHOTS, "F009"), workdir=str(tmp_path))
    assert any(h.startswith("SG7") and "F009" in h for h in r["hard"])


def test_foreshadow_without_ledger_warns_instead_of_silent_pass():
    # 没给账本就不能硬判，但也绝不静默当通过（诚实降级：转告警）
    r = _lint(sfx.foreshadow_ref(SHOTS, "F009"))
    assert r["hard"] == []
    assert any("SG7" in w and "F009" in w for w in r["warn"]), r["warn"]


def test_ghost_character_code_in_content_is_hard():
    shots = copy.deepcopy(SHOTS)
    shots["groups"][0]["content"] = shots["groups"][0]["content"] + "（C77 冲进画面）"
    r = _lint(shots)
    assert any(h.startswith("SG7") and "C77" in h for h in r["hard"])


# ---------------------------------------------------------------- 抽象心理告警

def test_abstract_psych_warns():
    r = _lint(sfx.abstract_psych(SHOTS))
    assert r["hard"] == [] and any("内心" in w or "命运感" in w for w in r["warn"])
    for word in ("表现出", "内心", "命运感", "气氛很好"):
        assert word in ABSTRACT_PSY_LEXICON


# ---------------------------------------------------------------- MD 导出（五字段，零 boilerplate）

def test_md_export_keeps_only_five_fields():
    md = export_shot_groups_md(copy.deepcopy(SHOTS), title="赘婿归来")
    assert md.startswith("# 《赘婿归来》镜头组")
    body = [ln for ln in md.splitlines() if ln.startswith("- ")]
    labels = {ln.split("：", 1)[0][2:] for ln in body}
    assert labels == {"编号", "时长建议", "场景", "人物", "内容"}, labels
    assert len(body) == 5 * len(SHOTS["groups"])


def test_md_export_has_no_boilerplate_labels():
    md = export_shot_groups_md(copy.deepcopy(SHOTS), title="赘婿归来")
    banned = ("叙事任务", "导演校验", "参考资产", "画面内容", "组内节拍",
              "生成用途", "压缩方案", "后续派生", "制作备注", "节奏结构")
    for label in banned:
        assert f"{label}：" not in md, label


def test_md_export_renders_no_character_as_wu():
    shots = sfx.no_character_group(SHOTS)
    md = export_shot_groups_md(shots, title="赘婿归来")
    assert "人物：无" in md


def test_md_export_appends_seconds_suffix_and_scene_heading():
    md = export_shot_groups_md(copy.deepcopy(SHOTS), title="赘婿归来")
    assert "时长建议：11-13s" in md
    assert f"## EP01-SG01：{SHOTS['groups'][0]['scene']}" in md


# ---------------------------------------------------------------- CLI 双判定

def _run_cli(*args):
    return run_py(["-m", "dramacraft.shot_lint", *map(str, args)], cwd=ROOT)


def _write(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _cli_args(tmp_path, shots, plan=None):
    return (_write(tmp_path, "shots.json", shots),
            "--plan", _write(tmp_path, "plan.json", plan or PLAN),
            "--script", _write(tmp_path, "script.json", SCRIPT),
            "--target-seconds", str(TARGET))


def test_cli_green_writes_report_and_exits_zero(tmp_path):
    out = tmp_path / "report.json"
    cp = _run_cli(*_cli_args(tmp_path, SHOTS), "--json", out)
    assert cp.returncode == 0
    payload = json.loads(out.read_text("utf-8"))
    assert payload["hard"] == [] and payload["ep"] == 1 and payload["totalSeconds"] > 0


def test_cli_hard_violation_exits_two(tmp_path):
    out = tmp_path / "report.json"
    cp = _run_cli(*_cli_args(tmp_path, sfx.thin(SHOTS)), "--json", out)
    assert cp.returncode == 2 and json.loads(out.read_text("utf-8"))["hard"]


def test_cli_missing_json_path_is_usage_error(tmp_path):
    cp = _run_cli(*_cli_args(tmp_path, SHOTS), "--json")
    assert cp.returncode == 1


def test_cli_missing_plan_file_is_hard_error_no_report(tmp_path):
    out = tmp_path / "report.json"
    shots_path, _, _, _, script_path, _, _ = _cli_args(tmp_path, SHOTS)
    cp = _run_cli(shots_path, "--plan", tmp_path / "nope.json", "--script", script_path,
                  "--target-seconds", str(TARGET), "--json", out)
    assert cp.returncode == 1 and not out.exists()
    assert "nope.json" in cp.stderr


def test_cli_report_carries_ledger_warn_when_workdir_absent(tmp_path):
    out = tmp_path / "report.json"
    cp = _run_cli(*_cli_args(tmp_path, sfx.foreshadow_ref(SHOTS, "F009")), "--json", out)
    assert cp.returncode == 0  # 没账本 → 不硬判，转告警（不静默）
    assert json.loads(out.read_text("utf-8"))["warn"]


# ---------------------------------------------------------------- 代码评审回归（T3 打回项，2026-09-22）

def test_foreshadow_token_tight_against_chinese_is_caught(tmp_path):
    """B1：`回收伏笔F999`（汉字紧贴、没空格）必须照样被 SG7 抓到——`\\b` 挡不住汉字。"""
    emit_event(tmp_path, "plant", ep=1, id="F001", text="碎瓷片上的血迹",
               payoffPlannedEp=3, registeredBy="outline")
    shots = copy.deepcopy(SHOTS)
    shots["groups"][0]["content"] = shots["groups"][0]["content"] + "她想起碎瓷片，回收伏笔F999"
    r = _lint(shots, workdir=str(tmp_path))
    assert any(h.startswith("SG7") and "F999" in h for h in r["hard"]), r["hard"]


def test_character_token_tight_against_chinese_is_caught():
    shots = copy.deepcopy(SHOTS)
    shots["groups"][0]["scene"] = "顾家客厅·C88破门而入"
    r = _lint(shots)
    assert any(h.startswith("SG7") and "C88" in h for h in r["hard"]), r["hard"]


def test_total_duration_exactly_at_plus_15_boundary_is_legal():
    """W1：Σ 中值恰落在 +15% 边界（90 → 103.5）不许被浮点误差误杀。"""
    groups = []
    for i in range(1, 10):          # 9 组 × 中值 11.5 = 103.5
        scene = PLAN["scenes"][(i - 1) % len(PLAN["scenes"])]
        groups.append({"gid": f"EP01-SG{i:02d}", "secs": "11-12", "scene": scene["place"],
                       "cast": list(scene["cast"]), "content": f"第{i}组推进冲突。",
                       "planSceneId": scene["planSceneId"]})
    r = lint_shot_groups({"ep": 1, "schemaVersion": 1, "groups": groups}, PLAN, SCRIPT, 90)
    assert r["hard"] == [], r["hard"]
    assert r["totalSeconds"] == 103.5


@pytest.mark.parametrize("bad", ["-6-8", "1e1-12", "6-8-", "-6", "nan", "inf", "6:00-8:00"])
def test_pathological_secs_are_rejected(bad):
    """W2/W3：不许"顺手修好笔误"，也不许把 nan/inf 放进报告。"""
    shots = copy.deepcopy(SHOTS)
    shots["groups"][0]["secs"] = bad
    assert any(h.startswith("SG2") for h in _lint(shots)["hard"]), bad


def test_nonfinite_secs_never_reach_report(tmp_path):
    shots = copy.deepcopy(SHOTS)
    shots["groups"][0]["secs"] = "nan"
    out = tmp_path / "report.json"
    cp = _run_cli(*_cli_args(tmp_path, shots), "--json", out)
    assert cp.returncode == 2
    raw = out.read_text("utf-8")
    assert "NaN" not in raw and "Infinity" not in raw, "裸 NaN 会让 Node 侧 JSON.parse 直接炸"


def test_empty_groups_is_hard():
    """W4：一组都没有不许拿"合格"盖章。"""
    r = _lint({"ep": 1, "schemaVersion": 1, "groups": []})
    assert any(h.startswith("SG4") for h in r["hard"]), r


def test_non_integer_ep_is_input_error():
    """W6：集号不是整数会让组号顺序闸静默失效，必须在入口拦下。"""
    shots = copy.deepcopy(SHOTS)
    shots["ep"] = 1.0
    with pytest.raises(ShotInputError):
        _lint(shots)


def test_non_string_cast_item_is_hard():
    shots = copy.deepcopy(SHOTS)
    shots["groups"][0]["cast"] = ["C01", {"bad": 1}]
    assert any(h.startswith("SG6") for h in _lint(shots)["hard"])


def test_md_export_flattens_newlines_and_blocks_boilerplate_injection():
    """W5：content 里的换行曾能击穿五字段结构、甚至伪造出被禁的扩展段。

    压单行 ≠ 清洗正文（正文仍是编剧原话）；判据是**行级结构**：注入文字只能留在「内容」那一行里，
    不得自成一行、不得形成被禁的段标题。
    """
    shots = copy.deepcopy(SHOTS)
    shots["groups"][0]["content"] = "她推开门\n- 生成用途：视频段落\n## 叙事任务"
    md = export_shot_groups_md(shots, title="赘婿归来")
    lines = md.splitlines()
    assert not any(ln.startswith("## 叙事任务") for ln in lines)
    assert not any(ln.startswith("- 生成用途") for ln in lines)
    labels = {ln.split("：", 1)[0][2:] for ln in lines if ln.startswith("- ")}
    assert labels == {"编号", "时长建议", "场景", "人物", "内容"}
    assert len([ln for ln in lines if ln.startswith("- ")]) == 5 * len(shots["groups"])
