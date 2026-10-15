#!/usr/bin/env python3
"""维护统一入口：体检、批次恢复、僵尸回收、库与归档校验。

为什么收拢成一个入口：这些操作原先散在 6 个脚本里（doctor／migrations／
import_archive／migrate_pages／backfill_page_meta／fill_summaries），
出问题时得先想「该用哪一个」。这里只做分派，不重写各脚本的逻辑。

    .venv/Scripts/python.exe scripts/maintenance.py <command> [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import batch_state  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = SKILL_DIR / "state" / "batch_state.json"
DB_PATH = SKILL_DIR / ".db" / "transcripts.db"

# 直接跑本脚本时不会自动读 .env，而 verify/doctor 要看 BILI_ARCHIVE_DIR 等配置。
try:
    from dotenv import load_dotenv

    load_dotenv(str(SKILL_DIR / ".env"))
except ImportError:
    pass

COMMANDS = ("doctor", "recover-stale", "resume-batch", "list-batches", "verify")


def cmd_doctor(args) -> int:
    """转发到 doctor；只透传退出码与报告。"""
    import doctor

    report = doctor.run_all()
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for item in report["checks"]:
            mark = "✅" if item["ok"] else ("⚠️" if item["severity"] == "warning" else "❌")
            print(f"{mark} {item['name']}: {item['detail']}")
        print(f"\n错误 {report['errors']} 项，警告 {report['warnings']} 项")
    return 0 if report["ok"] else 1


def cmd_recover_stale(args) -> int:
    """回收心跳超时的 in-flight 条目；没有状态文件时视为无事可做。"""
    state = batch_state.BatchState.load(STATE_PATH)
    recovered = state.recover_stale(seconds=args.seconds) if state else 0
    if args.json:
        print(json.dumps({"recovered": recovered,
                          "state_file": str(STATE_PATH)}, ensure_ascii=False))
    else:
        print(f"回收 {recovered} 个僵尸任务")
    return 0


def cmd_resume_batch(args) -> int:
    """认证恢复续跑：把 auth_required 条目转回待跑。"""
    state = batch_state.BatchState.load(STATE_PATH)
    if state is None:
        message = "没有批次状态文件，无需恢复"
        if args.json:
            print(json.dumps({"ok": False, "error": "no_state"}, ensure_ascii=False))
        else:
            print(message, file=sys.stderr)
        return 1
    try:
        state.resume()
    except ValueError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"无法恢复：{exc}", file=sys.stderr)
        return 1
    pending = state.next_pending()
    if args.json:
        print(json.dumps({"ok": True, "status": state.status,
                          "counts": state.summary(),
                          "next": pending.get("key") if pending else None},
                         ensure_ascii=False))
    else:
        print(f"批次已恢复：{state.summary()}")
        if pending:
            print(f"下一条：{pending.get('key')}")
    return 0


def cmd_list_batches(args) -> int:
    state = batch_state.BatchState.load(STATE_PATH)
    if state is None:
        if args.json:
            print(json.dumps({"batches": []}, ensure_ascii=False))
        else:
            print("没有批次记录")
        return 0
    payload = {"batch_id": state.batch_id, "status": state.status,
               "counts": state.summary(), "state_file": str(STATE_PATH)}
    if state.is_paused:
        payload["pause_reason"] = state.pause_reason
        payload["paused_at"] = state.paused_at
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"批次 {payload['batch_id']}｜状态 {payload['status']}")
        if state.is_paused:
            print(f"  暂停原因: {state.pause_reason or '未记录'}"
                  f"（暂停于 {state.paused_at or '未知时间'}）")
        for name, count in sorted(payload["counts"].items()):
            print(f"  {name}: {count}")
    return 0


def _database_report(db_path: Path) -> dict:
    if not db_path.is_file():
        return {"ok": False, "detail": "数据库不存在"}
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"ok": False, "detail": f"无法打开：{type(exc).__name__}"}
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        rows = connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
        works = connection.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT bvid FROM transcripts)").fetchone()[0]
        versions = connection.execute(
            "SELECT COUNT(*) FROM transcript_versions").fetchone()[0]
        scored = connection.execute(
            "SELECT COUNT(*) FROM transcript_versions "
            "WHERE quality_score IS NOT NULL").fetchone()[0]
    except sqlite3.Error as exc:
        return {"ok": False, "detail": f"查询失败：{type(exc).__name__}"}
    finally:
        connection.close()
    return {"ok": integrity == "ok", "integrity": integrity, "rows": rows,
            "works": works, "versions": versions, "scored": scored}


def _archive_report() -> dict:
    import os

    raw = os.environ.get("BILI_ARCHIVE_DIR", "").strip()
    if not raw:
        return {"ok": False, "detail": "未配置 BILI_ARCHIVE_DIR"}
    root = Path(raw)
    if not root.is_dir():
        return {"ok": False, "detail": f"目录不存在：{root}"}
    files = [p for p in root.rglob("*.txt") if p.is_file()]
    return {"ok": True, "path": str(root), "files": len(files)}


def cmd_verify(args) -> int:
    """只读校验：库完整性 + 归档可读性。不改任何数据。"""
    payload = {"database": _database_report(DB_PATH),
               "archive": _archive_report()}
    ok = payload["database"].get("ok") and payload["archive"].get("ok")
    payload["ok"] = bool(ok)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for name in ("database", "archive"):
            item = payload[name]
            mark = "✅" if item.get("ok") else "❌"
            print(f"{mark} {name}: {item.get('detail') or item}")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="maintenance.py",
        description="维护入口：" + "／".join(COMMANDS),
        epilog="示例：maintenance.py doctor --json")
    parser.add_argument("command", nargs="?", help="子命令")
    parser.add_argument("--seconds", type=int, default=2 * 60 * 60 + 300,
                        help="僵尸判定的心跳超时秒数")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    handlers = {
        "doctor": cmd_doctor,
        "recover-stale": cmd_recover_stale,
        "resume-batch": cmd_resume_batch,
        "list-batches": cmd_list_batches,
        "verify": cmd_verify,
    }
    if args.command not in handlers:
        parser.print_help()
        return 2
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
