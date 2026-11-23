#!/usr/bin/env python3
"""转录结果的唯一写库入口：更新库行 + 追加原始稿版本 + 导入分段与质量分。

流水线（`bilibili_transcript.sh` 第一步）调用本脚本，取代过去的内联写入。
一次转录 = 一个新版本，因此：

- `transcripts` 行按唯一键 `(bvid, page)` 更新为最新结果（保持既有行为）；
- `transcript_versions` 追加一条 `kind='raw'`、`is_current=1` 的新版本，
  并把该键的其它版本置为 `is_current=0`；**旧版本一律保留**，不做删除或覆盖；
- 若提供了分段侧车 JSON，则把它写入 `transcript_segments`，并据分段算出的指标
  调用质量评分，结果写入该版本的 `quality_score` / `metrics_json`。

    .venv/Scripts/python.exe scripts/record_transcript.py \
        --bvid BV1xxx --page 1 --meta meta.json --text-file text.txt \
        [--segments bilibili_audio.segments.json] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from transcript_db import DB_PATH, TranscriptDB  # noqa: E402
from logger import info  # noqa: E402

SOURCE = "record_transcript.py"


def _next_raw_version(conn, bvid: str, page: int) -> int:
    row = conn.execute(
        "SELECT MAX(version_no) FROM transcript_versions WHERE bvid=? AND page=?",
        (bvid, page),
    ).fetchone()
    return (int(row[0]) if row and row[0] else 0) + 1


def _segment_metrics(payload: dict, segments: list) -> dict:
    """由分段推导可用指标；音频类指标（响度/削波）由音频分析链路另行提供。"""
    duration = float(payload.get("duration") or 0.0)
    speech = sum(max(0.0, float(s.get("end", 0) or 0) - float(s.get("start", 0) or 0))
                 for s in segments)
    metrics = {
        "duration": duration,
        "segment_count": len(segments),
        "speech_seconds": round(speech, 3),
        "language": payload.get("language", "") or "",
        "language_probability": float(payload.get("language_probability", 0.0) or 0.0),
        "model": payload.get("model", "") or "",
        "device": payload.get("device", "") or "",
        "compute_type": payload.get("compute_type", "") or "",
    }
    if duration > 0:
        metrics["speech_ratio"] = round(min(speech / duration, 1.0), 4)
        metrics["silence_ratio"] = round(max(0.0, 1.0 - speech / duration), 4)
    return metrics


def _score(segments: list, metrics: dict) -> tuple[float | None, str]:
    """调用质量评分；评分层不可用时返回 (None, "unavailable")，不阻断写库。"""
    try:
        from asr_types import AsrAttemptResult, AsrStrategy
        from quality import evaluate_quality

        attempt = AsrAttemptResult(
            attempt_no=1, audio_variant="original",
            strategy=AsrStrategy(model=metrics.get("model", "unknown"),
                                 device=metrics.get("device", "cpu"),
                                 compute_type=metrics.get("compute_type", "int8")),
            transcript_text="".join(str(s.get("text", "")) for s in segments),
            segments=segments, metrics=metrics)
        report = evaluate_quality(attempt)
        return float(report.score), str(report.status)
    except Exception:  # noqa: BLE001
        return None, "unavailable"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="转录结果写库（库行 + 原始稿版本 + 分段）")
    parser.add_argument("--bvid", required=True)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--meta", required=True, help="元数据 JSON 文件")
    parser.add_argument("--text-file", required=True, help="转录正文文件")
    parser.add_argument("--segments", help="分段侧车 JSON（fw_whisper_main.py 产出）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", help="数据库路径（默认技能库）")
    args = parser.parse_args(argv)

    meta_path, text_path = Path(args.meta), Path(args.text_file)
    if not meta_path.is_file():
        print(f"❌ 元数据不存在：{meta_path}", file=sys.stderr)
        return 2
    if not text_path.is_file():
        print(f"❌ 正文文件不存在：{text_path}", file=sys.stderr)
        return 2

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    text = text_path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        print("❌ 正文为空，拒绝写入", file=sys.stderr)
        return 2

    segments_payload, segments = {}, []
    if args.segments:
        segments_path = Path(args.segments)
        if segments_path.is_file():
            segments_payload = json.loads(segments_path.read_text(encoding="utf-8"))
            segments = list(segments_payload.get("segments") or [])
        else:
            print(f"⚠️  分段侧车不存在，跳过：{segments_path}", file=sys.stderr)

    db_path = Path(args.db) if args.db else Path(DB_PATH)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    metrics = _segment_metrics(segments_payload, segments) if segments else {}
    score, status = _score(segments, metrics) if segments else (None, "no_segments")

    with TranscriptDB(db_path=db_path) as db:
        with db.conn:  # 单事务：库行 + 版本 + 分段一起成功或一起回滚
            db.conn.execute(
                """
                INSERT INTO transcripts
                    (bvid, page, cid, part_title, url, title, author, duration,
                     upload_date, transcript_source, transcript_file, transcript_text,
                     summary, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, '', 'transcribed', ?, ?)
                ON CONFLICT(bvid, page) DO UPDATE SET
                    cid=excluded.cid, part_title=excluded.part_title, url=excluded.url,
                    title=excluded.title, author=excluded.author,
                    duration=excluded.duration, upload_date=excluded.upload_date,
                    transcript_source=excluded.transcript_source,
                    transcript_text=excluded.transcript_text,
                    status='transcribed', updated_at=excluded.updated_at
                """,
                (args.bvid, args.page,
                 meta.get("cid") if str(meta.get("cid") or "").isdigit() else None,
                 meta.get("part_title", ""), meta.get("url", ""), meta.get("title", ""),
                 meta.get("author", ""), meta.get("duration", ""),
                 meta.get("upload_date", ""), meta.get("transcript_source", ""),
                 text, now, now),
            )
            version_no = _next_raw_version(db.conn, args.bvid, args.page)
            db.conn.execute(
                "UPDATE transcript_versions SET is_current=0 WHERE bvid=? AND page=?",
                (args.bvid, args.page),
            )
            db.conn.execute(
                """
                INSERT INTO transcript_versions
                    (bvid, page, version_no, kind, source_file, source_hash,
                     transcript_text, summary, is_current, created_at,
                     quality_score, metrics_json)
                VALUES (?, ?, ?, 'raw', ?, '', ?, '', 1, ?, ?, ?)
                """,
                (args.bvid, args.page, version_no,
                 meta.get("transcript_source", ""), text, now,
                 score if score is not None else None,
                 json.dumps(metrics, ensure_ascii=False) if metrics else ""),
            )
            if segments:
                db.conn.execute(
                    "DELETE FROM transcript_segments WHERE bvid=? AND page=? AND version_no=?",
                    (args.bvid, args.page, version_no),
                )
                for seq, item in enumerate(segments):
                    db.conn.execute(
                        """
                        INSERT INTO transcript_segments
                            (bvid, page, version_no, seq, start_seconds, end_seconds,
                             text, avg_logprob, no_speech_prob, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (args.bvid, args.page, version_no, seq,
                         float(item.get("start", 0) or 0),
                         float(item.get("end", 0) or 0),
                         str(item.get("text", "")),
                         item.get("avg_logprob"), item.get("no_speech_prob"), now),
                    )

    report = {
        "bvid": args.bvid, "page": args.page, "version_no": version_no,
        "chars": len(text), "segments": len(segments),
        "quality_score": score, "quality_status": status,
        "metrics": metrics,
    }
    info(SOURCE, f"{args.bvid} P{args.page:02d} 写库完成：v{version_no}，"
                 f"分段 {len(segments)}，质量 {score if score is not None else 'n/a'}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f"📝 已写入数据库: {args.bvid} P{args.page:02d} → v{version_no}"
              f"（{len(text)} 字，分段 {len(segments)}，"
              f"质量 {'%.1f' % score if score is not None else '未评分'}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
