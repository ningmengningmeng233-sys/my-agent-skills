"""自适应 OCR：抽帧策略、字幕激活/退出、去重合并、画面文字分类。"""

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ocr as ocr_module
from scripts.ocr import (FramePolicy, FrameRef, OcrObservation, OcrOutcome,
                         classify_text, extract_adaptive_frames, merge_observations,
                         normalize_text, run_ocr, text_similarity)


def ocr_engine_available() -> bool:
    """真实 RapidOCR 引擎（含 ONNX Runtime）是否可用——纯环境事实，非产品承诺。

    缺失时端到端真机识别不可跑，相关用例按 E 级 skipif；降级路径的产品红线
    由 test_missing_ocr_dependency_degrades_without_crash 恒定覆盖。
    """
    return (importlib.util.find_spec("rapidocr") is not None
            and importlib.util.find_spec("onnxruntime") is not None)

FRAME_SIZE = (360, 640)  # height, width
BOTTOM_BOX = ((120.0, 300.0), (500.0, 300.0), (500.0, 340.0), (120.0, 340.0))
CENTER_BOX = ((40.0, 100.0), (300.0, 100.0), (300.0, 125.0), (40.0, 125.0))
TITLE_BOX = ((60.0, 30.0), (560.0, 30.0), (560.0, 90.0), (60.0, 90.0))


def fake_ffmpeg_frames(pattern: Path, count: int) -> None:
    """假 ffmpeg 产物：写真实 PNG，保证宽高可读（分类依赖帧尺寸）。"""
    from PIL import Image

    pattern = Path(pattern)
    pattern.parent.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        target = pattern.parent / pattern.name.replace("%06d", f"{index:06d}")
        Image.new("RGB", (FRAME_SIZE[1], FRAME_SIZE[0]),
                  (30 + index, 30, 30)).save(target)


class FakeResult:
    """模拟 RapidOCR 的返回对象：boxes / txts / scores 三件套。"""

    def __init__(self, items):
        self.boxes = [item["box"] for item in items]
        self.txts = tuple(item["text"] for item in items)
        self.scores = tuple(item["score"] for item in items)


class FakeEngine:
    """按帧文件名返回预设识别结果，模拟 RapidOCR（未识别到文字时返回 None）。"""

    def __init__(self, mapping):
        self.mapping = mapping

    def __call__(self, image_path):
        items = self.mapping.get(Path(image_path).name)
        return FakeResult(items) if items else None


def engine_loader(mapping):
    return lambda: FakeEngine(mapping)


class ExtractFramesTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.video = self.root / "v.mp4"
        self.video.write_bytes(b"video")
        self.frames = self.root / "frames"

    def _extract(self, *, duration=60.0, policy=None, windows=None, calls=None):
        calls = calls if calls is not None else []

        def runner(argv, **kwargs):
            calls.append(argv)
            fake_ffmpeg_frames(Path(argv[-1]), 3)
            return mock.Mock(stdout="", stderr="", returncode=0)

        frames = extract_adaptive_frames(
            self.video, self.frames, policy or FramePolicy(), windows=windows,
            duration=duration, runner=runner,
            probe=lambda path: duration)
        return frames, calls


