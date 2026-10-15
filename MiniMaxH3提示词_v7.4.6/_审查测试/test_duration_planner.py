"""Task 4 guard：原子节拍 → 4—10 秒动态片段的顺序装箱。

覆盖十层：
1. 工单 Step 1 冻结样例逐条保留（`POLICY` 接到真实配置 `load_duration_policy()`）；
2. 台词估时：可发音字数 ÷ 语速，标点与空白不计字；
3. 装箱形状：文档顺序、可复现、`G001` 起三位编号、返回 `list[ClipDraft]`；
4. 不重复计时：简单动作与台词并行取较长项；未知/复杂类型一律相加（保守方向）；
5. 兼容性边界：换场强制分段、两名说话人 / 三轮短对白上限，且下限优先于轮次上限；
6. 自然停顿拆分：只按标点切、原文逐字无损、回分时台词供体同样无损；
7. 尾段与短片段回分：只回分不压缩、相邻段不得越界，不可行时阻断；
8. Fail-Closed：空交集、超上限不可切、子最小场景、坏输入、坏政策一律抛
   `DurationPlanError` 家族，并带机器可判定错误码；
9. 无固定六组：片段数由内容与政策共同决定（换政策即换结果，反硬编码）；
10. 与上游集成：真实政策 + Task 3 `parse_script_duration()` 的目标时长。

断言一律用 `self.assertEqual` 等不可被 `python -O` 剥离的形式。
"""
from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from duration_planner import (  # noqa: E402
    INFEASIBLE_CLIP_RANGE,
    INVALID_PLAN_INPUT,
    KIND_ACTION,
    KIND_DIALOGUE,
    UNPACKABLE_BEATS,
    Beat,
    ClipDraft,
    DurationPlanError,
    InfeasibleClipRangeError,
    UnpackableBeatsError,
    audible_char_count,
    effective_clip_range,
    estimate_dialogue_seconds,
    pack_beats,
)
from script_duration import parse_script_duration  # noqa: E402
from versioning import load_duration_policy  # noqa: E402

POLICY = load_duration_policy()
SPEECH_CPS_MIN = POLICY["speech_chars_per_second"]["min"]
SPEECH_CPS_MAX = POLICY["speech_chars_per_second"]["max"]

# 上游输入：可发音 24 字、语速 2 字/秒 → 恰好 12 秒，三个逗号加句末句号可停。
LONG_DIALOGUE_TEXT = "第一句要停一下，第二句也要停，第三句继续走，第四句收尾。"
LONG_DIALOGUE_CPS = 2

# 可发音 15 字、隐含语速 1.5 字/秒 → 10 秒整（工单上限），一个逗号可停。
TEN_SECOND_DIALOGUE_TEXT = "第一句到这里停，第二句接着往下说清楚。"

# 可发音 18 字、语速 3 字/秒 → 6 秒整，用于并行计时与回分用例。
SIX_SECOND_DIALOGUE_TEXT = "一二三四五六七八九十十二一二三四五六"


def action_beat(beat_id, seconds, scene="S1", splittable=True, kind=KIND_ACTION, speakers=()):
    return Beat(
        id=beat_id,
        scene_id=scene,
        estimated_seconds=seconds,
        text=f"{beat_id} 的动作描述",
        kind=kind,
        splittable=splittable,
        speaker_ids=tuple(speakers),
    )


def make_beats(durations, scene="S1", splittable=True, kind=KIND_ACTION):
    return [
        action_beat(f"B{index:03d}", seconds, scene=scene, splittable=splittable, kind=kind)
        for index, seconds in enumerate(durations, 1)
    ]


def dialogue_beat(text, cps=3, speaker="A", scene="S1", splittable=True, beat_id="B001", seconds=None):
    return Beat(
        id=beat_id,
        scene_id=scene,
        estimated_seconds=estimate_dialogue_seconds(text, cps) if seconds is None else seconds,
        text=text,
        kind=KIND_DIALOGUE,
        splittable=splittable,
        speaker_ids=(speaker,),
    )


def durations(clips):
    return [clip.duration_seconds for clip in clips]


def joined_text(clips):
    return "".join(clip.dialogue_text for clip in clips)


def flattened_beat_ids(clips):
    return [beat_id for clip in clips for beat_id in clip.beat_ids]


class WorkOrderSpecTest(unittest.TestCase):
    """工单 Task 4 Step 1 的四个冻结样例。"""

    def test_dialogue_uses_three_to_four_chars_per_second(self):
        self.assertEqual(estimate_dialogue_seconds("一二三四五六七八九十十二", cps=3), 4.0)
        self.assertEqual(estimate_dialogue_seconds("一二三四五六七八", cps=4), 2.0)

    def test_dynamic_group_count_is_not_six(self):
        beats = [
            Beat(id=f"B{i:03d}", scene_id="S1", estimated_seconds=4, text=f"beat-{i}", splittable=False)
            for i in range(30)
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(len(clips), 15)
        self.assertTrue(all(4 <= c.duration_seconds <= 10 for c in clips))
        self.assertEqual(sum(c.duration_seconds for c in clips), 120)

    def test_two_second_tail_is_rebalanced(self):
        beats = make_beats([10, 10, 2])
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [10, 8, 4])

    def test_long_dialogue_splits_at_natural_pause_without_text_loss(self):
        beat = dialogue_beat("第一句很长，需要停顿；第二句继续，不能删字。", cps=3)
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(joined_text(clips), beat.text)
        self.assertTrue(all(c.duration_seconds <= 10 for c in clips))


