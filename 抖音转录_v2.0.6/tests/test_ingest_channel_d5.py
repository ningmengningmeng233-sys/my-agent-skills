# -*- coding: utf-8 -*-
"""D5 —— 上游单点依赖（douyin-ingest）必须像人一样失败，而不是崩溃。

锁定版本 v0.3.0 的第三方 douyin-ingest 是唯一访问入口（~4 星、~2 月无提交）。
本测试钉死三条失败路径的白话可处置文案，并验证降级通道 yt-dlp 的选择逻辑：
  1. 锁定版本被改成不存在的版本 → 白话报错，含「--channel yt-dlp」「检查上游仓库」，无 Traceback；
  2. 完全离线（死代理）→ 同类白话报错，不挂起、不抛栈；
  3. 本机没有 yt-dlp → 给出确切安装命令。
绝不真的联网或安装任何东西（subprocess 全 mock）。
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ingest_runner
from scripts.ingest_runner import IngestRunResult, RetryPolicy

DEAD_PROXY = "http://127.0.0.1:9"
FAST = RetryPolicy(sleep=lambda _d: None)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job_dir = Path(self._tmp.name) / "job"
        (self.job_dir / "media").mkdir(parents=True)
        self._patch("resolve_executable",
                    return_value=Path("C:/venv/Scripts/douyin-ingest.exe"))

    def _patch(self, name, **kw):
        p = mock.patch.object(ingest_runner, name, **kw)
        started = p.start()
        self.addCleanup(p.stop)
        return started

    def _env(self, **kw):
        for key, value in kw.items():
            if value is None:
                self.addCleanup(_delenv, key)
                import os
                os.environ.pop(key, None)
            else:
                p = mock.patch.dict(__import__("os").environ, {key: str(value)})
                p.start()
                self.addCleanup(p.stop)


def _delenv(key):
    import os
    os.environ.pop(key, None)


class BadVersionTests(_Base):
    """锁定版本被改成不存在的版本：白话可处置、无 Traceback、不误调上游。"""

    def test_nonexistent_pinned_version_gives_plain_actionable_error(self):
        self._env(DOUYIN_INGEST_VERSION="9.9.9-never-released")
        self._patch("installed_ingest_version", return_value="0.3.0")
        self._patch("yt_dlp_path", return_value=None)
        run = self._patch("run_ingest")  # 版本门应先拦截，不该走到这里
        result = ingest_runner.ingest_with_fallback(
            "https://v.douyin.com/abc/", self.job_dir, headless=True,
            retry_policy=FAST)
        self.assertEqual(result.kind, "platform_failed")
        msg = result.message
        self.assertIn("--channel yt-dlp", msg)
        self.assertIn("检查上游仓库", msg)
        self.assertNotIn("Traceback", msg)
        # 白话中文：不含异常类名堆栈
        self.assertNotIn("Error:", msg)
        run.assert_not_called()


class OfflineTests(_Base):
    """完全离线（死代理）：同类白话报错，不挂起、不抛栈。"""

    def test_offline_connection_failure_degrades_like_a_human(self):
        pin = ingest_runner.DEFAULT_PINNED_VERSION
        self._env(
            DOUYIN_INGEST_VERSION=pin,
            HTTP_PROXY=DEAD_PROXY, HTTPS_PROXY=DEAD_PROXY,
            http_proxy=DEAD_PROXY, https_proxy=DEAD_PROXY, no_proxy="localhost,127.0.0.1")
        self._patch("installed_ingest_version", return_value=pin)
        self._patch("yt_dlp_path", return_value=None)
        with mock.patch.object(ingest_runner.subprocess, "run",
                               side_effect=OSError("[WinError 10061] 目标计算机积极拒绝")) as fake:
            result = ingest_runner.ingest_with_fallback(
                "https://v.douyin.com/abc/", self.job_dir, headless=True,
                retry_policy=FAST)
        # 有限次重试（不挂起），且产出白话可处置文案
        self.assertGreaterEqual(fake.call_count, 1)
        self.assertLessEqual(fake.call_count, 6)
        self.assertEqual(result.kind, "platform_failed")
        self.assertIn("--channel yt-dlp", result.message)
        self.assertNotIn("Traceback", result.message)


class MissingYtDlpTests(_Base):
    """没有降级通道时，给出确切的安装命令。"""

    def test_absent_ytdlp_message_carries_install_command(self):
        self._env(DOUYIN_INGEST_VERSION="9.9.9-never-released")
        self._patch("installed_ingest_version", return_value="0.3.0")
        self._patch("yt_dlp_path", return_value=None)
        result = ingest_runner.ingest_with_fallback(
            "https://v.douyin.com/abc/", self.job_dir, headless=True,
            retry_policy=FAST)
        msg = result.message
        self.assertIn("pip install", msg)
        self.assertIn("yt-dlp", msg)
        self.assertNotIn("Traceback", msg)


class FallbackSelectionTests(_Base):
    """上游失败 + 本机有 yt-dlp → 走降级通道，且不再调 douyin-ingest 二次。"""

    def test_picks_ytdlp_when_present(self):
        pin = ingest_runner.DEFAULT_PINNED_VERSION
        self._env(DOUYIN_INGEST_VERSION=pin)
        self._patch("installed_ingest_version", return_value=pin)
        self._patch("yt_dlp_path", return_value="C:/bin/yt-dlp.exe")
        self._patch("run_ingest",
                    return_value=IngestRunResult("ingest_contract_changed", None,
                                                 "上游结构变了"))
        sentinel = IngestRunResult("ok", {"ok": True}, "yt-dlp 成功")
        ytdlp = self._patch("run_yt_dlp", return_value=sentinel)
        result = ingest_runner.ingest_with_fallback(
            "https://v.douyin.com/abc/", self.job_dir, headless=True,
            retry_policy=FAST)
        ytdlp.assert_called_once()
        self.assertEqual(result.kind, "ok")

    def test_ytdlp_argv_uses_binary_never_shell(self):
        media = self.job_dir / "media"
        (media / "yt.m4a").write_bytes(b"\x00fakeaudio")
        completed = mock.Mock(returncode=0, stderr="",
                              stdout="7652684072266845450\t标题\t"
                                     "https://www.douyin.com/video/7652")
        with mock.patch.object(ingest_runner.subprocess, "run",
                               return_value=completed) as fake:
            result = ingest_runner.run_yt_dlp(
                "https://v.douyin.com/abc/", self.job_dir,
                executable="C:/bin/yt-dlp.exe")
        argv = fake.call_args.args[0]
        kwargs = fake.call_args.kwargs
        self.assertEqual(argv[0], "C:/bin/yt-dlp.exe")
        self.assertIn("--no-playlist", argv)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(result.kind, "ok")
        self.assertTrue(result.payload["ok"])
        self.assertEqual(result.payload["videos"][0]["speech_audio_file"],
                         str(media / "yt.m4a"))


class ChannelGateTests(_Base):
    def test_explicit_ingest_channel_disables_fallback(self):
        self._env(DOUYIN_INGEST_VERSION="9.9.9-never-released")
        self._patch("installed_ingest_version", return_value="0.3.0")
        ytdlp = self._patch("run_yt_dlp")
        self._patch("yt_dlp_path", return_value="C:/bin/yt-dlp.exe")
        result = ingest_runner.ingest_with_fallback(
            "x", self.job_dir, headless=True, channel="ingest",
            retry_policy=FAST)
        ytdlp.assert_not_called()
        self.assertEqual(result.kind, "platform_failed")

    def test_effective_channel_reads_env(self):
        self._env(DOUYIN_INGEST_CHANNEL="yt-dlp")
        self.assertEqual(ingest_runner.effective_channel(""), "yt-dlp")
        self.assertEqual(ingest_runner.effective_channel("auto"), "auto")
        self.assertEqual(ingest_runner.effective_channel("乱填"), "auto")


class PinningDefaultsTests(unittest.TestCase):
    def test_default_pin_matches_skill_markdown(self):
        self.assertEqual(ingest_runner.DEFAULT_PINNED_VERSION, "0.3.0")

    def test_pinned_version_reads_env_override(self):
        with mock.patch.dict(__import__("os").environ,
                             {"DOUYIN_INGEST_VERSION": "1.2.3"}):
            self.assertEqual(ingest_runner.pinned_version(), "1.2.3")
        _delenv("DOUYIN_INGEST_VERSION")
        self.assertEqual(ingest_runner.pinned_version(), "0.3.0")


if __name__ == "__main__":
    unittest.main()
