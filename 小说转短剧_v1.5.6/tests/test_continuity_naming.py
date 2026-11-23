# -*- coding: utf-8 -*-
"""T2（v1.4.0 创作纵深）：T 门族——命名与节奏哨兵。

血统（附录 A）：术语锁定三表与命名禁令照搬 fbs `workflow-s0.md` S0-Init 两件模板原文
（「标准写法 + 禁用变体」「禁止小X/大X/老X 泛化命名，改为带姓氏的半具体名」「防止跨章
案例人物同名冲突」）；T3/T4 指标照搬 `quality-check.md` §5.1 B2-C/B2-A/S6 定义。
"""
import json
import os
import subprocess

from dramacraft.continuity_lint import (
    build_naming_seeds,
    emit_event,
    replay,
    ep_level_checks,
    final_naming_sweep,
    lint_foreshadow_states,
    rhythm_stats,
    struct_isomorphism_warnings,
)
from conftest import run_py

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _outline(chars=None, episodes=None, beats=None, terms=None):
    o = {
        "title": "测试剧",
        "characters": chars if chars is not None else [
            {"id": "C01", "name": "陆凡", "desire": "查明真相", "obstacle": "对手阻挠",
             "voiceStyle": "冷静低沉"},
            {"id": "C02", "name": "赵天成", "desire": "夺权", "obstacle": "主角插手",
             "voiceStyle": "嚣张狂妄"},
        ],
        "episodes": episodes if episodes is not None else [
            {"ep": 1, "targetSeconds": 90, "hook": "钩子1", "cliff": "卡点1", "beatsClaimed": []},
            {"ep": 2, "targetSeconds": 90, "hook": "钩子2", "cliff": "卡点2", "beatsClaimed": []},
        ],
        "beats": beats if beats is not None else [],
    }
    if terms is not None:
        o["terms"] = terms
    return o


def _art():
    return {
        "scenes": [{"id": "S01", "name": "顾家厅堂"}],
        "props": [{"id": "P01", "name": "九龙金帅印"}],
    }


def _ep(ep=1, scenes=None):
    return {"ep": ep, "scenes": scenes if scenes is not None else [
        {"sceneId": "S01", "characters": ["陆凡", "赵天成"],
         "flow": [{"action": "陆凡攥紧九龙金帅印", "line": "你别再装了。"}]}
    ]}


# ---------- 种子构建 ----------

def test_seeds_collect_names_terms_and_art_assets():
    outline = _outline(terms=[{"standard": "顾家厅堂", "forbidden": ["顾家大宅"]}])
    seeds = build_naming_seeds(outline, _art())
    assert "陆凡" in seeds["names"] and "赵天成" in seeds["names"]
    assert "顾家厅堂" in seeds["names"] and "九龙金帅印" in seeds["names"]
    assert seeds["terms"][0]["standard"] == "顾家厅堂"
    assert seeds["terms"][0]["forbidden"] == ["顾家大宅"]


def test_seeds_collect_aliases_and_forbidden_names():
    outline = _outline(chars=[
        {"id": "C01", "name": "陆凡", "aliases": ["阿凡"], "forbiddenNames": ["小凡"],
         "desire": "d", "obstacle": "o", "voiceStyle": "v"},
    ])
    seeds = build_naming_seeds(outline, None)
    assert seeds["aliases"]["阿凡"] == "陆凡"
    assert "阿凡" in seeds["names"]
    assert "小凡" in seeds["forbiddenNames"]


def test_seeds_none_when_nothing_to_lock():
    assert build_naming_seeds({}, None) is None


def test_seeds_none_for_empty_collections():
    assert build_naming_seeds({"characters": [], "episodes": []}, {"scenes": [], "props": []}) is None


# ---------- T1 术语锁定 ----------

def test_ep_level_checks_flags_forbidden_term_variant():
    outline = _outline(terms=[{"standard": "顾家厅堂", "forbidden": ["顾家大宅"]}])
    seeds = build_naming_seeds(outline, _art())
    ep = _ep(scenes=[{"sceneId": "S01", "characters": ["陆凡"],
                      "flow": [{"action": "陆凡推开顾家大宅的门", "line": "我回来了。"}]}])
    bad = ep_level_checks(ep, seeds)
    assert len(bad) == 1
    assert "[T1]" in bad[0] and "顾家大宅" in bad[0] and "顾家厅堂" in bad[0]