class DialogueEstimateTest(unittest.TestCase):
    """可发音字数 ÷ 语速；标点、空白按停顿隐含在语速档位里，不单独计字。"""

    def test_punctuation_and_whitespace_are_not_counted_as_characters(self):
        self.assertEqual(estimate_dialogue_seconds("一二三，四五六。", cps=3), 2.0)
        self.assertEqual(estimate_dialogue_seconds("一二三 四五六\n", cps=3), 2.0)
        self.assertEqual(estimate_dialogue_seconds("一……二？！三", cps=3), 1.0)

    def test_latin_and_digits_are_counted_conservatively(self):
        self.assertEqual(estimate_dialogue_seconds("OK", cps=2), 1.0)
        self.assertEqual(estimate_dialogue_seconds("2026", cps=2), 2.0)

    def test_punctuation_only_text_costs_no_speech_time(self):
        self.assertEqual(estimate_dialogue_seconds("……，。！", cps=3), 0.0)

    def test_estimate_grows_with_text(self):
        short = estimate_dialogue_seconds("一二三", cps=3)
        long = estimate_dialogue_seconds("一二三四五六七八九十", cps=3)
        self.assertLess(short, long)
        self.assertGreater(short, 0.0)

    def test_estimate_never_returns_negative_time(self):
        for text in ("", "abc", "……"):
            self.assertGreaterEqual(estimate_dialogue_seconds(text, cps=3), 0.0)

    def test_non_text_is_rejected(self):
        for bad in (None, b"abc", 120, ["一二三"]):
            with self.subTest(bad=bad):
                with self.assertRaises(DurationPlanError):
                    estimate_dialogue_seconds(bad, cps=3)

    def test_non_positive_or_non_numeric_cps_is_rejected(self):
        for bad in (0, -3, float("nan"), float("inf"), "3", None, True):
            with self.subTest(cps=bad):
                with self.assertRaises(DurationPlanError):
                    estimate_dialogue_seconds("一二三", cps=bad)

    def test_bad_text_error_carries_the_machine_readable_code(self):
        with self.assertRaises(DurationPlanError) as ctx:
            estimate_dialogue_seconds(120, cps=3)
        self.assertIn(INVALID_PLAN_INPUT, str(ctx.exception))

    def test_real_policy_speech_band_is_accepted(self):
        text = "一二三四五六七八九十十二"
        self.assertEqual(estimate_dialogue_seconds(text, cps=SPEECH_CPS_MIN), 4.0)
        self.assertEqual(estimate_dialogue_seconds(text, cps=SPEECH_CPS_MAX), 3.0)


class AudibleCharacterCountTest(unittest.TestCase):

    def test_counts_readable_characters_only(self):
        self.assertEqual(audible_char_count("一二三，四。"), 4)
        self.assertEqual(audible_char_count("一二三 四"), 4)
        self.assertEqual(audible_char_count(""), 0)

    def test_crlf_and_decoration_do_not_count(self):
        self.assertEqual(audible_char_count("一\r\n二\r\n"), 2)

    def test_non_text_is_rejected(self):
        for bad in (None, b"abc", 3):
            with self.subTest(bad=bad):
                with self.assertRaises(DurationPlanError):
                    audible_char_count(bad)


class EffectiveRangeTest(unittest.TestCase):
    """用户默认范围与模型能力范围取交集；空交集由装箱阻断，不在纯函数里吞掉。"""

    def test_real_policy_intersects_with_a_wider_model_range(self):
        self.assertEqual(effective_clip_range(4, 15, POLICY), (4, 10))

    def test_model_range_wins_when_narrower(self):
        self.assertEqual(effective_clip_range(5, 7, POLICY), (5, 7))

    def test_model_range_wider_than_policy_is_clamped(self):
        self.assertEqual(effective_clip_range(1, 30, POLICY), (4, 10))

    def test_empty_intersection_is_a_blocking_error_not_a_silent_range(self):
        with self.assertRaises(InfeasibleClipRangeError) as ctx:
            pack_beats(make_beats([5, 5]), model_min=12, model_max=15, policy=POLICY)
        self.assertIn(INFEASIBLE_CLIP_RANGE, str(ctx.exception))

    def test_low_model_ceiling_is_a_blocking_error(self):
        with self.assertRaises(InfeasibleClipRangeError):
            pack_beats(make_beats([5, 5]), model_min=1, model_max=3, policy=POLICY)

    def test_public_range_helper_is_fail_closed(self):
        # 契约：公开函数必须抛家族异常，不能泄露 KeyError/TypeError，也不能把空交集
        # 当成合法区间返回（台账会把它当成有效范围写出去）。
        bad_inputs = (
            (4, 15, {}),
            (4, 15, None),
            (4, 15, {"clip_seconds": {"min": 4}}),
            (4, 10, {"clip_seconds": {"min": 12, "max": 15}}),
            (4, 10, {"clip_seconds": {"min": 12, "max": 15, "x": 1}}),
        )
        for model_min, model_max, policy in bad_inputs:
            with self.subTest(policy=policy):
                with self.assertRaises(InfeasibleClipRangeError) as ctx:
                    effective_clip_range(model_min, model_max, policy)
                self.assertIn(INFEASIBLE_CLIP_RANGE, str(ctx.exception))

    def test_equal_model_bounds_are_a_valid_degenerate_range(self):
        self.assertEqual(effective_clip_range(5, 5, POLICY), (5, 5))
        clips = pack_beats(make_beats([5]), model_min=5, model_max=5, policy=POLICY)
        self.assertEqual(durations(clips), [5])


