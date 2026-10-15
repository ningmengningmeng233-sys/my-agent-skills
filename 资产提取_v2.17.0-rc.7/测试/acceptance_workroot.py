# -*- coding: utf-8 -*-
"""验收工作根（Z02）：把包内自检的一切写入面从**被审包根**搬到外置独占工作根。

为什么需要它：`测试/run_release_acceptance.py` 的 heavy 段会在包根内跑
`测试/e2e_flow.py --snapshot`、`测试/run_all_checks.py`、`测试/run_asset_ledger_migration.py` 与
`compileall`，于是随包声明成员 `测试/e2e/资产库/交付清单.json` 被改写，`.pytest_cache`、`.process`、
`.lemon/run-ledger.jsonl`、`__pycache__`、e2e 状态库等新文件留在树内——跑完自己的门后「包树≠清单」，
逐成员对账再也无法证明包没变。这里只做三件事：边界判定、只读暂存副本、输出根重定向。
不碰产品业务规则，也不放宽任何验收判据。
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import stat as statmod

EXCLUDED_COPY = ()  # 暂存＝逐成员原样复制，不裁件


class Refused(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def is_reparse(p: pathlib.Path) -> bool:
    try:
        st = os.lstat(str(p))
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & statmod.FILE_ATTRIBUTE_REPARSE_POINT)


def within(child: pathlib.Path, parent: pathlib.Path) -> bool:
    """containment 用 resolve＋relative_to；字符串前缀会把合法兄弟目录误判成内部。"""
    try:
        pathlib.Path(os.path.normpath(str(child))).relative_to(
            pathlib.Path(os.path.normpath(str(parent))))
        return True
    except ValueError:
        return False


def sha_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def validate_work_root(*, pkg_root, work_root, protected=()) -> pathlib.Path:
    pkg = pathlib.Path(pkg_root).resolve()
    wr = pathlib.Path(work_root)
    if not wr.is_absolute():
        raise Refused("WORK_ROOT_NOT_ABSOLUTE", str(wr))
    for anc in [wr, *wr.parents]:
        if anc.exists() and is_reparse(anc):
            raise Refused("WORK_ROOT_THROUGH_REPARSE", str(anc))
    res = wr.resolve()
    if res == pkg or within(res, pkg):
        raise Refused("WORK_ROOT_INSIDE_PACKAGE", f"被审包根是 {pkg}，实到 {res}")
    for pr in protected:
        prr = pathlib.Path(pr).resolve()
        if within(res, prr):
            raise Refused("WORK_ROOT_IN_PROTECTED", f"{res} 落在受保护真实库/现役 {prr} 内")
    if res.exists() and any(res.iterdir()):
        raise Refused("WORK_ROOT_OCCUPIED", f"目标已存在且非空，不覆盖：{res}")
    return res


def validate_report_path(*, pkg_root, report_path) -> pathlib.Path:
    """报告也不得写进被审树——否则「跑完门后包树≠清单」会以另一种形式复现。"""
    rp = pathlib.Path(report_path)
    if not rp.is_absolute():
        rp = pathlib.Path.cwd() / rp
    if within(rp.resolve(), pathlib.Path(pkg_root).resolve()):
        raise Refused("REPORT_INSIDE_PACKAGE", f"报告不得写进被审树：{rp}")
    return rp.resolve()


def stage_paths(*, work_root, rnd: str) -> dict:
    w = pathlib.Path(work_root).resolve()
    base = w / "stages" / rnd
    return {"skill": base / "skill",
            "ledger": w / "ledger" / rnd / "run-ledger.jsonl",
            "pycache": w / "pycache" / rnd,
            "pytest_cache": w / "pytest-cache" / rnd,
            "temp": w / "temp" / rnd}


def gate_env_for_work(*, work_root, base_env: dict) -> dict:
    """账本/字节码/pytest 缓存/临时目录全部钉在工作根下。

    故意**不设** PYTHONDONTWRITEBYTECODE：那会让包内 `compileall` 变成写不出文件的空操作＝假绿。
    也故意**不设** PYTHONPYCACHEPREFIX：前缀会把整条 stage 路径再复刻一遍，Windows 260 字符上限下
    深层 e2e 测试文件会 FileNotFoundError（实测短路径过、长路径崩）。字节码改落 stage 副本内部——
    那已经在工作根里，被审树仍有一个字节都不变。
    """
    w = pathlib.Path(work_root).resolve()
    env = dict(base_env)
    for key in ("ledger", "temp"):
        (w / key).mkdir(parents=True, exist_ok=True)
    env["UNIFIED_LEDGER_PATH"] = str(w / "ledger" / "run-ledger.jsonl")
    # 不注入 PYTEST_ADDOPTS：包内全门会**嵌套调用 pytest**，多塞一个 --cache-dir 会让它 exit 4（用法错），
    # 把我方注入伪装成产品红。缓存留在暂存副本内（已在工作根里），被审树仍然干净。
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(w / "temp")
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return env


def tree_digest(root) -> dict:
    root = pathlib.Path(root)
    return {p.relative_to(root).as_posix(): sha_file(p) for p in root.rglob("*") if p.is_file()}


def audit_tree(root, before: dict) -> dict:
    now = tree_digest(root)
    return {"ok": not (sorted(k for k in set(before) & set(now) if before[k] != now[k]) or
                       set(now) - set(before) or set(before) - set(now)),
            "modified": sorted(k for k in set(before) & set(now) if before[k] != now[k]),
            "added": sorted(set(now) - set(before)), "removed": sorted(set(before) - set(now)),
            "files": len(now)}


def stage_copy(*, pkg_root, dest) -> dict:
    pkg, dest = pathlib.Path(pkg_root), pathlib.Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise Refused("STAGE_OCCUPIED", f"暂存副本已存在且非空，不覆盖：{dest}")
    n = 0
    for src in sorted(pkg.rglob("*")):
        if not src.is_file():
            continue
        to = dest / src.relative_to(pkg)
        to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(to))
        n += 1
    return {"files": n, "dest": str(dest), "digest": tree_digest(dest)}
