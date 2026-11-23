"""DR-01 受控 RED/GREEN：worker 主路径与导出器不得静默吞掉缺 type 拍。"""
import json
import os
import sys
from unittest import mock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dramacraft.exporter import export_to_csv, export_to_markdown  # noqa: E402


def _outline():
    return json.load(open(os.path.join(ROOT, "fixtures", "sample_outline.json"), encoding="utf-8"))


def _broken_ep():
    return {
        "ep": 1,
        "targetSeconds": 90,
        "hook": "钩子",
        "cliff": "悬念",
        "hookBeat": [1, 1],
        "beatsClaimed": [],
        "scenes": [
            {
                "sceneId": "S01",
                "location": "顾家大厅·夜",
                "characters": ["C01"],
                "flow": [
                    {"action": "无类型拍子应被拦截"},
                ],
            }
        ],
    }


def _legal_script():
    return json.load(open(os.path.join(ROOT, "fixtures", "sample_script.json"), encoding="utf-8"))


def test_exporter_rejects_missing_beat_type():
    script = {"source": "t", "episodes": [_broken_ep()]}
    outline = _outline()
    with pytest.raises(ValueError, match="type"):
        export_to_markdown(script, outline)
    with pytest.raises(ValueError, match="type"):
        export_to_csv(script, outline)


def test_exporter_accepts_legal_beats():
    script = _legal_script()
    outline = _outline()
    md = export_to_markdown(script, outline)
    csv_text = export_to_csv(script, outline)
    assert "第 01 集" in md
    assert "E01" in csv_text


def test_worker_path_rejects_missing_beat_type(tmp_path, monkeypatch):
    import run_batch

    outline = _outline()
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    workdir = tmp_path / "ws"

    broken = _broken_ep()

    def _fake_generate(**kwargs):
        return json.loads(json.dumps(broken))

    class _FakeGateway:
        def __init__(self, *a, **k):
            self.nodes = []
            self.max_calls = None
            self.call_count = 0
            self.current_ep = None
            self.ledger = None

    monkeypatch.setattr(run_batch, "check_node_available", lambda: (True, ""))
    monkeypatch.setattr(run_batch, "LLMGateway", _FakeGateway)
    monkeypatch.setattr(run_batch, "generate_episode_with_healing", _fake_generate)
    monkeypatch.setattr(run_batch, "run_quality_gate", lambda *a, **k: (True, "mock pass", []))
    monkeypatch.setattr(run_batch, "final_foreshadow_sweep", lambda *a, **k: [])
    monkeypatch.setattr(run_batch, "final_naming_sweep", lambda *a, **k: [])
    monkeypatch.setattr(run_batch, "export_all", lambda **k: {})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_batch.py",
            "--outline", str(outline_path),
            "--target-seconds", "90",
            "--workdir", str(workdir),
            "--concurrency", "1",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        run_batch.main()
    assert exc.value.code != 0, "worker 主路径放行了缺 type 破损数据（应非零拦截）"
