"""v2 repository 与事务式发布测试：文件/DB 必须原子一致，旧版本永不被降级。"""

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import migrations
from scripts.models import (ArtifactKind, CorrectionStatus, LifecycleStatus,
                            PreparedArtifact, QualityStatus, ReuseAction)
from scripts.transcript_db import PublishError, TranscriptDB

AWEME = "7410000000000001234"


class RepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = TranscriptDB(self.root / ".db" / "transcripts.db")
        self.addCleanup(self.db.close)

    def make_artifact(self, kind: str, name: str, body: str) -> PreparedArtifact:
        path = self.root / "output" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        raw = path.read_bytes()
        return PreparedArtifact(kind=kind, path=str(path),
                                sha256=hashlib.sha256(raw).hexdigest(),
                                size_bytes=len(raw))

    def mark_transcribed(self, version_id: int, *, raw_text: str = "原稿",
                         status: str = QualityStatus.PASSED,
                         score: float = 86.4) -> None:
        self.db.set_raw_text(version_id, raw_text)
        self.db.set_quality(version_id, status=status, score=score, report={})

    def mark_delivered(self, version_id: int) -> None:
        """把校正与摘要都补齐，视为一个完整交付的 v2 版本。"""
        self.db.save_correction(version_id, corrected_text="校正稿", changes=[],
                                status=CorrectionStatus.CORRECTED)
        self.db.save_summary(version_id, summary="摘要", status="summarized",
                             mode="normal", meta={})

    def publish_new_version(self, *, tag: str = "v2",
                            main: str = "主文件内容",
                            evidence: str = "证据文件内容") -> int:
        version_id = self.db.create_staging_version(
            AWEME, metadata={"canonical_url": f"https://www.douyin.com/video/{AWEME}",
                             "title": "标题", "author": "作者",
                             "publish_date": "2026-09-16",
                             "duration_seconds": 42.0})
        self.mark_transcribed(version_id, raw_text=f"原稿 {tag}")
        self.mark_delivered(version_id)
        self.db.publish_version(version_id, [
            self.make_artifact(ArtifactKind.MAIN_TXT, f"主_{tag}.txt", main),
            self.make_artifact(ArtifactKind.EVIDENCE_TXT, f"证_{tag}.txt", evidence),
        ])
        return version_id


class StagingVersionTests(RepositoryTestCase):
    def test_create_staging_version_upserts_video_and_numbers_versions(self):
        first = self.db.create_staging_version(
            AWEME, metadata={"title": "标题一", "author": "作者"})
        second = self.db.create_staging_version(
            AWEME, metadata={"title": "标题二", "author": "作者"})
        self.assertNotEqual(first, second)
        versions = self.db.list_versions(AWEME)
        self.assertEqual([v["version_no"] for v in versions], [1, 2])
        self.assertTrue(all(v["lifecycle_status"] == LifecycleStatus.STAGING
                            for v in versions))
        self.assertTrue(all(v["is_current"] == 0 for v in versions))
        video = self.db.get_video(AWEME)
        self.assertEqual(video["title"], "标题二")
        self.assertIsNone(video["current_version_id"])

    def test_staging_version_does_not_become_current(self):
        self.db.create_staging_version(AWEME, metadata={"title": "标题"})
        self.assertIsNone(self.db.get_current_version(AWEME))

    def test_json_columns_round_trip_as_chinese(self):
        version_id = self.db.create_staging_version(AWEME, metadata={"title": "标题"})
        self.db.set_quality(version_id, status=QualityStatus.NEEDS_REVIEW,
                            score=62.5, report={"reason": "背景音乐过强"})
        row = self.db.get_version(version_id)
        self.assertIn("背景音乐过强", row["quality_report_json"])
        self.assertNotIn("\\u", row["quality_report_json"])


