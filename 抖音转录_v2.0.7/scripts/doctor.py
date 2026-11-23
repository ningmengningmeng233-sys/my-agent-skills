"""环境与依赖体检：Python、FFmpeg、Chromium、上游 CLI、ASR、CUDA、OCR、
说话人分离、模型清单、摘要配置、数据库与目录可写性。

每项检查都给出可执行修复命令；可选能力缺失降级为 warning 并说明影响，
核心能力缺失才是 error。绝不把缺失谎报为就绪。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # python-dotenv 是锁定依赖；不可导入时退化为内置最小解析，与其它入口一致
    from dotenv import load_dotenv
except ImportError:
    from scripts.paths import minimal_load_dotenv as load_dotenv

from scripts import paths
from scripts.logger import ensure_utf8_stdio

_PROBE_TIMEOUT = 30
_MIN_PYTHON = (3, 12)
_EXPECTED_INGEST_VERSION = "0.3.0"
_LOCK_FIX = ".venv\\Scripts\\python.exe -m pip install -r requirements.lock"


def _check(name: str, ok: bool, detail: str, fix_command: str = "",
           severity: str = "error") -> dict:
    return {"name": name, "ok": bool(ok), "detail": str(detail),
            "fix_command": str(fix_command), "severity": severity}


def installed_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return ""


def module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check_python() -> dict:
    version = ".".join(str(part) for part in sys.version_info[:3])
    ok = sys.version_info[:2] >= _MIN_PYTHON
    required = ".".join(str(part) for part in _MIN_PYTHON)
    return _check("python", ok, f"当前 Python {version}，要求 >= {required}",
                  "" if ok else f"py -{required} -m venv .venv 并按 references/SETUP.md 重建环境")


def check_ffmpeg() -> dict:
    if shutil.which("ffprobe") is None:
        return _check("ffmpeg", False, "未找到 ffprobe（抽帧、音频分析与人声分离都依赖它）",
                      "winget install --id Gyan.FFmpeg -e")
    try:
        result = subprocess.run(
            ["ffprobe", "-version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return _check("ffmpeg", False, f"ffprobe 无法执行：{type(exc).__name__}",
                      "winget install --id Gyan.FFmpeg -e")
    ok = result.returncode == 0
    return _check("ffmpeg", ok, "ffprobe 可用" if ok else "ffprobe 退出码非零",
                  "" if ok else "winget install --id Gyan.FFmpeg -e")


def chromium_installed() -> bool:
    """Playwright 的 Chromium 是否已下载到本机驱动目录（不启动浏览器）。"""
    import os

    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or base / "ms-playwright")
    patterns = (
        "chromium*/chrome-win*/chrome.exe",
        "chromium*/chrome-linux*/chrome",
        "chromium_headless_shell*/chrome-headless-shell-win*/chrome-headless-shell.exe",
        "chromium_headless_shell*/chrome-win*/chrome-headless-shell.exe",
    )
    return any(browsers.glob(pattern) for pattern in patterns)


def check_playwright() -> dict:
    fix = ".venv\\Scripts\\python.exe -m playwright install chromium"
    try:
        installed = chromium_installed()
    except Exception as exc:
        return _check("playwright_chromium", False,
                      f"无法确认 Chromium：{type(exc).__name__}", fix)
    return _check("playwright_chromium", installed,
                  "Chromium 已就绪（首次登录需要它打开专用窗口）" if installed
                  else "缺少 Chromium，无法访问抖音",
                  "" if installed else fix)


def check_ingest() -> dict:
    fix = _LOCK_FIX
    exe = shutil.which("douyin-ingest")
    if exe is None:
        candidate = Path(sys.executable).parent / "douyin-ingest.exe"
        exe = str(candidate) if candidate.is_file() else None
    if exe is None:
        return _check("douyin_ingest", False, "未安装 douyin-ingest CLI", fix)
    try:
        result = subprocess.run(
            [exe, "--help"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return _check("douyin_ingest", False,
                      f"douyin-ingest 无法执行：{type(exc).__name__}", fix)
    version = installed_version("douyin-ingest")
    detail = f"douyin-ingest {version or '未知版本'}"
    ok = result.returncode == 0
    if ok and version and version != _EXPECTED_INGEST_VERSION:
        # 版本红线由 --doctor 退出码承担：非锁定版本一律判 error（阻断），
        # 而不是靠单测断言本机恰好装了哪个版本。
        return _check("douyin_ingest", False,
                      f"{detail}，锁定版本应为 {_EXPECTED_INGEST_VERSION}",
                      fix)
    return _check("douyin_ingest", ok,
                  detail if ok else "douyin-ingest 退出码非零",
                  "" if ok else fix)


def whisper_available() -> bool:
    return module_available("faster_whisper")


def check_whisper() -> dict:
    ok = whisper_available()
    version = installed_version("faster-whisper")
    return _check("faster_whisper", ok,
                  f"faster-whisper {version} 可导入" if ok else "faster-whisper 未安装",
                  "" if ok else _LOCK_FIX)


def detect_cuda() -> bool:
    from scripts.asr import detect_cuda as probe

    return probe()


def gpu_vram_gb():
    from scripts.asr import gpu_vram_gb as probe

    return probe()


def check_cuda() -> dict:
    try:
        available = detect_cuda()
    except Exception as exc:
        return _check("cuda", False, f"CUDA 探测失败：{type(exc).__name__}；将使用 CPU",
                      _LOCK_FIX, severity="warning")
    if not available:
        return _check("cuda", False,
                      "未启用 GPU（缺少 CUDA 设备或 cublas/cudnn 运行时）；"
                      "将使用 CPU，速度较慢",
                      _LOCK_FIX, severity="warning")
    vram = None
    try:
        vram = gpu_vram_gb()
    except Exception:
        vram = None
    tier = "medium" if (vram or 0) >= 6.0 else "small"
    text = f"CUDA 可用，显存约 {vram:.0f}GB" if vram else "CUDA 可用（显存未知）"
    return _check("cuda", True, f"{text}，默认档位 {tier}/float16")


def check_rapidocr() -> dict:
    ok = module_available("rapidocr") and module_available("onnxruntime")
    version = installed_version("rapidocr")
    return _check("rapidocr", ok,
                  f"RapidOCR {version} + ONNX Runtime {installed_version('onnxruntime')}"
                  if ok else "缺少 RapidOCR 或 ONNX Runtime，画面文字能力不可用",
                  "" if ok else _LOCK_FIX)


def check_sherpa() -> dict:
    ok = module_available("sherpa_onnx")
    return _check("sherpa_onnx", ok,
                  f"sherpa-onnx {installed_version('sherpa-onnx')} 可导入" if ok
                  else "缺少 sherpa-onnx：人声分离与说话人分离将降级，原音转录不受影响",
                  "" if ok else _LOCK_FIX,
                  severity="error" if ok else "warning")


_CAPABILITY_LABELS = (("diarization", "说话人分离"), ("uvr", "人声分离"),
                      ("vocals", "人声分离"), ("ocr", "画面文字"))


def _capability_of(name: str) -> str:
    lowered = str(name).lower()
    for key, label in _CAPABILITY_LABELS:
        if key in lowered:
            return label
    return "画面文字"


def _missing_capabilities(report: dict) -> list[str]:
    """从状态表与下载计划两边推断缺失能力，保证降级提示可读。"""
    names: list[str] = []
    for name, state in (report.get("states") or {}).items():
        status = state.get("status") if isinstance(state, dict) else \
            getattr(state, "status", "")
        if status == "ok":
            continue
        names.append(_capability_of(name))
    for item in report.get("download_plan") or []:
        names.append(_capability_of(item.get("name", "")))
    return sorted(set(names))


def model_report() -> dict:
    from scripts.model_assets import ModelAssetManager

    return ModelAssetManager().verify_all()


def check_models() -> dict:
    fix = (".venv\\Scripts\\python.exe scripts\\model_assets.py --sync；"
           "大模型按 --json 输出的 download_plan 手动下载")
    try:
        report = model_report()
    except Exception as exc:
        return _check("model_assets", False,
                      f"模型清单不可读：{type(exc).__name__}", fix)
    if report.get("broken_required"):
        return _check("model_assets", False,
                      "清单标记为必需的模型缺失或损坏", fix)
    missing = report.get("missing_optional") or 0
    if missing:
        capabilities = "、".join(_missing_capabilities(report))
        detail = f"缺少模型副本：{capabilities}"
        if any("画面文字" in name for name in _missing_capabilities(report)):
            detail += "（随 wheel 分发，功能可用，仅未按清单校验哈希）"
        if any(name in ("人声分离", "说话人分离")
               for name in _missing_capabilities(report)):
            detail += "；对应增强能力降级，原音转录不受影响"
        return _check("model_assets", False, detail, fix, severity="warning")
    return _check("model_assets", True, "全部模型哈希校验通过", "")


def check_summary_config() -> dict:
    load_dotenv(paths.ROOT / ".env")
    import os

    if os.environ.get("OPENAI_API_KEY", "").strip():
        return _check("summary_config", True, "摘要与校正 API 密钥已配置", "",
                      severity="warning")
    return _check("summary_config", False,
                  "未配置 OPENAI_API_KEY：转录可正常完成，摘要与校正标记为待补",
                  "复制 .env.example 为 .env 并填写 OPENAI_API_KEY",
                  severity="warning")


def database_status(db_path: Path) -> dict:
    db_path = Path(db_path)
    if not db_path.is_file():
        return {"ok": True, "detail": "数据库尚未创建（首次处理时会自动建立 v2 表）"}
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            versions = [row[0] for row in conn.execute(
                "SELECT version FROM schema_migrations")] \
                if "schema_migrations" in tables else []
            legacy_rows = conn.execute(
                "SELECT COUNT(*) FROM transcripts").fetchone()[0] \
                if "transcripts" in tables else 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"ok": False, "detail": f"数据库不可读：{type(exc).__name__}"}
    if integrity != "ok":
        return {"ok": False, "detail": f"quick_check 失败：{integrity}"}
    # 只有“确实存在 v1 数据且没迁移过”才需要动作；空库/新库不算问题
    if legacy_rows and not versions:
        return {"ok": False,
                "detail": f"检测到 v1 数据 {legacy_rows} 条尚未迁移到 v2"}
    if versions:
        return {"ok": True, "detail": f"schema v{max(versions)}，quick_check 通过"}
    return {"ok": True, "detail": "数据库为空（首次处理时会自动建立 v2 表）"}


def check_database() -> dict:
    status = database_status(paths.DB_PATH)
    return _check("database", bool(status.get("ok")), status.get("detail", ""),
                  "" if status.get("ok") else
                  ".venv\\Scripts\\python.exe scripts\\migrations.py --json")


def dirs_writable() -> tuple[bool, str]:
    for directory in paths.ensure_runtime_dirs():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory, delete=True):
                pass
        except OSError:
            return False, str(directory.relative_to(paths.ROOT))
    return True, ""


def check_dirs() -> dict:
    ok, failed = dirs_writable()
    return _check("writable_dirs", ok,
                  "运行目录可写" if ok else f"目录不可写：{failed}",
                  "" if ok else "检查磁盘空间与目录权限")


_CHECKS = (check_python, check_ffmpeg, check_playwright, check_ingest,
           check_whisper, check_cuda, check_rapidocr, check_sherpa,
           check_models, check_summary_config, check_database, check_dirs)


def run_checks() -> dict:
    checks = [check() for check in _CHECKS]
    ok = all(item["ok"] for item in checks if item["severity"] == "error")
    return {"ok": ok, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="抖音转录 Skill 环境体检")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    paths.load_skill_env()
    report = run_checks()
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for item in report["checks"]:
            mark = "OK " if item["ok"] else ("WARN" if item["severity"] == "warning"
                                             else "FAIL")
            print(f"[{mark}] {item['name']}: {item['detail']}"
                  + (f"  修复：{item['fix_command']}" if not item["ok"]
                     and item["fix_command"] else ""))
        print(f"总体：{'就绪' if report['ok'] else '未就绪'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
