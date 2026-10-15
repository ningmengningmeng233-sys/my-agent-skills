#!/usr/bin/env python3
"""Offline end-to-end snapshot regression test for the asset extraction skill."""
import os
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SKILL = Path(__file__).resolve().parent.parent
# F1.1：总门链路不注入编码保护伞，Windows 默认编码下必须全绿（IO 边界显式 UTF-8）。
for _enc_var in ("PYTHONUTF8", "PYTHONIOENCODING"):
    os.environ.pop(_enc_var, None)
ENV = {**os.environ}


def run(name: str, *cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV, cwd=str(SKILL))
    ok = r.returncode == 0
    tail = r.stdout.strip().splitlines()
    print(f"  {'✅' if ok else '❌'} {name} (exit {r.returncode})")
    if not ok:
        print("   " + " | ".join(tail[-3:]) if tail else r.stderr[-300:])
        sys.exit(1)
    return r


def main() -> int:
    print("🔍 运行单元测试")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(SKILL / "测试" / "unit"), "-q"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ENV,
        cwd=str(SKILL),
    )
    if r.returncode != 0:
        print(f"  ❌ 单元测试失败 (exit {r.returncode})")
        print(r.stdout[-600:] or r.stderr[-600:])
        return 1
    print("  ✅ 单元测试通过")

    e2e = SKILL / "测试" / "e2e" / "资产库"
    # 候选包按白名单排除 测试/e2e（静态快照不入包）：全新解压副本上没有该目录，
    # 这里必须给出可照做的重建指令，不能以 FileNotFoundError 崩栈收场。
    finals = [f for f in e2e.iterdir() if f.name == "反推_最终.md"] if e2e.is_dir() else []
    if not finals:
        print("❌ e2e 快照缺失（测试/e2e/资产库/反推_最终.md）——请先运行 测试/e2e_flow.py --snapshot 重建（离线 mock，不需要 API）")
        return 1
    final = finals[0]
    print("🔍 e2e 快照自动复验")
    r = run(
        "check_schedule 全套检查(分集/基础/衍生/合规/画风)",
        sys.executable,
        str(SKILL / "scripts" / "check_schedule.py"),
        str(final),
        "--lib",
        str(e2e),
        "--style",
        "仙侠奇幻风",
    )
    assert "全部通过" in r.stdout, r.stdout[-400:]
    run("check_final 反推文件12模块", sys.executable, str(SKILL / "scripts" / "check_final.py"), str(final))
    costume_dir = e2e / "服饰设计" / "测试全流程小说"
    costume_lib = costume_dir / "服饰标准库.md"
    costume_roles = costume_dir / "角色服饰标准表.md"
    if costume_lib.exists() and costume_roles.exists():
        run(
            "check_costumes 服饰样例校验(0阻断0警告)",
            sys.executable,
            str(SKILL / "服饰设计" / "scripts" / "check_costumes.py"),
            str(costume_lib),
            str(costume_roles),
        )
    run("check_style_lib 画风库健康", sys.executable, str(SKILL / "scripts" / "check_style_lib.py"))
    run("run_verdict_test 离线裁决链路", sys.executable, str(SKILL / "测试" / "run_verdict_test.py"))
    side = SKILL / "测试" / "样例" / "反推_最终.md"
    if side.exists():
        side.unlink()
    print("✅ e2e 快照自动复验全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
