"""双文件渲染：主文件 / 原始记录文件的内容、命名与纯 DB 重建。"""

import contextlib
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import artifacts
from scripts.artifacts import (build_version_bundle, render_evidence, render_main,
                              repair_artifacts, safe_filename, publish_artifacts)
from scripts.models import (ArtifactKind, CorrectionStatus, PreparedArtifact,
                            QualityStatus)
from scripts.transcript_db import TranscriptDB

AWEME = "7652684072266845450"
TITLE = "端午就看我奶龙大侠赛龙舟敲大鼓🐲 咸甜粽子都美味，吃不够吃不够~#奶龙 #轻漫计划"
SEGMENTS = [
    {"start": 2.0, "end": 4.0, "text": "五月五", "avg_logprob": -0.2,
     "no_speech_prob": 0.0, "low_confidence_words": [], "needs_review": False,
     "review_reasons": [], "speaker": ""},
    {"start": 56.0, "end": 58.0, "text": "还不够我塞眼红吗", "avg_logprob": -0.9,
     "no_speech_prob": 0.1, "low_confidence_words": ["眼红"],
     "needs_review": True, "review_reasons": ["low_avg_logprob"],
     "speaker": "speaker_a"},
]


class BundleTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = TranscriptDB(self.root / ".db" / "t.db")
        self.addCleanup(self.db.close)
        self.output = self.root / "output"
        self.version_id = self._seed_version()

    def _seed_version(self, *, quality=QualityStatus.PASSED, mode="normal",
                      correction=CorrectionStatus.CORRECTED,
                      summary_status="summarized") -> int:
        version_id = self.db.create_staging_version(
            AWEME, metadata={"canonical_url": f"https://www.douyin.com/video/{AWEME}",
                             "title": TITLE, "author": "奶龙",
                             "publish_date": "2026-06-19",
                             "duration_seconds": 70.0})
        self.db.add_asr_attempt(
            version_id, attempt_no=1, audio_variant="original",
            strategy={"vad_filter": True}, model="medium", device="cuda",
            compute_type="float16", language="zh", transcript_text="五月五",
            segments_json=SEGMENTS, metrics={"duration_after_vad": 66.0},
            hard_failures=[], score=88.0, passed=True)
        self.db.select_attempt(version_id, self.db.list_attempts(version_id)[0]["attempt_id"])
        self.db.set_quality(version_id, status=quality, score=88.0,
                            report={"deductions": {}})
        self.db.replace_ocr_observations(version_id, [
            {"start": 55.0, "end": 58.0, "text": "还不够我塞牙缝吗",
             "bbox": [[10, 300], [500, 300], [500, 340], [10, 340]],
             "confidence": 0.94, "kind": "speech_subtitle", "frame_hash": "ab"},
            {"start": 20.0, "end": 40.0, "text": "参数 2026-06-19 1.2 GB",
             "bbox": [[10, 150], [400, 150], [400, 175], [10, 175]],
             "confidence": 0.99, "kind": "static_info", "frame_hash": "cd"},
        ])
        self.db.replace_speaker_turns(version_id, [
            {"start": 0.0, "end": 30.0, "speaker_label": "speaker_a",
             "confidence": 0.9}])
        if correction != CorrectionStatus.PENDING:
            self.db.save_correction(version_id, corrected_text="五月五。\n还不够我塞牙缝吗？",
                                    changes=[{
                                        "source_segment_ref": "seg-2",
                                        "original_text": "还不够我塞眼红吗",
                                        "corrected_text": "还不够我塞牙缝吗？",
                                        "change_type": "word",
                                        "evidence": {"source": "ocr",
                                                     "subtitle": "还不够我塞牙缝吗"},
                                        "confidence": 0.95}],
                                    status=correction)
        self.db.save_summary(version_id, summary="**核心观点：**\n粽子好吃。\n\n"
                                                 "**主要论点：**\n1. 花样多。\n\n"
                                                 "**关键结论：**\n不够吃。",
                             status=summary_status, mode=mode,
                             meta={"model": "m", "mode": mode})
        return version_id

    def bundle(self, version_id=None) -> dict:
        return build_version_bundle(self.db, version_id or self.version_id)


class FilenameTests(unittest.TestCase):
    def test_illegal_characters_are_replaced(self):
        name = safe_filename('a/b\\c:d*e?f"g<h>i|j')
        for char in '/\\:*?"<>|':
            self.assertNotIn(char, name)

    def test_reserved_windows_names_are_avoided(self):
        self.assertNotEqual(safe_filename("CON", 10), "CON")
        self.assertNotEqual(safe_filename("nul", 10), "nul")

    def test_length_is_capped_for_windows(self):
        name = safe_filename("很长" * 100, 80)
        self.assertLessEqual(len(name), 80)

    def test_emoji_and_hashtags_survive(self):
        name = safe_filename(TITLE)
        self.assertIn("奶龙", name)
        self.assertIn("粽子", name)


