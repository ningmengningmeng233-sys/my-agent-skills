"""Task 3 合同测试：剧本目标时长的权威裁决与内部时间码解析。

分层：

1. 顶部声明（精准时长/总时长/时长）是唯一权威值——没有声明必须阻断，禁止
   用最后一个场景终点静默推断。
2. 声明格式只接受可精确换算的写法（秒、分钟、分钟+秒、时钟），小数分钟等
   不可精确换算的写法按缺失处理，避免把 ``2.5分钟`` 误读成 ``5分钟``。
3. 内部时间码只产出 ``TimeSegment`` 与 ``INTERNAL_TIMECODE_MISMATCH`` 警告，
   永不覆盖顶部声明。
4. 非时间码的方括号标题（``【环境光影】``、``[镜头 1]``）不得被误当成片段。

测试全部使用 ``self.assertEqual``/``assertRaises``：``assert`` 语句在
``python -O`` 下会被剥离，裸断言会假绿。
"""
from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
import sys

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from script_duration import (  # noqa: E402
    MISSING_SCRIPT_DURATION,
    INTERNAL_TIMECODE_MISMATCH,
    MissingScriptDurationError,
    ScriptDurationContract,
    TimeSegment,
    parse_script_duration,
)


class HeaderAuthorityTest(unittest.TestCase):
    """工单 Step 1 冻结用例：顶部声明优先，时间码只警告。"""

    def test_header_duration_is_authoritative(self):
        text = "**精准时长**：120秒\n## 【00:00 - 01:50】场景"
        contract = parse_script_duration(text)
        self.assertEqual(contract.target_seconds, 120)
        self.assertIn(INTERNAL_TIMECODE_MISMATCH, contract.warnings)

    def test_header_beats_last_timecode_end(self):
        """内部时间码终点 110s 不得把目标改写成 110s。"""
        text = "总时长：120秒\n## 【00:00 - 01:10】场景一\n## 【01:10 - 01:50】场景二"
        contract = parse_script_duration(text)
        self.assertEqual(contract.target_seconds, 120)
        self.assertEqual(contract.internal_segments[-1].end_seconds, 110)

    def test_first_declaration_wins_over_later_conflict(self):
        text = "**精准时长**：120秒\n正文……\n总时长：45秒（草案）"
        contract = parse_script_duration(text)
        self.assertEqual(contract.target_seconds, 120)

    def test_earliest_declaration_wins_across_patterns(self):
        """时钟写法出现在先时，后面更晚的秒数声明不得抢先。"""
        text = "总时长：01:30\n时长：45秒"
        contract = parse_script_duration(text)
        self.assertEqual(contract.target_seconds, 90)


class HeaderFormatTest(unittest.TestCase):
    def test_parses_minutes(self):
        self.assertEqual(parse_script_duration("时长：2分钟").target_seconds, 120)

    def test_parses_clock(self):
        self.assertEqual(parse_script_duration("总时长：01:30").target_seconds, 90)

    def test_parses_clock_with_hours(self):
        self.assertEqual(parse_script_duration("总时长：01:02:03").target_seconds, 3723)

    def test_parses_compound_minutes_and_seconds(self):
        """``1分钟30秒`` 必须算成 90，不得读成 60。"""
        self.assertEqual(parse_script_duration("时长：1分钟30秒").target_seconds, 90)

    def test_clock_must_not_be_excavated_from_a_longer_token(self):
        """``100:30:00`` 里不得截出 ``00:30``（30s）；非法写法一律阻断。"""
        with self.assertRaises(MissingScriptDurationError):
            parse_script_duration("总时长：100:30:00")

    def test_tolerates_markdown_and_spacing_variants(self):
        self.assertEqual(parse_script_duration("**总时长**：90秒").target_seconds, 90)
        self.assertEqual(parse_script_duration("时长: 90 秒").target_seconds, 90)
        self.assertEqual(parse_script_duration("精准时长 90秒").target_seconds, 90)

    def test_fractional_declaration_is_not_silently_truncated(self):
        """``2.5分钟`` 无法精确换算，禁止读成 5分钟（300s）或 2分钟（120s）。"""
        with self.assertRaises(MissingScriptDurationError):
            parse_script_duration("时长：2.5分钟")

    def test_header_scan_does_not_match_shot_table_rows(self):
        text = "| 镜头号 | 时长 | 画面内容 |\n|---|---|---|\n| 1 | 3s | 平视近景 |"
        with self.assertRaises(MissingScriptDurationError):
            parse_script_duration(text)


