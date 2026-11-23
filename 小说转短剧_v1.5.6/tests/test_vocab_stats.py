# -*- coding: utf-8 -*-
import json
import os
import sys
import pytest

from dramacraft.vocab_stats import generate_vocab_report, load_hits

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_vocab_stats_ranking_and_zero_hits(tmp_path):
    hits_file = tmp_path / "vocab_hits.jsonl"
    sample_hits = [
        {"ts": "2026-09-21T10:00:00+08:00", "gate": "literature-sickness", "term": "若有所思", "ep": 1, "scene": "S01", "snippet": "他若有所思地望向窗外"},
        {"ts": "2026-09-21T10:00:01+08:00", "gate": "literature-sickness", "term": "若有所思", "ep": 2, "scene": "S01", "snippet": "若有所思地喝茶"},
        {"ts": "2026-09-21T10:00:02+08:00", "gate": "literature-sickness", "term": "若有所思", "ep": 3, "scene": "S02", "snippet": "依然若有所思"},
        {"ts": "2026-09-21T10:00:03+08:00", "gate": "literature-sickness", "term": "神色复杂", "ep": 1, "scene": "S01", "snippet": "神色复杂地看着她"},
        {"ts": "2026-09-21T10:00:04+08:00", "gate": "ai-tone-removal", "pattern": "不是…而是…", "ep": 1, "scene": "S01", "snippet": "这不是巧合而是"},
    ]
    with open(hits_file, "w", encoding="utf-8") as fp:
        for h in sample_hits:
            fp.write(json.dumps(h, ensure_ascii=False) + "\n")

    report = generate_vocab_report(str(tmp_path))
    assert "# 词表命中分析报告" in report
    assert "高频命中 Top 10" in report
    assert "若有所思" in report
    assert "神色复杂" in report
    # 验证排序："若有所思" (3次) 应该排在 "神色复杂" (1次) 之前
    pos_ruo = report.find("若有所思")
    pos_shen = report.find("神色复杂")
    assert pos_ruo < pos_shen

    assert "零命中清单" in report
    # 词表中有很多词（如"嘴角勾起"等）未命中，应出现在零命中清单中
    assert "未命中文学病词" in report or "未命中" in report


def test_vocab_stats_cli(tmp_path, capsys, monkeypatch):
    import dramacraft.vocab_stats as vs

    ws = tmp_path / "ws"
    state_dir = ws / "state"
    state_dir.mkdir(parents=True)
    hits_file = state_dir / "vocab_hits.jsonl"
    hits_file.write_text(json.dumps({
        "ts": "2026-09-21T10:00:00+08:00",
        "gate": "literature-sickness",
        "term": "若有所思",
        "ep": 1,
        "scene": "S01",
        "snippet": "测试样例"
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["vocab_stats.py", str(ws)])
    vs.main()

    out = capsys.readouterr().out
    assert "高频命中 Top 10" in out
    assert "若有所思" in out