class MainFileTests(BundleTestCase):
    def test_name_contains_author_title_and_id(self):
        bundle = self.bundle()
        artifact = render_main(bundle, self.output)
        self.assertEqual(artifact.kind, ArtifactKind.MAIN_TXT)
        main, evidence = artifacts.final_paths(bundle, self.output)
        self.assertTrue(main.name.endswith(".txt"))
        self.assertIn(AWEME, main.name)
        self.assertIn("奶龙", main.name)
        self.assertIn("_原始记录", evidence.name)
        # 渲染阶段只产出临时文件，命名与最终名保持一致
        self.assertTrue(Path(artifact.path).is_file())
        self.assertTrue(Path(artifact.path).name.startswith(main.name))

    def test_output_is_archived_by_publish_month(self):
        artifact = render_main(self.bundle(), self.output)
        self.assertIn(str(Path("2026") / "06"), str(Path(artifact.path).parent))

    def test_body_contains_metadata_summary_and_corrected_text(self):
        artifact = render_main(self.bundle(), self.output)
        body = Path(artifact.path).read_text(encoding="utf-8")
        self.assertIn(TITLE, body)
        self.assertIn(AWEME, body)
        self.assertIn("奶龙", body)
        self.assertIn("核心观点", body)
        self.assertIn("还不够我塞牙缝吗？", body)
        self.assertIn("[00:00:02 → 00:00:04]", body)
        self.assertIn("说话人 A", body)

    def test_quality_status_is_visible(self):
        artifact = render_main(self.bundle(), self.output)
        body = Path(artifact.path).read_text(encoding="utf-8")
        self.assertIn("passed", body)
        self.assertIn("88.0", body)

    def test_needs_review_is_marked_at_the_top(self):
        version_id = self._seed_version(quality=QualityStatus.NEEDS_REVIEW)
        artifact = render_main(self.bundle(version_id), self.output)
        body = Path(artifact.path).read_text(encoding="utf-8")
        head = body[:600]
        self.assertIn("需要复核", head)

    def test_correction_pending_shows_raw_and_placeholder(self):
        version_id = self._seed_version(correction=CorrectionStatus.PENDING)
        artifact = render_main(self.bundle(version_id), self.output)
        body = Path(artifact.path).read_text(encoding="utf-8")
        self.assertIn("未经 AI 校正", body)
        self.assertIn("校正版待补", body)

    def test_visual_only_never_claims_speech_transcript(self):
        version_id = self._seed_version(quality=QualityStatus.VISUAL_TEXT_ONLY,
                                        mode="visual_text_only")
        artifact = render_main(self.bundle(version_id), self.output)
        body = Path(artifact.path).read_text(encoding="utf-8")
        self.assertNotIn("完整语音原稿", body)
        self.assertNotIn("完整原文", body)
        self.assertIn("画面文字记录", body)

    def test_low_confidence_segments_are_marked(self):
        artifact = render_main(self.bundle(), self.output)
        body = Path(artifact.path).read_text(encoding="utf-8")
        self.assertIn("低置信", body)

    def test_artifact_records_size_and_hash(self):
        artifact = render_main(self.bundle(), self.output)
        path = Path(artifact.path)
        self.assertEqual(artifact.size_bytes, path.stat().st_size)
        self.assertEqual(artifact.sha256,
                         hashlib.sha256(path.read_bytes()).hexdigest())

    def test_rendering_never_touches_the_final_name(self):
        # 渲染阶段只写临时文件，最终文件名由发布阶段原子替换
        artifact = render_main(self.bundle(), self.output)
        self.assertTrue(Path(artifact.path).name.endswith(".tmp"))


class EvidenceFileTests(BundleTestCase):
    def test_name_is_suffixed_with_original_record(self):
        artifact = render_evidence(self.bundle(), self.output)
        self.assertEqual(artifact.kind, ArtifactKind.EVIDENCE_TXT)
        self.assertIn("_原始记录", Path(artifact.path).name)

    def test_body_contains_processing_evidence(self):
        artifact = render_evidence(self.bundle(), self.output)
        body = Path(artifact.path).read_text(encoding="utf-8")
        self.assertIn("选中轮次", body)
        self.assertIn("medium", body)
        self.assertIn("还不够我塞眼红吗", body)          # 原始段
        self.assertIn("speech_subtitle", body)            # OCR 分类
        self.assertIn("static_info", body)
        self.assertIn("还不够我塞牙缝吗", body)          # 重要修正
        self.assertIn("speaker_a", body)                  # 说话人
        self.assertIn("疑点", body)

    def test_evidence_lists_attempts(self):
        artifact = render_evidence(self.bundle(), self.output)
        body = Path(artifact.path).read_text(encoding="utf-8")
        self.assertIn("第 1 轮", body)
        self.assertIn("original", body)

    def test_degradations_are_listed(self):
        bundle = self.bundle()
        bundle["degradations"] = ["ocr_failed", "diarization_failed"]
        artifact = render_evidence(bundle, self.output)
        body = Path(artifact.path).read_text(encoding="utf-8")
        self.assertIn("ocr_failed", body)
        self.assertIn("降级", body)


