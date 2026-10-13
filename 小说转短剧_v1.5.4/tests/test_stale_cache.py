# -*- coding: utf-8 -*-
import json
import os
import sys
import pytest

import run_batch
from dramacraft.outline_lint import compute_outline_hash
from dramacraft.ledger import load_events

OUTLINE_PATH = os.path.join("fixtures", "sample_outline.json")


def _seed_episode_cache(ep_dir, ep_num, meta=None):
    ep_data = {
        "ep": ep_num,
        "scenes": [
            {
                "scene": "S01",
                "heading": "日内 顾家客厅",
                "characters": ["C01"],
                "beats": [
                    {"type": "action", "beat": "B01", "content": "顾渊端起茶杯，轻轻抿了一口。"},
                    {"type": "line", "speaker": "C01", "content": "事情查得怎么样了？"}
                ]
            }
        ]
    }
    if meta is not None:
        ep_data["_meta"] = meta
    fpath = os.path.join(ep_dir, f"ep{ep_num:03d}.json")
    with open(fpath, "w", encoding="utf-8") as fp:
        json.dump(ep_data, fp, ensure_ascii=False, indent=2)
    return fpath


def test_outline_hash_deterministic():
    with open(OUTLINE_PATH, "r", encoding="utf-8") as fp:
        data1 = json.load(fp)
    h1 = compute_outline_hash(data1)
    assert len(h1) == 12

    # 改变字段插入顺序，哈希必须完全一致
    data2 = dict(reversed(list(data1.items())))
    h2 = compute_outline_hash(data2)
    assert h1 == h2

    # 修改内容，哈希必然不同
    data3 = json.loads(json.dumps(data1))
    data3["title"] = "改名后的短剧"
    h3 = compute_outline_hash(data3)
    assert h1 != h3


def test_stale_cache_ask_exits_code_2(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "ws"
    ep_dir = ws / "state" / "episodes"
    ep_dir.mkdir(parents=True)

    # 播种一个无 _meta 的旧版本缓存（v1.1 风格）
    _seed_episode_cache(str(ep_dir), 1, meta=None)

    monkeypatch.setattr(sys, "argv", [
        "run_batch.py",
        "--outline", OUTLINE_PATH,
        "--target-seconds", "90",
        "--workdir", str(ws),
        "--assemble-only"
    ])

    with pytest.raises(SystemExit) as ex:
        run_batch.main()
    assert ex.value.code == 2

    out = capsys.readouterr().out
    assert "发现" in out and "缓存" in out
    assert "--stale-cache" in out
    assert "redo" in out and "trust" in out


def test_stale_cache_redo_removes_cache_and_regenerates(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ep_dir = ws / "state" / "episodes"
    ep_dir.mkdir(parents=True)

    # 播种一个哈希失配的缓存
    _seed_episode_cache(str(ep_dir), 1, meta={"schemaVersion": 2, "outlineHash": "bad_hash_123"})

    class FakeGateway:
        def __init__(self, *args, **kwargs):
            self.call_count = 0
            self.nodes = []

        def call_llm(self, *args, **kwargs):
            raise RuntimeError("Stop generation")

    monkeypatch.setattr(run_batch, "LLMGateway", FakeGateway)
    monkeypatch.setattr(sys, "argv", [
        "run_batch.py",
        "--outline", OUTLINE_PATH,
        "--target-seconds", "90",
        "--stale-cache", "redo",
        "--workdir", str(ws)
    ])

    with pytest.raises(SystemExit):
        run_batch.main()

    # 验证旧缓存已被清理
    assert not (ep_dir / "ep001.json").exists()


def test_stale_cache_trust_allows_and_records_ledger(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ep_dir = ws / "state" / "episodes"
    ep_dir.mkdir(parents=True)

    # 播种满分 3 集，但均为旧版本 meta（无 outlineHash）
    with open("fixtures/sample_script.json", "r", encoding="utf-8") as fp:
        full = json.load(fp)
    for i, ep in enumerate(full["episodes"], 1):
        ep_copy = dict(ep)
        ep_copy["_meta"] = {"schemaVersion": 1}  # 旧版本
        with open(ep_dir / f"ep{i:03d}.json", "w", encoding="utf-8") as fp:
            json.dump(ep_copy, fp, ensure_ascii=False, indent=2)

    monkeypatch.setattr(sys, "argv", [
        "run_batch.py",
        "--outline", OUTLINE_PATH,
        "--target-seconds", "90",
        "--stale-cache", "trust",
        "--assemble-only",
        "--workdir", str(ws)
    ])

    run_batch.main()

    # 验证输出的 script.json 中不含 _meta 字段
    out_json = ws / "output" / "script.json"
    assert out_json.exists()
    out_data = json.loads(out_json.read_text(encoding="utf-8"))
    for ep in out_data["episodes"]:
        assert "_meta" not in ep, "交付物的 script.json 绝不能携带内部 _meta 键污染下游"

    # 验证 ledger 记录了 trust 行为
    events = load_events(str(ws / "state" / "ledger.jsonl"))
    stale_events = [e for e in events if e.get("event") == "stale_cache"]
    assert len(stale_events) >= 1
    assert stale_events[0].get("action") == "trust"
