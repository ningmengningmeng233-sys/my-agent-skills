#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六节点 API 故障转移 · 离线拔线演练（可重复、零外呼）。

真实「拔掉主节点 key」的演练需要有效密钥与真实外呼；本脚本改用**假传输**
（monkeypatch `requests.post`）+ **假密钥**，在临时目录里完整跑通 `ApiPool.call`
真实主循环，复现并验收这条链路：

    主节点 key 失效（HTTP 401）→ 判定额度耗尽 → 自动顺切第二顺位节点
    → 跨协议（openai → anthropic）流式解析 + 思考块剥离 → 事件日志留痕

顺带验证三条边界：止于首个健康节点（第三顺位零消耗）、坏节点不被回撞、
全节点失效时显式抛 `AllNodesExhaustedError` 并保留各节点原因。

用法:
  python scripts/failover_drill.py                 # 打印人读报告
  python scripts/failover_drill.py --workdir D:\\x  # 指定演练目录（可重复比对）
  python scripts/failover_drill.py --kill-all      # 演练「全部节点失效」分支
  python scripts/failover_drill.py --json          # 机器可读输出

返回码: 顺切按预期成立 0；否则 1。

注意: **真实环境的拔线演练仍是待办**（见 references/建立索引.md「离线演练与真实拔线」）。
本脚本只证明调度逻辑正确，不证明六个厂商的真实额度与契约。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

import requests

from api_pool import AllNodesExhaustedError, ApiPool

# 演练用的假密钥：只用于证明「密钥失效」这条分类路径，绝非任何真实凭据。
FAKE_KEYS = [
    "sk-DRILLFAKE-PRIMARY-DO-NOT-LOG-0001",
    "sk-DRILLFAKE-SECONDARY-DO-NOT-LOG-002",
    "sk-DRILLFAKE-THIRD-DO-NOT-LOG-00003",
]

PRIMARY_NAME = "主力-OpenAI"
SECONDARY_NAME = "备用-Anthropic"
THIRD_NAME = "兜底-OpenAI"
NODES_CONFIG_NAME = "drill_llm_nodes.json"
EVENT_LOG_NAME = ".api_pool_events.log"

_EXPECTED_CONTENT = "小说名称: 《演练》\n---\n正文"
_REAL_SESSION_SEND = requests.sessions.Session.send


# --------------------------------------------------------------------------
# 离线护栏
# --------------------------------------------------------------------------

@contextlib.contextmanager
def offline_lockdown():
    """演练上下文内禁止真实外呼：任何走到 HTTPAdapter 的请求都会当场炸掉。

    本机策略要求测试与演练完全离线。`_fake_post` 已经接管了 `requests.post`，
    这里再熔断 `Session.send`，保证万一代码绕过假传输也不会偷偷联网。
    """
    def _blocked(self, *args, **kwargs):  # pragma: no cover - 触发即测试失败
        raise RuntimeError("演练上下文禁止真实外呼（offline lockdown）")

    requests.sessions.Session.send = _blocked
    try:
        yield
    finally:
        requests.sessions.Session.send = _REAL_SESSION_SEND


# --------------------------------------------------------------------------
# 假响应（SSE 分块故意切在 UTF-8 多字节中间）
# --------------------------------------------------------------------------

class FakeResponse:
    """最小可用的流式响应替身，只实现 api_pool 用到的那几个接口。"""

    def __init__(self, status_code: int, text: str = "", sse: str = ""):
        self.status_code = status_code
        self.text = text
        self._sse = sse
        self.closed = False

    def iter_content(self, chunk_size: int = 8192):
        data = self._sse.encode("utf-8")
        step = 7  # 故意小于一个中文字符的字节跨度，压测字节级缓冲
        for i in range(0, len(data), step):
            yield data[i:i + step]

    def close(self) -> None:
        self.closed = True


def _sse_openai(parts: list[str], usage: dict[str, int] | None = None) -> str:
    """OpenAI 兼容 SSE 帧序列（含 usage 帧与 [DONE]）。"""
    out = []
    for p in parts:
        out.append("data: " + json.dumps(
            {"choices": [{"delta": {"content": p}}]}, ensure_ascii=False) + "\n")
    if usage:
        out.append("data: " + json.dumps({"usage": usage}, ensure_ascii=False) + "\n")
    out.append("data: " + json.dumps(
        {"choices": [{"finish_reason": "stop"}]}, ensure_ascii=False) + "\n")
    out.append("data: [DONE]\n")
    return "".join(out)


