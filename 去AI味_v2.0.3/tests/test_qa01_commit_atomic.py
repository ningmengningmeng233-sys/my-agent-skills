# -*- coding: utf-8 -*-
"""QA-01/QA-02 回归：commit_rewrite 必须委托 overwrite_atomically，保证权限与原子替换。

缺陷：commit_rewrite 自带一套重复写盘实现——非排他备份、无替换前复查、
os.replace 后不贴回原权限位（临时文件 0o600 语义覆盖目标，0o644 退化；只读
目标直接 WinError 5 失败）。修复：校验门之后统一委托 overwrite_atomically，
并同步重算 release-manifest.json（bytes/sha256/package_digest）。

受控 RED：以下断言在修复前必须失败，修复后通过。全离线；机械验证器用桩
代替（只测提交通道，不测验证器本身）。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

import file_commit
from file_commit import SafeFileCommitEngine

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PKG_ROOT = SCRIPTS.parent


def _stub_validator(monkeypatch):
    monkeypatch.setattr(
        SafeFileCommitEngine, "_run_mechanical_validator",
        staticmethod(lambda *a: ("PASS_MECHANICAL", [], 0)))


def _writable_tree(root: Path):
    for p in root.rglob("*"):
        try:
            if p.is_file() or p.is_dir():
                os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) | stat.S_IWRITE)
        except OSError:
            pass


def test_commit_rewrite_preserves_mode_on_readonly_target(tmp_path, monkeypatch):
    """只读目标也能提交，且提交后原权限位原样保留（权限退化 RED）。"""
    _stub_validator(monkeypatch)
    target = tmp_path / "doc.md"
    target.write_text("line1\nline2\n", encoding="utf-8")
    os.chmod(target, stat.S_IREAD)
    ro_mode = stat.S_IMODE(os.lstat(target).st_mode)
    try:
        res = SafeFileCommitEngine.commit_rewrite(target, "line1 changed\nline2\n")
        assert res["status"] == "COMMITTED", res
        assert target.read_text(encoding="utf-8") == "line1 changed\nline2\n"
        assert stat.S_IMODE(os.lstat(target).st_mode) == ro_mode, \
            f"原权限位未保留：{oct(ro_mode)} -> {oct(stat.S_IMODE(os.lstat(target).st_mode))}"
    finally:
        _writable_tree(tmp_path)


def test_commit_rewrite_keeps_mode_and_bytes_on_writable_target(tmp_path, monkeypatch):
    """可写目标：提交成功、字节更新、权限位不变（委托合同基线）。"""
    _stub_validator(monkeypatch)
    target = tmp_path / "doc.md"
    target.write_text("line1\nline2\n", encoding="utf-8")
    before_mode = stat.S_IMODE(os.lstat(target).st_mode)
    res = SafeFileCommitEngine.commit_rewrite(target, "line1 changed\nline2\n")
    assert res["status"] == "COMMITTED", res
    assert res["verdict"] == "PASS_MECHANICAL"
    assert res["backup_path"] and Path(res["backup_path"]).is_file()
    assert target.read_text(encoding="utf-8") == "line1 changed\nline2\n"
    assert stat.S_IMODE(os.lstat(target).st_mode) == before_mode
    assert hashlib.sha256(target.read_bytes()).hexdigest() == res["target_sha256"]


def test_commit_rewrite_rechecks_target_before_replace(tmp_path, monkeypatch):
    """校验后、替换前若目标被他人改动：必须 ABORTED_RACE 且不得丢弃他人字节。

    在提交通道创建临时文件的瞬间注入一次并发写入（模拟验证与替换之间的
    竞争窗口），同时覆盖新旧两种实现的临时文件创建入口。
    """
    _stub_validator(monkeypatch)
    target = tmp_path / "doc.md"
    target.write_text("original\n", encoding="utf-8")
    concurrent = b"concurrent writer\n"
    fired = {"n": 0}
    real_mkstemp = tempfile.mkstemp
    real_ntf = tempfile.NamedTemporaryFile

    def _inject_once():
        if fired["n"] == 0:
            fired["n"] += 1
            target.write_bytes(concurrent)

    def _mkstemp(*args, **kwargs):
        out = real_mkstemp(*args, **kwargs)
        _inject_once()
        return out

    def _ntf(*args, **kwargs):
        out = real_ntf(*args, **kwargs)
        _inject_once()
        return out

    monkeypatch.setattr(tempfile, "mkstemp", _mkstemp)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", _ntf)

    res = SafeFileCommitEngine.commit_rewrite(target, "candidate\n")
    assert fired["n"] == 1, "并发注入未命中提交窗口"
    assert res["status"] == "ABORTED_RACE", res
    assert target.read_bytes() == concurrent, "他人字节被丢弃或被陈旧备份覆盖"


def test_commit_rewrite_interrupt_paths_stay_atomic(tmp_path, monkeypatch):
    """中断/复核失败：ROLLED_BACK + 目标字节原样 + 无残留临时件与孤儿备份。"""
    _stub_validator(monkeypatch)
    target = tmp_path / "doc.md"
    target.write_text("a\n", encoding="utf-8")
    original = target.read_bytes()

    r1 = SafeFileCommitEngine.commit_rewrite(target, "b\n", force_interrupt=True)
    assert r1["status"] == "ROLLED_BACK", r1
    assert target.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == [], "残留临时件"
    assert list(tmp_path.glob("*.bak*")) == [], "替换前中断不得留下孤儿备份"

    r2 = SafeFileCommitEngine.commit_rewrite(target, "b\n", simulate_corrupt_write=True)
    assert r2["status"] == "ROLLED_BACK", r2
    assert target.read_bytes() == original


def test_release_manifest_matches_tree_and_digest():
    """发行身份清单闭环：file_commit 条目与磁盘一致，package_digest 可复算，
    runtime_identity() 校验通过。"""
    import package_identity as pi

    manifest_path = PKG_ROOT / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = pi.read_skill_version(PKG_ROOT / "SKILL.md")
    assert manifest["version"] == version, "发行清单与权威版本不一致"
    for entry in manifest["files"]:
        data = (PKG_ROOT / entry["path"]).read_bytes()
        assert len(data) == int(entry["bytes"]), f"bytes 不符: {entry['path']}"
        assert hashlib.sha256(data).hexdigest() == str(entry["sha256"]).lower(), \
            f"sha256 不符: {entry['path']}"
    assert manifest["package_digest"] == pi.package_digest(manifest["files"])

    import v2_task
    ident = v2_task.runtime_identity(PKG_ROOT)
    assert ident["origin"] == "installed"
    assert ident["skill_version"] == version
    assert ident["package_digest"] == manifest["package_digest"]
