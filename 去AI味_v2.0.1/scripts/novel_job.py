#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""novel_job.py - 整本书的任务目录、源锁与恢复（v1.2 Task 6）。

设计方案「性能与恢复合同」（第 76-80 行）要求：整本书要跨很多次会话，所以每个任务必须
有一个私有、可恢复、可审计的工作区。本模块是那个工作区的唯一实现，落实十一条：

  1. 工作区固定在原稿旁的 ``.<原文件名>.lieflat-work/jobs/<短 job-id>/``；不写进 Skill
     包目录（开发仓与安装目录同形），不与开发仓库的 ``.lemon/`` 台账混用，也不落宿主
     ``.codex/.agents/.claude/.qoder`` 的 skills 安装区。
  2. 同一原稿默认只允许一个活动任务（``second_active_job`` 拒绝）；不同原稿各自工作区，
     可以并行。这条保证由工作区根上的**源级排他认领**（``O_EXCL`` 创建，文件名取自源
     哈希）强制：先认领、再扫目录，创建失败或被拒当场收回，交付/清理时才释放——「扫一遍
     再建」的先后检查竞态窗口由此关掉，认领读不出时按「证明不了主人已退出」拒绝。
  3. 锁记 job ID、PID、开始时间、源哈希；陈旧锁只在「持有者被证明已退出」**且**源快照
     仍然一致时才清（清锁先于重审判定，但源核对在前）。
  4. 状态机 created / segmented / rewritten / reviewed / published / failed：不许跳步、
     不许回退；重复进入 failed 一律拒绝（否则恢复起点会被写成 ``failed`` 而把任务永久
     楔死）；从 failed 只能回到「失败的那一步」或它的下一步；状态记录里的恢复起点被改坏
     时也只冒 ``JobError``，绝不冒 ``KeyError``。全部落盘走 ``os.replace``。
  5. 源、package、力度、规则版本变化一律 ``refuse``（拒绝静默续跑）；只有模型变化返回
     ``re-review``，并把「已完成章节边界必须重审」记进状态，直到推进到 reviewed 才解除。
  6. 恢复时源哈希**重新算**：``read_source`` 取当前快照，``drift_fields`` 再独立复核一次
     字节与 stat，绝不信任清单里记住的那个值。原稿本身只读，不改、不删。
  7. 已发布任务的恢复是幂等的：交付记录按同一规则**重推导**通过就返回 ``complete`` 且一个
     字节都不写，判定排在源与身份核对之前（原稿后来漂移也不该卡在 ``refuse`` 永不收尾）；
     记录推导不出合法目标则 ``refuse``，绝不据坏记录宣称完成；``advance_state`` 在
     published 之后一律拒绝，防止二次发布。
  8. 创建前检查 Windows 路径长度与目录可写性；过长直接拒绝并提示显式改选更短的
     ``workspace``（不落半个目录，失败即回滚已建的空目录）。
  9. 成功后 ``cleanup_job`` 删临时正文候选、留任务清单/哈希/报告；失败或取消时状态里
     给出 ``recovery`` 与显式清理入口（``cleanup_job(keep_evidence=False)``）。
  10. 推进状态与清理同纪律：**调用方必须是任务锁持有者**（非持有者、锁损坏、无锁都
      ``lock_held`` 拒绝），第三方进程不能移动或发布别人正在做的任务。
  11. 进产物的自由文本必须有形状与长度上限：目标文件名必须保持默认交付形状
      ``<原文件名>.去AI味-<力度>[.编号].<扩展名>``，``rules_version`` 与 ``model_id``
      受短标签上限约束且不得含控制字符；超长或成段一律 ``JobError`` 拒绝，绝不截断。

隐私是正向设计而不是事后过滤：产物里只写摘要、状态码与相对定位。``SourceSnapshot.to_dict()``
含 ``requested_path`` / ``resolved_path``，所以本模块**不整份落盘**它，只摘取无路径字段
（见 ``_source_record``）；原稿位置由工作区形状复原，``resume_job`` 不传 ``source`` 也能
找回原稿，清单里因此不需要绝对路径。

不做什么（归后续任务）：发布与覆盖通道、mixed-newline 授权绑定、``file_commit`` 接线
（Task 12）；分段与候选正文的产生（Task 7/10）；账本与保护清单（Task 8）。本模块交出
状态机、清单、锁、清理入口，以及 ``candidates/`` 这个目录合同给上面各任务写正文候选。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath, PurePosixPath
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from text_source import (  # noqa: E402
    CHANNEL_OVERWRITE,
    WINDOWS_MAX_PATH,
    SourceInputError,
    drift_fields,
    read_source,
)
from tool_paths import AGENT_HOME_DIRS  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent

# ---- 目录与产物名（brief 固定形状） ----------------------------------------
WORKSPACE_SUFFIX = ".lieflat-work"
JOBS_DIRNAME = "jobs"
CANDIDATES_DIRNAME = "candidates"        # 临时正文候选；成功清理时整目录删除
MANIFEST_NAME = "manifest.json"
STATE_NAME = "state.json"
LOCK_NAME = "lock.json"
EVENTS_NAME = "events.jsonl"
LEDGER_NAMES = (MANIFEST_NAME, STATE_NAME, LOCK_NAME, EVENTS_NAME)
MANIFEST_SCHEMA = "lieflat-novel-job-manifest-v1"
STATE_SCHEMA = "lieflat-novel-job-state-v1"
JOB_ID_HEX_CHARS = 6                    # 12 个十六进制字符，短到能用完路径长度预算

STRENGTHS: tuple[str, ...] = ("conservative", "balanced", "deep")
STATES: tuple[str, ...] = ("created", "segmented", "rewritten", "reviewed", "published",
                           "failed")
_RANK = {name: index for index, name in enumerate(STATES) if name != "failed"}
LEDGER_DIRNAME = ".lemon"                # 开发仓库台账目录，运行期工作区不得落进去
SKILLS_DIRNAME = "skills"                # 宿主 Skill 安装区的固定父目录名
_SHA_RE = re.compile(r"[0-9a-fA-F]{64}")
_HEX_RE = re.compile(r"[0-9a-fA-F]+")

# ---- 入册字段的形状与长度上限：超限一律拒绝，绝不截断 --------------------
MAX_SHORT_FIELD_CHARS = 64          # rules_version / model_id 这类短标签字段
MAX_OUTPUT_NAME_CHARS = 160         # 目标文件名整体（要留够工作区的路径预算）
MAX_OUTPUT_STEM_CHARS = 120         # 目标文件名的主干部分
# 安全交付合同的默认交付名：<原文件名>.去AI味-<中文力度>[.或-冲突编号].<原扩展名>
_OUTPUT_NAME_RE = re.compile(
    r"[^\x00-\x1f/\\]{1,%d}\.去AI味-(?:保守|平衡|深度)(?:[.-][0-9]{1,4})?"
    r"\.[A-Za-z0-9][A-Za-z0-9_-]{0,15}" % MAX_OUTPUT_STEM_CHARS)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
CLAIM_INFIX = ".source-"            # 源级排他认领文件：落在工作区根
CLAIM_SUFFIX = ".claim.lock"
CLAIM_HASH_CHARS = 16               # 认领名只取源哈希前缀，够唯一又不至于撑路径

