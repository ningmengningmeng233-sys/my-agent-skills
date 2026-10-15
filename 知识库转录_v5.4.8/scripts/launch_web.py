#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键启动器：start / status / stop / restart Web 控制台。

职责：读 .env → 查真实库 → 查 Ollama 精确模型 → 查 5888 端口 →
启动 Web → 轮询 /api/health/ready → ready 后打开浏览器。
PID 文件记录 PID/启动时间/lib/port/版本；stop 先验证命令行确属本技能。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
PID_FILE = SKILL_DIR / ".web.pid"
LOG_FILE = SKILL_DIR / ".web.log"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(str(SKILL_DIR / ".env"))
    except Exception:
        pass


def _cfg():
    import config as v5config
    return v5config.get_config(), v5config


def _lib_dir(cfg) -> Path:
    return Path(cfg.lib_dir)


def _read_pid() -> dict | None:
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _run_bytes(args: list[str], timeout: int = 10) -> str:
    """跑子进程并按 Windows 常见编码宽容解码。"""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout)
        for enc in ("gbk", "utf-8", "gb18030"):
            try:
                return r.stdout.decode(enc)
            except Exception:
                continue
        return r.stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _command_belongs_to_skill(command_line: str) -> bool:
    """命令行必须同时包含 Web 入口和当前技能绝对路径。"""
    if not command_line.strip():
        return False
    normalized = os.path.normcase(command_line).replace("/", "\\")
    skill_path = os.path.normcase(str(SKILL_DIR.resolve())).replace("/", "\\")
    return "start_web.py" in normalized and skill_path in normalized


def _is_our_web(pid: int) -> bool:
    """验证 PID 确属本技能 start_web.py，无法证明归属时保守拒绝。"""
    try:
        if os.name == "nt":
            q = _run_bytes(["powershell", "-NoProfile", "-Command",
                            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"])
            return _command_belongs_to_skill(q)
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            q = f.read().decode("utf-8", errors="replace").replace("\x00", " ")
            return _command_belongs_to_skill(q)
    except Exception:
        return False


LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_KEEP = 3


def _rotate_logs() -> None:
    """日志轮转：.web.log 超 5MB 则轮转为 .web.log.1，最多保留 3 份。"""
    try:
        if not LOG_FILE.exists() or LOG_FILE.stat().st_size < LOG_MAX_BYTES:
            return
        oldest = LOG_FILE.with_name(LOG_FILE.name + f".{LOG_KEEP}")
        try:
            oldest.unlink(missing_ok=True)
        except OSError:
            pass
        for i in range(LOG_KEEP - 1, 0, -1):
            src = LOG_FILE.with_name(LOG_FILE.name + f".{i}")
            dst = LOG_FILE.with_name(LOG_FILE.name + f".{i + 1}")
            try:
                if src.exists():
                    src.replace(dst)
            except OSError:
                pass
        try:
            LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))
        except OSError:
            pass
    except Exception:
        pass


def _poll_ready(base: str, timeout: int = 60) -> dict | None:
    t0 = time.time()
    last: dict = {}
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(base + "/api/health/ready", timeout=5) as r:
                last = json.loads(r.read().decode("utf-8"))
            if last.get("ready"):
                return last
        except Exception as e:
            last = {"error": str(e)[:200]}
        time.sleep(2)
    return None


def cmd_status(cfg) -> int:
    info = _read_pid()
    print(f"技能目录: {SKILL_DIR}")
    print(f"知识库: {cfg.lib_dir}")
    print(f"模型: {cfg.embedding_model} | Ollama: {cfg.ollama_base_url}")
    if not info:
        print("Web 服务：未运行（无 PID 文件）")
        return 1
    pid = int(info.get("pid", 0))
    if _is_our_web(pid):
        build_id = info.get("build_id")
        if not build_id:
            try:
                port = int(info.get("port", cfg.web_port))
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/health/ready", timeout=3) as response:
                    build_id = json.loads(response.read().decode("utf-8")).get("build_id")
            except Exception:
                build_id = None
        print(f"Web 服务：运行中 PID={pid} port={info.get('port')} build={build_id or '?'}")
        return 0
    print(f"Web 服务：PID 文件存在但进程不属于本技能或已退出 PID={pid}")
    return 1


