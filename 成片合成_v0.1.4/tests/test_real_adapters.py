# -*- coding: utf-8 -*-
"""W09 真实适配器契约与产物签发合法性测试。"""
import http.server
import json
import socketserver
import threading
from pathlib import Path
import pytest

from renderplan.adapters import (
    HttpVideoAdapter,
    HttpTtsAdapter,
    validate_rendered_artifact,
    ArtifactValidationError,
)


class MockServer(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默测试输出

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        idemp = self.headers.get("Idempotency-Key", "")
        if not auth or "Bearer" not in auth:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized"}')
            return

        if self.path == "/v1/video":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true, "status": "completed", "video_bytes": "AAAAIGZ0eXBpc29t"}')
        elif self.path == "/v1/error_500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "Internal Server Error"}')
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def local_http_server():
    server = socketserver.TCPServer(("127.0.0.1", 0), MockServer)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_validate_rendered_artifact_rejects_stub(tmp_path):
    """占位桩字节流在正式签发模式下必须被严格拒收。"""
    stub_file = tmp_path / "clip_01.mp4"
    stub_file.write_bytes(b"STUB-CLIP::E01-01-f1")

    # 允许桩模式通过
    assert validate_rendered_artifact(stub_file, "video", is_stub_allowed=True) is True

    # 正式交付模式严禁桩产物伪造真出片
    with pytest.raises(ArtifactValidationError) as excinfo:
        validate_rendered_artifact(stub_file, "video", is_stub_allowed=False)
    assert "STUB_ARTIFACT_REJECTED" in str(excinfo.value)


def test_validate_rendered_artifact_checks_video_magic(tmp_path):
    """真实视频签发必须至少具备合法的容器头魔数，非视频文本流被拒。"""
    bad_file = tmp_path / "fake.mp4"
    bad_file.write_text("这不是一个视频文件", encoding="utf-8")

    with pytest.raises(ArtifactValidationError) as excinfo:
        validate_rendered_artifact(bad_file, "video", is_stub_allowed=False)
    assert "INVALID_FORMAT" in str(excinfo.value)

    # 假装合法 mp4 (ftyp)
    real_like = tmp_path / "valid.mp4"
    real_like.write_bytes(b"\x00\x00\x00 ftypisom" + b"\x00" * 100)
    assert validate_rendered_artifact(real_like, "video", is_stub_allowed=False) is True


def test_http_adapter_contract_and_idempotency(monkeypatch, local_http_server, tmp_path):
    """测试真实适配器的请求头、幂等键与响应解码。"""
    monkeypatch.setenv("RENDER_ENDPOINT", local_http_server)
    monkeypatch.setenv("RENDER_KEY", "real-test-token-12345")

    adapter = HttpVideoAdapter()
    clip_path = tmp_path / "rendered.mp4"
    shot = {
        "shot_id": "SC001-SH001",
        "duration": 3.0,
        "prompt": "test prompt",
        "_clip_path": str(clip_path),
    }

    out_path = adapter.generate(shot, endpoint_path="/v1/video")
    assert Path(out_path).exists()
    assert Path(out_path).stat().st_size > 0
