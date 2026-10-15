"""多链接串行批处理入口（v2）。

输入可以是分享文案、`--text` 或 TXT/Markdown/CSV 列表文件；链接识别与规范化
统一交给 `input_normalizer`，进度持久化在 SQLite 队列中，因此支持
`--resume`、`--retry-failed` 与认证暂停后续跑。标准输出永远恰好一行 JSON，
人类日志只写标准错误。
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
from scripts.input_normalizer import InputError, load_input_file, normalize_inputs
from scripts.job_queue import QueueRepository
from scripts.logger import configure_logging, ensure_utf8_stdio
from scripts.models import BatchStatus, ErrorCode, JobStatus, NormalizedInput
from scripts.transcript_db import TranscriptDB
from scripts.douyin_transcript import (_default_ingest, default_services,
                                      process_one)

logger = logging.getLogger("douyin_transcript.batch")

SCHEMA_VERSION = 2

_NEW_KINDS = {"completed": "new_transcribed",
              "needs_review": "needs_review",
              "awaiting_ai": "pending_summary",
              "visual_text_only": "visual_text_only",
              "reused": "reused",
              "duplicate": "duplicate",
              "login_required": "login_required",
              "failed": "failed"}

_TOTALS_TEMPLATE = {"new_transcribed": 0, "needs_review": 0,
                    "pending_summary": 0, "visual_text_only": 0, "reused": 0,
                    "duplicate": 0, "login_required": 0, "failed": 0}

_SETTLE_STATUS = {"completed": JobStatus.COMPLETED,
                  "needs_review": JobStatus.NEEDS_REVIEW,
                  "awaiting_ai": JobStatus.AWAITING_AI,
                  "visual_text_only": JobStatus.VISUAL_TEXT_ONLY,
                  "reused": JobStatus.REUSED,
                  "login_required": JobStatus.AUTH_REQUIRED}


def _failure(code: str, message: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "ok": False, "status": "failed",
            "error": {"code": str(code), "message": message}}


def summarize(results: list[dict]) -> dict:
    totals = dict(_TOTALS_TEMPLATE)
    for item in results:
        key = _NEW_KINDS.get(str(item.get("status", "")))
        if key:
            totals[key] += 1
    ok = totals["failed"] == 0 and totals["login_required"] == 0
    return {"ok": ok, "totals": totals, "results": results}


def collect_items(text: str | None = None, file_path: str | Path | None = None
                  ) -> list[NormalizedInput]:
    """把聊天文本或列表文件统一转成规范化输入（保序、不去重）。"""
    items: list[NormalizedInput] = []
    items.extend(normalize_inputs(text or ""))
    if file_path is not None:
        items.extend(load_input_file(Path(file_path)))
    return items


def _preview_of(preview) -> dict:
    return {"submitted": preview.submitted, "new": preview.new,
            "reused": preview.reused, "duplicates": preview.duplicates,
            "reprocess": preview.reprocess,
            "estimated_seconds": preview.estimated_seconds,
            "requires_confirmation": preview.requires_confirmation}


def _settle(queue: QueueRepository, job, result: dict) -> dict:
    """写回单条状态并返回逐条报告项。"""
    status = str(result.get("status", "failed"))
    target = _SETTLE_STATUS.get(status)
    try:
        if status == "login_required":
            queue.pause_for_auth(job.job_id)
        else:
            queue.finish_job(job.job_id, target or JobStatus.FAILED)
    except ValueError:
        logger.warning("job 状态写回失败：%s → %s", job.job_id, status)
    report = {"job_id": job.job_id, "position": job.position,
              "aweme_id": result.get("aweme_id") or job.candidate_aweme_id,
              "title": result.get("title", ""), "status": status,
              "ok": bool(result.get("ok")),
              "main_file": result.get("main_file", ""),
              "evidence_file": result.get("evidence_file", ""),
              "quality_status": result.get("quality_status", ""),
              "reuse_action": result.get("reuse_action", "")}
    if not result.get("ok"):
        report["error"] = result.get("error") or {"code": "failed",
                                                 "message": "处理失败"}
    return report


def _services_for(db, queue: QueueRepository, *, force: bool, headless: bool):
    services = default_services(db)
    services.force = bool(force)
    services.queue = queue
    services.headless = bool(headless)
    if not headless:
        services.ingest = lambda url, job_dir, _headless: _default_ingest(
            url, job_dir, False)
    return services


def _run_jobs(queue: QueueRepository, batch_id: str, *, force: bool,
              headless: bool, db) -> dict:
    results: list[dict] = []
    paused = False
    while True:
        job = queue.claim_next(batch_id)
        if job is None:
            break
        logger.info("批处理 %s（第 %d 位）：%s", job.job_id[:8], job.position + 1,
                    job.normalized_input)
        try:
            services = _services_for(db, queue, force=force, headless=headless)
            result = process_one(job, services)
        except Exception as exc:  # 单条异常不得终止批处理
            logger.exception("批处理单条异常")
            result = {"ok": False, "status": "failed",
                      "error": {"code": str(ErrorCode.DATABASE_FAILED),
                                "message": f"内部错误：{type(exc).__name__}"}}
        report = _settle(queue, job, result)
        results.append(report)
        if report["status"] == "login_required":
            paused = True
            break
    return {"results": results, "paused": paused}


def _entry_for_unprocessed(queue: QueueRepository, job) -> dict:
    """未经过编排器的 job（复用/重复）也要逐条出现在报告里。"""
    status = {"reused": "reused", "duplicate": "duplicate",
              "awaiting_ai": "awaiting_ai"}.get(str(job.status), str(job.status))
    resolved = job.aweme_id or job.candidate_aweme_id
    entry = {"job_id": job.job_id, "position": job.position,
             "aweme_id": resolved, "title": "", "status": status,
             "ok": status in ("reused", "duplicate"),
             "main_file": "", "evidence_file": "", "quality_status": "",
             "reuse_action": "", "processed": False}
    if job.duplicate_of_job_id:
        entry["duplicate_of_job_id"] = job.duplicate_of_job_id
    if status == "reused" and resolved:
        video = queue.db.get_video(resolved) or {}
        entry["title"] = video.get("title", "")
        current = queue.db.get_current_version(resolved)
        if current is not None:
            entry["main_file"] = current.get("main_file", "")
            entry["evidence_file"] = current.get("evidence_file", "")
            entry["quality_status"] = current.get("quality_status", "")
    return entry


def _report(queue: QueueRepository, batch_id: str, run: dict, *,
            preview=None, recovered: int = 0) -> dict:
    jobs = queue.list_jobs(batch_id)
    processed = {item["job_id"]: dict(item, processed=True)
                 for item in run["results"]}
    entries = [processed.get(job.job_id) or _entry_for_unprocessed(queue, job)
               for job in jobs]
    entries.sort(key=lambda item: item["position"])
    summary = summarize(entries)
    batch = queue.get_batch(batch_id) or {}
    payload = {"schema_version": SCHEMA_VERSION, "batch_id": batch_id,
               "ok": summary["ok"] and not run["paused"],
               "status": ("paused_auth" if run["paused"]
                          else "completed_with_errors" if not summary["ok"]
                          else "completed"),
               "totals": summary["totals"], "results": entries,
               "batch_status": batch.get("status", ""),
               "jobs_total": len(jobs),
               "jobs_remaining": sum(1 for job in jobs
                                     if job.status in (JobStatus.PENDING,
                                                       JobStatus.RETRY_WAIT)),
               "recovered_jobs": recovered}
    payload["preview"] = (_preview_of(preview) if preview is not None
                         else _preview_from_jobs(jobs))
    return payload


def _preview_from_jobs(jobs) -> dict:
    """续跑/重试的批次没有预览对象：按 job 现状还原四个计数。"""
    duplicates = sum(1 for job in jobs if job.status == JobStatus.DUPLICATE)
    reused = sum(1 for job in jobs if job.status == JobStatus.REUSED)
    submitted = len(jobs)
    return {"submitted": submitted, "new": submitted - duplicates - reused,
            "reused": reused, "duplicates": duplicates, "reprocess": 0,
            "estimated_seconds": 0, "requires_confirmation": False}


def run_batch(text: str = "", *, force: bool = False, headless: bool = True,
              file_path: str | Path | None = None, db=None,
              confirm: bool = False, resume_batch_id: str | None = None,
              retry_failed: str | None = None) -> dict:
    """串行处理一批链接；预览、暂停、续跑与重试都走持久队列。"""
    owns_db = db is None
    db = db or TranscriptDB(paths.DB_PATH)
    try:
        queue = QueueRepository(db)
        recovered = queue.recover_stale_jobs()
        if recovered:
            logger.info("恢复 %d 条中断的 job", len(recovered))

        if resume_batch_id or retry_failed:
            batch_id = str(resume_batch_id or retry_failed)
            batch = queue.get_batch(batch_id)
            if batch is None:
                return _failure(ErrorCode.INVALID_INPUT, "批次不存在")
            if retry_failed:
                created = queue.requeue_failed(batch_id)
                logger.info("为 %d 条失败项建立重试 job", len(created))
            elif batch["status"] == str(BatchStatus.PAUSED_AUTH):
                queue.resume_batch(batch_id)
            run = _run_jobs(queue, batch_id, force=force, headless=headless, db=db)
            return _report(queue, batch_id, run, recovered=len(recovered))

        try:
            items = collect_items(text, file_path)
        except InputError as error:
            return _failure(error.code, error.message)
        if not items:
            return _failure(ErrorCode.INVALID_INPUT,
                            "未在输入中找到抖音链接，请发送完整分享文案、作品链接或列表文件")

        preview = queue.create_batch(
            items, source_type="chat",
            source_label=(text or str(file_path or ""))[:120], confirmed=confirm)
        if preview.requires_confirmation:
            payload = _failure(ErrorCode.BATCH_CONFIRMATION_REQUIRED,
                               "本批新增工作超过阈值，请确认后执行")
            payload["batch_id"] = preview.batch_id
            payload["preview"] = _preview_of(preview)
            payload["status"] = str(ErrorCode.BATCH_CONFIRMATION_REQUIRED)
            return payload
        run = _run_jobs(queue, preview.batch_id, force=force, headless=headless,
                        db=db)
        return _report(queue, preview.batch_id, run, preview=preview,
                       recovered=len(recovered))
    finally:
        if owns_db:
            db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="串行转录多条抖音链接（v2）")
    parser.add_argument("input", nargs="?", default=None,
                        help="可包含多条抖音链接的分享文案")
    parser.add_argument("--text", dest="as_text", default=None,
                        help="显式以聊天文本方式导入")
    parser.add_argument("--file", dest="as_file", default=None,
                        help="TXT / Markdown / CSV 列表文件路径")
    parser.add_argument("--resume", dest="resume", default=None,
                        help="从指定批次继续（含认证暂停后的续跑）")
    parser.add_argument("--retry-failed", dest="retry_failed", default=None,
                        help="为指定批次的失败项建立重试 job 并继续")
    parser.add_argument("--confirm", action="store_true",
                        help="用户已确认直接执行（跳过批次预览）")
    parser.add_argument("--force", action="store_true", help="强制重转")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--visible", action="store_true",
                        help="以可见 Chromium 运行上游（用户已确认扫码登录）")
    parser.add_argument("--manifest", nargs="?", const="",
                        help="交付时同时输出 source-manifest.json 契约文件（可选指定输出路径）")
    args = parser.parse_args(argv)

    text = args.as_text if args.as_text is not None else args.input
    paths.ensure_runtime_dirs()
    ensure_utf8_stdio()
    paths.load_skill_env()
    configure_logging()
    out = run_batch(text or "", force=args.force, headless=not args.visible,
                    file_path=args.as_file, confirm=args.confirm,
                    resume_batch_id=args.resume, retry_failed=args.retry_failed)
    if args.manifest is not None and out.get("results"):
        from scripts.artifacts import render_manifest
        mf_path = Path(args.manifest) if args.manifest else paths.OUTPUT_DIR / "source-manifest.json"
        written = render_manifest(out["results"], mf_path)
        if written:
            out["manifest"] = str(written)
    print(json.dumps(out, ensure_ascii=False))
    if out.get("status") == str(ErrorCode.BATCH_CONFIRMATION_REQUIRED):
        return 2
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
