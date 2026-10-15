import json
import os
import time
from pathlib import Path


def _pid_alive(pid: int) -> bool | None:
    """判断 PID 是否存活；无法判断时返回 None。"""
    if os.name == "nt":
        try:
            import ctypes

            process_handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process_handle:
                return False
            ctypes.windll.kernel32.CloseHandle(process_handle)
            return True
        except Exception:  # noqa: BLE001 - 权限/平台差异按无法判断处理
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:  # noqa: BLE001
        return None


class ProjectLock:
    """跨进程项目锁：独占创建 + PID/时间戳 + 残留锁自愈。

    崩溃残留的锁：PID 已死时自动清理并写审计；PID 无法判断时，
    锁龄超过 ``stale_seconds`` 才清理，否则拒绝进入。
    """

    def __init__(self, project_dir: Path, stale_seconds: int = 600) -> None:
        self.path = project_dir / ".v4.lock"
        self.stale_seconds = stale_seconds
        self._handle: int | None = None

    def _recover_if_stale(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(payload.get("pid", 0))
            created_at = float(payload.get("created_at", 0.0))
        except (OSError, ValueError, TypeError):
            payload = None
            pid = 0
            created_at = 0.0

        alive = _pid_alive(pid) if pid else False
        age = time.time() - (created_at or self.path.stat().st_mtime)
        if alive is True:
            return False
        if alive is None and age < self.stale_seconds:
            return False
        if alive is False and payload is None and age < self.stale_seconds:
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        try:
            from .audit import append_audit

            append_audit(
                self.path.parent / "audit.jsonl",
                "stale_project_lock_recovered",
                lock_pid=pid if payload is not None else None,
                lock_age_seconds=round(age, 3),
            )
        except Exception:  # noqa: BLE001 - 清理审计失败不阻塞锁恢复
            pass
        return True

    def __enter__(self) -> "ProjectLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not self._recover_if_stale():
            raise RuntimeError(f"项目正在被其他进程使用: {self.path}")
        try:
            self._handle = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as exc:
            raise RuntimeError(f"项目正在被其他进程使用: {self.path}") from exc
        payload = json.dumps(
            {"pid": os.getpid(), "created_at": time.time()},
            ensure_ascii=False,
        )
        os.write(self._handle, payload.encode("utf-8"))
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is not None:
            os.close(self._handle)
            self._handle = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
