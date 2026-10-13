"""看门狗：批次意外中断后自动续跑（由 Windows 计划任务在登录时与每 15 分钟调用）。

由来：2026-09-18 14:37 电脑意外断电，批次进程随之消失；机器 14:39:37 就恢复了，
但没人重新拉起批次，白等 27 分钟才被手工续上。这个脚本把那一步自动化。

它**只续跑已经存在的批次**：没有状态文件、批次已跑完、或处于「等登录」的暂停态时
一律不动，更不会自作主张开一批新的。判定集中在 `decide()`，可单测。

两点刻意的取舍：
- **不做网络探测**。断网时拉起批次只会让条目各失败 3 次（失败条目会在下次开批被
  重排，不丢数据）；而误判「没网」却会让批次一直不恢复——后者的代价大得多，
  所以判定只看本地证据。
- **进程存活靠命令行匹配**，不靠状态文件的新鲜度。单条长音频（如 47 分钟）转写期间
  状态文件可以几十分钟不写一次，用新鲜度当存活信号会把正在跑的批次判成僵尸，
  拉起来就是重复干活。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
STATE_DIR = SKILL_DIR / "state"
STATE_PATH = STATE_DIR / "batch_state.json"
LOG_PATH = STATE_DIR / "logs" / "watchdog.log"
LOCK_PATH = STATE_DIR / "watchdog.lock"

BATCH_SCRIPT = "scripts/batch_transcribe.py"
MAINTENANCE_SCRIPT = "maintenance.py"

# 只要出现这几种状态，就说明这批还没结算完
UNFINISHED_STATUSES = ("pending", "retry_wait", "running", "auth_required")
COMPLETED = "completed"
PAUSED_AUTH = "paused_auth"
PAUSED_RISK = "paused_risk"

# 风控暂停后多久才允许自动续跑。与 batch_transcribe.RISK_RESUME_COOLDOWN 同源：
# 短于它会被批内自己的判定再挡一次，白撞一轮风控请求。
RISK_RESUME_COOLDOWN = 30 * 60
# 锁文件的过期时间：看门狗本身只跑几秒，超过这个时长还挂着说明上次异常退出
LOCK_STALE_SECONDS = 10 * 60
# 续跑前把 in-flight 条目回收成 retry_wait 的窗口。走到这一步时已经确认没有任何
# 批次进程存活，所以这些 running 必然是僵尸，小窗口足够且不会误伤。
RECOVER_STALE_SECONDS = 60
# 续跑频率上限：每次续跑都会让批次重扫一遍收藏夹（打 B站 API），所以崩溃循环
# ——进程反复死、每次都拉起、却毫无进展——必须停下来等人看，不能一直空烧 API。
REVIVE_WINDOW_SECONDS = 60 * 60
MAX_REVIVES_PER_WINDOW = 3
LOG_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(raw) -> datetime | None:
    """解析状态文件里的 ISO 时间戳；解析不出返回 None（调用方按「很久以前」处理）。"""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def unfinished_counts(payload: dict) -> dict[str, int]:
    """按状态统计未结算条目。done / failed 不计——它们已经结算过了。"""
    counts: dict[str, int] = {}
    for item in payload.get("items") or []:
        status = item.get("status")
        if status in UNFINISHED_STATUSES:
            counts[status] = counts.get(status, 0) + 1
    return counts


def decide(payload: dict | None, *, process_alive: bool,
           risk_cooldown: int = RISK_RESUME_COOLDOWN,
           revives_recently: int = 0,
           revive_limit: int = MAX_REVIVES_PER_WINDOW,
           now: datetime | None = None) -> tuple[str, str]:
    """给出 (action, reason)，action 为 'skip' 或 'resume'。只看本地证据。"""
    if process_alive:
        return "skip", "批次进程存活"
    if payload is None:
        return "skip", "没有可用的批次状态，不自动开新批"

    counts = unfinished_counts(payload)
    status = payload.get("status")
    if status == COMPLETED or not counts:
        return "skip", "批次已完成"

    if status == PAUSED_AUTH:
        return "skip", "认证暂停中：等登录后手工 resume-batch"

    if status == PAUSED_RISK:
        paused_at = parse_time(payload.get("paused_at"))
        if paused_at is not None:
            waited = (now or utc_now()) - paused_at
            if waited.total_seconds() < risk_cooldown:
                remain = int(risk_cooldown - waited.total_seconds())
                return "skip", f"风控暂停中：冷却还剩约 {remain} 秒"
        reason = "风控冷却已过，续跑剩余任务"
    else:
        detail = " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
        reason = f"上次运行已中断（{detail}），续跑剩余任务"

    if revives_recently >= revive_limit:
        minutes = REVIVE_WINDOW_SECONDS // 60
        return "skip", (f"最近 {minutes} 分钟已续跑 {revives_recently} 次仍未推进，"
                        f"疑似崩溃循环：停下来等人看 state/logs/watchdog.log")
    return "resume", reason


def batch_processes() -> list[int]:
    """命令行里带 batch_transcribe 的进程 PID。

    venv 的 python.exe 是转发壳，会以同样的参数再起一个子进程，所以这里通常
    会同时命中父子两个 PID——都算「批次活着」，判重时不需要区分。
    """
    if os.name == "nt":
        query = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                 "Where-Object { $_.CommandLine -like '*batch_transcribe*' } | "
                 "Select-Object -ExpandProperty ProcessId")
        try:
            done = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                                  capture_output=True, text=True, timeout=90)
        except (OSError, subprocess.SubprocessError):
            return []
        return [int(token) for token in done.stdout.split() if token.strip().isdigit()]

    try:
        done = subprocess.run(["ps", "-eo", "pid=,args="],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in done.stdout.splitlines():
        if "batch_transcribe" not in line:
            continue
        head = line.strip().split(None, 1)
        if head and head[0].isdigit():
            pids.append(int(head[0]))
    return pids


def lock_age_seconds(path: Path, now: datetime) -> float | None:
    """锁已持有的秒数：优先读锁内记录的时间，读不出再退回 mtime。

    只看 mtime 会把「时钟口径不一致」和「文件系统时间」带进判定；只看锁内时间则
    会漏掉刚 O_EXCL 建好、内容还没落盘的瞬间（那一瞬文件是空的）。两个信号叠起来，
    两种情形都能正确判成「正被持有」。
    """
    held = None
    try:
        held = parse_time(json.loads(path.read_text(encoding="utf-8")).get("at"))
    except (OSError, ValueError):
        held = None
    if held is not None:
        return (now - held).total_seconds()
    try:
        return now.timestamp() - path.stat().st_mtime
    except OSError:
        return None


def acquire_lock(path: Path = LOCK_PATH, *,
                 stale_seconds: int = LOCK_STALE_SECONDS,
                 now: datetime | None = None) -> bool:
    """独占锁。登录触发与定时触发可能同时到点，不锁就可能拉出两个批次。"""
    stamp = now or utc_now()
    if path.is_file():
        age = lock_age_seconds(path, stamp)
        if age is not None and age < stale_seconds:
            return False
        try:
            path.unlink()
        except OSError:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    with os.fdopen(handle, "w", encoding="utf-8") as sink:
        sink.write(json.dumps({"pid": os.getpid(), "at": stamp.isoformat()}))
    return True


def release_lock(path: Path = LOCK_PATH) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def python_bin() -> str:
    """优先用技能自带 venv（装了 faster-whisper 与 OCR 依赖），否则退回当前解释器。"""
    for candidate in (SKILL_DIR / ".venv" / "Scripts" / "python.exe",
                      SKILL_DIR / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def load_payload(path: Path = STATE_PATH) -> dict | None:
    """读状态文件；缺失或损坏都返回 None（看门狗据此不动手）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def recover_stale(python: str) -> int:
    """回收僵尸条目，返回回收条数；解析不出结果时返回 -1（只影响日志，不阻断续跑）。"""
    try:
        done = subprocess.run([python, str(SCRIPT_DIR / MAINTENANCE_SCRIPT),
                               "recover-stale", "--seconds", str(RECOVER_STALE_SECONDS),
                               "--json"],
                              cwd=str(SKILL_DIR), capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return -1
    for line in reversed(done.stdout.strip().splitlines()):
        try:
            return int(json.loads(line).get("recovered", -1))
        except (ValueError, json.JSONDecodeError):
            continue
    return -1


def launch_batch(python: str, *, logs_dir: Path = STATE_DIR / "logs") -> tuple[int, Path]:
    """以独立进程续跑批次，返回 (PID, 日志路径)。

    DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP 让它不随看门狗退出而结束，
    也不会被同一个控制台信号带走。
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"batch_run_{time.strftime('%Y%m%d-%H%M%S')}.log"
    kwargs: dict = {"cwd": str(SKILL_DIR), "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "wb") as sink:
        proc = subprocess.Popen([python, "-u", BATCH_SCRIPT],
                                stdout=sink, stderr=subprocess.STDOUT, **kwargs)
    return proc.pid, log_path


def write_log(message: str, *, path: Path = LOG_PATH, now: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (now or utc_now()).astimezone().strftime(LOG_STAMP_FORMAT)
    with open(path, "a", encoding="utf-8") as sink:
        sink.write(f"[{stamp}] {message}\n")


def parse_log_stamp(text: str) -> datetime | None:
    """解析日志行首的时间戳。

    日志写的是**本地时间**（写给人看的），解析时必须按本地时区解释；当成 UTC 会
    整整偏出一个时区，让「最近一小时的续跑次数」永远算成 0——续跑上限就会形同虚设。
    """
    try:
        naive = datetime.strptime(text.strip(), LOG_STAMP_FORMAT)
    except (ValueError, TypeError):
        return None
    return naive.astimezone()


def recent_revives(path: Path = LOG_PATH, *,
                   window_seconds: int = REVIVE_WINDOW_SECONDS,
                   now: datetime | None = None) -> int:
    """最近 window_seconds 内发生过几次续跑。读不出来就按 0 处理（不阻断正常续跑）。"""
    stamp = now or utc_now()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines:
        if not line.startswith("[") or "已续跑" not in line:
            continue
        when = parse_log_stamp(line[1:line.index("]")] if "]" in line else "")
        if when is None:
            continue
        if (stamp - when).total_seconds() < window_seconds:
            count += 1
    return count


def crash_tail(logs_dir: Path = STATE_DIR / "logs", *, limit: int = 2) -> str:
    """最近一次批次运行的日志末尾——记下「死之前在干什么」，省得事后翻日志。"""
    paths: list[Path] = []
    for candidate in logs_dir.glob("batch_run_*.log"):
        try:
            paths.append(candidate)
        except OSError:
            continue
    for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            lines = [line.strip() for line
                     in path.read_text(encoding="utf-8", errors="replace").splitlines()
                     if line.strip()]
        except OSError:
            continue
        if lines:
            return f"{path.name}: " + " / ".join(lines[-limit:])[:240]
    return "（没有可用的日志尾部）"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="批次看门狗：意外中断后自动续跑")
    parser.add_argument("--state", default=str(STATE_PATH), help="批次状态文件路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印判定结果，不回收僵尸也不续跑")
    args = parser.parse_args(argv)

    state_path = Path(args.state)
    if not acquire_lock():
        write_log("跳过：另一个看门狗在跑（或锁未过期）")
        return 0
    try:
        alive = batch_processes()
        payload = load_payload(state_path)
        revives = recent_revives()
        action, reason = decide(payload, process_alive=bool(alive),
                               revives_recently=revives)
        extra = f"｜存活 PID {', '.join(str(pid) for pid in alive)}" if alive else ""
        write_log(f"判定 {action}：{reason}{extra}")
        if action != "resume":
            return 0
        if args.dry_run:
            write_log("dry-run：不执行回收与续跑")
            return 0
        python = python_bin()
        recovered = recover_stale(python)
        evidence = crash_tail(logs_dir=state_path.parent / "logs")
        pid, log_path = launch_batch(python, logs_dir=state_path.parent / "logs")
        write_log(f"已续跑：回收 {recovered} 个僵尸，新进程 PID {pid}，"
                  f"日志 {log_path.name}｜死前最后输出 → {evidence}")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