class PackShapeTest(unittest.TestCase):

    def test_clip_ids_start_at_g001_with_three_digits(self):
        clips = pack_beats(make_beats([10] * 3), model_min=4, model_max=15, policy=POLICY)
        self.assertEqual([clip.id for clip in clips], ["G001", "G002", "G003"])

    def test_ids_keep_three_digits_past_999(self):
        clips = pack_beats(make_beats([10] * 1000), model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(len(clips), 1000)
        self.assertEqual(clips[999].id, "G1000")

    def test_return_type_is_a_plain_list_of_frozen_clips(self):
        clips = pack_beats(make_beats([5]), model_min=4, model_max=15, policy=POLICY)
        self.assertIsInstance(clips, list)
        self.assertIsInstance(clips[0], ClipDraft)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            clips[0].duration_seconds = 9

    def test_durations_are_whole_seconds_and_beat_ids_are_tuples(self):
        clips = pack_beats(make_beats([4.3, 2.2, 7.9]), model_min=4, model_max=15, policy=POLICY)
        for clip in clips:
            self.assertIsInstance(clip.duration_seconds, int)
            self.assertIsInstance(clip.beat_ids, tuple)
            self.assertIsInstance(clip.dialogue_text, str)

    def test_document_order_is_never_reordered(self):
        beats = make_beats([5, 4, 6, 3, 7, 2])
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(flattened_beat_ids(clips), [beat.id for beat in beats])

    def test_repaired_plans_still_visit_beats_in_document_order(self):
        beats = make_beats([3, 9, 4, 8, 2, 7])
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        first_seen = []
        for beat_id in flattened_beat_ids(clips):
            if beat_id not in first_seen:
                first_seen.append(beat_id)
        self.assertEqual(first_seen, [beat.id for beat in beats])

    def test_packing_is_deterministic(self):
        beats = make_beats([3, 9, 4, 8, 2, 7])
        first = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        second = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(first, second)

    def test_any_iterable_of_beats_is_accepted(self):
        beats = make_beats([5, 5])
        clips = pack_beats(tuple(beats), model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [10])

    def test_no_beats_means_no_clips(self):
        self.assertEqual(pack_beats([], model_min=4, model_max=15, policy=POLICY), [])

    def test_every_clip_stays_inside_the_effective_range(self):
        for fixture in ([4.3, 2.2, 7.9, 9.9, 1.1, 5.5, 3, 8.8], [6, 6, 6, 6], [10, 1, 9, 2, 8]):
            with self.subTest(fixture=fixture):
                clips = pack_beats(make_beats(fixture), model_min=4, model_max=15, policy=POLICY)
                for clip in clips:
                    self.assertTrue(4 <= clip.duration_seconds <= 10, durations(clips))


class NoDoubleCountingTest(unittest.TestCase):
    """设计 §6.2：简单动作可与台词同步，片段时长取较长项；其余类型相加。"""

    def test_simple_action_parallel_to_dialogue_is_not_added_twice(self):
        beats = [
            dialogue_beat(SIX_SECOND_DIALOGUE_TEXT, cps=3, beat_id="B001"),
            action_beat("B002", 4, kind=KIND_ACTION),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [6])
        self.assertEqual(clips[0].beat_ids, ("B001", "B002"))

    def test_overlap_does_not_depend_on_beat_order(self):
        beats = [
            action_beat("B001", 4, kind=KIND_ACTION),
            dialogue_beat(SIX_SECOND_DIALOGUE_TEXT, cps=3, beat_id="B002"),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [6])

    def test_action_longer_than_dialogue_still_costs_its_own_time(self):
        beats = [
            dialogue_beat(SIX_SECOND_DIALOGUE_TEXT, cps=3, beat_id="B001"),
            action_beat("B002", 9, kind=KIND_ACTION),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [9])

    def test_complex_action_never_hides_inside_dialogue_time(self):
        beats = [
            dialogue_beat(SIX_SECOND_DIALOGUE_TEXT, cps=3, beat_id="B001"),
            action_beat("B002", 4, kind="complex_action"),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [10])

    def test_unknown_kind_is_added_conservatively(self):
        beats = [
            dialogue_beat(SIX_SECOND_DIALOGUE_TEXT, cps=3, beat_id="B001"),
            action_beat("B002", 4, kind="key_reveal"),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [10])

    def test_action_only_clips_keep_the_plain_sum(self):
        clips = pack_beats(make_beats([4, 4, 4, 4]), model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [8, 8])
        self.assertEqual(joined_text(clips), "")


class CompatibilityTest(unittest.TestCase):
    """设计 §7.3 顺序装箱 + §6.1 说话人与轮次上限；下限优先于轮次上限。"""

    def test_scene_change_forces_a_clip_boundary(self):
        beats = make_beats([6], scene="S1") + make_beats([6], scene="S2")
        beats[1] = action_beat("B002", 6, scene="S2")
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [6, 6])
        self.assertEqual([clip.beat_ids for clip in clips], [("B001",), ("B002",)])

    def test_short_scenes_are_not_merged_into_one_clip(self):
        beats = [action_beat("B001", 4, scene="S1"), action_beat("B002", 4, scene="S2")]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [4, 4])

    def test_more_than_two_speakers_force_a_boundary(self):
        beats = [
            dialogue_beat("一二三四五六七八", cps=2, speaker="A", beat_id="B001"),
            dialogue_beat("一二三四五六七八", cps=2, speaker="B", beat_id="B002"),
            dialogue_beat("一二三四五六七八", cps=2, speaker="C", beat_id="B003"),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [8, 4])
        self.assertEqual([clip.beat_ids for clip in clips], [("B001", "B002"), ("B003",)])

    def test_more_than_three_dialogue_turns_force_a_boundary(self):
        beats = [
            dialogue_beat("一二", cps=1, speaker=speaker, beat_id=f"B{index:03d}")
            for index, speaker in enumerate(["A", "B", "A", "B", "A", "B"], 1)
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [6, 6])
        self.assertEqual([len(clip.beat_ids) for clip in clips], [3, 3])

    def test_a_third_speaker_closes_the_clip_before_the_minimum_is_at_risk(self):
        beats = [
            dialogue_beat("一二三四五六七八", cps=4, speaker=speaker, beat_id=f"B{index:03d}")
            for index, speaker in enumerate(["A", "B", "C", "A", "B", "C"], 1)
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [4, 4, 4])
        self.assertEqual([clip.beat_ids for clip in clips], [("B001", "B002"), ("B003", "B004"), ("B005", "B006")])

    def test_minimum_length_wins_over_the_turn_cap(self):
        beats = [
            dialogue_beat("一", cps=1, speaker=speaker, beat_id=f"B{index:03d}", seconds=1.4)
            for index, speaker in enumerate(["A", "B", "A", "B"], 1)
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(len(clips), 1)
        self.assertEqual(durations(clips), [6])
        self.assertEqual(len(clips[0].beat_ids), 4)

    def test_scene_that_alone_is_below_the_minimum_is_refused(self):
        beats = [action_beat("B001", 10, scene="S1"), action_beat("B002", 2, scene="S2")]
        with self.assertRaises(UnpackableBeatsError) as ctx:
            pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertIn(UNPACKABLE_BEATS, str(ctx.exception))

    def test_short_clip_is_not_rescued_by_merging_across_scenes(self):
        # 4s(S1) 合法但紧邻的 3s(S2) 低于下限：唯一“修好”它的方式是跨场合并，
        # 那会造出混场片段，因此必须阻断。
        beats = [action_beat("B001", 4, scene="S1"), action_beat("B002", 3, scene="S2")]
        with self.assertRaises(UnpackableBeatsError) as ctx:
            pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertIn(UNPACKABLE_BEATS, str(ctx.exception))


class DialogueSplitTest(unittest.TestCase):
    """长台词只按自然停顿（标点）跨片段，原文逐字无损。"""

    def test_single_beat_over_the_cap_is_split_losslessly(self):
        beat = dialogue_beat(LONG_DIALOGUE_TEXT, cps=LONG_DIALOGUE_CPS, beat_id="B001")
        self.assertEqual(beat.estimated_seconds, 12.0)
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [7, 6])
        self.assertEqual(joined_text(clips), LONG_DIALOGUE_TEXT)

    def test_every_cut_lands_right_after_a_pause_character(self):
        beat = dialogue_beat(LONG_DIALOGUE_TEXT, cps=LONG_DIALOGUE_CPS, beat_id="B001")
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        for clip in clips[:-1]:
            with self.subTest(clip=clip.id):
                self.assertIn(clip.dialogue_text[-1], "，。；！？：…、")

    def test_text_is_never_lost_or_duplicated(self):
        beats = [
            dialogue_beat(LONG_DIALOGUE_TEXT, cps=LONG_DIALOGUE_CPS, beat_id="B001"),
            dialogue_beat("短句一。短句二。", cps=1, beat_id="B002"),
            dialogue_beat(TEN_SECOND_DIALOGUE_TEXT + "补充一句话。", cps=2, beat_id="B003"),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(joined_text(clips), "".join(beat.text for beat in beats))
        for clip in clips:
            self.assertTrue(4 <= clip.duration_seconds <= 10, durations(clips))

    def test_plan_total_never_understates_spoken_time(self):
        beat = dialogue_beat(LONG_DIALOGUE_TEXT, cps=LONG_DIALOGUE_CPS, beat_id="B001")
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertGreaterEqual(sum(durations(clips)), 12)

    def test_beat_exactly_at_the_cap_is_not_split(self):
        beat = dialogue_beat(TEN_SECOND_DIALOGUE_TEXT, cps=1.5, speaker="A", beat_id="B001", seconds=10.0)
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [10])
        self.assertEqual(joined_text(clips), beat.text)

    def test_clause_without_a_pause_is_refused_instead_of_cut_mid_sentence(self):
        beat = dialogue_beat("一二三四五六七八九十" * 4, cps=3, beat_id="B001")
        with self.assertRaises(UnpackableBeatsError) as ctx:
            pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertIn(UNPACKABLE_BEATS, str(ctx.exception))

    def test_dialogue_over_the_cap_must_be_declared_splittable(self):
        beat = dialogue_beat(LONG_DIALOGUE_TEXT, cps=LONG_DIALOGUE_CPS, splittable=False, beat_id="B001")
        with self.assertRaises(UnpackableBeatsError):
            pack_beats([beat], model_min=4, model_max=15, policy=POLICY)

    def test_dialogue_needing_three_clips_splits_into_three(self):
        text = "一二三四五六七八，一二三四五六七八，一二三四五六七八。"
        beat = dialogue_beat(text, cps=1, beat_id="B001")
        self.assertEqual(beat.estimated_seconds, 24.0)
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [8, 8, 8])
        self.assertEqual(joined_text(clips), text)

    def test_dialogue_that_no_sentence_can_be_cut_out_of_is_refused(self):
        beat = dialogue_beat("一二三四五六七八九十甲，乙丙丁戊己庚辛壬癸子，丑寅卯。", cps=1, beat_id="B001")
        with self.assertRaises(UnpackableBeatsError) as ctx:
            pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertIn(UNPACKABLE_BEATS, str(ctx.exception))

    def test_dialogue_without_audible_characters_cannot_be_split(self):
        beats = [
            dialogue_beat("……", cps=1, beat_id="B001", seconds=9.5),
            action_beat("B002", 3.0, kind="complex_action"),
        ]
        with self.assertRaises(UnpackableBeatsError):
            pack_beats(beats, model_min=4, model_max=15, policy=POLICY)

    def test_oversize_dialogue_without_audible_characters_is_refused(self):
        beat = dialogue_beat("……", cps=1, beat_id="B001", seconds=12.0)
        with self.assertRaises(UnpackableBeatsError) as ctx:
            pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertIn(UNPACKABLE_BEATS, str(ctx.exception))

    def test_dialogue_that_only_fits_in_three_full_clips(self):
        text = "一二三四五，六七八九十，甲乙丙丁戊己庚辛壬癸，子丑寅卯辰巳午未申酉。"
        beat = dialogue_beat(text, cps=1, beat_id="B001")
        self.assertEqual(beat.estimated_seconds, 30.0)
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [10, 10, 10])
        self.assertEqual(joined_text(clips), text)

    def test_beat_a_hair_over_the_cap_still_plans_within_range(self):
        beat = dialogue_beat(TEN_SECOND_DIALOGUE_TEXT, cps=1.5, beat_id="B001", seconds=10.000000005)
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(joined_text(clips), TEN_SECOND_DIALOGUE_TEXT)
        for clip in clips:
            self.assertTrue(4 <= clip.duration_seconds <= 10, durations(clips))

    def test_dialogue_cut_style_is_retried_before_refusing(self):
        # 对抗评审反例（rev-task4-probeF）：只按“均衡”切分时，尾部会剩下 1.875s
        # 无法回分而阻断；同一段文本在自然停顿处确实存在合法切法。
        beat = dialogue_beat(
            "她说；门转说二？？？二想的一五。二坐的一抬说它二门看转，走四！门？？？口八想？",
            cps=1.6,
            beat_id="B001",
            seconds=17.5,
        )
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertGreaterEqual(len(clips), 2)
        self.assertEqual(joined_text(clips), beat.text)
        for clip in clips:
            self.assertTrue(4 <= clip.duration_seconds <= 10, durations(clips))

    def test_dialogue_without_a_trailing_pause_still_splits(self):
        beat = dialogue_beat("一，二", cps=1, beat_id="B001", seconds=12.0)
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [6, 6])
        self.assertEqual(joined_text(clips), "一，二")

    def test_a_pause_run_absorbs_the_closers_that_follow_it(self):
        beat = dialogue_beat(
            "第一句要停一下。」第二句继续往下说，第三句收尾。",
            cps=1,
            beat_id="B001",
            seconds=12.0,
        )
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [5, 8])
        self.assertEqual(joined_text(clips), beat.text)
        # 切口必须落在「。」+「」之后：收尾引号跟随停顿，不能甩到下一片段开头。
        self.assertTrue(clips[0].dialogue_text.endswith("。」"))
        for clip in clips[1:]:
            with self.subTest(clip=clip.id):
                self.assertTrue(clip.dialogue_text)
                self.assertNotIn(clip.dialogue_text[0], "」』）)】’”\"'")

    def test_ellipsis_run_is_never_cut_in_half(self):
        beat = dialogue_beat("第一句……第二句……第三句……第四句。", cps=1, beat_id="B001")
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(joined_text(clips), beat.text)
        for clip in clips[1:]:
            with self.subTest(clip=clip.id):
                self.assertNotEqual(clip.dialogue_text[0], "…")


