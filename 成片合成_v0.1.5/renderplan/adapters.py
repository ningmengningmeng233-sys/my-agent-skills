"""可注入适配器：桩（离线可跑）+ 真实 HTTP 层（凭据缺失即拒绝构造，测试永不可达）。

约定：stage 运行器调用适配器前，会把 shot["_clip_path"] / shot["_audio_path"]
预置为目标产物绝对路径；适配器只需往该路径写文件并回报。
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


class VideoAdapter:
    def generate(self, shot: dict) -> str:  # pragma: no cover - 协议声明
        raise NotImplementedError


class TtsAdapter:
    def synthesize(self, shot: dict) -> dict:  # pragma: no cover - 协议声明
        raise NotImplementedError


class StubVideoAdapter(VideoAdapter):
    """占位 clip：产出一个可辨识的桩 mp4，供离线跑通管线。"""

    def generate(self, shot):
        path = Path(shot["_clip_path"])
        return _write_bytes_atomic_validated(
            path, b"STUB-CLIP::" + shot["shot_id"].encode("utf-8"),
            "video", is_stub_allowed=True)


class StubTtsAdapter(TtsAdapter):
    """占位配音：音频时长取配音对齐单的计划秒数，零漂移（不误报）。"""

    def synthesize(self, shot):
        path = Path(shot["_audio_path"])
        out = _write_bytes_atomic_validated(
            path, b"STUB-AUDIO::" + shot["shot_id"].encode("utf-8"),
            "audio", is_stub_allowed=True)
        planned = shot.get("dialogue_planned_seconds") or shot["duration"]
        return {"path": out, "duration_seconds": float(planned)}


def _guard_credentials():
    """真实凭据门：本机所有池 key 为 REPLACE 占位，或端点未配 → 拒绝构造。"""
    endpoint = os.environ.get("RENDER_ENDPOINT", "").strip()
    key = os.environ.get("RENDER_KEY", "").strip()
    if not endpoint:
        raise RuntimeError("缺真实渲染凭据：RENDER_ENDPOINT 未设置——请设置 RENDER_ENDPOINT=<你的渲染端点>，"
                         "或改用 StubAdapter 走离线桩（桩产物严禁签发为正式交付，BLOCKED-CRED）")
    if not key or key == "REPLACE":
        raise RuntimeError("渲染凭据仍是 REPLACE 占位：请填 RENDER_KEY=<真实 key> 后重跑；"
                         "或改用 StubAdapter 走离线桩（真实渲染不可用，BLOCKED-CRED）")
    return endpoint, key


class ArtifactValidationError(ValueError):
    """产物校验失败：格式不合法或占位桩被伪造为真出片。"""
    pass


def validate_rendered_artifact(path: Path | str, expected_kind: str, is_stub_allowed: bool = False) -> bool:
    """对已渲染或待签发的音视频产物进行真实性与格式魔数探测。"""
    p = Path(path)
    if not p.is_file():
        raise ArtifactValidationError(f"FILE_MISSING: 目标产物不存在: {p}")
    raw = p.read_bytes()
    if len(raw) == 0:
        raise ArtifactValidationError(f"EMPTY_FILE: 产物文件为零字节: {p}")

    is_stub = raw.startswith(b"STUB-CLIP::") or raw.startswith(b"STUB-AUDIO::")
    if is_stub:
        if not is_stub_allowed:
            raise ArtifactValidationError("STUB_ARTIFACT_REJECTED: 占位桩产物严禁签发为正式交付文件")
        return True

    if expected_kind == "video":
        # 探测 MP4 (ftyp) 或 WebM (0x1A45DFA3)
        has_ftyp = b"ftyp" in raw[:32]
        has_webm = raw.startswith(b"\x1a\x45\xdf\xa3")
        if not (has_ftyp or has_webm):
            raise ArtifactValidationError(f"INVALID_FORMAT: 目标文件不包含有效的视频容器魔数: {p.name}")
    elif expected_kind == "audio":
        # 探测 WAV (RIFF....WAVE) 或 MP3 (ID3 / 0xFFFB)
        has_wav = raw.startswith(b"RIFF") and b"WAVE" in raw[:16]
        has_mp3 = raw.startswith(b"ID3") or (len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0)
        if not (has_wav or has_mp3):
            raise ArtifactValidationError(f"INVALID_FORMAT: 目标文件不包含有效的音频容器魔数: {p.name}")

    return True


def _write_bytes_atomic_validated(final_path: Path | str, raw_bytes: bytes,
                                  expected_kind: str, is_stub_allowed: bool = False) -> str:
    """先写 `*.part` 临时文件，经 validate_rendered_artifact 校验后原子替换到最终路径。

    校验失败时删除临时文件并抛错：最终路径要么保持旧文件、要么不存在，
    绝不出现直写一半的半截文件。
    """
    final_path = Path(final_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_name(final_path.name + ".part")
    tmp_path.write_bytes(raw_bytes)
    try:
        validate_rendered_artifact(tmp_path, expected_kind, is_stub_allowed=is_stub_allowed)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise
    os.replace(tmp_path, final_path)
    return str(final_path)


def _open(req, url: str, timeout: int):
    """回环端点（本机渲染/TTS 服务）直连，不套环境代理；远端维持默认行为。"""
    host = (urlparse(url).hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


class HttpVideoAdapter(VideoAdapter):
    """真实视频渲染 HTTP 层。在凭据具备时发起带幂等键与超时重试的 HTTP 请求。"""

    def __init__(self):
        self.endpoint, self.key = _guard_credentials()

    def generate(self, shot: dict, endpoint_path: str = "/v1/video") -> str:
        clip_path = Path(shot["_clip_path"])
        prompt = shot.get("prompt", "")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(
                "EMPTY_PROMPT: 镜头缺真实 prompt，拒绝发送视频渲染请求"
                "（空请求不得付费出片）")
        clip_path.parent.mkdir(parents=True, exist_ok=True)

        url = f"{self.endpoint.rstrip('/')}{endpoint_path}"
        idemp_key = f"{shot.get('shot_id', 'unknown')}:{shot.get('duration', 0)}"
        payload = json.dumps({
            "shot_id": shot.get("shot_id"),
            "duration": shot.get("duration"),
            "prompt": prompt,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Authorization", f"Bearer {self.key}")
        req.add_header("Idempotency-Key", idemp_key)
        req.add_header("Content-Type", "application/json")

        # 具备重试机制（缺媒体字段不重试：直接抛 ArtifactValidationError）
        last_exc = None
        for attempt in range(3):
            try:
                with _open(req, url, 60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if not data.get("video_bytes"):
                        raise ArtifactValidationError(
                            "MISSING_MEDIA_FIELD: 服务端响应缺 video_bytes，"
                            "拒绝写入占位伪字节")
                    try:
                        raw_bytes = base64.b64decode(data["video_bytes"])
                    except Exception as exc:
                        raise ArtifactValidationError(
                            f"MEDIA_DECODE_FAILED: video_bytes 非法: {exc}") from exc
                    if not raw_bytes:
                        raise ArtifactValidationError(
                            "MISSING_MEDIA_FIELD: video_bytes 解码为空，拒绝写入占位伪字节")
                    return _write_bytes_atomic_validated(
                        clip_path, raw_bytes, "video", is_stub_allowed=False)
            except ArtifactValidationError:
                raise
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code >= 500:
                    continue
                raise
            except Exception as exc:
                last_exc = exc
                continue

        raise RuntimeError(f"HTTP 视频渲染重试失败: {last_exc}")


class HttpTtsAdapter(TtsAdapter):
    """真实 TTS HTTP 层。在凭据具备时发起带幂等键与超时重试的 HTTP 请求。"""

    def __init__(self):
        self.endpoint, self.key = _guard_credentials()

    def synthesize(self, shot: dict, endpoint_path: str = "/v1/tts") -> dict:
        audio_path = Path(shot["_audio_path"])
        dlg = shot.get("dialogue", "")
        if not isinstance(dlg, str) or not dlg.strip():
            alt = shot.get("dialogue_text", "")
            if isinstance(alt, str) and alt.strip():
                dlg = alt
        if not isinstance(dlg, str) or not dlg.strip():
            raise ValueError(
                "EMPTY_DIALOGUE: 镜头缺真实台词 dialogue，拒绝发送 TTS 请求"
                "（空请求不得付费出片）")
        audio_path.parent.mkdir(parents=True, exist_ok=True)

        url = f"{self.endpoint.rstrip('/')}{endpoint_path}"
        idemp_key = f"{shot.get('shot_id', 'unknown')}:{dlg}"
        payload = json.dumps({
            "shot_id": shot.get("shot_id"),
            "dialogue": dlg,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Authorization", f"Bearer {self.key}")
        req.add_header("Idempotency-Key", idemp_key)
        req.add_header("Content-Type", "application/json")

        last_exc = None
        for attempt in range(3):
            try:
                with _open(req, url, 60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if not data.get("audio_bytes"):
                        raise ArtifactValidationError(
                            "MISSING_MEDIA_FIELD: 服务端响应缺 audio_bytes，"
                            "拒绝写入占位伪字节")
                    try:
                        raw_bytes = base64.b64decode(data["audio_bytes"])
                    except Exception as exc:
                        raise ArtifactValidationError(
                            f"MEDIA_DECODE_FAILED: audio_bytes 非法: {exc}") from exc
                    if not raw_bytes:
                        raise ArtifactValidationError(
                            "MISSING_MEDIA_FIELD: audio_bytes 解码为空，拒绝写入占位伪字节")
                    dur_raw = data.get("duration_seconds")
                    if dur_raw is None:
                        raise ArtifactValidationError(
                            "MISSING_DURATION: 服务端未返回实测 duration_seconds，"
                            "拒绝回退为计划值")
                    try:
                        dur = float(dur_raw)
                    except (TypeError, ValueError) as exc:
                        raise ArtifactValidationError(
                            f"INVALID_DURATION: duration_seconds 非数值: {dur_raw!r}") from exc
                    out = _write_bytes_atomic_validated(
                        audio_path, raw_bytes, "audio", is_stub_allowed=False)
                    return {"path": out, "duration_seconds": dur}
            except ArtifactValidationError:
                raise
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code >= 500:
                    continue
                raise
            except Exception as exc:
                last_exc = exc
                continue

        raise RuntimeError(f"HTTP TTS 渲染重试失败: {last_exc}")
