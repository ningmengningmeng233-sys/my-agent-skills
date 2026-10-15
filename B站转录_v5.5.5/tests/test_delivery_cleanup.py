"""交付收尾：临时媒体目录 output/_work 用完必须清空（对齐 SKILL.md 承诺）。

只用 tmp 目录造假中间媒体验证收尾函数，绝不触碰真实 output/_work 历史内容。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import batch_transcribe as batch


class FinalizeDeliveryCleanupTests(unittest.TestCase):
    def _make_work(self, output: Path) -> Path:
        work = output / "_work"
        (work / "BV1aa_1_uuid" ).mkdir(parents=True)
        (work / "BV1aa_1_uuid" / "video.mp4").write_bytes(b"video-bytes")
        (work / "BV1aa_1_uuid" / "audio.m4a").write_bytes(b"audio-bytes")
        (work / "BV1bb_2_uuid" / "frames").mkdir(parents=True)
        (work / "BV1bb_2_uuid" / "frames" / "base_000001.jpg").write_bytes(b"jpg")
        (work / "stray_video.mp4").write_bytes(b"stray")
        return work

    def test_work_is_emptied_after_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            work = self._make_work(output)
            removed = batch.finalize_delivery_cleanup(output)
            self.assertTrue(work.is_dir(), "只清内容，保留 _work 目录本身")
            self.assertEqual(list(work.iterdir()), [], "交付收尾后 _work 必须为空")
            self.assertGreaterEqual(removed, 1)

    def test_cleanup_never_touches_delivered_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            work = self._make_work(output)
            delivered = output / "BV1aa.md"
            delivered.write_text("正文", encoding="utf-8")
            batch.finalize_delivery_cleanup(output)
            self.assertTrue(delivered.is_file(), "交付物在 OUTPUT_DIR 根，不得被清理")

    def test_missing_work_dir_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            self.assertEqual(batch.finalize_delivery_cleanup(output), 0)
            self.assertFalse((output / "_work").exists())

    def test_cleanup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            self._make_work(output)
            batch.finalize_delivery_cleanup(output)
            self.assertEqual(batch.finalize_delivery_cleanup(output), 0)


if __name__ == "__main__":
    unittest.main()
