# -*- coding: utf-8 -*-
import json
import os
import subprocess
import sys
import pytest

from dramacraft.premise_to_outline import convert_premises_to_outline
from tests.test_premise_to_outline import _create_sample_premises
import run_batch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALIDATOR_PATH = os.path.join(ROOT, "skills_runtime", "dramacraft_patch", "validate.mjs")


def test_anthology_mode_silences_x1_warning(tmp_path):
    # 构造两集人物完全独立的剧本（第1集全为陆凡/赵天成，第2集全为苏晴/林薇薇）
    script_data = {
        "source": "单元剧测试",
        "episodes": [
            {
                "ep": 1,
                "scenes": [
                    {
                        "scene": "S01",
                        "heading": "日内 客厅",
                        "characters": ["C01", "C02"],
                        "flow": [
                            {"type": "action", "action": "陆凡猛地推开大门，反锁门栓。"},
                            {"type": "line", "speaker": "C01", "line": "这里是我的地盘。"}
                        ]
                    }
                ]
            },
            {
                "ep": 2,
                "scenes": [
                    {
                        "scene": "S01",
                        "heading": "夜外 拍卖行",
                        "characters": ["C03", "C04"],
                        "flow": [
                            {"type": "action", "action": "苏晴稳稳端起茶杯，快步向前。"},
                            {"type": "line", "speaker": "C03", "line": "我们重新谈谈。"}
                        ]
                    }
                ]
            }
        ]
    }

    sc_path = tmp_path / "script.json"
    sc_path.write_text(json.dumps(script_data, ensure_ascii=False), encoding="utf-8")

    # 1. 连续剧普通大纲（未带 mode: anthology）：应该触发 X1 告警
    serial_outline = {
        "title": "连续剧",
        "mode": "serial",
        "characters": [
            {"id": "C01", "name": "陆凡", "desire": "d", "obstacle": "o", "voiceStyle": "v"},
            {"id": "C02", "name": "赵天成", "desire": "d", "obstacle": "o", "voiceStyle": "v"},
            {"id": "C03", "name": "苏晴", "desire": "d", "obstacle": "o", "voiceStyle": "v"},
            {"id": "C04", "name": "林薇薇", "desire": "d", "obstacle": "o", "voiceStyle": "v"}
        ],
        "episodes": [
            {"ep": 1, "targetSeconds": 90, "hook": "h", "cliff": "c", "beatsClaimed": ["b1"]},
            {"ep": 2, "targetSeconds": 90, "hook": "h", "cliff": "c", "beatsClaimed": ["b2"]}
        ],
        "beats": [
            {"episode": 1, "type": "b1"},
            {"episode": 2, "type": "b2"}
        ]
    }
    so_path = tmp_path / "serial_outline.json"
    so_path.write_text(json.dumps(serial_outline, ensure_ascii=False), encoding="utf-8")

    res_serial = subprocess.run(["node", VALIDATOR_PATH, str(sc_path), "--outline", str(so_path)], capture_output=True, text=True, encoding="utf-8")
    assert "跨集连续性告警" in res_serial.stdout, "普通连续剧两集人物断档应触发 X1 告警"

    # 2. 单元剧大纲（带 mode: anthology）：X1 告警必须完全静默
    premises = _create_sample_premises()
    anthology_outline = convert_premises_to_outline(premises, title="单元剧合辑")
    ao_path = tmp_path / "anthology_outline.json"
    ao_path.write_text(json.dumps(anthology_outline, ensure_ascii=False), encoding="utf-8")

    res_anthology = subprocess.run(["node", VALIDATOR_PATH, str(sc_path), "--outline", str(ao_path)], capture_output=True, text=True, encoding="utf-8")
    assert "跨集连续性告警" not in res_anthology.stdout, "单元剧模式下 X1 告警必须被自动静默！"


def test_anthology_run_batch_full_flow(tmp_path, monkeypatch):
    premises = _create_sample_premises()
    outline = convert_premises_to_outline(premises, title="都市异闻录")
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")

    ws = tmp_path / "workspace"
    episodes_dir = ws / "state" / "episodes"
    episodes_dir.mkdir(parents=True)

    from dramacraft.outline_lint import compute_outline_hash
    h = compute_outline_hash(outline)

    with open("fixtures/sample_script.json", "r", encoding="utf-8") as fp:
        full_sample = json.load(fp)

    # 播种两集满分独立剧本，人物分别对应第一单元与第二单元
    for i in (1, 2):
        ep_outline = outline["episodes"][i - 1]
        base_ep = dict(full_sample["episodes"][i - 1])
        c_from = ["C01", "C02", "C03"]
        c_to = ["C01", "C02", "C01"] if i == 1 else ["C03", "C04", "C03"]

        # 映射角色编号
        def _remap(obj):
            if isinstance(obj, dict):
                new_o = {}
                for k, v in obj.items():
                    if k == "characters" and isinstance(v, list):
                        new_o[k] = [c_to[c_from.index(x)] if x in c_from else x for x in v]
                    elif k == "speaker" and v in c_from:
                        new_o[k] = c_to[c_from.index(v)]
                    else:
                        new_o[k] = _remap(v)
                return new_o
            elif isinstance(obj, list):
                return [_remap(x) for x in obj]
            return obj

        ep_data = _remap(base_ep)
        ep_data["ep"] = i
        ep_data["targetSeconds"] = 90
        ep_data["hook"] = ep_outline["hook"]
        ep_data["cliff"] = ep_outline["cliff"]
        ep_data["beatsClaimed"] = list(ep_outline["beatsClaimed"])
        ep_data["_meta"] = {"engineVersion": "1.2.5", "schemaVersion": 2, "outlineHash": h}

        with open(episodes_dir / f"ep{i:03d}.json", "w", encoding="utf-8") as fp:
            json.dump(ep_data, fp, ensure_ascii=False, indent=2)

    monkeypatch.setattr(sys, "argv", [
        "run_batch.py",
        "--outline", str(outline_path),
        "--target-seconds", "90",
        "--assemble-only",
        "--workdir", str(ws)
    ])

    run_batch.main()

    out_dir = ws / "output"
    assert (out_dir / "script.md").exists()
    assert (out_dir / "script.json").exists()
    assert (out_dir / "production_log.md").exists()

    # 验证导出的 script.json 继承了 mode: anthology
    script_out = json.loads((out_dir / "script.json").read_text(encoding="utf-8"))
    assert script_out.get("mode") == "anthology"