class TailRepairTest(unittest.TestCase):
    """设计 §7.5：尾部不足下限只能回分，不能生成违规短片段，也不能压缩节拍。"""

    def test_short_tail_merges_into_the_previous_clip(self):
        clips = pack_beats(make_beats([6, 2]), model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [8])

    def test_short_tail_shifts_time_instead_of_overfilling_the_neighbour(self):
        clips = pack_beats(make_beats([9, 9, 2]), model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [9, 7, 4])

    def test_fractional_tail_is_rounded_up_to_a_legal_minimum(self):
        clips = pack_beats(make_beats([10, 10, 2.6]), model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [10, 9, 4])

    def test_short_interior_clip_shifts_time_from_the_next_group(self):
        clips = pack_beats(make_beats([3, 7.5]), model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [4, 7])

    def test_rebalance_never_pushes_a_neighbour_past_the_cap(self):
        for fixture in ([9, 9, 2], [10, 10, 2.6], [5, 5, 2], [7, 3, 2]):
            with self.subTest(fixture=fixture):
                clips = pack_beats(make_beats(fixture), model_min=4, model_max=15, policy=POLICY)
                for clip in clips:
                    self.assertTrue(4 <= clip.duration_seconds <= 10, durations(clips))

    def test_rebalance_never_drops_content(self):
        for fixture in ([9, 9, 2], [3, 7.5], [10, 10, 2.6]):
            with self.subTest(fixture=fixture):
                beats = make_beats(fixture)
                clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
                self.assertEqual(set(flattened_beat_ids(clips)), {beat.id for beat in beats})
                self.assertGreaterEqual(len(flattened_beat_ids(clips)), len(beats))

    def test_dialogue_donor_keeps_every_character_when_shifting_time(self):
        beats = [
            dialogue_beat(TEN_SECOND_DIALOGUE_TEXT, cps=1.5, beat_id="B001", seconds=10.0),
            action_beat("B002", 2, kind="complex_action"),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [5, 8])
        self.assertEqual(joined_text(clips), TEN_SECOND_DIALOGUE_TEXT)
        self.assertTrue(clips[0].dialogue_text.endswith("，"))
        self.assertEqual(clips[1].beat_ids, ("B001", "B002"))

    def test_dialogue_head_shift_keeps_text_in_script_order(self):
        beats = [
            dialogue_beat("一二三", cps=1, beat_id="B001", seconds=3.0),
            dialogue_beat(TEN_SECOND_DIALOGUE_TEXT, cps=1.5, beat_id="B002", seconds=7.5),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [7, 5])
        self.assertEqual(joined_text(clips), "一二三" + TEN_SECOND_DIALOGUE_TEXT)
        self.assertTrue(clips[0].dialogue_text.endswith("，"))
        self.assertEqual(clips[0].beat_ids, ("B001", "B002"))

    def test_a_clip_listing_one_beat_several_times_is_reported_once(self):
        beats = [
            action_beat("B001", 10.0, scene="S2"),
            action_beat("B002", 9.9, scene="S1"),
            Beat(
                id="B003",
                scene_id="S1",
                estimated_seconds=1.0,
                text="谁在里面？",
                kind=KIND_DIALOGUE,
                splittable=False,
                speaker_ids=("C",),
            ),
            action_beat("B004", 2.0, scene="S1", kind="complex_action"),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [10, 8, 4])
        self.assertEqual(clips[2].beat_ids, ("B002", "B003", "B004"))
        self.assertEqual(clips[2].dialogue_text, "谁在里面？")

    def test_dialogue_tail_shift_moves_only_what_the_minimum_needs(self):
        beats = [
            dialogue_beat(
                "一二三四五六七八，九十干支丑寅，卯辰巳午，未，申。",
                cps=2,
                beat_id="B001",
                seconds=10.0,
            ),
            action_beat("B002", 3.0, kind="complex_action"),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [9, 4])
        self.assertEqual(joined_text(clips), beats[0].text)
        self.assertEqual(clips[1].beat_ids, ("B001", "B002"))

    def test_whole_atom_move_is_skipped_when_it_would_overfill_the_neighbour(self):
        clips = pack_beats(make_beats([2.5, 7.5, 3]), model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [9, 4])

    def test_partial_move_keeps_the_donor_legal_when_a_whole_atom_would_not(self):
        clips = pack_beats(make_beats([3, 6, 3]), model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [8, 4])

    def test_head_direction_move_skips_a_too_large_atom(self):
        beats = [
            action_beat("B001", 3.0, kind="complex_action"),
            action_beat("B002", 7.5, kind="complex_action"),
            action_beat("B003", 2.0, kind="complex_action"),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [4, 9])

    def test_head_direction_move_refused_when_the_next_beat_cannot_be_divided(self):
        beats = [
            action_beat("B001", 3.0, kind="complex_action"),
            action_beat("B002", 7.5, splittable=False),
        ]
        with self.assertRaises(UnpackableBeatsError) as ctx:
            pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertIn(UNPACKABLE_BEATS, str(ctx.exception))

    def test_short_leading_clause_leaves_no_legal_donation(self):
        beats = [
            dialogue_beat("一，二三四五六七八九十十一十二十三十四十五十六。", cps=1, beat_id="B001", seconds=10.0),
            action_beat("B002", 3.0, kind="complex_action"),
        ]
        with self.assertRaises(UnpackableBeatsError) as ctx:
            pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertIn(UNPACKABLE_BEATS, str(ctx.exception))

    def test_tail_that_cannot_be_repaired_is_refused(self):
        beats = [action_beat("B001", 10, splittable=False), action_beat("B002", 2, splittable=True)]
        with self.assertRaises(UnpackableBeatsError):
            pack_beats(beats, model_min=4, model_max=15, policy=POLICY)