# ---- 错误码：进日志、进 CLI，永不进正文 ------------------------------------
CODE_BAD_IDENTITY = "bad_identity"
CODE_FORBIDDEN_LOCATION = "forbidden_location"
CODE_PATH_TOO_LONG = "path_too_long"
CODE_WORKSPACE_UNRELATED = "workspace_unrelated"
CODE_WORKSPACE_NOT_WRITABLE = "workspace_not_writable"
CODE_SECOND_ACTIVE_JOB = "second_active_job"
CODE_UNKNOWN_JOB = "unknown_job"
CODE_BAD_STATE = "bad_state"
CODE_BAD_TRANSITION = "bad_transition"
CODE_ALREADY_PUBLISHED = "already_published"
CODE_BAD_PUBLISH_RECORD = "bad_publish_record"
#: 逐文件提交账本（v2 P13）的形状闸门：字段缺、哈希不合形状一律拒写。
CODE_BAD_COMMIT_RECORD = "bad_commit_record"
CODE_LOCK_HELD = "lock_held"
# resume 的拒绝原因（只记码与字段名，不记异常原文）
CODE_SOURCE_UNREADABLE = "source_unreadable"
CODE_SOURCE_DRIFT = "source_drift"
CODE_SOURCE_UNSTABLE = "source_unstable"
CODE_SOURCE_MISMATCH = "source_mismatch"
CODE_IDENTITY_CHANGED = "identity_changed"


class JobError(RuntimeError):
    """任务层的拒绝与失败。``code`` 是给机器读的码，``details`` 只放摘要与目录名。"""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details)

    def __str__(self) -> str:  # pragma: no cover - 只影响打印形状
        return f"[{self.code}] {self.message}"


@dataclass(frozen=True)
class JobIdentity:
    """一次任务的身份（brief 规定的八个字段，字段序即合同）。"""

    job_id: str
    source_sha256: str
    source_size: int
    source_mtime_ns: int
    strength: Literal["conservative", "balanced", "deep"]
    package_sha256: str
    rules_version: str
    model_id: str


# ---------------------------------------------------------------------------
# 小工具：路径、时间、原子落盘
# ---------------------------------------------------------------------------
def workspace_dir_for(source) -> Path:
    """原稿旁的工作区根：``.<原文件名>.lieflat-work``（点前缀让它默认隐藏）。"""
    src = Path(source)
    return src.parent / ("." + src.name + WORKSPACE_SUFFIX)


def _absolute(path) -> Path:
    return Path(os.path.abspath(str(path)))