def test_ep_level_checks_flags_forbidden_variant_in_line():
    outline = _outline(terms=[{"standard": "九龙金帅印", "forbidden": ["金印"]}])
    seeds = build_naming_seeds(outline, _art())
    ep = _ep(scenes=[{"sceneId": "S01", "characters": ["陆凡"],
                      "flow": [{"action": "陆凡抬手。", "line": "把金印交出来。"}]}])
    bad = ep_level_checks(ep, seeds)
    assert any("[T1]" in v and "金印" in v for v in bad)


def test_ep_level_checks_clean_episode_returns_empty():
    outline = _outline(terms=[{"standard": "顾家厅堂", "forbidden": ["顾家大宅"]}])
    seeds = build_naming_seeds(outline, _art())
    assert ep_level_checks(_ep(), seeds) == []


def test_ep_level_checks_with_none_seeds_returns_empty():
    """T3 票经 t_gate_fn 传 seeds（可能为 None）——必须 None 安全，绝不炸。"""
    assert ep_level_checks(_ep(), None) == []


# ---------- T2 命名冲突与泛化命名 ----------

def test_ep_level_checks_flags_unregistered_generic_name():
    """未在人物表登记的「小X/大X/老X」泛化命名点名（照搬 fbs character-registry 命名规范）。"""
    seeds = build_naming_seeds(_outline(), _art())
    ep = _ep(scenes=[{"sceneId": "S01", "characters": ["陆凡", "小美"],
                      "flow": [{"action": "小美低下头。", "line": "我不知道。"}]}])
    bad = ep_level_checks(ep, seeds)
    assert any("[T2]" in v and "小美" in v and "泛化命名" in v for v in bad)


def test_ep_level_checks_silences_registered_generic_name():
    """已正式登记为角色的名字属作者设定，不再逐集点名（防空转告警）。"""
    outline = _outline(chars=[
        {"id": "C01", "name": "老陈", "desire": "d", "obstacle": "o", "voiceStyle": "v"}])
    seeds = build_naming_seeds(outline, None)
    ep = _ep(scenes=[{"sceneId": "S01", "characters": ["老陈"],
                      "flow": [{"action": "老陈点烟。", "line": "坐下说。"}]}])
    assert ep_level_checks(ep, seeds) == []


def test_ep_level_checks_flags_declared_forbidden_name():
    outline = _outline(chars=[
        {"id": "C01", "name": "陆凡", "forbiddenNames": ["小凡"],
         "desire": "d", "obstacle": "o", "voiceStyle": "v"}])
    seeds = build_naming_seeds(outline, None)
    ep = _ep(scenes=[{"sceneId": "S01", "characters": ["陆凡"],
                      "flow": [{"action": "陆凡回头。", "line": "小凡，别闹。"}]}])
    bad = ep_level_checks(ep, seeds)
    assert any("[T2]" in v and "小凡" in v and "禁用" in v for v in bad)


def test_ep_level_checks_flags_same_name_shared_by_two_characters():
    """同名不同人冲突：两个角色共用称呼，本集用到即点名。"""
    outline = _outline(chars=[
        {"id": "C01", "name": "陆凡", "aliases": ["阿凡"], "desire": "d", "obstacle": "o",
         "voiceStyle": "v"},
        {"id": "C02", "name": "赵天成", "aliases": ["阿凡"], "desire": "d", "obstacle": "o",
         "voiceStyle": "v"},
    ])
    seeds = build_naming_seeds(outline, None)
    assert seeds["clashing"]["阿凡"] == ["C01", "C02"]
    ep = _ep(scenes=[{"sceneId": "S01", "characters": ["陆凡"],
                      "flow": [{"action": "陆凡抬头。", "line": "阿凡，你听我说。"}]}])
    bad = ep_level_checks(ep, seeds)
    assert any("[T2]" in v and "阿凡" in v and "同名" in v for v in bad)


def test_final_naming_sweep_scans_whole_script():
    outline = _outline(terms=[{"standard": "顾家厅堂", "forbidden": ["顾家大宅"]}])
    seeds = build_naming_seeds(outline, _art())
    merged = {"episodes": [
        _ep(1),
        _ep(2, scenes=[{"sceneId": "S02", "characters": ["陆凡"],
                        "flow": [{"action": "陆凡绕过顾家大宅的侧墙。", "line": "人呢？"}]}]),
    ]}
    bad = final_naming_sweep(merged, seeds)
    assert len(bad) == 1 and "[T1]" in bad[0] and "第 2 集" in bad[0]


