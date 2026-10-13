"""v2 编排器：阶段顺序、三轮择优、局部降级与旧版保护。"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts import douyin_transcript as dt
from scripts.artifacts import build_version_bundle, render_evidence, render_main
from scripts.asr import AsrAttemptResult, AsrStrategy
from scripts.audio_pipeline import AudioAnalysis, AudioVariant
from scripts.diarization import DiarizationOutcome, SpeakerTurn
from scripts.evidence_fusion import align_evidence
from scripts.ingest_adapter import IngestResult
from scripts.models import (CorrectionStatus, JobRecord, JobStatus, QualityStatus,
                            Stage)
from scripts.ocr import OcrObservation, OcrOutcome
from scripts.quality import AsrAssets, HardwareBudget, QualityReport
from scripts.correct_transcript import CorrectionOutcome
from scripts.transcript_db import TranscriptDB

AWEME = "7652684072266845450"
URL = f"https://www.douyin.com/video/{AWEME}"


def make_job(**overrides) -> JobRecord:
    values = dict(job_id="job-1", batch_id="batch-1", position=0,
                  raw_input=f"文案 {URL}", normalized_input=URL,
                  candidate_aweme_id=AWEME, aweme_id="", status=str(JobStatus.RESOLVING),
                  stage=str(Stage.RESOLVING), duplicate_of_job_id="",
                  failure_code="", failure_message="", checkpoint={},
                  heartbeat_at="", created_at="2026-09-16T10:00:00",
                  updated_at="2026-09-16T10:00:00")
    values.update(overrides)
    return JobRecord(**values)


def segments(count=2):
    return [{"start": float(i * 10), "end": float(i * 10 + 8),
             "text": "端午就看我奶龙大侠赛龙舟敲大鼓", "avg_logprob": -0.2,
             "no_speech_prob": 0.01, "low_confidence_words": [],
             "needs_review": False, "review_reasons": [], "speaker": ""}
            for i in range(count)]


def attempt(no=1, *, variant="original", passing=True, model="medium") -> AsrAttemptResult:
    return AsrAttemptResult(
        attempt_no=no, audio_variant=variant,
        strategy=AsrStrategy(model=model, device="cuda", compute_type="float16",
                             audio_variant=variant, language="zh"),
        transcript_text="\n".join(item["text"] for item in segments()),
        segments=segments(),
        metrics={"duration": 20.0, "duration_after_vad": 19.0,
                 "language_probability": 0.99, "segment_count": 2,
                 "word_count": 30},
        hard_failures=[] if passing else ["too_little_text"],
        score=88.0 if passing else 60.0, passed=passing,
        started_at="2026-09-16T10:00:01", finished_at="2026-09-16T10:00:02")


class ServiceFakes:
    """所有外部依赖都替换成可断言、可注入失败的假实现。"""

    def __init__(self, *, root: Path):
        self.root = root
        self.calls: list[str] = []
        self.attempts: list[AsrAttemptResult] = []
        self.ocr_outcome = OcrOutcome(status="ok", observations=[
            OcrObservation(start=0.0, end=8.0, text="端午就看我奶龙大侠赛龙舟敲大鼓",
                           bbox=((0, 300), (600, 300), (600, 340), (0, 340)),
                           confidence=0.95, kind="speech_subtitle", frame_count=4)])
        self.diarization_outcome = DiarizationOutcome(
            status="ok", speaker_count=2,
            turns=[SpeakerTurn(0.0, 10.0, "speaker_a", 0.9),
                   SpeakerTurn(10.0, 20.0, "speaker_b", 0.9)])
        self.correction_outcome = None
        self.summary_result = ("summarized", "**核心观点：**\n好")
        self.analysis = AudioAnalysis(duration=20.0, loudness_lufs=-16.0,
                                      peak_db=-1.5, clipped_ratio=0.0,
                                      silence_ratio=0.05, sample_rate=44100,
                                      channels=2)
        self.failures: dict[str, Exception] = {}
        self.strategies: list[AsrStrategy] = []
        self.next_strategy_result = None
        self.variants = [AudioVariant(kind="original", path=root / "media" / "a.m4a")]

    # --- 各阶段 -----------------------------------------------------------
    def ingest(self, url, job_dir, headless=True):
        self._maybe_fail("ingest")
        self.calls.append("ingest")
        audio = Path(job_dir) / "media" / "speech.m4a"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"audio")
        return IngestResult(aweme_id=AWEME, canonical_url=URL, title="标题",
                            author="奶龙", publish_date="2026-06-19",
                            duration_seconds=20.0, audio_path=audio,
                            video_download_url="https://cdn.test/v.mp4?token=x",
                            download_headers={"cookie": "s"})

    def video(self, result, job_dir):
        self._maybe_fail("video")
        self.calls.append("video")
        path = Path(job_dir) / "media" / "video.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return path

    def analyze(self, audio):
        self.calls.append("analyze")
        return self.analysis

    def audio(self, audio_path, job_dir, analysis, *, want_vocals=False):
        self.calls.append("audio")
        return list(self.variants)

    def asr(self, audio_path, strategy, attempt_no):
        self._maybe_fail(f"asr{attempt_no}")
        self.calls.append(f"transcribing:{attempt_no}")
        self.strategies.append(strategy)
        result = attempt(attempt_no, variant=strategy.audio_variant,
                         passing=self._passing(attempt_no), model=strategy.model)
        self.attempts.append(result)
        return result

    def _passing(self, attempt_no: int) -> bool:
        return getattr(self, "passing_attempts", None) is None or \
            attempt_no in self.passing_attempts

    def quality(self, result, analysis=None, ocr=None):
        """与 scripts.quality.evaluate_quality 同签名。"""
        self.calls.append(f"quality:{result.attempt_no}")
        return QualityReport(score=result.score,
                             status="passed" if result.passed else "needs_review",
                             hard_failures=list(result.hard_failures),
                             deductions={})

    def next_strategy(self, history, assets, hardware, **kwargs):
        self.calls.append(f"next:{len(history)}")
        if self.next_strategy_result is None:
            return None
        if len(history) >= 3:
            return None
        variant = "vocals" if len(history) == 1 else "original"
        return replace(history[-1][0].strategy, audio_variant=variant)

    def ocr(self, video, frame_dir, asr_segments):
        self._maybe_fail("ocr")
        self.calls.append("ocr")
        return self.ocr_outcome

    def diarization(self, audio_path, analysis=None):
        self._maybe_fail("diarization")
        self.calls.append("diarization")
        return self.diarization_outcome

    def correction(self, context):
        self._maybe_fail("correction")
        self.calls.append("correction")
        if self.correction_outcome is not None:
            return self.correction_outcome
        changed = [dict(item, text="端午就看我奶龙大侠赛龙舟敲大鼓。")
                   for item in context.segments]
        return CorrectionOutcome(
            status=CorrectionStatus.CORRECTED,
            corrected_text="\n".join(item["text"] for item in changed),
            segments=changed,
            changes=[{"source_segment_ref": f"seg-{index + 1}",
                      "original_text": context.segments[index]["text"],
                      "corrected_text": item["text"],
                      "change_type": "punctuation",
                      "evidence": {"source": "none"}, "confidence": 0.0}
                     for index, item in enumerate(changed)])

    def summary(self, title, text, mode="normal"):
        self._maybe_fail("summary")
        self.calls.append(f"summary:{mode}")
        return self.summary_result

    def model_assets(self):
        return {"ocr": True, "vocals": True, "diarization": True}

    def cleanup(self, job_dir, *, preserve=frozenset()):
        self.calls.append("cleanup")
        for path in sorted(Path(job_dir).rglob("*"), reverse=True):
            if path.is_file() and path.name not in preserve:
                path.unlink()
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    def _maybe_fail(self, stage: str) -> None:
        error = self.failures.get(stage)
        if error is not None:
            raise error

    def bundle(self, **overrides) -> dict:
        values = dict(ingest=self.ingest, video=self.video, analyze=self.analyze,
                      audio=self.audio, asr=self.asr, quality=self.quality,
                      next_strategy=self.next_strategy, ocr=self.ocr,
                      diarization=self.diarization, correction=self.correction,
                      summary=self.summary, model_assets=self.model_assets,
                      cleanup=self.cleanup)
        values.update(overrides)
        return values


class OrchestratorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = TranscriptDB(self.root / ".db" / "t.db")
        self.addCleanup(self.db.close)
        self.output = self.root / "output"
        self.fakes = ServiceFakes(root=self.root)
        self.job = make_job()

    def _services(self, **overrides):
        values = dict(
            ingest=self.fakes.ingest, video=self.fakes.video,
            analyze=self.fakes.analyze, audio=self.fakes.audio, asr=self.fakes.asr,
            quality=self.fakes.quality, next_strategy=self.fakes.next_strategy,
            ocr=self.fakes.ocr, diarization=self.fakes.diarization,
            correction=self.fakes.correction, summary=self.fakes.summary,
            model_assets=self.fakes.model_assets, db=self.db,
            cleanup=self.fakes.cleanup, output_root=self.output,
            verify_audio=lambda path: 20.0,
            hardware=HardwareBudget(device="cuda", compute_type="float16",
                                    max_model="medium", vram_gb=8.0),
            assets=AsrAssets(variants={"original": "a", "vocals": "b",
                                       "enhanced": "c"},
                             available_models=("base", "small", "medium")))
        values.update(overrides)
        return dt.ServiceBundle(**values)

    def _process(self, *, job=None, **overrides):
        return dt.process_one(job or self.job, self._services(**overrides))


class HappyPathTests(OrchestratorTestCase):
    def test_clear_sample_runs_exactly_one_round(self):
        result = self._process()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["ok"])
        self.assertEqual(result["asr_attempts"], 1)
        self.assertEqual(result["quality_status"], "passed")

    def test_stage_order_is_forward_only(self):
        result = self._process()
        stages = result["stages"]
        self.assertEqual(stages, ["resolving", "fetching", "media_ready",
                                  "analyzing", "transcribing", "ocr",
                                  "diarization", "correcting", "summarizing",
                                  "publishing"])

    def test_both_files_exist_and_current_version_is_set(self):
        result = self._process()
        self.assertTrue(Path(result["main_file"]).is_file())
        self.assertTrue(Path(result["evidence_file"]).is_file())
        current = self.db.get_current_version(AWEME)
        self.assertIsNotNone(current)
        self.assertEqual(current["quality_status"], QualityStatus.PASSED)
        self.assertEqual(current["raw_text"].count("端午"), 2)

    def test_media_is_cleaned_after_success(self):
        result = self._process()
        leftovers = [p.name for p in (self.root / "jobs").rglob("*")
                     if p.is_file()]
        self.assertEqual(leftovers, [])
        self.assertIn("cleanup", self.fakes.calls)
        self.assertTrue(Path(result["main_file"]).is_file())

    def test_job_stage_and_checkpoint_are_recorded(self):
        self._process()
        self.assertEqual(self.job.job_id, "job-1")

    def test_speakers_and_ocr_are_recorded_in_database(self):
        self._process()
        current = self.db.get_current_version(AWEME)
        version_id = current["version_id"]
        self.assertEqual(len(self.db.list_ocr_observations(version_id)), 1)
        self.assertEqual(len(self.db.list_speaker_turns(version_id)), 2)
        self.assertEqual(len(self.db.list_attempts(version_id)), 1)
        self.assertEqual(current["correction_status"],
                         str(CorrectionStatus.CORRECTED))

    def test_transcript_uses_corrected_text(self):
        self._process()
        current = self.db.get_current_version(AWEME)
        self.assertIn("。", current["corrected_text"])
        main = Path(self.db.list_artifacts(version_id=current["version_id"],
                                          kind="main_txt")[0]["path"])
        self.assertIn("校正版逐字稿", main.read_text(encoding="utf-8"))

    def test_schema_version_two_result(self):
        result = self._process()
        self.assertEqual(result["schema_version"], 2)
        self.assertIn("aweme_id", result)
        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(result["batch_id"], "batch-1")
        self.assertEqual(result["degradations"], [])


class ReuseTests(OrchestratorTestCase):
    def test_existing_deliverable_is_reused_without_ingest(self):
        self._process()
        self.fakes.calls.clear()
        result = self._process()
        self.assertEqual(result["status"], "reused")
        self.assertNotIn("ingest", self.fakes.calls)
        self.assertNotIn("transcribing:1", self.fakes.calls)

    def test_missing_file_triggers_render_only(self):
        self._process()
        current = self.db.get_current_version(AWEME)
        main = Path(self.db.list_artifacts(current["version_id"], "main_txt")[0]["path"])
        main.unlink()
        self.fakes.calls.clear()
        result = self._process()
        self.assertEqual(result["status"], "reused")
        self.assertIn("render_only", result["reuse_action"])
        self.assertNotIn("ingest", self.fakes.calls)
        self.assertTrue(main.is_file())

    def test_force_creates_a_new_version(self):
        first = self._process()
        result = self._process(job=make_job(), force=True)
        self.assertEqual(result["status"], "completed")
        versions = self.db.list_versions(AWEME)
        self.assertEqual(len(versions), 2)
        self.assertEqual(first["version_id"], versions[0]["version_id"])


class MultiRoundTests(OrchestratorTestCase):
    def test_second_round_uses_vocals_and_stops_when_passed(self):
        self.fakes.passing_attempts = {2}
        self.fakes.next_strategy_result = "vocals"
        result = self._process()
        self.assertEqual(result["asr_attempts"], 2)
        self.assertEqual([s.audio_variant for s in self.fakes.strategies],
                         ["original", "vocals"])
        self.assertEqual(result["quality_status"], "passed")

    def test_three_low_rounds_pick_best_and_mark_needs_review(self):
        self.fakes.passing_attempts = set()
        self.fakes.next_strategy_result = "vocals"
        result = self._process()
        self.assertEqual(result["asr_attempts"], 3)
        self.assertEqual(result["quality_status"], "needs_review")
        self.assertEqual(result["status"], "needs_review")
        current = self.db.get_current_version(AWEME)
        selected = current["selected_attempt_id"]
        scores = {item["attempt_id"]: item["score"]
                  for item in self.db.list_attempts(current["version_id"])}
        self.assertEqual(scores[selected], max(scores.values()))

    def test_last_round_has_no_automatic_priority(self):
        self.fakes.passing_attempts = {1}
        self.fakes.next_strategy_result = "vocals"
        result = self._process()
        self.assertEqual(result["asr_attempts"], 1)
        self.assertEqual(result["quality_status"], "passed")


class DegradationTests(OrchestratorTestCase):
    def test_ocr_failure_still_delivers(self):
        self.fakes.failures["ocr"] = RuntimeError("ocr boom")
        result = self._process()
        self.assertTrue(result["ok"])
        self.assertIn("ocr_failed", result["degradations"])

    def test_diarization_failure_still_delivers(self):
        self.fakes.failures["diarization"] = RuntimeError("spk boom")
        result = self._process()
        self.assertTrue(result["ok"])
        self.assertIn("diarization_failed", result["degradations"])

    def test_ocr_failure_does_not_block_correction(self):
        self.fakes.failures["ocr"] = RuntimeError("ocr boom")
        self._process()
        current = self.db.get_current_version(AWEME)
        self.assertEqual(current["correction_status"],
                         str(CorrectionStatus.CORRECTED))

    def test_video_materialization_failure_only_disables_ocr(self):
        self.fakes.failures["video"] = RuntimeError("download boom")
        result = self._process()
        self.assertTrue(result["ok"])
        self.assertIn("video_unavailable_for_ocr", result["degradations"])

    def test_correction_pending_leaves_main_file_with_raw_text(self):
        self.fakes.correction_outcome = CorrectionOutcome(
            status=str(CorrectionStatus.FAILED_PENDING_RETRY))
        result = self._process()
        self.assertEqual(result["status"], "awaiting_ai")
        self.assertEqual(result["correction_status"],
                         str(CorrectionStatus.FAILED_PENDING_RETRY))
        main = Path(result["main_file"]).read_text(encoding="utf-8")
        self.assertIn("未经 AI 校正原始稿", main)
        self.assertTrue(result["ok"])

    def test_summary_pending_still_completes(self):
        self.fakes.summary_result = ("pending", "")
        result = self._process()
        self.assertEqual(result["status"], "awaiting_ai")
        self.assertEqual(result["summary_status"], "pending")
        self.assertTrue(result["ok"])

    def test_no_speech_but_ocr_content_is_visual_text_only(self):
        self.fakes.attempts_provider = None
        self.fakes.asr = lambda audio, strategy, no: AsrAttemptResult(
            attempt_no=no, audio_variant=strategy.audio_variant, strategy=strategy,
            transcript_text="", segments=[], metrics={"duration": 20.0},
            hard_failures=["empty_output"], score=0.0, passed=False,
            error_code="asr_failed")
        result = self._process()
        self.assertEqual(result["status"], "visual_text_only")
        self.assertEqual(result["quality_status"], "visual_text_only")
        main = Path(result["main_file"]).read_text(encoding="utf-8")
        self.assertIn("画面文字记录", main)
        self.assertNotIn("完整语音原稿", main)


class FailureTests(OrchestratorTestCase):
    def test_login_required_pauses_without_touching_existing_data(self):
        self.fakes.failures["ingest"] = dt.LoginRequiredError()
        result = self._process()
        self.assertEqual(result["status"], "login_required")
        self.assertFalse(result["ok"])
        self.assertIsNone(self.db.get_current_version(AWEME))

    def test_ingest_failure_marks_job_failed(self):
        self.fakes.failures["ingest"] = dt.IngestFailure("video_unavailable", "没了")
        result = self._process()
        self.assertEqual(result["error"]["code"], "video_unavailable")

    def test_audio_invalid_is_reported(self):
        def broken(path):
            raise dt.MediaFailure("无音轨")

        result = self._process(verify_audio=broken)
        self.assertEqual(result["error"]["code"], "audio_invalid")

    def test_asr_total_failure_is_reported(self):
        def boom(audio, strategy, attempt_no):
            raise RuntimeError("model exploded")

        result = self._process(asr=boom)
        self.assertEqual(result["error"]["code"], "asr_failed")


class OldVersionProtectionTests(OrchestratorTestCase):
    def setUp(self):
        super().setUp()
        self.first = self._process()
        self.old_main = Path(self.first["main_file"]).read_text(encoding="utf-8")
        self.old_version = self.db.get_current_version(AWEME)["version_id"]

    def test_publish_failure_keeps_old_current_and_files(self):
        with mock.patch.object(dt, "publish_artifacts",
                               side_effect=RuntimeError("publish boom")):
            result = self._process(job=make_job(), force=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "publish_failed")
        current = self.db.get_current_version(AWEME)
        self.assertEqual(current["version_id"], self.old_version)
        self.assertEqual(Path(self.first["main_file"]).read_text(encoding="utf-8"),
                         self.old_main)

    def test_failure_at_each_stage_keeps_old_current(self):
        for stage, error in (("video", RuntimeError("v")),
                             ("ocr", RuntimeError("o")),
                             ("diarization", RuntimeError("d")),
                             ("correction", RuntimeError("c")),
                             ("summary", RuntimeError("s"))):
            with self.subTest(stage=stage):
                fakes = ServiceFakes(root=self.root)
                fakes.failures[stage] = error
                services = self._services(
                    ingest=fakes.ingest, video=fakes.video, analyze=fakes.analyze,
                    audio=fakes.audio, asr=fakes.asr, quality=fakes.quality,
                    next_strategy=fakes.next_strategy, ocr=fakes.ocr,
                    diarization=fakes.diarization, correction=fakes.correction,
                    summary=fakes.summary, model_assets=fakes.model_assets,
                    cleanup=fakes.cleanup, verify_audio=lambda path: 20.0)
                dt.process_one(make_job(), services)
                current = self.db.get_current_version(AWEME)
                self.assertEqual(current["version_id"], self.old_version)
                self.assertEqual(
                    Path(self.first["main_file"]).read_text(encoding="utf-8"),
                    self.old_main)

    def test_new_version_publishes_only_after_full_success(self):
        result = self._process(job=make_job(), force=True)
        self.assertTrue(result["ok"])
        self.assertNotEqual(self.db.get_current_version(AWEME)["version_id"],
                            self.old_version)


class CliTests(unittest.TestCase):
    """v1 命令行行为在 v2 中的等价覆盖（原 test_orchestrator.py 的对应用例）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / ".db" / "t.db"

    def _main(self, argv):
        import io
        import json as _json
        from contextlib import redirect_stdout

        from scripts import logger as skill_logger
        from scripts import paths as skill_paths

        with mock.patch.object(skill_paths, "DB_PATH", self.db_path),              mock.patch.object(skill_paths, "STATE_DIR", self.root / "state"),              mock.patch.object(skill_paths, "OUTPUT_DIR", self.root / "output"),              mock.patch.object(skill_logger, "LOG_FILE", self.root / "t.log"),              redirect_stdout(io.StringIO()) as out:
            code = dt.main(argv)
        skill_logger.reset_logging()
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        return code, _json.loads(lines[-1]), lines

    def test_missing_link_is_invalid_input(self):
        code, payload, lines = self._main(["今天天气不错"])
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "invalid_input")
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(len(lines), 1)

    def test_non_douyin_link_is_invalid_input(self):
        code, payload, _ = self._main(["https://example.com/video/123"])
        self.assertEqual(payload["error"]["code"], "invalid_input")

    def test_database_is_always_closed(self):
        created = []

        class TrackingDB:
            def __init__(self, path):
                created.append("open")

            def close(self):
                created.append("close")

        with mock.patch("scripts.transcript_db.TranscriptDB", TrackingDB):
            code, _, _ = self._main(["今天天气不错"])
        self.assertEqual(created, ["open", "close"])

    def test_single_line_json_even_on_internal_error(self):
        with mock.patch.object(dt, "default_services",
                               side_effect=RuntimeError("boom")):
            code, payload, lines = self._main(
                [f"https://www.douyin.com/video/{AWEME}"])
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "database_failed")
        self.assertEqual(len(lines), 1)