class PublishArtifactsTests(BundleTestCase):
    def test_two_files_are_published_and_registered(self):
        main = render_main(self.bundle(), self.output)
        evidence = render_evidence(self.bundle(), self.output)
        published = publish_artifacts(self.db, self.version_id, [main, evidence])
        self.assertTrue(Path(published.main_file).is_file())
        self.assertTrue(Path(published.evidence_file).is_file())
        self.assertFalse(Path(published.main_file).name.endswith(".tmp"))
        current = self.db.get_current_version(AWEME)
        self.assertEqual(current["version_id"], self.version_id)
        for artifact in self.db.list_artifacts(self.version_id):
            path = Path(artifact["path"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             artifact["sha256"])

    def test_publish_reports_hash_and_size(self):
        main = render_main(self.bundle(), self.output)
        evidence = render_evidence(self.bundle(), self.output)
        published = publish_artifacts(self.db, self.version_id, [main, evidence])
        self.assertEqual(published.main_sha256, main.sha256)
        self.assertEqual(published.evidence_sha256, evidence.sha256)

    def test_no_temp_files_remain_after_publish(self):
        main = render_main(self.bundle(), self.output)
        evidence = render_evidence(self.bundle(), self.output)
        publish_artifacts(self.db, self.version_id, [main, evidence])
        leftovers = [p.name for p in self.output.rglob("*.tmp")]
        self.assertEqual(leftovers, [])


class RepairArtifactsTests(BundleTestCase):
    def _publish(self) -> tuple[Path, Path]:
        main = render_main(self.bundle(), self.output)
        evidence = render_evidence(self.bundle(), self.output)
        published = publish_artifacts(self.db, self.version_id, [main, evidence])
        return Path(published.main_file), Path(published.evidence_file)

    def test_missing_file_is_rebuilt_from_database_only(self):
        main, evidence = self._publish()
        main.unlink()
        stack, spies = self._spies()
        with stack:
            published = repair_artifacts(self.db, AWEME, output_root=self.output)
        self.assertTrue(Path(published.main_file).is_file())
        for spy in spies.values():
            spy.assert_not_called()

    def test_tampered_file_is_rebuilt(self):
        main, evidence = self._publish()
        main.write_text("被外部改坏", encoding="utf-8")
        stack, _ = self._spies()
        with stack:
            published = repair_artifacts(self.db, AWEME, output_root=self.output)
        self.assertIn("核心观点", Path(published.main_file).read_text(encoding="utf-8"))

    def test_repair_keeps_current_version_unchanged(self):
        self._publish()
        main, _ = self._publish()
        main.unlink()
        repair_artifacts(self.db, AWEME, output_root=self.output)
        self.assertEqual(self.db.get_current_version(AWEME)["version_id"],
                         self.version_id)

    def test_repair_returns_render_only_when_artifacts_are_fine(self):
        self._publish()
        published = repair_artifacts(self.db, AWEME, output_root=self.output)
        self.assertTrue(published.repaired)

    def test_render_manifest_creates_valid_contract(self):
        from scripts.artifacts import render_manifest
        main, evidence = self._publish()
        results = [{
            "ok": True,
            "aweme_id": AWEME,
            "main_file": str(main),
            "evidence_file": str(evidence),
            "title": "测试作品",
            "summary_status": "summarized",
        }]
        manifest_path = self.output / "source-manifest.json"
        out = render_manifest(results, manifest_path)
        self.assertIsNotNone(out)
        self.assertTrue(manifest_path.is_file())
        import json
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], "1.0.0")
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["source_id"], AWEME)
        self.assertEqual(data["items"][0]["platform"], "douyin")


    def _spies(self):
        """重建只能读数据库：上游采集、ASR、媒体下载、增强都不允许被调用。"""
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        spies = {
            "ingest": stack.enter_context(mock.patch(
                "scripts.ingest_runner.run_ingest",
                side_effect=AssertionError("不得重新采集"))),
            "asr": stack.enter_context(mock.patch(
                "scripts.asr.run_attempt",
                side_effect=AssertionError("不得重新识别"))),
            "video": stack.enter_context(mock.patch(
                "scripts.video_media.materialize_video",
                side_effect=AssertionError("不得重新下载"))),
            "audio": stack.enter_context(mock.patch(
                "scripts.audio_pipeline.analyze_audio",
                side_effect=AssertionError("不得重新分析"))),
        }
        return stack, spies


