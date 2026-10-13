"""R02-06：技能根 .env 必须在每个 CLI 入口被读进 os.environ（作者共有不变量）。

上游 B 站仓四个入口都在 import/入口期 load_dotenv；抖音此前只有摘要阶段的惰性
加载点，导致 .env.example 对外承诺的 DOUYIN_WHISPER_MODEL /
DOUYIN_TRANSCRIPT_LOG_FILE 在 .env 通道上静默失效。本文件钉死三件事：
1. 入口跑完后 .env 里的运行期开关确实进了 os.environ 并被下游读到；
2. 六个 CLI 入口都在 configure_logging()（doctor 是 run_checks）之前调用共用的
   scripts.paths.load_skill_env()；
3. python-dotenv 不可导入时，所有入口行为一致：不崩、.env 键仍按内置最小解析
   加载，且与 dotenv 解析结果逐键相同；进程环境变量始终优先。

全部离线：tmp_path 合成技能根 + 假 ingest/假 ASR + 合成 .db，绝不碰真实端点、
真实数据库、真实登录态或真实 .env。
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import paths
from scripts import logger as skill_logger
from scripts.douyin_transcript import _initial_strategy
from scripts.fill_summaries import main as fill_summaries_main
from scripts.quality import HardwareBudget
from tests import conftest

PRODUCT_ROOT = Path(__file__).resolve().parents[1]

# 会被 .env 或入口写进 os.environ 的键：每条用例前后彻底隔离
_TOUCHED_KEYS = ("DOUYIN_WHISPER_MODEL", "DOUYIN_TRANSCRIPT_LOG_FILE",
                 "SUMMARY_API_URL", "SUMMARY_API_MODEL", "OPENAI_API_KEY",
                 "QUOTED")


@pytest.fixture(autouse=True)
def _isolated_environment():
    saved = dict(os.environ)
    for key in _TOUCHED_KEYS:
        os.environ.pop(key, None)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _synthetic_skill_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                          env_text: str = "") -> Path:
    """把 scripts.paths 的整套技能根常量指到 tmp_path/skill，绝不落真实目录。"""
    root = tmp_path / "skill"
    for sub in (".db", "output", "state/logs", "state/jobs", "state/ingest"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    if env_text:
        (root / ".env").write_text(env_text, encoding="utf-8")
    monkeypatch.setattr(paths, "ROOT", root)
    monkeypatch.setattr(paths, "DB_PATH", root / ".db" / "transcripts.db")
    monkeypatch.setattr(paths, "OUTPUT_DIR", root / "output")
    monkeypatch.setattr(paths, "STATE_DIR", root / "state")
    monkeypatch.setattr(paths, "JOBS_DIR", root / "state" / "jobs")
    monkeypatch.setattr(paths, "INGEST_STATE_DIR", root / "state" / "ingest")
    monkeypatch.setattr(paths, "LOG_DIR", root / "state" / "logs")
    monkeypatch.setattr(skill_logger, "LOG_FILE",
                        root / "state" / "logs" / "transcript.log")
    return root


def _env_text(tmp_path: Path) -> str:
    custom_log = (tmp_path / "custom" / "run.log").as_posix()
    return (f"DOUYIN_WHISPER_MODEL=tiny\n"
            f"DOUYIN_TRANSCRIPT_LOG_FILE={custom_log}\n")


def _stub_services():
    """假服务：_initial_strategy 只读 hardware 档位，绝不建真模型。"""
    return SimpleNamespace(hardware=HardwareBudget(device="cpu",
                                                   compute_type="int8",
                                                   max_model="base"))


def _run_fill_stats(root: Path) -> int:
    return fill_summaries_main(["--stats", "--db",
                                str(root / ".db" / "transcripts.db")])


def test_entry_loads_whisper_model_from_skill_dotenv(tmp_path, monkeypatch,
                                                     capsys):
    """行为①：技能根 .env 写 DOUYIN_WHISPER_MODEL=tiny，入口跑完后必须取到 tiny。"""
    root = _synthetic_skill_root(tmp_path, monkeypatch, _env_text(tmp_path))

    rc = _run_fill_stats(root)
    capsys.readouterr()

    assert rc == 0
    assert os.environ.get("DOUYIN_WHISPER_MODEL") == "tiny", (
        "CLI 入口没有把技能根 .env 读进 os.environ（R02-06 缺失 load_skill_env）")
    assert _initial_strategy(_stub_services(), 10.0, "中文标题").model == "tiny", (
        "_initial_strategy 仍在回落硬件档位，.env 的 DOUYIN_WHISPER_MODEL 不生效")


def test_entry_loads_log_file_switch_from_skill_dotenv(tmp_path, monkeypatch,
                                                       capsys):
    """行为②：.env 的 DOUYIN_TRANSCRIPT_LOG_FILE 必须让 production_log_file() 生效。"""
    expected = Path((tmp_path / "custom" / "run.log").as_posix())
    root = _synthetic_skill_root(tmp_path, monkeypatch, _env_text(tmp_path))

    rc = _run_fill_stats(root)
    capsys.readouterr()

    assert rc == 0
    assert skill_logger.production_log_file() == expected, (
        "configure_logging 读不到 .env 里的 DOUYIN_TRANSCRIPT_LOG_FILE，"
        "日志仍写 state/logs/transcript.log")
    assert expected.is_file(), "入口期没加载 .env，自定义日志路径完全没被使用"


def test_process_environment_wins_over_skill_dotenv(tmp_path, monkeypatch,
                                                    capsys):
    """进程环境变量优先（override=False），但 .env 的其余键仍要照常灌入。"""
    expected = Path((tmp_path / "custom" / "run.log").as_posix())
    root = _synthetic_skill_root(tmp_path, monkeypatch, _env_text(tmp_path))
    monkeypatch.setenv("DOUYIN_WHISPER_MODEL", "large-v3")

    rc = _run_fill_stats(root)
    capsys.readouterr()

    assert rc == 0
    assert os.environ.get("DOUYIN_WHISPER_MODEL") == "large-v3", (
        ".env 不得覆盖已存在的进程环境变量")
    assert skill_logger.production_log_file() == expected, (
        "入口期加载 .env 后，未被进程环境占据的键必须生效（.env.example 承诺的通道）")


# ---------------------------------------------------------------- 入口一致性

def _recording_entry_calls(monkeypatch: pytest.MonkeyPatch):
    """记录 load_skill_env 与「入口开始使用配置」的先后顺序。"""
    assert hasattr(paths, "load_skill_env"), (
        "scripts.paths 必须提供共用的 load_skill_env()（R02-06）")
    calls: list[str] = []
    monkeypatch.setattr(paths, "load_skill_env",
                        lambda *a, **k: (calls.append("load_skill_env"), True)[1])
    return calls


def _anchor_configure_logging(module, monkeypatch, calls, *, from_logger_module=False):
    def _record(*args, **kwargs):
        calls.append("uses_config")
        return logging.getLogger("skill-env-invariant")

    target = skill_logger if from_logger_module else module
    monkeypatch.setattr(target, "configure_logging", _record)


def _argv_for(entry: str, root: Path) -> list[str]:
    return {
        "douyin_transcript": ["https://www.douyin.com/video/7000000001"],
        "batch_transcribe": ["https://www.douyin.com/video/7000000001"],
        "maintenance": ["fill-ai", "--db", str(root / ".db" / "transcripts.db")],
        "fill_summaries": ["--stats", "--db", str(root / ".db" / "transcripts.db")],
        "migrations": ["--db", str(root / ".db" / "absent.db")],
        "doctor": ["--json"],
    }[entry]


ENTRIES = ("douyin_transcript", "batch_transcribe", "maintenance",
           "fill_summaries", "migrations", "doctor")


@pytest.mark.parametrize("entry", ENTRIES)
def test_cli_entry_loads_skill_env_before_using_configuration(
        entry, tmp_path, monkeypatch, capsys):
    """六个 CLI 入口都必须在读配置之前先 paths.load_skill_env()。"""
    root = _synthetic_skill_root(tmp_path, monkeypatch)
    calls = _recording_entry_calls(monkeypatch)
    module = importlib.import_module(f"scripts.{entry}")

    if entry == "douyin_transcript":
        _anchor_configure_logging(module, monkeypatch, calls)
        monkeypatch.setattr(module, "default_services",
                            lambda db, **kw: SimpleNamespace(force=False, ingest=None))
        monkeypatch.setattr(module, "process_one",
                            lambda job, services: {"ok": True, "status": "completed"})
    elif entry == "batch_transcribe":
        _anchor_configure_logging(module, monkeypatch, calls)
        monkeypatch.setattr(module, "run_batch",
                            lambda *a, **k: {"ok": True, "results": []})
    elif entry == "doctor":
        # doctor 不装 handler，它的「使用配置」时刻是跑各项检查
        monkeypatch.setattr(module, "run_checks",
                            lambda: (calls.append("uses_config"),
                                     {"ok": True, "checks": []})[1])
    elif entry == "migrations":
        # migrations.main 内部才 from scripts.logger import configure_logging
        _anchor_configure_logging(module, monkeypatch, calls,
                                  from_logger_module=True)
    else:
        _anchor_configure_logging(module, monkeypatch, calls)

    module.main(_argv_for(entry, root))
    capsys.readouterr()

    assert calls == ["load_skill_env", "uses_config"], (
        f"{entry} 没有在 configure_logging/检查之前调用 paths.load_skill_env()，"
        f".env 键对该入口静默无效（实际顺序：{calls}）")


# ------------------------------------------------------- dotenv 缺失时的同一条路

PARITY_ENV = ('DOUYIN_WHISPER_MODEL=tiny\n'
              '# 注释行\n'
              'export SUMMARY_API_MODEL=gpt-4o-mini\n'
              'QUOTED="a b"\n')


def _load_once(monkeypatch: pytest.MonkeyPatch, root: Path,
               *, block_dotenv: bool) -> dict:
    for key in _TOUCHED_KEYS:
        os.environ.pop(key, None)
    if block_dotenv:
        monkeypatch.setitem(sys.modules, "dotenv", None)
    assert hasattr(paths, "load_skill_env"), (
        "scripts.paths 必须提供共用的 load_skill_env()（R02-06）")
    loaded = paths.load_skill_env()
    return {"loaded": loaded,
            **{key: os.environ.get(key) for key in _TOUCHED_KEYS}}


def test_dotenv_missing_does_not_change_what_env_keys_arrive(
        tmp_path, monkeypatch):
    """缺 python-dotenv 时走内置最小解析：键与值与 dotenv 通道逐条相同。"""
    root = _synthetic_skill_root(tmp_path, monkeypatch, PARITY_ENV)

    with_dotenv = _load_once(monkeypatch, root, block_dotenv=False)
    without_dotenv = _load_once(monkeypatch, root, block_dotenv=True)

    assert with_dotenv["loaded"] is True
    assert with_dotenv["DOUYIN_WHISPER_MODEL"] == "tiny"
    assert without_dotenv == with_dotenv, (
        "dotenv 不可导入时 .env 键必须照常生效（内置最小解析），"
        "否则各入口在缺包机器上行为不一致")


BLOCKED_DOTENV_SOURCE = 'raise ImportError("dotenv blocked by R02-06 test")\n'


def _blocked_pythonpath(tmp_path: Path) -> str:
    stub = tmp_path / "blocked-dotenv"
    stub.mkdir()
    (stub / "dotenv.py").write_text(BLOCKED_DOTENV_SOURCE, encoding="utf-8")
    existing = os.environ.get("PYTHONPATH", "")
    return os.pathsep.join([str(stub)] + ([existing] if existing else []))


CLI_RUNS_WHEN_DOTENV_BLOCKED = (
    ("fill_summaries --stats",
     ["scripts/fill_summaries.py", "--stats", "--db", "{db}"], 0),
    ("maintenance fill-ai",
     ["scripts/maintenance.py", "fill-ai", "--db", "{db}"], 0),
    ("入口模块 import（generate_summary/doctor/migrations）",
     ["-c", "import scripts.generate_summary, scripts.doctor, scripts.migrations"], 0),
)


@pytest.mark.parametrize("label,argv,expected_rc",
                         CLI_RUNS_WHEN_DOTENV_BLOCKED)
def test_entries_survive_when_dotenv_is_unimportable(
        label, argv, expected_rc, tmp_path):
    """主链路被 try/except 兜住而其它入口直接崩，就是本条要消灭的不一致。"""
    db = tmp_path / "blocked.db"
    pythonpath = _blocked_pythonpath(tmp_path)
    res = conftest.run_py([item.replace("{db}", str(db)) for item in argv],
                          cwd=str(PRODUCT_ROOT),
                          env={"PYTHONPATH": pythonpath})
    print(res.stdout, res.stderr)

    assert "dotenv blocked by R02-06 test" not in res.stderr, (
        f"{label}：python-dotenv 缺失即 ModuleNotFoundError，入口行为不一致")
    assert res.returncode == expected_rc, (
        f"{label}：缺 dotenv 时入口必须照常完成（rc={res.returncode}，"
        f"stderr={res.stderr[:400]}）")
    if expected_rc == 0 and "{db}" in " ".join(argv):
        assert json.loads(res.stdout.strip().splitlines()[-1])
