#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_commit.py - File 模式生产提交模块（方案 R4 重建版）

这是技能在 File 模式下的唯一写盘通道：候选文本必须先通过机械保真验证器
(scripts/validate-rewrite.py) 的 PASS_MECHANICAL，才允许原子落盘。

状态契约（commit_rewrite 返回值中的 status 字段）：
  COMMITTED     验证通过、原子写入成功、落盘后哈希复核一致；保留可回滚备份
  NOOP          候选与目标原始字节完全一致，不触碰文件（无备份、无写入）
  BLOCKED       机械验证 FAIL / REVIEW / ERROR——目标字节保持不变
                （v1.2 Task 12 起验证器多一个 NO-OP 判词：候选与目标字节一致时上面
                 第 3 步就已经返回 NOOP，走不到这道门；真走到了也只会被当成不放行，
                 不存在「NO-OP 也算通过然后写盘」这条路）
  ABORTED_RACE  调用方声明的 expected_hash 与目标当前哈希不符（读后已被他人修改）
  ROLLED_BACK   写入中断或落盘复核失败——已从备份恢复原始字节
  ERROR         未预期异常——已尽力恢复原始字节

安全不变量：
  1. FAIL / REVIEW / ERROR / 中断 / 哈希竞争 / 复核失败：目标字节一律保持原样。
  2. 备份名唯一递增（.bak / .bak1 / .bak2 …），绝不覆盖既有备份。
  3. BOM 与 CRLF/LF 换行风格按目标文件原样保留。
  4. 候选先写入同目录临时文件，fsync 后经 os.replace 原子替换。
  5. 提交后立即重读目标，比对候选哈希，防止静默损坏。
  6. 每次提交均调用真实机械验证器，不接受测试替身。

v1.2 Task 12 追加的四件原语（安全交付合同的落盘端，命名与门禁在 novel_output.py）：
  create_exclusive        O_CREAT|O_EXCL 独占创建 + fsync，已存在即返回 False（不覆盖）
  commit_new_file         同卷临时文件 flush/fsync 后用**硬链接**独占挂交付名（方案 86 行）；
                          名字已存在返回 False，平台不认硬链接抛 NoClobberUnavailable
  probe_no_clobber        实测该目录能否兑现「目标存在即失败」（独占创建与硬链接各测一次）；
                          测不出来上层就安全停止，不降级成普通覆盖
  overwrite_atomically    交接 §9 的第 2–7 步：初始哈希 → 排他唯一备份 → 同目录临时文件
                          fsync → 替换前复查目标哈希 → 保留权限原子替换 → 复读验证/回滚

