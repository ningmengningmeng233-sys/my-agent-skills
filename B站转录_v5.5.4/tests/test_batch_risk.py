"""B站风控的识别、整批退避与暂停恢复。

原缺陷：元数据拉取失败时只打印「无法获取视频信息」，yt-dlp 的 412 全被
`2>/dev/null` 丢掉；批次层既不认识风控，也没有退避，94 条任务各自在 30 秒内
把 3 次重试撞完，把风控窗口撑得更久。这里锁定四件事：
识别得出、不在同一小时内反复撞、退避解不掉的会暂停、退避满了能自动继续。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import batch_state
from scripts import batch_transcribe as batch


class RiskDetectionTests(unittest.TestCase):
    def test_http_412_is_risk_control(self):
        text = ("ERROR: [BiliBili] Unable to download JSON metadata: "
                "HTTP Error 412: Precondition Failed")
        self.assertTrue(batch.detect_risk_control(text))

    def test_machine_readable_marker_is_risk_control(self):
        """脚本层输出的原因标记必须能被子进程输出解析到。"""
        self.assertTrue(batch.detect_risk_control(
            "[BILI-META-FAIL] reason=risk_control url=https://b.com/x"))

    def test_api_error_codes_are_risk_control(self):
        for text in ('{"code":-412,"message":"请求被拦截"}',
                     '{"code":-352,"message":"风控校验失败"}'):
            with self.subTest(text=text):
                self.assertTrue(batch.detect_risk_control(text))

    def test_plain_network_error_is_not_risk_control(self):
        text = "ERROR: Unable to download JSON metadata: <urlopen error timed out>"
        self.assertFalse(batch.detect_risk_control(text))

    def test_empty_text_is_not_risk_control(self):
        self.assertFalse(batch.detect_risk_control(""))
        self.assertFalse(batch.detect_risk_control(None))

    def test_cooldowns_come_from_env(self):
        with mock.patch.dict(os.environ, {"BILI_RISK_COOLDOWNS": "60,120"},
                             clear=False):
            self.assertEqual(batch.risk_cooldowns(), (60, 120))

    def test_broken_cooldowns_fall_back_to_default(self):
        with mock.patch.dict(os.environ, {"BILI_RISK_COOLDOWNS": ",abc"},
                             clear=False):
            self.assertEqual(batch.risk_cooldowns(), batch.RISK_COOLDOWNS)


class NoTightRetryTests(unittest.TestCase):
    """风控与认证一样，重试没有任何意义——不能把重试预算烧在同一堵墙上。"""

    def test_run_job_gives_up_immediately_on_risk_control(self):
        video = {"bvid": "BV1", "page": 1}
        with mock.patch.object(batch, "transcribe_video",
                               return_value=(False, "风控", None, False,
                                             "risk_control")) as probe, \
                mock.patch.object(batch, "OUTPUT_DIR", Path(tempfile.mkdtemp())):
            result = batch.run_job(video)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.failure_code, "risk_control")

    def test_run_job_gives_up_immediately_on_login_required(self):
        video = {"bvid": "BV1", "page": 1}
        with mock.patch.object(batch, "transcribe_video",
                               return_value=(False, "登录", None, False,
                                             "login_required")) as probe, \
                mock.patch.object(batch, "OUTPUT_DIR", Path(tempfile.mkdtemp())):
            result = batch.run_job(video)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(result.failure_code, "login_required")


class TranscribeClassificationTests(unittest.TestCase):
    """子进程输出的定性：412 必须判成风控，而不是「网络错误」或「登录失效」。

    这是本次缺陷的根因所在——原先元数据拉取把 yt-dlp 的 stderr 丢掉，
    412 根本没有进到 stdout，批次层只能看到「无法获取视频信息」。
    """

    class _Result:
        def __init__(self, stdout, stderr=""):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = 1

    def _classify(self, stdout, stderr=""):
        # transcribe_video 会往 stdout 打进度；测试里吞掉，免得刷屏。
        with mock.patch.object(
                batch.subprocess, "run",
                return_value=self._Result(stdout, stderr)), \
                mock.patch.object(batch, "OUTPUT_DIR",
                                  Path(tempfile.mkdtemp())), \
                contextlib.redirect_stdout(io.StringIO()):
            ok, _, _, _, failure_code = batch.transcribe_video("BV1", 1)
        return ok, failure_code

    def test_412_output_is_risk_control(self):
        ok, code = self._classify(
            "❌ 无法获取视频信息：疑似 B站风控/限流（HTTP 412）\n"
            "[BILI-META-FAIL] reason=risk_control url=https://b.com/x\n"
            "   ↳ ERROR: HTTP Error 412: Precondition Failed")
        self.assertFalse(ok)
        self.assertEqual(code, "risk_control")

    def test_risk_control_wins_over_auth_when_both_match(self):
        """412 同时命中两套模式时，必须按风控处理（可退避自愈），而不是要求人工登录。"""
        ok, code = self._classify(
            "ERROR: [BiliBili] HTTP Error 412: Precondition Failed\n"
            "需要登录后才能访问")
        self.assertFalse(ok)
        self.assertEqual(code, "risk_control")

    def test_login_wording_without_412_is_login_required(self):
        ok, code = self._classify("❌ 需要登录后才能访问该视频")
        self.assertFalse(ok)
        self.assertEqual(code, "login_required")

    def test_ordinary_failure_has_no_special_code(self):
        ok, code = self._classify("❌ Whisper 转录失败\nAudio decode error")
        self.assertFalse(ok)
        self.assertEqual(code, "")


class RiskPauseStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="risk_"))
        self.path = self.tmp / "batch_state.json"

    def make(self):
        return batch_state.BatchState.create(
            path=self.path, batch_id="B-risk",
            items=[{"key": k, "bvid": k, "page": 1}
                   for k in ("BV1", "BV2", "BV3")])

    def test_pause_for_risk_stops_the_batch_and_keeps_item_rerunnable(self):
        state = self.make()
        state.claim("BV1")
        state.pause_for_risk("BV1", reason="风控/限流持续未解除")
        self.assertEqual(state.status, "paused_risk")
        self.assertTrue(state.is_paused)
        self.assertEqual(state.get("BV1")["status"], "retry_wait")
        self.assertEqual(state.get("BV1")["failure_code"], "risk_control")
        self.assertIsNone(state.next_pending())
        self.assertEqual(state.pause_reason, "风控/限流持续未解除")
        self.assertTrue(state.paused_at)

    def test_risk_pause_is_distinguishable_from_auth_pause(self):
        state = self.make()
        state.claim("BV1")
        state.pause_for_risk("BV1")
        self.assertNotEqual(state.status, batch_state.BATCH_PAUSED_AUTH)

    def test_resume_clears_the_risk_pause(self):
        state = self.make()
        state.claim("BV1")
        state.pause_for_risk("BV1")
        state.resume()
        self.assertEqual(state.status, "running")
        self.assertEqual(state.pause_reason, "")
        self.assertEqual(state.next_pending()["key"], "BV1")

    def test_auth_items_are_still_requeued_by_resume(self):
        state = self.make()
        state.claim("BV1")
        state.pause_for_auth("BV1")
        state.resume()
        self.assertEqual(state.get("BV1")["status"], "retry_wait")


class RiskResumeTests(unittest.TestCase):
    """风控暂停后的启动处理：退避满自动继续，未满则停下等人。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="riskresume_"))
        self.path = self.tmp / "batch_state.json"
        self.state = batch_state.BatchState.create(
            path=self.path, batch_id="B-risk",
            items=[{"key": "BV1", "bvid": "BV1", "page": 1}])
        self.state.claim("BV1")
        self.state.pause_for_risk("BV1", reason="风控")

    def test_recent_pause_refuses_to_continue(self):
        with mock.patch.dict(os.environ, {"BILI_RISK_RESUME_COOLDOWN": "1800"},
                             clear=False):
            self.assertFalse(batch.resolve_risk_pause(self.state))
        self.assertEqual(self.state.status, "paused_risk")

    def test_expired_pause_resumes_automatically(self):
        old = (datetime.now(timezone.utc)
               - timedelta(seconds=3600)).isoformat()
        self.state.payload["paused_at"] = old
        with mock.patch.dict(os.environ, {"BILI_RISK_RESUME_COOLDOWN": "1800"},
                             clear=False):
            self.assertTrue(batch.resolve_risk_pause(self.state))
        self.assertEqual(self.state.status, "running")
        self.assertEqual(self.state.get("BV1")["status"], "retry_wait")

    def test_unknown_pause_time_refuses_to_continue(self):
        """时间戳损坏时宁可停下等人，也不要盲目继续撞墙。"""
        self.state.payload["paused_at"] = "不是时间"
        self.assertFalse(batch.resolve_risk_pause(self.state))

    def test_resumed_state_is_persisted(self):
        old = (datetime.now(timezone.utc)
               - timedelta(seconds=3600)).isoformat()
        self.state.payload["paused_at"] = old
        with mock.patch.dict(os.environ, {"BILI_RISK_RESUME_COOLDOWN": "60"},
                             clear=False):
            batch.resolve_risk_pause(self.state)
        reloaded = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["status"], "running")
        self.assertNotIn("pause_reason", reloaded)


if __name__ == "__main__":
    unittest.main()