class OversizeBeatTest(unittest.TestCase):

    def test_splittable_action_over_the_cap_is_divided_at_declared_boundaries(self):
        clips = pack_beats(make_beats([12]), model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [6, 6])
        self.assertEqual([clip.beat_ids for clip in clips], [("B001",), ("B001",)])

    def test_unsplittable_action_over_the_cap_is_refused(self):
        with self.assertRaises(UnpackableBeatsError) as ctx:
            pack_beats(make_beats([12], splittable=False), model_min=4, model_max=15, policy=POLICY)
        self.assertIn(UNPACKABLE_BEATS, str(ctx.exception))

    def test_oversize_beat_that_cannot_reach_a_legal_minimum_is_refused(self):
        policy = {"clip_seconds": {"min": 8, "max": 10}}
        with self.assertRaises(UnpackableBeatsError) as ctx:
            pack_beats(make_beats([12]), model_min=8, model_max=10, policy=policy)
        self.assertIn(UNPACKABLE_BEATS, str(ctx.exception))

    def test_split_pieces_may_be_short_because_they_are_packed_with_neighbours(self):
        policy = {"clip_seconds": {"min": 5, "max": 8}}
        clips = pack_beats(make_beats([9.9]), model_min=5, model_max=8, policy=policy)
        self.assertEqual(durations(clips), [5, 5])
        self.assertEqual([clip.beat_ids for clip in clips], [("B001",), ("B001",)])


