# -*- coding: utf-8 -*-
"""问题清单 B8：「没配 key 就不会外呼」的离线钉死测试。

背景：把 `GEMINI_API_KEY` 等密钥全部 unset、仓库无 `.env` 时，旧实现仍会走到
传输层拿到远端 502——密钥解析链隐式回退读取 `~/.asset-extract/.env`（内含密钥
与指向本机中转池的 `GOOGLE_GEMINI_BASE_URL`）。本文件在**禁止任何出站**的钉死
夹具下验收修复：

- 密钥缺失必须在任何网络调用（含重试）之前本地拒绝，文案含
  `GEMINI_API_KEY` 与手工导出路径（:381-382 口径：不改计划、不留事件）；
- `ASSET_OFFLINE=1` 置真时传输层直接抛 `OfflineRequested`，发生在任何 socket 之前；
- 验收用例连跑结果完全一致，且与网络状态（代理端口）无关。
"""
import json
import os
import socket
import sys
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine import common  # noqa: E402
from scripts.engine.gemini_image import GeminiImageExecutor  # noqa: E402
from scripts.engine.generator_registry import (  # noqa: E402
    CodexGeminiGatewayExecutor,
    GatewayResponse,
)
from scripts.engine.image_executor import (  # noqa: E402
    EXECUTOR_NOT_CONFIGURED,
    GenerationError,
    GenerationRequest,
    executor_is_configured,
)
from scripts.engine.offline import (  # noqa: E402
    OFFLINE_BLOCKED,
    OFFLINE_ENV,
    OfflineRequested,
    ensure_online,
    offline_requested,
)
from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_planner import plan_visual_tasks  # noqa: E402
from scripts.engine.visual_schema import write_visual_plan  # noqa: E402

VISUAL = REPO_ROOT / "scripts" / "visual_production.py"
DEAD_PROXY = "http://127.0.0.1:9"          # discard 端口：漏网外呼立即死在本地
DEAD_PROXY_ALT = "http://127.0.0.1:44599"  # 另一个不可达端口：验证与网络状态无关


# --- 传输替身：只记账，绝不出站 ---------------------------------------------------

class GeminiClientDouble:
    """假 Gemini SDK client：记录 generate_content 次数；被调用即视为外呼企图。"""

    def __init__(self):
        self.calls = 0

    class _Models:
        def __init__(self, owner):
            self._owner = owner

        def generate_content(self, **kwargs):
            self._owner.calls += 1
            raise AssertionError("禁止出站：传输替身不应在缺 key/离线场景被调用")

    @property
    def models(self):
        return GeminiClientDouble._Models(self)