class AttemptRepositoryTests(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.version_id = self.db.create_staging_version(
            AWEME, metadata={"title": "标题"})

    def test_attempt_round_trip_and_selection(self):
        first = self.db.add_asr_attempt(
            self.version_id, attempt_no=1, audio_variant="original",
            strategy={"vad_filter": True, "beam_size": 5}, model="medium",
            device="cuda", compute_type="float16", language="zh",
            transcript_text="第一轮文本", segments_json=[{"start": 0.0, "end": 1.0,
                                                    "text": "第一轮文本"}],
            metrics={"avg_logprob": -0.3}, hard_failures=[],
            score=58.0, passed=False)
        second = self.db.add_asr_attempt(
            self.version_id, attempt_no=2, audio_variant="vocals",
            strategy={"condition_on_previous_text": False}, model="medium",
            device="cuda", compute_type="float16", language="zh",
            transcript_text="第二轮文本", segments_json=[],
            metrics={"avg_logprob": -0.12}, hard_failures=[], score=88.0,
            passed=True)
        attempts = self.db.list_attempts(self.version_id)
        self.assertEqual([a["attempt_no"] for a in attempts], [1, 2])
        self.assertEqual(attempts[1]["strategy_json"],
                         '{"condition_on_previous_text": false}')
        self.assertEqual(attempts[1]["audio_variant"], "vocals")

        self.db.select_attempt(self.version_id, second)
        version = self.db.get_version(self.version_id)
        self.assertEqual(version["selected_attempt_id"], second)
        self.assertEqual(version["raw_text"], "第二轮文本")
        self.assertNotEqual(first, second)

    def test_select_attempt_rejects_foreign_attempt(self):
        other_version = self.db.create_staging_version(
            "7410000000000009999", metadata={"title": "别的"})
        foreign = self.db.add_asr_attempt(
            other_version, attempt_no=1, audio_variant="original", strategy={},
            model="base", device="cpu", compute_type="int8", language="zh",
            transcript_text="别的文本", segments_json=[], metrics={},
            hard_failures=[], score=80.0, passed=True)
        with self.assertRaises(ValueError):
            self.db.select_attempt(self.version_id, foreign)

    def test_hard_failure_attempt_records_error_code(self):
        attempt_id = self.db.add_asr_attempt(
            self.version_id, attempt_no=1, audio_variant="original", strategy={},
            model="medium", device="cuda", compute_type="float16", language="zh",
            transcript_text="", segments_json=[], metrics={},
            hard_failures=["empty_output"], score=0.0, passed=False,
            error_code="asr_failed")
        attempt = self.db.get_attempt(attempt_id)
        self.assertEqual(attempt["error_code"], "asr_failed")
        self.assertEqual(attempt["hard_failures_json"], '["empty_output"]')


class EvidenceRepositoryTests(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.version_id = self.db.create_staging_version(
            AWEME, metadata={"title": "标题"})

    def test_ocr_observations_are_replaced_not_appended(self):
        self.db.replace_ocr_observations(self.version_id, [
            {"start": 0.0, "end": 2.0, "text": "端午", "bbox": [[1, 2], [3, 4]],
             "confidence": 0.93, "kind": "speech_subtitle", "frame_hash": "abc"},
        ])
        self.db.replace_ocr_observations(self.version_id, [
            {"start": 5.0, "end": 7.0, "text": "粽子", "bbox": [[1, 2]],
             "confidence": 0.88, "kind": "static_info", "frame_hash": "def"},
        ])
        rows = self.db.list_ocr_observations(self.version_id)
        self.assertEqual([r["text"] for r in rows], ["粽子"])
        self.assertEqual(rows[0]["bbox_json"], "[[1, 2]]")
        self.assertAlmostEqual(rows[0]["confidence"], 0.88)
        self.assertEqual(rows[0]["kind"], "static_info")

    def test_speaker_turns_round_trip(self):
        self.db.replace_speaker_turns(self.version_id, [
            {"start": 0.0, "end": 3.0, "speaker_label": "speaker_a",
             "confidence": 0.9},
            {"start": 3.0, "end": 6.0, "speaker_label": "speaker_b",
             "confidence": 0.7},
        ])
        rows = self.db.list_speaker_turns(self.version_id)
        self.assertEqual([r["speaker_label"] for r in rows],
                         ["speaker_a", "speaker_b"])

    def test_save_correction_records_changes_and_status(self):
        self.db.save_correction(
            self.version_id, corrected_text="五月五，过端午。",
            changes=[{"source_segment_ref": "seg-1", "original_text": "五月五果端午",
                      "corrected_text": "五月五，过端午", "change_type": "punctuation",
                      "evidence": {"source": "ocr", "confidence": 0.91},
                      "confidence": 0.91}],
            status=CorrectionStatus.CORRECTED)
        version = self.db.get_version(self.version_id)
        self.assertEqual(version["corrected_text"], "五月五，过端午。")
        self.assertEqual(version["correction_status"], "corrected")
        changes = self.db.list_correction_changes(self.version_id)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["change_type"], "punctuation")
        self.assertIn("ocr", changes[0]["evidence_json"])

    def test_save_summary_keeps_traceability_metadata(self):
        self.db.save_summary(self.version_id, summary="三段式摘要",
                             status="summarized", mode="restricted",
                             meta={"model": "gemini-3.8-flash-medium",
                                   "prompt_version": "v2-restricted",
                                   "source_version_id": self.version_id})
        version = self.db.get_version(self.version_id)
        self.assertEqual(version["summary"], "三段式摘要")
        self.assertEqual(version["summary_mode"], "restricted")
        self.assertIn("gemini-3.8-flash-medium", version["summary_meta_json"])
        self.assertIn("v2-restricted", version["summary_meta_json"])


class PublishTransactionTests(RepositoryTestCase):
    def test_publish_makes_version_current_and_registers_artifacts(self):
        version_id = self.publish_new_version()
        version = self.db.get_version(version_id)
        self.assertEqual(version["is_current"], 1)
        self.assertEqual(version["lifecycle_status"], LifecycleStatus.PUBLISHED)
        self.assertIsNotNone(version["published_at"])
        current = self.db.get_current_version(AWEME)
        self.assertEqual(current["version_id"], version_id)
        self.assertEqual(self.db.get_video(AWEME)["current_version_id"], version_id)
        artifacts = self.db.list_artifacts(version_id)
        self.assertEqual({a["kind"] for a in artifacts},
                         {ArtifactKind.MAIN_TXT, ArtifactKind.EVIDENCE_TXT})
        for artifact in artifacts:
            self.assertTrue(Path(artifact["path"]).is_file())
            self.assertTrue(artifact["is_current"])
            self.assertEqual(artifact["sha256"],
                             migrations.file_sha256(Path(artifact["path"])))

    def test_publish_verification_failure_leaves_old_current_untouched(self):
        old_id = self.publish_new_version(tag="old", main="旧主文件",
                                          evidence="旧证据文件")
        old_main = self.db.list_artifacts(old_id, kind=ArtifactKind.MAIN_TXT)[0]
        old_body = Path(old_main["path"]).read_text(encoding="utf-8")

        new_id = self.db.create_staging_version(
            AWEME, metadata={"title": "新标题", "author": "作者"})
        self.mark_transcribed(new_id, raw_text="新原稿", score=90.0)
        prepared = [self.make_artifact(ArtifactKind.MAIN_TXT, "新主.txt", "新主文件"),
                    self.make_artifact(ArtifactKind.EVIDENCE_TXT, "新证.txt", "新证据文件")]

        def broken_verifier(artifact):
            raise PublishError("校验失败")

        with self.assertRaises(PublishError):
            self.db.publish_version(new_id, prepared, verifier=broken_verifier)

        self.assertEqual(self.db.get_current_version(AWEME)["version_id"], old_id)
        self.assertEqual(self.db.get_version(new_id)["is_current"], 0)
        self.assertEqual(self.db.get_version(new_id)["lifecycle_status"],
                         LifecycleStatus.STAGING)
        self.assertEqual(self.db.get_video(AWEME)["current_version_id"], old_id)
        self.assertEqual(
            Path(self.db.list_artifacts(old_id, kind=ArtifactKind.MAIN_TXT)[0]["path"]
                 ).read_text(encoding="utf-8"), old_body)
        self.assertEqual(self.db.list_artifacts(new_id), [])

    def test_publish_rejects_tampered_artifact(self):
        version_id = self.db.create_staging_version(AWEME, metadata={"title": "标题"})
        prepared = [self.make_artifact(ArtifactKind.MAIN_TXT, "主.txt", "内容"),
                    self.make_artifact(ArtifactKind.EVIDENCE_TXT, "证.txt", "内容")]
        Path(prepared[0].path).write_text("被篡改", encoding="utf-8")
        with self.assertRaises(PublishError):
            self.db.publish_version(version_id, prepared)
        self.assertEqual(self.db.list_artifacts(version_id), [])

    def test_publish_requires_both_artifacts(self):
        version_id = self.db.create_staging_version(AWEME, metadata={"title": "标题"})
        prepared = [self.make_artifact(ArtifactKind.MAIN_TXT, "主.txt", "内容")]
        with self.assertRaises(PublishError):
            self.db.publish_version(version_id, prepared)

    def test_second_publish_clears_previous_current(self):
        old_id = self.publish_new_version(tag="old")
        new_id = self.publish_new_version(tag="new")
        self.assertEqual(self.db.get_version(old_id)["is_current"], 0)
        self.assertEqual(self.db.get_version(new_id)["is_current"], 1)
        current_count = self.db.connect().execute(
            "SELECT count(*) FROM transcript_versions WHERE aweme_id = ? "
            "AND is_current = 1", (AWEME,)).fetchone()[0]
        self.assertEqual(current_count, 1)
        self.assertTrue(all(not a["is_current"]
                            for a in self.db.list_artifacts(old_id)))
        self.assertTrue(all(a["is_current"]
                            for a in self.db.list_artifacts(new_id)))

    def test_publish_rejects_unknown_version(self):
        prepared = [self.make_artifact(ArtifactKind.MAIN_TXT, "主.txt", "内容"),
                    self.make_artifact(ArtifactKind.EVIDENCE_TXT, "证.txt", "内容")]
        with self.assertRaises(PublishError):
            self.db.publish_version(999999, prepared)

    def test_failed_publish_leaves_no_partial_artifacts(self):
        version_id = self.db.create_staging_version(AWEME, metadata={"title": "标题"})
        prepared = [self.make_artifact(ArtifactKind.MAIN_TXT, "主.txt", "内容"),
                    self.make_artifact(ArtifactKind.EVIDENCE_TXT, "证.txt", "内容")]
        self.db.connect().execute(
            "CREATE TRIGGER block_publish BEFORE INSERT ON artifacts "
            "BEGIN SELECT RAISE(ABORT, '注入失败'); END")
        self.db.connect().commit()
        with self.assertRaises(sqlite3.Error):
            self.db.publish_version(version_id, prepared)
        self.assertEqual(self.db.list_artifacts(version_id), [])
        self.assertEqual(self.db.get_version(version_id)["is_current"], 0)


class ReuseActionTests(RepositoryTestCase):
    def test_no_version_means_fetch(self):
        self.assertEqual(self.db.find_reuse_action(AWEME), ReuseAction.FETCH)

    def test_staging_only_version_is_not_reusable(self):
        self.db.create_staging_version(AWEME, metadata={"title": "标题"})
        self.assertEqual(self.db.find_reuse_action(AWEME), ReuseAction.FETCH)

    def test_published_passed_version_is_reused(self):
        self.publish_new_version()
        self.assertEqual(self.db.find_reuse_action(AWEME), ReuseAction.REUSE)

    def test_missing_file_only_rerenders(self):
        version_id = self.publish_new_version()
        main = self.db.list_artifacts(version_id, kind=ArtifactKind.MAIN_TXT)[0]
        Path(main["path"]).unlink()
        self.assertEqual(self.db.find_reuse_action(AWEME), ReuseAction.RENDER_ONLY)

    def test_hash_mismatch_only_rerenders(self):
        version_id = self.publish_new_version()
        main = self.db.list_artifacts(version_id, kind=ArtifactKind.MAIN_TXT)[0]
        Path(main["path"]).write_text("外部改动", encoding="utf-8")
        self.assertEqual(self.db.find_reuse_action(AWEME), ReuseAction.RENDER_ONLY)

    def test_missing_evidence_file_only_rerenders(self):
        version_id = self.publish_new_version()
        evidence = self.db.list_artifacts(version_id,
                                          kind=ArtifactKind.EVIDENCE_TXT)[0]
        Path(evidence["path"]).unlink()
        self.assertEqual(self.db.find_reuse_action(AWEME), ReuseAction.RENDER_ONLY)

    def test_awaiting_ai_only_fills_ai(self):
        version_id = self.publish_new_version()
        self.db.save_summary(version_id, summary="", status="pending",
                             mode="normal", meta={})
        self.assertEqual(self.db.find_reuse_action(AWEME), ReuseAction.FILL_AI)

    def test_pending_correction_only_fills_ai(self):
        version_id = self.publish_new_version()
        self.db.save_correction(version_id, corrected_text="", changes=[],
                                status=CorrectionStatus.FAILED_PENDING_RETRY)
        self.assertEqual(self.db.find_reuse_action(AWEME), ReuseAction.FILL_AI)

    def test_review_version_reprocessed_when_pipeline_advanced(self):
        version_id = self.db.create_staging_version(
            AWEME, metadata={"title": "标题"}, processing_signature="v1.5")
        self.mark_transcribed(version_id, status=QualityStatus.NEEDS_REVIEW,
                              score=60.0)
        self.mark_delivered(version_id)
        self.db.publish_version(version_id, [
            self.make_artifact(ArtifactKind.MAIN_TXT, "主.txt", "主文件"),
            self.make_artifact(ArtifactKind.EVIDENCE_TXT, "证.txt", "证据"),
        ])
        self.assertEqual(self.db.find_reuse_action(AWEME), ReuseAction.REUSE)
        self.assertEqual(
            self.db.find_reuse_action(AWEME, reprocess_review=True),
            ReuseAction.REPROCESS)

    def test_review_version_with_current_signature_is_reused(self):
        version_id = self.publish_new_version()
        self.db.set_quality(version_id, status=QualityStatus.NEEDS_REVIEW,
                            score=60.0, report={})
        self.assertEqual(
            self.db.find_reuse_action(AWEME, reprocess_review=True),
            ReuseAction.REUSE)


class LegacyMigrationReuseTests(unittest.TestCase):
    """迁移后的 v1 记录必须可读、可重渲染，绝不因缺证据文件而被判失败。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "transcripts.db"
        self.txt = self.root / "output" / "旧标题_7410000000000001234.txt"
        self.txt.parent.mkdir(parents=True, exist_ok=True)
        self.txt.write_text("旧 TXT 内容", encoding="utf-8")
        from tests.test_migrations import make_v1_database

        conn = make_v1_database(self.db_path, transcript_file=self.txt)
        migrations.migrate_to_v2(conn)
        conn.close()
        self.db = TranscriptDB(self.db_path)
        self.addCleanup(self.db.close)

    def test_legacy_record_is_kept_and_current(self):
        current = self.db.get_current_version(AWEME)
        self.assertIsNotNone(current)
        self.assertEqual(current["raw_text"], "端午就看我奶龙大侠赛龙舟")
        self.assertEqual(current["quality_status"], QualityStatus.NEEDS_REVIEW)
        self.assertEqual(current["processing_signature"], "legacy-v1")

    def test_legacy_record_without_evidence_file_only_rerenders(self):
        self.assertEqual(self.db.find_reuse_action(AWEME), ReuseAction.RENDER_ONLY)

    def test_legacy_record_with_both_files_reuses(self):
        version_id = self.db.get_current_version(AWEME)["version_id"]
        # 双文件齐备后必须直接复用，不再追问 AI 校正
        self.db.publish_version(version_id, [
            self._prepared(ArtifactKind.MAIN_TXT, "主.txt"),
            self._prepared(ArtifactKind.EVIDENCE_TXT, "证.txt"),
        ])
        self.assertEqual(self.db.find_reuse_action(AWEME), ReuseAction.REUSE)

    def _prepared(self, kind: str, name: str) -> PreparedArtifact:
        path = self.root / "output" / name
        path.write_text(f"{kind} 内容", encoding="utf-8")
        raw = path.read_bytes()
        return PreparedArtifact(kind=kind, path=str(path),
                                sha256=hashlib.sha256(raw).hexdigest(),
                                size_bytes=len(raw))


if __name__ == "__main__":
    unittest.main()
