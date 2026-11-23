"""音频分析与自适应音轨：只在需要时才生成增强/分离音轨。"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import audio_pipeline
from scripts.audio_pipeline import (AudioAnalysis, AudioPipelineError,
                                    analyze_audio, create_enhanced,
                                    create_vocals)


def ffprobe_output(*, duration=71.2, sample_rate=44100, channels=2):
    return json.dumps({
        "streams": [{"codec_type": "audio", "sample_rate": str(sample_rate),
                     "channels": channels, "codec_name": "aac"}],
        "format": {"duration": str(duration), "bit_rate": "128000"},
    })


def ebur128_output(lufs="-16.5", peak="-1.2"):
    """真实 ffmpeg 输出：先逐帧打印 I/FTPK，末尾才是 Summary。"""
    return (
        "[Parsed_ebur128_0 @ 0000] t: 0.099977  TARGET:-23 LUFS    "
        "M: -70.0 S: -70.0     I: -70.0 LUFS       "
        "LRA:   0.0 LU  FTPK: -70.0 -70.0 dBFS  TPK: -70.0 -70.0 dBFS\n"
        "[Parsed_ebur128_0 @ 0000] t: 71.099977  TARGET:-23 LUFS    "
        "M: -12.7 S: -10.4     I: -70.0 LUFS       "
        "LRA:   2.6 LU  FTPK: -4.9 -3.8 dBFS  TPK:  -0.0   0.0 dBFS\n"
        "[Parsed_ebur128_0 @ 0000] Summary:\n\n"
        "  Integrated loudness:\n"
        f"    I:         {lufs} LUFS\n"
        "    Threshold: -26.5 LUFS\n\n"
        "  Loudness range:\n"
        "    LRA:         2.6 LU\n"
        "    LRA low:   -12.5 LUFS\n"
        "    LRA high:   -9.8 LUFS\n\n"
        "  True peak:\n"
        f"    Peak:      {peak} dBFS\n"
    )


def silencedetect_output(silence=0.05):
    return (
        f"[silencedetect @ 0000] silence_start: 0\n"
        f"[silencedetect @ 0000] silence_end: {silence} | silence_duration: {silence}\n"
    )


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audio = Path(self._tmp.name) / "speech.m4a"
        self.audio.write_bytes(b"audio-bytes")

    def _run(self, *, probe=ffprobe_output(), loudness=ebur128_output(),
             silence=silencedetect_output(), side_effect=None):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if side_effect is not None:
                return side_effect(argv)
            if "ffprobe" in argv[0]:
                return mock.Mock(stdout=probe, stderr="", returncode=0)
            if "-af" in argv and any("ebur128" in part for part in argv):
                return mock.Mock(stdout="", stderr=loudness, returncode=0)
            return mock.Mock(stdout="", stderr=silence, returncode=0)

        with mock.patch("scripts.audio_pipeline.subprocess.run",
                        side_effect=fake_run):
            analysis = analyze_audio(self.audio)
        return analysis, calls

    def test_analysis_reports_core_measurements(self):
        analysis, _ = self._run()
        self.assertEqual(analysis.duration, 71.2)
        self.assertAlmostEqual(analysis.loudness_lufs, -16.5)
        self.assertAlmostEqual(analysis.peak_db, -1.2)
        self.assertAlmostEqual(analysis.silence_ratio, 0.05 / 71.2, places=4)
        self.assertEqual(analysis.sample_rate, 44100)
        self.assertEqual(analysis.channels, 2)

    def test_analysis_is_frozen_dataclass(self):
        analysis, _ = self._run()
        self.assertIsInstance(analysis, AudioAnalysis)
        with self.assertRaises(Exception):
            analysis.duration = 1.0

    def test_missing_audio_raises(self):
        with self.assertRaises(AudioPipelineError) as ctx:
            analyze_audio(Path(self._tmp.name) / "nope.m4a")
        self.assertEqual(ctx.exception.code, "audio_invalid")

    def test_unparsable_probe_output_raises(self):
        with self.assertRaises(AudioPipelineError) as ctx:
            self._run(probe="not json")
        self.assertEqual(ctx.exception.code, "audio_invalid")

    def test_summary_wins_over_per_frame_lines(self):
        # 首帧静音时逐帧行会给 -70 LUFS；不能把整段判成异常安静
        analysis, _ = self._run()
        self.assertAlmostEqual(analysis.loudness_lufs, -16.5)
        self.assertFalse(analysis.needs_enhancement)

    def test_per_frame_only_output_falls_back_to_last_values(self):
        analysis, _ = self._run(loudness=(
            "[Parsed_ebur128_0 @ 0000] I: -70.0 LUFS  Peak: 0.0 dBFS\n"
            "[Parsed_ebur128_0 @ 0000] I: -18.2 LUFS  Peak: -1.5 dBFS\n"))
        self.assertAlmostEqual(analysis.loudness_lufs, -18.2)

    def test_commands_are_argument_arrays_without_shell(self):
        _, calls = self._run()
        self.assertTrue(calls)
        for argv in calls:
            self.assertIsInstance(argv, list)
            self.assertIsInstance(argv[0], str)


class EnhancedVariantTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.audio = self.root / "speech.m4a"
        self.audio.write_bytes(b"audio-bytes")
        self.job_dir = self.root / "job"
        (self.job_dir / "derived").mkdir(parents=True)

    def _analysis(self, **overrides) -> AudioAnalysis:
        values = dict(duration=71.2, loudness_lufs=-16.5, peak_db=-1.2,
                      clipped_ratio=0.0, silence_ratio=0.05, sample_rate=44100,
                      channels=2)
        values.update(overrides)
        return AudioAnalysis(**values)

    def _patch_ffmpeg(self, verify=True):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "ffmpeg" and "-i" in argv and "ffprobe" not in argv[0]:
                # 模拟生成产物
                target = Path(argv[-1])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"wav")
            return mock.Mock(stdout="", stderr="", returncode=0)

        patch = mock.patch("scripts.audio_pipeline.subprocess.run",
                           side_effect=fake_run)
        probe_patch = mock.patch.object(audio_pipeline, "verify_audio",
                                        return_value=71.2 if verify else 0.0)
        return patch, probe_patch, calls

    def test_clear_audio_creates_no_enhanced_file(self):
        patch, probe_patch, _ = self._patch_ffmpeg()
        with patch, probe_patch:
            variant = create_enhanced(self.audio, self.job_dir,
                                      analysis=self._analysis())
        self.assertIsNone(variant)

    def test_quiet_audio_produces_enhanced_wav_with_expected_filters(self):
        patch, probe_patch, calls = self._patch_ffmpeg()
        with patch, probe_patch:
            variant = create_enhanced(
                self.audio, self.job_dir,
                analysis=self._analysis(loudness_lufs=-38.0))
        self.assertIsNotNone(variant)
        self.assertEqual(variant.kind, "enhanced")
        self.assertTrue(variant.path.is_file())
        self.assertEqual(variant.path.parent, self.job_dir / "derived")
        argv = next(c for c in calls if c[0] == "ffmpeg")
        joined = " ".join(argv)
        self.assertIn("loudnorm", joined)
        self.assertIn("16000", joined)
        self.assertIn("-ac", argv)
        self.assertIn("1", argv)
        self.assertIn("highpass", joined)
        self.assertIn("lowpass", joined)

    def test_clipped_audio_triggers_enhancement(self):
        patch, probe_patch, _ = self._patch_ffmpeg()
        with patch, probe_patch:
            variant = create_enhanced(self.audio, self.job_dir,
                                      analysis=self._analysis(clipped_ratio=0.02))
        self.assertIsNotNone(variant)

    def test_enhanced_output_is_reverified(self):
        patch, probe_patch, _ = self._patch_ffmpeg()
        with patch, probe_patch as verify:
            create_enhanced(self.audio, self.job_dir,
                            analysis=self._analysis(loudness_lufs=-38.0))
        self.assertTrue(verify.called)

    def test_invalid_enhanced_output_raises_and_cleans(self):
        patch, probe_patch, _ = self._patch_ffmpeg(verify=False)
        with patch, probe_patch:
            with self.assertRaises(AudioPipelineError) as ctx:
                create_enhanced(self.audio, self.job_dir,
                                analysis=self._analysis(loudness_lufs=-38.0))
        self.assertEqual(ctx.exception.code, "audio_enhancement_failed")
        derived = self.job_dir / "derived"
        self.assertEqual([p.name for p in derived.iterdir()], [])

    def test_analysis_is_measured_when_not_supplied(self):
        patch, probe_patch, _ = self._patch_ffmpeg()
        with patch, probe_patch, \
             mock.patch.object(audio_pipeline, "analyze_audio",
                               return_value=self._analysis(loudness_lufs=-40.0)) as analyze:
            variant = create_enhanced(self.audio, self.job_dir)
        analyze.assert_called_once()
        self.assertEqual(variant.kind, "enhanced")


class VocalsVariantTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.audio = self.root / "speech.m4a"
        self.audio.write_bytes(b"audio-bytes")
        self.job_dir = self.root / "job"
        (self.job_dir / "derived").mkdir(parents=True)
        self.model = self.root / "uvr.onnx"
        self.model.write_bytes(b"fake-model")

    def _fake_separator(self, *, stems=2, monkey=None):
        class FakeSamples:
            def __init__(self, data):
                self.data = data

        class FakeOutput:
            def __init__(self, count):
                import numpy as np

                self.sample_rate = 44100
                self.stems = [FakeSamples(np.zeros((1, 44100), dtype="float32"))
                              for _ in range(count)]

        class FakeSeparator:
            def __init__(self, config):
                self.config = config

            @staticmethod
            def process(sample_rate, samples):
                if monkey is not None:
                    raise monkey
                return FakeOutput(stems)

        return FakeSeparator

    def test_vocals_variant_is_written_into_derived(self):
        import wave

        def fake_write(path, samples, sample_rate):
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                handle.writeframes(b"\x00\x00" * 100)

        # 产品红线（P 级）：分离成功时人声轨必须写进 <job>/derived。
        # 用替身喂确定输入：连 _uvr_config 一起桩掉，避免 import sherpa_onnx，
        # 使断言只考验收尾逻辑，不再依赖本机是否装人声分离依赖。
        with mock.patch.object(audio_pipeline, "_load_mono_samples",
                               return_value=([0.0] * 16000, 16000)), \
             mock.patch.object(audio_pipeline, "_write_wav", side_effect=fake_write), \
             mock.patch.object(audio_pipeline, "verify_audio", return_value=71.2), \
             mock.patch.object(audio_pipeline, "_uvr_config", return_value=object()), \
             mock.patch.object(audio_pipeline, "_separator_class",
                               return_value=self._fake_separator()):
            variant = create_vocals(self.audio, self.job_dir, model_path=self.model)
        self.assertEqual(variant.kind, "vocals")
        self.assertEqual(variant.path.parent, self.job_dir / "derived")
        self.assertEqual(variant.generator, "sherpa-uvr")

    def test_missing_model_returns_degraded_variant(self):
        variant = create_vocals(self.audio, self.job_dir,
                                model_path=self.root / "missing.onnx")
        self.assertIsNotNone(variant)
        self.assertFalse(variant.usable)
        self.assertEqual(variant.generator, "sherpa-uvr")

    def test_inference_failure_degrades_instead_of_raising(self):
        with mock.patch.object(audio_pipeline, "_load_mono_samples",
                               return_value=([0.0] * 16000, 16000)), \
             mock.patch.object(audio_pipeline, "_separator_class",
                               return_value=self._fake_separator(
                                   monkey=RuntimeError("boom"))):
            variant = create_vocals(self.audio, self.job_dir,
                                    model_path=self.model)
        self.assertFalse(variant.usable)
        self.assertIsNone(variant.path)

    def test_verify_failure_degrades(self):
        with mock.patch.object(audio_pipeline, "_load_mono_samples",
                               return_value=([0.0] * 16000, 16000)), \
             mock.patch.object(audio_pipeline, "_separator_class",
                               return_value=self._fake_separator()), \
             mock.patch.object(audio_pipeline, "_write_wav"), \
             mock.patch.object(audio_pipeline, "verify_audio", return_value=0.0):
            variant = create_vocals(self.audio, self.job_dir,
                                    model_path=self.model)
        self.assertFalse(variant.usable)


class VariantSelectionTests(unittest.TestCase):
    def test_original_is_always_the_default_candidate(self):
        self.assertEqual(audio_pipeline.original_variant(Path("a.m4a")).kind,
                         "original")

    def test_unusable_variants_are_not_candidates(self):
        variants = [audio_pipeline.original_variant(Path("a.m4a")),
                    audio_pipeline.degraded_variant("vocals", "模型缺失")]
        usable = audio_pipeline.usable_variants(variants)
        self.assertEqual([v.kind for v in usable], ["original"])


if __name__ == "__main__":
    unittest.main()
