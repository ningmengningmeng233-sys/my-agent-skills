#!/usr/bin/env python3
"""开工前自检：环境、依赖、数据库、配置、目录可写性。

    .venv/Scripts/python.exe scripts/doctor.py --json

每项给出 `ok` / `detail` / `fix_command` / `severity`：
severity=error 的项必须全绿；severity=warning 表示能力降级但可继续（例如缺 CUDA 运行库会退回 CPU）。
退出码 0 表示无 error 级失败。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

try:
    from dotenv import load_dotenv

    load_dotenv(str(SKILL_DIR / ".env"))
except ImportError:
    pass

DB_PATH = SKILL_DIR / ".db" / "transcripts.db"
ARCHIVE_FIX = "在 .env 设置 BILI_ARCHIVE_DIR 指向归档目录（如知识库的 转录文件/已转录）"
SCHEMA_VERSION = 4


def _check(name: str, ok: bool, detail: str, fix: str = "",
           severity: str = "error") -> dict:
    return {"name": name, "ok": ok, "detail": detail,
            "fix_command": fix, "severity": severity}


def module_available(name: str) -> bool:
    """模块能否导入；用于把「装了元数据但包目录被删」这类损坏也算作不可用。"""
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001 导入期任何异常都视为不可用
        return False


def installed_version(name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return "未知"


def check_python() -> dict:
    version = sys.version_info
    ok = version >= (3, 10)
    return _check("python", ok,
                  f"当前 Python {version.major}.{version.minor}.{version.micro}，要求 >= 3.10",
                  "使用 Python 3.10 以上重建 .venv")


def check_venv() -> dict:
    expected = (SKILL_DIR / ".venv").resolve()
    current = Path(sys.executable).resolve()
    ok = expected in current.parents
    return _check("venv", ok,
                  f"解释器 {current}" if ok else f"当前解释器不在技能自带 .venv 内：{current}",
                  r".venv\Scripts\python.exe scripts\doctor.py --json",
                  severity="error" if ok else "warning")


def check_ffmpeg() -> dict:
    found = shutil.which("ffmpeg")
    probe = shutil.which("ffprobe")
    ok = bool(found and probe)
    return _check("ffmpeg", ok,
                  f"ffmpeg={found or '未找到'}，ffprobe={probe or '未找到'}",
                  "安装 FFmpeg 并加入 PATH（winget install Gyan.FFmpeg）")


def check_ytdlp() -> dict:
    binary = shutil.which("yt-dlp")
    module_ok = True
    version = ""
    try:
        import yt_dlp  # noqa: F401
        version = getattr(yt_dlp.version, "__version__", "")
    except ImportError:
        module_ok = False
    ok = bool(binary or module_ok)
    return _check("yt_dlp", ok,
                  f"二进制={binary or '未找到'}，模块={version or '未安装'}",
                  ".venv\\Scripts\\python.exe -m pip install -r requirements.lock")


def check_faster_whisper() -> dict:
    try:
        import faster_whisper

        return _check("faster_whisper", True,
                      f"faster-whisper {getattr(faster_whisper, '__version__', 'OK')}")
    except ImportError:
        return _check("faster_whisper", False, "faster-whisper 未安装",
                      ".venv\\Scripts\\python.exe -m pip install -r requirements.lock")


def check_cuda() -> dict:
    """GPU 快路径是否可用；不可用时流水线会自动退回 CPU，故为 warning。"""
    try:
        from fw_whisper_main import cuda_available
    except Exception as exc:  # noqa: BLE001
        return _check("cuda", False, f"无法加载 fw_whisper_main：{type(exc).__name__}",
                      "", severity="warning")
    if cuda_available():
        return _check("cuda", True, "CUDA float16 可用（含 cublas/cudnn 运行时）")
    return _check("cuda", False,
                  "CUDA 不可用（缺设备或 cublas/cudnn 运行时），Whisper 将退回 CPU int8",
                  ".venv\\Scripts\\python.exe -m pip install -r requirements.lock",
                  severity="warning")


def check_env_config() -> dict:
    fav = os.environ.get("FAV_MEDIA_ID", "").strip()
    archive = os.environ.get("BILI_ARCHIVE_DIR", "").strip()
    api = os.environ.get("OPENAI_API_KEY", "").strip()
    details = (f"FAV_MEDIA_ID={'已设置' if fav else '空'}，"
               f"BILI_ARCHIVE_DIR={'已设置' if archive else '空'}，"
               f"摘要密钥={'已设置' if api else '空'}")
    return _check("env_config", bool(fav and archive), details,
                  "补齐 .env 的 FAV_MEDIA_ID 与 BILI_ARCHIVE_DIR",
                  severity="error" if (fav and archive) else "warning")


def check_archive_dir() -> dict:
    raw = os.environ.get("BILI_ARCHIVE_DIR", "").strip()
    if not raw:
        return _check("archive_dir", False, "未配置 BILI_ARCHIVE_DIR", ARCHIVE_FIX,
                      severity="warning")
    path = Path(raw)
    if not path.is_dir():
        return _check("archive_dir", False, f"目录不存在：{path}",
                      "确认知识库路径，或修正 .env 的 BILI_ARCHIVE_DIR")
    probe = path / ".doctor-write-probe"
    try:
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError:
        writable = False
    return _check("archive_dir", writable,
                  f"{path}（{'可写' if writable else '不可写'}）",
                  "检查目录权限")


def check_database() -> dict:
    if not DB_PATH.is_file():
        return _check("database", True, "数据库尚未创建（首次处理时会自动建立）")
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            rows = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
            works = conn.execute(
                "SELECT COUNT(DISTINCT bvid) FROM transcripts").fetchone()[0]
            versions = conn.execute(
                "SELECT COUNT(*) FROM transcript_versions").fetchone()[0] \
                if "transcript_versions" in tables else 0
            segments = conn.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0] if "transcript_segments" in tables else 0
            scored = conn.execute("SELECT COUNT(*) FROM transcript_versions WHERE quality_score IS NOT NULL").fetchone()[0] if "transcript_versions" in tables else 0
            applied = [row[0] for row in conn.execute(
                "SELECT version FROM schema_migrations")] \
                if "schema_migrations" in tables else []
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _check("database", False, f"数据库不可读：{type(exc).__name__}",
                      "用 .db/backups 下的备份恢复数据库")
    if integrity != "ok":
        return _check("database", False, f"integrity_check 失败：{integrity}",
                      "用 .db/backups 下的备份恢复数据库")
    if "transcript_versions" not in tables:
        return _check("database", False,
                      "缺少 transcript_versions 表（旧版建的库），写库会失败",
                      ".venv\\Scripts\\python.exe scripts\\migrations.py --apply")
    if applied and max(applied) < SCHEMA_VERSION:
        return _check("database", False,
                      f"schema v{max(applied)} 落后于要求的 v{SCHEMA_VERSION}",
                      ".venv\\Scripts\\python.exe scripts\\migrations.py --apply")
    detail = (f"schema v{max(applied) if applied else 1}，记录 {rows} 行 / {works} 个作品，"
              f"版本 {versions} 行（已评分 {scored}），分段 {segments} 行，integrity ok")
    return _check("database", True, detail)


def check_runtime_dirs() -> dict:
    problems = []
    for name in ("output", "state", "state/logs"):
        directory = SKILL_DIR / name
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".doctor-write-probe"
            probe.write_text("probe", encoding="utf-8")
            probe.unlink()
        except OSError:
            problems.append(name)
    ok = not problems
    return _check("runtime_dirs", ok,
                  "运行目录可写" if ok else f"以下目录不可写：{'、'.join(problems)}",
                  "检查技能目录权限")


def check_ocr() -> dict:
    """画面文字（硬字幕）证据链的引擎是否可用。

    OCR 是可选第四步，缺了不影响主转录，故为 warning；但不该「静默坏掉」——
    历史故障是卸载 onnxruntime-gpu 时连删了共享的 onnxruntime 包目录，
    导入失败却一路绿灯到真正跑 OCR 才炸。
    """
    missing = [name for name in ("onnxruntime", "rapidocr")
               if not module_available(name)]
    if missing:
        return _check("ocr", False,
                      f"缺少 {'、'.join(missing)}，画面文字证据不可用（校正阶段会降级）",
                      ".venv\\Scripts\\python.exe -m pip install --force-reinstall "
                      "--no-deps --no-cache-dir onnxruntime rapidocr",
                      severity="warning")
    return _check("ocr", True,
                  f"RapidOCR {installed_version('rapidocr')} + "
                  f"ONNX Runtime {installed_version('onnxruntime')}",
                  severity="warning")


def check_summary_api() -> dict:
    url = os.environ.get("SUMMARY_API_URL", "").strip()
    model = os.environ.get("SUMMARY_API_MODEL", "").strip()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key and url and model:
        return _check("summary_api", True, "摘要与校对接口三项齐全",
                      "", severity="warning")
    return _check("summary_api", False,
                  "摘要接口未配齐（缺密钥/地址/模型），转录照常但摘要留占位符",
                  "在 .env 补齐 OPENAI_API_KEY / SUMMARY_API_URL / SUMMARY_API_MODEL",
                  severity="warning")


CHECKS = (
    check_python,
    check_venv,
    check_ffmpeg,
    check_ytdlp,
    check_faster_whisper,
    check_cuda,
    check_env_config,
    check_archive_dir,
    check_database,
    check_runtime_dirs,
    check_ocr,
    check_summary_api,
)


def run_all() -> dict:
    checks = []
    for function in CHECKS:
        try:
            checks.append(function())
        except Exception as exc:  # noqa: BLE001
            checks.append(_check(function.__name__, False,
                                 f"检查自身异常：{type(exc).__name__}: {exc}", ""))
    ok = all(item["ok"] for item in checks if item["severity"] == "error")
    return {"ok": ok, "checks": checks,
            "errors": sum(1 for c in checks if not c["ok"] and c["severity"] == "error"),
            "warnings": sum(1 for c in checks if not c["ok"] and c["severity"] == "warning")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="B站转录环境自检")
    parser.add_argument("--json", action="store_true", help="输出一行 JSON")
    args = parser.parse_args(argv)
    report = run_all()
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print("🩺 B站转录自检")
        for item in report["checks"]:
            mark = "OK  " if item["ok"] else ("WARN" if item["severity"] == "warning" else "FAIL")
            print(f"   [{mark}] {item['name']:18} {item['detail']}")
            if not item["ok"] and item["fix_command"]:
                print(f"          修复：{item['fix_command']}")
        print(f"\n结论：{'全绿' if report['ok'] else '存在 error 级失败'}"
              f"（error 失败 {report['errors']}，warning {report['warnings']}）")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
