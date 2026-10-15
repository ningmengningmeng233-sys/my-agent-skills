"""临时视频素材下载：只允许 http(s)、校验媒体类型、原子落地并 FFprobe 验证。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.ingest_adapter import IngestResult
from scripts.video_media import (MediaDownloadError, materialize_video,
                                 verify_video)

MEDIA_URL = "https://v3-web.douyinvod.com/abc/video.mp4?token=SECRET_MEDIA_TOKEN"
COOKIE_HEADER = "SECRET_COOKIE_123"
HEADERS = {"user-agent": "Mozilla/5.0", "cookie": COOKIE_HEADER}


class FakeResponse:
    def __init__(self, status_code=200, content=b"fake-video-bytes",
                 content_type="video/mp4", url=MEDIA_URL):
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.url = url
        self._content = content

    def iter_content(self, chunk_size=0):
        yield self._content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        pass


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


class VideoMediaTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job_dir = Path(self._tmp.name) / "job"
        self.job_dir.mkdir(parents=True)
        audio = self.job_dir / "speech.mp3"
        audio.write_bytes(b"audio")
        self.result = IngestResult(
            aweme_id="7410000000000001234",
            canonical_url="https://www.douyin.com/video/7410000000000001234",
            title="标题", author="作者", publish_date="2026-06-19",
            duration_seconds=71.2, audio_path=audio,
            video_download_url=MEDIA_URL, download_headers=HEADERS)


class MaterializeVideoTests(VideoMediaTestCase):
    def _probe(self, *, ok=True):
        return mock.Mock(return_value=ok)

    def test_downloads_into_job_media_and_verifies(self):
        session = FakeSession()
        probe = self._probe()
        path = materialize_video(self.result, self.job_dir, session=session,
                                 probe=probe)
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.job_dir / "media")
        self.assertEqual(path.read_bytes(), b"fake-video-bytes")
        self.assertEqual(session.calls[0][0], MEDIA_URL)
        self.assertEqual(session.calls[0][1]["headers"]["cookie"], COOKIE_HEADER)
        probe.assert_called_once_with(path)

    def test_leftover_part_file_is_removed(self):
        session = FakeSession()
        path = materialize_video(self.result, self.job_dir, session=session,
                                 probe=self._probe())
        leftovers = [p.name for p in path.parent.iterdir()
                     if p.name.endswith(".part")]
        self.assertEqual(leftovers, [])

    def test_non_http_scheme_is_refused(self):
        result = IngestResult(**{**self.result.__dict__,
                                 "video_download_url": "file:///C:/secret.mp4"})
        session = FakeSession()
        with self.assertRaises(MediaDownloadError) as ctx:
            materialize_video(result, self.job_dir, session=session,
                              probe=self._probe())
        self.assertEqual(ctx.exception.code, "video_invalid")
        session.calls.clear()

    def test_missing_video_url_is_refused(self):
        result = IngestResult(**{**self.result.__dict__, "video_download_url": ""})
        with self.assertRaises(MediaDownloadError) as ctx:
            materialize_video(result, self.job_dir, session=FakeSession(),
                              probe=self._probe())
        self.assertEqual(ctx.exception.code, "video_invalid")

    def test_http_error_is_media_download_failed(self):
        session = FakeSession(response=FakeResponse(status_code=403))
        with self.assertRaises(MediaDownloadError) as ctx:
            materialize_video(self.result, self.job_dir, session=session,
                              probe=self._probe())
        self.assertEqual(ctx.exception.code, "media_download_failed")

    def test_non_media_content_type_is_refused(self):
        session = FakeSession(response=FakeResponse(
            content_type="text/html", content=b"<html>login</html>"))
        with self.assertRaises(MediaDownloadError) as ctx:
            materialize_video(self.result, self.job_dir, session=session,
                              probe=self._probe())
        self.assertEqual(ctx.exception.code, "media_download_failed")

    def test_redirect_to_html_is_refused_even_on_200(self):
        # 重定向后仍必须是媒体类型
        session = FakeSession(response=FakeResponse(
            status_code=200, content_type="text/html",
            url="https://www.douyin.com/login"))
        with self.assertRaises(MediaDownloadError):
            materialize_video(self.result, self.job_dir, session=session,
                              probe=self._probe())

    def test_empty_body_is_refused(self):
        session = FakeSession(response=FakeResponse(content=b""))
        with self.assertRaises(MediaDownloadError):
            materialize_video(self.result, self.job_dir, session=session,
                              probe=self._probe())

    def test_network_error_is_reported_without_leaking_url(self):
        session = FakeSession(error=ConnectionError("boom"))
        with self.assertRaises(MediaDownloadError) as ctx:
            materialize_video(self.result, self.job_dir, session=session,
                              probe=self._probe())
        self.assertEqual(ctx.exception.code, "media_download_failed")
        self.assertNotIn("SECRET_MEDIA_TOKEN", str(ctx.exception))
        self.assertNotIn(MEDIA_URL, str(ctx.exception))

    def test_probe_failure_reports_video_invalid_and_cleans_file(self):
        session = FakeSession()
        with self.assertRaises(MediaDownloadError) as ctx:
            materialize_video(self.result, self.job_dir, session=session,
                              probe=self._probe(ok=False))
        self.assertEqual(ctx.exception.code, "video_invalid")
        media_dir = self.job_dir / "media"
        self.assertEqual([p.name for p in media_dir.iterdir()], [])

    def test_message_never_contains_headers(self):
        session = FakeSession(error=ConnectionError("boom"))
        with self.assertRaises(MediaDownloadError) as ctx:
            materialize_video(self.result, self.job_dir, session=session,
                              probe=self._probe())
        self.assertNotIn(COOKIE_HEADER, str(ctx.exception))


class VerifyVideoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "v.mp4"
        self.path.write_bytes(b"x")

    def _probe_run(self, stdout: str, returncode: int = 0):
        return mock.patch(
            "scripts.video_media.subprocess.run",
            return_value=mock.Mock(stdout=stdout, stderr="", returncode=returncode))

    def test_video_stream_is_accepted(self):
        payload = ('{"streams": [{"codec_type": "video", "codec_name": "h264"}],'
                   ' "format": {"duration": "71.2"}}')
        with self._probe_run(payload):
            self.assertTrue(verify_video(self.path))

    def test_audio_only_file_is_rejected(self):
        payload = '{"streams": [{"codec_type": "audio", "codec_name": "aac"}]}'
        with self._probe_run(payload):
            self.assertFalse(verify_video(self.path))

    def test_broken_json_is_rejected(self):
        with self._probe_run("not json"):
            self.assertFalse(verify_video(self.path))

    def test_probe_failure_is_rejected(self):
        with mock.patch("scripts.video_media.subprocess.run",
                        side_effect=OSError("no ffprobe")):
            self.assertFalse(verify_video(self.path))

    def test_missing_file_is_rejected(self):
        self.assertFalse(verify_video(Path(self._tmp.name) / "nope.mp4"))


if __name__ == "__main__":
    unittest.main()
