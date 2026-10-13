"""asr.py 模型选择、片段归一化与时间标签测试（fake model，不加载真实权重）。"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import asr


def _seg(start, end, text, avg_logprob=-0.2, no_speech_prob=0.0, words=None):
    return SimpleNamespace(
        start=start, end=end, text=text,
        avg_logprob=avg_logprob, no_speech_prob=no_speech_prob,
        words=words or [],
    )


def _word(text, probability):
    return SimpleNamespace(word=text, probability=probability)


class FakeModel:
    def __init__(self, segments, language="zh"):
        self.segments = segments
        self.language = language
        self.calls = []

    def transcribe(self, path, **kwargs):
        self.calls.append(kwargs)
        info = SimpleNamespace(language=self.language)
        return iter(self.segments), info


class SelectProfileTests(unittest.TestCase):
    def test_cuda_profile(self):
        profile = asr.select_profile(True, 10)
        self.assertEqual((profile.model, profile.device, profile.compute_type),
                         ("small", "cuda", "float16"))

    def test_cuda_vram_6gb_or_more_uses_medium(self):
        # 对齐 B站策略：独显显存 ≥6GB → medium
        profile = asr.select_profile(True, 10, vram_gb=6.0)
        self.assertEqual((profile.model, profile.device, profile.compute_type),
                         ("medium", "cuda", "float16"))
        profile = asr.select_profile(True, 10, vram_gb=8.0)
        self.assertEqual(profile.model, "medium")

    def test_cuda_small_vram_keeps_small(self):
        profile = asr.select_profile(True, 10, vram_gb=4.0)
        self.assertEqual(profile.model, "small")

    def test_cpu_short_profile(self):
        profile = asr.select_profile(False, 1800)
        self.assertEqual((profile.model, profile.device, profile.compute_type),
                         ("base", "cpu", "int8"))

    def test_cpu_long_profile(self):
        profile = asr.select_profile(False, 1801)
        self.assertEqual((profile.model, profile.device, profile.compute_type),
                         ("tiny", "cpu", "int8"))

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"DOUYIN_WHISPER_MODEL": "medium"}):
            self.assertEqual(asr.select_profile(False, 10).model, "medium")
            self.assertEqual(asr.select_profile(True, 10).model, "medium")
            self.assertEqual(asr.select_profile(True, 10, vram_gb=6).model,
                             "medium")

    def test_env_override_does_not_change_device(self):
        with mock.patch.dict(os.environ, {"DOUYIN_WHISPER_MODEL": "large-v3"}):
            cpu = asr.select_profile(False, 10)
            self.assertEqual((cpu.device, cpu.compute_type), ("cpu", "int8"))


class VramDetectTests(unittest.TestCase):
    def test_parses_nvidia_smi_output(self):
        completed = mock.Mock(stdout="6144 MiB\n", returncode=0)
        with mock.patch.object(asr.subprocess, "run", return_value=completed):
            self.assertAlmostEqual(asr.gpu_vram_gb(), 6.0, places=1)

    def test_failure_returns_none(self):
        with mock.patch.object(asr.subprocess, "run",
                               side_effect=OSError("no nvidia-smi")):
            self.assertIsNone(asr.gpu_vram_gb())
        completed = mock.Mock(stdout="", returncode=1)
        with mock.patch.object(asr.subprocess, "run", return_value=completed):
            self.assertIsNone(asr.gpu_vram_gb())


class NvidiaDllDirTests(unittest.TestCase):
    def test_finds_bin_dirs_and_registers(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_a = root / "nvidia" / "cublas" / "bin"
            bin_a.mkdir(parents=True)
            (root / "nvidia" / "misc").mkdir(parents=True)
            found = asr._nvidia_bin_dirs(root)
            self.assertEqual([p.name for p in found], ["bin"])
            self.assertEqual(found[0].parent.name, "cublas")

    def test_registers_with_os_add_dll_directory(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "nvidia" / "cublas" / "bin"
            bin_dir.mkdir(parents=True)
            with mock.patch.object(asr.os, "add_dll_directory") as fake_add, \
                 mock.patch.object(asr.os, "name", "nt", create=True):
                asr._register_nvidia_dll_dirs(Path(tmp))
                fake_add.assert_called_once_with(str(bin_dir))

    def test_missing_tree_is_noop(self):
        # 产品红线（P 级）：DLL 目录树缺失时注册必须是安全的 no-op，不注册也不崩。
        # 用本地产生的确定 tmp 路径喂入，避免依赖本机磁盘拓扑（如失效的网络盘 Z:）。
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "definitely-missing-xyz"
            self.assertFalse(missing.exists())
            with mock.patch.object(asr.os, "add_dll_directory") as fake_add:
                asr._register_nvidia_dll_dirs(missing)
            fake_add.assert_not_called()


class TimeLabelTests(unittest.TestCase):
    def test_time_label(self):
        self.assertEqual(asr.time_label(83.2), "00:01:23")
        self.assertEqual(asr.time_label(0), "00:00:00")
        self.assertEqual(asr.time_label(3661.9), "01:01:01")


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audio = Path(self._tmp.name) / "speech.mp3"
        self.audio.write_bytes(b"fake")
        # 本测试类默认视为无 GPU 环境；GPU 分支单独 patch
        patcher = mock.patch.object(asr, "detect_cuda", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, segments, title="普通标题", language="zh", **kwargs):
        fake = FakeModel(segments, language=language)
        loader = mock.Mock(return_value=fake)
        result = asr.transcribe(self.audio, 100.0, title, loader=loader, **kwargs)
        return result, fake, loader

    def test_returns_text_segments_profile_language(self):
        (text, segs, profile, lang), fake, _ = self._run([
            _seg(0.0, 2.5, " 你好世界 "),
            _seg(2.5, 4.0, "第二句。"),
        ])
        self.assertEqual(text, "你好世界\n第二句。")
        self.assertEqual(lang, "zh")
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["text"], "你好世界")
        self.assertAlmostEqual(segs[0]["start"], 0.0)
        self.assertAlmostEqual(segs[0]["end"], 2.5)
        self.assertIn("avg_logprob", segs[0])
        self.assertFalse(segs[0]["needs_review"])
        self.assertEqual(segs[0]["review_reasons"], [])

    def test_blank_segments_dropped_and_all_blank_raises(self):
        (_, segs, _, _), _, _ = self._run([
            _seg(0.0, 1.0, "   "), _seg(1.0, 2.0, "有内容"),
        ])
        self.assertEqual(len(segs), 1)
        with self.assertRaises(asr.AsrError):
            self._run([_seg(0.0, 1.0, ""), _seg(1.0, 2.0, "  ")])

    def test_review_reasons(self):
        (_, segs, _, _), _, _ = self._run([
            _seg(0, 2, "低概率", avg_logprob=-1.5),
            _seg(2, 4, "低 置信 词 测试", words=[_word("嗯", 0.2), _word("啊", 0.3)]),
            _seg(4, 6, "啊啊啊啊啊啊", words=[_word("啊", 0.9)] * 6),
            _seg(6, 8, "疑似静音", no_speech_prob=0.9),
        ])
        self.assertIn("low_avg_logprob", segs[0]["review_reasons"])
        self.assertIn("low_confidence_words", segs[1]["review_reasons"])
        self.assertEqual(segs[1]["low_confidence_words"], ["嗯", "啊"])
        self.assertIn("repetitive", segs[2]["review_reasons"])
        self.assertIn("high_silence_prob", segs[3]["review_reasons"])
        self.assertTrue(all(s["needs_review"] for s in segs))
        self.assertFalse(segs[0]["needs_review"] and False)

    def test_chinese_title_forces_zh_language(self):
        _, fake, loader = self._run([_seg(0, 2, "你好")], title="中文标题作品")
        kwargs = fake.calls[0]
        self.assertEqual(kwargs.get("language"), "zh")
        self.assertTrue(kwargs.get("vad_filter"))
        self.assertEqual(kwargs.get("beam_size"), 5)
        # 生产路径不真正导入 faster_whisper；loader 被调用且带 profile
        profile = loader.call_args.args[0]
        self.assertEqual(profile.device, "cpu")  # detect 被测试环境 mock

    def test_non_chinese_title_uses_auto_language(self):
        _, fake, _ = self._run([_seg(0, 2, "hello")], title="English title",
                               language="en")
        self.assertIsNone(fake.calls[0].get("language"))

    def test_gpu_lock_held_only_for_cuda(self):
        lock = mock.MagicMock()
        with mock.patch.object(asr, "detect_cuda", return_value=True), \
             mock.patch.object(asr, "GpuLock", return_value=lock) as lock_cls:
            self._run([_seg(0, 2, "你好")], title="中文")
        lock_cls.assert_called_once()
        lock.__enter__.assert_called_once()
        lock.__exit__.assert_called_once()
        # CPU 推理不持锁
        with mock.patch.object(asr, "GpuLock") as lock_cls:
            self._run([_seg(0, 2, "hi")], title="t")
        lock_cls.assert_not_called()


    def test_selection_reason_is_logged(self):
        # 继承 B站版长处：模型档位选择必须对用户可观测
        with self.assertLogs("douyin_transcript.asr", level="INFO") as captured:
            self._run([_seg(0, 2, "你好")], title="中文")
        text = "\n".join(captured.output)
        self.assertIn("base", text)
        self.assertTrue("CPU" in text or "cpu" in text)


class DetectCudaTests(unittest.TestCase):
    def test_requires_device_and_float16(self):
        with mock.patch("ctranslate2.get_cuda_device_count", return_value=0):
            self.assertFalse(asr.detect_cuda())
        with mock.patch("ctranslate2.get_cuda_device_count", return_value=1), \
             mock.patch("ctranslate2.get_supported_compute_types",
                        return_value={"float32", "int8"}):
            self.assertFalse(asr.detect_cuda())
        with mock.patch("ctranslate2.get_cuda_device_count", return_value=1), \
             mock.patch("ctranslate2.get_supported_compute_types",
                        return_value={"float32", "float16", "int8"}), \
             mock.patch.object(asr, "cuda_runtime_available", return_value=True):
            self.assertTrue(asr.detect_cuda())

    def test_ctranslate2_failure_is_not_cuda(self):
        with mock.patch("ctranslate2.get_cuda_device_count",
                        side_effect=RuntimeError("boom")):
            self.assertFalse(asr.detect_cuda())

    def test_missing_cublas_runtime_disables_cuda(self):
        # CT2 声称支持 float16，但本机没有 CUDA 12 运行时 DLL：
        # 真机实测会抛 RuntimeError: cublas64_12.dll not found，必须回退 CPU
        with mock.patch("ctranslate2.get_cuda_device_count", return_value=1), \
             mock.patch("ctranslate2.get_supported_compute_types",
                        return_value={"float32", "float16", "int8"}), \
             mock.patch.object(asr, "cuda_runtime_available", return_value=False):
            self.assertFalse(asr.detect_cuda())

    def test_runtime_probe_loads_real_dll(self):
        # 探测函数本身必须走 ctypes，而不是永远返回固定值
        with mock.patch("ctypes.CDLL") as fake_cdll:
            fake_cdll.return_value = mock.Mock()
            asr.cuda_runtime_available()
            self.assertTrue(fake_cdll.called)
        with mock.patch("ctypes.CDLL",
                        side_effect=OSError("cannot find module")):
            self.assertFalse(asr.cuda_runtime_available())


class GpuLockTests(unittest.TestCase):
    def test_lock_blocks_second_holder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = Path(tmp) / "gpu.lock"
            first = asr.GpuLock(lock_file)
            first.acquire()
            second = asr.GpuLock(lock_file)
            with self.assertRaises(asr.GpuLockTimeout):
                second.acquire(timeout_seconds=0.2)
            first.release()
            second.acquire(timeout_seconds=1)
            second.release()


if __name__ == "__main__":
    unittest.main()