def _sse_anthropic() -> str:
    """Anthropic Messages SSE：混入 thinking_delta 与思考标签，验证不进正文。"""
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 12}}},
        {"type": "content_block_delta",
         "delta": {"type": "thinking_delta", "thinking": "不该出现在正文"}},
        {"type": "content_block_delta",
         "delta": {"type": "text_delta",
                   "text": "<think>先推理再回答</think>小说名称: 《演练》\n"}},
        {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": "---\n正文"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 9}},
        {"type": "message_stop"},
    ]
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
        for e in events
    )


_QUOTA_401_BODY = '{"error":{"message":"Incorrect API key provided"}}'


def transport_all_healthy(parts: list[str]) -> dict[str, Callable[[], FakeResponse]]:
    """变异测试用的假传输：所有节点都健康——此时**不该**发生顺切与失效。

    用来证明演练不是空跑：一旦池不再按「主节点优先」服务，演练就会判红。
    """
    usage = {"prompt_tokens": 3, "completion_tokens": 4}

    def make() -> FakeResponse:
        return FakeResponse(200, sse=_sse_openai(parts, usage))

    return {name: make for name in (PRIMARY_NAME, SECONDARY_NAME, THIRD_NAME)}


def _response_plan(kill_all: bool) -> dict[str, Callable[[], FakeResponse]]:
    """节点 → 假响应工厂。默认只拔掉主节点，模拟「主节点 key 失效」。"""
    plan: dict[str, Callable[[], FakeResponse]] = {
        PRIMARY_NAME: lambda: FakeResponse(401, text=_QUOTA_401_BODY),
        SECONDARY_NAME: lambda: FakeResponse(200, sse=_sse_anthropic()),
        THIRD_NAME: lambda: FakeResponse(200, sse=_sse_openai(
            ["兜底节点不该被消耗"], {"prompt_tokens": 7, "completion_tokens": 3})),
    }
    if kill_all:
        plan[SECONDARY_NAME] = lambda: FakeResponse(401, text=_QUOTA_401_BODY)
        plan[THIRD_NAME] = lambda: FakeResponse(401, text=_QUOTA_401_BODY)
    return plan