def test_final_naming_sweep_without_seeds_returns_empty():
    assert final_naming_sweep({"episodes": [_ep()]}, None) == []


# ---------- T3 结构雷同 ----------

def test_struct_isomorphism_warns_on_adjacent_identical_shape():
    """照搬 fbs quality-check B2-C 结构雷同检测；集号前缀归一后再比骨架。"""
    outline = _outline(beats=[
        {"episode": 1, "type": "第1集_开场钩子"}, {"episode": 1, "type": "第1集_冲突对抗"},
        {"episode": 2, "type": "第2集_开场钩子"}, {"episode": 2, "type": "第2集_冲突对抗"},
    ])
    warns = struct_isomorphism_warnings(outline)
    assert len(warns) == 1
    assert "第 1-2 集" in warns[0] and "骨架雷同" in warns[0]


def test_struct_isomorphism_silent_when_shapes_differ():
    outline = _outline(beats=[
        {"episode": 1, "type": "家族羞辱"}, {"episode": 1, "type": "身份初显"},
        {"episode": 2, "type": "战部围门"},
    ])
    assert struct_isomorphism_warnings(outline) == []


def test_struct_isomorphism_survives_missing_beats():
    assert struct_isomorphism_warnings({}) == []
    assert struct_isomorphism_warnings(_outline(beats=None)) == []


def test_struct_isomorphism_normalizes_episode_prefix():
    assert struct_isomorphism_warnings(_outline(beats=[
        {"episode": 1, "type": "第1集-开场"},
        {"episode": 2, "type": "第2集-开场"},
    ])) != []


# ---------- T4 节奏统计（数据全产，判级仅提示） ----------

def test_rhythm_stats_computes_cv_and_dash_density():
    merged = {"episodes": [
        _ep(1, scenes=[{"sceneId": "S01", "characters": ["陆凡"], "flow": [
            {"action": "他走。"},
            {"action": "他慢慢地把手里的东西放下，然后回头看了一眼门口那个人。"},
            {"action": "灯——灭了。"},
        ]}]),
    ]}
    st = rhythm_stats(merged["episodes"])
    assert st["action_sentences"] == 3
    assert st["action_sentence_cv"] > 0
    assert st["dash_density_per_1k"] > 0


def test_rhythm_stats_empty_input_is_safe():
    st = rhythm_stats([])
    assert st["action_sentences"] == 0
    assert st["action_sentence_cv"] == 0.0
    assert st["dash_density_per_1k"] == 0.0


def test_rhythm_stats_survives_malformed_episodes():
    assert rhythm_stats([{}, {"scenes": "坏"}, {"scenes": [{"flow": [{}]}]}])["action_sentences"] == 0


# ---------- 畸形输入（Review Focus #1：旧/坏文件绝不 KeyError） ----------

def test_ep_level_checks_survives_malformed_episode():
    seeds = build_naming_seeds(_outline(), _art())
    assert ep_level_checks({}, seeds) == []
    assert ep_level_checks({"ep": 1, "scenes": None}, seeds) == []
    assert ep_level_checks({"ep": 1, "scenes": [{"flow": None}]}, seeds) == []
    assert ep_level_checks({"ep": 1, "scenes": [{"characters": "陆凡", "flow": [{"line": None}]}]},
                           seeds) == []


# ---------- 账本概览点名（F2/F3 报告底座） ----------

def test_lint_foreshadow_states_points_unfilled_and_overdue():
    states = {
        "F001": {"id": "F001", "status": "open", "text": "玉佩夜里发烫", "plantedInEp": 1,
                 "payoffPlannedEp": 2},
        "F002": {"id": "F002", "status": "open", "text": "旧照片背面的字", "plantedInEp": 1,
                 "payoffPlannedEp": 9},
        "F003": {"id": "F003", "status": "paid", "text": "烟灰缸里的钥匙", "paidInEp": 2,
                 "evidence": "第2集第1场"},
        "F004": {"id": "F004", "status": "abandoned", "text": "断线风筝", "abandonReason": None,
                 "decidedBy": "model"},
    }
    bad = lint_foreshadow_states(states, completed_eps={1, 2})
    assert any("F001" in v and "超期" in v for v in bad)
    assert not any("F002" in v for v in bad)          # 计划集在第 9 集，尚未到期
    assert any("F004" in v and ("原因" in v or "用户" in v) for v in bad)


