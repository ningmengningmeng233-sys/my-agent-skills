# -*- coding: utf-8 -*-
import os
import re
from dramacraft.ledger import Ledger, load_events, summarize

BANNED_JARGON = [".py", ".mjs", "validate", "gateway", "self_healing", "jsonl", "dict", "stdout", "stderr"]


def test_summarize_with_full_events():
    events = [
        {"ts": "2026-09-21T10:00:00+08:00", "event": "run_start", "episodes_total": 3, "cached": 1, "target_seconds": 90, "budget_cap": 25},
        {"ts": "2026-09-21T10:00:01+08:00", "event": "llm_call", "ep": 2, "node": "MiniMax-M2", "ok": True},
        {"ts": "2026-09-21T10:00:02+08:00", "event": "gate_fail", "ep": 2, "attempt": 1, "failed_gates": ["line-length"]},
        {"ts": "2026-09-21T10:00:03+08:00", "event": "llm_call", "ep": 2, "node": "MiniMax-M2", "ok": True},
        {"ts": "2026-09-21T10:00:04+08:00", "event": "episode_generated", "ep": 2, "node": "MiniMax-M2", "attempt": 2, "seconds_elapsed": 3.5},
        {"ts": "2026-09-21T10:00:05+08:00", "event": "episode_cached", "ep": 2, "path": "state/episodes/ep002.json"},
        {"ts": "2026-09-21T10:00:06+08:00", "event": "run_finish", "status": "passed", "episodes": 3, "calls_used": 2}
    ]
    outline = {
        "title": "测试短剧",
        "episodes": [
            {"ep": 1, "targetSeconds": 90},
            {"ep": 2, "targetSeconds": 90},
            {"ep": 3, "targetSeconds": 90}
        ]
    }

    md = summarize(events, outline)
    assert "# 生产记录 —— 《测试短剧》" in md
    # 表头含有集数、结果/状态、写作来源、回炉自愈
    assert "第 1 集" in md
    assert "第 2 集" in md
    assert "MiniMax-M2" in md
    assert "回炉" in md
    assert "引擎账本记录的模型调用 2 次" in md
    assert "预算上限 25" in md

    # 收尾白名单铁律：严禁开发者黑话
    for jargon in BANNED_JARGON:
        assert jargon not in md.lower(), f"生产小结中出现违规黑话: {jargon}"


def test_latest_run_cached_episode_is_not_pending():
    """真跑实测故障（2026-09-23 closure-fix-ws）：已在库并已导出的第 1 集被写成「⏸ 待生成」。

    旧实现取**最早**一次 run_start 推断缓存状态，那次运行时这一集还没生成，于是当前状态被误判。
    """
    from dramacraft.ledger import summarize
    events = [
        {"event": "run_start", "cached": 0},
        {"event": "run_finish", "status": "missing_episodes"},
        {"event": "run_start", "cached": 1},
        {"event": "run_finish", "status": "passed", "episodes": 1},
    ]
    md = summarize(events, {"title": "测试剧", "episodes": [{"ep": 1}]})
    assert "第 1 集 | ✅ 已" in md
    assert "第 1 集 | ⏸ 待生成" not in md


def test_only_second_episode_in_library_does_not_mark_first_ready():
    """cached=N 不是「前 N 集就绪」：只缓存第 2 集时，第 1 集仍是待生成。"""
    from dramacraft.ledger import summarize
    events = [
        {"event": "run_start", "cached": 1},
        {"event": "episode_cached", "ep": 2, "path": "state/episodes/ep002.json"},
        {"event": "run_finish", "status": "missing_episodes", "missing_eps": [1]},
    ]
    md = summarize(events, {"title": "测试剧", "episodes": [{"ep": 1}, {"ep": 2}]})
    assert "第 2 集 | ✅ 已验收在库" in md
    assert "第 1 集 | ⏸ 待生成" in md


