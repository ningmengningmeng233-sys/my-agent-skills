"""校验 douyin-ingest 的成功 JSON，产出本项目内部契约对象。

只接受 ok=true 且 videos 恰好一条；绝不信任未校验字段，
也不在异常信息中泄露上游原始响应。

`video_download_url` 与 `download_headers` 是 OCR 需要的临时素材上下文：
只在当前 job 的进程内存中存在，`repr=False` 保证它们不会被顺手打进日志或异常，
需要落库的信息一律走 `public_metadata()`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_HTTP_SCHEMES = ("http://", "https://")


@dataclass(frozen=True)
class IngestResult:
    aweme_id: str
    canonical_url: str
    title: str
    author: str
    publish_date: str
    duration_seconds: float
    audio_path: Path
    video_download_url: str = field(default="", repr=False)
    download_headers: dict = field(default_factory=dict, repr=False)

    def public_metadata(self) -> dict:
        """可安全落库/写日志的元数据子集。"""
        return {
            "canonical_url": self.canonical_url,
            "title": self.title,
            "author": self.author,
            "publish_date": self.publish_date,
            "duration_seconds": self.duration_seconds,
        }


def _clean_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _publish_date(value: object) -> str:
    text = _clean_str(value)
    # 上游为 ISO datetime；只取本地日期部分
    return text[:10] if len(text) >= 10 and text[4:5] == "-" else ""


def _duration(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else 0.0
    return 0.0


def _http_url(value: object) -> str:
    """只接受 http(s) 直链；其它协议一律丢弃。"""
    text = _clean_str(value)
    return text if text.lower().startswith(_HTTP_SCHEMES) else ""


def _headers(value: object) -> dict[str, str]:
    """扁平化的字符串请求头；嵌套结构直接忽略，不做猜测。"""
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            cleaned[str(key)] = str(item)
    return cleaned


def parse_ingest_payload(payload: dict) -> IngestResult:
    if not isinstance(payload, dict):
        raise ValueError("上游返回的不是 JSON 对象")
    if payload.get("ok") is not True:
        raise ValueError("上游报告采集失败（ok 不为 true）")

    videos = payload.get("videos")
    if not isinstance(videos, list) or len(videos) != 1:
        count = len(videos) if isinstance(videos, list) else 0
        raise ValueError(f"上游返回的视频数量不是 1（实际 {count}），本 Skill 只处理单作品")

    video = videos[0]
    if not isinstance(video, dict):
        raise ValueError("上游视频条目不是 JSON 对象")

    aweme_id = _clean_str(video.get("aweme_id"))
    if not aweme_id:
        raise ValueError("上游结果缺少 aweme_id")

    audio_value = _clean_str(video.get("speech_audio_file"))
    if not audio_value:
        raise ValueError("上游结果缺少口播音频文件 speech_audio_file")
    audio_path = Path(audio_value)
    if not audio_path.is_file():
        raise ValueError("上游口播音频文件不存在")
    if audio_path.stat().st_size <= 0:
        raise ValueError("上游口播音频文件为空")

    user = payload.get("user")
    author = _clean_str(user.get("nickname")) if isinstance(user, dict) else ""

    return IngestResult(
        aweme_id=aweme_id,
        canonical_url=_clean_str(video.get("page_url")),
        title=_clean_str(video.get("name")),
        author=author,
        publish_date=_publish_date(video.get("publish_time")),
        duration_seconds=_duration(video.get("duration_seconds")),
        audio_path=audio_path.resolve(),
        video_download_url=_http_url(video.get("video_download_url")),
        download_headers=_headers(payload.get("download_headers")),
    )