class MissingDurationTest(unittest.TestCase):
    def test_missing_duration_blocks(self):
        with self.assertRaises(MissingScriptDurationError):
            parse_script_duration("只有剧情，没有目标秒数")

    def test_empty_text_blocks(self):
        with self.assertRaises(MissingScriptDurationError):
            parse_script_duration("")

    def test_zero_second_declaration_blocks(self):
        with self.assertRaises(MissingScriptDurationError):
            parse_script_duration("时长：0秒")

    def test_zero_clock_declaration_blocks(self):
        with self.assertRaises(MissingScriptDurationError):
            parse_script_duration("时长：00:00")

    def test_error_carries_frozen_code(self):
        with self.assertRaises(MissingScriptDurationError) as ctx:
            parse_script_duration("只有剧情")
        self.assertIn(MISSING_SCRIPT_DURATION, str(ctx.exception))

    def test_error_is_runtime_error_subclass(self):
        self.assertTrue(issubclass(MissingScriptDurationError, RuntimeError))

    def test_non_text_input_blocks(self):
        for bad in (None, b"**\xe7\xb2\xbe\xe5\x87\x86\xe6\x97\xb6\xe9\x95\xbf**\xef\xbc\x9a120\xe7\xa7\x92", 120):
            with self.subTest(value=bad):
                with self.assertRaises(MissingScriptDurationError):
                    parse_script_duration(bad)


class DeclarationScopeTest(unittest.TestCase):
    """关键词必须真是剧本级声明，不能是"每段/每镜/表格单元格"的时长。"""

    def test_keyword_glued_into_a_longer_word_is_not_a_target(self):
        """``每段时长：5秒`` 描述的是片段，不是整片目标。"""
        with self.assertRaises(MissingScriptDurationError):
            parse_script_duration("本集按每段时长：5秒 拆分")

    def test_earlier_glued_mention_does_not_beat_the_real_header(self):
        text = "参考宣传片时长：8分钟\n\n**总时长**：120秒\n【00:00-02:00】"
        contract = parse_script_duration(text)
        self.assertEqual(contract.target_seconds, 120)
        self.assertEqual(contract.warnings, ())

    def test_table_cell_declaration_is_not_a_script_declaration(self):
        text = (
            "| 镜头号 | 时长 | 画面内容 |\n"
            "|---|---|---|\n"
            "| 1 | 时长：3秒 | 平视近景 |\n"
            "| 2 | 时长：4秒 | 特写 |"
        )
        with self.assertRaises(MissingScriptDurationError):
            parse_script_duration(text)

    def test_per_shot_prefixed_declaration_is_not_a_script_declaration(self):
        with self.assertRaises(MissingScriptDurationError):
            parse_script_duration("镜头1：时长：5秒\n镜头2：时长：5秒")

    def test_line_leading_declaration_is_accepted(self):
        for text in ("时长：120秒", "## 总时长：120秒", "- 精准时长：120秒", "> 时长：120秒"):
            with self.subTest(text=text):
                self.assertEqual(parse_script_duration(text).target_seconds, 120)


