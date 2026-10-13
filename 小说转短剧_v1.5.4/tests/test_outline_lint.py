import copy
import json
import os
import subprocess

from dramacraft.outline_lint import lint_outline
from conftest import run_py

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _good_outline():
    return json.load(open(os.path.join(ROOT, "fixtures", "sample_outline.json"), encoding="utf-8"))


def test_sample_outline_passes_all_six_gates():
    assert lint_outline(_good_outline()) == []


def test_lint_catches_each_violation_class():
    bad = _good_outline()
    bad["title"] = ""                                  # O1
    bad["characters"][1]["name"] = ""                  # O2 缺字段
    bad["characters"].append(dict(bad["characters"][0]))  # O2 id 重复
    bad["episodes"][1]["ep"] = 1                       # O3 集号重复
    bad["episodes"][2]["targetSeconds"] = 45           # O3 时长不统一
    bad["episodes"][0]["cliff"] = "  "                 # O4
    bad["beats"].append({"episode": 99, "type": "天降横财"})  # O5 孤儿节拍
    bad["episodes"][0]["beatsClaimed"] = ["身份初显", "凭空加戏"]  # O6 漏认领+虚报

    report = lint_outline(bad)
    joined = "\n".join(report)
    assert "[O1]" in joined
    assert "[O2]" in joined and "缺 name" in joined and "重复" in joined
    assert "[O3]" in joined and "集号重复" in joined and "全剧统一" in joined
    assert "[O4]" in joined
    assert "[O5]" in joined and "孤儿节拍" in joined
    assert "[O6]" in joined and "漏认领" in joined and "虚报" in joined


def test_cli_module_mode_exits_nonzero_on_dirty_outline(tmp_path):
    bad = copy.deepcopy(_good_outline())
    bad["episodes"][0]["hook"] = ""
    p = tmp_path / "dirty.json"
    p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    res = run_py(["-m", "dramacraft.outline_lint", str(p)], cwd=ROOT)
    assert res.returncode != 0
    assert "[O4]" in res.stdout
