import json
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALIDATOR = os.path.join(ROOT, "skills_runtime", "dramacraft_patch", "validate.mjs")


def _node(args):
    return subprocess.run(["node", VALIDATOR] + args,
                          capture_output=True, text=True, encoding="utf-8", cwd=ROOT)


def test_art_gate_actually_audits_when_provided():
    # v1.1.0 前门10是死门：--art 参数根本没人收。现在给了 art 必须实扫且不跳过
    res = _node([
        os.path.join("fixtures", "sample_script.json"),
        "--outline", os.path.join("fixtures", "sample_outline.json"),
        "--art", os.path.join("fixtures", "sample_art.json"),
    ])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "本门跳过" not in res.stdout


def test_art_gate_catches_scene_not_registered(tmp_path):
    doc = json.load(open(os.path.join(ROOT, "fixtures", "sample_script.json"), encoding="utf-8"))
    doc["episodes"][0]["scenes"][0]["sceneId"] = "S99"
    p = tmp_path / "bad_scene.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    res = _node([str(p), "--art", os.path.join(ROOT, "fixtures", "sample_art.json")])
    assert res.returncode == 1
    assert "S99" in res.stdout


def test_cross_episode_zero_overlap_warns_without_blocking(tmp_path):
    doc = {
        "source": "告警测试",
        "episodes": [
            {"ep": 1, "scenes": [{"sceneId": "S01", "characters": ["C01"],
                                  "flow": [{"type": "action", "action": "甲摔门而去。"}]}]},
            {"ep": 2, "scenes": [{"sceneId": "S02", "characters": ["C02"],
                                  "flow": [{"type": "action", "action": "乙独自浇花。"}]}]},
        ],
    }
    p = tmp_path / "disjoint.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    res = _node([str(p)])
    assert "跨集连续性告警" in res.stdout
    assert "零交集" in res.stdout
    # 告警不新增门数：报告仍是 12 道门（此文档因缺 hook 等本就不过门，退出码与告警无关）
    assert "12 道确定性质量门" in res.stdout