class FailClosedTest(unittest.TestCase):
    """无法规划必须阻断：宁可向用户提问，也不杜撰一个看似合法的计划。"""

    def test_non_beat_items_are_rejected(self):
        for bad in ([action_beat("B001", 5), "B002"], [None], [{"id": "B001"}]):
            with self.subTest(bad=bad):
                with self.assertRaises(DurationPlanError):
                    pack_beats(bad, model_min=4, model_max=15, policy=POLICY)

    def test_a_string_is_not_an_iterable_of_beats(self):
        with self.assertRaises(DurationPlanError):
            pack_beats("B001", model_min=4, model_max=15, policy=POLICY)

    def test_duplicate_beat_ids_are_rejected(self):
        beats = [action_beat("B001", 5), action_beat("B001", 5)]
        with self.assertRaises(DurationPlanError) as ctx:
            pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertIn(INVALID_PLAN_INPUT, str(ctx.exception))

    def test_missing_ids_and_scenes_are_rejected(self):
        with self.assertRaises(DurationPlanError):
            pack_beats([action_beat("", 5)], model_min=4, model_max=15, policy=POLICY)
        with self.assertRaises(DurationPlanError):
            pack_beats([action_beat("B001", 5, scene="")], model_min=4, model_max=15, policy=POLICY)

    def test_non_text_identifiers_are_rejected(self):
        with self.assertRaises(DurationPlanError):
            pack_beats([Beat(id=7, scene_id="S1", estimated_seconds=5, text="动作")], model_min=4, model_max=15, policy=POLICY)
        with self.assertRaises(DurationPlanError):
            pack_beats([Beat(id="B001", scene_id=7, estimated_seconds=5, text="动作")], model_min=4, model_max=15, policy=POLICY)
        with self.assertRaises(DurationPlanError):
            pack_beats([Beat(id="B001", scene_id="S1", estimated_seconds=5, text=None)], model_min=4, model_max=15, policy=POLICY)

    def test_non_boolean_splittable_is_rejected(self):
        for bad in (1, 0, "yes", None):
            with self.subTest(splittable=bad):
                with self.assertRaises(DurationPlanError):
                    pack_beats(
                        [Beat(id="B001", scene_id="S1", estimated_seconds=5, text="动作", splittable=bad)],
                        model_min=4,
                        model_max=15,
                        policy=POLICY,
                    )

    def test_speaker_ids_must_be_a_sequence_of_ids(self):
        for bad in ("A", 7, None, {"A": 1}):
            with self.subTest(speaker_ids=bad):
                with self.assertRaises(DurationPlanError):
                    pack_beats(
                        [Beat(id="B001", scene_id="S1", estimated_seconds=5, text="台词", speaker_ids=bad)],
                        model_min=4,
                        model_max=15,
                        policy=POLICY,
                    )

    def test_bad_durations_are_rejected(self):
        for bad in (0, -1, 0.0, float("nan"), float("inf"), "5", None, True):
            with self.subTest(seconds=bad):
                with self.assertRaises(DurationPlanError):
                    pack_beats([action_beat("B001", bad)], model_min=4, model_max=15, policy=POLICY)

    def test_zero_second_beat_is_named_as_an_input_error(self):
        # 只断言“抛家族异常”会漏掉“零时长被静默接受”这类回归，错误码必须钉住。
        with self.assertRaises(DurationPlanError) as ctx:
            pack_beats([action_beat("B001", 5), action_beat("B002", 0)], model_min=4, model_max=15, policy=POLICY)
        self.assertIn(INVALID_PLAN_INPUT, str(ctx.exception))

    def test_absurd_durations_are_refused_without_allocating(self):
        for seconds in (1e6, 1e12, 1e308):
            with self.subTest(seconds=seconds):
                with self.assertRaises(UnpackableBeatsError) as ctx:
                    pack_beats(
                        [action_beat("B001", seconds)], model_min=4, model_max=15, policy=POLICY
                    )
                self.assertIn(UNPACKABLE_BEATS, str(ctx.exception))

    def test_unordered_inputs_are_rejected(self):
        beat_a = action_beat("B001", 5)
        beat_b = action_beat("B002", 5)
        bad_inputs = ({beat_a, beat_b}, {"B001": beat_a}, {"B001": beat_a}.keys())
        for bad in bad_inputs:
            with self.subTest(kind=type(bad).__name__):
                with self.assertRaises(DurationPlanError) as ctx:
                    pack_beats(bad, model_min=4, model_max=15, policy=POLICY)
                self.assertIn(INVALID_PLAN_INPUT, str(ctx.exception))

    def test_an_iterator_input_is_still_accepted(self):
        beats = iter(make_beats([5, 5]))
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [10])

    def test_bad_kinds_and_speakers_are_rejected(self):
        with self.assertRaises(DurationPlanError):
            pack_beats([action_beat("B001", 5, kind="")], model_min=4, model_max=15, policy=POLICY)
        with self.assertRaises(DurationPlanError):
            pack_beats([action_beat("B001", 5, kind=7)], model_min=4, model_max=15, policy=POLICY)
        with self.assertRaises(DurationPlanError):
            pack_beats([action_beat("B001", 5, speakers=("A", ""))], model_min=4, model_max=15, policy=POLICY)
        with self.assertRaises(DurationPlanError):
            pack_beats([action_beat("B001", 5, speakers=("A", 2))], model_min=4, model_max=15, policy=POLICY)

    def test_bad_model_range_is_rejected(self):
        for bad_min, bad_max in ((None, 10), (4, None), ("4", 10), (4, 10.5), (True, 10), (7, 5)):
            with self.subTest(model_min=bad_min, model_max=bad_max):
                with self.assertRaises(DurationPlanError):
                    pack_beats(make_beats([5]), model_min=bad_min, model_max=bad_max, policy=POLICY)

    def test_bad_policy_is_rejected(self):
        bad_policies = (
            None,
            [],
            {},
            {"clip_seconds": None},
            {"clip_seconds": {"min": 4}},
            {"clip_seconds": {"min": 4, "max": 10, "extra": 1}},
            {"clip_seconds": {"min": "4", "max": 10}},
            {"clip_seconds": {"min": 10, "max": 4}},
            {"clip_seconds": {"min": 0, "max": 10}},
            {"clip_seconds": {"min": True, "max": 10}},
        )
        for policy in bad_policies:
            with self.subTest(policy=policy):
                with self.assertRaises(DurationPlanError):
                    pack_beats(make_beats([5]), model_min=4, model_max=15, policy=policy)

    def test_every_refusal_is_inside_the_documented_exception_family(self):
        cases = (
            (make_beats([5]), 4, 15, None, InfeasibleClipRangeError),
            (make_beats([5]), 12, 15, POLICY, InfeasibleClipRangeError),
            (make_beats([12], splittable=False), 4, 15, POLICY, UnpackableBeatsError),
            ([action_beat("B001", 10, scene="S1"), action_beat("B002", 1, scene="S2")], 4, 15, POLICY, UnpackableBeatsError),
        )
        for beats, model_min, model_max, policy, expected in cases:
            with self.subTest(expected=expected.__name__):
                with self.assertRaises(expected) as ctx:
                    pack_beats(beats, model_min=model_min, model_max=model_max, policy=policy)
                self.assertIsInstance(ctx.exception, DurationPlanError)

    def test_no_input_produces_a_short_or_oversized_clip(self):
        # 期望值写死，避免“全部抛错也算通过”的假绿灯。
        fixtures = (([1, 1, 1], "block"), ([12, 12], "plan"), ([9.5, 9.5, 9.5], "plan"))
        for fixture, expected in fixtures:
            with self.subTest(fixture=fixture):
                beats = make_beats(fixture)
                if expected == "block":
                    with self.assertRaises(UnpackableBeatsError):
                        pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
                    continue
                clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
                self.assertTrue(clips)
                for clip in clips:
                    self.assertTrue(4 <= clip.duration_seconds <= 10, durations(clips))


