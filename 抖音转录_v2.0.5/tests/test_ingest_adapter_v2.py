"""ingest 契约 v2：临时视频上下文只驻留内存，绝不进入 repr、异常或库记录。"""

import tempfile
import unittest
from pathlib import Path

from scripts.ingest_adapter import IngestResult, parse_ingest_payload

MEDIA_URL = "https://v3-web.douyinvod.com/abc/video.mp4?token=SECRET_MEDIA_TOKEN"
COOKIE_HEADER = "SECRET_COOKIE_123"


def make_payload(audio: Path, **video_overrides) -> dict:
    video = {
        "aweme_id": "7410000000000001234",
        "name": "标题",
        "page_url": "https://www.douyin.com/video/7410000000000001234",
        "publish_time": "2026-06-19T12:00:00+08:00",
        "duration_seconds": 71.2,
        "speech_audio_file": str(audio),
        "video_download_url": MEDIA_URL,
    }
    video.update(video_overrides)
    return {
        "ok": True,
        "user": {"nickname": "作者"},
        "download_headers": {
            "user-agent": "Mozilla/5.0",
            "cookie": COOKIE_HEADER,
            "referer": "https://www.douyin.com/",
        },
        "videos": [video],
    }


class PayloadTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audio = Path(self._tmp.name) / "speech.mp3"
        self.audio.write_bytes(b"fake-audio")

    def payload(self, **video_overrides) -> dict:
        return make_payload(self.audio, **video_overrides)


class ContractParsingTests(PayloadTestCase):
    def test_temporary_video_context_is_accepted(self):
        result = parse_ingest_payload(self.payload())
        self.assertEqual(result.video_download_url, MEDIA_URL)
        self.assertEqual(result.download_headers["cookie"], COOKIE_HEADER)
        self.assertEqual(result.aweme_id, "7410000000000001234")
        self.assertEqual(result.publish_date, "2026-06-19")

    def test_missing_video_context_is_allowed(self):
        data = self.payload()
        data["videos"][0].pop("video_download_url")
        data.pop("download_headers")
        result = parse_ingest_payload(data)
        self.assertEqual(result.video_download_url, "")
        self.assertEqual(result.download_headers, {})

    def test_repr_and_str_never_expose_media_url_or_headers(self):
        result = parse_ingest_payload(self.payload())
        for text in (repr(result), str(result), f"{result!r}", f"{result}"):
            self.assertNotIn("SECRET_MEDIA_TOKEN", text)
            self.assertNotIn(COOKIE_HEADER, text)
            self.assertNotIn("SECRET", text)

    def test_public_metadata_is_safe_for_database(self):
        result = parse_ingest_payload(self.payload())
        metadata = result.public_metadata()
        self.assertEqual(metadata["title"], "标题")
        self.assertEqual(metadata["author"], "作者")
        self.assertNotIn("video_download_url", metadata)
        self.assertNotIn("download_headers", metadata)
        blob = repr(metadata)
        self.assertNotIn("SECRET_MEDIA_TOKEN", blob)
        self.assertNotIn(COOKIE_HEADER, blob)

    def test_violation_messages_never_contain_media_url(self):
        data = self.payload(speech_audio_file="")
        with self.assertRaises(ValueError) as ctx:
            parse_ingest_payload(data)
        self.assertNotIn("SECRET_MEDIA_TOKEN", str(ctx.exception))
        self.assertNotIn(COOKIE_HEADER, str(ctx.exception))


class TemporaryContextSafetyTests(PayloadTestCase):
    def test_non_http_video_url_is_dropped(self):
        for value in ("file:///C:/secret.mp4", "javascript:alert(1)",
                      "ftp://host/video.mp4", "C:\\local\\video.mp4", ""):
            with self.subTest(value=value):
                result = parse_ingest_payload(self.payload(video_download_url=value))
                self.assertEqual(result.video_download_url, "")

    def test_headers_must_be_flat_string_map(self):
        data = self.payload()
        data["download_headers"] = {"user-agent": "ua", "bad": {"nested": 1},
                                    "num": 5}
        result = parse_ingest_payload(data)
        self.assertEqual(result.download_headers, {"user-agent": "ua",
                                                   "num": "5"})

    def test_non_dict_headers_are_ignored(self):
        data = self.payload()
        data["download_headers"] = "user-agent: x"
        self.assertEqual(parse_ingest_payload(data).download_headers, {})

    def test_result_is_frozen(self):
        result = parse_ingest_payload(self.payload())
        with self.assertRaises(Exception):
            result.video_download_url = "https://evil.test/x.mp4"

    def test_audio_path_is_resolved(self):
        result = parse_ingest_payload(self.payload())
        self.assertTrue(result.audio_path.is_absolute())

    def test_invalid_payload_types_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_ingest_payload("not a dict")
        with self.assertRaises(ValueError):
            parse_ingest_payload({"ok": False})
        with self.assertRaises(ValueError):
            parse_ingest_payload({"ok": True, "videos": []})


class LegacyFieldCompatibilityTests(PayloadTestCase):
    def test_v1_fields_still_parse(self):
        result = parse_ingest_payload(self.payload())
        self.assertIsInstance(result, IngestResult)
        self.assertEqual(result.duration_seconds, 71.2)
        self.assertTrue(result.audio_path.is_file())


if __name__ == "__main__":
    unittest.main()
