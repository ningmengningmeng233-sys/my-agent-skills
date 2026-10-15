"""v2 公共类型测试：状态枚举与错误码必须与设计文档逐字一致。"""

import json
import unittest

from scripts import models


class JobStatusTests(unittest.TestCase):
    def test_values_match_spec_15_2(self):
        self.assertEqual(
            [item.value for item in models.JobStatus],
            ["pending", "resolving", "fetching", "media_ready", "analyzing",
             "transcribing", "ocr", "diarization", "correcting", "summarizing",
             "publishing", "completed", "needs_review", "awaiting_ai",
             "visual_text_only", "retry_wait", "auth_required", "failed",
             "duplicate", "reused"])

    def test_terminal_and_processing_sets(self):
        self.assertIn(models.JobStatus.COMPLETED, models.TERMINAL_JOB_STATUSES)
        self.assertIn(models.JobStatus.FAILED, models.TERMINAL_JOB_STATUSES)
        self.assertIn(models.JobStatus.DUPLICATE, models.TERMINAL_JOB_STATUSES)
        self.assertIn(models.JobStatus.REUSED, models.TERMINAL_JOB_STATUSES)
        # 进程重启后需要靠心跳判断是否中断的状态
        self.assertIn(models.JobStatus.FETCHING, models.PROCESSING_JOB_STATUSES)
        self.assertIn(models.JobStatus.TRANSCRIBING, models.PROCESSING_JOB_STATUSES)
        self.assertNotIn(models.JobStatus.COMPLETED, models.PROCESSING_JOB_STATUSES)
        self.assertNotIn(models.JobStatus.PENDING, models.PROCESSING_JOB_STATUSES)


class BatchStatusTests(unittest.TestCase):
    def test_values_match_spec_15_1(self):
        self.assertEqual(
            [item.value for item in models.BatchStatus],
            ["draft", "awaiting_confirmation", "pending", "running",
             "paused_auth", "completed", "completed_with_errors"])

    def test_paused_auth_is_resumable(self):
        self.assertIn(models.BatchStatus.PAUSED_AUTH,
                      models.RESUMABLE_BATCH_STATUSES)
        self.assertIn(models.BatchStatus.RUNNING, models.RESUMABLE_BATCH_STATUSES)
        self.assertNotIn(models.BatchStatus.COMPLETED,
                         models.RESUMABLE_BATCH_STATUSES)


class StageTests(unittest.TestCase):
    def test_stage_values_match_spec_15_2(self):
        self.assertEqual(
            [item.value for item in models.Stage],
            ["resolving", "fetching", "media_ready", "analyzing",
             "transcribing", "ocr", "diarization", "correcting",
             "summarizing", "publishing"])


class QualityStatusTests(unittest.TestCase):
    def test_values_match_spec_11_3_and_11_4(self):
        self.assertEqual([item.value for item in models.QualityStatus],
                         ["passed", "needs_review", "visual_text_only"])


class ErrorCodeTests(unittest.TestCase):
    def test_values_match_spec_18(self):
        self.assertEqual(
            [item.value for item in models.ErrorCode],
            ["invalid_input", "input_encoding_error",
             "batch_confirmation_required", "login_required",
             "video_unavailable", "platform_rate_limited", "ingest_timeout",
             "ingest_contract_changed", "media_download_failed",
             "audio_invalid", "video_invalid", "audio_enhancement_failed",
             "asr_failed", "ocr_failed", "diarization_failed",
             "correction_pending", "summary_pending", "database_failed",
             "publish_failed", "artifact_missing"])

    def test_auth_and_video_errors_are_classified(self):
        self.assertIn(models.ErrorCode.LOGIN_REQUIRED, models.AUTH_ERROR_CODES)
        self.assertIn(models.ErrorCode.VIDEO_UNAVAILABLE, models.VIDEO_ERROR_CODES)
        self.assertNotIn(models.ErrorCode.ASR_FAILED, models.VIDEO_ERROR_CODES)

    def test_unavailable_only_means_normalized_absence(self):
        # 只有平台确定性缺失才算 video_unavailable，认证问题必须走 login_required
        self.assertIn(models.ErrorCode.VIDEO_UNAVAILABLE, models.VIDEO_ERROR_CODES)
        self.assertNotIn(models.ErrorCode.LOGIN_REQUIRED, models.VIDEO_ERROR_CODES)


class StableJsonTests(unittest.TestCase):
    def test_enums_serialize_as_plain_strings(self):
        payload = json.dumps({"status": models.JobStatus.COMPLETED,
                              "stage": models.Stage.PUBLISHING,
                              "error": models.ErrorCode.ASR_FAILED},
                             ensure_ascii=False)
        self.assertEqual(json.loads(payload), {"status": "completed",
                                              "stage": "publishing",
                                              "error": "asr_failed"})

    def test_enums_compare_equal_to_their_string_value(self):
        self.assertEqual(models.JobStatus.COMPLETED, "completed")
        self.assertEqual(models.ErrorCode.LOGIN_REQUIRED, "login_required")

    def test_error_payload_shape(self):
        payload = models.error_payload(models.ErrorCode.INVALID_INPUT, "缺少链接")
        self.assertEqual(payload, {"code": "invalid_input", "message": "缺少链接"})

    def test_timestamp_is_second_precision_local_iso(self):
        from datetime import datetime

        stamp = models.utc_now()
        parsed = datetime.strptime(stamp, models.ISO_TIMESTAMP_FORMAT)
        self.assertLess(abs((datetime.now() - parsed).total_seconds()), 60)


if __name__ == "__main__":
    unittest.main()
