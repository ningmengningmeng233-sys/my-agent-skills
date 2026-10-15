"""Task 10 guard：端到端固定夹具验收——解析 → 装箱 → 验证 → 渲染全链。

证明四件事（设计 §C 覆盖矩阵）：
1. 不固定六组：120 秒夹具动态产生 ≥12 个片段（由内容决定，测试不硬指定组数）；
2. 时长不缩水：原问题反例 `[5,15,12,13,15,8]` 对 120 秒目标必须 FAIL 并报告少 52 秒
   （`PLANNED_DURATION_UNDERSHOOT` + `overage_seconds == -52.0`）；
3. 格式按路由 / 权威目标接线（Task 9 评审追加义务）：管线把 `parse_script_duration`
   解析的剧本声明作为 `authoritative_target_seconds` 传入门禁与渲染器——计划私自把
   申报目标改小（120→100）会被 `SCRIPT_TARGET_MISMATCH` 拦截；
4. 超 20% 暂停：episode-120-over20 夹具渲染 rc=2，只出审查摘要与机器清单。

每条中文短剧提示词都通过真实 `audit_cli`（渲染器内部串联）；真实媒体字段保持为空，
计划数字不得抄入 `actual_generated_seconds`/`final_edit_seconds`。

夹具：`_审查测试/fixtures/episode-{30,60,90,120,120-over20}.json`——
字段：episode_id / workflow / model / script（含时长声明）/ beats（Agent 提取的原子节拍）。

运行（目录名不是合法 Python 标识符，禁止 `-m unittest` 模块路径）：

    python "minimax-h3-craft/_审查测试/test_episode_pipeline.py" -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from duration_planner import KIND_ACTION, KIND_DIALOGUE, Beat, pack_beats  # noqa: E402
from episode_contract import (  # noqa: E402
    PLANNED_DURATION_UNDERSHOOT,
    SCRIPT_TARGET_MISMATCH,
    STATUS_PASS_WITH_TRIM,
    validate_episode_plan,
)
from render_episode_pack import EXIT_RENDERED, EXIT_REVIEW_REQUIRED, render_episode_pack  # noqa: E402
from script_duration import parse_script_duration  # noqa: E402
from versioning import load_duration_policy  # noqa: E402

FIXTURES = SKILL_ROOT / "_审查测试" / "fixtures"
POLICY = load_duration_policy()


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def fixture_beats(fixture: dict) -> list:
    return [
        Beat(
            id=beat["id"],
            scene_id=beat.get("scene_id", "S1"),
            estimated_seconds=beat["estimated_seconds"],
            text=beat["text"],
            kind=beat.get("kind", KIND_ACTION),
            splittable=beat.get("splittable", False),
            speaker_ids=tuple(beat.get("speaker_ids", ())),
        )
        for beat in fixture["beats"]
    ]


def drama_prompt(evolution: str) -> str:
    """native-drama-series 六段式（audit_cli 实测 failed=0 的骨架）。"""
    return "\n".join(
        [
            "这是一段连续的全画幅画面，遵守对话轴和 180 度原则。",
            "",
            "【环境光影】冷蓝雪夜，低照度侧光。",
            "",
            "【空间基准】雪原边缘的废弃哨塔外。",
            "",
            "【角色声音签名】",
            "主角（男，低沉沙哑，语速偏慢，音量压低）。",
            "",
            "【画面演进】",
            evolution,
            "",
            "【声音设计】风声持续，无配乐。",
            "",
            "【画面风格】写实真人摄影质感；真实皮肤质感；自然真实的人类面部特征，带有轻微的生理不完美。",
            "",
        ]
    )


def build_plan(fixture: dict, beats: list, clips: list) -> dict:
    """集成适配层：ClipDraft + Beat → episode-plan dict。

    动作原文按 beat_ids 反查 Beat.text（冻结决定 #22），台词原文并入画面演进。
    """
    units = []
    refs_by_beat = {}
    cursor = 0
    for beat in beats:
        unit = {
            "id": f"U{beat.id}",
            "kind": beat.kind,
            "span_start": cursor,
            "span_end": cursor + len(beat.text),
            "text": beat.text,
            "scene_id": beat.scene_id,
        }
        if beat.kind == KIND_DIALOGUE:
            unit["speaker_ids"] = list(beat.speaker_ids)
        units.append(unit)
        refs_by_beat[beat.id] = {
            "source_id": f"U{beat.id}",
            "span_start": cursor,
            "span_end": cursor + len(beat.text),
        }
        cursor += len(beat.text)

    plan_clips = []
    start = 0
    for index, clip in enumerate(clips, start=1):
        beat_texts = [beat.text for beat in beats if beat.id in clip.beat_ids]
        evolution = "；".join(beat_texts) if beat_texts else "主角在雪原边缘缓慢前行。"
        dialogue_text = clip.dialogue_text
        if dialogue_text:
            evolution = f'{evolution}（台词："{dialogue_text}"）'
        plan_clips.append(
            {
                "id": f"G{index:03d}",
                "start_seconds": start,
                "duration_seconds": clip.duration_seconds,
                "source_refs": [refs_by_beat[beat_id] for beat_id in clip.beat_ids],
                "start_state": "主角站在雪地边缘，面朝镜头。",
                "end_state": "主角继续向前，雪迹延伸。",
                "system_added_transitions": [],
                "trim_candidates": [],
                "prompt_text": drama_prompt(evolution),
            }
        )
        start += clip.duration_seconds

    return {
        "episode_id": fixture["episode_id"],
        "workflow": fixture["workflow"],
        "model": fixture["model"],
        "script_target_seconds": start,
        "source_units": units,
        "clips": plan_clips,
        "planned_prompt_seconds": start,
        "real_model_render_status": "NOT_RUN",
        "actual_generated_seconds": None,
        "final_edit_seconds": None,
    }


def run_pipeline(fixture: dict, target: float) -> tuple:
    """解析 → 装箱 → 构造计划 → 门禁（带权威目标）→ 返回 (plan, report)。"""
    beats = fixture_beats(fixture)
    clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
    plan = build_plan(fixture, beats, clips)
    plan["script_target_seconds"] = target
    report = validate_episode_plan(
        plan, authoritative_target_seconds=parse_script_duration(fixture["script"]).target_seconds
    )
    return plan, report


class PipelineEndToEndTest(unittest.TestCase):
    """四档正片夹具：全链必须产出可渲染的合格计划。"""

    def test_episode_30_60_90_120_pass_and_render(self):
        for name in ("episode-30.json", "episode-60.json", "episode-90.json", "episode-120.json"):
            with self.subTest(fixture=name):
                fixture = load_fixture(name)
                target = parse_script_duration(fixture["script"]).target_seconds
                plan, report = run_pipeline(fixture, target)
                self.assertIn(
                    report.status, ("PASS_EXACT", "PASS_WITH_TRIM"),
                    f"{name}: {list(report.error_codes)}",
                )
                self.assertEqual(report.coverage_ratio, 1.0)
                self.assertIs(report.timeline_contiguous, True)
                self.assertGreaterEqual(report.planned_prompt_seconds, target)

                with tempfile.TemporaryDirectory() as td:
                    out = Path(td) / "out"
                    rc = render_episode_pack(
                        plan, out, authoritative_target_seconds=target
                    )
                    self.assertEqual(rc, EXIT_RENDERED, f"{name} 渲染被拒绝")
                    manifest = json.loads((out / "duration-manifest.json").read_text("utf-8"))
                    self.assertIn(manifest["duration_status"], ("PASS_EXACT", "PASS_WITH_TRIM"))
                    self.assertGreaterEqual(manifest["planned_prompt_seconds"], target)
                    self.assertEqual(manifest["real_model_render_status"], "NOT_RUN")
                    self.assertIsNone(manifest["actual_generated_seconds"])
                    self.assertIsNone(manifest["final_edit_seconds"])
                    self.assertEqual(manifest["authoritative_target_check"], "performed")
                    prompts = sorted((out / "prompts").glob("G*.md"))
                    self.assertEqual(len(prompts), len(plan["clips"]))

    def test_clip_count_is_dynamic_and_at_least_twelve_for_120(self):
        """120 秒夹具动态产生 ≥12 个片段——组数由内容决定，测试不硬指定"等于六组"。"""
        fixture = load_fixture("episode-120.json")
        plan, _ = run_pipeline(fixture, parse_script_duration(fixture["script"]).target_seconds)
        self.assertGreaterEqual(len(plan["clips"]), 12)
        policy_range = (
            POLICY["clip_seconds"]["min"],
            min(POLICY["clip_seconds"]["max"], 15),
        )
        for clip in plan["clips"]:
            self.assertGreaterEqual(clip["duration_seconds"], policy_range[0])
            self.assertLessEqual(clip["duration_seconds"], policy_range[1])

    def test_all_fixture_targets_match_their_names(self):
        """夹具剧本声明的目标时长必须与文件名一致（防夹具自身漂移）。"""
        for name, seconds in (
            ("episode-30.json", 30),
            ("episode-60.json", 60),
            ("episode-90.json", 90),
            ("episode-120.json", 120),
            ("episode-120-over20.json", 120),
        ):
            fixture = load_fixture(name)
            self.assertEqual(
                parse_script_duration(fixture["script"]).target_seconds, seconds, name
            )


class OriginalDefectCounterexampleTest(unittest.TestCase):
    """原问题反例：[5,15,12,13,15,8] 对 120 秒目标必须失败并报告少 52 秒。"""

    def test_shrunk_plan_fails_reporting_52_second_shortage(self):
        durations = [5, 15, 12, 13, 15, 8]
        total = sum(durations)
        self.assertEqual(total, 68)
        units = [{"id": "S001", "kind": "dialogue", "span_start": 0, "span_end": 68,
                  "text": "一" * 68, "speaker_ids": ["主角"], "scene_id": "S1"}]
        clips = []
        start = 0
        cursor = 0
        for index, duration in enumerate(durations, 1):
            clips.append({
                "id": f"G{index:03d}", "start_seconds": start, "duration_seconds": duration,
                "source_refs": [{"source_id": "S001", "span_start": cursor, "span_end": cursor + duration}],
                "start_state": "a", "end_state": "b", "system_added_transitions": [],
                "trim_candidates": [], "prompt_text": "提示词正文",
            })
            cursor += duration
            start += duration
        plan = {
            "episode_id": "EP-COUNTEREXAMPLE", "workflow": "native-drama-series", "model": "H3",
            "script_target_seconds": 120, "source_units": units, "clips": clips,
            "planned_prompt_seconds": total, "real_model_render_status": "NOT_RUN",
            "actual_generated_seconds": None, "final_edit_seconds": None,
        }
        report = validate_episode_plan(plan, authoritative_target_seconds=120)
        self.assertEqual(report.status, "FAIL")
        self.assertIn(PLANNED_DURATION_UNDERSHOOT, report.error_codes)
        self.assertEqual(report.overage_seconds, -52.0)


class AuthoritativeTargetWiringTest(unittest.TestCase):
    """Task 9 评审追加义务的端到端证明：反缩水不能退化为自我申报。"""

    def test_lowered_declaration_is_caught_against_script_authority(self):
        fixture = load_fixture("episode-120.json")
        target = parse_script_duration(fixture["script"]).target_seconds
        plan, _ = run_pipeline(fixture, target)
        plan["script_target_seconds"] = 100
        plan["planned_prompt_seconds"] = plan["clips"][-1]["start_seconds"] + plan["clips"][-1]["duration_seconds"]
        report = validate_episode_plan(plan, authoritative_target_seconds=target)
        self.assertEqual(report.status, "FAIL")
        self.assertIn(SCRIPT_TARGET_MISMATCH, report.error_codes)

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            rc = render_episode_pack(plan, out, authoritative_target_seconds=target)
            self.assertNotEqual(rc, EXIT_RENDERED)
            self.assertFalse(out.exists())

    def test_over20_fixture_pauses_review_and_renders_no_pack(self):
        fixture = load_fixture("episode-120-over20.json")
        target = parse_script_duration(fixture["script"]).target_seconds
        plan, report = run_pipeline(fixture, target)
        self.assertEqual(report.status, "REVIEW_REQUIRED")
        self.assertIs(report.may_render_full_pack, False)
        self.assertGreater(report.overage_ratio, POLICY["overage_review_ratio"])

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            rc = render_episode_pack(plan, out, authoritative_target_seconds=target)
            self.assertEqual(rc, EXIT_REVIEW_REQUIRED)
            self.assertEqual(
                sorted(p.name for p in out.iterdir()),
                ["duration-manifest.json", "duration-review.md"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