class FramePolicyTests(ExtractFramesTestCase):
    def test_base_scan_uses_one_fps(self):
        frames, calls = self._extract()
        joined = " ".join(" ".join(argv) for argv in calls)
        self.assertIn("fps=1", joined)
        self.assertTrue(any(f.source == "base" for f in frames))
        self.assertTrue(all(f.path.is_file() for f in frames))

    def test_scene_frames_use_threshold_and_showinfo(self):
        _, calls = self._extract()
        scene = [argv for argv in calls
                 if any("gt(scene,0.25)" in str(part) for part in argv)]
        self.assertTrue(scene, "必须有一次带场景阈值的 ffmpeg 调用")
        self.assertTrue(any("showinfo" in str(part) for part in scene[0]))

    def test_scene_timestamps_are_parsed_from_showinfo(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            out = Path(argv[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            if "scene" in out.name:
                for index in range(1, 3):
                    (out.parent / out.name.replace("%06d", f"{index:06d}")
                     ).write_bytes(b"png")
                stderr = ("[Parsed_showinfo_1 @ 0] n:   0 pts:    120 "
                          "pts_time:2.5   pos: 1 fmt:yuv420p\n"
                          "[Parsed_showinfo_1 @ 0] n:   1 pts:    240 "
                          "pts_time:4.75  pos: 2 fmt:yuv420p\n")
                return mock.Mock(stdout="", stderr=stderr, returncode=0)
            for index in range(1, 4):
                (out.parent / out.name.replace("%06d", f"{index:06d}")
                 ).write_bytes(b"png")
            return mock.Mock(stdout="", stderr="", returncode=0)

        frames = extract_adaptive_frames(
            self.video, self.frames, FramePolicy(), duration=10.0,
            runner=runner, probe=lambda path: 10.0)
        scene = [f for f in frames if f.source == "scene"]
        self.assertEqual([round(f.timestamp, 2) for f in scene], [2.5, 4.75])

    def test_subtitle_windows_sample_at_four_fps_with_crop(self):
        _, calls = self._extract(windows=[(2.0, 4.0)])
        subtitle_calls = [argv for argv in calls
                          if any("fps=4" in str(part) for part in argv)]
        self.assertEqual(len(subtitle_calls), 1)
        joined = " ".join(str(part) for part in subtitle_calls[0])
        self.assertIn("crop=", joined)
        self.assertIn("-ss", subtitle_calls[0])

    def test_subtitle_frames_are_tagged_with_region(self):
        frames, _ = self._extract(windows=[(2.0, 4.0)])
        subtitle = [f for f in frames if f.source == "subtitle"]
        self.assertTrue(subtitle)
        self.assertIsNotNone(subtitle[0].region)
        self.assertEqual(subtitle[0].region[0], 0)

    def test_long_video_frame_cap_is_respected(self):
        frames, _ = self._extract(duration=5_000.0, windows=[(0.0, 5_000.0)])
        self.assertLessEqual(len(frames), FramePolicy().max_frames)

    def test_long_video_keeps_scene_frames_and_lowers_subtitle_fps(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            fake_ffmpeg_frames(Path(argv[-1]), 2)
            return mock.Mock(stdout="", stderr="", returncode=0)

        frames = extract_adaptive_frames(
            self.video, self.frames, FramePolicy(), duration=5_000.0,
            windows=[(0.0, 5_000.0)], runner=runner, probe=lambda p: 5_000.0)
        joined = " ".join(" ".join(str(p) for p in argv) for argv in calls)
        self.assertIn("fps=2", joined)
        self.assertTrue(any(f.source == "scene" for f in frames))

    def test_missing_video_is_reported(self):
        with self.assertRaises(Exception):
            extract_adaptive_frames(self.root / "nope.mp4", self.frames,
                                    FramePolicy(), duration=10.0,
                                    runner=lambda *a, **k: None,
                                    probe=lambda p: 10.0)


class TextHelpersTests(unittest.TestCase):
    def test_normalize_text_strips_punctuation_and_spaces(self):
        self.assertEqual(normalize_text("端午 粽子，不分南北！"), "端午粽子不分南北")

    def test_similarity_of_identical_text_is_one(self):
        self.assertAlmostEqual(text_similarity("端午粽子", "端午粽子"), 1.0)

    def test_similarity_of_unrelated_text_is_low(self):
        self.assertLess(text_similarity("端午粽子", "参数2026"), 0.4)


class ClassifyTests(unittest.TestCase):
    def test_stable_bottom_text_is_speech_subtitle(self):
        self.assertEqual(classify_text("端午粽子", BOTTOM_BOX, FRAME_SIZE,
                                       stable_frames=2), "speech_subtitle")

    def test_bottom_text_with_matching_asr_is_speech_subtitle(self):
        self.assertEqual(classify_text("端午粽子", BOTTOM_BOX, FRAME_SIZE,
                                       stable_frames=2,
                                       asr_similarity=0.9), "speech_subtitle")

    def test_single_bottom_frame_is_not_a_subtitle(self):
        self.assertEqual(classify_text("端午粽子", BOTTOM_BOX, FRAME_SIZE,
                                       stable_frames=1),
                         "unknown_visual_text")

    def test_large_top_text_is_title_emphasis(self):
        self.assertEqual(classify_text("奶龙大侠", TITLE_BOX, FRAME_SIZE,
                                       stable_frames=3), "title_emphasis")

    def test_parameter_line_is_static_info(self):
        self.assertEqual(classify_text("参数 2026-06-19 1.2 GB", CENTER_BOX,
                                       FRAME_SIZE, stable_frames=4),
                         "static_info")

    def test_middle_plain_text_is_unknown(self):
        self.assertEqual(classify_text("今天天气不错", CENTER_BOX, FRAME_SIZE,
                                       stable_frames=4),
                         "unknown_visual_text")

    def test_parameter_text_in_bottom_band_is_static_info(self):
        # 参数行也可能落在下部 60% 区域：内容形态优先于位置
        self.assertEqual(classify_text("参数 2026-06-19 1.2 GB", BOTTOM_BOX,
                                       FRAME_SIZE, stable_frames=3),
                         "static_info")

    def test_parameter_text_with_matching_asr_is_subtitle(self):
        self.assertEqual(classify_text("参数 2026-06-19 1.2 GB", BOTTOM_BOX,
                                       FRAME_SIZE, stable_frames=3,
                                       asr_similarity=0.9), "speech_subtitle")

    def test_wide_multiline_text_is_screenshot(self):
        box = ((10.0, 60.0), (620.0, 60.0), (620.0, 300.0), (10.0, 300.0))
        self.assertEqual(classify_text("聊天记录\n张三：你好\n李四：在吗",
                                       box, FRAME_SIZE, stable_frames=3,
                                       line_count=3), "screenshot_text")


class MergeTests(unittest.TestCase):
    def _observation(self, start, end, text, kind="speech_subtitle",
                     confidence=0.9, bbox=BOTTOM_BOX):
        return OcrObservation(start=start, end=end, text=text, bbox=bbox,
                              confidence=confidence, kind=kind,
                              frame_hash=hashlib.sha1(text.encode()).hexdigest())

    def test_adjacent_similar_subtitles_merge_into_one_range(self):
        merged = merge_observations([
            self._observation(0.0, 1.0, "端午粽子"),
            self._observation(1.0, 2.0, "端午粽子"),
            self._observation(2.0, 3.0, "端午粽子。"),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0].start, merged[0].end), (0.0, 3.0))

    def test_dissimilar_text_is_not_merged(self):
        merged = merge_observations([
            self._observation(0.0, 1.0, "端午粽子"),
            self._observation(1.0, 2.0, "不分南北"),
        ])
        self.assertEqual(len(merged), 2)

    def test_gap_breaks_the_merge(self):
        merged = merge_observations([
            self._observation(0.0, 1.0, "端午粽子"),
            self._observation(5.0, 6.0, "端午粽子"),
        ])
        self.assertEqual(len(merged), 2)

    def test_different_kinds_never_merge(self):
        merged = merge_observations([
            self._observation(0.0, 1.0, "端午粽子"),
            self._observation(1.0, 2.0, "端午粽子", kind="static_info"),
        ])
        self.assertEqual(len(merged), 2)

    def test_merging_keeps_highest_confidence(self):
        merged = merge_observations([
            self._observation(0.0, 1.0, "端午粽子", confidence=0.7),
            self._observation(1.0, 2.0, "端午粽子", confidence=0.95),
        ])
        self.assertAlmostEqual(merged[0].confidence, 0.95)


class RunOcrTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.video = self.root / "v.mp4"
        self.video.write_bytes(b"video")
        self.frames = self.root / "frames"
        self.frames.mkdir()
        fake_ffmpeg_frames(self.frames / "base_%06d.jpg", 6)

    def _runner(self, calls):
        def runner(argv, **kwargs):
            calls.append(argv)
            out = Path(argv[-1])
            fake_ffmpeg_frames(out, 3 if "subtitle" in out.name else 6)
            return mock.Mock(stdout="", stderr="", returncode=0)
        return runner

    def _run(self, mapping, **kwargs):
        calls = []
        return run_ocr(self.video, self.frames, engine_loader(mapping),
                       duration=6.0, runner=self._runner(calls),
                       probe=lambda p: 6.0, **kwargs), calls

    def test_stable_bottom_subtitle_is_classified_and_merged(self):
        box = {"box": BOTTOM_BOX, "text": "端午粽子", "score": 0.93}
        mapping = {f"base_{i:06d}.jpg": [box] for i in range(1, 7)}
        outcome, _ = self._run(mapping)
        self.assertEqual(outcome.status, "ok")
        subtitles = [o for o in outcome.observations if o.kind == "speech_subtitle"]
        self.assertTrue(subtitles)
        self.assertEqual(subtitles[0].text, "端午粽子")
        self.assertGreater(subtitles[0].end - subtitles[0].start, 1.0)

    def test_subtitle_activates_extra_high_rate_sampling(self):
        box = {"box": BOTTOM_BOX, "text": "端午粽子", "score": 0.93}
        mapping = {f"base_{i:06d}.jpg": [box] for i in range(1, 7)}
        _, calls = self._run(mapping)
        self.assertTrue(any("fps=4" in " ".join(str(p) for p in argv)
                            for argv in calls),
                        "连续两帧稳定字幕后必须提高采样率")

    def test_subtitle_exit_stops_high_rate_sampling(self):
        # 只有前两帧有字幕：第三帧起退出，不应触发 4 fps 采样
        mapping = {
            "base_000001.jpg": [{"box": BOTTOM_BOX, "text": "端午粽子", "score": 0.9}],
            "base_000002.jpg": [],
            "base_000003.jpg": [],
        }
        _, calls = self._run(mapping)
        self.assertFalse(any("fps=4" in " ".join(str(p) for p in argv)
                             for argv in calls))

    def test_engine_failure_returns_failed_outcome(self):
        def boom():
            raise RuntimeError("engine missing")

        outcome = run_ocr(self.video, self.frames, boom, duration=6.0,
                          runner=self._runner([]), probe=lambda p: 6.0)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.observations, [])

    def test_missing_ocr_dependency_degrades_without_crash(self):
        """产品红线（P 级）：缺 OCR 依赖时必须走降级、给出可读标记而非崩栈。

        这条不依赖本机是否装 rapidocr，用替身 loader 恒定复现缺依赖场景，
        与真机端到端用例（E 级 skipif）配套。
        """
        def loader():
            raise ModuleNotFoundError("No module named 'rapidocr'")

        outcome = run_ocr(self.video, self.frames, loader, duration=6.0,
                          runner=self._runner([]), probe=lambda p: 6.0)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.observations, [])
        self.assertIn("ocr_engine_unavailable", outcome.degradations)

    def test_engine_exception_per_frame_is_degraded_not_raised(self):
        def loader():
            class Flaky:
                def __call__(self, path):
                    raise RuntimeError("boom")
            return Flaky()

        outcome = run_ocr(self.video, self.frames, loader, duration=6.0,
                          runner=self._runner([]), probe=lambda p: 6.0)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.observations, [])

    def test_static_parameter_text_is_not_speech_evidence(self):
        mapping = dict(
            {f"base_{i:06d}.jpg": [{"box": CENTER_BOX,
                                    "text": "参数 2026-06-19 1.2 GB",
                                    "score": 0.95}] for i in range(1, 7)})
        outcome, _ = self._run(mapping)
        kinds = {o.kind for o in outcome.observations}
        self.assertIn("static_info", kinds)
        self.assertNotIn("speech_subtitle", kinds)

    def test_asr_similarity_boosts_subtitle_classification(self):
        box = {"box": BOTTOM_BOX, "text": "端午粽子", "score": 0.9}
        mapping = {f"base_{i:06d}.jpg": [box] for i in range(1, 7)}
        outcome, _ = self._run(mapping, asr_segments=[
            {"start": 0.0, "end": 6.0, "text": "端午粽子"}])
        subtitle = next(o for o in outcome.observations
                        if o.kind == "speech_subtitle")
        self.assertGreaterEqual(subtitle.asr_similarity, 0.9)

    def test_empty_result_is_ok_with_no_observations(self):
        outcome, _ = self._run({})
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.observations, [])

    def test_one_text_stream_never_splits_into_two_kinds(self):
        box = {"box": BOTTOM_BOX, "text": "参数 2026-06-19 1.2 GB", "score": 0.9}
        mapping = {f"base_{i:06d}.jpg": [box] for i in range(1, 7)}
        outcome, _ = self._run(mapping)
        kinds = {o.kind for o in outcome.observations
                 if o.text.startswith("参数")}
        self.assertEqual(kinds, {"static_info"})

    def test_observations_carry_bbox_and_hash(self):
        box = {"box": BOTTOM_BOX, "text": "端午粽子", "score": 0.93}
        mapping = {f"base_{i:06d}.jpg": [box] for i in range(1, 7)}
        outcome, _ = self._run(mapping)
        observation = outcome.observations[0]
        self.assertEqual(len(observation.bbox), 4)
        self.assertTrue(observation.frame_hash)


