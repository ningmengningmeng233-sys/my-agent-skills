import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENTRY = REPO_ROOT / "run_pipeline.sh"


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
    out = subprocess.run(["bash", str(ENTRY), "--lib", str(tmp_path), "--dry-run"],
                         capture_output=True, text=True, encoding="utf-8",
                         cwd=str(REPO_ROOT))
    assert out.returncode == 0, out.stdout + out.stderr
    for step in ("拆分", "登记", "提取", "聚合", "对账", "服饰", "描述", "提示词", "verify"):
        assert step in out.stdout
    assert not (tmp_path / ".process" / "耗时账本.jsonl").exists()      # dry-run 不出网不落账
