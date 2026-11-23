import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIX = Path(__file__).resolve().parent / "fixtures"

# 子进程 Python 编码三件套：任何调用方（含 extra）都不得覆盖
_PYTHON_ENCODING_FLAGS = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONLEGACYWINDOWSSTDIO": "0",
}


def clean_env(**extra):
    """以 os.environ 为底，强制钉死 Python 子进程编码 flag（merge 后再钉一次）。"""
    env = dict(os.environ)
    env.update(_PYTHON_ENCODING_FLAGS)
    env.update(extra)
    env.update(_PYTHON_ENCODING_FLAGS)
    return env


def run_py(args, cwd=None, **kw):
    """跑 Python 子进程的唯一出口：永不返回 None 流，永不受 GBK 控制台影响。"""
    env = clean_env(**(kw.pop("env", None) or {}))
    kw.pop("text", None)
    kw.pop("encoding", None)
    kw.pop("universal_newlines", None)
    res = subprocess.run([sys.executable, *args], cwd=cwd, env=env,
                         capture_output=True, **kw)
    res.stdout = (res.stdout or b"").decode("utf-8", "replace")
    res.stderr = (res.stderr or b"").decode("utf-8", "replace")
    return res


@pytest.fixture
def storyboard_path():
    return FIX / "storyboard.json"


@pytest.fixture
def bad_storyboard_path():
    return FIX / "storyboard_bad.json"


@pytest.fixture
def dubbing_path():
    return FIX / "dubbing.json"


@pytest.fixture
def asset_ledger_path():
    return FIX / "asset_ledger.json"


@pytest.fixture
def storyboard_dict(storyboard_path):
    return json.loads(storyboard_path.read_text(encoding="utf-8"))


class RecordingStubVideoAdapter:
    """桩视频适配器：产出占位 mp4，并记录每次调用的 shot_id。

    fail_ids 命中的 shot 抛错，用于制造失败隔离场景。"""

    def __init__(self, fail_ids=()):
        self.calls = []
        self.fail_ids = set(fail_ids)

    def generate(self, shot):
        self.calls.append(shot["shot_id"])
        if shot["shot_id"] in self.fail_ids:
            raise RuntimeError("stub: 受控失败 " + shot["shot_id"])
        out = Path(shot["_out_dir"]) / f"{shot['shot_id']}.mp4"
        out.write_bytes(b"stub-video")
        return str(out)


class FixedDurationTtsAdapter:
    """桩 TTS 适配器：按 shot 计划秒数 ± 偏差产出音频时长。"""

    def __init__(self, durations):
        self.durations = dict(durations)
        self.calls = []

    def synthesize(self, shot):
        self.calls.append(shot["shot_id"])
        dur = self.durations.get(shot["shot_id"], shot.get("dialogue_planned_seconds", 1.0))
        return {"audio_seconds": dur, "path": f"{shot['shot_id']}.wav"}
