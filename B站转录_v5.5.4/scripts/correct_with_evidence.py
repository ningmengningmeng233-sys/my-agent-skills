#!/usr/bin/env python3
"""用画面文字证据做受约束校正，产出新的派生版本。

流程：
    库中原稿 → 抽帧 OCR → 文本级证据挂接 → 受约束校正（无证据不允许改）→ 存为新版本 + 逐条改动记录

    .venv/Scripts/python.exe scripts/correct_with_evidence.py --bvid BV1xxx --video 视频.mp4 --dry-run --json
    .venv/Scripts/python.exe scripts/correct_with_evidence.py --bvid BV1xxx --video 视频.mp4 --apply --json

契约：
- 默认 dry-run：跑 OCR 与证据挂接，但不调用校正 API、不写库；
- 原始稿永不被覆盖，校正结果只作为新的 corrected 版本追加；
- 每次改动写入 `correction_changes`，含原文、改后、证据与置信度；
- 校正失败或被拒绝时不写版本，原稿与既有版本保持不动。

B站适配说明：库中不保存分段时间轴，故证据按**文本相似度**挂接（见
`evidence_fusion.align_evidence_by_text`），证据强度判定沿用与抖音版一致的阈值。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from transcript_db import DB_PATH, TranscriptDB  # noqa: E402
from logger import info, warn  # noqa: E402
from correction import CorrectionContext, STATUS_CORRECTED, correct_or_pending, load_summary_config  # noqa: E402
from evidence_fusion import align_evidence_by_text, visual_context_from_text  # noqa: E402
from ocr import FramePolicy, default_engine, run_ocr  # noqa: E402

SOURCE = "correct_with_evidence.py"


def raw_version_text(db: TranscriptDB, bvid: str, page: int) -> tuple[str, int]:
    """取最新原始稿版本的正文与版本号。"""
    row = db.conn.execute(
        "SELECT version_no, transcript_text FROM transcript_versions"
        " WHERE bvid=? AND page=? AND kind='raw' ORDER BY version_no DESC LIMIT 1",
        (bvid, page),
    ).fetchone()
    if row:
        return row[1] or "", int(row[0])
    record = db.get_by_bvid(bvid, page)
    return (record or {}).get("transcript_text", "") or "", 1


def ensure_raw_version(db: TranscriptDB, bvid: str, page: int, text: str,
                       record: dict) -> int:
    """确保原始稿 v1 已物化，避免派生版本与 v1 撞号。返回原始稿版本号。"""
    row = db.conn.execute(
        "SELECT version_no FROM transcript_versions"
        " WHERE bvid=? AND page=? AND kind='raw' ORDER BY version_no LIMIT 1",
        (bvid, page),
    ).fetchone()
    if row:
        return int(row[0])
    created = str(record.get("created_at") or "")
    with db.conn:
        db.conn.execute(
            """
            INSERT INTO transcript_versions
                (bvid, page, version_no, kind, source_file, source_hash,
                 transcript_text, summary, is_current, created_at)
            VALUES (?, ?, 1, 'raw', ?, '', ?, ?, 1, ?)
            """,
            (bvid, page, record.get("transcript_file") or "",
             text, record.get("summary") or "",
             created or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
    return 1


def next_version_no(db: TranscriptDB, bvid: str, page: int) -> int:
    row = db.conn.execute(
        "SELECT MAX(version_no) FROM transcript_versions WHERE bvid=? AND page=?",
        (bvid, page),
    ).fetchone()
    return (int(row[0]) if row and row[0] else 0) + 1


def frame_policy_from_env() -> FramePolicy:
    """抽帧策略可用 .env 覆盖，便于按视频长度换取速度。

    两阶段抽帧的规模由 base_fps（底帧）与 subtitle_fps（字幕带加密帧）决定：
    一段 9 分钟、全程硬字幕的视频实测约 2600 帧，CPU 推理需十几分钟。
    长视频或赶时间时可用 BILI_OCR_BASE_FPS／BILI_OCR_SUBTITLE_FPS／BILI_OCR_MAX_FRAMES 收敛。
    """

    def number(name: str, default, cast=float):
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return cast(float(raw))
        except ValueError:
            return default

    return FramePolicy(
        base_fps=number("BILI_OCR_BASE_FPS", FramePolicy.base_fps),
        subtitle_fps=number("BILI_OCR_SUBTITLE_FPS", FramePolicy.subtitle_fps),
        max_frames=number("BILI_OCR_MAX_FRAMES", FramePolicy.max_frames, int),
    )


def download_video(bvid: str, page: int, target_dir: Path) -> Path:
    """用 yt-dlp 取一段仅视频流（默认 ≤720p）用于抽帧；返回视频路径。

    只用于 OCR 取帧，下载后即删除，不进任何归档。
    """
    url = f"https://www.bilibili.com/video/{bvid}/"
    if page and int(page) > 1:
        url += f"?p={int(page)}"
    pattern = str(target_dir / f"{bvid}_P{int(page):02d}.%(ext)s")
    binary = shutil.which("yt-dlp") or "yt-dlp"
    result = subprocess.run(
        [binary, "--no-warnings", "--no-playlist",
         "-f", "bv*[height<=720]/bv*/b", "-o", pattern, url],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900,
    )
    candidates = [p for p in sorted(target_dir.glob(f"{bvid}_P{int(page):02d}.*"))
                  if p.suffix not in (".json", ".part")]
    if not candidates:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(f"下载视频失败：{detail[-1] if detail else '未知原因'}")
    return candidates[0]


def collect_observations(video: Path, frame_dir: Path, policy: FramePolicy) -> tuple[list, dict]:
    outcome = run_ocr(video, frame_dir, default_engine, policy=policy)
    records = [observation.as_record() for observation in outcome.observations]
    return records, {
        "status": outcome.status,
        "frames_scanned": outcome.frames_scanned,
        "degradations": list(outcome.degradations),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="基于画面文字证据的受约束校正")
    parser.add_argument("--bvid", required=True)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--video", help="本地视频文件（用于抽帧 OCR）")
    parser.add_argument("--download", action="store_true",
                        help="用 yt-dlp 自行下载视频（仅视频流，用完即删）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", help="数据库路径（默认技能库）")
    parser.add_argument("--apply", action="store_true", help="调用校正 API 并写库")
    parser.add_argument("--dry-run", action="store_true", help="只跑 OCR 与证据挂接（默认）")
    args = parser.parse_args(argv)

    if not args.video and not args.download:
        parser.error("需要 --video 或 --download")
    video = Path(args.video) if args.video else None
    if video is not None and not video.is_file():
        print(f"❌ 视频不存在：{video}", file=sys.stderr)
        return 2

    db_path = Path(args.db) if args.db else Path(DB_PATH)
    if not db_path.is_file():
        print(f"❌ 数据库不存在：{db_path}", file=sys.stderr)
        return 2

    with TranscriptDB(db_path=db_path) as db:
        record = db.get_by_bvid(args.bvid, args.page)
        if not record:
            print(f"❌ 库中无此记录：{args.bvid} P{args.page:02d}", file=sys.stderr)
            return 2
        text, raw_no = raw_version_text(db, args.bvid, args.page)
        if not text.strip():
            print("❌ 原始稿为空，无法校正", file=sys.stderr)
            return 2

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            if video is None:
                print(f"⬇️  下载视频用于抽帧：{args.bvid} P{args.page:02d}",
                      file=sys.stderr)
                try:
                    video = download_video(args.bvid, args.page, work)
                except Exception as exc:  # noqa: BLE001
                    print(f"❌ {exc}", file=sys.stderr)
                    return 2
            policy = frame_policy_from_env()
            print(f"  抽帧策略：base_fps={policy.base_fps:g} "
                  f"subtitle_fps={policy.subtitle_fps:g} "
                  f"max_frames={policy.max_frames}", file=sys.stderr)
            observations, ocr_meta = collect_observations(
                video, work / "frames", policy)

        evidence = align_evidence_by_text(text, observations)
        segments = [{"start": item.start, "end": item.end, "text": item.text}
                    for item in evidence]
        visual_texts = visual_context_from_text(observations)
        strong = sum(1 for item in evidence if item.strong)

        report = {
            "dry_run": not args.apply,
            "bvid": args.bvid,
            "page": args.page,
            "video": str(video) if video else "",
            "raw_version_no": raw_no,
            "raw_chars": len(text),
            "ocr": ocr_meta,
            "observations": len(observations),
            "segments": len(segments),
            "strong_evidence": strong,
            "visual_texts": len(visual_texts),
            "status": "",
            "version_no": 0,
            "changes": 0,
        }

        if not args.apply:
            if args.json:
                print(json.dumps(report, ensure_ascii=False))
            else:
                print(f"🔍 证据挂接（预览）：{args.bvid} P{args.page:02d}")
                print(f"   OCR：{ocr_meta['status']}，抽帧 {ocr_meta['frames_scanned']}，"
                      f"观察 {len(observations)} 条")
                print(f"   文本块 {len(segments)} 个，强证据 {strong} 个，"
                      f"画面文字 {len(visual_texts)} 条")
            return 0

        context = CorrectionContext(
            title=str(record.get("title", "")),
            segments=segments,
            evidence=evidence,
            quality_status="passed",
            visual_texts=visual_texts,
        )
        outcome = correct_or_pending(context, load_summary_config())
        report["status"] = outcome.status
        if outcome.status != STATUS_CORRECTED or not outcome.corrected_text.strip():
            warn(SOURCE, f"校正未产出：{outcome.status}")
            report["meta"] = outcome.meta
            if args.json:
                print(json.dumps(report, ensure_ascii=False))
            else:
                print(f"⚠️  校正未产出（{outcome.status}），库未改动")
            return 1

        ensure_raw_version(db, args.bvid, args.page, text, record)
        version_no = next_version_no(db, args.bvid, args.page)
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with db.conn:  # 单事务：版本与改动记录一起写入
            db.conn.execute(
                """
                INSERT INTO transcript_versions
                    (bvid, page, version_no, kind, source_file, source_hash,
                     transcript_text, summary, is_current, created_at)
                VALUES (?, ?, ?, 'corrected', ?, '', ?, '', 0, ?)
                """,
                (args.bvid, args.page, version_no,
                 f"ocr-evidence:{video.name}", outcome.corrected_text, created),
            )
            for change in outcome.changes or []:
                db.conn.execute(
                    """
                    INSERT INTO correction_changes
                        (bvid, page, version_no, segment_ref, original, corrected,
                         evidence, confidence, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (args.bvid, args.page, version_no,
                     str(change.get("source_segment_ref", "")),
                     str(change.get("original_text", "")),
                     str(change.get("corrected_text", "")),
                     json.dumps(change.get("evidence", {}), ensure_ascii=False),
                     float(change.get("confidence", 0) or 0), created),
                )
        report["version_no"] = version_no
        report["changes"] = len(outcome.changes or [])
        info(SOURCE, f"{args.bvid} P{args.page:02d} 校正完成：v{version_no}，"
                     f"改动 {report['changes']} 处")

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f"✅ 校正完成：{args.bvid} P{args.page:02d} 新版本 v{report['version_no']}，"
              f"改动 {report['changes']} 处（强证据 {strong} 个）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
