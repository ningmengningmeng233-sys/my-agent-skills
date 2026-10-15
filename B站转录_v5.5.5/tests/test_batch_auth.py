"""批量认证失效识别与状态接线的测试。

批量最痛的失败模式是 Cookie 过期：原先它和普通失败混在一起，
脚本会继续空跑剩下的几百条，既浪费时间又触发更多风控。
这里锁定「能识别、会停下、能续跑」三件事。
"""

from __future__ import annotations

import unittest

from scripts import batch_transcribe as batch


class AuthFailureDetectionTests(unittest.TestCase):
    """只有强信号才算认证失效；缺失 Cookie 的常规告警不算。"""

    def test_412_is_auth_failure(self):
        text = "ERROR: [BiliBili] Unable to download JSON metadata: HTTP Error 412: Precondition Failed"
        self.assertTrue(batch.detect_auth_failure(text))

    def test_precondition_failed_is_auth_failure(self):
        self.assertTrue(batch.detect_auth_failure("HTTP Error 412: Precondition Failed"))

    def test_login_wording_is_auth_failure(self):
        for text in ("需要登录后才能访问", "请先登录", "Please log in to continue",
                     "风控校验未通过"):
            with self.subTest(text=text):
                self.assertTrue(batch.detect_auth_failure(text))

    def test_ordinary_missing_cookie_warning_is_not_auth_failure(self):
        """缺 Cookie 时脚本会退回 Whisper 正常出稿，不该因此暂停整批。"""
        text = "   [WARN] 无可用Cookie，B站AI字幕可能无法获取\n   [TIP] 请先用 Chrome/Edge 登录 bilibili.com"
        self.assertFalse(batch.detect_auth_failure(text))

    def test_ordinary_failure_is_not_auth_failure(self):
        text = "❌ Whisper 转录失败\nfaster-whisper 转录失败: cublas64_12.dll is not found"
        self.assertFalse(batch.detect_auth_failure(text))

    def test_empty_text_is_not_auth_failure(self):
        self.assertFalse(batch.detect_auth_failure(""))
        self.assertFalse(batch.detect_auth_failure(None))


class JobFailureClassificationTests(unittest.TestCase):
    def test_result_carries_a_failure_code(self):
        """失败的返回值要能区分「认证」与「其他」，编排层才知道该不该停。"""
        fields = batch.job_result_fields()
        self.assertIn("failure_code", fields)


if __name__ == "__main__":
    unittest.main()
