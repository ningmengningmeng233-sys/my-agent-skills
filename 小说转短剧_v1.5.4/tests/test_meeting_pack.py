# -*- coding: utf-8 -*-
"""T6 会议机读件 meeting_pack.py（工单 T6；方案 §4 + fbs session-protocols v2.1.2）

口径订正（2026-09-22）：工单原写「fbs 尾部格式**九字段**强校验」——原件 session-protocols.md :60-72
的纪要是**五个字段**（核心结论/关键决策/被放弃的方向/待人类确认的问题/下一步）+ 会议类型 + 日期。
按原件执行；工单与《会议机制卡》同步注明。
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dramacraft.meeting_pack import (  # noqa: E402
    MINUTES_FIELDS,
    MeetingInputError,
    append_concerns,
    append_minutes,
    build_brief,
    decide_concern,
    grade_review,
    merge_concerns,
    reader_closure_open,
    write_brief,
)
from conftest import run_py


# ---------------------------------------------------------------- 会议简报

def test_brief_is_single_source_template_with_length_guard():
    brief = build_brief("reader", "赘婿归来", "确认前三集留不留人", "x" * 900)
    assert len(brief) <= 620 and "…（截断）" in brief
    assert "会议简报 · 读者会" in brief
    assert "剧名：赘婿归来" in brief and "本次目标：确认前三集留不留人" in brief
    # 关键背景 ≤300 字（原件 :25-38）
    background_line = next(ln for ln in brief.splitlines() if ln.startswith("关键背景"))
    assert len(background_line) <= 320, len(background_line)


def test_brief_short_background_kept_intact():
    brief = build_brief("review", "赘婿归来", "过一遍 P0", "世界观只有两条设定")
    assert "世界观只有两条设定" in brief and "…（截断）" not in brief


def test_write_brief_lands_under_state_meetings(tmp_path):
    path = write_brief(tmp_path, "reader", "简报正文")
    assert path == tmp_path / "state" / "meetings" / "brief-reader.md"
    assert "简报正文" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 关切合并

def test_duplicate_concerns_merge_by_jaccard():
    # 阈值 0.8 = 精度优先：只并"几乎同一句"，不并"相似但语义可能相反"的
    merged = merge_concerns([[{"t": "开头太慢拖沓"}], [{"t": "开头太慢，拖沓！"}], [{"t": "世界观看不懂"}]])
    assert len(merged) == 2
    assert merged[0]["votes"] == 2, merged
    assert sum(m["votes"] for m in merged) == 3, "合并不得丢票"


def test_opposite_polarity_concerns_do_not_merge():
    merged = merge_concerns([[{"t": "节奏太慢"}], [{"t": "节奏太乱"}], [{"t": "主角太弱"}], [{"t": "主角太强"}]])
    assert len(merged) == 4, merged


def test_concern_without_text_is_an_error_not_a_silent_drop():
    with pytest.raises(ValueError):
        merge_concerns([[{"concern": "开头太慢"}]])


def test_merge_concerns_keeps_representative_text_and_sources():
    merged = merge_concerns([[{"t": "开头太慢拖沓", "from": "观众A"}], [{"t": "开头太慢拖沓。", "from": "观众B"}]])
    assert len(merged) == 1
    assert merged[0]["t"] in ("开头太慢拖沓", "开头拖沓节奏慢")
    assert len(merged[0]["merged"]) == 2


# ---------------------------------------------------------------- 评审分级

def test_grade_review_p0_p1_p2():
    items = [
        {"t": "主角动机前后矛盾", "severity": "🔴 严重"},
        {"t": "中段节奏拖", "severity": "🟡 中等"},
        {"t": "某个用词可以更好", "severity": "⚪ 轻微"},
    ]
    g = grade_review(items)
    assert [x["t"] for x in g["P0"]] == ["主角动机前后矛盾"]
    assert [x["t"] for x in g["P1"]] == ["中段节奏拖"]
    assert [x["t"] for x in g["P2"]] == ["某个用词可以更好"]


def test_grade_review_redline_forces_p0_even_if_marked_minor():
    g = grade_review([{"t": "台词被改写了两个字", "severity": "⚪ 轻微", "redline": "R2"}])
    assert [x["t"] for x in g["P0"]] == ["台词被改写了两个字"]


# ---------------------------------------------------------------- 纪要（五字段）

def test_minutes_require_all_five_fields(tmp_path):
    assert set(MINUTES_FIELDS) == {"核心结论", "关键决策", "被放弃的方向", "待人类确认的问题", "下一步"}
    with pytest.raises(ValueError):
        append_minutes(tmp_path, "reader", {"核心结论": "x"})
    with pytest.raises(ValueError):
        append_minutes(tmp_path, "reader", {k: "x" for k in MINUTES_FIELDS if k != "下一步"})


def test_append_minutes_writes_fbs_tail_format(tmp_path):
    path = append_minutes(tmp_path, "reader", {
        "核心结论": "前三集留得住，但第二集要加速",
        "关键决策": ["第二集砍一场", "保留醉酒戏"],
        "被放弃的方向": ["加旁白解释世界观（观众说像上课）"],
        "待人类确认的问题": ["是否接受第二集删到 80 秒"],
        "下一步": "把关切清单逐条给决议",
    })
    text = path.read_text(encoding="utf-8")
    assert text.startswith("━━ 会议纪要 · 读者会 ·"), text[:60]
    for field in MINUTES_FIELDS:
        assert f"{field}：" in text, field
    assert text.rstrip().endswith("━━━━━━━━━━━━━━━━━━")


def test_append_minutes_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        append_minutes(tmp_path, "创意会", {k: "x" for k in MINUTES_FIELDS})


# ---------------------------------------------------------------- 读者关切闭环

def test_unclosed_reader_concern_detected_then_closed(tmp_path):
    append_concerns(tmp_path, [{"id": "RC1", "t": "开头太慢"}], kind="reader")
    assert [c["id"] for c in reader_closure_open(tmp_path)] == ["RC1"]
    decide_concern(tmp_path, "RC1", "接受", reason="第二集起加速")
    assert reader_closure_open(tmp_path) == []


def test_decision_must_be_one_of_three_verdicts(tmp_path):
    append_concerns(tmp_path, [{"id": "RC1", "t": "开头太慢"}], kind="reader")
    with pytest.raises(ValueError):
        decide_concern(tmp_path, "RC1", "随便看看")


def test_closure_only_checks_reader_kind(tmp_path):
    append_concerns(tmp_path, [{"id": "RV1", "t": "P0 级硬伤"}], kind="review")
    append_concerns(tmp_path, [{"id": "RC1", "t": "开头太慢"}], kind="reader")
    assert [c["id"] for c in reader_closure_open(tmp_path)] == ["RC1"]   # review 的关切不串门


def test_closure_on_kind_that_never_met_is_input_error(tmp_path):
    """没开过会 ≠ 闭环：没有关切清单就必须报「还没开过」，不许报「条条有决议」。"""
    with pytest.raises(MeetingInputError):
        reader_closure_open(tmp_path, kind="review")


# ---------------------------------------------------------------- 复跑稳定 ID（票 3）

def _concern_lines(tmp_path):
    path = tmp_path / "state" / "meetings" / "concerns-reader.jsonl"
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_reader_rerun_reuses_id_and_preserves_decision(tmp_path):
    from dramacraft.meeting_pack import sync_reader_concerns
    first = sync_reader_concerns(tmp_path, [{"t": "开头太慢", "votes": 2}])
    decide_concern(tmp_path, first[0]["id"], "接受", "加快开头")
    second = sync_reader_concerns(tmp_path, [{"t": "开头太慢。", "votes": 3}, {"t": "人物动机不清", "votes": 1}])
    assert [row["id"] for row in second] == ["RC1", "RC2"]
    assert [row["id"] for row in reader_closure_open(tmp_path)] == ["RC2"]
    rows = (tmp_path / "state" / "meetings" / "concerns-reader.jsonl").read_text("utf-8").splitlines()
    assert len(rows) == 2


def test_reader_rerun_twice_more_appends_nothing(tmp_path):
    """同一工作区反复重跑同一批关切：清单不再变长，已决议的也不许被翻回未决议。"""
    from dramacraft.meeting_pack import sync_reader_concerns
    first = sync_reader_concerns(tmp_path, [{"t": "开头太慢", "votes": 2}])
    decide_concern(tmp_path, first[0]["id"], "接受", "加快开头")
    sync_reader_concerns(tmp_path, [{"t": "开头太慢。", "votes": 3}])
    third = sync_reader_concerns(tmp_path, [{"t": "开头 太慢！", "votes": 4}])
    assert [row["id"] for row in third] == ["RC1"]
    assert len(_concern_lines(tmp_path)) == 1
    assert reader_closure_open(tmp_path) == []


def test_new_concern_takes_next_free_id(tmp_path):
    from dramacraft.meeting_pack import sync_reader_concerns
    sync_reader_concerns(tmp_path, [{"t": "开头太慢"}, {"t": "结尾太赶"}])
    rows = sync_reader_concerns(tmp_path, [{"t": "人物动机不清"}])
    assert [row["id"] for row in rows] == ["RC3"]
    assert len(_concern_lines(tmp_path)) == 3


def test_existing_decision_is_not_overwritten_by_rerun(tmp_path):
    from dramacraft.meeting_pack import sync_reader_concerns
    sync_reader_concerns(tmp_path, [{"t": "开头太慢"}])
    decide_concern(tmp_path, "RC1", "驳回", "这条是风格偏好")
    sync_reader_concerns(tmp_path, [{"t": "开头太慢！", "votes": 9}])
    decisions = (tmp_path / "state" / "meetings" / "decisions-reader.jsonl").read_text("utf-8").splitlines()
    assert len(decisions) == 1 and json.loads(decisions[0])["decision"] == "驳回"
    assert reader_closure_open(tmp_path) == []


def test_same_batch_duplicates_dedupe_and_bad_batch_appends_nothing(tmp_path):
    """同批同句只落一条；有一条读不出正文就整批不落（不许留半批）。"""
    from dramacraft.meeting_pack import sync_reader_concerns
    rows = sync_reader_concerns(tmp_path, [{"t": "开头太慢", "votes": 1}, {"t": "开头太慢。", "votes": 2}])
    assert [row["id"] for row in rows] == ["RC1"]
    assert rows[0]["votes"] == 3                      # 同批同句的票累加，不丢票
    assert len(_concern_lines(tmp_path)) == 1

    other = tmp_path / "other"
    with pytest.raises(ValueError):
        sync_reader_concerns(other, [{"t": "开头太慢"}, {"where": "第 2 集"}])
    assert not (other / "state" / "meetings" / "concerns-reader.jsonl").exists()


def test_sync_concerns_ignores_other_kinds_concerns(tmp_path):
    """评审会的关切不串进读者会的编号（闭环只看一种会）。"""
    from dramacraft.meeting_pack import sync_reader_concerns
    append_concerns(tmp_path, [{"id": "RV1", "t": "P0 级硬伤"}], kind="review")
    rows = sync_reader_concerns(tmp_path, [{"t": "开头太慢"}])
    assert [row["id"] for row in rows] == ["RC1"]


def test_zero_concern_meeting_is_closed_not_unreadable(tmp_path):
    """开过会但一条关切都没有：清单文件在 = 闭环（不是「读不出来」）。"""
    from dramacraft.meeting_pack import sync_reader_concerns
    assert sync_reader_concerns(tmp_path, []) == []
    assert (tmp_path / "state" / "meetings" / "concerns-reader.jsonl").exists()
    assert reader_closure_open(tmp_path) == []           # 没有关切 = 条条有决议


# ---------------------------------------------------------------- 复跑不串账（票 4）

def _cli_round(tmp_path, tag, raw_files, decisions):
    """模拟工作流一轮读者会（走 CLI：与工作流 world.run 的 argv 逐字同形）：
    并条落盘 → 取开放集 → 只对开放项提请决议 → 写纪要 → 返回本轮合并件。"""
    merged = tmp_path / f"merged-{tag}.json"
    cp = _run_cli("sync-concerns", "--workdir", tmp_path,
                  "--inputs", "|".join(str(p) for p in raw_files), "--json", merged)
    assert cp.returncode == 0, cp.stderr
    rows = json.loads(merged.read_text("utf-8"))
    assert isinstance(rows, list) and all(r.get("id") for r in rows), rows   # 工作流按这个形状解析

    closure = tmp_path / f"closure-{tag}.json"
    cp2 = _run_cli("check-closure", "--workdir", tmp_path, "--json", closure)
    assert cp2.returncode in (0, 4), cp2.stderr
    open_ids = [str(c["id"]) for c in json.loads(closure.read_text("utf-8"))["open"]]
    asked = [r for r in rows if r["id"] in open_ids][:6]      # 上限 6 条，已决议的不占名额
    for row in asked:
        cp_dec = _run_cli("decide-concern", "--workdir", tmp_path, "--id", row["id"],
                          "--decision", decisions[row["id"]], "--reason", "用户当场决定",
                          "--json", tmp_path / f"dec-{tag}-{row['id']}.json")
        assert cp_dec.returncode == 0, cp_dec.stderr

    cp3 = _run_cli("minutes-reader", "--workdir", tmp_path, "--merged", merged,
                   "--json", tmp_path / f"minutes-{tag}.json")
    assert cp3.returncode == 0, cp3.stderr
    return rows, [r["id"] for r in asked], cp2.returncode


def test_reader_three_round_rerun_has_no_crosstalk(tmp_path):
    """首轮两条各自决议；重跑同两条不再问；第三次加一条只问新的——清单不重复追加、决议不被翻回。"""
    a = tmp_path / "a.json"
    a.write_text(json.dumps([{"t": "开头太慢", "votes": 2}, {"t": "人物动机不清", "votes": 1}]),
                 encoding="utf-8")
    rows1, asked1, closure1 = _cli_round(tmp_path, "r1", [a], {"RC1": "接受", "RC2": "部分接受"})
    assert [r["id"] for r in rows1] == ["RC1", "RC2"] and asked1 == ["RC1", "RC2"] and closure1 == 4

    rows2, asked2, closure2 = _cli_round(tmp_path, "r2", [a], {})
    assert asked2 == [] and closure2 == 0                    # 已决议：一条都不再问
    assert [r["id"] for r in rows2] == ["RC1", "RC2"]
    assert len(_concern_lines(tmp_path)) == 2                # 重跑不重复追加

    b = tmp_path / "b.json"
    b.write_text(json.dumps([{"t": "开头太慢", "votes": 3}, {"t": "人物动机不清", "votes": 1},
                             {"t": "结尾太赶", "votes": 1}]), encoding="utf-8")
    rows3, asked3, closure3 = _cli_round(tmp_path, "r3", [b], {"RC3": "驳回"})
    assert asked3 == ["RC3"] and closure3 == 4               # 只问新来的那一条
    assert [r["id"] for r in rows3] == ["RC1", "RC2", "RC3"]
    assert len(_concern_lines(tmp_path)) == 3

    fields = json.loads((tmp_path / "minutes-r3.json").read_text("utf-8"))["fields"]
    assert "未决议 0 条" in fields["核心结论"]
    assert any("RC1：接受" in x for x in fields["关键决策"])
    assert any("RC3：结尾太赶" in x for x in fields["被放弃的方向"])   # 驳回项进「被放弃的方向」


def test_ask_quota_skips_decided_and_keeps_new_ones(tmp_path):
    """6 条询问上限：已决议的不占名额，新关切照样问得到。"""
    a = tmp_path / "a.json"
    a.write_text(json.dumps([{"t": f"关切{i}"} for i in range(1, 8)], ensure_ascii=False), encoding="utf-8")
    rows1, asked1, _ = _cli_round(tmp_path, "q1", [a], {f"RC{i}": "接受" for i in range(1, 7)})
    assert len(rows1) == 7 and asked1 == [f"RC{i}" for i in range(1, 7)]     # 7 条里只问前 6 条

    rows2, asked2, closure2 = _cli_round(tmp_path, "q2", [a], {"RC7": "接受"})
    assert asked2 == ["RC7"], asked2                       # 前 6 条已决议：第 7 条这次问得到
    assert closure2 == 4 and len(_concern_lines(tmp_path)) == 7


# ---------------------------------------------------------------- 评审会纪要（票 5）

REVIEW_CASES = [
    ([], "无 P0", False),
    ([{"t": "主角动机塌", "where": "ep1 Sc3", "advice": "补一个动机镜头"}], "卡住，先改（我补一句方向）", True),
    ([{"t": "红线违规", "where": "ep2 Sc1", "advice": "删掉"}], "照常交付，问题记进报告", False),
]


@pytest.mark.parametrize("p0, p0_decision, holds", REVIEW_CASES)
def test_review_minutes_reflect_p0_handling(p0, p0_decision, holds):
    from dramacraft.meeting_pack import build_review_minutes
    minutes = build_review_minutes({"P0": p0, "P1": [{"t": "中段拖"}], "P2": []},
                                   "均衡（问题和亮点并重）", p0_decision)
    assert set(minutes) == set(MINUTES_FIELDS)
    assert p0_decision in minutes["核心结论"]
    assert "均衡（问题和亮点并重）" in minutes["核心结论"]
    assert ("没有 P0 硬伤" in minutes["关键决策"][0]) is (not p0)
    assert ("无" == minutes["待人类确认的问题"][0]) is (not p0)


@pytest.mark.parametrize("p0, p0_decision, holds", REVIEW_CASES)
def test_cli_review_minutes_land_on_disk(tmp_path, p0, p0_decision, holds):
    """评审只要开了会就得落五字段纪要（含 P0 的真实处置）——三档都要落。"""
    graded = tmp_path / "graded.json"
    graded.write_text(json.dumps({"P0": p0, "P1": [], "P2": []}, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "minutes.json"
    cp = _run_cli("minutes-review", "--workdir", tmp_path, "--graded", graded,
                  "--strength", "均衡（问题和亮点并重）", "--p0-decision", p0_decision, "--json", out)
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(out.read_text("utf-8"))
    assert payload["landed"] is True and payload["missing"] == []
    text = (tmp_path / "state" / "meetings" / "minutes-review.md").read_text("utf-8")
    for field in MINUTES_FIELDS:
        assert f"{field}：" in text, field
    assert p0_decision in text
    assert (p0[0]["t"] in text) if p0 else "P0 0" in text


def test_review_minutes_refuse_bad_input_instead_of_writing_half_minutes(tmp_path):
    out = tmp_path / "minutes.json"
    cp = _run_cli("minutes-review", "--workdir", tmp_path, "--graded", tmp_path / "nope.json",
                  "--strength", "均衡", "--p0-decision", "无 P0", "--json", out)
    assert cp.returncode == 1
    assert not (tmp_path / "state" / "meetings" / "minutes-review.md").exists()


def test_cli_decide_concern_must_pair_with_an_existing_id(tmp_path):
    """决议必须与关切配对：id 写错的话这条决议什么都关不掉，不许报成功。"""
    append_concerns(tmp_path, [{"id": "RC2", "t": "人物动机不清"}], kind="reader")
    out = tmp_path / "dec.json"
    cp = _run_cli("decide-concern", "--workdir", tmp_path, "--id", "RC2", "--decision", "部分接受",
                  "--reason", "只改前两集", "--json", out)
    assert cp.returncode == 0, cp.stderr
    assert json.loads(out.read_text("utf-8"))["decision"] == "部分接受"
    assert reader_closure_open(tmp_path) == []

    cp2 = _run_cli("decide-concern", "--workdir", tmp_path, "--id", "RC9", "--decision", "接受", "--json", out)
    assert cp2.returncode == 1 and "RC9" in cp2.stderr


def test_cli_grade_review_forces_redline_to_p0(tmp_path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps([{"t": "动机塌", "severity": "🔴 严重"}, {"t": "用词", "severity": "⚪ 轻微"}],
                            ensure_ascii=False), encoding="utf-8")
    b = tmp_path / "b.json"
    b.write_text(json.dumps([{"t": "台词被改写", "redline": "R2"}], ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "graded.json"
    cp = _run_cli("grade-review", "--inputs", f"{a}|{b}", "--json", out)
    assert cp.returncode == 0, cp.stderr
    graded = json.loads(out.read_text("utf-8"))
    assert [x["t"] for x in graded["P0"]] == ["动机塌", "台词被改写"]
    assert [x["t"] for x in graded["P2"]] == ["用词"]
    cp2 = _run_cli("grade-review", "--inputs", str(tmp_path / "nope.json"), "--json", out)
    assert cp2.returncode == 1


# ---------------------------------------------------------------- CLI

def _run_cli(*args):
    return run_py(["-m", "dramacraft.meeting_pack", *map(str, args)], cwd=ROOT)


def test_cli_check_closure_exit_four_when_open(tmp_path):
    append_concerns(tmp_path, [{"id": "RC1", "t": "开头太慢"}], kind="reader")
    out = tmp_path / "closure.json"
    cp = _run_cli("check-closure", "--workdir", tmp_path, "--json", out)
    assert cp.returncode == 4
    payload = json.loads(out.read_text("utf-8"))
    assert [c["id"] for c in payload["open"]] == ["RC1"]

    decide_concern(tmp_path, "RC1", "接受")
    cp2 = _run_cli("check-closure", "--workdir", tmp_path, "--json", out)
    assert cp2.returncode == 0
    assert json.loads(out.read_text("utf-8"))["open"] == []


def test_cli_missing_json_path_is_usage_error(tmp_path):
    cp = _run_cli("check-closure", "--workdir", tmp_path)
    assert cp.returncode == 1


def test_explicit_duplicate_ids_are_rejected(tmp_path):
    """评审 W7：重复 id 会让一条决议关掉两条关切。"""
    with pytest.raises(ValueError):
        append_concerns(tmp_path, [{"id": "RC1", "t": "A"}, {"id": "RC1", "t": "B"}], kind="reader")


# ---------------------------------------------------------------- 复跑时的写作指导（v1.5.1 T3）

def _reader_rows(texts, tmp_path):
    from dramacraft.meeting_pack import sync_reader_concerns
    return sync_reader_concerns(tmp_path, [{"t": t} for t in texts])


def test_reader_guidance_survives_rerun(tmp_path):
    """真跑实测缺口：复跑时旧决议不再重问，却没再进写手指导——已接受的改法必须有第二次生命。"""
    from dramacraft.meeting_pack import reader_guidance, sync_reader_concerns
    rows = _reader_rows(["开头太慢", "结尾太急"], tmp_path)
    decide_concern(tmp_path, rows[0]["id"], "接受", "前十秒补动作")
    decide_concern(tmp_path, rows[1]["id"], "驳回", "悬念保留")
    same = sync_reader_concerns(tmp_path, [{"t": "开头太慢"}, {"t": "结尾太急"}])
    guide = reader_guidance(tmp_path, same)
    assert "开头太慢" in guide and "前十秒补动作" in guide
    assert "结尾太急" not in guide


def test_reader_guidance_carries_accepted_and_partial_only(tmp_path):
    """第三次开会新添一条「部分接受」，指导里应有它和上一条接受的；驳回的仍然不进。"""
    from dramacraft.meeting_pack import reader_guidance, sync_reader_concerns
    rows = _reader_rows(["开头太慢", "结尾太急"], tmp_path)
    decide_concern(tmp_path, rows[0]["id"], "接受", "前十秒补动作")
    decide_concern(tmp_path, rows[1]["id"], "驳回", "悬念保留")
    rolled = sync_reader_concerns(tmp_path, [{"t": "开头太慢"}, {"t": "结尾太急"}, {"t": "中段垮"}])
    decide_concern(tmp_path, rolled[2]["id"], "部分接受", "只压缩过场戏")
    guide = reader_guidance(tmp_path, rolled)
    assert "开头太慢" in guide and "中段垮" in guide and "只压缩过场戏" in guide
    assert "结尾太急" not in guide


def test_reader_guidance_excludes_concerns_not_in_this_round(tmp_path):
    """历史关切不在本轮集合里：即使当初接受了，也不许塞进本轮指导。"""
    from dramacraft.meeting_pack import reader_guidance, sync_reader_concerns
    rows = _reader_rows(["开头太慢", "结尾太急"], tmp_path)
    decide_concern(tmp_path, rows[0]["id"], "接受", "前十秒补动作")
    decide_concern(tmp_path, rows[1]["id"], "接受", "结尾加一拍")
    this_round = sync_reader_concerns(tmp_path, [{"t": "结尾太急"}])
    guide = reader_guidance(tmp_path, this_round)
    assert "结尾太急" in guide and "开头太慢" not in guide


def test_reader_guidance_fails_closed_on_conflicting_decisions(tmp_path):
    """同一条关切出现两个结论矛盾的决议行：不能随便取第一条，必须当场失败。"""
    from dramacraft.meeting_pack import MeetingInputError, reader_guidance
    rows = _reader_rows(["开头太慢"], tmp_path)
    decide_concern(tmp_path, rows[0]["id"], "接受", "前十秒补动作")
    decide_concern(tmp_path, rows[0]["id"], "驳回", "其实不用改")
    with pytest.raises(MeetingInputError):
        reader_guidance(tmp_path, rows)


def test_reader_guidance_fails_closed_on_broken_decisions_file(tmp_path):
    """决议清单读坏：不能当成「没有指导」继续写稿。"""
    from dramacraft.meeting_pack import MeetingInputError, reader_guidance
    rows = _reader_rows(["开头太慢"], tmp_path)
    decide_concern(tmp_path, rows[0]["id"], "接受", "前十秒补动作")
    p = tmp_path / "state" / "meetings" / "decisions-reader.jsonl"
    p.write_text(p.read_text(encoding="utf-8") + "{坏行\n", encoding="utf-8")
    with pytest.raises(MeetingInputError):
        reader_guidance(tmp_path, rows)


def test_reader_guidance_is_empty_when_nothing_accepted(tmp_path):
    from dramacraft.meeting_pack import reader_guidance
    rows = _reader_rows(["开头太慢"], tmp_path)
    decide_concern(tmp_path, rows[0]["id"], "驳回", "悬念保留")
    assert reader_guidance(tmp_path, rows) == ""


def test_reader_guidance_repeats_identical_decision_without_conflict(tmp_path):
    """同一结论重复落行（复跑撞上）不算矛盾：指导照样重建，不误报失败。"""
    from dramacraft.meeting_pack import reader_guidance
    rows = _reader_rows(["开头太慢"], tmp_path)
    decide_concern(tmp_path, rows[0]["id"], "接受", "前十秒补动作")
    decide_concern(tmp_path, rows[0]["id"], "接受", "前十秒补动作")
    guide = reader_guidance(tmp_path, rows)
    assert "前十秒补动作" in guide


def test_cli_reader_guidance_rebuilds_from_disk(tmp_path):
    """工作流按这条 argv 重建指导（与 dwf 里 world.run 的调用逐字同形）。"""
    from dramacraft.meeting_pack import sync_reader_concerns
    sync_reader_concerns(tmp_path, [{"t": "开头太慢"}, {"t": "结尾太急"}])
    decide_concern(tmp_path, "RC1", "接受", "前十秒补动作")
    decide_concern(tmp_path, "RC2", "驳回", "悬念保留")
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps([{"id": "RC1", "t": "开头太慢", "votes": 1},
                                  {"id": "RC2", "t": "结尾太急", "votes": 1}], ensure_ascii=False),
                      encoding="utf-8")
    out = tmp_path / "guidance.json"
    cp = _run_cli("reader-guidance", "--workdir", tmp_path, "--merged", merged, "--json", out)
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(out.read_text("utf-8"))
    assert "开头太慢" in payload["guidance"] and "前十秒补动作" in payload["guidance"]
    assert "结尾太急" not in payload["guidance"]
    assert payload["lines"] == 1


def test_two_round_reader_meeting_keeps_guidance_for_writer(tmp_path):
    """工作流两轮预演（argv 与 dwf 里的 world.run 逐字同形）：

    第一轮开会：RC1 接受、RC2 驳回，纪要落盘，指导含 RC1 的改法。
    第二轮同数据重跑：不再提问（未决议 0 条），但发给写手的指导**仍须**含第一轮接受的改法——
    「ID 稳定」不等于「指导稳定」，旧决议必须在复跑时重新生效。
    """
    def round_trip(tag, texts, decisions_map):
        """返回 (本轮合并件路径, 本轮指导文本, 未决议 id 列表)。"""
        raw_files = []
        for i, t in enumerate(texts):
            p = tmp_path / f"aud-{tag}-{i}.json"
            p.write_text(json.dumps([{"t": t, "where": "第 1 场", "severity": "🔴 严重"}],
                                    ensure_ascii=False), encoding="utf-8")
            raw_files.append(p)
        merged = tmp_path / f"merged-{tag}.json"
        cp = _run_cli("sync-concerns", "--workdir", tmp_path,
                      "--inputs", "|".join(str(p) for p in raw_files), "--json", merged)
        assert cp.returncode == 0, cp.stderr
        closure = tmp_path / f"closure-{tag}.json"
        assert _run_cli("check-closure", "--workdir", tmp_path,
                        "--json", closure).returncode in (0, 4)
        open_ids = [str(c["id"]) for c in json.loads(closure.read_text("utf-8"))["open"]]
        asked = [r for r in json.loads(merged.read_text("utf-8")) if str(r["id"]) in open_ids][:6]
        for row in asked:                                  # 只对未决议的提问（第二轮应为 0 条）
            assert _run_cli("decide-concern", "--workdir", tmp_path, "--id", row["id"],
                            "--decision", decisions_map[row["id"]], "--reason", "用户当场决定",
                            "--json", tmp_path / f"dec-{tag}-{row['id']}.json").returncode == 0
        assert _run_cli("minutes-reader", "--workdir", tmp_path, "--merged", merged,
                        "--json", tmp_path / f"minutes-{tag}.json").returncode == 0
        gpath = tmp_path / f"guidance-{tag}.json"
        assert _run_cli("reader-guidance", "--workdir", tmp_path, "--merged", merged,
                        "--json", gpath).returncode == 0
        return str(merged), json.loads(gpath.read_text("utf-8"))["guidance"], open_ids, asked

    _, guide1, open1, asked1 = round_trip("r1", ["开头太慢", "结尾太急"],
                                          {"RC1": "接受", "RC2": "驳回"})
    assert len(asked1) == 2 and "开头太慢" in guide1 and "结尾太急" not in guide1

    _, guide2, open2, asked2 = round_trip("r2", ["开头太慢", "结尾太急"], {})
    assert open2 == [] and asked2 == []          # 第二轮不再重问
    assert guide2 == guide1                      # 但发给写手的指导一字不差地重建出来
    assert "开头太慢" in guide2
