#!/usr/bin/env python3

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
if load_dotenv is not None:
    load_dotenv(str(SKILL_DIR / ".env"))

from transcript_db import TranscriptDB  # noqa: E402
from generate_summary import display_key, generate_summary_by_bvid  # noqa: E402
from logger import log, error as log_error  # noqa: E402

_print_lock = Lock()


def _process_one(bvid: str, page: int, title: str) -> tuple[str, str, bool]:
    key = display_key(bvid, page)
    try:
        ok, _ = generate_summary_by_bvid(bvid, page=page)
        with _print_lock:
            mark = "✅" if ok else "❌"
            suffix = "" if ok else " (失败)"
            print(f"   {mark} {key} - {title[:40]}{suffix}")
        return key, title, ok
    except Exception as e:
        with _print_lock:
            print(f"   ❌ {key} - {title[:40]} (异常: {e})")
        log_error("fill_summaries", f"{key} 异常: {e}")
        return key, title, False


def main():
    parser = argparse.ArgumentParser(description="批量补全视频摘要")
    parser.add_argument(
        "--dry-run", action="store_true", help="只显示待处理数量，不调API"
    )
    parser.add_argument("--stats", action="store_true", help="显示统计信息")
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="并行worker数（默认5，LLM API为I/O密集型，增大可加速）",
    )
    args = parser.parse_args()

    with TranscriptDB() as db:
        if args.stats:
            s = db.stats()
            print("📊 数据库统计:")
            print(f"   总记录: {s['total']}")
            print(f"   已有摘要: {s['with_summary']}")
            print(f"   待补摘要: {s['pending_summary']}")
            return 0
        pending = db.get_pending_summaries()

    if not pending:
        print("✅ 所有视频都已有摘要，无需处理")
        return 0

    print(f"📋 发现 {len(pending)} 个视频待补摘要")
    log("fill_summaries", f"开始批量补摘要，共 {len(pending)} 个待处理")

    if args.dry_run:
        for r in pending:
            key = display_key(r["bvid"], r.get("page", 1))
            print(f"   - {key} | {r['title'][:30]}...")
        return 0

    workers = min(args.workers, len(pending))
    print(f"🔄 使用 {workers} 个并行 worker...")

    success_count = 0
    fail_count = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_process_one, r["bvid"], r.get("page", 1), r["title"]): r
            for r in pending
        }
        for future in as_completed(futures):
            _, _, ok = future.result()
            if ok:
                success_count += 1
            else:
                fail_count += 1

    print(f"\n{'=' * 50}")
    print(f"📊 完成: 成功 {success_count}, 失败 {fail_count}, 总计 {len(pending)}")
    log(
        "fill_summaries",
        f"批量补摘要完成: 成功 {success_count}, 失败 {fail_count}, 总计 {len(pending)}",
    )
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
