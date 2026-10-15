"""以子进程 CLI 边界调用 douyin-ingest，不 import 其内部模块。

结果分类见 `_classify`：认证失效、作品不可用、结构变化都立即定性；
只有超时、连接错误、429/5xx 才重试（最多 3 次总尝试，退避 1 秒、3 秒）。
message 面向用户可读，绝不包含敏感路径、登录态或上游原始响应。
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from scripts import paths

logger = logging.getLogger("douyin_transcript.ingest")

LOGIN_REQUIRED_MESSAGE = "抖音匿名访问被拒绝，需要专用 Chromium 扫码登录"
PLATFORM_FAILED_MESSAGE = "抖音作品无法访问或上游采集失败"
TIMEOUT_MESSAGE = "上游采集超时，已自动重试仍未成功"
RATE_LIMITED_MESSAGE = "抖音限制了访问频率，已自动重试仍未成功"
UNAVAILABLE_MESSAGE = "作品不存在、已删除或不可公开访问"
CONTRACT_MESSAGE = "上游输出结构与本项目契约不兼容，请先验收再升级依赖"

IngestKind = Literal["ok", "login_required", "video_unavailable",
                     "platform_rate_limited", "ingest_timeout",
                     "ingest_contract_changed", "platform_failed"]

_UNAVAILABLE_PATTERNS = (
    re.compile(r"\b(?:404|410)\b"),
    re.compile("作品不存在|视频不存在|已删除|已下架|不可见|私密|无权限|已失效"),
)
_RATE_LIMIT_PATTERNS = (
    re.compile(r"\b429\b"),
    re.compile("请求过于频繁|访问频繁|限流|风控|Too Many Requests", re.IGNORECASE),
)
_SERVER_ERROR_PATTERN = re.compile(r"\b(?:500|502|503|504)\b")
_TIMEOUT_PATTERNS = (
    re.compile("超时|timeout|timed out", re.IGNORECASE),
    re.compile("未在限定时间内"),
)
_CONNECTION_PATTERNS = (
    re.compile("connection|连接|网络|network|ssl|eof", re.IGNORECASE),
)
_AUTH_ERROR_TYPES = frozenset({"AuthenticationError", "AuthenticationExpiredError"})
_STRUCTURE_ERROR_TYPES = frozenset({"ResponseSchemaError", "SignatureRejectedError",
                                    "PaginationError", "TranscriptionDependencyError"})

_KNOWN_ERROR_TYPES = frozenset({
    "AuthenticationError", "AuthenticationExpiredError", "ApiError",
    "CaptureError", "MediaExtractionError", "InvalidUserUrlError",
    "TranscriptionError", "TranscriptionModelError", "TranscriptionInputError",
    "ResponseSchemaError", "SignatureRejectedError", "PaginationError",
    "BrowserError", "PlaywrightError",
})


@dataclass(frozen=True)
class IngestRunResult:
    kind: IngestKind
    payload: dict | None
    message: str
    attempts: int = 1


@dataclass(frozen=True)
class RetryPolicy:
    """只重试可恢复的失败；`sleep` 可注入，便于测试不真实等待。"""

    max_attempts: int = 3
    backoff_seconds: tuple[float, ...] = (1.0, 3.0)
    timeout_seconds: int = 300
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def delay_for(self, completed_attempts: int) -> float:
        if not self.backoff_seconds:
            return 0.0
        index = min(max(completed_attempts - 1, 0), len(self.backoff_seconds) - 1)
        return float(self.backoff_seconds[index])


DEFAULT_RETRY_POLICY = RetryPolicy()


def resolve_executable() -> Path:
    """定位与当前解释器同环境的 douyin-ingest CLI。"""
    scripts_dir = Path(sys.executable).parent
    candidate = scripts_dir / "douyin-ingest.exe"
    if candidate.is_file():
        return candidate
    found = shutil.which("douyin-ingest")
    if found:
        return Path(found)
    raise FileNotFoundError("douyin-ingest CLI 未安装")


_URL_IN_MESSAGE = re.compile(r"https?://[^\s\"'<>）】]+", re.IGNORECASE)
_HEADER_IN_MESSAGE = re.compile(
    r"(?:cookie|set-cookie|authorization|x-tt-token|x-bogus)\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE)


def _scrub(message: str, secrets: list[str]) -> str:
    """上游消息在对外之前必须去掉直链、请求头与本地路径。"""
    text = message or ""
    text = _URL_IN_MESSAGE.sub("<上游直链>", text)
    text = _HEADER_IN_MESSAGE.sub("<请求头>", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<本地路径>")
    return text.strip()[:300]


def _matches(patterns: tuple[re.Pattern, ...], text: str) -> bool:
    return any(pattern.search(text or "") for pattern in patterns)


def _classify(error_type: str, message: str) -> tuple[IngestKind, str]:
    """把上游错误映射为稳定分类；未知结构一律不盲目重试。"""
    text = message or ""
    if error_type in _AUTH_ERROR_TYPES:
        return "login_required", LOGIN_REQUIRED_MESSAGE
    if _matches(_UNAVAILABLE_PATTERNS, text):
        return "video_unavailable", UNAVAILABLE_MESSAGE
    if _matches(_RATE_LIMIT_PATTERNS, text):
        return "platform_rate_limited", RATE_LIMITED_MESSAGE
    if _SERVER_ERROR_PATTERN.search(text):
        return "platform_rate_limited", RATE_LIMITED_MESSAGE
    if _matches(_TIMEOUT_PATTERNS, text) or _matches(_CONNECTION_PATTERNS, text):
        return "ingest_timeout", TIMEOUT_MESSAGE
    if error_type in _STRUCTURE_ERROR_TYPES or error_type not in _KNOWN_ERROR_TYPES:
        return "ingest_contract_changed", CONTRACT_MESSAGE
    return "platform_failed", text[:300] or PLATFORM_FAILED_MESSAGE


def _parse_stdout(stdout: str) -> tuple[dict | None, IngestRunResult | None]:
    """返回 (payload, 直接失败结果)；二者恰有一个为 None。"""
    text = (stdout or "").strip()
    if not text:
        return None, IngestRunResult("ingest_contract_changed", None, CONTRACT_MESSAGE)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, IngestRunResult("ingest_contract_changed", None, CONTRACT_MESSAGE)
    if not isinstance(payload, dict):
        return None, IngestRunResult("ingest_contract_changed", None, CONTRACT_MESSAGE)
    return payload, None


def _invoke(argv: list[str], env: dict, timeout: int, scrub_secrets: list[str]
            ) -> tuple[dict | None, IngestRunResult | None, bool]:
    """执行一次上游调用。第三项表示该失败是否值得重试。"""
    try:
        completed = subprocess.run(  # noqa: S603 — 固定参数列表，不使用 shell
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("上游采集失败，将按策略重试：%s", type(exc).__name__)
        return None, IngestRunResult("ingest_timeout", None, TIMEOUT_MESSAGE), True

    payload, failure = _parse_stdout(completed.stdout or "")
    if failure is not None:
        logger.warning("上游输出无法解析为约定 JSON，按契约变化处理")
        return None, failure, False
    if payload.get("ok") is True:
        return payload, None, False

    error = payload.get("error")
    error_type = ""
    raw_message = ""
    if isinstance(error, dict):
        error_type = str(error.get("type", "") or "")
        raw_message = _scrub(str(error.get("message", "") or ""), scrub_secrets)
    kind, message = _classify(error_type, raw_message)
    retryable = kind in ("ingest_timeout", "platform_rate_limited")
    return None, IngestRunResult(kind, payload, message), retryable


def run_ingest(user_input: str, job_dir: Path, *, headless: bool,
               retry_policy: RetryPolicy | None = None) -> IngestRunResult:
    policy = retry_policy or DEFAULT_RETRY_POLICY
    paths.INGEST_STATE_DIR.mkdir(parents=True, exist_ok=True)
    audio_dir = Path(job_dir) / "media"
    audio_dir.mkdir(parents=True, exist_ok=True)

    try:
        executable = resolve_executable()
    except FileNotFoundError:
        return IngestRunResult("platform_failed", None, PLATFORM_FAILED_MESSAGE)

    argv = [str(executable), user_input, "--json"]
    if headless:
        argv.append("--headless")
    argv += ["--speech-audio-dir", str(audio_dir)]

    env = {**os.environ, "DOUYIN_INGEST_HOME": str(paths.INGEST_STATE_DIR)}
    scrub_secrets = [str(job_dir), str(executable)]

    attempts = 0
    last: IngestRunResult | None = None
    while attempts < max(1, policy.max_attempts):
        attempts += 1
        payload, failure, retryable = _invoke(argv, env, policy.timeout_seconds,
                                              scrub_secrets)
        if payload is not None:
            return IngestRunResult("ok", payload, "", attempts=attempts)
        assert failure is not None
        last = failure
        if not retryable or attempts >= policy.max_attempts:
            return IngestRunResult(failure.kind, failure.payload, failure.message,
                                   attempts=attempts)
        delay = policy.delay_for(attempts)
        if delay > 0:
            policy.sleep(delay)
    assert last is not None
    return IngestRunResult(last.kind, last.payload, last.message, attempts=attempts)


# ====================================================================
# D5 —— 上游单点依赖（douyin-ingest）失败路径与降级通道选择
#
# 全部抖音访问都钉死在固定版本第三方 douyin-ingest（requirements.lock 里 git+...@v0.3.0）。
# 单点依赖的正常失效方式是上游停更/改版/删库（同类 bilibili-api 今年已归档）。
# 因此这里提供：① 可验证的失败路径 + 白话预警；② 降级通道 yt-dlp（仅在探测到可执行文件时启用，
# 绝不 pip 安装任何东西）；③ 本地第三方源码副本见 third-party/douyin-ingest。
# ====================================================================

DEFAULT_PINNED_VERSION = "0.3.0"
INGEST_VERSION_ENV = "DOUYIN_INGEST_VERSION"
CHANNEL_ENV = "DOUYIN_INGEST_CHANNEL"
UPSTREAM_NAME = "douyin-ingest"
UPSTREAM_REPO_HINT = "检查上游仓库 github.com/ltppp/douyin-ingest"
YTDLP_INSTALL_HINT = 'python -m pip install "yt-dlp[default]"（或 winget install yt-dlp）'
YTDLP_TIMEOUT = 180

_VALID_CHANNELS = ("auto", "ingest", "yt-dlp")
# 认证失效不是上游死亡（另有扫码流程），不进降级
_OK_OR_AUTH_KINDS = ("ok", "login_required")
_UPSTREAM_FAILURE_KINDS = ("platform_failed", "ingest_contract_changed",
                           "ingest_timeout", "platform_rate_limited")


def pinned_version() -> str:
    """锁定版本：默认 v0.3.0，可用 DOUYIN_INGEST_VERSION 覆盖（也用于失败路径演练）。"""
    return (os.environ.get(INGEST_VERSION_ENV) or DEFAULT_PINNED_VERSION).strip()


def installed_ingest_version() -> str:
    """本机实际安装的 douyin-ingest 版本；未安装返回空串。"""
    try:
        return importlib.metadata.version(UPSTREAM_NAME)
    except Exception:  # PackageNotFoundError / metadata 损坏都按未安装处理
        return ""


def yt_dlp_path() -> str | None:
    """只探测、绝不安装：用 shutil.which 判断降级通道是否就绪。"""
    return shutil.which("yt-dlp")


def effective_channel(channel: str = "") -> str:
    """通道优先级：显式入参 > 环境变量 > auto；非法值一律回落 auto。"""
    value = (channel or "").strip().lower()
    if value:
        return value if value in _VALID_CHANNELS else "auto"
    env = (os.environ.get(CHANNEL_ENV) or "").strip().lower()
    return env if env in _VALID_CHANNELS else "auto"


def upstream_dead_message(pin: str, installed: str, has_ytdlp: bool) -> str:
    """单点依赖不可用时给用户的白话可处置文案（无异常类名、无 Traceback）。"""
    ver = pin or "未知"
    if installed:
        reason = f"本机实际版本 {installed} 与锁定版本 {ver} 不一致"
    else:
        reason = f"本机未安装锁定版本 {ver}"
    text = (f"上游单点依赖 {UPSTREAM_NAME}（锁定 {ver}）当前不可用：{reason}。"
            f"建议先用降级通道重试：加 `--channel yt-dlp`。")
    if not has_ytdlp:
        text += (f"本机未检测到 yt-dlp，安装命令：{YTDLP_INSTALL_HINT}，"
                 f"装好后加 `--channel yt-dlp` 重试。")
    text += (f"若上游长期失败，请{UPSTREAM_REPO_HINT} 是否停更或改版"
             f"（同类上游今年已发生归档），必要时更换采集器。")
    return text


def run_yt_dlp(user_input: str, job_dir: Path, *, executable: str,
               timeout: int = YTDLP_TIMEOUT) -> IngestRunResult:
    """降级通道：用 yt-dlp 下载音轨，产出与上游契约同形的 payload 供下游 ASR 消费。

    只负责取音频，不改写签名逻辑；任何失败都返回白话文案，绝不抛出堆栈。
    """
    media_dir = Path(job_dir) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(media_dir / "yt.%(ext)s")
    argv = [str(executable), "-f", "bestaudio/best", "--no-playlist",
            "--restrict-filenames", "-o", outtmpl,
            "--print", "after_move:%(id)s\t%(title)s\t%(webpage_url)s",
            user_input]
    try:
        completed = subprocess.run(  # noqa: S603 — 固定参数列表，不使用 shell
            argv, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("降级通道 yt-dlp 执行失败：%s", type(exc).__name__)
        return IngestRunResult("platform_failed", None,
                               f"降级通道 yt-dlp 执行失败（{type(exc).__name__}），"
                               f"请稍后重试或加 `--channel ingest` 走原通道")
    downloaded = sorted(media_dir.glob("yt.*"))
    if completed.returncode != 0 or not downloaded:
        return IngestRunResult("platform_failed", None,
                               "降级通道 yt-dlp 未能取得可转写的音频")
    vid = title = page = ""
    lines = (completed.stdout or "").splitlines()
    printed = next((l for l in lines if l.strip()), "")
    parts = [p.strip() for p in printed.split("\t")]
    if len(parts) >= 3:
        vid, title, page = parts[0], parts[1], parts[2]
    elif printed:
        logger.warning("降级通道打印行字段不足（%d/3），按 id 缺失处理", len(parts))
    if not vid:
        # 取不到 id 就不能造身份：videos.aweme_id 是主键、transcript_versions 按
        # (aweme_id, version_no) 唯一，落到常量会让所有降级产物塌进同一行并互相串用。
        logger.warning("降级通道未取得视频 id（yt-dlp 打印行缺字段），按失败处理而非伪造共享身份")
        return IngestRunResult(
            "platform_failed", None,
            "降级通道没能取得视频 id（identity 缺失），已停止以免与其他降级任务共用同一记录；"
            "请重试，或加 `--channel ingest` 走原通道")
    payload = {
        "ok": True,
        "channel": "yt-dlp",
        "videos": [{
            "aweme_id": vid,   # 上面已保证非空，不再留 "ytdlp" 这种共享身份的兜底
            "name": title,
            "page_url": page,
            "speech_audio_file": str(downloaded[0]),
            "duration_seconds": 0,
            "downloaded_via": "yt-dlp",
        }],
    }
    return IngestRunResult("ok", payload, "")


def _degrade_to_yt_dlp(user_input: str, job_dir: Path, *, pin: str,
                       installed: str) -> IngestRunResult:
    """探测 yt-dlp：就绪则尝试下载；缺席则回白话安装命令。绝不 pip。"""
    exe = yt_dlp_path()
    if not exe:
        return IngestRunResult("platform_failed", None,
                               upstream_dead_message(pin, installed, False))
    fb = run_yt_dlp(user_input, job_dir, executable=exe)
    if fb.kind == "ok":
        return fb
    # 降级通道已经用过 yt-dlp，失败时不能再回「请改用 --channel yt-dlp」——那是指错方向。
    return IngestRunResult("platform_failed", None,
                           fb.message or upstream_dead_message(pin, installed, True))


def ingest_with_fallback(user_input: str, job_dir: Path, *, headless: bool,
                         channel: str = "",
                         retry_policy: RetryPolicy | None = None) -> IngestRunResult:
    """带降级通道的采集入口：先跑锁定版 ingest，上游失效时按策略回落 yt-dlp。"""
    ch = effective_channel(channel)
    pin = pinned_version()
    installed = installed_ingest_version()

    if ch == "yt-dlp":
        return _degrade_to_yt_dlp(user_input, job_dir, pin=pin, installed=installed)

    # 版本红线：锁定版本与本机实际不一致即判上游不可用，不再盲调上游
    if installed != pin:
        if ch == "ingest":
            return IngestRunResult("platform_failed", None,
                                   upstream_dead_message(pin, installed,
                                                          bool(yt_dlp_path())))
        return _degrade_to_yt_dlp(user_input, job_dir, pin=pin, installed=installed)

    run = run_ingest(user_input, job_dir, headless=headless,
                     retry_policy=retry_policy)
    if ch == "ingest" or run.kind in _OK_OR_AUTH_KINDS:
        return run
    if run.kind in _UPSTREAM_FAILURE_KINDS:
        return _degrade_to_yt_dlp(user_input, job_dir, pin=pin, installed=installed)
    return run