def test_repeat_assembly_keeps_verified_status():
    """同一工作区重复装配：第二次同样由逐集事件判定，不因事件累积而失真。"""
    from dramacraft.ledger import summarize
    events = [
        {"event": "run_start", "cached": 1},
        {"event": "episode_cached", "ep": 1, "path": "state/episodes/ep001.json"},
        {"event": "run_finish", "status": "passed", "episodes": 1},
        {"event": "run_start", "cached": 1},
        {"event": "episode_cached", "ep": 1, "path": "state/episodes/ep001.json"},
        {"event": "run_finish", "status": "passed", "episodes": 1},
    ]
    md = summarize(events, {"title": "测试剧", "episodes": [{"ep": 1}]})
    assert md.count("第 1 集 |") == 1
    assert "第 1 集 | ✅ 已验收在库" in md


def test_old_failed_run_does_not_poison_later_success():
    """上一轮装配失败、本轮成功：状态取本轮，不能被历史的 gate_failed 拖成「生成中断」。"""
    from dramacraft.ledger import summarize
    events = [
        {"event": "run_start", "cached": 0},
        {"event": "gate_fail", "ep": 1, "attempt": 1, "failed_gates": ["line-length"]},
        {"event": "run_finish", "status": "gate_failed", "failed_items": 1},
        {"event": "run_start", "cached": 1},
        {"event": "episode_cached", "ep": 1, "path": "state/episodes/ep001.json"},
        {"event": "run_finish", "status": "passed", "episodes": 1},
    ]
    md = summarize(events, {"title": "测试剧", "episodes": [{"ep": 1}]})
    assert "第 1 集 | ✅ 已验收在库" in md
    assert "❌ 生成中断" not in md


def test_stale_or_blocked_episode_is_not_reported_as_pending():
    """在库旧件被安检拒收、以及回炉未收敛的集，都不能写成「待生成」（那是把未知当没动工）。"""
    from dramacraft.ledger import summarize
    stale = [
        {"event": "run_start", "cached": 0},
        {"event": "stale_cache", "ep": 1, "reason": "缺少 _meta 元数据（旧版本缓存）", "action": "ask"},
    ]
    md = summarize(stale, {"title": "测试剧", "episodes": [{"ep": 1}]})
    assert "第 1 集 | ⚠ 需处置" in md
    assert "待生成" not in md

    blocked = [
        {"event": "run_start", "cached": 0},
        {"event": "episode_undelivered", "ep": 1, "verdict": "escalate", "rounds": 2,
         "detail_digest": "第 3 场时长不够"},
        {"event": "run_finish", "status": "partial", "blocked_eps": [1], "ready_eps": 0},
    ]
    md2 = summarize(blocked, {"title": "测试剧", "episodes": [{"ep": 1}]})
    assert "第 1 集 | ❌ 未交付" in md2
    assert "待生成" not in md2


def test_workflow_written_episode_reports_honest_provenance():
    """工作流写手写在外面的件：引擎没有生成记录 → 来源栏写「来源未记录」，不许猜执笔模型；
    汇总里的调用次数必须写明口径（引擎账本口径，ZCode 子代理调用不在其中），
    不能出现能被读成「本剧总模型调用为 0」的句子。"""
    from dramacraft.ledger import summarize
    events = [
        {"event": "run_start", "cached": 1},
        {"event": "episode_cached", "ep": 1, "verified": "cache_check"},
        {"event": "run_finish", "status": "passed", "episodes": 1},
    ]
    md = summarize(events, {"title": "测试剧", "episodes": [{"ep": 1}]})
    assert "来源未记录" in md
    assert "引擎账本记录的模型调用 0 次" in md
    assert "ZCode 子代理的调用不在这个数里" in md
    assert "本剧累计调用模型 0 次" not in md


def test_external_writer_provenance_when_explicitly_recorded():
    """有明确外部写入事件时按事件写来源；事件里没写谁写的就只写「外部入库」。"""
    from dramacraft.ledger import summarize
    base = [
        {"event": "run_start", "cached": 1},
        {"event": "episode_cached", "ep": 1, "verified": "cache_check"},
        {"event": "run_finish", "status": "passed", "episodes": 1},
    ]
    outline = {"title": "测试剧", "episodes": [{"ep": 1}]}
    md = summarize(base + [{"event": "episode_imported", "ep": 1, "by": "工作流写手"}], outline)
    assert "外部入库（工作流写手）" in md
    md2 = summarize(base + [{"event": "episode_imported", "ep": 1}], outline)
    assert "外部入库" in md2
    assert "来源未记录" not in md2


