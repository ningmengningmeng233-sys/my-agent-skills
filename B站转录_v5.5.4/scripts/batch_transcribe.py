#!/usr/bin/env python3

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import NamedTuple

import requests as _requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from dotenv import load_dotenv, dotenv_values

    load_dotenv(str(SKILL_DIR / ".env"))
    _env_file = dotenv_values(str(SKILL_DIR / ".env"))
    for _k in ("OPENAI_API_KEY", "SUMMARY_API_URL", "SUMMARY_API_MODEL"):
        if _env_file.get(_k):
            os.environ[_k] = _env_file[_k]
except ImportError:
    pass

from transcript_db import TranscriptDB  # noqa: E402
from logger import info, success, warn, error  # noqa: E402
import batch_state  # noqa: E402

SCANNER = SCRIPT_DIR / "bilibili_scanner.py"
TRANSCRIPT_SH = SCRIPT_DIR / "bilibili_transcript.sh"
# 单条流水线写库成功后输出的机器可读完成标记（bilibili_transcript.sh 尾部）。
# 批量层只认此标记：阶段性输出（如 Whisper 转写完成）不得判为成功。
SUCCESS_MARKER = "[BILI-DONE]"
FAV_MEDIA_ID = os.environ.get("FAV_MEDIA_ID", "")


def fetch_latest_titles(media_id: str) -> dict[str, str]:
    if not media_id:
        return {}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}
    titles = {}
    page = 1
    while True:
        url = f"https://api.bilibili.com/x/v3/fav/resource/list?media_id={media_id}&ps=20&pn={page}"
        try:
            resp = _requests.get(url, headers=headers, timeout=15)
            data = resp.json()
            if data.get("code") != 0:
                break
            medias = data.get("data", {}).get("medias", [])
            if not medias:
                break
            for item in medias:
                bvid = item.get("bvid", "")
                title = item.get("title", "")
                if bvid and title:
                    titles[bvid] = title
            if data.get("data", {}).get("has_more") is False:
                break
            page += 1
        except Exception:
            break
    return titles


def verify_and_fix_titles() -> int:
    latest = fetch_latest_titles(FAV_MEDIA_ID)
    if not latest:
        print("⚠️ 无法获取收藏夹最新标题，跳过校验")
        return 0

    fixed = 0
    try:
        with TranscriptDB() as db:
            rows = db.conn.execute("SELECT bvid, title FROM transcripts").fetchall()
            for row in rows:
                bvid = row["bvid"]
                db_title = row["title"]
                api_title = latest.get(bvid)
                if api_title and api_title != db_title:
                    db.conn.execute(
                        "UPDATE transcripts SET title = ? WHERE bvid = ?",
                        (api_title, bvid),
                    )
                    db.conn.commit()
                    print(f"   🔧 标题修正: {db_title} → {api_title}")
                    fixed += 1
    except Exception as e:
        print(f"⚠️ 标题校验出错: {e}")
    return fixed


STATE_DIR = SKILL_DIR / "state"
PROCESSED_FILE = STATE_DIR / "processed_videos.txt"
REPORT_FILE = STATE_DIR / "transcript_report.csv"
OUTPUT_DIR = SKILL_DIR / "output"
ARCHIVE_DIR = Path(os.environ.get("BILI_ARCHIVE_DIR") or SKILL_DIR / "转录文件" / "已转录")
MAX_RETRIES = 2
BATCH_DELAY = 3
# 单条任务的合理上限是 2 小时（subprocess timeout），超过即视为进程被杀留下的僵尸。
STALE_HEARTBEAT_SECONDS = 2 * 60 * 60 + 300
# 命中 B站风控时的整批退避梯度（秒）。先等 10 分钟给限速窗口自然解除的机会，
# 不解再等 20 分钟；仍未解除才暂停整批等人工介入。为什么不做成「每条各自重试」：
# 风控窗口是账号级的，90 多条各自撞墙只会把窗口撑得更久。
RISK_COOLDOWNS = (10 * 60, 20 * 60)
# 风控暂停后再次启动批次时的自动恢复门槛：退避满这么久就自己继续，
# 免得半夜被一个已经自愈的限速卡住整晚。
RISK_RESUME_COOLDOWN = 30 * 60

STATE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


