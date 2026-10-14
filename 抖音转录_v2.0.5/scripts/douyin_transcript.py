"""v2 单视频编排器：把采集、音频、ASR、OCR、说话人、校正、摘要与发布串起来。

设计要点（设计文档 14、15）：
- 处理中间结果全部先写 staging 版本，只有两个交付文件校验通过才原子切换 current；
- 每个增强能力都可以局部降级，原始稿与旧版本始终可用；
- 阶段顺序只前进，每个阶段完成后更新 job.stage/checkpoint 与心跳。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import media as media_module
from scripts import paths
from scripts.artifacts import (build_version_bundle, publish_artifacts,
                              render_evidence, render_main, repair_artifacts)
from scripts.asr import AsrAttemptResult, AsrStrategy, contains_cjk
from scripts.correct_transcript import CorrectionContext, CorrectionOutcome
from scripts.evidence_fusion import align_evidence, visual_context
from scripts.input_normalizer import normalize_inputs
from scripts.logger import configure_logging, ensure_utf8_stdio
from scripts.models import (CURRENT_PROCESSING_SIGNATURE, CorrectionStatus,
                            ErrorCode, JobRecord, JobStatus, QualityStatus,
                            ReuseAction, Stage, SummaryMode, SummaryStatus,
                            can_transition, utc_now)
from scripts.quality import (AsrAssets, HardwareBudget, QualityReport,
                             next_strategy as default_next_strategy)

logger = logging.getLogger("douyin_transcript")

SCHEMA_VERSION = 2
MAX_ROUNDS = 3

LOGIN_NEEDED_MESSAGE = "抖音匿名访问被拒绝，需要打开专用 Chromium 完成一次扫码登录"
LOGIN_WINDOW_OPEN_MESSAGE = "抖音匿名访问被拒绝，已打开专用 Chromium，请扫码登录后重试"


class LoginRequiredError(Exception):
    """匿名访问被拒绝，需要用户扫码。"""


class IngestFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MediaFailure(Exception):
    pass


@dataclass
class ServiceBundle:
    """所有外部依赖都可注入：编排器自己不 new 任何重资源。"""

    ingest: Callable
    video: Callable
    analyze: Callable
    audio: Callable
    asr: Callable
    quality: Callable
    next_strategy: Callable
    ocr: Callable
    diarization: Callable
    correction: Callable
    summary: Callable
    model_assets: Callable
    db: object
    cleanup: Callable
    output_root: Path = field(default_factory=lambda: paths.OUTPUT_DIR)
    runtime_root: Path = field(default_factory=lambda: paths.STATE_DIR)
    hardware: HardwareBudget = field(default_factory=HardwareBudget)
    assets: AsrAssets = field(default_factory=AsrAssets)
    queue: object | None = None
    force: bool = False
    headless: bool = True
    max_rounds: int = MAX_ROUNDS
    verify_audio: Callable = media_module.verify_audio


# ----------------------------------------------------------------------
# 生产默认依赖
# ----------------------------------------------------------------------

def _default_ingest(user_input: str, job_dir: Path, headless: bool):
    from scripts.ingest_adapter import parse_ingest_payload
    from scripts.ingest_runner import ingest_with_fallback

    run = ingest_with_fallback(user_input, job_dir, headless=headless)
    if run.kind == "login_required":
        raise LoginRequiredError()
    if run.kind != "ok":
        raise IngestFailure(run.kind, run.message)
    return parse_ingest_payload(run.payload)


def _default_video(result, job_dir: Path):
    from scripts.video_media import materialize_video

    return materialize_video(result, job_dir)


def _default_audio(audio_path: Path, job_dir: Path, analysis, *,
                   want_vocals: bool = False):
    from scripts.audio_pipeline import build_candidates
    from scripts.model_assets import ModelAssetManager

    manager = ModelAssetManager()
    vocals_model = None
    if want_vocals and manager.capability_usable("vocals"):
        vocals_model = manager.path_of("uvr_vocals")
    return build_candidates(audio_path, job_dir, analysis=analysis,
                            vocals_model=vocals_model, want_vocals=want_vocals)


def _default_asr(audio_path: Path, strategy: AsrStrategy, attempt_no: int):
    from scripts.asr import run_attempt

    return run_attempt(audio_path, strategy, attempt_no=attempt_no)


def _default_ocr(video_path: Path, frame_dir: Path, asr_segments):
    from scripts.ocr import FramePolicy, default_engine, run_ocr

    return run_ocr(video_path, frame_dir, default_engine,
                   policy=FramePolicy(), asr_segments=asr_segments)


def _default_diarization(audio_path: Path, analysis=None):
    from scripts.diarization import diarize

    return diarize(audio_path, audio_analysis=analysis)


def _default_correction(context: CorrectionContext):
    from scripts.correct_transcript import correct_or_pending

    return correct_or_pending(context)


def _default_summary(title: str, text: str, mode: str = SummaryMode.NORMAL):
    from scripts.generate_summary import generate_or_pending

    return generate_or_pending(title, text, mode=str(mode))


def default_services(db, *, output_root: Path | None = None,
                     runtime_root: Path | None = None) -> ServiceBundle:
    """生产依赖装配：模型档位、显存与增强候选都按本机实际情况决定。"""
    from scripts.asr import GPU_MEDIUM_MIN_VRAM_GB, detect_cuda, gpu_vram_gb
    from scripts.model_assets import ModelAssetManager
    from scripts.quality import evaluate_quality

    manager = ModelAssetManager()
    cuda = detect_cuda()
    vram = gpu_vram_gb() if cuda else None
    if cuda:
        ceiling = "medium" if (vram or 0) >= GPU_MEDIUM_MIN_VRAM_GB else "small"
        hardware = HardwareBudget(device="cuda", compute_type="float16",
                                  max_model=ceiling, vram_gb=vram)
    else:
        hardware = HardwareBudget(device="cpu", compute_type="int8",
                                  max_model="base")
    ladder = tuple(name for name in ("tiny", "base", "small", "medium",
                                     "large-v3"))
    return ServiceBundle(
        ingest=_default_ingest, video=_default_video, analyze=_analyze_default,
        audio=_default_audio, asr=_default_asr,
        quality=lambda attempt, analysis=None, ocr=None: evaluate_quality(
            attempt, analysis, ocr),
        next_strategy=default_next_strategy, ocr=_default_ocr,
        diarization=_default_diarization, correction=_default_correction,
        summary=_default_summary,
        model_assets=lambda: {
            "ocr": manager.capability_usable("ocr"),
            "vocals": manager.capability_usable("vocals"),
            "diarization": manager.capability_usable("diarization")},
        db=db, cleanup=media_module.cleanup_job_directory,
        output_root=Path(output_root or paths.OUTPUT_DIR),
        runtime_root=Path(runtime_root or paths.STATE_DIR),
        hardware=hardware,
        assets=AsrAssets(variants={}, available_models=ladder))


def _analyze_default(audio_path: Path):
    from scripts.audio_pipeline import AudioPipelineError, analyze_audio

    try:
        return analyze_audio(audio_path)
    except AudioPipelineError as exc:
        logger.warning("音频分析失败（%s），按默认策略继续", exc.code)
        return None


def extract_douyin_urls(text: str) -> list[str]:
    """提取抖音链接（规范化后按出现顺序去重）。"""
    urls: list[str] = []
    seen: set[str] = set()
    for item in normalize_inputs(text or ""):
        if item.url not in seen:
            seen.add(item.url)
            urls.append(item.url)
    return urls


def candidate_aweme_id(text: str) -> str | None:
    for item in normalize_inputs(text or ""):
        if item.candidate_aweme_id:
            return item.candidate_aweme_id
    return None


# ----------------------------------------------------------------------
# 编排
# ----------------------------------------------------------------------

class _Runner:
    """一次 job 的执行上下文：阶段推进、checkpoint 与结果累积。"""

    def __init__(self, job: JobRecord, services: ServiceBundle):
        self.job = job
        self.services = services
        self.stages: list[str] = []
        self.degradations: list[str] = []
        self.checkpoint: dict = dict(job.checkpoint or {})
        self.version_id: int | None = None

    def enter(self, stage: Stage, **checkpoint) -> None:
        self.stages.append(str(stage))
        self.checkpoint.update(checkpoint)
        queue = self.services.queue
        if queue is None:
            return
        try:
            # 状态跟随阶段前进：合法前进迁移直接推进 status，同步落盘
            # stage/checkpoint；同态或非法回退时仍以同态写入落盘，不阻塞业务。
            target = str(stage)
            current = str(self.job.status)
            if target != current and can_transition(current, target):
                self.job = queue.update_status(
                    self.job.job_id, target, stage=stage,
                    checkpoint=self.checkpoint)
            else:
                self.job = queue.update_status(
                    self.job.job_id, current, stage=stage,
                    checkpoint=self.checkpoint)
            queue.heartbeat(self.job.job_id)
        except ValueError:
            # 阶段推进失败不影响业务结果，仅记录
            logger.warning("job 阶段推进失败：%s", stage)

    def degrade(self, reason: str) -> None:
        if reason not in self.degradations:
            self.degradations.append(reason)

    def result(self, status: str, *, ok: bool = True, **extra) -> dict:
        payload = {"schema_version": SCHEMA_VERSION, "ok": ok, "status": status,
                   "job_id": self.job.job_id, "batch_id": self.job.batch_id,
                   "aweme_id": self.job.aweme_id or self.job.candidate_aweme_id,
                   "stages": list(self.stages),
                   "degradations": list(self.degradations)}
        payload.update(extra)
        return payload


def _failure(runner: _Runner, code: str, message: str, **extra) -> dict:
    payload = runner.result("failed", ok=False, error={"code": str(code),
                                                       "message": message})
    payload.update(extra)
    return payload


def _initial_strategy(services: ServiceBundle, duration: float,
                      title: str) -> AsrStrategy:
    hardware = services.hardware
    model = hardware.max_model
    override = ""
    import os

    override = os.environ.get("DOUYIN_WHISPER_MODEL", "").strip()
    if override:
        model = override
    language = "zh" if contains_cjk(title) else None
    return AsrStrategy(model=model, device=hardware.device,
                       compute_type=hardware.compute_type,
                       audio_variant="original", language=language)


def _resolve_audio_input(services: ServiceBundle, default_path: Path,
                         variant: str) -> tuple[Path, str]:
    """按 strategy.audio_variant 解析本轮真实音频文件。

    增强候选缺失或文件不存在时回退原音，并返回实际生效的 variant；
    调用方必须用它改写 strategy，库里记录的才是真实出处（DY-03）。
    """
    requested = str(variant or "original")
    variants = getattr(services.assets, "variants", None) or {}
    candidate = variants.get(requested)
    if candidate:
        path = Path(candidate)
        if path.is_file():
            return path, requested
    return Path(default_path), "original"


def _run_rounds(runner: _Runner, audio_path: Path, title: str, duration: float,
                ocr_subtitles, analysis) -> list[tuple[AsrAttemptResult, QualityReport]]:
    """最多三轮；清晰首轮即停，困难按证据逐轮升级。"""
    services = runner.services
    strategy = _initial_strategy(services, duration, title)
    history: list[tuple[AsrAttemptResult, QualityReport]] = []
    for round_no in range(1, max(1, services.max_rounds) + 1):
        audio_for_round, effective = _resolve_audio_input(
            services, audio_path, strategy.audio_variant)
        if effective != strategy.audio_variant:
            runner.degrade("audio_variant_unavailable")
            strategy = replace(strategy, audio_variant=effective)
        attempt = services.asr(audio_for_round, strategy, round_no)
        if isinstance(attempt, AsrAttemptResult) and attempt.error_code == "asr_oom":
            runner.degrade("asr_oom")
        report = services.quality(attempt, analysis, ocr_subtitles)
        history.append((attempt, report))
        runner.checkpoint["attempts"] = len(history)
        if report.passed or attempt.error_code in ("audio_invalid",):
            break
        following = services.next_strategy(history, services.assets,
                                           services.hardware)
        if following is None:
            break
        strategy = following
    return history


def _pick_best(history):
    def rank(pair):
        attempt, report = pair
        return (not attempt.hard_failures, report.score)

    return max(history, key=rank)


def _store_attempts(db, version_id: int, history, selected_index: int):
    attempt_ids = []
    for index, (attempt, report) in enumerate(history):
        attempt_id = db.add_asr_attempt(
            version_id, attempt_no=attempt.attempt_no,
            audio_variant=attempt.audio_variant,
            strategy=attempt.strategy.as_record(), model=attempt.strategy.model,
            device=attempt.strategy.device,
            compute_type=attempt.strategy.compute_type,
            language=attempt.language, transcript_text=attempt.transcript_text,
            segments_json=attempt.segments,
            metrics={**attempt.metrics, "quality": report.as_dict()},
            hard_failures=attempt.hard_failures, score=report.score,
            passed=report.passed, error_code=attempt.error_code,
            started_at=attempt.started_at, finished_at=attempt.finished_at)
        attempt_ids.append(attempt_id)
    if attempt_ids:
        db.select_attempt(version_id, attempt_ids[selected_index])
    return attempt_ids


def _quality_status(history, selected, report) -> str:
    if not selected.segments:
        return str(QualityStatus.VISUAL_TEXT_ONLY)
    return str(QualityStatus.PASSED if report.passed
               else QualityStatus.NEEDS_REVIEW)


def _summary_mode(quality_status: str) -> str:
    if quality_status == str(QualityStatus.VISUAL_TEXT_ONLY):
        return str(SummaryMode.VISUAL_TEXT_ONLY)
    if quality_status == str(QualityStatus.PASSED):
        return str(SummaryMode.NORMAL)
    return str(SummaryMode.RESTRICTED)


def _visual_text(ocr_observations) -> str:
    parts = [f"画面文字：{item.get('text', '')}"
             for item in (ocr_observations or [])
             if str(item.get("kind", "")) != "speech_subtitle"
             and str(item.get("text", "")).strip()]
    return "\n".join(dict.fromkeys(parts))


def process_one(job: JobRecord, services: ServiceBundle) -> dict:
    """处理单个 job；返回 schema_version=2 的稳定 JSON 结果。"""
    runner = _Runner(job, services)
    db = services.db
    url = job.normalized_input or job.raw_input
    force = bool(services.force)
    job_dir = Path(services.runtime_root) / "jobs" / f"{job.job_id or uuid.uuid4()}"
    job_dir.mkdir(parents=True, exist_ok=True)
    aweme_id = job.candidate_aweme_id or job.aweme_id
    keep_media = False

    try:
        # ---- 复用判定：已有可用交付就不重新下载 -------------------------
        if aweme_id and not force:
            action = db.find_reuse_action(aweme_id)
            if action in (ReuseAction.REUSE, ReuseAction.RENDER_ONLY,
                          ReuseAction.FILL_AI):
                return _reuse(runner, aweme_id, action, force=force)

        runner.enter(Stage.RESOLVING)
        try:
            ingested = services.ingest(url, job_dir, True)
        except LoginRequiredError:
            return runner.result("login_required", ok=False,
                                 error={"code": str(ErrorCode.LOGIN_REQUIRED),
                                        "message": LOGIN_NEEDED_MESSAGE})
        except IngestFailure as failure:
            return _failure(runner, failure.code, failure.message)
        except Exception as exc:
            logger.exception("采集阶段异常")
            return _failure(runner, ErrorCode.INGEST_CONTRACT_CHANGED,
                            f"采集失败：{type(exc).__name__}")

        runner.enter(Stage.FETCHING, aweme_id=ingested.aweme_id)
        runner.job = replace(runner.job, aweme_id=ingested.aweme_id)

        if not force:
            action = db.find_reuse_action(ingested.aweme_id)
            if action in (ReuseAction.REUSE, ReuseAction.RENDER_ONLY,
                          ReuseAction.FILL_AI):
                runner.degrade("media_discarded_after_reuse")
                return _reuse(runner, ingested.aweme_id, action, force=force)

        version_id = db.create_staging_version(
            ingested.aweme_id, metadata=ingested.public_metadata(),
            processing_signature=CURRENT_PROCESSING_SIGNATURE)
        runner.version_id = version_id

        # ---- 媒体验证与音频分析 -----------------------------------------
        try:
            duration = services.verify_audio(ingested.audio_path) or \
                ingested.duration_seconds
        except Exception:
            logger.warning("音频验证失败")
            return _failure(runner, ErrorCode.AUDIO_INVALID,
                            "音频准备失败（无音轨或时长为零）",
                            aweme_id=ingested.aweme_id)
        runner.enter(Stage.MEDIA_READY, duration=duration)

        runner.enter(Stage.ANALYZING)
        analysis = services.analyze(ingested.audio_path)
        capabilities = {}
        try:
            capabilities = services.model_assets() or {}
        except Exception as exc:
            logger.warning("模型资产检查失败：%s", type(exc).__name__)
        variants = services.audio(ingested.audio_path, job_dir, analysis) or []
        variant_map = {str(item.kind): item.path for item in variants
                       if getattr(item, "usable", False) and item.path}
        services.assets = replace(services.assets, variants=variant_map)

        # ---- ASR：最多三轮，按证据择优 ----------------------------------
        runner.enter(Stage.TRANSCRIBING)
        try:
            history = _run_rounds(runner, ingested.audio_path, ingested.title,
                                  duration, [], analysis)
        except Exception:
            logger.exception("语音识别阶段异常")
            return _failure(runner, ErrorCode.ASR_FAILED, "本地语音识别失败",
                            aweme_id=ingested.aweme_id)
        if not history or not history[0][0].segments:
            history = history or []
        selected_index = 0
        if history:
            selected_index = history.index(_pick_best(history))
        selected, report = history[selected_index] if history else (
            AsrAttemptResult(attempt_no=1, audio_variant="original",
                             strategy=_initial_strategy(services, duration,
                                                        ingested.title)),
            QualityReport(score=0.0, status="needs_review",
                          hard_failures=["empty_output"], deductions={}))
        attempt_ids = _store_attempts(db, version_id, history, selected_index)
        quality_status = _quality_status(history, selected, report)
        db.set_quality(version_id, status=quality_status, score=report.score,
                       report=report.as_dict())

        no_speech = not selected.segments

        # ---- OCR：需要视频素材，失败只降级 ---------------------------------
        runner.enter(Stage.OCR)
        video_path = None
        ocr_outcome = None
        if capabilities.get("ocr", True) and not no_speech:
            try:
                video_path = services.video(ingested, job_dir)
            except Exception as exc:
                runner.degrade("video_unavailable_for_ocr")
                logger.warning("临时视频不可用，跳过 OCR：%s", type(exc).__name__)
        elif no_speech and capabilities.get("ocr", True):
            try:
                video_path = services.video(ingested, job_dir)
            except Exception:
                runner.degrade("video_unavailable_for_ocr")
        if video_path is not None:
            try:
                ocr_outcome = services.ocr(video_path, job_dir / "frames",
                                           selected.segments)
            except Exception as exc:
                runner.degrade("ocr_failed")
                logger.warning("OCR 失败：%s", type(exc).__name__)
        if ocr_outcome is not None and getattr(ocr_outcome, "status", "") == "failed":
            runner.degrade("ocr_failed")
        observations = [
            item.as_record() if hasattr(item, "as_record") else dict(item)
            for item in (getattr(ocr_outcome, "observations", []) or [])]
        db.replace_ocr_observations(version_id, observations)

        # 没有语音但有画面文字：交付画面文字记录
        if no_speech and observations:
            quality_status = str(QualityStatus.VISUAL_TEXT_ONLY)
            db.set_quality(version_id, status=quality_status, score=0.0,
                           report=report.as_dict())
        if no_speech and not observations:
            return _failure(runner, ErrorCode.ASR_FAILED,
                            "既没有识别到语音，也没有识别到画面文字",
                            aweme_id=ingested.aweme_id)

        # ---- 说话人分离 --------------------------------------------------
        runner.enter(Stage.DIARIZATION)
        turns = []
        if capabilities.get("diarization", True) and not no_speech:
            try:
                outcome = services.diarization(ingested.audio_path,
                                               analysis)
                turns = list(getattr(outcome, "turns", []) or [])
                if getattr(outcome, "status", "") == "skipped_or_failed":
                    runner.degrade("diarization_failed")
            except Exception as exc:
                runner.degrade("diarization_failed")
                logger.warning("说话人分离失败：%s", type(exc).__name__)
        db.replace_speaker_turns(version_id, [item.as_record() for item in turns])

        from scripts.diarization import assign_speakers

        spoken_segments = assign_speakers(selected.segments, turns)
        if attempt_ids:
            # 说话人引用写回选中轮次的片段记录（文本与时间不变）
            db.set_attempt_speakers(attempt_ids[selected_index], spoken_segments)

        # ---- 证据融合与 AI 校正 ------------------------------------------
        runner.enter(Stage.CORRECTING)
        evidence = align_evidence(spoken_segments, observations)
        context = CorrectionContext(
            title=ingested.title, segments=spoken_segments, evidence=evidence,
            quality_status=quality_status,
            visual_texts=visual_context(evidence))
        correction = CorrectionOutcome(status=str(CorrectionStatus.PENDING))
        try:
            correction = services.correction(context)
        except Exception as exc:
            runner.degrade("correction_failed")
            logger.warning("AI 校正异常：%s", type(exc).__name__)
        correction_status = str(getattr(correction, "status",
                                       CorrectionStatus.PENDING))
        if correction.corrected_text:
            db.save_correction(version_id,
                               corrected_text=correction.corrected_text,
                               changes=correction.changes,
                               status=correction_status)
        elif correction_status in (str(CorrectionStatus.PENDING),
                                   str(CorrectionStatus.FAILED_PENDING_RETRY)):
            db.save_correction(version_id, corrected_text="", changes=[],
                               status=correction_status)
        if correction_status != str(CorrectionStatus.CORRECTED):
            runner.degrade("correction_pending")

        # ---- 摘要 --------------------------------------------------------
        runner.enter(Stage.SUMMARIZING)
        mode = _summary_mode(quality_status)
        source = correction.corrected_text or selected.transcript_text
        visual = _visual_text(observations)
        if quality_status == str(QualityStatus.VISUAL_TEXT_ONLY):
            source = visual
        try:
            summary_status, summary_text = services.summary(
                ingested.title, source, mode)
        except Exception as exc:
            runner.degrade("summary_failed")
            logger.warning("摘要异常：%s", type(exc).__name__)
            summary_status, summary_text = str(SummaryStatus.PENDING), ""
        db.save_summary(version_id, summary=summary_text,
                        status=summary_status, mode=mode,
                        meta={"mode": mode, "pipeline_version": "v2",
                              "source": "corrected" if correction.corrected_text
                              else "raw"})
        if summary_status != str(SummaryStatus.SUMMARIZED):
            runner.degrade("summary_pending")

        # ---- 发布：两个文件 + DB 原子切换 ---------------------------------
        runner.enter(Stage.PUBLISHING)
        bundle = build_version_bundle(
            db, version_id, degradations=runner.degradations)
        bundle["is_current"] = True
        try:
            prepared = [render_main(bundle, services.output_root),
                        render_evidence(bundle, services.output_root)]
            published = publish_artifacts(db, version_id, prepared,
                                          output_root=services.output_root)
        except Exception:
            logger.exception("发布失败，旧版本保持不变")
            return _failure(runner, ErrorCode.PUBLISH_FAILED,
                            "交付文件发布失败，旧版本仍可读取",
                            aweme_id=ingested.aweme_id)

        if no_speech:
            status = "visual_text_only"
        elif (correction_status != str(CorrectionStatus.CORRECTED)
              or summary_status != str(SummaryStatus.SUMMARIZED)):
            status = "awaiting_ai"
        elif quality_status == str(QualityStatus.PASSED):
            status = "completed"
        else:
            status = "needs_review"

        return runner.result(
            status, aweme_id=ingested.aweme_id, title=ingested.title,
            version_id=version_id, job_id=job.job_id,
            quality_status=quality_status, quality_score=round(report.score, 2),
            asr_attempts=len(history),
            main_file=published.main_file, evidence_file=published.evidence_file,
            correction_status=correction_status, summary_status=summary_status,
            summary_mode=mode, reused=False)
    finally:
        if not keep_media:
            try:
                services.cleanup(job_dir)
            except Exception:
                logger.warning("清理 job 目录失败：%s", job_dir.name)


def _reuse(runner: _Runner, aweme_id: str, action: ReuseAction, *,
           force: bool) -> dict:
    """复用已有交付；文件缺失或哈希不符时只从数据库重渲染。"""
    db = runner.services.db
    version = db.get_current_version(aweme_id)
    if version is None:
        return _failure(runner, ErrorCode.DATABASE_FAILED, "复用判定失效，请重试")
    reuse_action = str(action)
    main_file = version.get("main_file", "")
    evidence_file = version.get("evidence_file", "")
    if action in (ReuseAction.RENDER_ONLY, ReuseAction.FILL_AI):
        try:
            published = repair_artifacts(db, aweme_id,
                                         output_root=runner.services.output_root)
            main_file, evidence_file = published.main_file, published.evidence_file
            reuse_action = f"{action}"
        except Exception as exc:
            logger.warning("重渲染失败（%s），改从来源重新处理", type(exc).__name__)
            return _failure(runner, ErrorCode.ARTIFACT_MISSING,
                            "既有交付文件损坏且重渲染失败")
    runner.stages = [str(Stage.RESOLVING)]
    payload = runner.result(
        "reused", aweme_id=aweme_id, version_id=int(version["version_id"]),
        title=(db.get_video(aweme_id) or {}).get("title", ""),
        quality_status=version.get("quality_status"),
        quality_score=float(version.get("quality_score") or 0),
        main_file=main_file, evidence_file=evidence_file,
        correction_status=version.get("correction_status"),
        summary_status=version.get("summary_status"),
        reuse_action=reuse_action, reused=True)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="转录单个抖音作品（v2）")
    parser.add_argument("input", help="抖音分享文案、短链或作品链接")
    parser.add_argument("--force", action="store_true", help="强制重转")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--visible", action="store_true",
                        help="以可见 Chromium 运行上游（用户已确认扫码登录）")
    parser.add_argument("--channel", default="",
                        choices=["auto", "ingest", "yt-dlp"],
                        help="采集通道：auto（默认，锁定版 ingest 失败时回落 yt-dlp）、"
                             "ingest（只用上游）、yt-dlp（只用降级通道）")
    args = parser.parse_args(argv)

    if args.channel:
        import os
        os.environ["DOUYIN_INGEST_CHANNEL"] = args.channel

    paths.ensure_runtime_dirs()
    ensure_utf8_stdio()
    paths.load_skill_env()
    configure_logging()

    from scripts.transcript_db import TranscriptDB

    db = TranscriptDB(paths.DB_PATH)
    try:
        urls = extract_douyin_urls(args.input)
        if not urls:
            payload = {"schema_version": SCHEMA_VERSION, "ok": False,
                       "status": "failed",
                       "error": {"code": str(ErrorCode.INVALID_INPUT),
                                 "message": "未在输入中找到抖音链接"}}
        else:
            job = JobRecord(
                job_id=str(uuid.uuid4()), batch_id="", position=0,
                raw_input=args.input, normalized_input=urls[0],
                candidate_aweme_id=candidate_aweme_id(args.input) or "",
                aweme_id="", status=str(JobStatus.RESOLVING),
                stage=str(Stage.RESOLVING), duplicate_of_job_id="",
                failure_code="", failure_message="", checkpoint={},
                heartbeat_at="", created_at=utc_now(), updated_at=utc_now())
            services = default_services(db)
            services.force = bool(args.force)
            if args.visible:
                services.ingest = lambda url, job_dir, headless: \
                    _default_ingest(url, job_dir, False)
            payload = process_one(job, services)
    except Exception as exc:  # CLI 兜底：仍输出一行 JSON
        logger.exception("入口异常")
        payload = {"schema_version": SCHEMA_VERSION, "ok": False,
                   "status": "failed",
                   "error": {"code": str(ErrorCode.DATABASE_FAILED),
                             "message": f"内部错误：{type(exc).__name__}"}}
    finally:
        db.close()
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
