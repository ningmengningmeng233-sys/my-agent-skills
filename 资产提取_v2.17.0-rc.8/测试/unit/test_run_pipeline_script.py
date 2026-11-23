import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENTRY = REPO_ROOT / "run_pipeline.sh"


def _find_usable_bash() -> str | None:
    """查找真实可用的 bash（避开 Windows 上 WindowsApps/System32 的 WSL 空壳占位器）。"""
    candidates: list[str] = []
    if os.name == "nt":
        for p in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ):
            if Path(p).is_file():
                candidates.append(p)
    which_bash = shutil.which("bash")
    if which_bash:
        candidates.append(which_bash)
    for cand in candidates:
        try:
            probe = subprocess.run([cand, "-c", "exit 0"], capture_output=True, timeout=5)
            if probe.returncode == 0:
                return cand
        except Exception:
            continue
    return None


def test_entry_script_exists_at_repo_root():
    """一键入口必须真的在仓库根。

    这条看着多余，但 F34 的成因之一就是下面那个测试用**相对路径**跑
    `bash run_pipeline.sh`：在仓库里能过，在解压出来的包里那个文件却不存在
    （白名单当时漏了它），而发布门是在仓库里跑的——于是没有任何一处会发现
    "包里没有入口脚本"。现在改成按仓库根定位，并把入口列入发布守卫。
    """
    assert ENTRY.is_file(), f"缺少一键入口 {ENTRY}"
    assert (REPO_ROOT / "run_pipeline.bat").is_file(), "缺少 Windows 一键入口 run_pipeline.bat"


def test_dry_run_lists_steps(tmp_path):
    bash = _find_usable_bash()
    if not bash:
        pytest.skip("缺少可用 bash（仅检测到未初始化的 WSL 占位器）")
    out = subprocess.run(
        [bash, str(ENTRY), "--lib", str(tmp_path), "--dry-run"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
    )
    assert out.returncode == 0, (out.stdout or "") + (out.stderr or "")
    for step in ("拆分", "登记", "提取", "聚合", "对账", "服饰", "描述", "提示词", "verify"):
        assert step in (out.stdout or "")
    assert not (tmp_path / ".process" / "耗时账本.jsonl").exists()      # dry-run 不出网不落账


def test_entry_script_is_lf_only():
    """AE-11：入口脚本必须 LF-only，否则 Linux/macOS 解析 shebang 崩溃。

    CRLF 下内核把解释器读成 `/usr/bin/env bash\\r`（No such file），
    脚本在 POSIX 直接无法启动。Windows 检出由 .gitattributes 锁定 LF。
    """
    raw = ENTRY.read_bytes()
    assert b"\r" not in raw, "run_pipeline.sh 含 CR，必须为纯 LF 换行"


def test_gitattributes_locks_entry_script_to_lf():
    """AE-11：.gitattributes 必须显式锁定入口脚本 eol=lf。"""
    candidates = [REPO_ROOT / ".gitattributes", REPO_ROOT.parent / ".gitattributes"]
    decl = ""
    for cand in candidates:
        if cand.is_file():
            decl += cand.read_text(encoding="utf-8", errors="replace") + "\n"
    assert "run_pipeline.sh" in decl and "eol=lf" in decl, \
        "缺少 run_pipeline.sh text eol=lf 锁定规则，Windows 检出会转回 CRLF"