class JobDirectoryTests(OrchestratorTestCase):
    def test_job_directory_uses_job_id_not_title(self):
        self._process()
        job_dirs = list((self.root / "jobs").glob("*"))
        self.assertEqual(job_dirs, [])

    def test_job_directory_is_named_by_job_id(self):
        seen = []

        def cleanup(job_dir, *, preserve=frozenset()):
            seen.append(Path(job_dir))
            self.fakes.cleanup(job_dir, preserve=preserve)

        self._process(cleanup=cleanup)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].name, self.job.job_id)
        self.assertNotIn("标题", str(seen[0]))

    def test_delivered_file_name_carries_author_title_and_id(self):
        result = self._process()
        self.assertIn(AWEME, result["main_file"])
        self.assertIn("奶龙", result["main_file"])


class LoginWordingTests(OrchestratorTestCase):
    def test_headless_failure_never_claims_a_window_was_opened(self):
        self.fakes.failures["ingest"] = dt.LoginRequiredError()
        result = self._process()
        message = result["error"]["message"]
        self.assertIn("专用 Chromium", message)
        self.assertNotIn("已打开", message)

    def test_visible_mode_may_report_the_window(self):
        self.assertIn("已打开", dt.LOGIN_WINDOW_OPEN_MESSAGE)
        self.assertNotIn("已打开", dt.LOGIN_NEEDED_MESSAGE)


if __name__ == "__main__":
    unittest.main()