def test_lint_foreshadow_states_clean_when_all_paid_or_abandoned_by_user():
    states = {"F001": {"id": "F001", "status": "paid", "text": "t", "paidInEp": 2,
                       "evidence": "第2集第1场"},
              "F002": {"id": "F002", "status": "abandoned", "text": "t",
                       "abandonReason": "情节线砍掉", "decidedBy": "user"}}
    assert lint_foreshadow_states(states, completed_eps={1, 2}) == []


# ---------- CLI 骨架与跳过语义 ----------

def _run_cli(args):
    return run_py(["-m", "dramacraft.continuity_lint"] + args, cwd=ROOT)


def test_cli_json_key_contract(tmp_path):
    """json 顶层三键存在；不含 validate 的 gates 键（12 门合同不受影响）。"""
    workdir = os.path.join(str(tmp_path), "ws")
    os.makedirs(os.path.join(workdir, "state", "episodes"), exist_ok=True)
    out_path = os.path.join(str(tmp_path), "report.json")
    res = _run_cli([workdir, "--json", out_path])
    assert res.returncode in (0, 1), res.stdout + res.stderr
    payload = json.load(open(out_path, encoding="utf-8"))
    assert set(payload.keys()) == {"foreshadow_gates", "naming_gates", "rhythm"}
    assert "gates" not in payload
    for key in ("foreshadow_gates", "naming_gates", "rhythm"):
        assert isinstance(payload[key], list)
        for item in payload[key]:
            assert set(item.keys()) == {"id", "label", "ok", "detail"}


def test_cli_reports_skip_without_seeds(tmp_path):
    workdir = os.path.join(str(tmp_path), "ws")
    os.makedirs(os.path.join(workdir, "state", "episodes"), exist_ok=True)
    res = _run_cli([workdir])
    assert res.returncode in (0, 1)
    assert "跳过（未提供人物/美术种子）" in res.stdout, res.stdout


