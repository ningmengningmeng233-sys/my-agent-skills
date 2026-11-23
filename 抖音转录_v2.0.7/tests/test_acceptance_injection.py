"""Task 14 破坏性注入验收：每个阶段注入失败，逐项核对旧版本保护与降级语义。

分两类预期：
- 致命阶段（采集、识别异常、发布）失败 → 旧 current 与两个旧文件必须原样保留；
- 增强阶段（视频素材、OCR、校正、摘要）失败 → 局部降级并照常发布新版本，
  旧版本行仍保留，媒体照常清理。
"""

import pathlib
import shutil
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from scripts import douyin_transcript as dt
from scripts.models import JobStatus
from scripts.transcript_db import TranscriptDB
from tests.test_end_to_end_v2 import AWEME, EndToEndV2Tests


class InjectionAcceptanceTests(EndToEndV2Tests):
    """每个子测试都先建立基线交付，再注入失败，避免相互污染。"""

    def setUp(self):
        super().setUp()
        self._process()          # 基线：真实渲染 + 真实发布
        self.baseline = self._snapshot()
        self.baseline_versions = len(self.db.list_versions(AWEME))

    def _snapshot(self) -> tuple:
        current = self.db.get_current_version(AWEME)
        main = self.db.list_artifacts(current["version_id"], "main_txt")[0]["path"]
        evidence = self.db.list_artifacts(current["version_id"],
                                          "evidence_txt")[0]["path"]
        return (int(current["version_id"]), pathlib.Path(main).read_bytes(),
                pathlib.Path(evidence).read_bytes(), main, evidence)

    def _inject(self, stage: str, exc: Exception) -> dict:
        services = self._services()
        targets = {"ingest": "ingest", "download": "video", "ocr": "ocr",
                   "correction": "correction", "summary": "summary"}
        if stage in targets:
            def broken(*args, **kwargs):
                raise exc
            setattr(services, targets[stage], broken)
        elif stage.startswith("asr"):
            round_no = int(stage[3:])
            original = services.asr

            def broken_asr(audio, strategy, no, _original=original):
                if no == round_no:
                    raise exc
                return _original(audio, strategy, no)
            services.asr = broken_asr
        else:  # publish
            patcher = mock.patch.object(dt, "publish_artifacts", side_effect=exc)
            patcher.start()
            self.addCleanup(patcher.stop)
        return dt.process_one(self._job(), replace(services, force=True))

    def _assert_old_version_intact(self) -> None:
        version_id, main_raw, evidence_raw, main, evidence = self.baseline
        current = self.db.get_current_version(AWEME)
        self.assertEqual(int(current["version_id"]), version_id)
        self.assertEqual(pathlib.Path(main).read_bytes(), main_raw)
        self.assertEqual(pathlib.Path(evidence).read_bytes(), evidence_raw)
        self.assertEqual(self._leftover_media(), [])
        # 版本行只增不减（采集阶段失败时甚至不会新建 staging 版本）
        self.assertGreaterEqual(len(self.db.list_versions(AWEME)),
                                self.baseline_versions)

    def _assert_degraded_but_published(self, result: dict, reason: str) -> None:
        """局部降级时照样发布新版本；旧版本的文字证据仍完整保留在 SQLite。"""
        self.assertTrue(result["ok"], f"{reason} 不应导致整条失败")
        self.assertIn(reason, result["degradations"])
        version_id, _main_raw, _evidence_raw, _main, _evidence = self.baseline
        current = self.db.get_current_version(AWEME)
        self.assertNotEqual(int(current["version_id"]), version_id)
        previous = self.db.get_version(version_id)
        self.assertIsNotNone(previous)
        self.assertEqual(str(previous["aweme_id"]), AWEME)
        self.assertTrue(str(previous["raw_text"]).strip())
        # 输出目录只展示当前版：文件名相同但内容已换成新版本
        self.assertTrue(pathlib.Path(current["main_file"]).is_file())
        self.assertEqual(self._leftover_media(), [])


class FatalityTests(InjectionAcceptanceTests):
    def test_ingest_failure_protects_old_delivery(self):
        result = self._inject("ingest", RuntimeError("采集注入失败"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "ingest_contract_changed")
        self._assert_old_version_intact()

    def test_first_round_asr_exception_protects_old_delivery(self):
        result = self._inject("asr1", RuntimeError("识别注入失败"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "asr_failed")
        self._assert_old_version_intact()

    def test_publish_failure_protects_old_delivery(self):
        result = self._inject("publish", RuntimeError("发布注入失败"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "publish_failed")
        self._assert_old_version_intact()

    def test_login_required_protects_old_delivery(self):
        result = self._inject("ingest", dt.LoginRequiredError())
        self.assertEqual(result["status"], "login_required")
        self._assert_old_version_intact()


class DegradationTests(InjectionAcceptanceTests):
    def test_video_materialization_failure_degrades_ocr_only(self):
        result = self._inject("download", RuntimeError("下载注入失败"))
        self._assert_degraded_but_published(result, "video_unavailable_for_ocr")

    def test_ocr_failure_degrades_only_that_capability(self):
        result = self._inject("ocr", RuntimeError("OCR 注入失败"))
        self._assert_degraded_but_published(result, "ocr_failed")

    def test_correction_failure_yields_awaiting_ai(self):
        result = self._inject("correction", RuntimeError("校正注入失败"))
        self.assertEqual(result["status"], "awaiting_ai")
        self._assert_degraded_but_published(result, "correction_failed")

    def test_summary_failure_yields_awaiting_ai(self):
        result = self._inject("summary", RuntimeError("摘要注入失败"))
        self.assertEqual(result["status"], "awaiting_ai")
        self._assert_degraded_but_published(result, "summary_failed")


class MediaCleanupMatrixTests(InjectionAcceptanceTests):
    def test_every_injected_stage_leaves_no_media(self):
        for stage in ("ingest", "download", "asr1", "ocr", "correction",
                      "summary", "publish"):
            with self.subTest(stage=stage):
                self._process()          # 每轮重建基线
                self._inject(stage, RuntimeError(f"{stage} 注入失败"))
                self.assertEqual(self._leftover_media(), [])


if __name__ == "__main__":
    unittest.main()
