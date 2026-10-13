"""主流水线的跨组件契约测试。

这些断言锁住"改了 A 就忘了改 B"的接口：bash 与 Python 之间的元数据契约、
TXT 与分段侧车的同名契约、可选校正阶段的启用开关与执行顺序。
它们不跑完整流水线（那需要联网），但能在编辑脚本时立刻发现断链。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from scripts import import_archive, migrations, record_transcript  # noqa: F401

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SKILL_DIR / "scripts" / "bilibili_transcript.sh"
META_KEYS = {"bvid", "page", "cid", "part_title", "url", "title", "author",
             "duration", "upload_date", "transcript_source"}


class BashSyntaxTests(unittest.TestCase):
    def test_script_is_syntactically_valid(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("缺少 bash")
        result = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, result.stderr)


class PipelineWiringTests(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8", errors="replace")

    def test_first_step_calls_record_transcript(self):
        """写库必须走唯一入口，不能回到内联 upsert。"""
        self.assertIn("record_transcript.py", self.text)
        self.assertNotIn("db.insert(", self.text)

    def test_record_transcript_receives_meta_and_text(self):
        for flag in ("--meta", "--text-file", "--bvid", "--page"):
            self.assertIn(flag, self.text)
        self.assertIn("SEGMENTS_ARG", self.text)
        self.assertIn("--segments", self.text)

    def test_meta_keys_written_by_bash_match_the_reader(self):
        """bash 写进 meta.json 的键，必须与 record_transcript 读取的键一致。"""
        match = re.search(r"data = \{(?P<body>.*?)\n\}", self.text, re.S)
        self.assertIsNotNone(match, "未找到 bash 里的元数据构造片段")
        written = set(re.findall(r"'([a-z_]+)':", match.group("body")))
        self.assertEqual(written, META_KEYS,
                         f"bash 写的键 {written} 与约定不符")

    def test_segments_sidecar_path_matches_the_writer(self):
        """bash 由 TXT 推导侧车路径，必须与 fw_whisper_main 的命名一致。"""
        self.assertIn('SEGMENTS_FILE="${TXT_FILE%.txt}.segments.json"', self.text)
        from scripts.fw_whisper_main import write_segments_sidecar
        import inspect

        source = inspect.getsource(write_segments_sidecar)
        self.assertIn('with_suffix(".segments.json")', source)

    def test_optional_correction_stage_is_env_gated(self):
        self.assertIn("BILI_EVIDENCE_CORRECTION", self.text)
        self.assertIn('"$EVIDENCE_CORRECTION" = "true"', self.text)
        self.assertIn("correct_with_evidence.py", self.text)
        self.assertIn("--download", self.text)

    def test_correction_runs_after_render_and_rerenders_on_success(self):
        """校正排在渲染之后，因此成功后必须重渲染，交付文件才会反映最优文本。"""
        render_at = self.text.index("# ===== 第三步：从DB渲染TXT")
        correction_at = self.text.index("# ===== 第四步（可选）")
        self.assertLess(render_at, correction_at, "校正阶段应在渲染之后")
        hook = self.text[correction_at:]
        self.assertIn("render_delivery.py", hook, "校正成功后缺少重渲染")
        self.assertIn("重渲染", hook)


class AsrRunnerWiringTests(unittest.TestCase):
    """多轮转写的接线契约：执行器存在、被调用、且失败时能退回单轮。"""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8", errors="replace")

    def test_whisper_path_calls_the_runner(self):
        self.assertIn("asr_runner.py", self.text)
        self.assertIn("--max-rounds", self.text)

    def test_multi_round_is_env_gated_with_fallback(self):
        """可关闭多轮；执行器整体失败时必须退回单轮，不能比改造前更差。"""
        self.assertIn("BILI_WHISPER_ROUNDS", self.text)
        self.assertIn("fw_whisper_main.py", self.text,
                      "缺少单轮兜底路径")

    def test_runner_stderr_does_not_pollute_the_state_file(self):
        """stdout 是 JSON；stderr 若混入会让状态文件无法解析。"""
        self.assertIn(".asr_runner.stderr", self.text)

    def test_script_does_not_toggle_errexit(self):
        """脚本未启用 set -e，任何 set +e / set -e 都会改变后续所有命令的语义。"""
        self.assertNotIn("set -e", self.text)
        self.assertNotIn("set +e", self.text)

    def test_runner_output_names_match_what_bash_reads(self):
        """执行器写出的 TXT 名必须与 bash 推导的一致（否则找不到结果）。"""
        self.assertIn('TXT_FILE="${OUTPUT_DIR}/bilibili_audio.txt"', self.text)
        from scripts import asr_runner
        import inspect

        source = inspect.getsource(asr_runner._write_results)
        self.assertIn('Path(audio).stem + ".txt"', source)

    def test_correction_failure_does_not_abort_the_run(self):
        """校正失败只提示，不改动库，也不让整条流水线失败。"""
        hook = self.text[self.text.index("# ===== 第四步（可选）"):]
        self.assertIn("证据校正未完成", hook)

    def test_txt_and_sidecar_are_cleaned_after_write(self):
        """写库后清理中转文件；侧车只在写库前使用，不留在 output。"""
        match = re.search(r'rm -f "\$META_JSON".*', self.text)
        self.assertIsNotNone(match)
        self.assertIn("$TXT_FILE", match.group(0))
        self.assertIn("$SEGMENTS_FILE", match.group(0))


class EntryPointTests(unittest.TestCase):
    def test_correct_entry_supports_download_flag(self):
        source = (SKILL_DIR / "scripts" / "correct_with_evidence.py").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn('"--download"', source)
        self.assertIn("def download_video", source)

    def test_render_delivery_and_record_transcript_exist(self):
        for name in ("render_delivery.py", "record_transcript.py",
                     "correct_with_evidence.py", "migrations.py", "doctor.py"):
            self.assertTrue((SKILL_DIR / "scripts" / name).is_file(), name)

    def test_migrations_constant_matches_expected_schema(self):
        """schema 版本常量是各处的锚点，改动必须显式。"""
        self.assertGreaterEqual(migrations.SCHEMA_VERSION, 4)


if __name__ == "__main__":
    unittest.main()