平台局限（如实记录，不假装 POSIX 语义）：Windows 的 ``os.fsync`` 只对文件句柄有效，
目录项同步（POSIX 用来把「新建文件」这件事刷进目录）在本机不可用，探针会把
``directory_fsync`` 报成 False；崩溃时可能留下一个已写入但未同步的目录项，因此
独占语义只由 ``O_EXCL``/``CREATE_NEW`` 与 ``CreateHardLinkW`` 承担，回滚材料一律保留到
复读通过为止。``os.replace`` 在 Windows 上不能替换只读目标（WinError 5），覆盖通道会先
临时清掉只读位、替换成功后把原权限位整份贴回；``os.chmod`` 在本机只兑现只读位，
原文件的 ACL 不在能保留的范围内。
"""

import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
import time
import types
from pathlib import Path


def _load_validator():
    """加载同目录的真实机械验证器 validate-rewrite.py（文件名含连字符，需按路径加载）。

    刻意不用 importlib 的常规加载：常规加载会在同目录写出
    `scripts/__pycache__/validate-rewrite.cpython-*.pyc`，使安装目录的文件集合
    在使用一次之后就与 release-manifest.json 不再一致（安装完整性失效）。

    这里改为「读源码 → compile → exec 到新模块」，既不写缓存，也不修改全局的
    sys.dont_write_bytecode。加载失败一律抛出异常，由调用方阻断写盘——
    绝不静默降级成「跳过校验后照常落盘」。
    """
    script = Path(__file__).resolve().parent / "validate-rewrite.py"
    if not script.is_file():
        raise RuntimeError(f"机械验证器缺失，拒绝继续: {script}")

    try:
        source = script.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"机械验证器无法读取，拒绝继续: {script} ({exc})") from exc

    try:
        code = compile(source, str(script), "exec", dont_inherit=True)
    except SyntaxError as exc:
        raise RuntimeError(f"机械验证器无法编译，拒绝继续: {script} ({exc})") from exc

    module = types.ModuleType("validate_rewrite_prod")
    module.__file__ = str(script)
    module.__builtins__ = __builtins__
    # __name__ 固定为非 __main__，验证器的 CLI 入口因此不会被触发
    exec(code, module.__dict__)

    if not hasattr(module, "validate"):
        raise RuntimeError(f"机械验证器缺少 validate()，拒绝继续: {script}")
    return module


class SimulatedInterrupt(RuntimeError):
    """测试注入的写入中断模拟：发生在临时文件写盘之后、原子替换之前。"""


class NoClobberUnavailable(RuntimeError):
    """本平台/本目录兑现不了「目标存在即失败」的原子挂名。

    设计方案 87 行对此只有一个要求：**安全停止**，不许降级成普通覆盖。调用方据此
    停止发布并留下原因码，绝不「那就直接写吧」。
    """


# ===========================================================================
# Task 12 追加：安全交付合同的写盘原语（全仓唯一写者仍是本文件）
#
# 三层分工：``novel_output.py`` 负责命名、L0–L3 门禁、任务状态与锁；**任何**落到磁盘
# 上的字节都只经下面这几个函数。它们的存在不改变 ``commit_rewrite`` 的既有合同。
# ===========================================================================

#: 覆盖通道的固定顺序（设计方案 88 行 / 交接 §9 的第 2–7 步；第 1 步「取任务锁」
#: 在 novel_output 那一层，因为它属于任务状态机而不属于写盘）。
OVERWRITE_STEPS = ("initial_hash", "exclusive_backup", "temp_fsync",
                   "recheck_target_hash", "preserve_mode_replace", "reread_verify")
OVERWRITE_STATUS_VALUES = ("COMMITTED", "NOOP", "ABORTED_RACE", "ABORTED",
                           "ROLLED_BACK", "ERROR")
#: 故障注入点：只接受这四个位置，写错一律 ValueError，绝不静默当成「不注入」。
OVERWRITE_INJECTION_STEPS = ("backup", "temp-write", "replace", "verify")
BACKUP_NAME_LIMIT = 1000
PROBE_PREFIX = ".lieflat-noclobber-probe"
#: 并发实证的**确定性地板**（与 ``fail_at``/``after_backup`` 同族，只为测试而存在）：
#: 两个写者只有在「初始哈希都核对过之后」才一起冲向排他备份，才算真竞争；没踩到这个
#: 窗口的并发跑赢了也只是运气。值 = 放标记文件的目录；参与数 = 第二个环境变量（默认 2）。
#: 不设环境变量时一次 ``os.environ.get`` 直接返回，生产路径零开销、行为不变。
BACKUP_RENDEZVOUS_ENV = "LIEFLAT_BACKUP_RENDEZVOUS_DIR"
BACKUP_RENDEZVOUS_PARTICIPANTS_ENV = "LIEFLAT_BACKUP_RENDEZVOUS_PARTICIPANTS"
BACKUP_RENDEZVOUS_TIMEOUT_SECONDS = 30.0


def rendezvous_before_backup(target) -> int:
    """在「初始哈希核对之后、排他备份创建之前」这一格里会合，返回当场数到的同窗写者数。

    每个写者先落一个 ``pid-<pid>`` 标记，再等到标记凑齐约定数量才继续。标记留在目录里，
    测试据此**证明**两个写者真的同时处在窗口内——拿不到标记就是窗口没被踩到，用例自己红。
    """
    directory = os.environ.get(BACKUP_RENDEZVOUS_ENV)
    if not directory:
        return 0
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / f"pid-{os.getpid()}"
    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:               # 同进程第二次覆盖：标记已在，不影响会合
        fd = None
    except OSError:
        fd = None
    if fd is not None:
        try:
            os.write(fd, str(target).encode("utf-8"))
        except OSError:                   # 标记写不进去也不许把交付拖下水
            pass
        finally:
            os.close(fd)
    try:
        want = max(2, int(os.environ.get(BACKUP_RENDEZVOUS_PARTICIPANTS_ENV) or 2))
    except ValueError:
        want = 2
    deadline = time.time() + BACKUP_RENDEZVOUS_TIMEOUT_SECONDS
    while len(list(root.glob("pid-*"))) < want and time.time() < deadline:
        time.sleep(0.005)
    return len(list(root.glob("pid-*")))


def _write_and_fsync(handle, data: bytes) -> None:
    """写入 + flush + fsync 的统一出口：新原语的独占创建、备份、同目录临时文件都走这一份。"""
    handle.write(data)
    handle.flush()
    os.fsync(handle.fileno())


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class SafeFileCommitEngine:
    """File 模式安全提交引擎（生产实现，测试必须直接使用本类）。"""

    BACKUP_BASE_SUFFIX = ".bak"

    @staticmethod
    def inspect_file(file_path: Path) -> dict:
        raw_bytes = Path(file_path).read_bytes()
        has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
        encoding = "utf-8-sig" if has_bom else "utf-8"
        newline = "\r\n" if b"\r\n" in raw_bytes else "\n"
        return {
            "raw_bytes": raw_bytes,
            "has_bom": has_bom,
            "encoding": encoding,
            "newline": newline,
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        }

    @staticmethod
    def _run_mechanical_validator(original_text: str, candidate_text: str) -> tuple:
        """通过真实验证器以文件接口校验，返回 (verdict, issues, exit_code)。"""
        vr = _load_validator()
        with tempfile.TemporaryDirectory() as td:
            before_path = Path(td) / "before.md"
            after_path = Path(td) / "after.md"
            before_path.write_text(original_text, encoding="utf-8")
            after_path.write_text(candidate_text, encoding="utf-8")
            return vr.validate(before_path, after_path)

    @classmethod
    def commit_rewrite(
        cls,
        target_path,
        candidate_text: str,
        expected_hash: str = None,
        force_interrupt: bool = False,
        simulate_corrupt_write: bool = False,
    ) -> dict:
        target = Path(target_path)
        try:
            info = cls.inspect_file(target)
            original_bytes = info["raw_bytes"]
            original_sha = info["sha256"]

            # 1. 目标哈希竞争检查：调用方读过的目标必须仍是调用方看到的那份
            if expected_hash is not None and expected_hash != original_sha:
                return {"status": "ABORTED_RACE", "written": False,
                        "reason": f"目标当前哈希 {original_sha[:12]}… 与调用方声明 {expected_hash[:12]}… 不符"}

            # 2. 按目标文件的换行风格与 BOM 归一化候选字节
            if candidate_text.startswith(chr(0xFEFF)):
                # BOM 是编码元数据不是正文：这一步下面会给带 BOM 的目标无条件补一层，
                # 候选自带 BOM 就静默叠成双 BOM，而机械验证照样 PASS（G5 宿主复测抓到）。
                return {"status": "BLOCKED", "written": False,
                        "reason": "候选文本自带 BOM：请交不含 BOM 的正文（BOM 由本门按目标文件补），"
                                  "否则会在目标 BOM 之上再叠一层"}
            normalized = candidate_text.replace("\r\n", "\n").replace("\n", info["newline"])
            candidate_bytes = normalized.encode("utf-8")
            if info["has_bom"]:
                candidate_bytes = b"\xef\xbb\xbf" + candidate_bytes

            # 3. No-Op：原始字节完全一致则不触碰文件
            if candidate_bytes == original_bytes:
                return {"status": "NOOP", "written": False, "reason": "Raw bytes identical"}

            # 4. 机械验证门：只有 PASS_MECHANICAL 允许进入写入流程
            original_text = original_bytes.decode(info["encoding"])
            verdict, issues, code = cls._run_mechanical_validator(original_text, normalized)
            if verdict != "PASS_MECHANICAL":
                return {"status": "BLOCKED", "written": False, "verdict": verdict,
                        "validator_exit_code": code, "issues": issues}

            # 5–9. QA-01：写盘统一委托 overwrite_atomically（排他唯一备份、替换前
            # 复查目标哈希、保留原权限原子替换、复读验证/回滚），消除本方法内重复
            # 实现与其 0o600 权限退化隐患；状态契约保持不变。
            fail_at = None
            if force_interrupt:
                fail_at = "replace"    # 中断注入：替换前失败，目标保持原字节
            elif simulate_corrupt_write:
                fail_at = "verify"     # 复核失败注入：复读不符触发回滚
            outcome = cls.overwrite_atomically(
                target, candidate_bytes,
                expected_sha256=original_sha, fail_at=fail_at)
            status = outcome.get("status")
            if status == "COMMITTED":
                return {"status": "COMMITTED", "written": True, "verdict": verdict,
                        "backup_path": outcome.get("backup_path"),
                        "target_sha256": outcome.get("target_sha256"),
                        "previous_sha256": original_sha}
            if status == "NOOP":
                return {"status": "NOOP", "written": False, "reason": "Raw bytes identical"}
            if status == "ABORTED_RACE":
                return {"status": "ABORTED_RACE", "written": False,
                        "reason": outcome.get("reason"),
                        "backup_path": outcome.get("backup_path")}
            if status == "ABORTED":
                # 中断发生在替换前：目标仍是调用方核对过的原字节；旧合同无 ABORTED
                # 状态，沿用其中断语义归入 ROLLED_BACK（备份已收回，无孤儿）。
                return {"status": "ROLLED_BACK", "written": False,
                        "reason": outcome.get("reason"),
                        "backup_path": outcome.get("backup_path")}
            if status == "ROLLED_BACK":
                return {"status": "ROLLED_BACK", "written": False,
                        "reason": outcome.get("reason"),
                        "backup_path": outcome.get("backup_path")}
            return {"status": "ERROR", "written": False,
                    "reason": outcome.get("reason"),
                    "backup_path": outcome.get("backup_path")}

        except Exception as exc:  # 未预期异常：委托前只读、无需恢复；委托内自恢复
            return {"status": "ERROR", "written": False, "reason": f"{type(exc).__name__}: {exc}"}

    # ---- 1) 独占创建：no-clobber 的唯一原子依据 --------------------------------
    @staticmethod
    def create_exclusive(path, data: bytes, mode: int = 0o600) -> bool:
        """``O_CREAT|O_EXCL`` 创建并 fsync；目标已存在返回 **False**（绝不截断、绝不覆盖）。

        写入中途炸掉要把刚创建的名字收回：否则那个序号被一个半成品永久占住，
        下一个写者会以为自己「撞名」而跳到再下一个序号。
        """
        target = Path(path)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(str(target), flags, mode)       # 已存在 → FileExistsError
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as handle:
                _write_and_fsync(handle, data)
        except BaseException:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return True

    @classmethod
    def _create_unique_backup(cls, target, data: bytes, mode: int = 0o600) -> Path:
        """排他创建唯一备份并当场回读核实——两个进程不能选中同一个同名备份。

        「先算名字再写」是 check-then-act：两个写者会同时算出 ``x.bak`` 并互相覆盖，
        被覆盖的那份就再也恢复不回来了。这里直接让创建本身排他，撞名就顺延下一个序号。
        """
        target = Path(target)
        for index in range(BACKUP_NAME_LIMIT):
            name = (target.name + cls.BACKUP_BASE_SUFFIX if index == 0
                    else f"{target.name}{cls.BACKUP_BASE_SUFFIX}{index}")
            path = target.with_name(name)
            if not cls.create_exclusive(path, data, mode):
                continue                            # 名字被抢：顺延下一个序号，绝不覆盖既有备份
            if path.read_bytes() != data:
                raise OSError(f"备份回读与原始字节不一致，拒绝据此覆盖: {path}")
            return path
        raise OSError(f"备份序号在 {BACKUP_NAME_LIMIT} 次内都没排他创建成功: {target.name}")

    # ---- 1b) 新文件的提交：同目录临时文件 fsync，再用硬链接独占挂名 --------------
    @classmethod
    def commit_new_file(cls, path, data: bytes, mode: int = 0o600) -> bool:
        """成稿先写**同卷临时文件**并 flush/fsync，再用硬链接一次性把交付名挂上去。

        设计方案 86 行的原话是「新文件在同卷写临时文件、flush/fsync，再用不覆盖现存
        目标的独占创建或硬链接提交」。为什么不直接用 ``create_exclusive`` 写交付名：
        写入是一个缓冲区一个缓冲区落盘的，进程在写的中途被杀就会在**交付名**上留下半个
        文件——那个序号被半成品永久占住，而读的人无从分辨它是不是完整成稿。先落临时名、
        字节 fsync 成功后 ``os.link`` 挂名（``CreateHardLink``/``link()`` 在目标已存在时
        原子地失败，既不覆盖也不截断），交付名就只剩两种状态：从来不存在，或一出现就是
        完整的。崩在挂名之前只留下一个 ``.partial`` 临时名，不占序号、也不是成稿。

        返回 ``False`` = 交付名已被占，调用方只推进序号；文件系统不认硬链接时抛
        :class:`NoClobberUnavailable`，由上层安全停止，**绝不降级成普通覆盖**。
        """
        target = Path(path)
        link = getattr(os, "link", None)
        if link is None:                               # pragma: no cover - 老平台分支
            raise NoClobberUnavailable("本平台 Python 没有 os.link，无法独占挂名")
        temp_path = None
        try:
            handle, temp_name = tempfile.mkstemp(dir=str(target.parent),
                                                 prefix=target.name + ".", suffix=".partial")
            temp_path = Path(temp_name)
            with os.fdopen(handle, "wb") as stream:
                _write_and_fsync(stream, data)         # 同卷 + flush + fsync
            cls._apply_mode(temp_path, mode)
            try:
                link(str(temp_path), str(target))
            except FileExistsError:
                return False                           # 名字被抢：一个字节都没碰过它
            except OSError as exc:
                raise NoClobberUnavailable(
                    f"该目录不接受硬链接提交（{type(exc).__name__}: {exc}）："
                    "拒绝降级为普通覆盖") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass                               # 挂名成功后临时名本就指向同一 inode
        return True

    @staticmethod
    def _apply_mode(path, mode: int) -> None:
        """把原稿权限位贴到将要挂名的 inode 上。

        平台局限（如实写明）：POSIX 上这是真的权限位；Windows 的 ``os.chmod`` 只能兑现
        只读位，ACL 由目录继承，这里不假装能保住原 ACL。
        """
        try:
            os.chmod(str(path), mode)
        except OSError:
            pass

    # ---- 2) 平台能力探针 ------------------------------------------------------
    @classmethod
    def probe_no_clobber(cls, directory) -> dict:
        """实测这个目录能不能兑现「目标存在即失败」，测不出来如实报 False。

        上层（``novel_output``）据此**安全停止**：设计方案 87 行明确「平台无法提供
        ‘目标存在则失败’的原子创建时安全停止，不降级为普通覆盖」。Windows 的
        ``O_EXCL`` 由 ``CREATE_NEW`` 实现、``os.link`` 由 ``CreateHardLinkW`` 实现，
        本机实测都能兑现；换到网络盘/exFAT/异常文件系统上兑现不了时，探针报 False
        而不是假装能——两条路各测一次，因为交付走的是硬链接那条。
        """
        report = {"atomic_exclusive_create": False, "atomic_link": False,
                  "file_fsync": False, "directory_fsync": False, "reason": ""}
        probe = Path(directory) / f"{PROBE_PREFIX}-{os.getpid()}-{secrets.token_hex(4)}.tmp"
        payload = b"lieflat-probe-a"
        try:
            try:
                created = cls.create_exclusive(probe, payload)
            except OSError as exc:
                report["reason"] = f"独占创建直接失败：{type(exc).__name__}: {exc}"
                return report
            if not created:
                report["reason"] = "探针文件名刚创建就报「已存在」，无法证明独占语义"
                return report
            try:
                again = cls.create_exclusive(probe, b"lieflat-probe-b")
            except OSError as exc:
                report["reason"] = f"第二次独占创建抛出错而非拒绝：{type(exc).__name__}: {exc}"
                return report
            if again:
                report["reason"] = "第二次独占创建仍然成功——该目录不会「目标存在即失败」"
                return report
            if probe.read_bytes() != payload:
                report["reason"] = "已存在的探针字节被改动过——不是 no-clobber 语义"
                return report
            report["atomic_exclusive_create"] = True
            report["file_fsync"] = True          # 走到这里说明上面的写入已 fsync 成功
            link_probe = probe.with_name(probe.name + "-link")
            try:
                report["atomic_link"] = cls._probe_link(link_probe, payload)
            except NoClobberUnavailable as exc:
                report["reason"] = str(exc)
            if not report["atomic_link"] and not report["reason"]:
                report["reason"] = "硬链接挂名不兑现「目标存在即失败」，或挂名后字节被改动"
            report["directory_fsync"] = cls._probe_directory_fsync(directory)
            return report
        finally:
            for name in (probe, probe.with_name(probe.name + "-link")):
                try:
                    name.unlink(missing_ok=True)
                except OSError:
                    pass

    @classmethod
    def _probe_link(cls, probe, payload: bytes) -> bool:
        """真跑一次「临时文件 fsync → 硬链接挂名 → 同名再挂必须失败且不改动字节」。"""
        if not cls.commit_new_file(probe, payload):
            return False
        if probe.read_bytes() != payload:
            return False
        if cls.commit_new_file(probe, b"lieflat-probe-b"):
            return False
        return probe.read_bytes() == payload

    @staticmethod
    def _probe_directory_fsync(directory) -> bool:
        """目录项能否 fsync。Windows 没有这个能力（POSIX 文档那套在此不可用），报 False。"""
        try:
            fd = os.open(str(Path(directory)), os.O_RDONLY)
        except OSError:
            return False
        try:
            os.fsync(fd)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    # ---- 3) 覆盖通道：交接 §9 的固定顺序 --------------------------------------
    @classmethod
    def overwrite_atomically(cls, target_path, candidate_bytes: bytes, *,
                             expected_sha256: str | None = None, fail_at: str | None = None,
                             after_backup=None) -> dict:
        """按「初始哈希 → 排他备份 → 同目录临时文件 fsync → 替换前复查哈希 →
        保留权限原子替换 → 复读验证 → 失败回滚」覆盖一个既有文件。

        第 1 步（任务锁）不在这里：它属于任务状态机，由 ``novel_output`` 在进入本函数
        之前完成。故障注入点只用于测试（``fail_at``），写错位置直接 ValueError。

        返回状态的含义（``OVERWRITE_STATUS_VALUES``）：

          COMMITTED      替换成功且复读字节与候选一致；备份保留可回滚
          NOOP           候选字节与目标字节完全一致，一个字节都没动
          ABORTED_RACE   初始哈希或替换前复查对不上；目标由别人拥有，我们不回滚别人的内容
          ABORTED        替换之前就中断；目标字节仍是调用方核对过的那份，备份已收回
          ROLLED_BACK    替换之后复核不过；已从备份恢复原始字节
          ERROR          平台/目录层故障（含目标在途中消失）；能留的材料一律留着
        """
        if fail_at is not None and fail_at not in OVERWRITE_INJECTION_STEPS:
            raise ValueError(f"未知的故障注入点 {fail_at!r}，只能是 "
                             f"{' / '.join(OVERWRITE_INJECTION_STEPS)}")
        target = Path(target_path)
        backup_path = None
        temp_path = None
        replaced = False
        original = None
        mode = None
        steps: list = []
        try:
            # 步 2：初始哈希
            original = target.read_bytes()
            current = _sha256(original)
            if expected_sha256 is not None and current != str(expected_sha256).lower():
                return {"status": "ABORTED_RACE", "written": False, "steps": list(steps),
                        "reason": f"目标当前哈希 {current[:12]}… 与调用方声明 "
                                  f"{str(expected_sha256)[:12]}… 不符"}
            if candidate_bytes == original:
                return {"status": "NOOP", "written": False, "steps": list(steps),
                        "backup_path": None, "reason": "候选字节与目标完全一致"}
            mode = stat.S_IMODE(os.lstat(target).st_mode)
            steps.append("initial_hash")
            # 并发实证的确定性地板（未设环境变量时立刻返回，生产路径零开销）：两个写者
            # 都在「初始哈希已过、备份还没排他创建」这一格里会合，才一起冲向步 3。
            rendezvous_before_backup(target)

            # 步 3：排他创建唯一备份并验证
            if fail_at == "backup":
                raise OSError(13, "模拟：备份无法排他创建")
            backup_path = cls._create_unique_backup(target, original, mode)
            steps.append("exclusive_backup")
            if after_backup is not None:
                after_backup()

            # 步 4：同目录临时文件 + fsync
            if fail_at == "temp-write":
                raise PermissionError(13, "模拟：目录在备份之后失去写权限")
            handle, temp_name = tempfile.mkstemp(dir=str(target.parent),
                                                 prefix=target.name + ".", suffix=".tmp")
            temp_path = Path(temp_name)
            with os.fdopen(handle, "wb") as stream:
                _write_and_fsync(stream, candidate_bytes)
            steps.append("temp_fsync")

            # 步 5：替换前再次检查目标哈希（窗口越短越好，但必须有）
            latest = target.read_bytes()
            if _sha256(latest) != current:
                return {"status": "ABORTED_RACE", "written": False, "steps": list(steps),
                        "backup_path": str(backup_path),
                        "reason": "替换前复查：目标已被他人改动；备份保留原始字节可恢复"}
            steps.append("recheck_target_hash")

            # 步 6：保留原权限并原子替换
            if fail_at == "replace":
                raise SimulatedInterrupt("模拟：原子替换失败")
            # Windows 实测：os.replace 不能替换只读文件（WinError 5），POSIX 则不看这个位。
            # 先临时清掉只读位，替换成功后把原权限位整份贴回目标——净效果与替换前一致。
            cls._make_writable(target)
            os.replace(str(temp_path), str(target))
            temp_path = None
            replaced = True
            os.chmod(target, mode)          # 覆盖必须保留原权限位（唯一的形态，没有开关）
            steps.append("preserve_mode_replace")

            # 步 7：复读验证
            if fail_at == "verify":
                cls._make_writable(target)
                target.write_bytes(original + b"CORRUPTED-TAIL")
            committed = target.read_bytes()
            if _sha256(committed) != _sha256(candidate_bytes):
                cls._make_writable(target)
                target.write_bytes(backup_path.read_bytes())
                os.chmod(target, mode)
                if target.read_bytes() != original:
                    return {"status": "ERROR", "written": False, "steps": list(steps),
                            "backup_path": str(backup_path),
                            "reason": "复读不符且回滚也失败；备份仍在，请人工用备份恢复"}
                return {"status": "ROLLED_BACK", "written": False, "steps": list(steps),
                        "backup_path": str(backup_path),
                        "reason": "落盘复读哈希与候选不符，已从备份恢复原始字节"}
            steps.append("reread_verify")
            return {"status": "COMMITTED", "written": True, "steps": list(steps),
                    "path": str(target), "backup_path": str(backup_path),
                    "target_sha256": _sha256(committed), "previous_sha256": current,
                    "mode": mode, "fsync": True}
        except BaseException as exc:
            return cls._overwrite_failure_outcome(exc, target, backup_path, temp_path,
                                                  replaced, original, steps, mode)
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _make_writable(path) -> None:
        """清掉只读位（回滚与替换都需要）；权限的最终状态由调用方贴回。"""
        try:
            os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IWRITE)
        except OSError:
            pass

    @classmethod
    def _overwrite_failure_outcome(cls, exc, target, backup_path, temp_path, replaced,
                                   original, steps, mode) -> dict:
        """失败收尾：替换后一律从备份恢复；替换前绝不留下孤儿备份或半成品。"""
        reason = f"{type(exc).__name__}: {exc}"
        if replaced and backup_path is not None:
            try:
                cls._make_writable(target)
                target.write_bytes(backup_path.read_bytes())
                if mode is not None:
                    os.chmod(target, mode)
                return {"status": "ROLLED_BACK", "written": False, "steps": list(steps),
                        "backup_path": str(backup_path), "reason": f"替换后故障已回滚：{reason}"}
            except OSError as rollback_exc:
                return {"status": "ERROR", "written": False, "steps": list(steps),
                        "backup_path": str(backup_path),
                        "reason": f"回滚失败：{rollback_exc}；原始字节只在 {backup_path}"}
        intact = False
        try:
            intact = target.read_bytes() == original
        except OSError:
            intact = False
        if intact and backup_path is not None:
            try:                                  # 什么都没换成：备份不留成孤儿
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
        if not isinstance(exc, SimulatedInterrupt) or not intact:
            return {"status": "ERROR", "written": False, "steps": list(steps),
                    "backup_path": str(backup_path) if backup_path is not None else None,
                    "reason": reason + ("" if intact else "；目标已不可读，备份是唯一恢复材料")}
        return {"status": "ABORTED", "written": False, "steps": list(steps),
                "backup_path": None, "reason": reason}


def main() -> None:
    """手工/流水线 CLI：python scripts/file_commit.py TARGET --candidate-file FILE [--expected-hash H]"""
    args = sys.argv[1:]
    if len(args) < 3 or args[1] != "--candidate-file":
        print("用法: python scripts/file_commit.py <target> --candidate-file <file> [--expected-hash <sha256>]",
              file=sys.stderr)
        sys.exit(3)
    target, candidate_file = args[0], args[2]
    expected_hash = None
    if "--expected-hash" in args:
        expected_hash = args[args.index("--expected-hash") + 1]
    # 候选文件按 UTF-8 读：文件开头的 BOM 是编码元数据，剥掉后再交给门，
    # 否则它会作为正文残留并被门的 BOM 归一化再补一层。
    candidate_text = Path(candidate_file).read_text(encoding="utf-8-sig")
    res = SafeFileCommitEngine.commit_rewrite(Path(target), candidate_text, expected_hash=expected_hash)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.exit(0 if res["status"] in ("COMMITTED", "NOOP") else 1)


if __name__ == "__main__":
    main()
