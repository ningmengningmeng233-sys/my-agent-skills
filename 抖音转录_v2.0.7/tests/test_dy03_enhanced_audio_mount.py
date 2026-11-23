"""DY-03 回归：多轮转录必须真实挂载增强音轨，并如实记录音频出处。

原缺陷：
- process_one 把增强候选（vocals/enhanced）只写入 services.assets.variants，
  但 _run_rounds 每一轮都把原始 ingested.audio_path 传给 services.asr，
  strategy.audio_variant 形同虚设：第 2/3 轮白跑原始音轨。
- AsrAttemptResult.audio_variant 照抄 strategy，导致库里记录的出处
  （vocals/enhanced）与实际转录的音频（original）不一致。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts import douyin_transcript as dt
from scripts.asr import AsrAttemptResult, AsrStrategy
from scripts.audio_pipeline import AudioAnalysis, AudioVariant
from scripts.correct_transcript import CorrectionOutcome
from scripts.diarization import DiarizationOutcome
from scripts.ingest_adapter import IngestResult
from scripts.models import (CorrectionStatus, JobRecord, JobStatus, Stage)
from scripts.ocr import OcrOutcome
from scripts.quality import AsrAssets, HardwareBudget, QualityReport
from scripts.transcript_db import TranscriptDB

AWEME = "7652684072266845450"
URL = f"https://www.douyin.com/video/{AWEME}"


def _segments():
    return [{"start": 0.0, "end": 8.0, "text": "端午就看我奶龙大侠赛龙舟",
             "avg_logprob": -0.2, "no_speech_prob": 0.01,
             "low_confidence_words": [], "needs_review": False,
             "review_reasons": [], "speaker": ""}]


def _attempt(no, *, variant, passing, model="medium"):
    return AsrAttemptResult(
        attempt_no=no, audio_variant=variant,
        strategy=AsrStrategy(model=model, device="cuda",
                             compute_type="float16",
                             audio_variant=variant, language="zh"),
        transcript_text="端午就看我奶龙大侠赛龙舟",
        segments=_segments(),
        metrics={"duration": 20.0, "language_probability": 0.99},
        hard_failures=[] if passing else ["too_little_text"],
        score=88.0 if passing else 60.0, passed=passing,
        started_at="2026-09-16T10:00:01",
        finished_at="2026-09-16T10:00:02")


class _Fakes:
    def __init__(self, root: Path):
        self.root = root
        self.asr_paths: list[Path] = []
        self.strategies: list[AsrStrategy] = []
        self.passing_attempts = {3}
        self.plan = ["vocals", "enhanced"]
        media = root / "media"
        media.mkdir(parents=True, exist_ok=True)
        self.original_file = media / "speech.m4a"
        self.vocals_file = media / "speech.vocals.wav"
        self.enhanced_file = media / "speech.enhanced.wav"
        for path in (self.original_file, self.vocals_file,
                     self.enhanced_file):
            path.write_bytes(b"audio")
        self.variants = [
            AudioVariant(kind="original", path=self.original_file),
            AudioVariant(kind="vocals", path=self.vocals_file,
                         generator="sherpa-uvr"),
            AudioVariant(kind="enhanced", path=self.enhanced_file,
                         generator="ffmpeg-loudnorm"),
        ]
        self.analysis = AudioAnalysis(
            duration=20.0, loudness_lufs=-16.0, peak_db=-1.5,
            clipped_ratio=0.0, silence_ratio=0.05, sample_rate=44100,
            channels=2)

    def ingest(self, url, job_dir, headless=True):
        return IngestResult(
            aweme_id=AWEME, canonical_url=URL, title="标题", author="奶龙",
            publish_date="2026-06-19", duration_seconds=20.0,
            audio_path=self.original_file,
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
        return list(self.variants)

    def asr(self, audio_path, strategy, attempt_no):
        self.asr_paths.append(Path(audio_path))
        self.strategies.append(strategy)
        return _attempt(attempt_no, variant=strategy.audio_variant,
                        passing=attempt_no in self.passing_attempts,
                        model=strategy.model)

    def quality(self, result, analysis=None, ocr=None):
        return QualityReport(
            score=result.score,
            status="passed" if result.passed else "needs_review",
            hard_failures=list(result.hard_failures), deductions={})

    def next_strategy(self, history, assets, hardware, **kwargs):
        if len(history) >= 3:
            return None
        variant = self.plan[len(history) - 1]
        return replace(history[-1][0].strategy, audio_variant=variant)

    def ocr(self, video, frame_dir, asr_segments):
        return OcrOutcome(status="ok", observations=[])

    def diarization(self, audio_path, analysis=None):
        return DiarizationOutcome(status="ok", speaker_count=1, turns=[])

    def correction(self, context):
        return CorrectionOutcome(
            status=str(CorrectionStatus.CORRECTED),
            corrected_text="\n".join(
                item["text"] for item in context.segments),
            segments=list(context.segments), changes=[])

    def summary(self, title, text, mode="normal"):
        return ("summarized", "**核心观点：**\n好")

    def model_assets(self):
        return {"ocr": True, "vocals": True, "diarization": True}

    def cleanup(self, job_dir, *, preserve=frozenset()):
        return None


class EnhancedAudioMountTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = TranscriptDB(self.root / ".db" / "t.db")
        self.addCleanup(self.db.close)
        self.fakes = _Fakes(self.root)
        self.job = JobRecord(
            job_id="job-dy03", batch_id="batch-1", position=0,
            raw_input=f"文案 {URL}", normalized_input=URL,
            candidate_aweme_id=AWEME, aweme_id="",
            status=str(JobStatus.RESOLVING), stage=str(Stage.RESOLVING),
            duplicate_of_job_id="", failure_code="", failure_message="",
            checkpoint={}, heartbeat_at="",
            created_at="2026-09-16T10:00:00",
            updated_at="2026-09-16T10:00:00")

    def _services(self, **overrides):
        values = dict(
            ingest=self.fakes.ingest, video=self.fakes.video,
            analyze=self.fakes.analyze, audio=self.fakes.audio,
            asr=self.fakes.asr, quality=self.fakes.quality,
            next_strategy=self.fakes.next_strategy, ocr=self.fakes.ocr,
            diarization=self.fakes.diarization,
            correction=self.fakes.correction, summary=self.fakes.summary,
            model_assets=self.fakes.model_assets, db=self.db,
            cleanup=self.fakes.cleanup, output_root=self.root / "output",
            verify_audio=lambda path: 20.0,
            hardware=HardwareBudget(device="cuda", compute_type="float16",
                                    max_model="medium", vram_gb=8.0),
            assets=AsrAssets(variants={},
                             available_models=("base", "small", "medium")))
        values.update(overrides)
        return dt.ServiceBundle(**values)

    def test_rounds_mount_real_enhanced_files(self):
        """第 2/3 轮传给 runner 的必须是增强产物，而非原始音轨。"""
        result = dt.process_one(self.job, self._services())
        self.assertEqual(result["asr_attempts"], 3)
        self.assertEqual(
            [path.name for path in self.fakes.asr_paths],
            ["speech.m4a", "speech.vocals.wav", "speech.enhanced.wav"])
        self.assertEqual(self.fakes.asr_paths[1],
                         self.fakes.vocals_file)
        self.assertEqual(self.fakes.asr_paths[2],
                         self.fakes.enhanced_file)

    def test_database_records_truthful_audio_source(self):
        """库里记录的 audio_variant 必须与实际挂载的音频一致。"""
        result = dt.process_one(self.job, self._services())
        current = self.db.get_current_version(AWEME)
        self.assertIsNotNone(current)
        attempts = self.db.list_attempts(current["version_id"])
        self.assertEqual([item["audio_variant"] for item in attempts],
                         ["original", "vocals", "enhanced"])
        mounted = {item["audio_variant"]: path
                   for item, path in zip(attempts, self.fakes.asr_paths)}
        self.assertEqual(mounted["vocals"], self.fakes.vocals_file)
        self.assertEqual(mounted["enhanced"], self.fakes.enhanced_file)
        self.assertEqual(result["status"], "completed")

    def test_missing_variant_falls_back_and_records_original(self):
        """增强文件缺失时回退原音，且库里必须如实记 original，不得谎称增强。"""
        self.fakes.vocals_file.unlink()
        self.fakes.passing_attempts = {2}
        self.fakes.plan = ["vocals", "original"]
        result = dt.process_one(self.job, self._services())
        self.assertEqual(result["asr_attempts"], 2)
        self.assertEqual([path.name for path in self.fakes.asr_paths],
                         ["speech.m4a", "speech.m4a"])
        current = self.db.get_current_version(AWEME)
        attempts = self.db.list_attempts(current["version_id"])
        self.assertEqual([item["audio_variant"] for item in attempts],
                         ["original", "original"])
        self.assertIn("audio_variant_unavailable", result["degradations"])


if __name__ == "__main__":
    unittest.main()
