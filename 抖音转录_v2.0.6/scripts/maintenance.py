"""维护命令：补 AI、重渲染、续跑、重试失败与版本回退。

所有命令都只作用于 SQLite 与已渲染文件：不重新采集、不下载媒体、不重跑 ASR。
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
from scripts.artifacts import (build_version_bundle, publish_artifacts,
                              render_evidence, render_main)
from scripts.artifacts import repair_artifacts as _rebuild_artifacts
from scripts import correct_transcript, generate_summary
from scripts.correct_transcript import CorrectionContext
from scripts.evidence_fusion import align_evidence
from scripts.generate_summary import (MODE_NORMAL, MODE_RESTRICTED,
                                      MODE_VISUAL, summary_source_text)
from scripts.job_queue import QueueRepository
from scripts.logger import configure_logging, ensure_utf8_stdio
from scripts.models import (CorrectionStatus, ErrorCode, JobStatus,
                            NormalizedInput, QualityStatus, SummaryStatus,
                            utc_now)
from scripts.transcript_db import PublishError, TranscriptDB

logger = logging.getLogger("douyin_transcript.maintenance")

SCHEMA_VERSION = 2


def _ai_versions(db: TranscriptDB) -> list[dict]:
    rows = db.connect().execute(
        "SELECT * FROM transcript_versions WHERE is_current = 1 "
        "ORDER BY aweme_id").fetchall()
    return [dict(row) for row in rows]


def repair_artifacts(db: TranscriptDB, *, aweme_id: str | None = None,
                     output_root: Path | None = None) -> dict:
    """只读数据库重建缺失或哈希不符的交付文件。"""
    output_root = Path(output_root or paths.OUTPUT_DIR)
    versions = _ai_versions(db)
    if aweme_id:
        versions = [item for item in versions if item["aweme_id"] == str(aweme_id)]
    checked = repaired = ok = failed = 0
    details = []
    for version in versions:
        checked += 1
        current = db.get_current_version(version["aweme_id"])
        if db.artifacts_verified(current):
            ok += 1
            continue
        try:
            published = _rebuild_artifacts(db, version["aweme_id"],
                                           output_root=output_root)
            repaired += 1
            details.append({"aweme_id": version["aweme_id"],
                            "main_file": published.main_file,
                            "evidence_file": published.evidence_file})
        except Exception as exc:
            failed += 1
            logger.warning("重建失败（%s）：%s", version["aweme_id"],
                           type(exc).__name__)
            details.append({"aweme_id": version["aweme_id"],
                            "error": type(exc).__name__})
    return {"checked": checked, "ok": ok, "repaired": repaired,
            "failed": failed, "details": details}


def _needs_ai(version: dict) -> bool:
    if str(version.get("quality_status")) == str(QualityStatus.VISUAL_TEXT_ONLY):
        return (str(version.get("summary_status")) != str(SummaryStatus.SUMMARIZED))
    return (str(version.get("correction_status")) in
            (str(CorrectionStatus.PENDING),
             str(CorrectionStatus.FAILED_PENDING_RETRY))
            or str(version.get("summary_status")) != str(SummaryStatus.SUMMARIZED))


def fill_ai(db: TranscriptDB, *, aweme_id: str | None = None,
            output_root: Path | None = None, correction_call=None,
            summary_call=None) -> dict:
    """补齐待校正与待摘要内容并重渲染主文件；不触碰媒体与 ASR。"""
    output_root = Path(output_root or paths.OUTPUT_DIR)
    correction_call = correction_call or correct_transcript.correct_or_pending
    summary_call = summary_call or generate_summary.generate_or_pending
    all_versions = _ai_versions(db)
    if aweme_id:
        all_versions = [item for item in all_versions
                        if item["aweme_id"] == str(aweme_id)]
    versions = [item for item in all_versions if _needs_ai(item)]
    checked = len(all_versions)
    filled = pending = failed = 0
    details = []
    for version in versions:
        version_id = int(version["version_id"])
        bundle = build_version_bundle(db, version_id)
        quality_status = str(version.get("quality_status"))
        try:
            if quality_status != str(QualityStatus.VISUAL_TEXT_ONLY) and \
                    str(version.get("correction_status")) not in (
                        str(CorrectionStatus.CORRECTED),
                        str(CorrectionStatus.PARTIAL_CORRECTED)):
                context = CorrectionContext(
                    title=bundle.get("title", ""),
                    segments=bundle.get("segments") or [],
                    evidence=align_evidence(bundle.get("segments") or [],
                                            bundle.get("ocr_observations") or []),
                    quality_status=quality_status)
                outcome = correction_call(context)
                status = str(getattr(outcome, "status", CorrectionStatus.PENDING))
                db.save_correction(version_id,
                                   corrected_text=getattr(outcome,
                                                         "corrected_text", ""),
                                   changes=getattr(outcome, "changes", []),
                                   status=status)

            bundle = build_version_bundle(db, version_id)
            mode = _mode_for(quality_status)
            visual = _visual_text(bundle.get("ocr_observations") or [])
            source = summary_source_text(bundle.get("raw_text", ""),
                                         bundle.get("corrected_text", ""), mode,
                                         visual_text=visual)
            status, text = summary_call(bundle.get("title", ""), source, mode)
            db.save_summary(version_id, summary=text, status=status, mode=mode,
                            meta={"mode": mode, "pipeline_version": "v2",
                                  "source": "corrected" if bundle.get(
                                      "corrected_text") else "raw"})
        except Exception as exc:
            # AI 服务不可用：保持 pending，不影响交付，也不计入失败
            logger.warning("补 AI 调用失败（%s）：%s", version["aweme_id"],
                           type(exc).__name__)
            details.append({"aweme_id": version["aweme_id"],
                            "ai_error": type(exc).__name__})
            pending += 1
            continue
        try:
            bundle = build_version_bundle(db, version_id)
            bundle["is_current"] = True
            publish_artifacts(db, version_id,
                              [render_main(bundle, output_root),
                               render_evidence(bundle, output_root)],
                              output_root=output_root)
        except Exception as exc:
            failed += 1
            logger.warning("补 AI 后重渲染失败（%s）：%s", version["aweme_id"],
                           type(exc).__name__)
            details.append({"aweme_id": version["aweme_id"],
                            "error": type(exc).__name__})
            continue
        refreshed = db.get_version(version_id)
        if str(refreshed.get("summary_status")) == str(SummaryStatus.SUMMARIZED):
            filled += 1
            details.append({"aweme_id": version["aweme_id"],
                            "summary_status": "summarized"})
        else:
            pending += 1
            details.append({"aweme_id": version["aweme_id"],
                            "summary_status": str(refreshed.get("summary_status"))})
    return {"checked": checked, "filled": filled, "pending": pending,
            "failed": failed, "details": details}


def _mode_for(quality_status: str) -> str:
    if quality_status == str(QualityStatus.VISUAL_TEXT_ONLY):
        return str(MODE_VISUAL)
    if quality_status == str(QualityStatus.PASSED):
        return str(MODE_NORMAL)
    return str(MODE_RESTRICTED)


def _visual_text(observations) -> str:
    return "\n".join(dict.fromkeys(
        f"画面文字：{item.get('text', '')}" for item in observations
        if str(item.get("kind", "")) != "speech_subtitle"
        and str(item.get("text", "")).strip()))


def recover_interrupted(db: TranscriptDB, *, timeout_seconds: int | None = None
                        ) -> dict:
    """把心跳过期的 job 放回队列，供 `--resume` 继续。"""
    queue = QueueRepository(db)
    kwargs = {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
    recovered = queue.recover_stale_jobs(**kwargs)
    return {"recovered": len(recovered),
            "jobs": [{"job_id": item.job_id, "batch_id": item.batch_id,
                      "position": item.position, "stage": item.stage}
                     for item in recovered]}


def reprocess_review(db: TranscriptDB, *, aweme_id: str | None = None) -> dict:
    """把“需要复核且管线已升级”的作品重新排队（不删除任何历史版本）。"""
    from scripts.models import CURRENT_PROCESSING_SIGNATURE

    versions = _ai_versions(db)
    if aweme_id:
        versions = [item for item in versions if item["aweme_id"] == str(aweme_id)]
    targets = [item for item in versions
               if str(item.get("quality_status")) == str(QualityStatus.NEEDS_REVIEW)
               and str(item.get("processing_signature") or "")
               != CURRENT_PROCESSING_SIGNATURE]
    if not targets:
        return {"queued": 0, "batch_id": "", "aweme_ids": []}
    queue = QueueRepository(db)
    video_rows = {}
    for item in targets:
        video = db.get_video(item["aweme_id"]) or {}
        video_rows[item["aweme_id"]] = video
        queue.db.find_reuse_action(item["aweme_id"])  # 只是读取，避免误判
    items = [NormalizedInput(
        raw_input=f"reprocess {item['aweme_id']}",
        url=str(video_rows[item["aweme_id"]].get("canonical_url")
                or f"https://www.douyin.com/video/{item['aweme_id']}"),
        candidate_aweme_id=str(item["aweme_id"]), kind="video")
        for item in targets]
    preview = queue.create_batch(items, source_type="reprocess",
                                 source_label="reprocess-review", confirmed=True)
    for job in queue.list_jobs(preview.batch_id):
        db.connect().execute(
            "UPDATE jobs SET aweme_id = ?, status = ?, updated_at = ? "
            "WHERE job_id = ?",
            (job.candidate_aweme_id, str(JobStatus.PENDING), utc_now(),
             job.job_id))
    db.connect().commit()
    # 重处理必须跳过复用判定，因此续跑时要带 --force
    return {"queued": len(items), "batch_id": preview.batch_id,
            "aweme_ids": [item["aweme_id"] for item in targets],
            "force_required": True}


def rollback_version(db: TranscriptDB, aweme_id: str, version_id: int, *,
                     output_root: Path | None = None) -> dict:
    """把 current 切回历史版本并重渲染；文字版本一个都不删。"""
    output_root = Path(output_root or paths.OUTPUT_DIR)
    version = db.get_version(int(version_id))
    if version is None or str(version["aweme_id"]) != str(aweme_id):
        raise PublishError("指定版本不属于该作品")
    bundle = build_version_bundle(db, int(version_id))
    bundle["is_current"] = True
    published = publish_artifacts(
        db, int(version_id),
        [render_main(bundle, output_root), render_evidence(bundle, output_root)],
        output_root=output_root)
    return {"aweme_id": str(aweme_id), "version_id": int(version_id),
            "main_file": published.main_file,
            "evidence_file": published.evidence_file,
            "versions_kept": len(db.list_versions(aweme_id))}


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="抖音转录维护命令")
    parser.add_argument("command",
                        choices=["repair-artifacts", "fill-ai",
                                 "recover-interrupted", "reprocess-review",
                                 "rollback-version"])
    parser.add_argument("--aweme-id", dest="aweme_id", default=None)
    parser.add_argument("--version-id", dest="version_id", type=int, default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--output-root", dest="output_root", default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    paths.load_skill_env()
    configure_logging()
    db = TranscriptDB(Path(args.db) if args.db else paths.DB_PATH)
    output_root = Path(args.output_root) if args.output_root else paths.OUTPUT_DIR
    try:
        if args.command == "repair-artifacts":
            payload = {"repair": repair_artifacts(db, aweme_id=args.aweme_id,
                                                  output_root=output_root)}
        elif args.command == "fill-ai":
            payload = {"fill": fill_ai(db, aweme_id=args.aweme_id,
                                       output_root=output_root)}
        elif args.command == "recover-interrupted":
            payload = {"recover": recover_interrupted(db)}
        elif args.command == "reprocess-review":
            payload = {"reprocess": reprocess_review(db, aweme_id=args.aweme_id)}
        else:
            if not args.aweme_id or not args.version_id:
                payload = {"error": {"code": str(ErrorCode.INVALID_INPUT),
                                     "message": "回退需要 --aweme-id 与 --version-id"}}
                print(json.dumps({"schema_version": SCHEMA_VERSION, "ok": False,
                                  **payload}, ensure_ascii=False))
                return 1
            payload = {"rollback": rollback_version(db, args.aweme_id,
                                                    args.version_id,
                                                    output_root=output_root)}
    except (PublishError, LookupError, ValueError) as exc:
        payload = {"error": {"code": str(ErrorCode.DATABASE_FAILED),
                             "message": str(exc)[:200]}}
    finally:
        db.close()

    ok = "error" not in payload
    out = {"schema_version": SCHEMA_VERSION, "ok": ok,
           "status": "completed" if ok else "failed", **payload}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