class DynamicCountTest(unittest.TestCase):
    """完成判定：不存在“每集固定六组”，片段数由时长内容决定。"""

    def test_clip_count_follows_the_script_length(self):
        expected = {30: 3, 60: 6, 77: 8, 90: 9, 95: 10, 120: 12}
        counts = {}
        for total, clip_count in expected.items():
            with self.subTest(total=total):
                beats = make_beats([10] * (total // 10) + ([total % 10] if total % 10 else []))
                clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
                self.assertEqual(len(clips), clip_count)
                counts[total] = len(clips)
        self.assertGreater(len(set(counts.values())), 3)

    def test_uneven_scripts_get_more_clips_than_a_fixed_six(self):
        clips = pack_beats(make_beats([8] * 15), model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(len(clips), 15)

    def test_clip_count_changes_with_the_policy_range(self):
        policy = {"clip_seconds": {"min": 5, "max": 8}}
        clips = pack_beats(make_beats([3] * 10), model_min=5, model_max=8, policy=policy)
        self.assertEqual(durations(clips), [6, 6, 6, 6, 6])

    def test_policy_minimum_is_honoured_not_hardcoded(self):
        policy = {"clip_seconds": {"min": 6, "max": 10}}
        clips = pack_beats(make_beats([4] * 10), model_min=6, model_max=10, policy=policy)
        self.assertEqual(durations(clips), [8, 8, 8, 8, 8])


class UpstreamIntegrationTest(unittest.TestCase):
    """装箱消费 Task 2 政策与 Task 3 目标时长，不另建时长常量或第二个解析器。"""

    def test_real_policy_drives_the_effective_range(self):
        self.assertEqual(POLICY["clip_seconds"], {"min": 4, "max": 10})
        self.assertEqual(effective_clip_range(4, 15, POLICY), (4, 10))

    def test_real_policy_speech_band_is_the_one_used_for_dialogue_beats(self):
        beat = dialogue_beat("一二三四五六七八九十十二", cps=SPEECH_CPS_MIN, beat_id="B001")
        self.assertEqual(beat.estimated_seconds, 4.0)
        clips = pack_beats([beat], model_min=4, model_max=15, policy=POLICY)
        self.assertEqual(durations(clips), [4])

    def test_plan_total_meets_the_declared_screenplay_target(self):
        screenplay = "**精准时长**：120秒\n\n## 【00:00-02:00】开场\n动作描述。\n"
        target = parse_script_duration(screenplay).target_seconds
        self.assertEqual(target, 120)
        clips = pack_beats(make_beats([10] * 12), model_min=4, model_max=15, policy=POLICY)
        planned = sum(clip.duration_seconds for clip in clips)
        self.assertEqual(planned, 120)
        self.assertGreaterEqual(planned, target)

    def test_plan_total_covers_a_dialogue_heavy_screenplay(self):
        screenplay = "**精准时长**：60秒\n"
        target = parse_script_duration(screenplay).target_seconds
        beats = [
            dialogue_beat(LONG_DIALOGUE_TEXT, cps=LONG_DIALOGUE_CPS, beat_id="B001"),
            dialogue_beat(LONG_DIALOGUE_TEXT, cps=LONG_DIALOGUE_CPS, beat_id="B002"),
            dialogue_beat(LONG_DIALOGUE_TEXT, cps=LONG_DIALOGUE_CPS, beat_id="B003"),
            dialogue_beat(LONG_DIALOGUE_TEXT, cps=LONG_DIALOGUE_CPS, beat_id="B004"),
            dialogue_beat(LONG_DIALOGUE_TEXT, cps=LONG_DIALOGUE_CPS, beat_id="B005"),
        ]
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        planned = sum(clip.duration_seconds for clip in clips)
        self.assertGreaterEqual(planned, target)
        self.assertEqual(joined_text(clips), "".join(beat.text for beat in beats))


class ContractShapeTest(unittest.TestCase):

    def test_beat_interface_is_frozen(self):
        field_names = [field.name for field in dataclasses.fields(Beat)]
        self.assertEqual(
            field_names,
            ["id", "scene_id", "estimated_seconds", "text", "kind", "splittable", "speaker_ids"],
        )
        self.assertEqual(Beat.__dataclass_params__.frozen, True)

    def test_clip_interface_is_frozen(self):
        field_names = [field.name for field in dataclasses.fields(ClipDraft)]
        self.assertEqual(field_names, ["id", "duration_seconds", "beat_ids", "dialogue_text"])
        self.assertEqual(ClipDraft.__dataclass_params__.frozen, True)

    def test_beat_defaults_match_the_order_contract(self):
        beat = Beat(id="B001", scene_id="S1", estimated_seconds=4, text="beat")
        self.assertEqual(beat.kind, KIND_ACTION)
        self.assertEqual(beat.splittable, False)
        self.assertEqual(beat.speaker_ids, ())

    def test_clip_defaults_match_the_order_contract(self):
        clip = ClipDraft(id="G001", duration_seconds=4, beat_ids=("B001",))
        self.assertEqual(clip.dialogue_text, "")

    def test_dialogue_kind_is_the_documented_literal(self):
        self.assertEqual(KIND_DIALOGUE, "dialogue")

    def test_error_codes_are_stable_strings(self):
        self.assertEqual(UNPACKABLE_BEATS, "UNPACKABLE_BEATS")
        self.assertEqual(INFEASIBLE_CLIP_RANGE, "INFEASIBLE_CLIP_RANGE")
        self.assertEqual(INVALID_PLAN_INPUT, "INVALID_PLAN_INPUT")

    def test_planner_errors_are_runtime_errors(self):
        self.assertTrue(issubclass(DurationPlanError, RuntimeError))
        self.assertTrue(issubclass(UnpackableBeatsError, DurationPlanError))
        self.assertTrue(issubclass(InfeasibleClipRangeError, DurationPlanError))


if __name__ == "__main__":
    unittest.main()
