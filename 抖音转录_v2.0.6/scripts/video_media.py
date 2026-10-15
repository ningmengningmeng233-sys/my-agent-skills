"""临时视频素材：用上游给的下载上下文在 job 目录内获取并验证。

媒体 URL 与请求头只在函数调用期间存在于内存；任何失败信息都不得携带它们。
下载完成后先写 `.part` 再原子改名，只有 FFprobe 确认存在视频流才算成功。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Callable

from scripts.ingest_adapter import IngestResult

logger = logging.getLogger("douyin_transcript.video")

_PROBE_TIMEOUT = 60
_MEDIA_PREFIXES = ("video/", "application/octet-stream", "binary/octet-stream")

DOWNLOAD_FAILED_MESSAGE = "临时视频素材下载失败"
VIDEO_INVALID_MESSAGE = "临时视频素材不是有效视频"


class MediaDownloadError(Exception):
    """视频素材不可用；`code` 为稳定错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _default_session():
    import requests

    return requests.Session()


def probe_media(path: Path) -> dict:
    """返回 FFprobe 的 JSON（流 + 容器）；任何异常都返回空字典。"""
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        return {}
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def probe_duration(path: Path) -> float:
    """容器时长（秒）；未知时返回 0。"""
    payload = probe_media(path)
    try:
        return float((payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def has_video_stream(payload: dict) -> bool:
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return False
    return any(isinstance(stream, dict) and stream.get("codec_type") == "video"
               for stream in streams)


def verify_video(path: Path) -> bool:
    """FFprobe 确认文件存在且含至少一条视频流。"""
    return has_video_stream(probe_media(Path(path)))


def _is_media_response(response) -> bool:
    headers = getattr(response, "headers", {}) or {}
    content_type = ""
    for key, value in headers.items():
        if str(key).lower() == "content-type":
            content_type = str(value).split(";", 1)[0].strip().lower()
            break
    return content_type.startswith(_MEDIA_PREFIXES)


def materialize_video(result: IngestResult, job_dir: Path, *,
                      session=None,
                      probe: Callable[[Path], bool] | None = None) -> Path:
    """把上游临时视频下载到 `job_dir/media/`，返回验证通过的本地路径。"""
    job_dir = Path(job_dir)
    url = str(getattr(result, "video_download_url", "") or "")
    if not url.lower().startswith(("http://", "https://")):
        raise MediaDownloadError("video_invalid", "上游未提供可用的视频直链")

    media_dir = job_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / f"{result.aweme_id}.mp4"
    part = media_dir / f"{target.name}.{uuid.uuid4().hex}.part"
    headers = dict(getattr(result, "download_headers", {}) or {})
    client = session or _default_session()

    try:
        response = client.get(url, headers=headers, stream=True, timeout=120,
                              allow_redirects=True)
    except Exception as exc:  # 网络层异常：不回显 URL
        logger.warning("临时视频下载失败：%s", type(exc).__name__)
        raise MediaDownloadError("media_download_failed",
                                 DOWNLOAD_FAILED_MESSAGE) from exc

    try:
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200 or not _is_media_response(response):
            raise MediaDownloadError("media_download_failed", DOWNLOAD_FAILED_MESSAGE)
        written = 0
        with open(part, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)
                    written += len(chunk)
        if written <= 0:
            raise MediaDownloadError("media_download_failed", DOWNLOAD_FAILED_MESSAGE)
    except MediaDownloadError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:
        part.unlink(missing_ok=True)
        logger.warning("临时视频写入失败：%s", type(exc).__name__)
        raise MediaDownloadError("media_download_failed",
                                 DOWNLOAD_FAILED_MESSAGE) from exc
    finally:
        try:
            response.close()
        except Exception:
            pass

    os.replace(part, target)
    checker = probe or verify_video
    if not checker(target):
        target.unlink(missing_ok=True)
        raise MediaDownloadError("video_invalid", VIDEO_INVALID_MESSAGE)
    return target
