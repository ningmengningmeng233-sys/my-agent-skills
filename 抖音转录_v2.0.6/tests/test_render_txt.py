"""render_txt.py 兼容包装：转发给 v2 主文件渲染器的路径与内容测试。

v2 起主文件标题改为「第一部分：摘要（AI 生成）」「第二部分：未经 AI 校正原始稿」，
并以 `作者_标题_作品ID.txt` 命名（设计文档第 16 节），因此本文件断言同步更新。
"""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.render_txt import RenderError, render_record
from scripts.transcript_db import TranscriptDB


def _record(**overrides) -> dict:
    record = {
        "aweme_id": "7000000001",
        "canonical_url": "https://www.douyin.com/video/7000000001",
        "title": "测试*标题?与:非法字符",
        "author": "作者<>|",
        "publish_date": "2026-09-15",
        "duration_seconds": 83.0,
        "transcript_source": "faster-whisper",
        "whisper_model": "base",
        "transcript_text": "第一句\n第二句",
        "segments_json": json.dumps([
            {"start": 1.0, "end": 2.0, "text": "第一句"},
            {"start": 2.5, "end": 4.2, "text": "第二句"},
        ], ensure_ascii=False),
        "summary": "核心观点：...\n主要论点：...\n关键结论：...",
        "summary_status": "summarized",
        "updated_at": "2026-09-15 12:00:00",
    }
    record.update(overrides)
    return record


class RenderTxtTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.output_root = Path(self._tmp.name).resolve() / "output"  # resolve：Windows 临时目录常是 8.3 短名（RUNNER~1）

    def test_archive_path_uses_publish_year_month_and_sanitized_title(self):
        path = Path(render_record(_record(), self.output_root))
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.output_root / "2026" / "09")
        name = path.name
        for illegal in ':*?"<>|\\':
            self.assertNotIn(illegal, name)
        self.assertIn("7000000001", name)
        self.assertTrue(name.endswith(".txt"))

    def test_invalid_publish_date_falls_back_to_processing_month(self):
        """发布日缺失时按「处理当月」归档：期望值必须由当日推导，不得写死月份。

        原断言把期望硬编码成 2026/09 并注释「今天 2026-09-15」，等于一把日历尺——
        除九月外任何时间跑都会红。上一条用例（有效发布日 → 用发布月）仍在，
        两条合起来才真正区分「发布月」与「处理月」，去掉时间依赖并不削弱判据。
        """
        import datetime

        now = datetime.datetime.now()
        path = Path(render_record(_record(publish_date=""), self.output_root))
        self.assertEqual(path.parent, self.output_root / str(now.year) / f"{now.month:02d}")
        # 正对照：给一个明显不同于当月的有效发布日，必须走发布月而不是处理月
        other = datetime.date(2019, 3, 7)
        path2 = Path(render_record(_record(publish_date=other.isoformat()), self.output_root))
        self.assertEqual(path2.parent, self.output_root / "2019" / "03")

    def test_body_contains_metadata_summary_and_timestamped_lines(self):
        path = Path(render_record(_record(), self.output_root))
        body = path.read_text(encoding="utf-8")
        self.assertIn("📹 视频标题：", body)
        self.assertIn("🔗 抖音链接：https://www.douyin.com/video/7000000001", body)
        self.assertIn("👤 作者：", body)
        self.assertIn("📝 版本：", body)
        self.assertIn("📅 发布时间：2026-09-15", body)
        self.assertIn("🆔 作品 ID：7000000001", body)
        self.assertIn("第一部分：摘要（AI 生成）", body)
        self.assertIn("核心观点", body)
        self.assertIn("第二部分：未经 AI 校正原始稿", body)
        self.assertIn("校正版待补", body)
        self.assertIn("[00:00:01 → 00:00:02] 第一句", body)
        self.assertIn("[00:00:02 → 00:00:04] 第二句", body)
        self.assertIn("文档结束", body)

    def test_empty_summary_renders_pending_notice(self):
        path = Path(render_record(
            _record(summary="", summary_status="pending"), self.output_root))
        body = path.read_text(encoding="utf-8")
        self.assertIn("摘要待补", body)
        self.assertIn("第二部分：未经 AI 校正原始稿", body)

    def test_invalid_segments_json_raises(self):
        with self.assertRaises(RenderError):
            render_record(_record(segments_json="{坏 JSON"), self.output_root)

    def test_database_path_roundtrip(self):
        db = TranscriptDB(Path(self._tmp.name) / ".db" / "t.db")
        self.addCleanup(db.close)
        db.upsert_fetching(aweme_id="7000000001", original_input="",
                           canonical_url="", metadata={})
        db.mark_transcribed(
            "7000000001", transcript_text="第一句", segments_json='[]',
            transcript_source="faster-whisper", whisper_model="base", language="zh")
        record = db.get_by_aweme_id("7000000001")
        path = render_record(record, self.output_root)
        db.set_transcript_file("7000000001", path)
        reused = db.get_reusable("7000000001")
        self.assertIsNotNone(reused)
        self.assertEqual(reused["transcript_file"], path)


if __name__ == "__main__":
    unittest.main()
