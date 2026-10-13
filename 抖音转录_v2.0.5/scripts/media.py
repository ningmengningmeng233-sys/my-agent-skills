"""音频验证（FFprobe）与 job 目录受限清理。

清理只能作用于 state/jobs 下的已解析绝对路径，
绝不触及 .db、output、state/ingest 或 jobs 根目录本身。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts import paths


class MediaError(Exception):
    """音频验证失败。"""


_PROBE_TIMEOUT_SECONDS = 60


def verify_audio(path: Path) -> float:
    """用 FFprobe 验证音频存在音轨且时长大于零，返回秒数。"""
    path = Path(path)
    if not path.is_file():
        raise MediaError("音频文件不存在")
    argv = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-show_streams",
        "-of", "json",
        str(path),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 — 固定参数列表
            argv, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError("FFprobe 不可用或执行失败") from exc
    if completed.returncode != 0:
        raise MediaError("FFprobe 验证失败")
    try:
        probe = json.loads(completed.stdout or "")
    except json.JSONDecodeError as exc:
        raise MediaError("FFprobe 输出不是合法 JSON") from exc
    if not isinstance(probe, dict):
        raise MediaError("FFprobe 输出结构异常")

    streams = probe.get("streams")
    if not isinstance(streams, list):
        streams = []
    has_audio = any(
        isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams
    )
    if not has_audio:
        raise MediaError("文件不包含音轨")

    duration = _parse_duration(probe.get("format"))
    if duration <= 0:
        for stream in streams:
            if isinstance(stream, dict) and stream.get("codec_type") == "audio":
                duration = max(duration, _parse_duration(stream))
    if duration <= 0:
        raise MediaError("音频时长为零或不可解析")
    return duration


def _parse_duration(container: object) -> float:
    if isinstance(container, dict):
        value = container.get("duration")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return 0.0
    return 0.0


def cleanup_job_directory(job_dir: Path, *, preserve: set[Path] = frozenset()) -> None:
    """删除 job 目录内的媒体与中间文件；只允许 state/jobs 内的目录。"""
    target = Path(job_dir).resolve()
    jobs_root = paths.JOBS_DIR.resolve()
    if target == jobs_root or not target.is_relative_to(jobs_root):
        raise ValueError(f"拒绝清理 state/jobs 之外的路径: {target.name}")
    if not target.exists():
        return

    keep = {Path(p).resolve() for p in preserve}
    for entry in sorted(target.rglob("*"), key=lambda p: len(str(p)), reverse=True):
        if entry.is_file() and entry not in keep:
            entry.unlink(missing_ok=True)
    # 自底向上移除空目录（含 job 目录本身，若已空）
    for directory in sorted(
        [d for d in target.rglob("*") if d.is_dir()],
        key=lambda d: len(str(d)), reverse=True,
    ) + [target]:
        try:
            directory.rmdir()
        except OSError:
            pass
