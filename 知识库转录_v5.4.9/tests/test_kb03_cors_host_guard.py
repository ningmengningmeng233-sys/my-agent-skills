# -*- coding: utf-8 -*-
"""KB-03: Web 控制台 CORS 收敛与 Host 校验。

受控 RED→GREEN：恶意跨域请求不得拿到通配 CORS 头，非环回 Host 必须 403；
合法 localhost/127.0.0.1 请求保持 200。
"""
from __future__ import annotations

import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import start_web


def _parse_response(raw: bytes) -> tuple[int, dict[str, str]]:
    head, _, _ = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers


@pytest.fixture()
def web_server(tmp_lib):
    """在环回地址上起一个真实的控制台服务（临时端口，仅本机可达）。"""
    class Handler(start_web.KnowledgeWebHandler):
        pass

    Handler.lib_path = Path(tmp_lib)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port: int, method: str, path: str, headers: dict[str, str]) -> tuple[int, dict[str, str]]:
    """裸 socket 发包：精确控制 Host/Origin，不经过任何代理。"""
    lines = [f"{method} {path} HTTP/1.1"]
    for key, value in headers.items():
        lines.append(f"{key}: {value}")
    lines.extend(["Connection: close", "", ""])
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall("\r\n".join(lines).encode("utf-8"))
        raw = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
    return _parse_response(raw)


def test_evil_origin_gets_no_wildcard_cors(web_server):
    port = web_server.server_address[1]
    status, headers = _request(port, "GET", "/api/health/live", {
        "Host": f"127.0.0.1:{port}",
        "Origin": "http://evil.com",
    })
    assert status == 200
    assert headers.get("access-control-allow-origin") != "*", \
        "恶意 Origin 不得拿到通配 CORS 头"


def test_evil_origin_options_gets_no_wildcard_cors(web_server):
    port = web_server.server_address[1]
    status, headers = _request(port, "OPTIONS", "/api/health/live", {
        "Host": f"127.0.0.1:{port}",
        "Origin": "http://evil.com",
    })
    assert status == 200
    assert headers.get("access-control-allow-origin") != "*", \
        "OPTIONS 预检不得对恶意 Origin 返回通配 CORS 头"


def test_non_loopback_host_is_forbidden(web_server):
    port = web_server.server_address[1]
    status, _ = _request(port, "GET", "/api/health/live", {
        "Host": "evil.com",
        "Origin": "http://evil.com",
    })
    assert status == 403, "非环回 Host 请求必须返回 403"


def test_legit_loopback_request_still_ok(web_server):
    port = web_server.server_address[1]
    status, _ = _request(port, "GET", "/api/health/live", {
        "Host": f"127.0.0.1:{port}",
    })
    assert status == 200


def test_legit_origin_is_echoed_not_wildcard(web_server):
    port = web_server.server_address[1]
    origin = f"http://127.0.0.1:{port}"
    status, headers = _request(port, "GET", "/api/health/live", {
        "Host": f"127.0.0.1:{port}",
        "Origin": origin,
    })
    assert status == 200
    assert headers.get("access-control-allow-origin") == origin