def _write_config(workdir: Path) -> str:
    """写演练用节点配置（假密钥、.invalid 域名，绝不指向任何真实端点）。"""
    nodes = [
        {"name": PRIMARY_NAME, "proto": "openai",
         "base_url": "https://primary.drill.invalid/v1", "model": "drill-m1",
         "api_key": FAKE_KEYS[0], "max_tokens": 28000, "max_concurrency": 2},
        {"name": SECONDARY_NAME, "proto": "anthropic",
         "base_url": "https://secondary.drill.invalid", "model": "drill-m2",
         "api_key": FAKE_KEYS[1], "max_tokens": 28000, "max_concurrency": 2},
        {"name": THIRD_NAME, "proto": "openai",
         "base_url": "https://third.drill.invalid/v1", "model": "drill-m3",
         "api_key": FAKE_KEYS[2], "max_tokens": 28000, "max_concurrency": 2},
    ]
    path = workdir / NODES_CONFIG_NAME
    path.write_text(json.dumps({"nodes": nodes}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# 演练主体
# --------------------------------------------------------------------------

def run_drill(workdir: str | os.PathLike[str], kill_all: bool = False,
              plan: dict[str, Callable[[], FakeResponse]] | None = None) -> dict[str, Any]:
    """跑一次离线拔线演练，返回结构化结论（除 log_path 外完全确定，可比对）。

    `plan` 供变异测试注入自定义假传输（如「主节点居然还健康」）。
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    pool = ApiPool.from_config(_write_config(workdir))
    # 演练不等待退避，保证秒级可重复
    pool.transient_retries, pool.transient_backoff = 1, 0.0
    pool.rate_limit_retries, pool.rate_limit_backoff = 0, 0.0
    log_path = workdir / EVENT_LOG_NAME
    pool.event_log_path = str(log_path)
    if log_path.exists():
        log_path.unlink()  # 同一目录重跑也要能比对

    own_plan = plan is not None
    plan = plan or _response_plan(kill_all)
    endpoints = {n.endpoint(): n.name for n in pool.nodes}
    hits: dict[str, int] = {n.name: 0 for n in pool.nodes}
    first_status: dict[str, int] = {}

    def fake_post(url, **kwargs):
        name = endpoints.get(url)
        if name is None:  # 白名单硬约束：池只应打配置里的地址
            raise AssertionError(f"演练越界：访问了配置外的地址 {url}")
        hits[name] += 1
        first_status.setdefault(name, None)
        if first_status[name] is None:
            resp = plan[name]()
            first_status[name] = resp.status_code
            return resp
        return plan[name]()

    content: str | None = None
    usage: dict[str, Any] | None = {}
    served: str | None = None
    raised: str | None = None
    error_message = ""
    second_round: dict[str, Any] | None = None

    with offline_lockdown():
        real_post = requests.post
        requests.post = fake_post
        try:
            try:
                content, usage, served = pool.call("演练系统提示", "演练用户提示")
            except AllNodesExhaustedError as exc:
                raised, error_message = type(exc).__name__, str(exc)
                content, usage, served = None, {}, None

            if served is not None:
                before = dict(hits)
                _, _, served2 = pool.call("演练系统提示", "演练用户提示")
                second_round = {
                    "served_by": served2,
                    "new_primary_attempts": hits[PRIMARY_NAME] - before[PRIMARY_NAME],
                    "new_secondary_attempts": hits[SECONDARY_NAME] - before[SECONDARY_NAME],
                }
        finally:
            requests.post = real_post

    def node_view(name: str) -> dict[str, Any]:
        idx = [n.name for n in pool.nodes].index(name)
        node = pool.nodes[idx]
        return {
            "attempts": hits[name],
            "http_status": first_status.get(name),
            "exhausted": node.exhausted,
            "reason": node.exhausted_reason,
            "served": node.served,
        }

    events: list[str] = []
    if log_path.is_file():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            cols = line.split("\t")
            if len(cols) >= 2:
                events.append(cols[1])

    if kill_all:
        # 全节点失效：必须显式报错、零可用节点、且不留正文
        ok = (raised == "AllNodesExhaustedError"
              and pool.active_count() == 0
              and content is None
              and events.count("EXHAUSTED") >= 3)
    else:
        ok = (served == SECONDARY_NAME
              and content == _EXPECTED_CONTENT
              and pool.nodes[0].exhausted
              and pool.nodes[0].exhausted_reason.startswith("额度耗尽")
              and hits[PRIMARY_NAME] == 1
              and hits[THIRD_NAME] == 0
              and "EXHAUSTED" in events and "SERVED" in events)

    return {
        "ok": bool(ok),
        "scenario": ("custom-plan" if own_plan
                     else "all-nodes-dead" if kill_all else "primary-key-dead"),
        "primary_name": PRIMARY_NAME,
        "secondary_name": SECONDARY_NAME,
        "third_name": THIRD_NAME,
        "primary": node_view(PRIMARY_NAME),
        "secondary": node_view(SECONDARY_NAME),
        "third": node_view(THIRD_NAME),
        "served_by": served,
        "content": content,
        "usage": usage or {},
        "active_nodes": pool.active_count(),
        "summary": pool.summary(),
        "log_path": str(log_path),
        "log_events": events,
        "raised": raised,
        "error_message": error_message,
        "second_round": second_round,
    }


def _report(res: dict[str, Any]) -> str:
    """人读报告：只出现节点名/状态/事件，绝不回显任何密钥。"""
    lines = [
        "=" * 70,
        "六节点故障转移 · 离线拔线演练（假传输，零真实外呼）",
        "=" * 70,
        f"场景          : {res['scenario']}",
        f"结论          : {'✅ 顺切行为符合预期' if res['ok'] else '❌ 未按预期顺切'}",
        f"主节点 {res['primary_name']}"
        f"  → HTTP {res['primary']['http_status']} · 撞 {res['primary']['attempts']} 次"
        f" · 失效={res['primary']['exhausted']} · {res['primary']['reason'][:60]}",
        f"次节点 {res['secondary_name']}"
        f"  → HTTP {res['secondary']['http_status']} · 撞 {res['secondary']['attempts']} 次"
        f" · 服务 {res['secondary']['served']} 次",
        f"兜底 {res['third_name']}"
        f"  → 撞 {res['third']['attempts']} 次（默认场景应为 0，证明止于首个健康节点）",
        f"实际服务节点  : {res['served_by']}",
        f"正文（剥离思考后）: {res['content']!r}" if res["content"] else "正文          : （无）",
        f"usage         : {res['usage']}",
        f"第二轮调用    : {res['second_round']}",
        f"异常          : {res['raised'] or '（无）'} {res['error_message'][:80]}",
        f"节点池摘要    : {res['summary']}",
        f"可用节点数    : {res['active_nodes']}",
        f"事件留痕      : {' → '.join(res['log_events']) or '（无）'}",
        f"事件日志      : {res['log_path']}",
        "=" * 70,
        "⚠️ 真实环境拔线演练仍是待办：需有效密钥逐节点真实外呼确认额度与契约。",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="六节点故障转移离线演练")
    parser.add_argument("--workdir", default=None,
                        help="演练目录（默认临时目录；产物只有假密钥配置与事件日志）")
    parser.add_argument("--kill-all", action="store_true",
                        help="演练「全部节点失效」分支")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结论")
    args = parser.parse_args(argv)

    workdir = args.workdir or tempfile.mkdtemp(prefix="failover-drill-")
    res = run_drill(workdir, kill_all=args.kill_all)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(_report(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
