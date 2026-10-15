# -*- coding: utf-8 -*-
"""CP-04: 产物先写 .part 临时文件、经 validate_rendered_artifact 校验后原子替换；
断点续跑必须校验文件完整性，0 字节/截断文件不得判为 cache hit，必须重新生成或拦截。

反例（修前）：适配器直写最终路径；续跑只看 exists()，半截文件被当完成。
"""
import base64
import io
import json
from pathlib import Path

from renderplan import adapters as ad
from renderplan.adapters import ArtifactValidationError, HttpVideoAdapter
from renderplan.plan import build_plan, load_json
from renderplan.stages import run_tts_stage, run_video_stage


def _plan(storyboard_dict, dubbing_path, asset_ledger_path):
    return build_plan(storyboard_dict, dubbing=load_json(dubbing_path),
                      asset_ledger=load_json(asset_ledger_path))


class _StubWritingVideoAdapter:
    """重生成用桩：写 STUB-CLIP 占位并记录调用。"""

    def __init__(self):
        self.calls = []

    def generate(self, shot):
        self.calls.append(shot["shot_id"])
        p = Path(shot["_clip_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"STUB-CLIP::" + shot["shot_id"].encode("utf-8"))
        return str(p)


class _StubWritingTtsAdapter:
    def __init__(self):
        self.calls = []

    def synthesize(self, shot):
        self.calls.append(shot["shot_id"])
        p = Path(shot["_audio_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"STUB-AUDIO::" + shot["shot_id"].encode("utf-8"))
        planned = shot.get("dialogue_planned_seconds") or shot["duration"]
        return {"path": str(p), "duration_seconds": float(planned)}


def test_resume_rejects_empty_and_truncated_clips(
        tmp_path, storyboard_dict, dubbing_path, asset_ledger_path):
    """clips 注入 0 字节 / 截断 mp4 → 续跑不得判 cache hit，必须重新生成；完好桩不重跑。"""
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)
    clips = tmp_path / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    (clips / "E01-01-f1.mp4").write_bytes(b"")  # 0 字节
    (clips / "E01-01-f2.mp4").write_bytes(b"\x00\x01\x02TRUNCATED")  # 截断：无魔数
    (clips / "E01-01-f3.mp4").write_bytes(b"STUB-CLIP::E01-01-f3")  # 完好对照
    adapter = _StubWritingVideoAdapter()
    res = run_video_stage(plan, tmp_path, adapter)
    assert sorted(adapter.calls) == ["E01-01-f1", "E01-01-f2"], \
        f"0字节/截断文件必须重新生成，完好文件不得重跑：{adapter.calls!r}"
    assert res["succeeded"] == 2 and res["skipped"] == 1
    assert list(clips.glob("*.part")) == [], "不得残留 .part 临时文件"
    failed = json.loads((tmp_path / "failed_items.json").read_text(encoding="utf-8"))
    assert failed == [], f"重生成后失败台账应收敛：{failed!r}"


def test_resume_rejects_empty_audio(tmp_path, storyboard_dict, dubbing_path, asset_ledger_path):
    """audio 注入 0 字节 wav → tts 续跑不得跳过，必须重新合成。"""
    plan = _plan(storyboard_dict, dubbing_path, asset_ledger_path)
    audio = tmp_path / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    (audio / "E01-01-f1.wav").write_bytes(b"")
    adapter = _StubWritingTtsAdapter()
    res = run_tts_stage(plan, tmp_path, adapter)
    assert "E01-01-f1" in adapter.calls, f"0字节音频必须重新合成：{adapter.calls!r}"
    assert res["skipped"] == 0


def _mock_open(payload):
    class _FakeResp:
        def __init__(self):
            self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

        def read(self):
            return self._buf.read()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake(req, url, timeout):
        return _FakeResp()

    return _fake


def test_http_video_adapter_rejects_invalid_bytes_without_leaving_files(
        monkeypatch, tmp_path):
    """服务端回包字节无视频魔数 → 校验拦截抛错，不落最终文件、不残留 .part。"""
    monkeypatch.setenv("RENDER_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("RENDER_KEY", "real-test-token")
    bad = base64.b64encode(b"GARBAGE-NO-MAGIC-AT-ALL!!!!").decode("ascii")
    monkeypatch.setattr(ad, "_open", _mock_open({"video_bytes": bad}))
    clip = tmp_path / "c.mp4"
    shot = {"shot_id": "E01-01-f1", "duration": 3, "prompt": "real prompt",
            "_clip_path": str(clip)}
    try:
        HttpVideoAdapter().generate(shot)
    except ArtifactValidationError:
        pass
    else:
        raise AssertionError("无魔数字节必须被 validate_rendered_artifact 拦截")
    assert not clip.exists(), "校验失败不得落最终文件"
    assert list(tmp_path.glob("*.part")) == [], "校验失败不得残留 .part 临时文件"


def test_http_video_adapter_atomic_replace_leaves_no_part(monkeypatch, tmp_path):
    """合法魔数字节 → 经 .part 原子替换落盘（正向守卫：无残留、内容一致）。"""
    monkeypatch.setenv("RENDER_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("RENDER_KEY", "real-test-token")
    raw = b"\x00\x00\x00\x00ftypisom" + b"\x01" * 16
    payload = {"video_bytes": base64.b64encode(raw).decode("ascii")}
    monkeypatch.setattr(ad, "_open", _mock_open(payload))
    clip = tmp_path / "c.mp4"
    shot = {"shot_id": "E01-01-f1", "duration": 3, "prompt": "real prompt",
            "_clip_path": str(clip)}
    HttpVideoAdapter().generate(shot)
    assert clip.read_bytes() == raw
    assert list(tmp_path.glob("*.part")) == [], "成功路径不得残留 .part 临时文件"