_BASH_CANDIDATES = [
    Path(r"C:/Program Files/Git/bin/bash.exe"),
    Path(r"C:/Program Files (x86)/Git/bin/bash.exe"),
    Path.home() / "AppData/Local/Programs/Git/bin/bash.exe",
    Path(r"C:/msys64/usr/bin/bash.exe"),
]


def find_bash() -> str:
    found = shutil.which("bash")
    if found:
        return found
    for candidate in _BASH_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    return "bash"


BASH = find_bash()


# 认证失效的强信号。注意不能把脚本常规的「[WARN] 无可用Cookie」算进来——
# 那条在没配 Cookie 时每次都会打印，而流水线会退回 Whisper 正常出稿；
# 若据此暂停，没登录的用户将永远跑不了批量。
_AUTH_PATTERNS = (
    r"HTTP Error 412",
    r"Precondition Failed",
    r"需要登录",
    r"请先登录",
    r"登录已过期",
    r"log ?in to continue",
    r"sign in to confirm",
    r"风控校验",
)


def detect_auth_failure(text: str | None) -> bool:
    """判断输出是否表明登录态失效（需要人工重新登录而非重试）。"""
    if not text:
        return False
    return any(re.search(pattern, text, re.IGNORECASE)
               for pattern in _AUTH_PATTERNS)


# B站风控/限流的信号。与「登录态失效」分开：风控是限速窗口，等一段时间多半自己好，
# 而登录态失效必须人工处理。412 是 B站 风控的标准响应（Cookie 过期或请求过密），
# 触发的表现与登录失效高度重合，因此两套模式各自保留、由调用方决定优先判定哪一个。
_RISK_PATTERNS = (
    r"HTTP Error 412",
    r"Precondition Failed",
    r"\[BILI-META-FAIL\][^\n]*reason=risk_control",
    r"风控",
    r"请求过于频繁",
    r"code\"?\s*:\s*-412",
    r"code\"?\s*:\s*-352",
)


def detect_risk_control(text: str | None) -> bool:
    """判断输出是否表明被 B站 风控/限流（应整批退避，而不是连续重试）。"""
    if not text:
        return False
    return any(re.search(pattern, text, re.IGNORECASE)
               for pattern in _RISK_PATTERNS)


def risk_cooldowns() -> tuple[int, ...]:
    """风控退避梯度（秒）；可用 BILI_RISK_COOLDOWNS="600,1800" 覆盖。"""
    raw = (os.environ.get("BILI_RISK_COOLDOWNS") or "").strip()
    if not raw:
        return RISK_COOLDOWNS
    parsed = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            value = int(float(piece))
        except ValueError:
            continue
        if value >= 0:
            parsed.append(value)
    return tuple(parsed) if parsed else RISK_COOLDOWNS


def risk_resume_cooldown() -> int:
    try:
        return max(0, int(float(os.environ.get("BILI_RISK_RESUME_COOLDOWN")
                               or RISK_RESUME_COOLDOWN)))
    except (TypeError, ValueError):
        return RISK_RESUME_COOLDOWN


def _sleep_with_countdown(seconds: float, *, chunk: float = 60.0) -> None:
    """分片睡眠：长时间退避期间仍能被打断，也能看到还剩多久。"""
    remaining = float(seconds)
    while remaining > 0:
        step = min(chunk, remaining)
        time.sleep(step)
        remaining -= step
        if remaining > 0:
            print(f"      …还需等待 {int(remaining // 60)} 分"
                  f"{int(remaining % 60)} 秒", flush=True)


def _seconds_since(stamp: str) -> float | None:
    """距给定 ISO 时间戳（UTC）已过去多少秒；解析失败返回 None。"""
    try:
        moment = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