class GatewayTransportDouble:
    """假网关 transport：记录 calls 次数，返回固定 200 响应。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, payload):
        self.calls += 1
        return GatewayResponse(status_code=200, image_bytes=b"PNG",
                               mime_type="image/png", response_id="double")


class EgressHarness:
    def __init__(self, gemini_client, transport):
        self.gemini_client = gemini_client
        self.transport = transport


@pytest.fixture
def assert_no_egress(monkeypatch):
    """出站钉死夹具：代理指向丢弃端口、no_proxy 清空、全部 *_API_KEY 清除、
    socket.socket 直接抛 AssertionError('禁止出站')，并注入记录次数的传输替身。"""
    monkeypatch.setenv("HTTP_PROXY", DEAD_PROXY)
    monkeypatch.setenv("HTTPS_PROXY", DEAD_PROXY)
    monkeypatch.setenv("http_proxy", DEAD_PROXY)
    monkeypatch.setenv("https_proxy", DEAD_PROXY)
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    for name in list(os.environ):
        if name.upper().endswith("_API_KEY"):
            monkeypatch.delenv(name, raising=False)
    for name in ("ASSET_ENV_PATH", "GOOGLE_GEMINI_BASE_URL", "CODEX_GEMINI_BASE_URL",
                 "SUMMARY_API_URL", "SUMMARY_API_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(OFFLINE_ENV, "1")

    def _forbid_socket(*args, **kwargs):
        raise AssertionError("禁止出站")

    monkeypatch.setattr(socket, "socket", _forbid_socket)
    return EgressHarness(GeminiClientDouble(), GatewayTransportDouble())


def _pinned_subprocess_env(extra=None):
    """CLI 子进程环境：与 assert_no_egress 同口径（清 key + 离线 + 死代理）。"""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().endswith("_API_KEY")
           and k.upper() not in ("ASSET_ENV_PATH", "GOOGLE_GEMINI_BASE_URL",
                                 "CODEX_GEMINI_BASE_URL")}
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY,
        "http_proxy": DEAD_PROXY, "https_proxy": DEAD_PROXY,
        "no_proxy": "", "NO_PROXY": "",
        OFFLINE_ENV: "1",
    })
    env.update(extra or {})
    return env


def _lib(tmp_path):
    index = {
        "schema_version": 1, "source_fingerprint": "sha256:demo",
        "assets": [{"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘",
                    "priority": "P1", "fact_level": "剧本事实",
                    "evidence": [{"segment": "段01.md", "line": "00001",
                                  "quote": "林姑娘走进茶楼"}],
                    "relations": [], "dependencies": [], "status": "CONFIRMED"}],
    }
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")
    plan = plan_visual_tasks(index, resolve_style_record("仙侠国风电影CG"))
    write_visual_plan(plan, lib / "视觉资产" / "计划.toml")
    return lib


def _cli(lib, *args, env=None):
    return run_py(
            [str(VISUAL), *[str(a) for a in args], "--lib", str(lib)],
            cwd=str(REPO_ROOT),
            env=env or _pinned_subprocess_env(),
    )


# --- 1) 缺 key：任何传输调用之前本地拒绝（double.calls == 0） ----------------------

def test_missing_key_rejects_before_any_transport_call(assert_no_egress):
    double = assert_no_egress.gemini_client
    executor = GeminiImageExecutor(client=double)   # 即使注入了可用 client，
    request = GenerationRequest(task_id="CH_林姑娘-正脸-v01", prompt="正脸三视图")
    with pytest.raises(GenerationError) as ctx:
        executor.generate(request)
    assert ctx.value.code == EXECUTOR_NOT_CONFIGURED
    assert "GEMINI_API_KEY" in ctx.value.detail
    assert "设置环境变量 GEMINI_API_KEY" in ctx.value.detail
    assert "export" in ctx.value.detail, "必须给出手工导出提示词的替代路径"
    assert double.calls == 0, "缺 key 时不得触达任何传输调用（更不得进重试）"
    assert assert_no_egress.transport.calls == 0


def test_missing_key_cli_refuses_before_any_transport_call(tmp_path, assert_no_egress):
    """产品入口（run/run-ready）同口径：拒绝、非零退出、不改计划、不留事件。"""
    lib = _lib(tmp_path)
    plan_path = lib / "视觉资产" / "计划.toml"
    before = plan_path.read_bytes()
    r = _cli(lib, "run", "--task", "CH_林姑娘-正脸-v01")
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "GEMINI_API_KEY" in out and "export" in out
    assert "RATE_LIMITED" not in out, "拒绝必须发生在传输层之前，不得出现重试外呼痕迹"
    assert plan_path.read_bytes() == before, "拒绝时不得改动计划"
    assert not (lib / ".process" / "视觉事件.jsonl").exists(), "拒绝时不得派发、不得留事件"

    rr = _cli(lib, "run-ready")
    out2 = rr.stdout + rr.stderr
    assert rr.returncode != 0 and "GEMINI_API_KEY" in out2 and "export" in out2
    assert plan_path.read_bytes() == before, "拒绝时不得改动计划"
    assert not (lib / ".process" / "视觉事件.jsonl").exists(), "拒绝时不得派发、不得留事件"
    assert assert_no_egress.gemini_client.calls == 0 and assert_no_egress.transport.calls == 0


# --- 2) ASSET_OFFLINE：任何 socket 之前抛 OfflineRequested ------------------------

def test_offline_flag_blocks_real_gemini_client_before_socket(assert_no_egress, monkeypatch):
    """显式导出密钥 + 离线开关：真实 SDK client 的构造（socket 之前一步）必须被拦。"""
    monkeypatch.setenv("GEMINI_API_KEY", "explicit-shell-key")
    executor = GeminiImageExecutor()   # 不注入 client：走真实 _client_or_create
    with pytest.raises(OfflineRequested) as ctx:
        executor.generate(GenerationRequest(task_id="t", prompt="p"))
    assert ctx.value.code == OFFLINE_BLOCKED
    assert assert_no_egress.gemini_client.calls == 0


def test_offline_flag_blocks_gateway_socket(assert_no_egress):
    gw = CodexGeminiGatewayExecutor("https://billing.example.invalid/v1/generate",
                                    api_key="explicit-key")
    with pytest.raises(OfflineRequested):
        gw.generate(GenerationRequest(task_id="t", prompt="p"))
    assert assert_no_egress.transport.calls == 0


def test_injected_transports_are_unaffected_by_offline(assert_no_egress, monkeypatch):
    """离线开关钉的是真实 socket：注入的替身传输照常可测（不挡测试接缝）。"""
    monkeypatch.setenv("GEMINI_API_KEY", "explicit-shell-key")
    gw = CodexGeminiGatewayExecutor("https://gw.example/v1",
                                    transport=assert_no_egress.transport)
    result = gw.generate(GenerationRequest(task_id="t", prompt="p"))
    assert result.status == "OK"
    assert assert_no_egress.transport.calls == 1


def test_ensure_online_semantics(monkeypatch):
    monkeypatch.setenv(OFFLINE_ENV, "1")
    assert offline_requested()
    with pytest.raises(OfflineRequested):
        ensure_online("远端端点", url="https://generativelanguage.googleapis.com")
    with pytest.raises(OfflineRequested):
        ensure_online("端点未知")
    ensure_online("单测假端点", url="http://127.0.0.1:59999/v1")     # loopback 放行
    ensure_online("localhost 假端点", url="http://localhost:8080/x")  # loopback 放行
    monkeypatch.delenv(OFFLINE_ENV)
    assert not offline_requested()
    ensure_online("在线模式", url="https://anything")


# --- 3) 隐藏回退被钉死：隐式包外 .env 不再供给密钥 --------------------------------

def test_no_hidden_env_fallback_when_offline(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".asset-extract").mkdir(parents=True)
    (fake_home / ".asset-extract" / ".env").write_text(
        "GEMINI_API_KEY=hidden-relay-key\nGOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8050\n",
        encoding="utf-8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ASSET_ENV_PATH", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv(OFFLINE_ENV, "1")
    assert common.load_env() is None, "离线模式不得隐式回退 ~/.asset-extract/.env"
    assert executor_is_configured() is False, "隐藏回退被钉死：进程无 key 即未配置"
    # 显式 ASSET_ENV_PATH 仍是受支持的产品能力（不是一刀切禁用 .env）
    env_file = tmp_path / "显式.env"
    env_file.write_text("GEMINI_API_KEY=explicit-file-key\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_ENV_PATH", str(env_file))
    assert common.load_env() == str(env_file)
    assert executor_is_configured() is True


# --- 4) 确定性验收：连跑 3 次结果一致，且与网络状态无关 ----------------------------

def _three_identical_runs(lib, env_factory):
    results = []
    for _ in range(3):
        r = _cli(lib, "run", "--task", "CH_林姑娘-正脸-v01", env=env_factory())
        results.append((r.returncode, r.stdout, r.stderr))
    assert results[0] == results[1] == results[2], "连续 3 次运行结果必须完全一致"
    rc, out, err = results[0]
    text = out + err
    assert rc != 0 and "GEMINI_API_KEY" in text and "export" in text
    return text


def test_run_without_key_is_deterministic_across_three_runs(tmp_path):
    lib = _lib(tmp_path)
    _three_identical_runs(lib, _pinned_subprocess_env)


def test_run_without_key_independent_of_network_state(tmp_path):
    """把代理换到另一个不可达端口：结果与网络状态无关（同一份输出）。"""
    lib = _lib(tmp_path)

    def other_dead_port():
        env = _pinned_subprocess_env()
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[var] = DEAD_PROXY_ALT
        return env

    baseline = _three_identical_runs(lib, _pinned_subprocess_env)
    # 换到另一个不可达代理端口（网络状态不同）：钉死口径下输出仍逐字一致。
    moved = _three_identical_runs(lib, other_dead_port)
    assert baseline == moved, "换不可达代理端口后输出必须逐字一致"
