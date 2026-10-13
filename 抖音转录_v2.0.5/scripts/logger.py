"""统一日志：写入 state/logs/transcript.log 与标准错误，并自动脱敏凭据。

日志绝不记录 API 密钥、Cookie、登录态内容或媒体直链。
测试通过 `configure_logging(log_file=...)` 注入临时文件，绝不触碰生产日志。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from scripts import paths

LOG_FILE = paths.LOG_DIR / "transcript.log"
LOG_FILE_ENV = "DOUYIN_TRANSCRIPT_LOG_FILE"
_MIN_SECRET_LENGTH = 8
_REDACTED = "***"
_SENSITIVE_ENV_KEYS = ("OPENAI_API_KEY",)
_MARKER = "_douyin_transcript_handler"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def production_log_file() -> Path:
    """默认日志路径；`DOUYIN_TRANSCRIPT_LOG_FILE` 可把整套运行日志重定向到别处。

    该开关用于测试与 CI：自动测试绝不写真实 `state/logs/transcript.log`。
    """
    override = os.environ.get(LOG_FILE_ENV, "").strip()
    return Path(override) if override else LOG_FILE


class RedactingFilter(logging.Filter):
    """把注册过的秘密字符串从日志正文中替换掉。"""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()

    def register(self, value: object) -> None:
        text = str(value or "").strip()
        if len(text) >= _MIN_SECRET_LENGTH:
            self._secrets.add(text)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, _REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(str(record.getMessage()))
        record.args = ()
        return True


_FILTER = RedactingFilter()


def ensure_utf8_stdio() -> None:
    """继承 B站版长处：把标准输出/错误固定为 UTF-8。

    Windows GBK 控制台下打印含 emoji 的标题或 JSON 会直接 UnicodeEncodeError。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def register_environment_secrets() -> None:
    for key in _SENSITIVE_ENV_KEYS:
        _FILTER.register(os.environ.get(key, ""))


def _owned_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, _MARKER, False)]


def _install_handlers(log_file: Path | None) -> None:
    """替换本项目自己的 handler；绝不删除调用者安装的其它 handler。"""
    root = logging.getLogger()
    for handler in _owned_handlers():
        root.removeHandler(handler)
        handler.close()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        setattr(handler, _MARKER, True)
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(_FILTER)
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def configure_logging(*secret_values: object,
                      log_file: Path | str | None = None) -> logging.Logger:
    """把日志接到目标文件与标准错误（幂等），并注册敏感值。

    `log_file` 为 None 时写生产日志 `state/logs/transcript.log`；
    显式传入路径时只写该路径——测试必须用这种方式隔离。
    """
    for value in secret_values:
        _FILTER.register(value)
    register_environment_secrets()
    _install_handlers(production_log_file() if log_file is None else Path(log_file))
    return logging.getLogger("douyin_transcript")


def reset_logging() -> None:
    """移除本项目安装的 handler，供测试清理（不改动调用者 handler）。"""
    root = logging.getLogger()
    for handler in _owned_handlers():
        root.removeHandler(handler)
        handler.close()
