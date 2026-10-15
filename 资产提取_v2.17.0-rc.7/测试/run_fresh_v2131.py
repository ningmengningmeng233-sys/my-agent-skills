#!/usr/bin/env python3
"""v2.13.1 fresh 全流程回归（R2d/R3a/R3c）：真实执行路径 + 签发后回退矩阵。

不复用裁决断点、不手写交付清单：fresh 正例完全复用 e2e_flow.py 的真实命令链
（拆段→登记→提取→聚合→描述/基础/衍生→verify 真实签发→status --json DELIVERED），
再在真实签发的库上执行"签发后变化"矩阵：
  改原始输入 / 删必需基础节 / 删调度清单行 / 删关系端点 / 改场景母版 / 画风内容变化
每次扰动必须脱离 DELIVERED（或校验器非零），恢复后重跑 verify 必须重新达到 DELIVERED。
签发门负例（E3）：删 .start-state.json / 删原始源 / 显式 --style 与启动画风冲突，
  三者都必须 verify 非零、写原因码、不覆盖历史清单、脱离 DELIVERED，恢复后重新签发。
画风内容变化用隔离技能包副本执行真实 status，不改仓库真实画风文件。
失败即非零退出并输出失败检查名；所有操作只针对临时库与临时副本。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL = Path(__file__).resolve().parent.parent
# F1.1：本 runner 必须在 Windows 默认编码（GBK 控制台/区域设置）下全程可用，
# 子进程不得依赖 PYTHONUTF8/PYTHONIOENCODING 保护伞；跨平台编码只在实际
# 读写与标准输出边界显式 UTF-8 处理（subprocess encoding="utf-8", errors="replace"）。
for _enc_var in ("PYTHONUTF8", "PYTHONIOENCODING"):
    os.environ.pop(_enc_var, None)
ENV = {**os.environ, "ASSET_WORKERS": "8"}
assert "PYTHONUTF8" not in ENV and "PYTHONIOENCODING" not in ENV, "编码保护伞必须移除"
REQUIRED_CHECKS = ("check_schedule", "check_final", "check_asset_index")


def _run(cmd: list, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable] + [str(c) for c in cmd], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=dict(ENV), cwd=str(cwd or SKILL),
    )


def _assert(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"（{detail}）" if detail and not cond else ""))
    if not cond:
        if detail:
            print(f"     {str(detail)[:400]}")
        sys.exit(1)


def _status_json(lib: Path, skill_root: Path | None = None) -> tuple[int, dict]:
    """真实 maintenance.py status --json；skill_root 可指向隔离技能包副本。"""
    root = skill_root or SKILL
    r = _run([root / "scripts" / "maintenance.py", "status", "--lib", str(lib), "--json"])
    try:
        return r.returncode, json.loads(r.stdout)
    except ValueError:
        return r.returncode, {}


def _verify(lib: Path, style: str = "仙侠奇幻风") -> subprocess.CompletedProcess:
    return _run([SKILL / "scripts" / "maintenance.py", "verify", "--lib", str(lib), "--style", style])


def _init(lib: Path, src: Path, genre: str = "古风", style: str = "仙侠奇幻风") -> subprocess.CompletedProcess:
    """真实 maintenance.py init（E2 手动初始化入口，同时用于恢复被删的启动状态）。"""
    return _run([SKILL / "scripts" / "maintenance.py", "init", "--lib", str(lib),
                 "--input-source", str(src), "--genre", genre, "--style", style])


def _expect_undelivered(name: str, lib: Path, expect_reason: str | None = None,
                        skill_root: Path | None = None) -> None:
    _rc, payload = _status_json(lib, skill_root=skill_root)
    ok = payload.get("pipeline_state") != "DELIVERED"
    if expect_reason:
        ok = ok and payload.get("reason_code") == expect_reason
    expected = f" 且 reason={expect_reason}" if expect_reason else ""
    _assert(name, ok,
            f"实际 state={payload.get('pipeline_state')}/{payload.get('reason_code')}，期望≠DELIVERED{expected}")


def _expect_redelivered(name: str, lib: Path) -> None:
    r = _verify(lib)
    _rc, payload = _status_json(lib)
    _assert(name, r.returncode == 0 and payload.get("pipeline_state") == "DELIVERED",
            f"verify rc={r.returncode}（{r.stdout[-200:]}）state={payload.get('pipeline_state')}")


def main() -> int:
    print("🔍 v2.13.1 fresh 回归：真实执行路径 + 签发后回退矩阵")

    # ---- 第一步：真实执行链（e2e_flow 含 verify 签发与 status DELIVERED 断言）----
    print("🔍 运行 e2e_flow 真实命令链（临时空库 → DELIVERED）")
    flow = _run([SKILL / "测试" / "e2e_flow.py"])
    if flow.returncode != 0:
        print(flow.stdout[-1500:])
        _assert("e2e_flow 真实链路全绿", False, "退出码非零")
    m = re.search(r"产物目录: (.+)", flow.stdout)
    _assert("解析产物库路径", bool(m), flow.stdout[-300:])
    lib = Path(m.group(1).strip())

    manifest_path = lib / "交付清单.json"
    _assert("交付清单已真实签发", manifest_path.is_file())
    mf = json.loads(manifest_path.read_text(encoding="utf-8"))
    names = {c.get("name") for c in mf.get("checks", []) if isinstance(c, dict)}
    _assert("清单状态与必需检查集合", mf.get("status") == "DELIVERED"
            and set(REQUIRED_CHECKS) <= names
            and all(c.get("rc") == 0 for c in mf.get("checks", [])),
            f"status={mf.get('status')} checks={sorted(n for n in names if n)}")
    _rc, payload = _status_json(lib)
    _assert("status --json=DELIVERED/DELIVERY_VALID",
            payload.get("pipeline_state") == "DELIVERED" and payload.get("reason_code") == "DELIVERY_VALID",
            f"{payload.get('pipeline_state')}/{payload.get('reason_code')}")

    # ---- 回退矩阵（真实 status/verify/校验器）----
    print("🔍 回退矩阵：签发后变化必须脱离 DELIVERED")

    # a. 改原始输入（源改了但尚未重拆段）
    st = json.loads((lib / ".start-state.json").read_text(encoding="utf-8"))
    src = Path(st["input_source"])
    _assert("前置：原始源文件独立存在", src.is_file(), str(src))
    raw = src.read_text(encoding="utf-8")
    src.write_text(raw + "\n追更一行尚未重拆段", encoding="utf-8")
    _expect_undelivered("改源→SOURCE_CHANGED", lib, "SOURCE_CHANGED")
    src.write_text(raw, encoding="utf-8")
    _expect_redelivered("恢复源→重验 DELIVERED", lib)

    # b. 删一个必需基础节（R3c：verify 失败不得覆盖交付清单）
    manifest_bytes = manifest_path.read_bytes()
    base = lib / "提示词" / "基础" / "人物提示词.md"
    braw = base.read_text(encoding="utf-8")
    heads = list(re.finditer(r"^## \d{3}\. .+$", braw, re.M))
    _assert("前置：人物基础节存在", bool(heads))
    base.write_text(braw[:heads[-1].start()], encoding="utf-8")
    r = _verify(lib)
    _assert("删基础节→verify 失败", r.returncode != 0, f"rc={r.returncode}")
    _assert("失败不覆盖历史交付清单", manifest_path.read_bytes() == manifest_bytes)
    _expect_undelivered("删基础节→脱离 DELIVERED", lib)
    base.write_text(braw, encoding="utf-8")
    _expect_redelivered("恢复基础节→重验 DELIVERED", lib)

    # c. 删调度清单数据行
    sched = lib / "提示词" / "调度清单.md"
    sraw = sched.read_text(encoding="utf-8")
    lines = sraw.splitlines(keepends=True)
    data_rows = [i for i, l in enumerate(lines)
                 if l.strip().startswith("|") and not l.strip().startswith("|--")
                 and "类型" not in l and "母版" not in l]
    _assert("前置：调度清单有数据行", bool(data_rows))
    sched.write_text("".join(lines[:data_rows[0]] + lines[data_rows[0] + 1:]), encoding="utf-8")
    _expect_undelivered("删调度行→脱离 DELIVERED", lib)
    sched.write_text(sraw, encoding="utf-8")
    _expect_redelivered("恢复调度行→重验 DELIVERED", lib)

    # d. 删一个关系端点资产（索引内出现悬空边）
    idx_path = lib / ".process" / "资产索引.json"
    iraw = idx_path.read_bytes()
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    victim = next((a for a in idx["assets"] if a["type"] == "场景"), None)
    _assert("前置：存在场景资产可删除", victim is not None)
    idx["assets"] = [a for a in idx["assets"] if a["asset_id"] != victim["asset_id"]]
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    r = _run([SKILL / "scripts" / "check_asset_index.py", "--lib", str(lib)])
    _assert("删关系端点→check_asset_index 非零", r.returncode != 0, r.stdout[-200:])
    _expect_undelivered("删关系端点→脱离 DELIVERED", lib)
    idx_path.write_bytes(iraw)
    _expect_redelivered("恢复索引→重验 DELIVERED", lib)

    # e. 改场景母版（证据锚点漂移的校验器语义另由 test_continuity 覆盖）
    master = lib / "连续性" / "场景母版.md"
    mraw = master.read_text(encoding="utf-8")
    _assert("前置：场景母版存在", bool(mraw.strip()))
    master.write_text(mraw + "\n<!-- 签发后人为改写 -->\n", encoding="utf-8")
    _expect_undelivered("改母版→脱离 DELIVERED", lib)
    master.write_text(mraw, encoding="utf-8")
    _expect_redelivered("恢复母版→重验 DELIVERED", lib)

    # f. 画风内容变化：隔离技能包副本上跑真实 status（不动仓库真实画风文件）
    with tempfile.TemporaryDirectory(prefix="v2131_skillcopy_") as ct:
        copy = Path(ct) / "技能包"
        shutil.copytree(SKILL, copy,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "测试", "e2e"))
        card = copy / "references" / "画风库.md"
        craw = card.read_text(encoding="utf-8")
        marker = "云雾缭绕，仙风道骨"
        _assert("前置：A2 卡内容标记存在", marker in craw)
        card.write_text(craw.replace(marker, "云雾缭绕X，仙风道骨", 1), encoding="utf-8")
        _expect_undelivered("画风内容变→STYLE_CHANGED（副本内真实 status）", lib,
                            "STYLE_CHANGED", skill_root=copy)
    _expect_redelivered("画风副本销毁→原包重验 DELIVERED", lib)

    # ---- 签发门负例（E1/E2/E3）：误签发三类来源必须被长期门拦下 ----
    print("🔍 签发门负例：缺状态 / 缺源 / 画风冲突一律拒签且不覆盖历史清单")

    # g. 删 .start-state.json：曾经可直接签发，现必须拒签；用真实 init 恢复后再签发
    manifest_bytes = manifest_path.read_bytes()
    state_path = lib / ".start-state.json"
    state_raw = state_path.read_text(encoding="utf-8")
    state_path.unlink()
    r = _verify(lib)
    _assert("删启动状态→verify 非零", r.returncode != 0, f"rc={r.returncode}")
    _assert("删启动状态→报告 SOURCE_UNVERIFIABLE", "SOURCE_UNVERIFIABLE" in r.stdout, r.stdout[-300:])
    _assert("删启动状态→不覆盖历史交付清单", manifest_path.read_bytes() == manifest_bytes)
    _expect_undelivered("删启动状态→脱离 DELIVERED", lib, "SOURCE_UNVERIFIABLE")
    ri = _init(lib, src)
    _assert("init 恢复启动状态退出 0", ri.returncode == 0, ri.stdout[-300:])
    _expect_redelivered("init 恢复→重验 DELIVERED", lib)

    # h. 删原始源文件（源不可核验即不可交付）
    manifest_bytes = manifest_path.read_bytes()
    sraw_bytes = src.read_bytes()
    src.unlink()
    r = _verify(lib)
    _assert("删原始源→verify 非零", r.returncode != 0, f"rc={r.returncode}")
    _assert("删原始源→报告 SOURCE_UNVERIFIABLE", "SOURCE_UNVERIFIABLE" in r.stdout, r.stdout[-300:])
    _assert("删原始源→不覆盖历史交付清单", manifest_path.read_bytes() == manifest_bytes)
    _expect_undelivered("删原始源→脱离 DELIVERED", lib, "SOURCE_UNVERIFIABLE")
    src.write_bytes(sraw_bytes)
    _expect_redelivered("恢复原始源→重验 DELIVERED", lib)

    # i. 画风篡改：启动画风改另一张卡 → 失效；显式 --style 与启动画风冲突 → 拒签 STYLE_MISMATCH
    manifest_bytes = manifest_path.read_bytes()
    tampered = json.loads(state_raw)
    tampered["style"] = "H12"
    state_path.write_text(json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8")
    _expect_undelivered("改启动画风→脱离 DELIVERED", lib, "STYLE_CHANGED")
    r = _verify(lib)
    _assert("画风冲突→verify 非零", r.returncode != 0, f"rc={r.returncode}")
    _assert("画风冲突→报告 STYLE_MISMATCH", "STYLE_MISMATCH" in r.stdout, r.stdout[-300:])
    _assert("画风冲突→不覆盖历史交付清单", manifest_path.read_bytes() == manifest_bytes)
    state_path.write_text(state_raw, encoding="utf-8")
    _expect_redelivered("恢复启动画风→重验 DELIVERED", lib)

    print("✅ v2.13.1 fresh 回归全部通过（真实签发 + 回退矩阵 + 签发门负例 + 恢复重验）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
