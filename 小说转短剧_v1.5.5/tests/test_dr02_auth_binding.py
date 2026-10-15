"""DR-02 受控 RED/GREEN：质量门绑定权威集号与时长，禁信模型自报。

PROVE_BEFORE_FIX 证据（修复前实测，生产入口=run_quality_gate 12 门全量）：
- BASELINE 合法：生产 passed=True，独立 validateScript problems=0
- A 缺失 targetSeconds：生产 passed=True（错误签发），独立 problems=3（ catches）
- B 篡改 90→80：生产 passed=True（错误签发），独立 problems=0（同信自报）
- C 篡改 ep 1→99：生产 passed=True（错误签发），独立 problems=0（同信自报）
结论：真实生产入口因信任模型自报字段而错误签发，需引擎强制绑定权威值。
"""
import json
import os
import subprocess
import sys
from unittest import mock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dramacraft.self_healing import generate_episode_with_healing, run_quality_gate  # noqa: E402


def _load(name):
    from dramacraft.ingestor import _read_file_safe
    return json.loads(_read_file_safe(os.path.join("fixtures", name)))


def test_legal_all_green():
    script = _load("sample_script.json")
    outline = _load("sample_outline.json")
    passed, _, failed = run_quality_gate(script, outline)
    assert passed is True and not failed
    passed2, _, failed2 = run_quality_gate(
        script, outline, expected_target_seconds=90
    )
    assert passed2 is True and not failed2


def test_missing_target_blocked():
    script = _load("sample_script.json")
    outline = _load("sample_outline.json")
    bad = json.loads(json.dumps(script))
    for ep in bad["episodes"]:
        ep.pop("targetSeconds", None)
    passed, _, failed = run_quality_gate(bad, outline)
    assert passed is False, "缺失 targetSeconds 应判硬伤"
    assert any("targetSeconds" in f for f in failed)
    passed2, _, _ = run_quality_gate(bad, outline, expected_target_seconds=90)
    assert passed2 is False


def test_tampered_target_blocked():
    script = _load("sample_script.json")
    outline = _load("sample_outline.json")
    bad = json.loads(json.dumps(script))
    for ep in bad["episodes"]:
        ep["targetSeconds"] = 80
    passed, _, failed = run_quality_gate(bad, outline)
    assert passed is False, "篡改 targetSeconds 90→80 应判硬伤"
    assert any("不一致" in f or "权威" in f for f in failed)
    passed2, _, failed2 = run_quality_gate(bad, outline, expected_target_seconds=90)
    assert passed2 is False
    assert any("不一致" in f or "权威" in f for f in failed2)


def test_tampered_ep_blocked():
    script = _load("sample_script.json")
    outline = _load("sample_outline.json")
    bad = {"source": script["source"], "episodes": [json.loads(json.dumps(script["episodes"][0]))]}
    bad["episodes"][0]["ep"] = 99
    bad["episodes"][0]["beatsClaimed"] = []
    passed, _, failed = run_quality_gate(bad, outline)
    assert passed is False, "篡改 ep 1→99 应判硬伤"
    assert any("99" in f or "大纲" in f or "集号" in f for f in failed)


def test_healing_path_rejects_tampered_then_accepts_legal():
    outline = _load("sample_outline.json")
    legal_ep = json.loads(json.dumps(_load("sample_script.json")["episodes"][0]))
    tampered = json.loads(json.dumps(legal_ep))
    tampered["targetSeconds"] = 80
    gw = mock.MagicMock()
    gw.call_llm.side_effect = [
        json.dumps(tampered, ensure_ascii=False),
        json.dumps(legal_ep, ensure_ascii=False),
    ]
    result = generate_episode_with_healing(
        ep_meta={"ep": 1, "targetSeconds": 90},
        gateway=gw,
        outline_data=outline,
        max_retries=2,
    )
    assert gw.call_llm.call_count == 2, "篡改首轮应触发回炉而非直接签发"
    assert result["targetSeconds"] == 90


def test_mjs_duration_requires_target(tmp_path):
    script = _load("sample_script.json")
    bad = json.loads(json.dumps(script))
    for ep in bad["episodes"]:
        ep.pop("targetSeconds", None)
    sc = tmp_path / "bad.json"
    sc.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "rep.json"
    validator = os.path.join(ROOT, "skills_runtime", "dramacraft_patch", "validate.mjs")
    res = subprocess.run(
        ["node", validator, str(sc), "--json", str(out)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert res.returncode != 0, "mjs 时长门缺 targetSeconds 应失败（fail-closed）"
