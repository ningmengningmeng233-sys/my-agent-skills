# -*- coding: utf-8 -*-
"""E01：备份孙进程编码合同（缺陷已实测复现，非假设）。

现场：默认 GBK 控制台下 `migrate_legacy_visuals` 抛 `BACKUP_FAILED`，
根因是 `_backup()` 用 `sys.executable -c` 起孙进程时没带 UTF-8，
而 `maintenance.do_backup` 成功路径要打印 `✅ 备份完成…` → 孙进程 `UnicodeEncodeError` → 非零退出。
实测同一棵树：不设 PYTHONIOENCODING 时 9 红，设 utf-8 时 20 全绿（见 RunRoot\\evidence\\e00-triage-*.log）。

本文件钉四条合同：
① 受控孙进程显式 UTF-8（argv `-X utf8` 且 env 复制继承后仅补编码变量，宿主全局环境不被改）；
② 路径经 argv 传参，不再把绝对路径插值进 `-c` 源码（避免引号/转义把合同写歪）；
③ 在**没有** UTF-8 环境的父进程里真实跑一次迁移：备份成功、zip 内容完整、旧文件哈希不变；
④ 备份真失败时仍报 `BACKUP_FAILED`（不被编码崩溃掩盖，也不吞成 0）。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
from conftest import run_py  # noqa: F401  （与同目录测试一致的子进程出口）

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
FIXTURE = REPO_ROOT / "测试" / "fixtures" / "legacy_visual"
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.visual_migration import (  # noqa: E402
    MigrationError, _backup, migrate_legacy_visuals,
)


def _write_index(lib: pathlib.Path) -> None:
    (lib / ".process").mkdir(parents=True, exist_ok=True)
    (lib / ".process" / "资产索引.json").write_text(json.dumps({
        "schema_version": 1, "source_fingerprint": "sha256:e01",
        "assets": [{"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘", "priority": "P1",
                    "fact_level": "剧本事实",
                    "evidence": [{"segment": "段01.md", "line": "00001",
                                  "quote": "林姑娘走进茶楼"}],
                    "relations": [], "dependencies": [], "status": "CONFIRMED"}]},
        ensure_ascii=False), encoding="utf-8")


def _legacy_lib(tmp_path: pathlib.Path) -> pathlib.Path:
    lib = tmp_path / "资产库"
    shutil.copytree(FIXTURE, lib)
    _write_index(lib)
    return lib


def _hash_all(root: pathlib.Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_backup_forces_utf8_for_its_own_child(monkeypatch, tmp_path):
    seen = {}

    class Fake:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        seen["env"] = dict(kw.get("env") or {})
        return Fake()

    lib = tmp_path / "资产库"
    lib.mkdir()
    backup_dir = tmp_path / "资产库备份"
    backup_dir.mkdir()
    (backup_dir / "资产库备份-20260101-000000.zip").write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(subprocess, "run", fake_run)
    _backup(lib)
    assert "-X" in seen["argv"] and "utf8" in seen["argv"], seen["argv"]
    assert seen["env"].get("PYTHONUTF8") == "1"
    assert seen["env"].get("PYTHONIOENCODING") == "utf-8"
    # Windows 的 os.environ 查找大小写不敏感，普通 dict 副本不是 ⇒ 比对要各自归一化
    child_env = {k.upper(): v for k, v in seen["env"].items()}
    host_env = {k.upper(): v for k, v in os.environ.items()}
    assert child_env.get("SYSTEMROOT") == host_env.get("SYSTEMROOT"), "必须复制继承环境而不是另造一套"
    assert child_env.get("PATH") == host_env.get("PATH")


def test_backup_passes_paths_as_argv_not_interpolated_source():
    captured = {}
    real = subprocess.run

    def spy(argv, **kw):
        captured["argv"] = list(argv)
        return real(argv, **kw)

    subprocess.run = spy
    try:
        _backup(pathlib.Path("Q:/不存在/资产库"))
    except Exception:      # noqa: BLE001  这里只校验传参形状，不校验业务结果
        pass
    finally:
        subprocess.run = real
    script = next((a for a in captured["argv"] if a.startswith("import")), "")
    assert "Q:" not in script and "%s" not in script, "路径不应插值进 -c 源码"
    lib_arg = pathlib.Path("Q:/不存在/资产库")
    assert captured["argv"][-3:] == [str(REPO_ROOT), str(lib_arg),
                                     str(lib_arg.parent / "资产库备份")], captured["argv"]
    assert captured["argv"][:2] == [sys.executable, "-X"], captured["argv"]


def test_migration_backup_works_without_utf8_console(monkeypatch, tmp_path):
    """真环境验证：父进程刻意去掉 UTF-8 变量，孙进程仍必须完成备份（emoji 输出保留）。"""
    lib = _legacy_lib(tmp_path)
    before = _hash_all(lib)
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE")}
    script = tmp_path / "run_migrate.py"
    result_file = tmp_path / "result.json"
    script.write_text(
        "import json, pathlib, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from scripts.engine.visual_migration import migrate_legacy_visuals\n"
        "r = migrate_legacy_visuals(pathlib.Path(sys.argv[1]), '仙侠国风电影CG')\n"
        # 结果写文件而不是走 stdout：中文路径经管道时的编码由子进程决定，
        # 拿它当断言依据会把我方 harness 的编码假设当成产品合同。
        f"pathlib.Path({str(result_file)!r}).write_text("
        "json.dumps({k: str(v) for k, v in r.items()}, ensure_ascii=False), encoding='utf-8')\n",
        encoding="utf-8")
    p = subprocess.run([sys.executable, str(script), str(lib)], env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300)
    out = (p.stdout or "") + (p.stderr or "")
    assert p.returncode == 0, out[-900:]
    assert result_file.is_file(), out[-900:]
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload.get("migrated") in (True, "True"), payload
    backup = pathlib.Path(payload["backup"])
    assert backup.is_file() and backup.stat().st_size > 0, payload
    after = _hash_all(lib)
    for rel, digest in before.items():
        assert after[rel] == digest, rel        # 备份语义不变：旧文件一个都不改


def test_real_backup_failure_still_reports_backup_failed(tmp_path):
    """真失败仍要报 BACKUP_FAILED，不能被编码崩溃顶替或被吞掉。"""
    lib = _legacy_lib(tmp_path)
    blocker = tmp_path / "资产库备份"
    blocker.write_text("占位文件，不是目录", encoding="utf-8")   # 备份输出目录不可用
    with pytest.raises(MigrationError) as e:
        migrate_legacy_visuals(lib, "仙侠国风电影CG")
    assert e.value.code == "BACKUP_FAILED", e.value.code
