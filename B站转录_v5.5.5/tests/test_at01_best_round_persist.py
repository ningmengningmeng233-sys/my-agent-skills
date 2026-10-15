"""AT-01 回归：多轮 ASR 的落盘必须等于选中的最佳轮，而非最后一轮。

原缺陷：每轮写同一路径，磁盘上留的是最后一轮；内存里选中的最佳轮从不
回写。若最后一轮为空，还会删掉已选有效结果的文件。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import asr_runner
from scripts.asr_types import AsrStrategy
from tests.test_asr_runner import FakeWhisperModel, fake_segments

SKILL_DIR = Path(__file__).resolve().parent.parent
TRANSCRIPT_SH = SKILL_DIR / "scripts" / "bilibili_transcript.sh"

INITIAL = {"model": "small", "device": "cpu", "compute_type": "int8",
           "language": "zh"}
STRATEGY = AsrStrategy(model="small", device="cpu", compute_type="int8",
                       language="zh")


class BestRoundPersistTests(unittest.TestCase):
    """假轮次评分 50/80/40：三轮全跑完，最佳轮是第 2 轮。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="at01_"))
        self.audio = self.tmp / "bilibili_audio.wav"
        self.audio.write_bytes(b"fake")

    def _run_three_rounds(self, texts, scores):
        seq = [fake_segments(text=t, count=2) if t is not None else []
               for t in texts]
        state = {"i": 0}

        def loader(*args, **kwargs):
            segs = seq[min(state["i"], len(seq) - 1)]
            state["i"] += 1
            return FakeWhisperModel(segments=segs)

        reports = [SimpleNamespace(score=s, passed=False) for s in scores]
        with mock.patch.object(asr_runner, "evaluate_quality",
                               side_effect=reports), \
                mock.patch.object(asr_runner, "next_strategy",
                                  side_effect=[STRATEGY, STRATEGY, None]):
            return asr_runner.run_rounds(
                self.audio, self.tmp, loader=loader,
                initial=dict(INITIAL), max_rounds=3)

    def test_at_01_contract(self):
        outcome = self._run_three_rounds(
            ["第一轮文本", "第二轮最佳", "第三轮文本"], [50.0, 80.0, 40.0])
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["rounds"], 3)
        selected = outcome["selected"]
        self.assertEqual(selected["round"], 2)
        self.assertEqual(outcome["score"], 80.0)
        txt = Path(selected["txt"])
        self.assertTrue(txt.is_file(), "最佳轮落盘 TXT 缺失")
        body = txt.read_text(encoding="utf-8")
        self.assertIn("第二轮最佳", body)
        self.assertNotIn("第三轮文本", body)
        expected = "\n".join(s["text"] for s in selected["segments"]) + "\n"
        self.assertEqual(body, expected, "落盘 TXT 必须等于最佳轮正文")
        sidecar = json.loads(
            Path(selected["segments_file"]).read_text(encoding="utf-8"))
        self.assertEqual([s["text"] for s in sidecar["segments"]],
                         [s["text"] for s in selected["segments"]],
                         "分段侧车必须等于最佳轮分段")

    def test_empty_last_round_does_not_delete_best(self):
        """末轮空结果不得删除已选有效结果的落盘。"""
        outcome = self._run_three_rounds(
            ["第一轮文本", "第二轮最佳", None], [50.0, 80.0, 0.0])
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["rounds"], 3)
        selected = outcome["selected"]
        self.assertEqual(selected["round"], 2)
        txt = Path(selected["txt"])
        self.assertTrue(txt.is_file(), "末轮空结果删掉了最佳轮的 TXT")
        body = txt.read_text(encoding="utf-8")
        self.assertIn("第二轮最佳", body)
        self.assertTrue(Path(selected["segments_file"]).is_file(),
                        "末轮空结果删掉了最佳轮的分段侧车")


class RunnerJsonWiringTests(unittest.TestCase):
    """bash 以执行器 JSON 报告的实际落盘路径为准，约定路径仅作兜底。"""

    def test_bash_prefers_runner_json_paths(self):
        text = TRANSCRIPT_SH.read_text(encoding="utf-8")
        self.assertIn("get('txt'", text)
        self.assertIn("get('segments_file'", text)
        gate = text.index('TRANSCRIPT_TEXT=$(cat "$TXT_FILE")')
        self.assertLess(text.index("get('txt'"), gate,
                        "JSON 路径解析必须在消费 TXT 之前")


if __name__ == "__main__":
    unittest.main()