class DeclarationContinuationTest(unittest.TestCase):
    """数字在受支持写法之后仍在继续时，必须阻断，禁止截断取首项。"""

    def test_unsupported_continuations_block(self):
        cases = {
            "小数秒": "时长：1分钟30.5秒",
            "中文连接词零": "时长：1分钟零30秒",
            "中文连接词加": "时长：1分钟加15秒",
            "裸数字续写": "时长：1分钟30",
            "加法续写": "时长：90秒+15秒",
            "口语半": "时长：2分钟半",
            "短秒字段": "总时长：01:30:5",
            "秒与分钟粘连": "总时长：01:305",
            "时钟粘连": "总时长：1:2345",
            "小数时钟": "总时长：01:30.5",
            "四段时钟": "总时长：01:30:00:00",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(MissingScriptDurationError):
                    parse_script_duration(text)

    def test_trailing_prose_and_units_do_not_block(self):
        cases = {
            "秒钟": ("时长：120秒钟", 120),
            "注解括号": ("时长：120 秒（含片头）", 120),
            "逗号注解": ("总时长：01:30，共90秒", 90),
            "换行后的标题": ("时长：120秒\n## 【00:00 - 02:00】场景", 120),
            "换行后的编号": ("时长：120秒\n1. 场景一", 120),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(parse_script_duration(text).target_seconds, expected)


class WrongValueRegressionTest(unittest.TestCase):
    """对抗评审实测出的静默错值用例，逐条钉死。"""

    def test_no_case_returns_a_truncated_value(self):
        wrong_values = {
            "每段时长": ("本集按每段时长：5秒 拆分", 5),
            "宣传片时长": ("参考宣传片时长：8分钟\n**总时长**：120秒", 480),
            "表格行": ("| 1 | 时长：3秒 | 近景 |", 3),
            "镜号前缀": ("镜头1：时长：5秒", 5),
            "小数秒": ("时长：1分钟30.5秒", 60),
            "中文零": ("时长：1分钟零30秒", 60),
            "加法": ("时长：90秒+15秒", 90),
            "口语半": ("时长：2分钟半", 120),
            "短秒字段": ("总时长：01:30:5", 90),
            "秒数粘连": ("总时长：01:305", 90),
            "时钟粘连": ("总时长：1:2345", 83),
            "四段时钟": ("总时长：01:30:00:00", 5400),
        }
        for name, (text, forbidden) in wrong_values.items():
            with self.subTest(case=name):
                try:
                    actual = parse_script_duration(text).target_seconds
                except MissingScriptDurationError:
                    continue
                self.assertNotEqual(
                    actual, forbidden, f"{name}: 截断出的错值 {actual} 被当成合法目标时长"
                )


class InternalTimecodeWarningTest(unittest.TestCase):
    """每条触发条件的独立覆盖：起点与终点必须各自可判。"""

    def test_start_only_mismatch_warns(self):
        contract = parse_script_duration("时长：120秒\n## 【00:05 - 02:00】场景")
        self.assertEqual(contract.internal_segments[-1].end_seconds, 120)
        self.assertEqual(contract.warnings, (INTERNAL_TIMECODE_MISMATCH,))

    def test_end_only_mismatch_warns(self):
        contract = parse_script_duration("时长：120秒\n## 【00:00 - 01:50】场景")
        self.assertEqual(contract.internal_segments[0].start_seconds, 0)
        self.assertEqual(contract.warnings, (INTERNAL_TIMECODE_MISMATCH,))

    def test_all_dash_separators_are_accepted(self):
        for separator in ("-", "–", "—", "~", "～"):
            with self.subTest(separator=separator):
                contract = parse_script_duration(f"时长：30秒\n【00:00 {separator} 00:30】一")
                self.assertEqual(contract.internal_segments, (TimeSegment(0, 30, "一"),))
                self.assertEqual(contract.warnings, ())

    def test_fullwidth_colons_are_accepted(self):
        contract = parse_script_duration("时长：100秒\n【00：00 - 01：40】一")
        self.assertEqual(contract.internal_segments, (TimeSegment(0, 100, "一"),))

    def test_long_minute_fields_are_accepted(self):
        contract = parse_script_duration("总时长：6000秒\n## 【00:00 - 100:00】长片")
        self.assertEqual(contract.internal_segments, (TimeSegment(0, 6000, "长片"),))
        self.assertEqual(contract.warnings, ())


class InternalTimecodeTest(unittest.TestCase):
    def test_parses_bracket_timecode_with_label(self):
        contract = parse_script_duration("总时长：110秒\n## 【00:00 - 01:50】雨夜码头")
        self.assertEqual(
            contract.internal_segments, (TimeSegment(0, 110, "雨夜码头"),)
        )
        self.assertEqual(contract.warnings, ())

    def test_parses_multiple_segments_in_document_order(self):
        text = (
            "总时长：90秒\n"
            "## 【00:00 - 00:30】开场\n"
            "## 【00:30 - 01:00】对峙\n"
            "## 【01:00 - 01:30】收束\n"
        )
        contract = parse_script_duration(text)
        self.assertEqual(
            contract.internal_segments,
            (
                TimeSegment(0, 30, "开场"),
                TimeSegment(30, 60, "对峙"),
                TimeSegment(60, 90, "收束"),
            ),
        )
        self.assertEqual(contract.warnings, ())

    def test_parses_square_bracket_and_hour_forms(self):
        text = "总时长：100秒\n[00:00-00:40] 一\n[00:00:40-00:01:40] 二"
        contract = parse_script_duration(text)
        self.assertEqual(
            contract.internal_segments,
            (TimeSegment(0, 40, "一"), TimeSegment(40, 100, "二")),
        )

    def test_parses_parenthesised_timecode_forms(self):
        text = "总时长：60秒\n（00:00-00:30）一\n(00:30-00:60) 二"
        contract = parse_script_duration(text)
        self.assertEqual(
            contract.internal_segments,
            (TimeSegment(0, 30, "一"), TimeSegment(30, 60, "二")),
        )

    def test_label_before_bracket_is_used_and_destriped(self):
        """括号后无文字时退回括号前的行内文字，且剥掉 ``##``/``*`` 装饰。"""
        contract = parse_script_duration("总时长：30秒\n## 开场【00:00 - 00:30】")
        self.assertEqual(
            contract.internal_segments, (TimeSegment(0, 30, "开场"),)
        )

    def test_lone_dot_is_not_a_range_separator(self):
        """``【00:00.00:30】`` 不是合法区间写法，不得被当成片段。"""
        contract = parse_script_duration("时长：30秒\n【00:00.00:30】一")
        self.assertEqual(contract.internal_segments, ())

    def test_warns_when_timecodes_do_not_span_the_header(self):
        text = "总时长：120秒\n## 【00:05 - 00:50】场景"
        contract = parse_script_duration(text)
        self.assertEqual(contract.warnings, (INTERNAL_TIMECODE_MISMATCH,))

    def test_no_timecodes_means_no_warning(self):
        contract = parse_script_duration("总时长：30秒\n没有内部时间码。")
        self.assertEqual(contract.internal_segments, ())
        self.assertEqual(contract.warnings, ())

    def test_crlf_script_lines_keep_labels_clean(self):
        """core.autocrlf=true 下剧本常是 CRLF，行尾 ``\\r`` 不得混进 label。"""
        text = "总时长：110秒\r\n## 【00:00 - 01:50】雨夜码头\r\n"
        contract = parse_script_duration(text)
        self.assertEqual(contract.internal_segments, (TimeSegment(0, 110, "雨夜码头"),))

    def test_non_timecode_brackets_are_not_segments(self):
        text = (
            "总时长：30秒\n"
            "【环境光影】冷调台灯\n"
            "【空间基准】不足十平米的出租屋\n"
            "[镜头 1] 平视近景\n"
        )
        contract = parse_script_duration(text)
        self.assertEqual(contract.internal_segments, ())
        self.assertEqual(contract.warnings, ())


class ContractShapeTest(unittest.TestCase):
    def test_dataclasses_are_frozen(self):
        segment = TimeSegment(0, 10, "一")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            segment.end_seconds = 20
        contract = parse_script_duration("总时长：10秒")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            contract.target_seconds = 20

    def test_sequences_are_tuples(self):
        contract = parse_script_duration("总时长：10秒\n【00:00 - 00:10】一")
        self.assertIsInstance(contract.internal_segments, tuple)
        self.assertIsInstance(contract.warnings, tuple)

    def test_contract_is_value_comparable(self):
        left = parse_script_duration("总时长：10秒\n【00:00 - 00:10】一")
        right = ScriptDurationContract(10, (TimeSegment(0, 10, "一"),), ())
        self.assertEqual(left, right)

    def test_target_is_int(self):
        contract = parse_script_duration("时长：2分钟")
        self.assertIsInstance(contract.target_seconds, int)


if __name__ == "__main__":
    unittest.main()