class ManifestStatusTests(BundleTestCase):
    """DY-05：source-manifest 如实反映阶段状态与摘要标记。

    半成品（needs_review / awaiting_ai）不许虚报 completed；has_summary 以交付
    文件中的摘要节为准（占位节一律判 False），调用方断言只在文件不可读时兜底。
    """

    def _publish_version(self, output_dir, **seed_kwargs):
        from scripts.artifacts import publish_artifacts, render_evidence, render_main
        version_id = self._seed_version(**seed_kwargs)
        bundle = self.bundle(version_id)
        main = render_main(bundle, output_dir)
        evidence = render_evidence(bundle, output_dir)
        published = publish_artifacts(self.db, version_id, [main, evidence],
                                      output_root=output_dir)
        return Path(published.main_file), Path(published.evidence_file)

    def _manifest_items(self, results):
        from scripts.artifacts import render_manifest
        manifest_path = self.root / "mf" / "source-manifest.json"
        out = render_manifest(results, manifest_path)
        self.assertIsNotNone(out)
        import json
        return json.loads(manifest_path.read_text(encoding="utf-8"))["items"]

    def _entry(self, main, evidence, **overrides):
        entry = {"ok": True, "status": "completed", "aweme_id": AWEME,
                 "title": "测试作品", "main_file": str(main),
                 "evidence_file": str(evidence), "quality_status": "passed"}
        entry.update(overrides)
        return entry

    def test_awaiting_ai_entry_is_not_completed(self):
        main, evidence = self._publish_version(self.output)
        (item,) = self._manifest_items(
            [self._entry(main, evidence, status="awaiting_ai")])
        self.assertEqual(item["status"], "partial")
        self.assertNotEqual(item["status"], "completed")

    def test_needs_review_entry_is_not_completed(self):
        main, evidence = self._publish_version(self.output)
        (item,) = self._manifest_items(
            [self._entry(main, evidence, status="needs_review")])
        self.assertEqual(item["status"], "partial")

    def test_completed_entry_stays_completed(self):
        main, evidence = self._publish_version(self.output)
        (item,) = self._manifest_items([self._entry(main, evidence)])
        self.assertEqual(item["status"], "completed")

    def test_reused_passed_entry_stays_completed(self):
        main, evidence = self._publish_version(self.output)
        (item,) = self._manifest_items(
            [self._entry(main, evidence, status="reused",
                          quality_status="passed")])
        self.assertEqual(item["status"], "completed")

    def test_reused_needs_review_entry_is_partial(self):
        main, evidence = self._publish_version(self.output)
        (item,) = self._manifest_items(
            [self._entry(main, evidence, status="reused",
                          quality_status="needs_review")])
        self.assertEqual(item["status"], "partial")

    def test_real_summary_file_reports_has_summary_true(self):
        main, evidence = self._publish_version(self.output)
        body = main.read_text(encoding="utf-8")
        self.assertIn("核心观点", body)
        self.assertNotIn("（摘要待补", body)
        (item,) = self._manifest_items([self._entry(main, evidence)])
        self.assertTrue(item["has_summary"])

    def test_placeholder_summary_file_reports_has_summary_false(self):
        out2 = self.root / "output2"
        main, evidence = self._publish_version(out2, summary_status="pending")
        self.assertIn("（摘要待补", main.read_text(encoding="utf-8"))
        (item,) = self._manifest_items([self._entry(main, evidence)])
        self.assertFalse(item["has_summary"])

    def test_stale_summarized_claim_loses_to_placeholder_file(self):
        out2 = self.root / "output2"
        main, evidence = self._publish_version(out2, summary_status="pending")
        (item,) = self._manifest_items(
            [self._entry(main, evidence, summary_status="summarized")])
        self.assertFalse(item["has_summary"])


class BundleTests(BundleTestCase):
    def test_bundle_contains_every_traceable_piece(self):
        bundle = build_version_bundle(self.db, self.version_id)
        for key in ("aweme_id", "title", "author", "segments", "attempts",
                    "ocr_observations", "speaker_turns", "correction_changes",
                    "quality_status", "summary", "version_id"):
            self.assertIn(key, bundle)

    def test_bundle_uses_selected_attempt_segments(self):
        bundle = build_version_bundle(self.db, self.version_id)
        self.assertEqual(len(bundle["segments"]), 2)
        self.assertEqual(bundle["selected_attempt"]["attempt_no"], 1)

    def test_bundle_marks_current_and_no_files_before_publish(self):
        bundle = build_version_bundle(self.db, self.version_id)
        self.assertFalse(bundle["is_current"])

    def test_missing_version_raises(self):
        with self.assertRaises(LookupError):
            build_version_bundle(self.db, 999999)


if __name__ == "__main__":
    unittest.main()



if __name__ == "__main__":
    unittest.main()
