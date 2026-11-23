"""Skill 根目录与运行期路径的唯一来源。

所有路径从本文件的物理位置推导，绝不依赖当前工作目录。
`.env` 的加载也只有一个入口函数 `load_skill_env()`，供所有 CLI 入口在读配置之前调用。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]

DB_PATH: Path = ROOT / ".db" / "transcripts.db"
OUTPUT_DIR: Path = ROOT / "output"
STATE_DIR: Path = ROOT / "state"
JOBS_DIR: Path = STATE_DIR / "jobs"
INGEST_STATE_DIR: Path = STATE_DIR / "ingest"
LOG_DIR: Path = STATE_DIR / "logs"


def ensure_runtime_dirs() -> list[Path]:
    """创建并返回全部运行期目录（幂等）。"""
    dirs = [DB_PATH.parent, OUTPUT_DIR, JOBS_DIR, LOG_DIR, INGEST_STATE_DIR]
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
    return dirs


_ENV_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$""",
                       re.MULTILINE)


def minimal_load_dotenv(dotenv_path: Path | str, *, override: bool = False) -> bool:
    """python-dotenv 不可导入时的内置最小解析（签名与 `dotenv.load_dotenv` 对齐）。

    只支持 `.env.example` 承诺的形态：注释行、空行、`KEY=VALUE`、可选 `export`
    前缀与成对引号；与 dotenv 一样默认不覆盖已存在的进程环境变量。
    """
    try:
        text = Path(dotenv_path).read_text(encoding="utf-8-sig")
    except OSError:
        return False
    for key, value in _ENV_LINE.findall(text):
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
    return True


def load_skill_env() -> bool:
    """把技能根 `.env` 读进 `os.environ`：每个 CLI 入口都必须在 `configure_logging()`
    之前调用一次（作者 B 站仓四个入口共有的入口期加载不变量）。

    - 进程环境变量始终优先（等价上游 `load_dotenv(override=False)`）；
    - 没有 `.env` 属正常态，直接返回 False；
    - python-dotenv 不可导入时不崩，退化为 `minimal_load_dotenv`，
      保证所有入口在缺包机器上行为一致。
    `.env` 内容（密钥等）永不写日志、不进 TXT/DB、不出现在回复里。
    """
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        return minimal_load_dotenv(env_file)
    return bool(load_dotenv(env_file, override=False))
