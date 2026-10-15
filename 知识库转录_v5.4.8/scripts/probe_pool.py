#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六节点 API 体检脚本。

读取 llm_nodes.json，逐节点发一次极短请求（max_tokens=8），
报告连通性、协议、模型名与耗时；本地节点先探测端口是否监听。

用法:
  python probe_pool.py                  # 全节点体检
  python probe_pool.py --json           # JSON 输出
  python probe_pool.py --only 小米MiMo  # 只测指定节点（可多次）
  python probe_pool.py --timeout 30     # 单节点超时（秒）

返回码: 全部可用 0；任一不可用 1；配置错误 2。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

import requests

from api_pool import ApiPool, looks_like_placeholder_key

DEFAULT_CONFIG = os.path.join(SKILL_DIR, "llm_nodes.json")
PROBE_MAX_TOKENS = 8
LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0")


def _local_port(base_url: str) -> int | None:
    """本地地址返回端口号，非本地返回 None。"""
    try:
        parsed = urlparse(base_url)
    except Exception:
        return None
    if parsed.hostname in LOCAL_HOSTS:
        return parsed.port or (443 if parsed.scheme == "https" else 80)
    return None


def _port_open(port: int, timeout: float = 0.8) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def probe_node(node, timeout: float = 40.0) -> dict:
    """探测单个节点。返回 {ok, status, detail, elapsed, endpoint}。"""
    result = {
        "name": node.name,
        "proto": node.proto,
        "model": node.model,
        "endpoint": node.endpoint(),
        "ok": False,
        "status": None,
        "detail": "",
        "elapsed": 0.0,
        "local_port": None,
    }
    port = _local_port(node.base_url)
    if looks_like_placeholder_key(getattr(node, "api_key", "")):
        # 占位符密钥发出去注定 401，直接点名，省掉六次无意义外呼
        result["detail"] = "api_key 仍为占位符（请编辑 llm_nodes.json 填入真实密钥）"
        return result
    if port is not None:
        result["local_port"] = port
        if not _port_open(port):
            result["detail"] = f"本地端口 {port} 未监听（请先运行 start-proxies.bat）"
            return result

    payload = node.payload("你是连通性探针。", "回复 OK", PROBE_MAX_TOKENS)
    payload["stream"] = False

    t0 = time.time()
    try:
        resp = requests.post(
            node.endpoint(), json=payload, headers=node.headers(),
            timeout=(10, timeout), proxies=node.proxies(),
        )
    except Exception as exc:
        result["elapsed"] = time.time() - t0
        result["detail"] = f"{type(exc).__name__}: {str(exc)[:150]}"
        return result

    result["elapsed"] = time.time() - t0
    result["status"] = resp.status_code
    try:
        if resp.status_code != 200:
            result["detail"] = (resp.text or "")[:200].replace("\n", " ")
            return result
        data = resp.json()
        if node.proto == "anthropic":
            blocks = data.get("content") or []
            text = "".join(
                b.get("text", "") for b in blocks if isinstance(b, dict)
            )
        else:
            choices = data.get("choices") or [{}]
            text = (choices[0].get("message") or {}).get("content", "")
        result["ok"] = True
        result["detail"] = f"回复样例: {str(text).strip()[:50]!r}"
    except Exception as exc:
        result["detail"] = f"响应解析失败: {type(exc).__name__}: {str(exc)[:120]}"
    finally:
        resp.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="六节点 API 体检")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="节点配置文件")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--only", action="append", default=None,
                        help="只测指定节点名（可多次）")
    parser.add_argument("--timeout", type=float, default=40.0, help="单节点超时秒数")
    args = parser.parse_args()

    try:
        pool = ApiPool.from_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    nodes = pool.nodes
    if args.only:
        wanted = set(args.only)
        nodes = [n for n in nodes if n.name in wanted]
        if not nodes:
            print(f"[配置错误] --only 未匹配到任何节点: {sorted(wanted)}", file=sys.stderr)
            return 2

    results = [probe_node(n, args.timeout) for n in nodes]

    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    else:
        print("=" * 78)
        print("六节点 API 体检")
        print("=" * 78)
        for i, r in enumerate(results, 1):
            mark = "OK  " if r["ok"] else "FAIL"
            print(f"\n[{mark}] {i}. {r['name']}  [{r['proto']}]  {r['model']}")
            print(f"       {r['endpoint']}")
            if r["status"] is not None:
                print(f"       HTTP {r['status']} | 耗时 {r['elapsed']:.2f}s")
            if r["detail"]:
                print(f"       {r['detail']}")
        ok_n = sum(1 for r in results if r["ok"])
        print()
        print("=" * 78)
        print(f"汇总: {ok_n}/{len(results)} 可用")
        print("=" * 78)

    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