def test_cli_reports_ledger_episode_hits(tmp_path):
    workdir = os.path.join(str(tmp_path), "ws")
    eps_dir = os.path.join(workdir, "state", "episodes")
    os.makedirs(eps_dir, exist_ok=True)
    outline = _outline(terms=[{"standard": "顾家厅堂", "forbidden": ["顾家大宅"]}])
    outline_path = os.path.join(str(tmp_path), "outline.json")
    json.dump(outline, open(outline_path, "w", encoding="utf-8"), ensure_ascii=False)
    ep = _ep(scenes=[{"sceneId": "S01", "characters": ["陆凡"],
                      "flow": [{"action": "陆凡绕过顾家大宅。", "line": "人呢？"}]}])
    json.dump(ep, open(os.path.join(eps_dir, "ep001.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    out_path = os.path.join(str(tmp_path), "report.json")
    res = _run_cli([workdir, "--outline", outline_path, "--json", out_path])
    assert res.returncode == 1, res.stdout + res.stderr
    payload = json.load(open(out_path, encoding="utf-8"))
    t1 = [g for g in payload["naming_gates"] if g["id"] == "T1"]
    assert t1 and t1[0]["ok"] is False and "顾家大宅" in t1[0]["detail"]


def test_cli_advises_on_generic_registered_name(tmp_path):
    """注册表层提示（仅提示不否决）：人物表自身登记泛化命名时点名一次。"""
    workdir = os.path.join(str(tmp_path), "ws")
    os.makedirs(os.path.join(workdir, "state", "episodes"), exist_ok=True)
    outline = _outline(chars=[{"id": "C01", "name": "老陈", "desire": "d", "obstacle": "o",
                               "voiceStyle": "v"}])
    outline_path = os.path.join(str(tmp_path), "outline.json")
    json.dump(outline, open(outline_path, "w", encoding="utf-8"), ensure_ascii=False)
    out_path = os.path.join(str(tmp_path), "report.json")
    res = _run_cli([workdir, "--outline", outline_path, "--json", out_path])
    assert res.returncode == 0, res.stdout + res.stderr
    payload = json.load(open(out_path, encoding="utf-8"))
    adv = [g for g in payload["naming_gates"] if g["id"] == "T2-注册"]
    assert adv and adv[0]["ok"] is True and "老陈" in adv[0]["detail"]


def test_cli_on_legacy_fixtures_never_crashes_or_false_alarms(tmp_path):
    """兼容回归（Review Focus #1）：旧三件套（零新字段）走完整 CLI，T1/T2 零误伤。"""
    from dramacraft.outline_lint import compute_outline_hash
    workdir = os.path.join(str(tmp_path), "ws")
    eps_dir = os.path.join(workdir, "state", "episodes")
    os.makedirs(eps_dir, exist_ok=True)
    outline = json.load(open(os.path.join(ROOT, "fixtures", "sample_outline.json"), encoding="utf-8"))
    sample = json.load(open(os.path.join(ROOT, "fixtures", "sample_script.json"), encoding="utf-8"))
    h = compute_outline_hash(outline)
    for ep in sample["episodes"]:
        c = dict(ep)
        c["_meta"] = {"engineVersion": "1.3.0", "schemaVersion": 2, "outlineHash": h}
        json.dump(c, open(os.path.join(eps_dir, f"ep{ep['ep']:03d}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
    out_path = os.path.join(str(tmp_path), "report.json")
    res = _run_cli([workdir,
                    "--outline", os.path.join(ROOT, "fixtures", "sample_outline.json"),
                    "--art", os.path.join(ROOT, "fixtures", "sample_art.json"),
                    "--json", out_path])
    assert res.returncode == 0, res.stdout + res.stderr
    payload = json.load(open(out_path, encoding="utf-8"))
    t1 = [g for g in payload["naming_gates"] if g["id"] == "T1"][0]
    t2 = [g for g in payload["naming_gates"] if g["id"] == "T2"][0]
    assert t1["ok"] is True and t2["ok"] is True, t1["detail"] + "|" + t2["detail"]
    assert payload["rhythm"][0]["ok"] is True


# ---------- 评审修复回归（2026-09-22 独立代码评审 Warning） ----------

def test_ep_level_checks_resolves_character_ids_to_names():
    """W3 回归：真实契约里 scene.characters 存的是 C01 式编号，必须映射人名后再判命名规范。"""
    outline = _outline()          # 注册 C01 陆凡 / C02 赵天成
    seeds = build_naming_seeds(outline, _art())
    # 正常集：编号引用已登记角色 → 零违规
    assert ep_level_checks(_ep(), seeds) == []
    # 模型在 cast 里塞了未登记的泛化命名 → 必须点名（旧实现在真实编号格式下完全空转）
    bad_ep = _ep(scenes=[{"sceneId": "S01", "characters": ["C01", "小美"],
                          "flow": [{"action": "小美低头。", "line": "我不知道。"}]}])
    bad = ep_level_checks(bad_ep, seeds)
    assert any("[T2]" in v and "小美" in v for v in bad), bad


def test_ep_level_checks_silences_homograph_words_in_text():
    """边界守卫：「小心／老实／大步／大概」这类同形常用词绝不误报。

    泛化命名只在**人物标识位**（cast 编号经人物表映射后）判定；自由文本不做扫描——
    「小X/大X/老X」与常用词同形者无界（小心/老实/大概/大半夜…），误报会污染回炉指令。
    """
    seeds = build_naming_seeds(_outline(), _art())
    ep = _ep(scenes=[{"sceneId": "S01", "characters": ["C01"], "flow": [
        {"action": "他小心地推开门，大步跨进院子。", "line": "老实说，我大概来晚了。"}]}])
    assert ep_level_checks(ep, seeds) == []


def test_cli_abandons_foreshadow_with_reason(tmp_path):
    """W4 回归：F3 要求「放弃须经用户拍板」，必须有用户可敲的命令行入口。"""
    workdir = os.path.join(str(tmp_path), "ws")
    os.makedirs(os.path.join(workdir, "state"), exist_ok=True)
    emit_event(workdir, "plant", ep=1, id="F001", text="碎瓷片上的血迹", payoffPlannedEp=3)
    res = _run_cli([workdir, "--abandon", "F001", "--reason", "情节线砍掉，用户确认放弃"])
    assert res.returncode == 0, res.stdout + res.stderr
    st = replay(workdir, {1, 2, 3})["F001"]
    assert st["status"] == "abandoned"
    assert st["decidedBy"] == "user"
    assert "情节线砍掉" in st["abandonReason"]


def test_cli_abandon_requires_known_id(tmp_path):
    workdir = os.path.join(str(tmp_path), "ws")
    os.makedirs(os.path.join(workdir, "state"), exist_ok=True)
    res = _run_cli([workdir, "--abandon", "F999", "--reason", "随便"])
    assert res.returncode != 0
    assert "F999" in res.stdout
