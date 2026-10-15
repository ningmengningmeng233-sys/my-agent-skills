# -*- coding: utf-8 -*-
"""CP-01: 真实适配器缺媒体字段必须抛异常，绝不写占位伪字节。"""
import base64
import io
import json
from pathlib import Path

import pytest

from renderplan import adapters as ad
from renderplan.adapters import (
    ArtifactValidationError,
    HttpTtsAdapter,
    HttpVideoAdapter,
)


class _FakeResp:
    def __init__(self, payload: dict):
        self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mock_open(payload):
    def _fake(req, url, timeout):
        return _FakeResp(payload)
    return _fake


def _env(monkeypatch):
    monkeypatch.setenv("RENDER_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("RENDER_KEY", "real-test-token")


def test_video_missing_media_field_raises_and_writes_nothing(monkeypatch, tmp_path):
    """响应缺 video_bytes（如 {}）→ 抛 ArtifactValidationError，不落伪文件。"""
    _env(monkeypatch)
    monkeypatch.setattr(ad, "_open", _mock_open({}))
    clip = tmp_path / "clip.mp4"
    shot = {"shot_id": "E01-01-f1", "duration": 3, "prompt": "real prompt",
            "_clip_path": str(clip)}
    with pytest.raises(ArtifactValidationError):
        HttpVideoAdapter().generate(shot)
    assert not clip.exists(), "缺媒体字段时不得生成任何产物文件"


def test_tts_missing_media_field_raises_and_writes_nothing(monkeypatch, tmp_path):
    """响应缺 audio_bytes（如 {}）→ 抛 ArtifactValidationError，不落伪文件。"""
    _env(monkeypatch)
    monkeypatch.setattr(ad, "_open", _mock_open({}))
    audio = tmp_path / "a.wav"
    shot = {"shot_id": "E01-01-f1", "duration": 3, "dialogue": "你好",
            "_audio_path": str(audio)}
    with pytest.raises(ArtifactValidationError):
        HttpTtsAdapter().synthesize(shot)
    assert not audio.exists(), "缺媒体字段时不得生成任何产物文件"


def test_tts_duration_only_from_server_no_plan_fallback(monkeypatch, tmp_path):
    """有音频字节但缺 duration_seconds → 不得回退计划值，必须抛异常。"""
    _env(monkeypatch)
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x01" * 32
    payload = {"audio_bytes": base64.b64encode(wav).decode("ascii")}
    monkeypatch.setattr(ad, "_open", _mock_open(payload))
    audio = tmp_path / "b.wav"
    shot = {"shot_id": "E01-01-f1", "duration": 7.5, "dialogue": "你好",
            "dialogue_planned_seconds": 7.5, "_audio_path": str(audio)}
    with pytest.raises(ArtifactValidationError):
        HttpTtsAdapter().synthesize(shot)
