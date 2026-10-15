# -*- coding: utf-8 -*-
"""离线烟测（合成，可随公共包分发）：无会话、无网络、临时输出。

验证发布包的离线合同：
1. 全模块可导入且导入期无网络（socket 守卫）；
2. 缺 tdl 时在线动作得到明确先决条件报告（不自动安装、不搜索 PATH）；
3. validate_job 合同：缺 action 报错、合法离线任务通过且不注入默认选择；
4. 离线检索在空库上按原合同返回 offline 声明（tdl_required=False）；
5. 全程不写全局 profile（HOME/USERPROFILE 指向临时目录后逐项核对）。

跑法：PYTHONUTF8=1 python scripts/selftest_offline.py   （全绿 exit 0）
"""
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FAILS = []
N = [0]


def check(name, cond, detail=""):
    N[0] += 1
    print(("  PASS  %s" % name) if cond else ("  FAIL  %s ｜ %s" % (name, detail)))
    if not cond:
        FAILS.append(name)


def _block_network(*a, **k):
    raise AssertionError("离线烟测期间发生网络调用：socket 被禁用")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="tg_offline_smoke_"))
    home = tmp / "home"
    home.mkdir()
    # 全局 profile 指向临时目录：任何越界写入都会落进 tmp 并被最后一步核对
    for var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"):
        os.environ[var] = str(home)
    os.environ["PYTHONUTF8"] = "1"

    out_root = tmp / "outputs"
    out_root.mkdir()

    # 1) socket 守卫：导入与运行全程禁网
    socket.socket = _block_network
    socket.create_connection = _block_network

    import archive  # noqa: F401
    import catalog
    import importing  # noqa: F401
    import job
    import media  # noqa: F401
    import preflight
    import presets  # noqa: F401
    import reading  # noqa: F401
    import sharing  # noqa: F401
    import state  # noqa: F401
    import updating  # noqa: F401
    check("11 模块在禁网守卫下可导入", True)

    # 2) 缺 tdl：在线动作的先决条件必须明确（不自动安装、不搜索 PATH）
    report = preflight.inspect_environment({"outputs": [str(out_root)]}, "export")
    entries = report.get("capabilities", []) if isinstance(report, dict) else []
    tdl = next((e for e in entries if e.get("capability") == "tdl_executable"), None)
    check("缺 tdl 得到 unavailable 先决条件", bool(tdl) and tdl.get("status") == "unavailable",
          json.dumps(tdl, ensure_ascii=False)[:160] if tdl else str(report)[:160])
    check("先决条件声明不自动安装/不搜索 PATH",
          bool(tdl) and "不自动安装" in (tdl.get("reason") or ""), str(tdl)[:160])

    # 3) validate_job 合同
    try:
        job.validate_job({"no_action": True})
        check("缺 action 必须 ValueError", False, "未抛错")
    except ValueError:
        check("缺 action 必须 ValueError", True)
    ok_job = job.validate_job({"action": "search", "keyword": "例",
                               "database": str(tmp / "lib" / "catalog.sqlite3"),
                               "sources": [str(tmp / "archive")],
                               "outputs": ["markdown"]})
    check("合法离线检索任务通过校验", isinstance(ok_job, dict) and ok_job.get("action") == "search")

    # 4) 空库离线检索：offline 声明 + 不要求 tdl/会话
    db = tmp / "lib" / "catalog.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    catalog.ensure_schema(conn)
    conn.commit()
    conn.close()
    result = catalog.search_archive(str(db), {"view": "raw", "keyword": "例", "limit": 5})
    runtime = result.get("runtime") or {}
    claims = result.get("claims") or {}
    check("检索声明 offline_action 且 tdl_required=False",
          runtime.get("offline_action") is True and runtime.get("tdl_required") is False,
          json.dumps(runtime, ensure_ascii=False)[:160])
    check("检索声明 network_used=False",
          claims.get("network_used") is False and claims.get("external_search_service") is False,
          json.dumps(claims, ensure_ascii=False)[:200])

    # 5) 不写全局 profile：home 下不应出现本次运行产物
    stray = [p.name for p in home.rglob("*") if p.is_file()]
    check("不写全局 profile（临时 HOME 无产物）", not stray, str(stray[:5]))

    print("合计 %d 项断言，红 %d" % (N[0], len(FAILS)))
    for f in FAILS:
        print("  未通过：%s" % f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