def resolve_risk_pause(state) -> bool:
    """处理「风控暂停」的批次状态：退避满了就自动恢复。返回 True 表示可以继续跑。

    风控是限速窗口，不是登录态失效——退避够久通常自己就好，没必要非等人来点一次。
    未满退避时返回 False，由调用方以退出码 2 结束（不领取任何任务，也就不会继续撞墙）。
    """
    waited = _seconds_since(state.paused_at)
    cooldown = risk_resume_cooldown()
    if waited is not None and waited >= cooldown:
        print(f"\n▶️  风控退避已满 {int(cooldown // 60)} 分钟"
              f"（暂停原因：{state.pause_reason or '风控/限流'}），自动恢复批次")
        state.resume()
        return True
    remain = ("" if waited is None
              else f"，还需等待约 {max(0, int((cooldown - waited) // 60))} 分钟")
    print(f"\n⏸️  批次因 B站风控暂停"
          f"（原因：{state.pause_reason or '风控/限流'}，"
          f"暂停于 {state.paused_at or '未知时间'}）。")
    print(f"   等待 {int(cooldown // 60)} 分钟退避后重跑本脚本会自动继续{remain}；")
    print("   也可以确认已登录 bilibili.com 后手动恢复：")
    print("     .venv/Scripts/python.exe scripts/maintenance.py resume-batch")
    return False


def job_result_fields() -> tuple[str, ...]:
    """`run_job` 返回元组的字段名，供编排层与测试共用。"""
    return ("ok", "output_file", "transcript_source", "attempts", "failure_code")


class JobResult(NamedTuple):
    ok: bool
    output_file: str | None
    transcript_source: str | None
    attempts: int
    failure_code: str = ""


def _state_covers_pending(state, pending: list[dict]) -> bool:
    """上次的状态能否复用于本次待跑清单。

    只要求「本次待跑的每个 key 都在状态里」——上次留在状态里但本次不再待跑的
    条目（已归档的会被 processed 台账过滤掉）不影响复用，保留它们即可看到历史。
    """
    known = {item.get("key") for item in state.items}
    wanted = {processed_key(v["bvid"], v.get("page", 1)) for v in pending}
    return wanted.issubset(known)


def _requeue_previously_failed(state, pending: list[dict]) -> int:
    """把「上次失败但本次仍在待跑清单里」的条目转回可领取。

    没有这一步会静默漏跑：`failed` 不可领取，而状态复用会让它停在 failed 上，
    于是重跑批量时这一条被跳过——改造前的行为是「失败项下次会重试」。
    只对仍在 pending 清单里的 key 生效，避免复活已被台账排除的旧条目。
    """
    wanted = {processed_key(v["bvid"], v.get("page", 1)) for v in pending}
    requeued = 0
    for item in state.items:
        if item.get("key") not in wanted:
            continue
        if item.get("status") == batch_state.FAILED:
            item["status"] = batch_state.RETRY_WAIT
            item["failure_code"] = ""
            requeued += 1
    if requeued:
        state.save()
    return requeued


def processed_key(bvid: str, page: int = 1) -> str:
    page = int(page or 1)
    return bvid if page == 1 else f"{bvid}_P{page}"


def load_processed() -> set[str]:
    if not PROCESSED_FILE.exists():
        return set()
    return set(
        line.strip()
        for line in PROCESSED_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def save_processed(bvid: str, page: int = 1) -> None:
    with PROCESSED_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{processed_key(bvid, page)}\n")


def is_processed(processed: set[str], bvid: str, page: int = 1) -> bool:
    page = int(page or 1)
    if processed_key(bvid, page) in processed:
        return True
    return page == 1 and f"{bvid}_P1" in processed


def get_content_hash(filepath: str) -> str:
    path = Path(filepath)
    if not path.exists():
        return ""
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            h.update(f.read(65536))
        return h.hexdigest()[:16]
    except OSError:
        return ""


def scan_videos() -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCANNER)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(SKILL_DIR),
    )
    if result.returncode != 0:
        print(f"Scanner error: {result.stderr}")
        return []

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        print(f"Scanner 输出解析失败: {result.stdout[:200]}")
        return []

    if data.get("error"):
        print(f"Scanner 错误: {data['error']}")
        return []

    print(
        f"📊 收藏夹共 {data.get('collection_total', 0)} 个，"
        f"已处理 {data.get('processed', 0)} 个"
        f"（分P展开: {'开' if data.get('expand_pages') else '关'}）"
    )
    return data.get("new_videos", [])


def _extract_output_file(stdout: str) -> str | None:
    for line in stdout.splitlines():
        candidate = line.strip()
        if not (candidate.endswith(".txt") and ("/" in candidate or "\\" in candidate)):
            continue
        if candidate.startswith("📄"):
            continue
        if Path(candidate).is_file():
            return candidate
    return None


