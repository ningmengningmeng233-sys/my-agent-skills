"""DY-04 回归：长文本校正必须分块 + 截断降级，杜绝死循环空耗 API 费用。

原缺陷：
- correct_or_pending 单次请求携带全部段落，输出预算仅 2048 tokens；
  中长视频必然被模型截断，缺段又触发“必须覆盖全部段”硬校验 → PENDING；
  fill-ai 下次原样重试同一超长请求，卡在 awaiting_ai 反复烧钱，永不收敛。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import douyin_transcript as dt
from scripts import maintenance
from scripts.asr import AsrAttemptResult, AsrStrategy
from scripts.audio_pipeline import AudioAnalysis, AudioVariant
from scripts.correct_transcript import (CorrectionContext, CorrectionOutcome,
                                        correct_or_pending)
from scripts.diarization import DiarizationOutcome
from scripts.ingest_adapter import IngestResult
from scripts.models import (CorrectionStatus, JobRecord, JobStatus,
                            QualityStatus, Stage)
from scripts.ocr import OcrOutcome
from scripts.quality import AsrAssets, HardwareBudget, QualityReport
from scripts.transcript_db import TranscriptDB

AWEME = "7652684072266845450"
URL = f"https://www.douyin.com/video/{AWEME}"
CONFIG = {"api_key": "sk-test", "url": "http://127.0.0.1:9/v1",
          "model": "mock-model"}


def _segments(count):
    return [{"start": float(i), "end": float(i + 1),
             "text": f"第{i + 1}段口播内容需要校正", "speaker": ""}
            for i in range(count)]


def _context(count):
    return CorrectionContext(title="长视频标题",
                             segments=_segments(count), evidence=[],
                             quality_status="passed", language="zh")


def _requested_ids(messages):
    body = messages[-1]["content"]
    material = body.split("素材：", 1)[1].split("\n\n画面文字", 1)[0]
    return [item["id"] for item in json.loads(material)]


def _requested_texts(messages):
    body = messages[-1]["content"]
    material = body.split("素材：", 1)[1].split("\n\n画面文字", 1)[0]
    return {item["id"]: item["text"] for item in json.loads(material)}


class TruncatingModel:
    """模拟 2048 token 截断：永远只返回请求段的前一部分。"""

    def __init__(self, keep_ratio=0.34):
        self.calls = 0
        self.keep_ratio = keep_ratio

    def __call__(self, messages, config):
        self.calls += 1
        texts = _requested_texts(messages)
        ids = list(texts)
        keep = max(1, int(len(ids) * self.keep_ratio))
        return json.dumps({"segments": [
            {"id": seg_id, "text": texts[seg_id] + "。",
             "evidence": "", "confidence": 0.0}
            for seg_id in ids[:keep]]}, ensure_ascii=False)


class FullCoverageModel:
    """行为良好的模型：完整覆盖请求段并加标点。"""

    def __init__(self):
        self.calls = 0
        self.seen_ids: list[list[str]] = []

    def __call__(self, messages, config):
        self.calls += 1
        texts = _requested_texts(messages)
        self.seen_ids.append(list(texts))
        return json.dumps({"segments": [
            {"id": seg_id, "text": text + "。",
             "evidence": "", "confidence": 0.0}
            for seg_id, text in texts.items()]}, ensure_ascii=False)


class TruncationDefenseTests(unittest.TestCase):
    def test_truncated_long_text_yields_partial_with_safe_merge(self):
        """长文本被截断：一次调用收敛为 partial，有界调用且安全合并。"""
        model = TruncatingModel()
        outcome = correct_or_pending(_context(90), CONFIG, call=model)
        self.assertEqual(outcome.status, "partial_corrected")
        # 有界：分块后每块只试一次，不存在重试循环。
        self.assertGreater(model.calls, 1)
        self.assertLessEqual(model.calls, 5)
        # 安全合并：90 段齐全有序，未覆盖段保留原文。
        self.assertEqual(len(outcome.segments), 90)
        self.assertEqual([item["segment_id"] for item in outcome.segments],
                         [f"seg-{i + 1}" for i in range(90)])
        originals = {f"seg-{i + 1}": f"第{i + 1}段口播内容需要校正"
                     for i in range(90)}
        for item in outcome.segments:
            expected = originals[item["segment_id"]]
            self.assertIn(item["text"], (expected, expected + "。"))
        self.assertEqual(len(outcome.corrected_text.split("\n")), 90)
        skipped = [change for change in outcome.changes
                   if change["change_type"] == "skipped"]
        self.assertTrue(skipped)

    def test_single_request_truncation_degrades_without_retry(self):
        """短请求缺段：同样降级为 partial，且只调用一次模型。"""
        model = TruncatingModel(keep_ratio=0.7)
        outcome = correct_or_pending(_context(3), CONFIG, call=model)
        self.assertEqual(outcome.status, "partial_corrected")
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(outcome.segments), 3)
        self.assertEqual(outcome.segments[2]["text"], "第3段口播内容需要校正")
        by_ref = {change["source_segment_ref"]: change
                  for change in outcome.changes}
        self.assertEqual(by_ref["seg-3"]["change_type"], "skipped")
        self.assertIn("truncated", by_ref["seg-3"]["evidence"]["doubts"])

    def test_garbage_output_stays_retryable_pending(self):
        """非截断的结构性失败仍保持 pending 可重试（不误伤原有语义）。"""
        outcome = correct_or_pending(
            _context(2), CONFIG, call=lambda m, c: "这不是 JSON")
        self.assertEqual(outcome.status, "pending")


class ChunkingTests(unittest.TestCase):
    def test_long_text_is_corrected_in_chunks(self):
        """长文本分多块校正：全覆盖时仍为 corrected，且每块独立请求。"""
        model = FullCoverageModel()
        outcome = correct_or_pending(_context(65), CONFIG, call=model)
        self.assertEqual(outcome.status, "corrected")
        self.assertGreater(model.calls, 1)
        self.assertLessEqual(model.calls, 5)
        self.assertEqual(len(outcome.segments), 65)
        self.assertEqual([item["segment_id"] for item in outcome.segments],
                         [f"seg-{i + 1}" for i in range(65)])
        for item in outcome.segments:
            self.assertTrue(item["text"].endswith("。"))
        self.assertEqual([change["source_segment_ref"]
                          for change in outcome.changes],
                         [f"seg-{i + 1}" for i in range(65)])


class PartialTerminalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = TranscriptDB(self.root / ".db" / "t.db")
        self.addCleanup(self.db.close)
        self.output = self.root / "output"

    def _seed_partial(self):
        version_id = self.db.create_staging_version(
            AWEME, metadata={"title": "标题", "author": "奶龙",
                             "publish_date": "2026-06-19",
                             "duration_seconds": 30.0})
        self.db.add_asr_attempt(
            version_id, attempt_no=1, audio_variant="original",
            strategy={"vad_filter": True}, model="medium", device="cuda",
            compute_type="float16", language="zh",
            transcript_text="五月五，过端午",
            segments_json=[{"start": 0.0, "end": 4.0,
                            "text": "五月五，过端午",
                            "avg_logprob": -0.2, "no_speech_prob": 0.0,
                            "review_reasons": []}],
            metrics={"duration": 30.0}, hard_failures=[], score=90.0,
            passed=True)
        attempt = self.db.list_attempts(version_id)[0]
        self.db.select_attempt(version_id, attempt["attempt_id"])
        self.db.set_quality(version_id, status=QualityStatus.PASSED,
                            score=90.0, report={})
        self.db.save_summary(version_id, summary="", status="pending",
                             mode="normal", meta={})
        self.db.save_correction(version_id,
                                corrected_text="五月五，过端午。",
                                changes=[], status="partial_corrected")
        from scripts.artifacts import (build_version_bundle, publish_artifacts,
                                      render_evidence, render_main)

        bundle = build_version_bundle(self.db, version_id)
        bundle["is_current"] = True
        publish_artifacts(self.db, version_id,
                          [render_main(bundle, self.output),
                           render_evidence(bundle, self.output)],
                          output_root=self.output)
        return version_id

    def test_fill_ai_does_not_recall_correction_for_partial(self):
        """partial 是终态：fill-ai 只补摘要，不再重调校正烧钱。"""
        version_id = self._seed_partial()
        correction_call = mock.Mock(
            return_value=mock.Mock(status="corrected", corrected_text="x",
                                   changes=[]))
        report = maintenance.fill_ai(
            self.db, output_root=self.output,
            correction_call=correction_call,
            summary_call=lambda title, text, mode: ("summarized", "新摘要"))
        correction_call.assert_not_called()
        self.assertEqual(report["filled"], 1)
        version = self.db.get_version(version_id)
        self.assertEqual(version["correction_status"], "partial_corrected")
        self.assertEqual(version["summary_status"], "summarized")


class _SingleRoundFakes:
    def __init__(self, root: Path):
        self.root = root
        media = root / "media"
        media.mkdir(parents=True, exist_ok=True)
        self.audio_file = media / "speech.m4a"
        self.audio_file.write_bytes(b"audio")
        self.analysis = AudioAnalysis(
            duration=20.0, loudness_lufs=-16.0, peak_db=-1.5,
            clipped_ratio=0.0, silence_ratio=0.05, sample_rate=44100,
            channels=2)

    def ingest(self, url, job_dir, headless=True):
        return IngestResult(
            aweme_id=AWEME, canonical_url=URL, title="标题", author="奶龙",
            publish_date="2026-06-19", duration_seconds=20.0,
            audio_path=self.audio_file,
            video_download_url="https://cdn.test/v.mp4",
            download_headers={})

    def video(self, result, job_dir):
        path = Path(job_dir) / "media" / "video.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return path

    def analyze(self, audio):
        return self.analysis

    def audio(self, audio_path, job_dir, analysis, **kwargs):
        return [AudioVariant(kind="original", path=self.audio_file)]

    def asr(self, audio_path, strategy, attempt_no):
        segments = [{"start": 0.0, "end": 8.0,
                     "text": "端午就看我奶龙大侠赛龙舟",
                     "avg_logprob": -0.2, "no_speech_prob": 0.01,
                     "low_confidence_words": [], "needs_review": False,
                     "review_reasons": [], "speaker": ""}]
        return AsrAttemptResult(
            attempt_no=attempt_no, audio_variant=strategy.audio_variant,
            strategy=strategy, transcript_text=segments[0]["text"],
            segments=segments,
            metrics={"duration": 20.0, "language_probability": 0.99},
            hard_failures=[], score=88.0, passed=True,
            started_at="2026-09-16T10:00:01",
            finished_at="2026-09-16T10:00:02")

    def quality(self, result, analysis=None, ocr=None):
        return QualityReport(score=result.score, status="passed",
                             hard_failures=[], deductions={})

    def next_strategy(self, history, assets, hardware, **kwargs):
        return None

    def ocr(self, video, frame_dir, asr_segments):
        return OcrOutcome(status="ok", observations=[])

    def diarization(self, audio_path, analysis=None):
        return DiarizationOutcome(status="ok", speaker_count=1, turns=[])

    def correction(self, context):
        return CorrectionOutcome(
            status="partial_corrected",
            corrected_text="\n".join(
                item["text"] for item in context.segments),
            segments=list(context.segments), changes=[],
            meta={"reason": "truncated", "partial": True})

    def summary(self, title, text, mode="normal"):
        return ("summarized", "**核心观点：**\n好")

    def model_assets(self):
        return {"ocr": True, "vocals": True, "diarization": True}

    def cleanup(self, job_dir, *, preserve=frozenset()):
        return None


class OrchestratorPartialTests(unittest.TestCase):
    def test_partial_correction_completes_instead_of_awaiting_ai(self):
        """编排器遇到 partial 校正：按完成态交付，不卡 awaiting_ai。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        db = TranscriptDB(root / ".db" / "t.db")
        self.addCleanup(db.close)
        fakes = _SingleRoundFakes(root)
        job = JobRecord(
            job_id="job-dy04", batch_id="batch-1", position=0,
            raw_input=f"文案 {URL}", normalized_input=URL,
            candidate_aweme_id=AWEME, aweme_id="",
            status=str(JobStatus.RESOLVING), stage=str(Stage.RESOLVING),
            duplicate_of_job_id="", failure_code="", failure_message="",
            checkpoint={}, heartbeat_at="",
            created_at="2026-09-16T10:00:00",
            updated_at="2026-09-16T10:00:00")
        services = dt.ServiceBundle(
            ingest=fakes.ingest, video=fakes.video, analyze=fakes.analyze,
            audio=fakes.audio, asr=fakes.asr, quality=fakes.quality,
            next_strategy=fakes.next_strategy, ocr=fakes.ocr,
            diarization=fakes.diarization, correction=fakes.correction,
            summary=fakes.summary, model_assets=fakes.model_assets, db=db,
            cleanup=fakes.cleanup, output_root=root / "output",
            verify_audio=lambda path: 20.0,
            hardware=HardwareBudget(device="cuda", compute_type="float16",
                                    max_model="medium", vram_gb=8.0),
            assets=AsrAssets(variants={},
                             available_models=("base", "small", "medium")))
        result = dt.process_one(job, services)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["correction_status"], "partial_corrected")
        self.assertIn("correction_partial", result["degradations"])
        self.assertNotIn("correction_pending", result["degradations"])
        body = Path(result["main_file"]).read_text(encoding="utf-8")
        self.assertIn("校正版逐字稿", body)
        self.assertNotIn("未经 AI 校正原始稿", body)


if __name__ == "__main__":
    unittest.main()