def cmd_start(cfg, v5config, port: int | None) -> int:
    lib = _lib_dir(cfg)
    port = port or cfg.web_port
    print(f"[1/6] 检查真实库: {lib}")
    for sub in ("原文", "段索引", "书导航"):
        if not (lib / sub).exists():
            print(f"❌ 缺少目录: {sub}")
            return 1
    print("[2/6] 检查 Ollama 精确模型...")
    probe = v5config.probe_ollama(cfg.ollama_base_url, cfg.embedding_model, timeout=5)
    if not probe.get("online"):
        print(f"❌ Ollama 离线: {probe.get('error')}")
        return 1
    if not probe.get("model_found"):
        print(f"❌ 模型未精确命中: {cfg.embedding_model}，已装: {probe.get('models')}")
        return 1
    print(f"  ✅ {probe.get('matched_model')} 维度 {probe.get('dimension')}")
    print(f"[3/6] 检查端口 {port}...")
    import socket
    s = socket.socket()
    s.settimeout(2)
    in_use = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    if in_use:
        old = _read_pid()
        if old and _is_our_web(int(old.get("pid", 0))):
            print(f"  服务已运行 PID={old['pid']}，直接使用 http://127.0.0.1:{port}")
            return 0
        print(f"❌ 端口 {port} 被其他程序占用，请先定位占用进程（禁止误杀）")
        return 1
    print(f"[4/6] 启动 Web（lib={lib} port={port}）...")
    _rotate_logs()
    logf = open(str(LOG_FILE), "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(SKILL_DIR / "scripts" / "start_web.py"),
         "--lib", str(lib), "--host", "127.0.0.1", "--port", str(port)],
        stdout=logf, stderr=subprocess.STDOUT,
    )
    PID_FILE.write_text(json.dumps({
        "pid": proc.pid, "started_at": int(time.time()),
        "lib": str(lib), "port": port, "version": v5config.APP_VERSION,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[5/6] 等待 ready（PID={proc.pid}）...")
    ready = _poll_ready(f"http://127.0.0.1:{port}", timeout=90)
    if not ready:
        print("❌ ready 超时，请看日志: .web.log")
        return 1
    pid_info = _read_pid() or {}
    if int(pid_info.get("pid", 0)) == proc.pid:
        pid_info["build_id"] = ready.get("build_id")
        PID_FILE.write_text(
            json.dumps(pid_info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"  ✅ ready state={ready.get('state')} build={ready.get('build_id')}")
    print(f"[6/6] 打开浏览器 http://127.0.0.1:{port}")
    try:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass
    print(f"前台日志: {LOG_FILE}（关闭本终端不影响已启动服务，用 stop 停止）")
    return 0


def cmd_stop() -> int:
    info = _read_pid()
    if not info:
        print("无 PID 文件，无需停止")
        return 0
    pid = int(info.get("pid", 0))
    if not _is_our_web(pid):
        print(f"拒绝：PID {pid} 不属于本技能 start_web.py，禁止误杀，已保留 PID 文件请人工确认")
        return 1
    print(f"正在停止 PID={pid}...")
    try:
        if os.name == "nt":
            # 以前这里写的是 "//F" "//PID"——那是从 Git Bash 里防路径转换的写法被带进了源码，
            # 正常调用下 taskkill 直接报「无效参数」，杀不动却照样往下打印「已停止」并删掉 PID 文件：
            # 服务留在端口上，还失去了唯一的管理凭据。现在按 Windows 正确写法调用并检查结果。
            kill = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                  capture_output=True, text=True, errors="replace", timeout=15)
            if kill.returncode != 0:
                detail = ((kill.stdout or "") + (kill.stderr or "")).strip()[:200]
                print(f"停止失败：taskkill 退出码 {kill.returncode}（{detail}）。"
                      f"已保留 PID 文件，端口可能仍在监听。")
                return 1
        else:
            os.kill(pid, 15)
    except Exception as e:
        print(f"停止失败: {e}")
        return 1
    time.sleep(0.5)
    if _is_our_web(pid):
        print(f"停止未生效：PID {pid} 仍在运行。已保留 PID 文件，请人工确认后再处理。")
        return 1
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    print("已停止")
    return 0


def main() -> int:
    _load_env()
    cfg, v5config = _cfg()
    import argparse
    ap = argparse.ArgumentParser(description="知识库 Web 一键启动器")
    ap.add_argument("action", choices=["start", "status", "stop", "restart"])
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()
    if args.action == "status":
        return cmd_status(cfg)
    if args.action == "stop":
        return cmd_stop()
    if args.action == "start":
        return cmd_start(cfg, v5config, args.port)
    cmd_stop()
    time.sleep(2)
    return cmd_start(cfg, v5config, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