def transcribe_video(bvid: str, page: int = 1, attempt: int = 1, max_retries: int = 1, work_dir: Path | None = None):
    page = int(page or 1)
    url = f"https://www.bilibili.com/video/{bvid}/"
    if page > 1:
        url = f"https://www.bilibili.com/video/{bvid}/?p={page}"
    key = processed_key(bvid, page)
    print(f"\n{'=' * 70}")
    print(f"🎬 开始转录: {key} (尝试 {attempt}/{max_retries})")
    print(f"{'=' * 70}")

    work_dir = work_dir or OUTPUT_DIR
    env = os.environ.copy()
    env["BILI_OUTPUT_DIR"] = str(work_dir)
    result = subprocess.run(
        [BASH, str(TRANSCRIPT_SH), url, str(work_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(SKILL_DIR),
        env=env,
        timeout=7200,
    )

    if result.stdout:
        print(result.stdout[-2000:])
    if result.stderr:
        stderr_preview = result.stderr.strip()[-500:]
        if stderr_preview:
            print(f"STDERR: {stderr_preview}")

    used_stt = "Whisper" in result.stdout or "语音转文字" in result.stdout

    if SUCCESS_MARKER in result.stdout:
        saved_file = _extract_output_file(result.stdout)
        transcript_source = "unknown"
        try:
            with TranscriptDB() as db:
                record = db.get_by_bvid(bvid, page)
                if record:
                    transcript_source = record.get("transcript_source", "unknown")
                    if not saved_file or saved_file == "unknown":
                        saved_file = record.get("transcript_file") or None
        except Exception as e:
            warn("batch_transcribe", f"DB读取失败（不影响转录结果）: {e}")
        return True, saved_file or "unknown", transcript_source, used_stt, ""

    error_msg = result.stdout[-300:] if result.stdout else "无输出"
    # 风控与认证失效都要单独定性，且风控优先：412 在两套模式里都命中，
    # 但处理方式不同（风控先退避，认证只能人工）。
    if detect_risk_control(result.stdout) or detect_risk_control(result.stderr):
        return False, error_msg, None, used_stt, "risk_control"
    # 认证失效要单独定性：编排层据此暂停整批，而不是把它当成普通失败重试。
    if detect_auth_failure(result.stdout) or detect_auth_failure(result.stderr):
        return False, error_msg, None, used_stt, "login_required"
    return False, error_msg, None, used_stt, ""


def run_job(video: dict) -> JobResult:
    bvid = video["bvid"]
    page = int(video.get("page") or 1)
    work_dir = OUTPUT_DIR / "_work" / f"{processed_key(bvid, page)}_{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = source = None
    try:
        max_attempts = MAX_RETRIES + 1
        for attempt in range(1, max_attempts + 1):
            ok, output_path, source, used_stt, failure_code = transcribe_video(
                bvid, page, attempt, max_attempts, work_dir
            )
            if ok:
                return JobResult(True, output_path, source, attempt)
            if failure_code in ("login_required", "risk_control"):
                # 重试不会让登录态或风控窗口恢复；立刻交给编排层统一退避或暂停。
                return JobResult(False, output_path, None, attempt, failure_code)
            if used_stt:
                return JobResult(False, output_path, source, attempt,
                                 "transcribe_failed")
            if attempt <= MAX_RETRIES:
                time.sleep(BATCH_DELAY * attempt)
        return JobResult(False, output_path, source, max_attempts,
                         "transcribe_failed")
    except Exception as exc:
        return JobResult(False, str(exc), None, 1, "exception")


def _versioned_destination(destination: Path) -> Path:
    """为同名冲突挑选下一个可用的 `_vN` 文件名（不覆盖任何既有文件）。"""
    for index in range(2, 100):
        candidate = destination.with_name(
            f"{destination.stem}_v{index}{destination.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"归档目标冲突过多: {destination}")


def archive_transcript(path: str, bvid: str, page: int) -> str:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(path)
    with TranscriptDB() as db:
        record = db.get_by_bvid(bvid, page)
        date = (record or {}).get("upload_date") or ""
    parts = date.split("-")
    folder = ARCHIVE_DIR / parts[0] / parts[1] if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() else ARCHIVE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / source.name
    if destination.exists() and destination.read_bytes() != source.read_bytes():
        # 同名不同内容：多为"重转录产出新稿"或"人工改过副本"。
        # 不覆盖、不报错，改写成不带歧义的版本化文件名（_v2、_v3…），旧文件保留。
        destination = _versioned_destination(destination)
    if destination != source:
        if not destination.exists():
            shutil.move(str(source), str(destination))
        else:
            source.unlink()
    with TranscriptDB() as db:
        db.conn.execute(
            "UPDATE transcripts SET transcript_file=? WHERE bvid=? AND page=?",
            (str(destination), bvid, page),
        )
        db.conn.commit()
    work_root = (OUTPUT_DIR / "_work").resolve()
    try:
        relative = source.resolve().relative_to(work_root)
        work_folder = work_root / relative.parts[0]
        if work_folder.is_dir():
            shutil.rmtree(work_folder)
    except ValueError:
        pass
    return str(destination)


def _work_root(output_dir: Path | None = None) -> Path:
    return Path(output_dir or OUTPUT_DIR) / "_work"


def finalize_delivery_cleanup(output_dir: Path | None = None) -> int:
    """交付收尾：清空临时媒体目录 `<output>/_work`。

    视频、音频、分离音轨与帧图都是临时资产，交付成功后立即删除（对齐
    SKILL.md 承诺）。只删 `_work` 下的内容，绝不触碰交付物（OUTPUT_DIR 根、
    归档目录、数据库、`_work` 目录本身）；幂等，目录不存在返回 0。返回删除的顶层条目数。
    """
    work_root = _work_root(output_dir)
    if not work_root.is_dir():
        return 0
    removed = 0
    for entry in work_root.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(description="B站收藏夹批量转录（分P支持）")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="只处理前 N 个待转录任务（0=全部；用于小批量验收）",
    )
    parser.add_argument("--workers", type=int, default=2, choices=(1, 2), help="并行槽数，默认 2")
    args = parser.parse_args(argv)

    if BASH == "bash" and not shutil.which("bash"):
        print(
            "❌ 未找到 bash（Git for Windows）。"
            "请安装 Git，或从 Git Bash 中运行本脚本。"
        )
        error("batch_transcribe", "未找到 bash，无法调用 bilibili_transcript.sh")
        return 1

    print("=" * 70)
    print("📼 B站收藏夹批量转录 v2.2（分P支持）")
    print("=" * 70)
    info("batch_transcribe", "开始批量转录任务")

    videos = scan_videos()
    if not videos:
        print("没有新视频需要转录")
        info("batch_transcribe", "没有新视频，结束")
        return 0

    processed = load_processed()
    pending_by_key = {
        processed_key(v["bvid"], v.get("page", 1)): v
        for v in videos
        if not is_processed(processed, v["bvid"], v.get("page", 1))
    }
    pending = list(pending_by_key.values())
    total = len(videos)
    remaining = len(pending)

    print(f"\n📊 总计 {total} 个视频（含分P）")
    print(f"✅ 已处理 {total - remaining} 个")
    print(f"⏳ 待处理 {remaining} 个")

    if args.limit and args.limit > 0:
        pending = pending[: args.limit]
        remaining = len(pending)
        print(f"🔬 限制模式：本次只处理前 {remaining} 个任务")

    if remaining == 0:
        print("🎉 全部视频已转录完成！")
        return 0

    ai_mode = (
        "已启用（转录时自动生成）"
        if os.environ.get("OPENAI_API_KEY")
        else "未启用（设置 OPENAI_API_KEY 可开启）"
    )
    print(f"📝 AI摘要生成: {ai_mode}")

    start_time = time.time()
    success_count = 0
    fail_count = 0
    report_rows = []

    # 批次控制面：记录逐条状态，使 Cookie 失效能暂停而非空跑，
    # 也让进程被杀之后能靠心跳识别出「跑到一半」的条目。
    batch_id = time.strftime("%Y%m%d-%H%M%S")
    state_path = batch_state.default_path(STATE_DIR)
    state = batch_state.BatchState.load(state_path)

    # 僵尸回收必须作用在「上次留下的」状态上：先回收，再判断能否复用。
    # （顺序反了的话，刚建的新批次里没有任何 running 条目，回收永远无事可做。）
    if state is not None:
        recovered = state.recover_stale(seconds=STALE_HEARTBEAT_SECONDS)
        if recovered:
            print(f"♻️  回收 {recovered} 个心跳超时的任务（上次运行中断）")

    if state is not None and state.status == batch_state.BATCH_PAUSED_AUTH:
        print("\n⏸️  检测到未恢复的认证暂停。请先登录 bilibili.com，然后运行：")
        print("     .venv/Scripts/python.exe scripts/maintenance.py resume-batch")
        print("   再重新执行本脚本继续。")
        return 2

    if state is not None and state.status == batch_state.BATCH_PAUSED_RISK:
        if not resolve_risk_pause(state):
            return 2

    # 复用未跑完的批次状态（保留已 done 的进度），否则按本次待跑清单新建。
    if state is None or not _state_covers_pending(state, pending):
        state = batch_state.BatchState.create(
            path=state_path, batch_id=batch_id,
            items=[{"key": processed_key(v["bvid"], v.get("page", 1)),
                    "bvid": v["bvid"], "page": int(v.get("page") or 1)}
                   for v in pending])
    else:
        # 复用旧状态时，上次失败的条目仍应在本次重试（与改造前的行为一致）。
        requeued = _requeue_previously_failed(state, pending)
        if requeued:
            print(f"🔁 重新排入 {requeued} 个上次失败的任务")

    # Apply any legacy DB migration once before the worker processes open it.
    with TranscriptDB():
        pass
    # 逐个领取而非一次性提交：认证失效能立刻停下，未开始的任务不会被白跑。
    executor = ThreadPoolExecutor(max_workers=args.workers)
    paused = False
    index = 0
    risk_waits_used = 0
    try:
        while True:
            item = state.next_pending()
            if item is None:
                break
            key = item["key"]
            v = next((candidate for candidate in pending
                      if processed_key(candidate["bvid"],
                                       candidate.get("page", 1)) == key), None)
            if v is None:
                state.mark(key, batch_state.DONE)
                continue
            index += 1
            bvid = v["bvid"]
            page = int(v.get("page") or 1)
            part_title = v.get("part_title") or ""
            current_remaining = remaining - index + 1

            elapsed = time.time() - start_time if index > 1 else 0
            if elapsed > 0 and (index - 1) > 0:
                avg_time = elapsed / (index - 1)
                eta = avg_time * current_remaining
                print(
                    f"\n⏱️  已用: {int(elapsed // 60)}分{int(elapsed % 60)}秒"
                    f" | 预计剩余: {int(eta // 60)}分{int(eta % 60)}秒"
                )

            label = f"P{page}" + (f" · {part_title}" if part_title else "")
            print(f"\n📌 [{index}/{remaining}] {v['title']} {label}")
            print(f"   ⏱️  {v['duration']} | 👤 {v['upper']}")

            state.claim(key)
            job_result = executor.submit(run_job, v).result()
            ok = job_result.ok
            output_file = job_result.output_file
            transcript_source = job_result.transcript_source
            attempt = job_result.attempts

            if ok and output_file and output_file != "unknown":
                try:
                    output_file = archive_transcript(output_file, bvid, page)
                except (OSError, ValueError) as exc:
                    print(f"   ❌ 归档失败: {exc}")
                    ok = False

            base_row = {
                "bvid": bvid,
                "page": page,
                "cid": v.get("cid") or "",
                "part_title": part_title,
                "title": v.get("title", ""),
                "author": v.get("upper", ""),
                "duration": v.get("duration", ""),
            }

            if ok and output_file and output_file != "unknown":
                report_rows.append(
                    {
                        **base_row,
                        "source": transcript_source or "unknown",
                        "output_file": output_file,
                        "content_hash": get_content_hash(output_file),
                        "status": "success",
                        "attempts": attempt,
                    }
                )
                success_count += 1
                save_processed(bvid, page)
                state.mark(key, batch_state.DONE)
                print(
                    f"   ✅ [{success_count}/{remaining}] 成功! 来源: {transcript_source}"
                )
                success("batch_transcribe", f"转录成功: {key} ({transcript_source})")
            else:
                report_rows.append(
                    {
                        **base_row,
                        "source": "失败",
                        "output_file": "",
                        "content_hash": "",
                        "status": f"failed_after_{attempt}_attempts",
                        "attempts": attempt,
                    }
                )
                fail_count += 1
                if job_result.failure_code == "risk_control":
                    cooldowns = risk_cooldowns()
                    if risk_waits_used < len(cooldowns):
                        wait = cooldowns[risk_waits_used]
                        risk_waits_used += 1
                        state.mark(key, batch_state.RETRY_WAIT,
                                   failure_code="risk_control")
                        print(f"   ⏳ 疑似 B站风控：整批退避 "
                              f"{int(wait // 60)} 分钟后自动重试"
                              f"（第 {risk_waits_used}/{len(cooldowns)} 次）")
                        warn("batch_transcribe",
                             f"风控退避 {wait}s 后重试: {key}")
                        _sleep_with_countdown(wait)
                        continue
                    state.pause_for_risk(key, reason="B站风控/限流持续未解除")
                    paused = True
                    print("   ⏸️  风控未解除：已暂停整批，剩余任务保留待跑")
                    print("       建议等待 30 分钟以上（或确认浏览器已登录 "
                          "bilibili.com）后执行：")
                    print("         .venv/Scripts/python.exe "
                          "scripts/maintenance.py resume-batch")
                    error("batch_transcribe", f"风控暂停批次于 {key}")
                    break
                if job_result.failure_code == "login_required":
                    state.pause_for_auth(key)
                    paused = True
                    print("   ⏸️  登录态失效：已暂停整批，剩余任务保留待跑")
                    print("       登录 bilibili.com 后执行 "
                          "scripts/maintenance.py resume-batch 再续跑")
                    error("batch_transcribe",
                          f"登录态失效，批次暂停于 {key}")
                    break
                state.mark(key, batch_state.FAILED,
                           failure_code=job_result.failure_code)
                print(f"   ❌ [{fail_count}] 失败 (尝试{attempt}次后放弃)")
                error("batch_transcribe", f"转录失败: {key} (尝试{attempt}次)")

    finally:
        executor.shutdown(wait=True)

    if not paused and state.next_pending() is None:
        state.status = batch_state.BATCH_COMPLETED
        state.save()

    total_time = time.time() - start_time
    print(f"\n{'=' * 70}")
    print("📊 批量转录完成")
    print(f"{'=' * 70}")
    print(f"   总计: {remaining} 个")
    print(f"   成功: {success_count} 个 ✅")
    print(f"   失败: {fail_count} 个 {'❌' if fail_count else '✅'}")
    print(f"   耗时: {int(total_time // 60)}分{int(total_time % 60)}秒")
    info(
        "batch_transcribe",
        f"批量转录完成: 成功 {success_count}, 失败 {fail_count}, "
        f"耗时 {int(total_time // 60)}分{int(total_time % 60)}秒",
    )

    if report_rows:
        fieldnames = [
            "bvid",
            "page",
            "cid",
            "part_title",
            "title",
            "author",
            "duration",
            "source",
            "output_file",
            "content_hash",
            "status",
            "attempts",
        ]
        with REPORT_FILE.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(report_rows)
        print(f"   📄 报告已保存: {REPORT_FILE}")

        sources = {}
        for row in report_rows:
            if row["status"] == "success":
                sources[row["source"]] = sources.get(row["source"], 0) + 1
        print("\n   📝 转录来源分布:")
        for src, count in sorted(sources.items(), key=lambda x: -x[1]):
            print(f"      - {src}: {count} 个")

    if fail_count:
        print("\n   ❌ 失败列表:")
        for row in report_rows:
            if row["status"] != "success":
                print(
                    f"      - {processed_key(row['bvid'], row['page'])} {row['title']}"
                )

    print("\n🔍 校验标题一致性...")
    fixed_titles = verify_and_fix_titles()
    if fixed_titles:
        print(f"   🔧 修正了 {fixed_titles} 条标题")
    else:
        print("   ✅ 所有标题一致")

    # 交付收尾：临时媒体（视频/音频/帧图）用完即删，对齐 SKILL.md「交付成功后立即删除」。
    # 暂停待续跑时保留 _work（下载可能中断，续跑要复用），不在此清理。
    if not paused:
        cleaned = finalize_delivery_cleanup()
        if cleaned:
            print(f"\n🧹 已清理临时媒体目录 output/_work（{cleaned} 项）")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