class RapidOcrAdapterTests(unittest.TestCase):
    def test_engine_is_imported_lazily_inside_loader(self):
        source = Path(ocr_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from rapidocr import", source.split("def default_engine")[0])
        self.assertIn("def default_engine", source)

    def test_result_objects_are_converted_to_plain_observations(self):
        class Result:
            boxes = [((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))]
            txts = ("端午粽子",)
            scores = (0.91,)

        converted = ocr_module._convert_result(Result())
        self.assertEqual(converted[0]["text"], "端午粽子")
        self.assertAlmostEqual(converted[0]["score"], 0.91)
        self.assertEqual(len(converted[0]["box"]), 4)

    def test_none_result_is_empty(self):
        self.assertEqual(ocr_module._convert_result(None), [])

    def test_numpy_scalars_are_converted_to_plain_floats(self):
        """真实 RapidOCR 返回 numpy float32；直接入库会 TypeError（真实运行发现）。"""
        import json

        import numpy as np

        class Result:
            boxes = [((np.float32(0.0), np.float32(0.0)),
                      (np.float32(10.5), np.float32(0.0)),
                      (np.float32(10.5), np.float32(10.0)),
                      (np.float32(0.0), np.float32(10.0)))]
            txts = ("端午粽子",)
            scores = (np.float32(0.91),)

        converted = ocr_module._convert_result(Result())
        json.dumps(converted)          # 必须可序列化
        self.assertIsInstance(converted[0]["score"], float)
        self.assertIsInstance(converted[0]["box"][0][0], float)

    def test_observation_record_is_json_serializable(self):
        import json

        import numpy as np

        observation = OcrObservation(
            start=0.0, end=1.0, text="端午粽子",
            bbox=((np.float32(0.0), np.float32(300.0)),
                  (np.float32(400.0), np.float32(300.0))),
            confidence=np.float32(0.95), kind="speech_subtitle")
        json.dumps(observation.as_record())


class RealSyntheticVideoTests(unittest.TestCase):
    """真实 FFmpeg + 真实 RapidOCR 的端到端验证（无真实抖音样本依赖）。"""

    def setUp(self):
        from tests.fixtures.ocr import synthetic_video

        if not synthetic_video.ffmpeg_available():
            self.skipTest("缺少系统 FFmpeg")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.synthetic = synthetic_video
        self.video = synthetic_video.build_synthetic_video(self.root)

    @unittest.skipUnless(ocr_engine_available(),
                         "SKIP(E): 缺 OCR 引擎依赖（rapidocr + onnxruntime）")
    def test_real_video_yields_subtitle_and_static_observations(self):
        if not self.synthetic.font_available():
            self.skipTest("缺少中文字体")
        outcome = run_ocr(self.video, self.root / "frames",
                          ocr_module.default_engine, duration=6.0,
                          policy=FramePolicy(base_fps=1.0))
        self.assertEqual(outcome.status, "ok")
        self.assertTrue(outcome.observations, "真实合成视频必须至少识别到一条文字")
        kinds = {o.kind for o in outcome.observations}
        self.assertIn("speech_subtitle", kinds)
        # 参数行绝不能变成语音证据
        for observation in outcome.observations:
            if "参数" in observation.text:
                self.assertNotEqual(observation.kind, "speech_subtitle")
        self.assertTrue(any("粽子" in o.text or "南北" in o.text or "参数" in o.text
                            for o in outcome.observations),
                        f"识别结果异常：{[o.text for o in outcome.observations]}")


if __name__ == "__main__":
    unittest.main()
