import pytest
from unittest.mock import MagicMock

from dramacraft.self_healing import (
    run_quality_gate,
    build_healing_prompt,
    generate_episode_with_healing
)


def test_run_quality_gate_on_good_script():
    from dramacraft.ingestor import _read_file_safe
    import json
    good_script = json.loads(_read_file_safe(r"fixtures\sample_script.json"))
    good_outline = json.loads(_read_file_safe(r"fixtures\sample_outline.json"))

    passed, report, failed_items = run_quality_gate(good_script, outline_data=good_outline)
    assert passed is True
    assert len(failed_items) == 0


def test_run_quality_gate_on_bad_script():
    bad_script = {
        "source": "测试违规",
        "episodes": [
            {
                "ep": 1,
                "targetSeconds": 60,
                "hook": "开场钩子",
                "hookBeat": [1, 1],
                "cliff": "结尾悬念",
                "beatsClaimed": ["爽点"],
                "scenes": [
                    {
                        "sceneId": "S01",
                        "characters": ["C01"],
                        "flow": [
                            {
                                "type": "action",
                                "action": "主角若有所思地坐下。"
                            }
                        ]
                    }
                ]
            }
        ]
    }
    passed, report, failed_items = run_quality_gate(bad_script)
    assert passed is False
    assert any("若有所思" in item for item in failed_items)

    healing_prompt = build_healing_prompt(bad_script["episodes"][0], failed_items)
    assert "若有所思" in healing_prompt
    assert "修改指令" in healing_prompt


def test_generate_episode_with_healing_success():
    # 模拟第一次生成违规，第二次生成合规的自愈全流程
    bad_ep = {
        "ep": 1,
        "targetSeconds": 90,
        "hook": "开场",
        "hookBeat": [1, 1],
        "cliff": "结尾",
        "beatsClaimed": ["爽点"],
        "scenes": [{"sceneId": "S01", "characters": ["C01"], "flow": [{"type": "action", "action": "若有所思"}]}]
    }

    import json
    from dramacraft.ingestor import _read_file_safe
    good_ep = json.loads(_read_file_safe(r"fixtures\sample_script.json"))["episodes"][0]
    good_outline = json.loads(_read_file_safe(r"fixtures\sample_outline.json"))

    mock_gateway = MagicMock()
    # 第一次返回 bad_ep 的 json 字符串，第二次返回 good_ep 的 json 字符串
    mock_gateway.call_llm.side_effect = [
        json.dumps(bad_ep, ensure_ascii=False),
        json.dumps(good_ep, ensure_ascii=False)
    ]

    ep_result = generate_episode_with_healing(
        ep_meta={"ep": 1, "targetSeconds": 90},
        gateway=mock_gateway,
        outline_data=good_outline,
        max_retries=2
    )

    assert ep_result is not None
    assert mock_gateway.call_llm.call_count == 2


def test_cli_reports_unreadable_inputs_on_stderr(capsys, tmp_path):
    """`python -m dramacraft.self_healing` 的失败出口本身不得再炸（曾经漏 `import sys` → NameError）。

    这个入口是文档钦定的单集复核命令；出错路径没人跑过，就没人知道它抛的是白话还是栈。
    """
    from dramacraft.self_healing import _cli_main

    report = tmp_path / "不该被写出的报告.json"
    with pytest.raises(SystemExit) as e:
        _cli_main(["--ep", str(tmp_path / "不存在的单集件.json"), "--json", str(report)])
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "读不了" in err and "Traceback" not in err
    assert not report.exists()
