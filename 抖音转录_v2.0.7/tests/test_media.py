"""media.py 音频验证与受限清理测试。"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import media, paths


def _probe_json(duration, audio_streams=1):
    streams = [{"codec_type": "audio", "codec_name": "mp3"}] * audio_streams
    if not audio_streams:
        streams = [{"codec_type": "video", "codec_name": "h264"}]
    return json.dumps({"format": {"duration": str(duration)}, "streams": streams})


class VerifyAudioTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audio = Path(self._tmp.name) / "speech.mp3"
        self.audio.write_bytes(b"x" * 64)

    def _probe(self, stdout, returncode=0):
        completed = mock.Mock(stdout=stdout, stderr="", returncode=returncode)
        with mock.patch.object(media.subprocess, "run",
                               return_value=completed) as fake:
            result = media.verify_audio(self.audio)
        return result, fake

    def test_valid_audio_returns_duration_and_uses_expected_flags(self):
        duration, fake = self._probe(_probe_json(42.5))
        self.assertAlmostEqual(duration, 42.5)
        argv = fake.call_args.args[0]
        self.assertIn("-v", argv)
        self.assertIn("error", argv)
        self.assertIn("-show_entries", argv)
        self.assertIn("format=duration", argv)
        self.assertIn("-show_streams", argv)
        self.assertIn("-of", argv)
        self.assertIn("json", argv)
        self.assertNotIn("shell", fake.call_args.kwargs)

    def test_no_audio_stream_raises(self):
        with self.assertRaises(media.MediaError):
            self._probe(_probe_json(42.5, audio_streams=0))

    def test_zero_duration_raises(self):
        with self.assertRaises(media.MediaError):
            self._probe(_probe_json(0))

    def test_unparseable_probe_output_raises(self):
        with self.assertRaises(media.MediaError):
            self._probe("not json")

    def test_ffprobe_failure_raises(self):
        with mock.patch.object(media.subprocess, "run",
                               side_effect=FileNotFoundError("no ffprobe")):
            with self.assertRaises(media.MediaError):
                media.verify_audio(self.audio)

    def test_missing_file_raises(self):
        with self.assertRaises(media.MediaError):
            media.verify_audio(Path(self._tmp.name) / "ghost.mp3")


class CleanupJobDirectoryTests(unittest.TestCase):
    def setUp(self):
        paths.ensure_runtime_dirs()
        self.job_dir = paths.JOBS_DIR / "test-cleanup-job"
        if self.job_dir.exists():
            shutil.rmtree(self.job_dir)
        (self.job_dir / "media").mkdir(parents=True)
        (self.job_dir / "media" / "video.mp4").write_bytes(b"v")
        (self.job_dir / "media" / "speech.mp3").write_bytes(b"a")
        (self.job_dir / "run.log").write_text("log", encoding="utf-8")
        self.addCleanup(lambda: shutil.rmtree(self.job_dir, ignore_errors=True))

    def test_removes_media_keeps_preserved(self):
        media.cleanup_job_directory(
            self.job_dir, preserve={self.job_dir / "run.log"})
        self.assertFalse((self.job_dir / "media" / "video.mp4").exists())
        self.assertFalse((self.job_dir / "media" / "speech.mp3").exists())
        self.assertTrue((self.job_dir / "run.log").is_file())

    def test_removes_empty_directories(self):
        log = self.job_dir / "run.log"
        media.cleanup_job_directory(self.job_dir, preserve={log})
        # media 子目录已空并被移除
        self.assertFalse((self.job_dir / "media").exists())

    def test_refuses_path_outside_jobs(self):
        for forbidden in (paths.ROOT, paths.OUTPUT_DIR, paths.INGEST_STATE_DIR,
                          paths.DB_PATH.parent):
            with self.assertRaises(ValueError):
                media.cleanup_job_directory(forbidden)

    def test_refuses_jobs_root_itself(self):
        with self.assertRaises(ValueError):
            media.cleanup_job_directory(paths.JOBS_DIR)

    def test_refuses_traversal_escape(self):
        escape = paths.JOBS_DIR / ".." / "output"
        with self.assertRaises(ValueError):
            media.cleanup_job_directory(escape)

    def test_missing_job_dir_is_noop(self):
        media.cleanup_job_directory(paths.JOBS_DIR / "does-not-exist-xyz")


if __name__ == "__main__":
    unittest.main()