def _resolved(path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:  # pragma: no cover - 仅极端文件系统状态下会走到
        return _absolute(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_text(payload) -> str:
    # sort_keys：产物字节可比较，幂等恢复用例能真的比出「一个字节都没变」
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_write(path: Path, payload) -> None:
    """同目录临时文件加 ``os.replace``：要么完整换掉，要么原字节不动。"""
    text = _json_text(payload)
    tmp = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise JobError(CODE_WORKSPACE_NOT_WRITABLE,
                       f"任务产物写入失败，临时文件已撤回（{path.name}）：{exc}") from exc


def _append_event(job_dir: Path, event: str, *, code=None, detail=None) -> None:
    """只追加事实：时刻、任务短号、事件与码。日志是审计面，不是正文的副本。"""
    record = {"at": _now_iso(), "job_id": job_dir.name, "event": event}
    if code:
        record["code"] = code
    if detail:
        record["detail"] = detail
    try:
        with open(job_dir / EVENTS_NAME, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        # 日志写不进去不能盖掉主流程的结论（拒绝就是拒绝），也不能连带抛第二个错
        pass


def _write_lock(job_dir: Path, job_id: str, source_sha256: str) -> None:
    """排他创建锁：两个进程同时抢同一个任务时，后到的当场失败而不是覆盖。"""
    payload = {"job_id": job_id, "pid": os.getpid(), "started_at": _now_iso(),
               "source_sha256": source_sha256}
    try:
        fd = os.open(str(job_dir / LOCK_NAME),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise JobError(CODE_LOCK_HELD,
                       f"任务锁已存在，不能抢占：{job_dir.name}") from exc
    except OSError as exc:
        raise JobError(CODE_WORKSPACE_NOT_WRITABLE,
                       f"任务锁写入失败：{job_dir.name}（{exc}）") from exc
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_json_text(payload))
        handle.flush()
        os.fsync(handle.fileno())


def _drop_lock(job_dir: Path) -> None:
    try:
        (job_dir / LOCK_NAME).unlink(missing_ok=True)
    except OSError as exc:
        raise JobError(CODE_WORKSPACE_NOT_WRITABLE,
                       f"任务锁解除失败：{job_dir.name}（{exc}）") from exc


# ---------------------------------------------------------------------------
# 源级排他认领：把「同一原稿只有一个活动任务」从检查变成强制
# ---------------------------------------------------------------------------
def _claim_path(ws_root: Path, source_sha256) -> Path:
    """认领文件路径：名字由源哈希确定，所以同一原稿的两个进程必然撞同一个名字。"""
    digest = _HEX_RE.findall(str(source_sha256 or ""))
    return ws_root / (CLAIM_INFIX + "".join(digest)[:CLAIM_HASH_CHARS] + CLAIM_SUFFIX)


def _claim_source(ws_root: Path, source_sha256, job_id: str) -> None:
    """排他创建认领，**先于**「扫一遍已有任务」的判断，把 check-then-act 的窗口关掉。

    已存在时先按 ``_release_stale_claim`` 试收回一次；收不回就先跑扫描给出「哪个任务占着」
    的明确拒绝，扫不出来的（另一个进程正在建的同一半）也只报 ``second_active_job``。
    """
    path = _claim_path(ws_root, source_sha256)
    for _ in (1, 2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _release_stale_claim(path):
                continue
            _refuse_if_active(ws_root)
            raise JobError(CODE_SECOND_ACTIVE_JOB,
                           f"同一原稿的认领仍然生效（{path.name}），另一个任务正在建立或"
                           f"尚未收尾；请恢复它或先显式清理再开新任务",
                           claim=path.name)
        except OSError as exc:
            raise JobError(CODE_WORKSPACE_NOT_WRITABLE,
                           f"源认领写入失败：{path.name}（{exc}）") from exc
        handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        try:
            handle.write(_json_text({"job_id": job_id, "pid": os.getpid(),
                                     "source_sha256": str(source_sha256).lower(),
                                     "claimed_at": _now_iso()}))
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            try:
                path.unlink(missing_ok=True)      # 半成品认领不能把原稿永久锁死
            except OSError:
                pass
            raise JobError(CODE_WORKSPACE_NOT_WRITABLE,
                           f"源认领写入失败：{path.name}（{exc}）") from exc
        finally:
            handle.close()
        return
    raise JobError(CODE_SECOND_ACTIVE_JOB,           # pragma: no cover - 两次都被抢先
                   f"同一原稿的认领反复被抢先（{path.name}），请稍后重试或先清理工作区")


def _source_sha_of(manifest) -> str:
    """清单里的源摘要，只用来还原认领文件名；被改坏的清单最多让认领对不上号。"""
    identity = manifest.get("identity") if isinstance(manifest, dict) else None
    return str(identity.get("source_sha256") or "") if isinstance(identity, dict) else ""


def _claim_job_dir(ws_root: Path, claim: dict):
    """认领里记的 job_id 必须是任务目录的裸短名；形状不认就当证明不了。"""
    job_id = str(claim.get("job_id") or "")
    if len(job_id) == JOB_ID_HEX_CHARS * 2 and _HEX_RE.fullmatch(job_id):
        return ws_root / JOBS_DIRNAME / job_id
    return None


def _release_stale_claim(path: Path) -> bool:
    """陈旧认领的收回判据与活动锁同向：只收「原稿已经释放」或「主人证明已退出且没留下任务」。"""
    claim = _read_json(path)
    if not isinstance(claim, dict):
        return False                          # 读不出的认领证明不了主人已退出：不动它
    job_dir = _claim_job_dir(path.parent, claim)
    if job_dir is None:
        return False                          # 认领里的任务号不可信：不据它删东西
    if job_dir.is_dir():
        state = _read_json(job_dir / STATE_NAME) or {}
        if state.get("state") != "published":
            return False                      # 任务还活动：留给 resume/cleanup 收尾
    elif process_is_alive(claim.get("pid")):
        # 任务目录还没建出来而主人还活着：这正是并发窗口，绝不能替别人收回
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _release_claim(path: Path, job_id: str) -> None:
    """交付或清理后收回认领；认领名字不是本任务的绝不动（可能是别人新一轮的认领）。"""
    claim = _read_json(path)
    if isinstance(claim, dict) and claim.get("job_id") == job_id:
        try:
            path.unlink(missing_ok=True)
        except OSError:                       # 收回失败不能推翻已经完成的交付
            pass


def process_is_alive(pid) -> bool:
    """这个 PID 能否被**证明**还活着；证明不了就返回 False（当作可清的陈旧锁）。

    Windows 上绝不能用 ``os.kill(pid, 0)`` 探活：CPython 在 Windows 下对非
    CTRL_* 信号走 ``TerminateProcess``，探活会变成把别人的任务杀掉。这里改用
    kernel32 的只读查询；打不开句柄时只有「访问被拒绝」算活着（那说明进程存在，
    只是无权查询），其余失败都算已退出。PID 被系统复用只会误判成「还活着」，
    方向是保守的：宁可拒绝清锁，不抢别人的任务。
    """
    try:
        numeric = int(pid)
    except (TypeError, ValueError):
        return False
    if numeric <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(numeric, 0)             # POSIX 上信号 0 只探活，不杀进程
        except ProcessLookupError:
            return False
        except OSError:
            return True                     # 权限不足或查不清：算活着，不抢别人的锁
        return True
    return _windows_process_alive(numeric)


def _windows_process_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    query_limited = 0x1000                 # PROCESS_QUERY_LIMITED_INFORMATION
    access_denied = 5                      # ERROR_ACCESS_DENIED：进程存在，只是查不到
    still_active = 259                     # STILL_ACTIVE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    handle = kernel32.OpenProcess(query_limited, False, pid)
    if not handle:
        return ctypes.get_last_error() == access_denied
    try:
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE,
                                                ctypes.POINTER(wintypes.DWORD))
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True                    # 查不到退出码就不算证明死亡
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# 入参校验与工作区落位
# ---------------------------------------------------------------------------
def _short_field_problem(label: str, value) -> str | None:
    """短标签字段（会原样进任务清单）的形状判据：非空、有长度上限、不含控制字符。

    超限或成段都只拒绝、不截断：截断等于悄悄改掉用户交来的身份。
    """
    text = str(value or "").strip()
    if not text:
        return f"{label} 不能为空"
    if len(text) > MAX_SHORT_FIELD_CHARS:
        return (f"{label} 长度 {len(text)} 超过上限 {MAX_SHORT_FIELD_CHARS} 字符，"
                f"拒绝写入任务清单（超长一律拒绝，不做截断）")
    if _CONTROL_RE.search(text):
        return f"{label} 不能含控制字符或换行（成段文本与提示词的形态不进产物）"
    return None


def _validate_identity(strength, package_sha256, rules_version, model_id) -> None:
    problems = []
    if strength not in STRENGTHS:
        problems.append(f"strength 只能是 {' / '.join(STRENGTHS)}")
    if not _SHA_RE.fullmatch(str(package_sha256 or "")):
        problems.append("package_sha256 必须是 64 位十六进制摘要")
    for label, value in (("rules_version", rules_version), ("model_id", model_id)):
        problem = _short_field_problem(label, value)
        if problem:
            problems.append(problem)
    if problems:
        raise JobError(CODE_BAD_IDENTITY,
                       "任务身份不完整，拒绝据此创建或恢复：" + "；".join(problems))


def _check_location(ws_root: Path) -> None:
    """工作区不得落进 Skill 包目录、``.lemon`` 台账或宿主 skills 安装区。"""
    for candidate in (ws_root, _resolved(ws_root)):
        parts = candidate.parts
        if LEDGER_DIRNAME in parts:
            raise JobError(CODE_FORBIDDEN_LOCATION,
                           f"运行期工作区不得与开发仓库的 .lemon 台账混用：{candidate}")
        if candidate == SKILL_ROOT or candidate.is_relative_to(SKILL_ROOT):
            raise JobError(CODE_FORBIDDEN_LOCATION,
                           f"运行期工作区不得写进 Skill 安装目录：{candidate}")
        for index in range(len(parts) - 1):
            if parts[index] in AGENT_HOME_DIRS and parts[index + 1] == SKILLS_DIRNAME:
                raise JobError(CODE_FORBIDDEN_LOCATION,
                               f"运行期工作区不得落进宿主 Skill 安装区：{candidate}")


def _check_path_length(job_dir: Path) -> None:
    probe = str(job_dir / MANIFEST_NAME)
    if len(probe) > WINDOWS_MAX_PATH:
        raise JobError(CODE_PATH_TOO_LONG,
                       f"任务目录路径长度 {len(probe)} 已超过 Windows MAX_PATH "
                       f"{WINDOWS_MAX_PATH}，请显式改用更短的 workspace 工作目录"
                       f"（默认工作区在原稿旁：{probe}）")


def _ensure_dirs(paths) -> list:
    """按需创建目录并记住「哪些是本次新建的」，失败时只回收自己建的空目录。

    并发开两个任务时，工作区与 ``jobs/`` 可能同时被两边 mkdir：``exist_ok`` 让这种
    「对手刚建好」不当成不可写来报错；回滚只收空目录，所以顺手标记别人刚建的空目录也不会
    删掉别人的东西。
    """
    created = []
    try:
        for index, directory in enumerate(paths):
            if directory.is_dir():
                continue
            if index == 0:
                directory.mkdir(parents=True, exist_ok=True)   # 显式选定的短工作区允许顺手建出来
            else:
                directory.mkdir(exist_ok=True)
            created.append(directory)
    except OSError as exc:
        _rollback(created, Path(paths[-1]))
        raise JobError(CODE_WORKSPACE_NOT_WRITABLE,
                       f"工作目录不可写，拒绝创建任务：{exc}") from exc
    return created


def _rollback(created, job_dir: Path) -> None:
    """失败回滚：先收掉本次新建的任务目录（含任何半成品产物），再收掉空掉的新建目录。"""
    if job_dir.is_dir():
        shutil.rmtree(job_dir, ignore_errors=True)
    for directory in reversed(list(created)):
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass


def _relative_location(job_dir: Path, anchor: Path) -> str:
    """只记「从任务目录回到原稿目录」的相对定位；同卷之外拒建任务，绝不记绝对路径。"""
    try:
        raw = os.path.relpath(str(anchor), str(job_dir))
    except ValueError as exc:
        raise JobError(CODE_WORKSPACE_UNRELATED,
                       f"工作目录与原稿不在同一卷，无法只记相对定位，"
                       f"请把 workspace 选在原稿同卷：{exc}") from exc
    return PurePath(raw).as_posix()


def _source_record(snapshot, job_dir: Path) -> dict:
    """快照里可入册的部分：摘要与 stat，去掉两个路径字段，补上相对定位。"""
    record = snapshot.to_dict()
    record.pop("requested_path", None)
    record.pop("resolved_path", None)
    requested = _absolute(snapshot.requested_path)
    record["name"] = requested.name
    record["dir_rel"] = _relative_location(job_dir, requested.parent)
    return record


def identity_from_snapshot(snapshot, *, job_id: str, strength: str, package_sha256: str,
                           rules_version: str, model_id: str) -> JobIdentity:
    return JobIdentity(
        job_id=job_id, source_sha256=snapshot.sha256, source_size=snapshot.size,
        source_mtime_ns=snapshot.mtime_ns, strength=strength,
        package_sha256=package_sha256, rules_version=rules_version, model_id=model_id)


def _new_state(job_id: str) -> dict:
    return {"schema": STATE_SCHEMA, "job_id": job_id, "state": "created",
            "updated_at": _now_iso(), "boundary_recheck_required": False,
            "failed_from": None, "recovery": None, "output": None}


# ---------------------------------------------------------------------------
# 读回产物
# ---------------------------------------------------------------------------
def load_manifest(job_dir) -> dict:
    job_dir = _absolute(Path(job_dir))
    manifest = _read_json(job_dir / MANIFEST_NAME)
    if manifest is None:
        raise JobError(CODE_UNKNOWN_JOB,
                       f"不是本工具创建的任务目录（缺 {MANIFEST_NAME}）：{job_dir}")
    return manifest


def load_state(job_dir) -> dict:
    job_dir = _absolute(Path(job_dir))
    state = _read_json(job_dir / STATE_NAME)
    if state is None:
        raise JobError(CODE_UNKNOWN_JOB,
                       f"不是本工具创建的任务目录（缺 {STATE_NAME}）：{job_dir}")
    return state


def load_identity(job_dir) -> JobIdentity:
    return JobIdentity(**load_manifest(job_dir)["identity"])


# ---------------------------------------------------------------------------
# 1) 创建
# ---------------------------------------------------------------------------
def create_job(source, *, strength: str, package_sha256: str, rules_version: str,
               model_id: str, workspace=None) -> Path:
    """在原稿旁开一个私有任务目录，返回该目录；失败不留下任何半成品。

    ``workspace`` 是给「默认工作区路径太长」准备的显式出口，必须与原稿同卷。
    源文件的格式、编码、重解析点等闸门全部交给 Task 5 的 ``read_source``，本函数不复制
    一套判断，也不改原稿一个字节。
    """
    _validate_identity(strength, package_sha256, rules_version, model_id)
    snapshot, _text = read_source(source)
    ws_root = _absolute(Path(workspace) if workspace is not None
                        else workspace_dir_for(snapshot.requested_path))
    _check_location(ws_root)
    job_id = secrets.token_hex(JOB_ID_HEX_CHARS)
    job_dir = ws_root / JOBS_DIRNAME / job_id
    _check_path_length(job_dir)

    created = _ensure_dirs((ws_root, ws_root / JOBS_DIRNAME))
    claim = _claim_path(ws_root, snapshot.sha256)
    won_claim = False
    try:
        # 先认领、后扫描：同一原稿的两个进程只可能有一个拿到认领（Important 5）
        _claim_source(ws_root, snapshot.sha256, job_id)
        won_claim = True
        _refuse_if_active(ws_root)
        try:
            job_dir.mkdir()                     # 名字唯一，不存在「对手刚建好」的歧义
        except OSError as exc:
            raise JobError(CODE_WORKSPACE_NOT_WRITABLE,
                           f"任务目录创建失败：{job_dir.name}（{exc}）") from exc
        identity = identity_from_snapshot(
            snapshot, job_id=job_id, strength=strength, package_sha256=package_sha256,
            rules_version=rules_version, model_id=model_id)
        manifest = {"schema": MANIFEST_SCHEMA, "created_at": _now_iso(),
                    "identity": dataclasses.asdict(identity),
                    "source": _source_record(snapshot, job_dir)}
        _atomic_write(job_dir / MANIFEST_NAME, manifest)
        _atomic_write(job_dir / STATE_NAME, _new_state(job_id))
        _write_lock(job_dir, job_id, identity.source_sha256)
    except BaseException:
        if won_claim:
            # 我们真的开过工：收回认领，连自己建的空目录一起回收，不把原稿锁死
            _release_claim(claim, job_id)
            _rollback(created, job_dir)
        # 没抢到认领就是被拒的旁观者：工作区归活动任务，一个字节都不动（否则会把
        # 对手刚建好的工作区连根收走，让赢家平白失败）
        raise
    _append_event(job_dir, "create", detail={"strength": strength})
    return job_dir


def _refuse_if_active(ws_root: Path) -> None:
    """同一原稿默认只允许一个活动任务；已发布的任务不再占位。"""
    jobs_dir = ws_root / JOBS_DIRNAME
    if not jobs_dir.is_dir():
        return
    for job in sorted(p for p in jobs_dir.iterdir() if p.is_dir()):
        manifest = _read_json(job / MANIFEST_NAME)
        if manifest is None:
            continue                              # 不是本工具建的目录，不动它也不据它判断
        state = _read_json(job / STATE_NAME) or {}
        if state.get("state") == "published":
            continue
        _append_event(job, "refuse", code=CODE_SECOND_ACTIVE_JOB)
        raise JobError(CODE_SECOND_ACTIVE_JOB,
                       f"同一原稿已有活动任务 {job.name}（状态 {state.get('state')}）；"
                       f"默认不并行，请恢复它或先显式清理再开新任务",
                       job_dir=str(job))


# ---------------------------------------------------------------------------
# 2) 状态机
# ---------------------------------------------------------------------------
def _require_lock_holder(job_dir: Path, action: str) -> None:
    """状态移动与发布只认持有者（与 ``cleanup_job`` 同纪律）。"""
    lock = _read_lock(job_dir)
    if lock is None:
        raise JobError(CODE_LOCK_HELD,
                       f"任务 {job_dir.name} 没有锁记录（{LOCK_NAME}），本进程不算持有者，"
                       f"拒绝{action}；请先 resume_job 取得锁")
    if lock is False:
        raise JobError(CODE_LOCK_HELD,
                       f"任务 {job_dir.name} 的锁记录读不出（{LOCK_NAME} 已损坏），"
                       f"无法证明持有者，拒绝{action}")
    holder = lock.get("pid")
    if holder != os.getpid():
        raise JobError(CODE_LOCK_HELD,
                       f"任务 {job_dir.name} 的锁由进程 {holder} 持有，本进程（{os.getpid()}）"
                       f"不是持有者，拒绝{action}；请由该进程收尾或先 resume_job 接管")


def advance_state(job_dir, state: str, *, output_name=None, output_sha256=None,
                  channel=None) -> dict:
    """推进状态机；合法移动只有「下一步」与「failed」，落盘走 ``os.replace``。

    推进前必须证明本进程持有任务锁（非持有者、锁损坏、无锁一律 ``lock_held``）；已发布
    的任务在取锁判断之前先拒（``already_published``），二次发布不可能。

    ``channel`` 只在推进到 ``published`` 时才有意义，是发布方对自己走哪条交付通道的
    **显式声明**：``"overwrite"`` 允许把交付名记成本任务清单里那份原稿的名字（覆盖的
    交付物就是原稿本身），其余形状一律由 ``_publish_record`` 拒绝。默认 ``None`` 保持
    Task 6 的口径不变——不声明通道就只认交付形状。
    """
    job_dir = _absolute(Path(job_dir))
    manifest = load_manifest(job_dir)               # 认目录，否则 unknown_job
    if state not in STATES:
        raise JobError(CODE_BAD_STATE,
                       f"未知状态 {state!r}，只能是 {' / '.join(STATES)}")
    record = load_state(job_dir)
    current = record["state"]
    if current not in STATES:
        raise JobError(CODE_BAD_STATE, f"任务状态记录里的 {current!r} 不是合法状态")
    if current == "published":
        raise JobError(CODE_ALREADY_PUBLISHED,
                       f"任务 {job_dir.name} 已发布，拒绝再推进（避免二次发布）")
    _require_lock_holder(job_dir, "推进状态")

    if state == "failed":
        if current == "failed":
            raise JobError(CODE_BAD_TRANSITION,
                           f"任务 {job_dir.name} 已经处于 failed（恢复起点 "
                           f"{record.get('failed_from')}），拒绝重复进入失败态："
                           f"那会把恢复起点写成 failed 本身，之后任何合法推进都判不了")
        record["failed_from"] = current
        record["recovery"] = {
            "kept": [MANIFEST_NAME, STATE_NAME, EVENTS_NAME, CANDIDATES_DIRNAME],
            "cleanup": "novel_job.cleanup_job(job_dir, keep_evidence=False)",
            "resume": "novel_job.resume_job(job_dir, ...)"}
    else:
        _check_transition(record, current, state)
        record["failed_from"] = None
        record["recovery"] = None
        if state == "reviewed":
            record["boundary_recheck_required"] = False
        if state == "published":
            record["output"] = _publish_record(output_name, output_sha256,
                                               source_name=_manifest_source_name(manifest),
                                               channel=channel)
    record["state"] = state
    record["updated_at"] = _now_iso()
    _atomic_write(job_dir / STATE_NAME, record)
    if state == "published":
        _drop_lock(job_dir)                       # 交付完成，原稿释放
        _release_claim(_claim_path(job_dir.parent.parent,
                                   _source_sha_of(manifest)), job_dir.name)
    _append_event(job_dir, "advance", code=state)
    return record


def _check_transition(record: dict, current: str, target: str) -> None:
    if current != "failed":
        if _RANK[target] != _RANK[current] + 1:
            raise JobError(CODE_BAD_TRANSITION,
                           f"状态只能按 {' / '.join(s for s in STATES if s != 'failed')} "
                           f"逐级前进，拒绝从 {current} 跳到 {target}")
        return
    base = record.get("failed_from") or "created"
    if base not in _RANK:
        raise JobError(CODE_BAD_STATE,
                       f"任务状态记录里的失败起点 {base!r} 不是合法状态，"
                       f"无法判定能恢复到哪一步")
    lowest, highest = _RANK[base], _RANK[base] + 1
    if _RANK[target] < lowest:
        raise JobError(CODE_BAD_TRANSITION,
                       f"失败恢复只能重做 {base} 或其之后的状态，拒绝回到 {target}")
    if _RANK[target] > highest:
        raise JobError(CODE_BAD_TRANSITION,
                       f"失败恢复只许回到失败的那一步（{base}）或它的下一步，"
                       f"不许跳步，拒绝跳到 {target}")


def _manifest_source_name(manifest) -> str:
    """清单里那份原稿的裸文件名：覆盖通道的交付名只能等于它，别的名字一概不认。"""
    source = manifest.get("source") if isinstance(manifest, dict) else None
    return str(source.get("name") or "") if isinstance(source, dict) else ""


def _publish_record(output_name, output_sha256, *, source_name=None, channel=None) -> dict:
    """发布事实的入册校验：另存记交付形状的名字，覆盖记**本任务清单里那份原稿的名字**。

    路径不进这条记录：另存由 ``output_dir``、覆盖由清单的 ``dir_rel`` + ``name`` 复原，
    恢复端都只要裸文件名 + 目标哈希就能认出那份已交付的字节（设计方案 86 行）。
    """
    name = str(output_name or "").strip()
    if (not name or "/" in name or "\\" in name
            or PurePath(name).is_absolute() or PurePath(name).name != name):
        raise JobError(CODE_BAD_PUBLISH_RECORD,
                       "发布必须给出目标文件名（只能是不带目录的裸文件名）")
    if len(name) > MAX_OUTPUT_NAME_CHARS:
        raise JobError(CODE_BAD_PUBLISH_RECORD,
                       f"目标文件名长度 {len(name)} 超过上限 {MAX_OUTPUT_NAME_CHARS}，"
                       f"拒绝写入任务状态（超长一律拒绝，不做截断）")
    if not _OUTPUT_NAME_RE.fullmatch(name):
        # 覆盖通道的交付物就是原稿本身（设计方案 82 行的两条通道之一），它的文件名按形状
        # 一定不是交付名。闸门只被如实扩展**一格**：调用方显式声明走覆盖，且这个名字必须
        # 等于本任务清单里的那份原稿名；其余一律照旧拒绝——再多放宽一寸，就是把手里的
        # 字节交给调用方随口命名（设计方案 86 行要的是「路径与目标哈希入册」，不是「随便记」）。
        if not (channel == CHANNEL_OVERWRITE and source_name and name == str(source_name)):
            raise JobError(CODE_BAD_PUBLISH_RECORD,
                           "目标文件名必须符合安全交付合同的默认交付形状 "
                           "<原文件名>.去AI味-<保守|平衡|深度>[.冲突编号].<原扩展名>，"
                           "或在显式声明覆盖通道时等于本任务清单记录的原稿文件名；"
                           "成段正文或提示词形态一律拒绝")
    if not _SHA_RE.fullmatch(str(output_sha256 or "")):
        raise JobError(CODE_BAD_PUBLISH_RECORD,
                       "发布必须给出目标文件的 64 位十六进制哈希，否则恢复时无法识别已交付")
    return {"name": name, "sha256": str(output_sha256).lower()}


def _recorded_output_verified(state: dict, source_name: str = "") -> bool:
    """交付记录只认「按同一规则重推导一遍还能得到同样记录」，不认记忆里的字段。

    ``source_name`` 是清单里那份原稿的裸名：覆盖通道记的就是它，重推导时必须同样认它，
    否则一个已经如实入册的覆盖交付会被判成「记录不可信」而卡在 resume。
    """
    output = state.get("output")
    if not isinstance(output, dict) or set(output) != {"name", "sha256"}:
        return False
    # 按两条通道各推一遍：任一通道能推出同样记录，记录就自洽。每次推导单独容错——
    # 另存形状的推导必然抛 JobError，整段包在一个 try 里会让第一次抛错直接 return False，
    # 覆盖通道那份如实入册的交付就永远验不过（P13 现场发现的旧缺陷）。
    for channel in (None, CHANNEL_OVERWRITE):
        try:
            verified = _publish_record(output.get("name"), output.get("sha256"),
                                       source_name=source_name, channel=channel)
        except JobError:
            continue
        if verified == output:
            return True
    return False


# ---------------------------------------------------------------------------
# 3) 恢复
# ---------------------------------------------------------------------------
def resume_job(job_dir, *, strength: str, package_sha256: str, rules_version: str,
               model_id: str, source=None) -> Literal["resume", "re-review", "refuse",
                                                      "complete"]:
    """判定能否续跑，返回 ``resume`` / ``re-review`` / ``refuse`` / ``complete``。

    判定顺序是刻意的：先看交付（已发布且交付记录重推导通过就幂等 ``complete``，一个字节
    都不写，也不因原稿后续漂移或身份变化卡在 ``refuse``）；没交付完的才把源重新读一遍重算
    哈希（不信任清单记住的值），再比身份，最后才碰锁。「拒绝」不写状态也不改锁，恢复现场
    原样留着。
    """
    job_dir = _absolute(Path(job_dir))
    manifest = load_manifest(job_dir)
    _validate_identity(strength, package_sha256, rules_version, model_id)
    recorded = JobIdentity(**manifest["identity"])
    state = load_state(job_dir)                 # 清单在而状态缺：目录不完整，拒绝据此续跑

    if state.get("state") == "published" and _recorded_output_verified(
            state, _manifest_source_name(manifest)):
        return "complete"                         # 幂等收尾：不写一个字节，也不再发布

    path = _resume_source(job_dir, manifest, source)
    if path is None:
        return _refuse(job_dir, CODE_SOURCE_MISMATCH)
    try:
        snapshot, _text = read_source(path)
    except SourceInputError as exc:
        return _refuse(job_dir, CODE_SOURCE_UNREADABLE, gate=exc.code)
    drifted = [name for name, expected, actual in (
        ("sha256", recorded.source_sha256, snapshot.sha256),
        ("size", recorded.source_size, snapshot.size),
        ("mtime_ns", recorded.source_mtime_ns, snapshot.mtime_ns))
        if expected != actual]
    if drifted:
        return _refuse(job_dir, CODE_SOURCE_DRIFT, fields=sorted(drifted))
    unstable = drift_fields(path, snapshot)       # 第二次独立重算，防读取途中被动过
    if unstable:
        return _refuse(job_dir, CODE_SOURCE_UNSTABLE, fields=sorted(unstable))

    changed = [name for name, expected, actual in (
        ("strength", recorded.strength, strength),
        ("package_sha256", recorded.package_sha256, package_sha256),
        ("rules_version", recorded.rules_version, rules_version))
        if expected != actual]
    if changed:
        return _refuse(job_dir, CODE_IDENTITY_CHANGED, fields=sorted(changed))
    model_changed = recorded.model_id != model_id

    if state.get("state") == "published":
        # 状态写着已发布，交付记录却重推导出不了合法目标：不能据此宣称完成，也不抢锁续跑
        return _refuse(job_dir, CODE_ALREADY_PUBLISHED)

    if not _takeover_lock(job_dir, recorded):
        return _refuse(job_dir, CODE_LOCK_HELD)

    if model_changed or state.get("boundary_recheck_required"):
        if model_changed:
            manifest["identity"]["model_id"] = model_id
            _atomic_write(job_dir / MANIFEST_NAME, manifest)
        state["boundary_recheck_required"] = True
        state["updated_at"] = _now_iso()
        _atomic_write(job_dir / STATE_NAME, state)
        _append_event(job_dir, "re-review",
                      code="model_changed" if model_changed else "boundary_recheck_pending")
        return "re-review"
    _append_event(job_dir, "resume")
    return "resume"


def _resume_source(job_dir: Path, manifest: dict, source):
    """由清单的相对定位复原原稿位置；调用方显式交来的路径必须与它一致。"""
    record = manifest["source"]
    derived = _resolved(job_dir / PurePosixPath(record["dir_rel"])) / record["name"]
    if source is None:
        return derived
    given = _resolved(source)
    return given if given == derived else None


def _refuse(job_dir: Path, code: str, **detail) -> str:
    _append_event(job_dir, "refuse", code=code, detail=detail or None)
    return "refuse"


def _read_lock(job_dir: Path):
    """三态读锁：``None`` 没有锁；``False`` 锁记录读不出（损坏）；其余是锁内容。

    损坏的锁按「持有者无法被证明已退出」处理，方向和活锁一致：不接手、不清理。
    """
    path = job_dir / LOCK_NAME
    if not path.is_file():
        return None
    return _read_json(path) or False


def _takeover_lock(job_dir: Path, recorded: JobIdentity) -> bool:
    """活锁归别人就拒绝；持有者被证明已退出才清锁重取（此时源快照已核对通过）。"""
    lock = _read_lock(job_dir)
    if lock is None:
        _write_lock(job_dir, job_dir.name, recorded.source_sha256)
        return True
    if lock is False:
        return False
    holder = lock.get("pid")
    if holder == os.getpid():
        return True
    if process_is_alive(holder):
        return False
    _append_event(job_dir, "lock_cleared", code="holder_exited")
    _drop_lock(job_dir)
    _write_lock(job_dir, job_dir.name, recorded.source_sha256)
    return True


# ---------------------------------------------------------------------------
# 4) 清理
# ---------------------------------------------------------------------------
def cleanup_job(job_dir, *, keep_evidence: bool = True) -> None:
    """显式清理入口。

    默认（``keep_evidence=True``）：删除临时正文候选并释放任务锁，保留任务清单、哈希与
    报告（设计方案「成功后删除临时正文候选，保留任务清单、哈希和报告」）。
    ``keep_evidence=False``：连任务目录一起回收，并顺带收掉空掉的工作区；这是失败或取消
    时告知用户的那个入口。两种形态都只动任务目录内部，原稿与成稿永不在此列。
    持有者还活着的任务锁拒绝清理，避免把别人正在写的任务拆掉。
    """
    job_dir = _absolute(Path(job_dir))
    manifest = load_manifest(job_dir)
    lock = _read_lock(job_dir)
    if lock is False:
        raise JobError(CODE_LOCK_HELD,
                       f"任务 {job_dir.name} 的锁记录读不出（{LOCK_NAME} 已损坏），"
                       f"无法证明持有者已退出，拒绝清理")
    if lock is not None and lock.get("pid") != os.getpid() and process_is_alive(lock.get("pid")):
        raise JobError(CODE_LOCK_HELD,
                       f"任务 {job_dir.name} 仍被进程 {lock.get('pid')} 持有，拒绝清理；"
                       f"请先结束该进程或恢复该任务")
    if lock is not None:
        _drop_lock(job_dir)
    # 清理意味着这个任务不再占用原稿：源级认领一起收回，否则工作区永远删不干净
    _release_claim(_claim_path(job_dir.parent.parent,
                               _source_sha_of(manifest)), job_dir.name)
    candidates = job_dir / CANDIDATES_DIRNAME
    if candidates.is_dir():
        try:
            shutil.rmtree(candidates)
        except OSError as exc:
            raise JobError(CODE_WORKSPACE_NOT_WRITABLE,
                           f"临时正文候选删除失败：{exc}") from exc
    if keep_evidence:
        _append_event(job_dir, "cleanup", detail={"keep_evidence": True,
                                                  "removed": [CANDIDATES_DIRNAME]})
        return
    try:
        shutil.rmtree(job_dir)
    except OSError as exc:
        raise JobError(CODE_WORKSPACE_NOT_WRITABLE,
                       f"任务目录删除失败：{exc}") from exc
    jobs_dir = job_dir.parent
    for directory in (jobs_dir, jobs_dir.parent):
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass


def update_job_extension(job_dir, *, manifest_extension=None,
                         state_extension=None) -> None:
    """v2 扩展元数据的唯一更新入口（alpha.1 补正 Q03，补正方案 4.2）。

    先证明本进程持有任务锁，再复用 ``_atomic_write`` 原地合并——不复制第二套
    原子写，也不另建任务账本。扩展字典在顶层按键合并（调用方用 ``v2``
    命名空间打包自己的键），不整体覆盖既有清单/状态；state.json 缺失视为
    不完整任务，拒绝写扩展。
    """
    job_dir = _absolute(Path(job_dir))
    load_manifest(job_dir)                        # 认目录：unknown_job 由它判
    _require_lock_holder(job_dir, "更新 v2 扩展元数据")
    if manifest_extension is not None:
        if not isinstance(manifest_extension, dict):
            raise JobError(CODE_BAD_IDENTITY,
                           "manifest_extension 必须是字典")
        manifest = load_manifest(job_dir)
        manifest.update(manifest_extension)
        _atomic_write(job_dir / MANIFEST_NAME, manifest)
    if state_extension is not None:
        if not isinstance(state_extension, dict):
            raise JobError(CODE_BAD_IDENTITY,
                           "state_extension 必须是字典")
        state = _read_json(job_dir / STATE_NAME)
        if state is None:
            raise JobError(CODE_UNKNOWN_JOB,
                           f"任务 {job_dir.name} 缺 {STATE_NAME}：不完整任务"
                           "拒绝写 v2 扩展")
        state.update(state_extension)
        _atomic_write(job_dir / STATE_NAME, state)


# ---------------------------------------------------------------------------
# 2-丁) 逐文件提交账本（v2.0.0 alpha.3 P13）
# ---------------------------------------------------------------------------
#: 提交事实的事件名；账本就落在现有事件层，不另建第二份附属文件。
COMMIT_LEDGER_EVENT = "commit"
#: 一条提交事实的必备字段（枚举与语义由 ``v2_contracts.CommitRecord`` 负责，
#: 这里只管形状：状态字符串在这里不重复权威，读回时由合同层判级）。
COMMIT_LEDGER_FIELDS: tuple[str, ...] = ("file", "before_sha256", "after_sha256",
                                         "backup_ref", "stage", "status", "judge_epoch",
                                         "judge_provider", "judge_model")
#: 提交意图的事件名与字段（channel/任务身份/候选摘要在这里先记，登记在成功后补）。
COMMIT_INTENT_EVENT = "commit_intent"
#: 第二版意图记录：多了稳定意图身份与版本标记。恢复面按身份而不是按「前态哈希相同」
#: 认一次交付——同前态的两次不同意图（另一版候选、另一个裁判纪元）不能互相顶掉。
COMMIT_INTENT_SCHEMA = 2
COMMIT_INTENT_FIELDS: tuple[str, ...] = ("file", "before_sha256", "channel", "task_id",
                                         "candidate_sha256", "stage", "judge_epoch",
                                         "judge_provider", "judge_model",
                                         "intent_schema", "intent_id", "output_dir")
#: 交付事实（写成之后、终态之前落账）：路径与备份只由实际写者提供，恢复不再猜名字。
COMMIT_DELIVERY_EVENT = "commit_delivery"
COMMIT_DELIVERY_FIELDS: tuple[str, ...] = ("intent_id", "file", "channel", "delivered",
                                           "backup_ref", "after_sha256",
                                           "delivered_text_sha256", "output_dir")
#: 提交事实从第二版起带意图身份；旧事实（无该字段）按兼容规则读。
COMMIT_LEDGER_FIELDS_V2: tuple[str, ...] = COMMIT_LEDGER_FIELDS + ("intent_id",)
#: 身份取自这些字段：任何进程重算都得到同一个值，不含时间戳也不含随机数。
_INTENT_IDENTITY_FIELDS: tuple[str, ...] = ("task_id", "file", "channel", "before_sha256",
                                            "candidate_sha256", "stage", "judge_epoch",
                                            "output_dir")


def intent_identity(record) -> str:
    """稳定意图身份 = 这些定位字段的规范 JSON 摘要（缺字段就拒，不补默认值）。"""
    payload = {name: record.get(name, "") if name == "output_dir" else record[name]
               for name in _INTENT_IDENTITY_FIELDS}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()



def read_events(job_dir) -> list:
    """事件层的公开读口（P13 之前只有写口）：坏行不静默跳过。

    提交账本要用来决定「哪些文件该还原、还原成什么」，读不动的一行如果被当成
    不存在，回滚就会漏件——所以解析失败一律报错，而不是继续。
    """
    job_dir = _absolute(Path(job_dir))
    load_manifest(job_dir)
    path = job_dir / EVENTS_NAME
    if not path.is_file():
        return []
    events: list = []
    with open(path, encoding="utf-8", newline="") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise JobError(CODE_BAD_COMMIT_RECORD,
                               f"{path.name} 第 {number} 行不是合法 JSON，"
                               "无法据其判定提交事实（不静默跳过）") from exc
            if not isinstance(record, dict):
                raise JobError(CODE_BAD_COMMIT_RECORD,
                               f"{path.name} 第 {number} 行不是对象")
            events.append(record)
    return events


def _ledger_problem(record, fields, allow_empty=()) -> str | None:
    """账本条目的形状自检：字段齐、哈希 64 位十六进制、字符串非空、纪元非负整数。"""
    if not isinstance(record, dict):
        return "必须是字典"
    missing = [name for name in fields if name not in record]
    extra = sorted(set(record) - set(fields))
    if missing or extra:
        return (f"字段不符：缺 {missing or '无'}，多 {extra or '无'}（权威字段集 "
                f"{tuple(fields)}）")
    for name in ("file", "backup_ref", "stage", "status", "channel", "task_id",
                 "candidate_sha256", "judge_provider", "judge_model",
                 "intent_id", "delivered", "delivered_text_sha256", "output_dir"):
        if name not in record:
            continue
        if not isinstance(record[name], str):
            return f"{name} 必须是字符串"
        if not record[name].strip() and name not in allow_empty:
            return f"{name} 必须是非空字符串"
    for name in ("before_sha256", "after_sha256", "candidate_sha256"):
        if name not in record:
            continue
        if not _SHA_RE.fullmatch(str(record[name])):
            return f"{name} 必须是 64 位十六进制哈希"
    schema = record.get("intent_schema")
    if schema is not None and (isinstance(schema, bool) or not isinstance(schema, int)
                               or schema != COMMIT_INTENT_SCHEMA):
        return f"intent_schema 只能是 {COMMIT_INTENT_SCHEMA}，收到 {schema!r}"
    if "judge_epoch" in record:
        epoch = record["judge_epoch"]
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            return f"judge_epoch 必须是非负整数，收到 {epoch!r}"
    return None


def _record_ledger_event(job_dir, event: str, record, fields, allow_empty=()) -> dict:
    """把一条账本事实追加进事件层并**回读自证**已经落盘。

    ``_append_event`` 对写失败是沉默的（日志不能盖掉主流程结论）；提交账本不同：
    记不上就等于以后回滚找不到备份。所以写完立刻读回比对，最后一条不是这条就抛错。
    """
    problem = _ledger_problem(record, fields, allow_empty=allow_empty)
    if problem:
        raise JobError(CODE_BAD_COMMIT_RECORD, f"拒绝登记 {event}：{problem}")
    job_dir = _absolute(Path(job_dir))
    load_manifest(job_dir)
    _append_event(job_dir, event, detail=dict(record))
    landed = [item.get("detail") for item in read_events(job_dir)
              if item.get("event") == event]
    if not landed or landed[-1] != dict(record):
        raise JobError(CODE_BAD_COMMIT_RECORD,
                       f"{event} 未能落进 {job_dir.name} 的事件台账，"
                       "回滚与恢复将无从核对，拒绝把这次动作当成已登记")
    return dict(record)


def record_commit(job_dir, record) -> dict:
    """逐文件提交事实（committed / rolled_back / failed）。

    第二版带 ``intent_id``（与意图同一身份，恢复才能认出「是哪一次提交」）；
    不带该字段的旧形状仍然合法——旧账本不能被这次改动判成没提交过。
    """
    fields = (COMMIT_LEDGER_FIELDS_V2 if "intent_id" in record else COMMIT_LEDGER_FIELDS)
    return _record_ledger_event(job_dir, COMMIT_LEDGER_EVENT, record, fields)


def record_commit_intent(job_dir, record) -> dict:
    """提交**意图**：动手之前先记下这次要覆盖哪个文件、覆盖前的字节摘要与通道。

    它是「已发布但未登记」这种半成状态的唯一可发现凭据（P13 的账本只有在发布成功
    之后才存在，此刻的失败要靠这条意图事实接回去）。第二版必须自带稳定身份，而且
    身份要能由定位字段重算出来——对不上一律拒，不给「手工塞一条意图」留后门。
    """
    if record.get("intent_schema") == COMMIT_INTENT_SCHEMA:
        if record.get("intent_id") != intent_identity(record):
            raise JobError(CODE_BAD_COMMIT_RECORD,
                           f"拒绝登记 {COMMIT_INTENT_EVENT}：intent_id 与定位字段重算值不符")
    return _record_ledger_event(job_dir, COMMIT_INTENT_EVENT, record,
                                 COMMIT_INTENT_FIELDS, allow_empty=("output_dir",))


def record_commit_delivery(job_dir, record) -> dict:
    """写成之后、终态之前的交付事实：路径、备份、字节与文本摘要都取自实际写者。

    ``backup_ref`` 允许为空串——那如实表示「这次是新建交付，没有前态备份」；
    ``output_dir`` 允许为空串——那如实表示「交付落在原稿同目录」。
    """
    return _record_ledger_event(job_dir, COMMIT_DELIVERY_EVENT, record,
                                COMMIT_DELIVERY_FIELDS,
                                allow_empty=("backup_ref", "output_dir"))


def commit_records(job_dir) -> list:
    """按写入顺序返回全部提交事实（同一文件的最后一条即其当前状态）。"""
    return [dict(item["detail"]) for item in read_events(job_dir)
            if item.get("event") == COMMIT_LEDGER_EVENT
            and isinstance(item.get("detail"), dict)]


def commit_intents(job_dir) -> list:
    """按写入顺序返回全部提交意图（同一文件最后一条即其当前意图）。"""
    return [dict(item["detail"]) for item in read_events(job_dir)
            if item.get("event") == COMMIT_INTENT_EVENT
            and isinstance(item.get("detail"), dict)]


def commit_deliveries(job_dir) -> list:
    """按写入顺序返回全部交付事实（写成但未收尾时，恢复只认这里记着的路径）。"""
    return [dict(item["detail"]) for item in read_events(job_dir)
            if item.get("event") == COMMIT_DELIVERY_EVENT
            and isinstance(item.get("detail"), dict)]


def pending_intent(job_dir) -> dict | None:
    """最新一条还没有交付事实的提交意图——写者用它把交付绑回同一轮，不靠猜。"""
    delivered = {str(item.get("intent_id") or "") for item in commit_deliveries(job_dir)}
    for intent in reversed(commit_intents(job_dir)):
        if str(intent.get("intent_id") or "") not in delivered:
            return intent
    return None


def find_ledger_leaks(paths, needles) -> list:
    """隐私自检的唯一判据：返回内容命中任何 needle 的清单类产物文件名（读不出也算可疑）。"""
    leaked = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            leaked.append(path.name)
            continue
        if any(needle and needle in text for needle in needles):
            leaked.append(path.name)
    return leaked


def self_check() -> int:
    """可运行自检：临时目录里走一遍全生命周期；任何泄露都让调用方非零退出。"""
    import tempfile

    SAMPLE = "紫貂禅寺的钟声在半夜三点十七响了一次。\n"
    WANTED = {"strength": "balanced", "package_sha256": "a" * 64,
              "rules_version": "1.2.0", "model_id": "model-alpha"}
    with tempfile.TemporaryDirectory(prefix="lieflat-job-selfcheck-") as td:
        # 阶段钉子不落在本模块：总工单全局约束 12 要求版本/阶段身份只由现有权威出。
        # 惰性导入是有意的——self_check 只在开发与门链里跑，接收者路径不经过这里。
        import v2_task
        stage = v2_task.DEV_RELEASE_STAGE
        book = Path(td) / "book.txt"
        book.write_text(SAMPLE, encoding="utf-8", newline="\n")
        job = create_job(book, **WANTED)
        assert load_state(job)["state"] == "created"
        assert resume_job(job, **WANTED) == "resume"
        assert resume_job(job, **{**WANTED, "model_id": "model-beta"}) == "re-review"
        assert resume_job(job, **{**WANTED, "strength": "deep"}) == "refuse"
        for step in ("segmented", "rewritten", "reviewed", "published"):
            advance_state(job, step, output_name="book.去AI味-平衡.txt", output_sha256="b" * 64)
        assert resume_job(job, **WANTED) == "complete"

        # 逐文件提交账本（P13）：登记要能读回，不合形状必须拒登记
        committed = {"file": str(book), "before_sha256": "c" * 64,
                     "after_sha256": "d" * 64, "backup_ref": str(book) + ".bak",
                     "stage": stage, "status": "committed", "judge_epoch": 0,
                  "judge_provider": "host", "judge_model": "host-current"}
        assert record_commit(job, committed) == committed
        assert commit_records(job) == [committed]
        try:
            record_commit(job, dict(committed, after_sha256="not-hex"))
        except JobError:
            pass
        else:
            raise AssertionError("哈希不合形状的提交事实必须拒登记")
        assert commit_records(job) == [committed], "被拒的登记不得留在账本里"
        intent = {"file": str(book), "before_sha256": "c" * 64, "channel": "overwrite",
                  "task_id": "t-selfcheck", "candidate_sha256": "d" * 64,
                  "stage": stage, "judge_epoch": 1,
                  "judge_provider": "host", "judge_model": "host-current",
                  "intent_schema": COMMIT_INTENT_SCHEMA,
                  "intent_id": "", "output_dir": ""}
        intent["intent_id"] = intent_identity(intent)
        assert record_commit_intent(job, intent) == intent
        assert commit_intents(job) == [intent]
        try:
            record_commit_intent(job, dict(intent, intent_id="f" * 64))
        except JobError:
            pass
        else:
            raise AssertionError("身份与定位字段重算值不符的意图必须拒登记")
        assert len(commit_intents(job)) == 1, "被拒的意图不得留在台账里"
        delivery = {"intent_id": intent["intent_id"], "file": str(book),
                    "channel": "overwrite", "delivered": str(book),
                    "backup_ref": str(book) + ".bak", "after_sha256": "d" * 64,
                    "delivered_text_sha256": "e" * 64, "output_dir": ""}
        assert record_commit_delivery(job, delivery) == delivery
        assert commit_deliveries(job) == [delivery]
        assert pending_intent(job) is None, "已交付的意图不应再被当成待收尾"
        assert commit_records(job) == [committed], "意图与交付不得混进提交事实账本"

        # 状态机棘轮探针：重复失败与跳步恢复都只能得到 JobError，绝不楔死
        probe_book = Path(td) / "probe.txt"
        probe_book.write_text(SAMPLE, encoding="utf-8", newline="\n")
        probe = create_job(probe_book, **WANTED)
        advance_state(probe, "failed")
        for refused in ("failed", "reviewed", "published"):
            try:
                advance_state(probe, refused, output_name="probe.去AI味-平衡.txt",
                              output_sha256="c" * 64)
            except JobError as exc:
                assert exc.code == CODE_BAD_TRANSITION, (refused, exc.code)
            else:
                raise AssertionError(f"自检：从 failed 推进到 {refused} 本该被拒却没报错")
        assert advance_state(probe, "created")["state"] == "created"
        cleanup_job(probe, keep_evidence=False)
        assert not workspace_dir_for(probe_book).exists()

        candidates = job / CANDIDATES_DIRNAME
        candidates.mkdir()
        (candidates / "chapter-01.txt").write_text(SAMPLE, encoding="utf-8", newline="\n")
        cleanup_job(job)
        assert not candidates.exists() and (job / MANIFEST_NAME).is_file()
        ledgers = [p for p in workspace_dir_for(book).rglob("*")
                   if p.is_file() and CANDIDATES_DIRNAME not in p.parts]
        leaked = find_ledger_leaks(ledgers, [SAMPLE, str(book.parent),
                                             book.parent.as_posix()])
        if leaked:
            print(f"自检：FAIL（{len(leaked)} 件清单类产物含正文或绝对路径："
                  f"{', '.join(sorted(set(leaked)))}）")
            return 1
        cleanup_job(job, keep_evidence=False)
        assert not workspace_dir_for(book).exists()
        assert book.read_text(encoding="utf-8") == SAMPLE
        print(f"工作区：{job.relative_to(book.parent).as_posix()}")
        print(f"状态机：{' / '.join(s for s in STATES if s != 'failed')}；"
              f"failed 只能由未发布的状态进入一次，恢复只许回到失败的那一步或它的下一步")
        print(f"产物：{', '.join(LEDGER_NAMES)}，临时正文候选在 {CANDIDATES_DIRNAME}/，"
              f"源级认领在 {CLAIM_INFIX}<源哈希前缀>{CLAIM_SUFFIX}")
        print(f"清单类产物 {len(ledgers)} 件，其中含正文或绝对路径的：0 件")
        print("自检：PASS（创建、认领、恢复、重审、拒绝、发布、幂等、清理、隐私检查全部走通）")
        return 0


if __name__ == "__main__":  # pragma: no cover - 泄露或断言失败都要让进程非零退出
    raise SystemExit(self_check())
