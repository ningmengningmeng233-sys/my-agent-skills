"""
共享日志模块 — 所有脚本统一写入同一个日志文件。
日志路径: <技能目录>/state/logs/transcript.log
可用环境变量 BILI_TRANSCRIPT_LOG_FILE 覆盖（测试与 CI 用，避免污染真实日志）。

用法:
    from logger import log
    log("bilibili_transcript.sh", "开始转录 BVxxx", level="INFO")
    log("generate_summary.py", "AI摘要生成成功", level="SUCCESS")
"""

import os
from datetime import datetime

_DEFAULT_LOG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "logs", "transcript.log")
LOG_FILE = os.environ.get("BILI_TRANSCRIPT_LOG_FILE") or _DEFAULT_LOG_FILE
LOG_DIR = os.path.dirname(LOG_FILE)

os.makedirs(LOG_DIR, exist_ok=True)


def log(source, message, level="INFO"):
    """写入一条日志记录。source 为脚本名，message 为内容。"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] [{source}] {message}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # 日志写入失败不能影响主流程


def info(source, message):
    log(source, message, "INFO")


def success(source, message):
    log(source, message, "SUCCESS")


def warn(source, message):
    log(source, message, "WARN")


def error(source, message):
    log(source, message, "ERROR")