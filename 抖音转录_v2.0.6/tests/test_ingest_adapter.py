"""ingest_adapter 上游 JSON 契约校验测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.ingest_adapter import IngestResult, parse_ingest_payload


def _payload(audio_file: str, **overrides) -> dict:
    video = {
        "aweme_id": "7000000001",
        "name": "作品标题",
        "duration_seconds": 15.5,
        "publish_time": "2026-09-15T10:00:00",
        "page_url": "https://www.douyin.com/video/7000000001",
        "speech_audio_file": audio_file,
    }
    video.update(overrides.pop("video", {}))
    payload = {
        "ok": True,
        "collection_mode": "single_video",
        "user": {"nickname": "作者昵称", "sec_user_id": "MSxabc"},
        "videos": [video],
    }
    payload.update(overrides)
    return payload


class ParseIngestPayloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audio = Path(self._tmp.name).resolve() / "speech.mp3"  # resolve：Windows 临时目录常是 8.3 短名（RUNNER~1）
        self.audio.write_bytes(b"ID3fake-audio-bytes")

    def test_valid_payload_returns_result(self):
        result = parse_ingest_payload(_payload(str(self.audio)))
        self.assertIsInstance(result, IngestResult)
        self.assertEqual(result.aweme_id, "7000000001")
        self.assertEqual(result.title, "作品标题")
        self.assertEqual(result.author, "作者昵称")
        self.assertEqual(result.canonical_url,
                         "https://www.douyin.com/video/7000000001")
        self.assertEqual(result.publish_date, "2026-09-15")
        self.assertAlmostEqual(result.duration_seconds, 15.5)
        self.assertEqual(result.audio_path, self.audio)

    def test_missing_optional_metadata_uses_defaults(self):
        payload = _payload(str(self.audio))
        video = payload["videos"][0]
        for key in ("duration_seconds", "publish_time", "page_url"):
            video.pop(key)
        payload["user"] = {}
        result = parse_ingest_payload(payload)
        self.assertEqual(result.publish_date, "")
        self.assertEqual(result.author, "")
        self.assertEqual(result.canonical_url, "")
        self.assertEqual(result.duration_seconds, 0.0)

    def test_rejects_ok_false(self):
        payload = _payload(str(self.audio), ok=False)
        with self.assertRaises(ValueError):
            parse_ingest_payload(payload)

    def test_rejects_multiple_or_zero_videos(self):
        payload = _payload(str(self.audio))
        payload["videos"] = payload["videos"] * 2
        with self.assertRaises(ValueError):
            parse_ingest_payload(payload)
        payload["videos"] = []
        with self.assertRaises(ValueError):
            parse_ingest_payload(payload)

    def test_rejects_missing_aweme_id(self):
        payload = _payload(str(self.audio), video={"aweme_id": ""})
        with self.assertRaises(ValueError):
            parse_ingest_payload(payload)

    def test_rejects_missing_speech_audio_file(self):
        payload = _payload(str(self.audio), video={"speech_audio_file": None})
        with self.assertRaises(ValueError):
            parse_ingest_payload(payload)

    def test_rejects_nonexistent_audio_path(self):
        payload = _payload(str(Path(self._tmp.name) / "ghost.mp3"))
        with self.assertRaises(ValueError):
            parse_ingest_payload(payload)

    def test_rejects_empty_audio_file(self):
        empty = Path(self._tmp.name) / "empty.mp3"
        empty.touch()
        payload = _payload(str(empty))
        with self.assertRaises(ValueError):
            parse_ingest_payload(payload)

    def test_error_message_excludes_full_payload(self):
        payload = _payload(str(self.audio), ok=False, secret_marker="DO-NOT-LEAK")
        with self.assertRaises(ValueError) as ctx:
            parse_ingest_payload(payload)
        self.assertNotIn("DO-NOT-LEAK", str(ctx.exception))
        self.assertNotIn("secret_marker", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
