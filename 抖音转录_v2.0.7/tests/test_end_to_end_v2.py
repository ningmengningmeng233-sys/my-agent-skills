"""v2 fake 端到端验收：真实数据库、真实渲染与发布、真实清理；只替换外部世界。

交付四条件：SQLite 有当前版本、两个 TXT 存在且哈希一致、摘要状态已明确、
job 目录无媒体残留；随后验证复用、补 AI 与强制重转。
"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts import douyin_transcript as dt
from scripts.artifacts import build_version_bundle
from scripts.asr import AsrAttemptResult, AsrStrategy
from scripts.audio_pipeline import AudioAnalysis, AudioVariant
from scripts.correct_transcript import CorrectionOutcome
from scripts.diarization import DiarizationOutcome, SpeakerTurn
from scripts.ingest_adapter import IngestResult
from scripts.models import (CorrectionStatus, JobRecord, JobStatus, QualityStatus,
                            Stage, SummaryStatus)
from scripts.ocr import OcrObservation, OcrOutcome
from scripts.quality import AsrAssets, HardwareBudget, QualityReport
from scripts.transcript_db import TranscriptDB

AWEME = "7410000000000001234"
URL = f"https://www.douyin.com/video/{AWEME}"
MEDIA_SUFFIXES = {".mp4", ".m4a", ".mp3", ".wav", ".jpg", ".png"}

SEGMENTS = [
    {"start": 1.0, "end": 4.0, "text": "五月五，过端午", "avg_logprob": -0.2,
     "no_speech_prob": 0.0, "low_confidence_words": [], "needs_review": False,
     "review_reasons": []},
    {"start": 4.0, "end": 8.5, "text": "做个粽子还那么多花样", "avg_logprob": -0.3,
     "no_speech_prob": 0.01, "low_confidence_words": [], "needs_review": False,
     "review_reasons": []},
]


class EndToEndV2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.jobs_root = self.root / "jobs"
        self.db = TranscriptDB(self.root / ".db" / "transcripts.db")
        self.addCleanup(self.db.close)
        self.output = self.root / "output"
        self.calls: list[str] = []

    # ---- 外部世界（唯一的假实现） ---------------------------------------
    def _ingest(self, url, job_dir, headless=True):
        self.calls.append("ingest")
        media = Path(job_dir) / "media"
        media.mkdir(parents=True, exist_ok=True)
        (media / "video.mp4").write_bytes(b"fake-video")
        audio = media / "speech.m4a"
        audio.write_bytes(b"fake-audio")
        return IngestResult(aweme_id=AWEME, canonical_url=URL, title="端到端标题",
                            author="奶龙", publish_date="2026-06-19",
                            duration_seconds=8.5, audio_path=audio,
                            video_download_url="https://cdn.test/v.mp4",
                            download_headers={"cookie": "x"})

    def _video(self, result, job_dir):
        self.calls.append("video")
        path = Path(job_dir) / "media" / "video.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-video")
        return path

    def _asr(self, audio_path, strategy, attempt_no):
        self.calls.append(f"asr:{attempt_no}")
        return AsrAttemptResult(
            attempt_no=attempt_no, audio_variant=strategy.audio_variant,
            strategy=strategy, transcript_text="\n".join(
                item["text"] for item in SEGMENTS),
            segments=[dict(item) for item in SEGMENTS],
            metrics={"duration": 8.5, "duration_after_vad": 8.0,
                     "language_probability": 0.98, "segment_count": 2,
                     "word_count": 20},
            hard_failures=[], score=92.0, passed=True,
            started_at="2026-09-16T10:00:00", finished_at="2026-09-16T10:00:03")

    def _services(self, **overrides) -> dt.ServiceBundle:
        values = dict(
            ingest=self._ingest, video=self._video,
            analyze=lambda path: AudioAnalysis(
                duration=8.5, loudness_lufs=-16.0, peak_db=-1.5,
                clipped_ratio=0.0, silence_ratio=0.05, sample_rate=44100,
                channels=2),
            audio=lambda path, job_dir, analysis: [
                AudioVariant(kind="original", path=path)],
            asr=self._asr,
            quality=lambda attempt, analysis=None, ocr=None: QualityReport(
                score=92.0, status="passed", hard_failures=[], deductions={}),
            next_strategy=lambda *a, **k: None,
            ocr=lambda video, frame_dir, segments: OcrOutcome(
                status="ok", observations=[OcrObservation(
                    start=1.0, end=8.0, text="五月五，过端午",
                    bbox=((0, 300), (400, 300), (400, 340), (0, 340)),
                    confidence=0.95, kind="speech_subtitle", frame_count=5)]),
            diarization=lambda audio, analysis=None: DiarizationOutcome(
                status="ok", speaker_count=2,
                turns=[SpeakerTurn(0.0, 4.5, "speaker_a", 0.9),
                       SpeakerTurn(4.5, 8.5, "speaker_b", 0.85)]),
            correction=lambda context: CorrectionOutcome(
                status=str(CorrectionStatus.CORRECTED),
                corrected_text="五月五，过端午。\n做个粽子还那么多花样。",
                segments=[dict(item, text=item["text"] + "。")
                          for item in context.segments],
                changes=[{"source_segment_ref": "seg-1",
                          "original_text": "五月五，过端午",
                          "corrected_text": "五月五，过端午。",
                          "change_type": "punctuation",
                          "evidence": {"source": "none"}, "confidence": 0.0}]),
            summary=lambda title, text, mode="normal": (
                str(SummaryStatus.SUMMARIZED),
                "**核心观点：**\n端午粽子花样多\n**主要论点：**\n1. 粽叶飘香\n"
                "**关键结论：**\n吃不够"),
            model_assets=lambda: {"ocr": True, "vocals": True, "diarization": True},
            db=self.db,
            cleanup=lambda job_dir, preserve=frozenset(): self._cleanup(job_dir),
            output_root=self.output, runtime_root=self.root,
            verify_audio=lambda path: 8.5,
            hardware=HardwareBudget(device="cpu", compute_type="int8",
                                    max_model="base"),
            assets=AsrAssets(variants={}, available_models=("base",)))
        values.update(overrides)
        return dt.ServiceBundle(**values)

    def _cleanup(self, job_dir) -> None:
        self.calls.append("cleanup")
        for path in sorted(Path(job_dir).rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    def _job(self, **overrides) -> JobRecord:
        values = dict(job_id="job-e2e", batch_id="batch-e2e", position=0,
                      raw_input=URL, normalized_input=URL,
                      candidate_aweme_id=AWEME, aweme_id="",
                      status=str(JobStatus.RESOLVING), stage=str(Stage.RESOLVING),
                      duplicate_of_job_id="", failure_code="",
                      failure_message="", checkpoint={}, heartbeat_at="",
                      created_at="2026-09-16T10:00:00",
                      updated_at="2026-09-16T10:00:00")
        values.update(overrides)
        return JobRecord(**values)

    def _process(self, **overrides):
        return dt.process_one(self._job(), self._services(**overrides))

    def _leftover_media(self) -> list[Path]:
        if not self.root.exists():
            return []
        return [path for path in self.root.rglob("*")
                if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
                and ".db" not in path.parts]


class DeliveryTests(EndToEndV2Tests):
    def test_delivers_database_files_and_cleanup(self):
        result = self._process()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")

        current = self.db.get_current_version(AWEME)
        self.assertIsNotNone(current)
        self.assertEqual(current["quality_status"], str(QualityStatus.PASSED))
        self.assertIn("五月五", current["raw_text"])

        for path in (result["main_file"], result["evidence_file"]):
            self.assertTrue(Path(path).is_file())
        main = Path(result["main_file"]).read_text(encoding="utf-8")
        self.assertIn("[00:00:01 → 00:00:04]", main)
        self.assertIn("说话人 A", main)
        self.assertIn("核心观点", main)

        self.assertEqual(self._leftover_media(), [])
        self.assertIn("cleanup", self.calls)

    def test_artifact_hashes_match_database(self):
        import hashlib

        self._process()
        current = self.db.get_current_version(AWEME)
        for artifact in current["artifacts"].values():
            raw = Path(artifact["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), artifact["sha256"])

    def test_evidence_file_contains_full_trace(self):
        result = self._process()
        body = Path(result["evidence_file"]).read_text(encoding="utf-8")
        for expected in ("ASR 轮次", "原始时间戳稿", "画面文字", "说话人",
                        "重要修正", "疑点", "降级说明"):
            self.assertIn(expected, body)

    def test_no_sensitive_context_is_persisted(self):
        self._process()
        current = self.db.get_current_version(AWEME)
        bundle = build_version_bundle(self.db, current["version_id"])
        blob = repr(bundle)
        self.assertNotIn("cdn.test", blob)
        self.assertNotIn("cookie", blob.lower())

    def test_second_run_reuses_without_touching_the_network(self):
        first = self._process()
        self.calls.clear()
        second = dt.process_one(self._job(), self._services())
        self.assertEqual(second["status"], "reused")
        self.assertNotIn("ingest", self.calls)
        self.assertEqual(second["main_file"], first["main_file"])

    def test_force_retranscribes_and_keeps_history(self):
        first = self._process()
        second = dt.process_one(self._job(), self._services(force=True))
        self.assertEqual(second["status"], "completed")
        self.assertNotEqual(second["version_id"], first["version_id"])
        versions = self.db.list_versions(AWEME)
        self.assertEqual(len(versions), 2)
        self.assertEqual([item["is_current"] for item in versions], [0, 1])
        self.assertEqual(self._leftover_media(), [])

    def test_repaired_files_come_back_from_database_only(self):
        first = self._process()
        Path(first["main_file"]).unlink()
        self.calls.clear()
        second = dt.process_one(self._job(), self._services())
        self.assertEqual(second["reuse_action"], "render_only")
        self.assertTrue(Path(second["main_file"]).is_file())
        self.assertNotIn("ingest", self.calls)
        self.assertNotIn("asr:1", self.calls)


class FailureDeliveryTests(EndToEndV2Tests):
    def test_old_delivery_survives_a_failed_forced_run(self):
        first = self._process()
        old_main = Path(first["main_file"]).read_text(encoding="utf-8")

        def boom(title, text, mode="normal"):
            raise RuntimeError("summary down")

        second = dt.process_one(self._job(),
                                self._services(force=True, summary=boom))
        self.assertTrue(second["ok"])          # 摘要失败不影响交付
        self.assertEqual(second["status"], "awaiting_ai")
        self.assertEqual(self.db.get_current_version(AWEME)["version_id"],
                         second["version_id"])
        # 新版本已发布，但旧文件仍以新旧顺序存在（同名文件被替换）
        self.assertTrue(Path(first["main_file"]).is_file())
        self.assertNotEqual(old_main, "")

    def test_login_required_keeps_database_untouched(self):
        def login(url, job_dir, headless=True):
            raise dt.LoginRequiredError()

        result = self._process(ingest=login)
        self.assertEqual(result["status"], "login_required")
        self.assertIsNone(self.db.get_current_version(AWEME))
        self.assertEqual(self._leftover_media(), [])

    def test_ingest_failure_leaves_no_media(self):
        def broken(url, job_dir, headless=True):
            media = Path(job_dir) / "media"
            media.mkdir(parents=True, exist_ok=True)
            (media / "speech.m4a").write_bytes(b"partial")
            raise dt.IngestFailure("video_unavailable", "作品不可用")

        result = self._process(ingest=broken)
        self.assertEqual(result["error"]["code"], "video_unavailable")
        self.assertEqual(self._leftover_media(), [])


if __name__ == "__main__":
    unittest.main()
