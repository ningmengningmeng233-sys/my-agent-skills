# -*- coding: utf-8 -*-
"""CP-02: 真实工单送入适配器时，请求必须携带非空 prompt 与正确台词字符串。"""
import base64
import io
import json
from pathlib import Path

import pytest

from renderplan import adapters as ad
from renderplan.adapters import HttpTtsAdapter, HttpVideoAdapter
from renderplan.plan import build_plan, load_json


class _FakeResp:
    def __init__(self, payload: dict):
        self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _env(monkeypatch):
    monkeypatch.setenv("RENDER_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("RENDER_KEY", "real-test-token")


def _video_ok():
    raw = b"\x00\x00\x00 ftypisom" + b"\x01" * 16
    return {"video_bytes": base64.b64encode(raw).decode("ascii")}


def _audio_ok(dur=2.5):
    raw = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x01" * 32
    return {"audio_bytes": base64.b64encode(raw).decode("ascii"),
            "duration_seconds": dur}


def test_video_request_carries_real_prompt(monkeypatch, tmp_path, storyboard_dict,
                                           dubbing_path, asset_ledger_path):
    """真实 build_plan 输出送视频适配器：捕获请求 prompt 非空且等于分镜 h3Prompt。"""
    _env(monkeypatch)
    plan = build_plan(storyboard_dict, dubbing=load_json(dubbing_path),
                      asset_ledger=load_json(asset_ledger_path))
    expected = storyboard_dict["episodes"][0]["segments"][0]["h3Prompt"]
    assert expected, "夹具 h3Prompt 应非空"
    captured = {}

    def _fake(req, url, timeout):
        captured.update(json.loads(req.data.decode("utf-8")))
        return _FakeResp(_video_ok())

    monkeypatch.setattr(ad, "_open", _fake)
    shot = dict(plan["shots"][0])
    shot["_clip_path"] = str(tmp_path / "c.mp4")
    HttpVideoAdapter().generate(shot)
    assert isinstance(captured.get("prompt"), str) and captured["prompt"].strip(), \
        f"视频请求 prompt 为空：{captured!r}"
    assert captured["prompt"] == expected


def test_tts_request_carries_real_dialogue(monkeypatch, tmp_path, storyboard_dict,
                                           dubbing_path, asset_ledger_path):
    """真实 build_plan 输出送 TTS 适配器：捕获请求 dialogue 为正确台词字符串。"""
    _env(monkeypatch)
    plan = build_plan(storyboard_dict, dubbing=load_json(dubbing_path),
                      asset_ledger=load_json(asset_ledger_path))
    dub = load_json(dubbing_path)
    expected_line = dub["alignments"][0]["line"]
    captured = {}

    def _fake(req, url, timeout):
        captured.update(json.loads(req.data.decode("utf-8")))
        return _FakeResp(_audio_ok())

    monkeypatch.setattr(ad, "_open", _fake)
    shot = dict(plan["shots"][0])
    shot["_audio_path"] = str(tmp_path / "a.wav")
    HttpTtsAdapter().synthesize(shot)
    assert isinstance(captured.get("dialogue"), str) and captured["dialogue"].strip(), \
        f"TTS 请求 dialogue 非字符串或为空：{captured!r}"
    assert captured["dialogue"] == expected_line


def test_video_adapter_rejects_empty_prompt_without_http(monkeypatch, tmp_path):
    """prompt 为空时拒绝发送：抛错且不触达 HTTP。"""
    _env(monkeypatch)
    calls = []

    def _fake(req, url, timeout):
        calls.append(url)
        return _FakeResp(_video_ok())

    monkeypatch.setattr(ad, "_open", _fake)
    shot = {"shot_id": "E01-01-f1", "duration": 3, "prompt": "",
            "_clip_path": str(tmp_path / "c.mp4")}
    with pytest.raises(ValueError, match="prompt"):
        HttpVideoAdapter().generate(shot)
    assert calls == [], "空 prompt 不得发送 HTTP 请求"


def test_tts_adapter_rejects_empty_dialogue_without_http(monkeypatch, tmp_path):
    """台词为空（空串/缺失/布尔）时拒绝发送：抛错且不触达 HTTP。"""
    _env(monkeypatch)
    calls = []

    def _fake(req, url, timeout):
        calls.append(url)
        return _FakeResp(_audio_ok())

    monkeypatch.setattr(ad, "_open", _fake)
    for bad in ("", None, True):
        shot = {"shot_id": "E01-01-f1", "duration": 3, "dialogue": bad,
                "_audio_path": str(tmp_path / "a.wav")}
        with pytest.raises(ValueError, match="[Dd]ialogue|台词"):
            HttpTtsAdapter().synthesize(shot)
    assert calls == [], "空台词不得发送 HTTP 请求"