def test_engine_generation_keeps_recorded_model():
    """引擎自己生成的路径保持不变：仍写引擎账本记录的执笔模型。"""
    from dramacraft.ledger import summarize
    events = [
        {"event": "run_start", "cached": 0},
        {"event": "episode_generated", "ep": 1, "node": "MiniMax-M2", "attempt": 1},
        {"event": "episode_cached", "ep": 1, "path": "state/episodes/ep001.json"},
        {"event": "run_finish", "status": "passed", "episodes": 1},
    ]
    md = summarize(events, {"title": "测试剧", "episodes": [{"ep": 1}]})
    assert "MiniMax-M2" in md
    assert "来源未记录" not in md


def test_summarize_budget_stop_status():
    events = [
        {"ts": "2026-09-21T10:00:00+08:00", "event": "run_start", "episodes_total": 2, "cached": 0, "target_seconds": 90, "budget_cap": 2},
        {"ts": "2026-09-21T10:00:01+08:00", "event": "llm_call", "ep": 1, "node": "Node1", "ok": False, "error": "429 Too Many Requests"},
        {"ts": "2026-09-21T10:00:02+08:00", "event": "llm_call", "ep": 1, "node": "Node2", "ok": True},
        {"ts": "2026-09-21T10:00:03+08:00", "event": "budget_stop", "calls_used": 2, "cap": 2},
        {"ts": "2026-09-21T10:00:04+08:00", "event": "run_finish", "status": "budget", "calls_used": 2, "cap": 2}
    ]
    outline = {
        "title": "熔断短剧",
        "episodes": [
            {"ep": 1, "targetSeconds": 90},
            {"ep": 2, "targetSeconds": 90}
        ]
    }
    md = summarize(events, outline)
    assert "熔断短剧" in md
    assert "预算熔断" in md or "中止" in md or "暂停" in md
    assert "引擎账本记录的模型调用 2 次" in md
    for jargon in BANNED_JARGON:
        assert jargon not in md.lower(), f"生产小结中出现违规黑话: {jargon}"


def test_run_batch_creates_production_log_on_finish(tmp_path, monkeypatch):
    import json
    import sys
    import run_batch

    ws = tmp_path / "ws"
    ep_dir = ws / "state" / "episodes"
    ep_dir.mkdir(parents=True)

    # 装载满分样例的 3 集
    with open("fixtures/sample_script.json", "r", encoding="utf-8") as fp:
        full_script = json.load(fp)
    outline = json.load(open("fixtures/sample_outline.json", "r", encoding="utf-8"))
    from dramacraft.outline_lint import compute_outline_hash
    h = compute_outline_hash(outline)
    for i, ep in enumerate(full_script["episodes"], 1):
        ep_c = dict(ep)
        ep_c["_meta"] = {"engineVersion": "1.2.0", "schemaVersion": 2, "outlineHash": h}
        with open(ep_dir / f"ep{i:03d}.json", "w", encoding="utf-8") as fp:
            json.dump(ep_c, fp, ensure_ascii=False, indent=2)

    monkeypatch.setattr(sys, "argv", [
        "run_batch.py",
        "--outline", "fixtures/sample_outline.json",
        "--target-seconds", "90",
        "--assemble-only",
        "--workdir", str(ws)
    ])

    run_batch.main()

    log_file = ws / "output" / "production_log.md"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "# 生产记录 —— 《战神归来之龙帅临门》" in content
    assert "第 1 集" in content
    assert "第 2 集" in content
    assert "第 3 集" in content
    for jargon in BANNED_JARGON:
        assert jargon not in content.lower(), f"生成的 production_log.md 含黑话: {jargon}"

    # 装配的逐集安检结果必须入账（v1.5.1 T1）：生产记录据此判定「已验收在库」，
    # 而不是靠 cached=N 推断「前 N 集就绪」。
    lines = [re.search(r"\{.*\}", ln).group(0)
             for ln in (ws / "state" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
             if ln.strip().startswith("{")]
    cached_eps = [json.loads(ln).get("ep") for ln in lines if json.loads(ln).get("event") == "episode_cached"]
    assert sorted(cached_eps) == [1, 2, 3]

