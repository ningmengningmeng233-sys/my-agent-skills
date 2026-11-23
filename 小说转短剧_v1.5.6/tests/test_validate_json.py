# -*- coding: utf-8 -*-
import json
import os
import subprocess
import tempfile
import pytest

from dramacraft.self_healing import run_quality_gate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALIDATOR_PATH = os.path.join(ROOT, "skills_runtime", "dramacraft_patch", "validate.mjs")


def test_validate_cli_outputs_structured_json(tmp_path):
    # 构造一个含 G11 文学病与 G12 AI 腔违规的测试剧本
    bad_script = {
        "source": "测试剧本",
        "episodes": [
            {
                "ep": 1,
                "scenes": [
                    {
                        "scene": "S01",
                        "heading": "日内 书房",
                        "characters": ["C01"],
                        "flow": [
                            {"type": "action", "action": "他若有所思地望向窗外，神色复杂。"},
                            {"type": "line", "speaker": "C01", "line": "这不是巧合，而是蓄谋已久的报复。"}
                        ]
                    }
                ]
            }
        ]
    }
    sc_path = tmp_path / "bad_script.json"
    sc_path.write_text(json.dumps(bad_script, ensure_ascii=False), encoding="utf-8")
    json_out = tmp_path / "report.json"

    cmd = ["node", VALIDATOR_PATH, str(sc_path), "--json", str(json_out)]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert res.returncode != 0
    assert json_out.exists()

    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert "gates" in data and len(data["gates"]) == 12
    assert "hits" in data

    # 验证 G11 和 G12 命中了词与模式
    g11 = next(g for g in data["gates"] if g["id"] == "literature-sickness")
    assert not g11["ok"]
    assert "若有所思" in g11["detail"]

    g12 = next(g for g in data["gates"] if g["id"] == "ai-tone-removal")
    assert not g12["ok"]
    assert "命中" in g12["detail"]

    # 验证 hits 结构
    hit_terms = [h.get("term") for h in data["hits"] if "term" in h]
    assert "若有所思" in hit_terms
    hit_patterns = [h.get("pattern") for h in data["hits"] if "pattern" in h]
    assert any("不是" in str(p) for p in hit_patterns)
    for h in data["hits"]:
        assert h.get("ep") == 1
        assert "snippet" in h


def test_self_healing_run_quality_gate_uses_json(tmp_path):
    # 构造违规剧本，验证 run_quality_gate 提取出清晰的 failed_items
    bad_script = {
        "source": "测试剧本",
        "episodes": [
            {
                "ep": 1,
                "scenes": [
                    {
                        "scene": "S01",
                        "heading": "日内 书房",
                        "characters": ["C01"],
                        "flow": [
                            {"type": "action", "action": "他若有所思地望着对方。"}
                        ]
                    }
                ]
            }
        ]
    }
    passed, report, failed_items = run_quality_gate(bad_script)
    assert not passed
    assert any("若有所思" in item for item in failed_items)
