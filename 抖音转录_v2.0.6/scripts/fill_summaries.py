"""补跑待摘要记录：只读数据库文本，不下载媒体、不调 ingest、不调 ASR。

补摘要成功后回写数据库并重新渲染对应 TXT；单条失败保持 pending 并继续。
CLI 标准输出一行 JSON。--stats 只输出统计；--dry-run 不调用 API。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import paths
from scripts.logger import configure_logging, ensure_utf8_stdio
from scripts.generate_summary import generate_or_pending
from scripts.render_txt import render_record
from scripts.transcript_db import TranscriptDB

logger = logging.getLogger("douyin_transcript.fill")


def stats_json(db: TranscriptDB) -> dict:
    return db.stats()


def run_fill(
    db: TranscriptDB,
    *,
    summary_fn=generate_or_pending,
    renderer=render_record,
    output_root: Path | None = None,
    dry_run: bool = False,
    only_aweme_id: str | None = None,
) -> dict:
    output_root = Path(output_root or paths.OUTPUT_DIR)
    pending = db.pending_summaries()
    if only_aweme_id:
        # 继承 B站版 generate_summary --bvid 的单条补摘要入口
        pending = [r for r in pending if r["aweme_id"] == str(only_aweme_id)]
    out = {"pending_found": len(pending), "summarized": 0,
           "still_pending": 0, "render_failed": 0}
    if dry_run:
        out["dry_run"] = True
        return out

    for record in pending:
        aweme_id = record["aweme_id"]
        try:
            status, summary_text = summary_fn(record.get("title", ""),
                                              record.get("transcript_text", ""))
        except Exception as exc:
            logger.warning("作品 %s 摘要请求异常（%s），保持待补",
                           aweme_id, type(exc).__name__)
            out["still_pending"] += 1
            continue
        if status != "summarized":
            out["still_pending"] += 1
            continue
        db.mark_summarized(aweme_id, summary_text)
        out["summarized"] += 1
        updated = db.get_by_aweme_id(aweme_id)
        try:
            txt_path = renderer(updated, output_root)
        except Exception:
            logger.exception("作品 %s 的 TXT 重渲染失败（摘要已入库）", aweme_id)
            out["render_failed"] += 1
            continue
        db.set_transcript_file(aweme_id, txt_path)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补跑待摘要并重新渲染 TXT")
    parser.add_argument("--stats", action="store_true", help="只输出数据库统计")
    parser.add_argument("--dry-run", action="store_true", help="只列出待补数量，不调用 API")
    parser.add_argument("--db", default=None, help="数据库路径（默认 .db/transcripts.db）")
    parser.add_argument("--aweme-id", default=None,
                        help="只补指定作品的摘要（继承 B站版单条补摘要入口）")
    args = parser.parse_args(argv)

    paths.ensure_runtime_dirs()
    ensure_utf8_stdio()
    paths.load_skill_env()
    configure_logging()
    db = TranscriptDB(Path(args.db) if args.db else paths.DB_PATH)
    try:
        if args.stats:
            payload = stats_json(db)
        else:
            payload = run_fill(db, dry_run=args.dry_run,
                               only_aweme_id=args.aweme_id)
    finally:
        db.close()
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
