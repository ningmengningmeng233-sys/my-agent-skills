"""`download_video` 的行为测试：命令构造、产物挑选、失败报错（不联网）。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from scripts import correct_with_evidence as cwe
from tests.support import SkillTestCase


class DownloadVideoTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def fake_run(self, created: str | None, *, stderr: str = ""):
        """伪造 yt-dlp：按需生成产物文件，并记录调用参数。"""
        calls = []

        def runner(argv, **kwargs):
            calls.append({"argv": argv, "kwargs": kwargs})
            if created:
                (self.dir / created).write_bytes(b"fake-video")
            return subprocess.CompletedProcess(argv, 0 if created else 1, "",
                                               stderr)
        return runner, calls

    def test_builds_video_only_command_and_returns_artifact(self):
        runner, calls = self.fake_run("BV1aaaaaaaaa_P01.mp4")
        with mock.patch.object(cwe.shutil, "which", return_value="yt-dlp"), \
             mock.patch.object(cwe.subprocess, "run", runner):
            path = cwe.download_video("BV1aaaaaaaaa", 1, self.dir)
        self.assertEqual(path.name, "BV1aaaaaaaaa_P01.mp4")
        argv = calls[0]["argv"]
        self.assertIn("bv*[height<=720]/bv*/b", argv)
        self.assertIn("--no-playlist", argv)
        self.assertIn("https://www.bilibili.com/video/BV1aaaaaaaaa/", argv)
        self.assertNotIn("?p=", " ".join(argv))

    def test_page_greater_than_one_appends_p_parameter(self):
        runner, calls = self.fake_run("BV1aaaaaaaaa_P03.mp4")
        with mock.patch.object(cwe.shutil, "which", return_value="yt-dlp"), \
             mock.patch.object(cwe.subprocess, "run", runner):
            cwe.download_video("BV1aaaaaaaaa", 3, self.dir)
        self.assertIn("https://www.bilibili.com/video/BV1aaaaaaaaa/?p=3",
                      calls[0]["argv"])

    def test_partial_and_json_files_are_ignored(self):
        runner, _ = self.fake_run(None)
        (self.dir / "BV1aaaaaaaaa_P01.mp4.part").write_bytes(b"x")
        (self.dir / "BV1aaaaaaaaa_P01.info.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(cwe.shutil, "which", return_value="yt-dlp"), \
             mock.patch.object(cwe.subprocess, "run", runner):
            with self.assertRaises(RuntimeError):
                cwe.download_video("BV1aaaaaaaaa", 1, self.dir)

    def test_failure_message_includes_last_stderr_line(self):
        runner, _ = self.fake_run(None, stderr="ERROR: 视频不存在\nERROR: 请检查链接\n")
        with mock.patch.object(cwe.shutil, "which", return_value="yt-dlp"), \
             mock.patch.object(cwe.subprocess, "run", runner):
            with self.assertRaises(RuntimeError) as ctx:
                cwe.download_video("BV1aaaaaaaaa", 1, self.dir)
        self.assertIn("请检查链接", str(ctx.exception))

class DownloadCliTests(SkillTestCase):
    """下载失败时的 CLI 行为（用完整 schema 的测试库）。"""

    def test_cli_returns_two_when_download_fails(self):
        self.add_row("BV1aaaaaaaaa", transcript_text="原始正文")
        with mock.patch.object(cwe, "download_video",
                               side_effect=RuntimeError("下载视频失败：网络不可达")):
            code, out = self.capture(cwe.main, [
                "--json", "--db", str(self.db), "--bvid", "BV1aaaaaaaaa",
                "--page", "1", "--download", "--apply"])
        self.assertEqual(code, 2)
        conn = sqlite3.connect(str(self.db))
        try:
            count = conn.execute("SELECT COUNT(*) FROM transcript_versions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0, "下载失败不得写入任何版本")


class SidecarTests(SkillTestCase):
    """分段侧车的写出契约（真实 Whisper 之外的部分）。"""

    def test_sidecar_contains_timing_and_confidence(self):
        from scripts.fw_whisper_main import write_segments_sidecar

        txt = self.root / "audio.txt"
        txt.write_text("一段\n两段\n", encoding="utf-8")

        class Info:
            language = "zh"
            language_probability = 0.98
            duration = 12.5

        segments = [
            {"start": 0.0, "end": 6.0, "text": "一段",
             "avg_logprob": -0.2, "no_speech_prob": 0.01},
            {"start": 6.0, "end": 12.5, "text": "两段",
             "avg_logprob": -0.3, "no_speech_prob": 0.02},
        ]
        sidecar = write_segments_sidecar(txt, segments, Info(), "small", "cuda",
                                        "float16")
        self.assertEqual(sidecar, txt.with_suffix(".segments.json"))
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(payload["language"], "zh")
        self.assertEqual(payload["duration"], 12.5)
        self.assertEqual(payload["model"], "small")
        self.assertEqual(len(payload["segments"]), 2)
        self.assertEqual(payload["segments"][0]["avg_logprob"], -0.2)


if __name__ == "__main__":
    unittest.main()
