#!/usr/bin/env python3
"""Unit tests for generate_prompts and prepare_costume_input parsers (no LLM calls)."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import generate_prompts as gp

from generate_prompts import (
    _should_skip,
    build_costume_map,
    build_default_schedule,
    find_section,
    format_prompt_header,
    load_template,
    parse_asset_table,
    parse_deriv_needs,
    parse_merged_sections,
    parse_schedule_list,
    render_merged_sections,
    should_generate_base,
)
from prepare_costume_input import load_genre_map, parse_table_section


FINAL_TEXT = """# 测试小说

## 人物资产提取表
| 资产ID | 人物姓名 | 别名/称呼 | 出现位置 | 重要等级 | 备注 |
| p1 | 林姑娘 | | 第1章 | 主角 | 女主 |
| p2 | 船夫 | | 第2章 | 配角 | |

## 场景资产提取表
| 资产ID | 场景名称 | 出现位置 | 重要等级 | 备注 |
| s1 | 茶楼 | 第2章 | 重要 | |

## 道具资产提取表
| 资产ID | 道具名称 | 出现位置 | 重要等级 | 备注 |
| pr1 | 琴 | 第3章 | 道具 | |

## 服装妆造资产提取表
| 资产ID | 造型名称 | 绑定人物 | 出现位置 | 重要等级 | 备注 |
| c1 | 淡蓝长裙 | 林姑娘 | 第1章 | 重要 | |
| c2 | 蓑衣 | 船夫 | 第2章 | 普通 | |
| c3 | （未绑定） | | 第3章 | 普通 | |

## 衍生需求汇总
| 类型 | 资产名称 | 需求 | 章节 | 摘句 | 备注 |
| 场景 | 茶楼 | 黄昏 | 第2章 | 夕阳西下 | |
| 道具 | 琴 | 破损 | 第3章 | 琴弦断裂 | |
"""

# 哨兵落在「绑定人物」列的夹具。
# FINAL_TEXT 的 c3 把哨兵写在「造型名称」列、绑定列留空——那条分支（bind == ""）早就有拦截，
# 所以旧夹具从未覆盖到真正的缺陷分支：模型把 待确认 直接写进绑定列。
FINAL_TEXT_PLACEHOLDER_BINDING = """# 测试小说

## 服装妆造资产提取表
| 资产ID | 造型名称 | 绑定人物 | 出现位置 | 重要等级 | 备注 |
| c1 | 淡蓝长裙 | 林姑娘 | 第1章 | 重要 | |
| c4 | 猩红战袍 | 待确认 | 第4章 | 普通 | 散落台阶，出处未明确指明穿着者 |
| c5 | 青玉发带 | 未知 | 第5章 | 普通 | 出处未指明 |

## 衍生需求汇总
| 类型 | 资产名称 | 需求 | 章节 | 摘句 | 备注 |
| 场景 | 茶楼 | 黄昏 | 第2章 | 夕阳西下 | |
"""


class TestParseAssetTable(unittest.TestCase):
    def test_parses_costume_table(self):
        rows = parse_asset_table(FINAL_TEXT, "服装妆造")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][1], "淡蓝长裙")
        self.assertEqual(rows[1][2], "船夫")

    def test_parses_character_table(self):
        rows = parse_asset_table(FINAL_TEXT, "人物资产")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], "林姑娘")


class TestParseScheduleList(unittest.TestCase):
    def test_parses_schedule_rows(self):
        text = """# 衍生调度清单
| 类型 | 资产 | 变体 | 备注 | 画风 |
|---|---|---|---|---|
| 人物 | 林姑娘 | 淡蓝长裙 | 服装造型衍生 | |
| 场景 | 茶楼 | 黄昏 | 剧本驱动 | |
"""
        items = parse_schedule_list(text)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["类型"], "人物")
        self.assertEqual(items[1]["变体"], "黄昏")


class TestParseDerivNeeds(unittest.TestCase):
    def test_parses_needs(self):
        needs = parse_deriv_needs(FINAL_TEXT)
        self.assertEqual(len(needs), 2)
        self.assertEqual(needs[0], ("场景", "茶楼", "黄昏", "第2章"))
        self.assertEqual(needs[1], ("道具", "琴", "破损", "第3章"))


class TestBuildDefaultSchedule(unittest.TestCase):
    def test_includes_bound_costumes_and_deriv_needs(self):
        schedule = build_default_schedule(FINAL_TEXT, "古风")
        self.assertIn("人物 | 林姑娘 | 淡蓝长裙 | 服装造型衍生", schedule)
        self.assertIn("人物 | 船夫 | 蓑衣 | 服装造型衍生", schedule)
        self.assertIn("场景 | 茶楼 | 黄昏 | 剧本驱动", schedule)
        self.assertIn("道具 | 琴 | 破损 | 剧本驱动", schedule)
        # 本意是"未绑定的造型不得变成人物行"。不按裸词判定——头部跳过说明里合法地含"未绑定"三字。
        person_rows = [l for l in schedule.splitlines() if l.startswith("| 人物 |")]
        self.assertNotIn("（未绑定）", "\n".join(person_rows))

    def test_placeholder_binding_is_not_scheduled(self):
        # 待确认/未知 写在绑定列时不得当人名写出，否则衍生步骤按它找父文档必然失败
        schedule = build_default_schedule(FINAL_TEXT_PLACEHOLDER_BINDING, "古风")
        self.assertNotIn("人物 | 待确认", schedule)
        self.assertNotIn("人物 | 未知", schedule)
        self.assertNotIn("| 猩红战袍 | 服装造型衍生", schedule)
        self.assertNotIn("| 青玉发带 | 服装造型衍生", schedule)


class TestScheduleTypeFollowsIndex(unittest.TestCase):
    """F30：调度行的类型以**资产索引**为准；无衍生通道的类型不得写成调度行。

    2026-09-21 全新库实测：模型把服装写在汇总表的「道具」行里（`| 道具 | 月白长裙 | 湿边 |`），
    调度清单照抄 → 衍生步骤按"道具"找父资产 → 抛错停链。
    这里把该行挡在调度之前，并在说明里点名（不静默丢）。
    """

    TEXT = FINAL_TEXT.replace(
        "| 道具 | 琴 | 破损 | 第3章 | 琴弦断裂 | |",
        "| 道具 | 琴 | 破损 | 第3章 | 琴弦断裂 | |\n| 道具 | 月白长裙 | 湿边 | 第3章 | 裙摆沾水 | |")

    def _lib(self, tmp: str) -> Path:
        lib = Path(tmp)
        (lib / ".process").mkdir(parents=True)
        (lib / ".process" / "资产索引.json").write_text(json.dumps({"assets": [
            {"asset_id": "CO_月白长裙", "type": "服装", "name": "月白长裙", "alias_display": ""},
            {"asset_id": "PR_琴", "type": "道具", "name": "琴", "alias_display": ""},
        ]}, ensure_ascii=False), encoding="utf-8")
        return lib

    def test_costume_row_is_not_written_as_a_prop_row(self):
        with tempfile.TemporaryDirectory() as td:
            schedule = build_default_schedule(self.TEXT, "古风", lib=self._lib(td))
        self.assertNotIn("| 道具 | 月白长裙 |", schedule, "服装不得写成道具衍生行")
        self.assertIn("月白长裙", schedule, "被略过的行必须在说明里点名，不许静默消失")

    def test_without_index_behaviour_is_unchanged(self):
        """反例（旧库兼容）：不传库时退回原行为——文本怎么写就怎么调度。"""
        with tempfile.TemporaryDirectory():
            schedule = build_default_schedule(self.TEXT, "古风")
        self.assertIn("| 道具 | 月白长裙 |", schedule)


class TestDerivSkipsChannellessType(unittest.TestCase):
    """F31（防御）：调度清单里若仍有无衍生通道的类型（手改/旧库），衍生步骤要记名略过，不得停链。"""

    def test_costume_job_is_skipped_with_trace(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / ".process").mkdir(parents=True)
            (lib / ".process" / "资产索引.json").write_text(json.dumps({"assets": [
                {"asset_id": "CO_月白长裙", "type": "服装", "name": "月白长裙", "alias_display": ""},
            ]}, ensure_ascii=False), encoding="utf-8")
            (lib / "提示词" / "基础").mkdir(parents=True)
            (lib / "提示词" / "基础" / "道具提示词.md").write_text("## 001. 别的道具\n甲\n", encoding="utf-8")
            items = [{"类型": "道具", "资产": "月白长裙", "变体": "湿边", "备注": ""}]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = gp.generate_deriv(items, "古风", lib, False)
            out = buf.getvalue()
            self.assertEqual(rc, 0, "无通道类型不得把整条链判失败")
            self.assertIn("月白长裙", out, "略过必须留痕")
            events = lib / ".process" / "调用事件.jsonl"
            self.assertTrue(events.is_file(), "略过要落机器可读事件")
            self.assertIn("deriv", events.read_text(encoding="utf-8"))
            self.assertFalse(os.path.exists(lib / "提示词" / "衍生" / "道具提示词.md"), "不得产出空衍生文档")

    def test_real_character_binding_still_scheduled(self):
        # 防误杀：真实人名必须照旧出衍生行
        schedule = build_default_schedule(FINAL_TEXT_PLACEHOLDER_BINDING, "古风")
        self.assertIn("人物 | 林姑娘 | 淡蓝长裙 | 服装造型衍生", schedule)

    def test_empty_binding_row_count_unchanged(self):
        # 防放宽：原夹具的绑定列为空那一行仍被挡掉，且人物行恰好两行
        schedule = build_default_schedule(FINAL_TEXT, "古风")
        person_rows = [l for l in schedule.splitlines() if l.startswith("| 人物 |")]
        self.assertEqual(len(person_rows), 2)

    def test_skipped_costume_is_annotated_in_header(self):
        # 跳过的造型必须留痕，否则衍生需求静默消失；说明落在表格之前，解析器看不到
        schedule = build_default_schedule(FINAL_TEXT_PLACEHOLDER_BINDING, "古风")
        # 切分点必须用「行首的表格头」，不能用裸的 "| 类型 |"——头部说明行里也有这四个字
        head = schedule.split("\n| 类型 |")[0]
        notes = [l for l in head.splitlines() if l.startswith("> ") and "跳过" in l]
        self.assertEqual(len(notes), 1, f"头部应有且仅有一条跳过说明，实际：{notes}")
        self.assertIn("猩红战袍", notes[0])
        self.assertIn("青玉发带", notes[0])

    def test_no_annotation_when_nothing_skipped(self):
        # 全部是真名绑定时，头部不得多出说明行（保持既有输出的字节不变）
        text = """# 测试小说

## 服装妆造资产提取表
| 资产ID | 造型名称 | 绑定人物 | 出现位置 | 重要等级 | 备注 |
| c1 | 淡蓝长裙 | 林姑娘 | 第1章 | 重要 | |

## 衍生需求汇总
| 类型 | 资产名称 | 需求 | 章节 | 摘句 | 备注 |
| 场景 | 茶楼 | 黄昏 | 第2章 | 夕阳西下 | |
"""
        schedule = build_default_schedule(text, "古风")
        self.assertNotIn("跳过", schedule)

    def test_placeholder_name_still_annotated_with_asset_id(self):
        """对抗评审 B2：绑定列与造型名**都**是哨兵时仍必须留痕，用资产 ID 兜底。

        否则该行既不出人物行、也没有跳过说明——真实衍生任务完全不可见地消失。
        """
        text = """# 测试小说

## 服装妆造资产提取表
| 资产ID | 造型名称 | 绑定人物 | 出现位置 | 重要等级 | 备注 |
| CO_007 | 未知 | 待确认 | 第4章 | 普通 | 出处未明确指明穿着者 |

## 衍生需求汇总
| 类型 | 资产名称 | 需求 | 章节 | 摘句 | 备注 |
| 场景 | 茶楼 | 黄昏 | 第2章 | 夕阳西下 | |
"""
        schedule = build_default_schedule(text, "古风")
        head = schedule.split("\n| 类型 |")[0]
        notes = [l for l in head.splitlines() if l.startswith("> ") and "跳过" in l]
        self.assertEqual(len(notes), 1, f"应留一条说明，实际：{notes}")
        self.assertIn("CO_007", notes[0])
        self.assertIn("绑定列=待确认", notes[0])
        # 该行既不得变成人物行（占用哨兵当人名）
        self.assertNotIn("人物 | 待确认", schedule)
        self.assertNotIn("人物 | 未知", schedule)


class TestBuildCostumeMap(unittest.TestCase):
    def test_maps_character_to_costumes(self):
        costumes = build_costume_map(FINAL_TEXT)
        self.assertIn("林姑娘", costumes)
        self.assertIn("淡蓝长裙", costumes["林姑娘"])
        self.assertIn("船夫", costumes)
        self.assertNotIn("（未绑定）", costumes)

    def test_costume_map_keeps_narrow_guard(self):
        """对抗评审 B1：`build_costume_map` 是**查表**（键=角色名），刻意保持窄口径。

        只挡显式占位符，**不**认 待确认/未知 这类词——否则合法人名被误判成未绑定，
        该角色的服装会静默绑不上且毫无留痕。实测缺陷只出在 `build_default_schedule` 那边。
        """
        text = """# 测试小说

## 服装妆造资产提取表
| 资产ID | 造型名称 | 绑定人物 | 出现位置 | 重要等级 | 备注 |
| c1 | 淡蓝长裙 | 林姑娘 | 第1章 | 重要 | |
| c2 | 蓑衣 | （未绑定） | 第2章 | 普通 | |
| c3 | 头巾 | (未绑定) | 第3章 | 普通 | |
| c4 | 腰带 | 未绑定 | 第4章 | 普通 | |
| c5 | 猩红战袍 | 待确认 | 第5章 | 普通 | 出处未明确指明穿着者 |
"""
        costumes = build_costume_map(text)
        self.assertIn("林姑娘", costumes)
        self.assertIn("淡蓝长裙", costumes["林姑娘"])
        # 三种显式占位符（全角/半角/裸写）都不得成为键——调度侧跳过它们，查表侧必须一致
        for placeholder in ("（未绑定）", "(未绑定)", "未绑定"):
            self.assertNotIn(placeholder, costumes)
        # 宽泛哨兵**保留**为键：有意的窄口径，它只会被人物表里的真名查到，查不到即无害
        self.assertIn("待确认", costumes)


class TestShouldSkip(unittest.TestCase):
    def test_skip_existing_large_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "out.md"
            p.write_text("x" * 200, encoding="utf-8")
            self.assertTrue(_should_skip(p, force=False))
            self.assertFalse(_should_skip(p, force=True))

    def test_do_not_skip_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "missing.md"
            self.assertFalse(_should_skip(p, force=False))


class TestLoadGenreMap(unittest.TestCase):
    def test_loads_known_genres(self):
        gmap = load_genre_map()
        self.assertIsInstance(gmap, dict)
        # 古风 is a known genre in the reference file.
        if "古风" in gmap:
            drama, approx = gmap["古风"]
            self.assertTrue(drama)


class TestParseTableSection(unittest.TestCase):
    def test_parses_section(self):
        text = """## 人物资产提取表
| 资产ID | 人物姓名 | 备注 |
| p1 | 林姑娘 | 女主 |
| p2 | 船夫 | |

## 其他
"""
        rows = parse_table_section(text, "人物资产提取表")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], "林姑娘")

    def test_missing_section_returns_empty(self):
        self.assertEqual(parse_table_section("# 无表\n", "人物资产提取表"), [])


class TestMergedPersonDocument(unittest.TestCase):
    def test_parse_merged_sections(self):
        text = "# 人物基础提示词\n\n## 001. 林姑娘\n\n内容一\n\n---\n\n## 002. 船夫\n\n内容二\n\n---\n"
        sections = parse_merged_sections(text)
        self.assertEqual(sections, {"林姑娘": "内容一", "船夫": "内容二"})

    def test_render_numbered_sections(self):
        rendered = render_merged_sections("人物基础提示词（按序号）", "> 说明", [("林姑娘", "内容一"), ("船夫", "内容二")])
        self.assertIn("## 001. 林姑娘", rendered)
        self.assertIn("## 002. 船夫", rendered)
        self.assertLess(rendered.index("001"), rendered.index("002"))

    def test_find_section(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "人物提示词.md"
            p.write_text("# 人物基础提示词\n\n## 001. 林姑娘\n\n内容一\n\n---\n", encoding="utf-8")
            self.assertEqual(find_section(p, "林姑娘"), "内容一")
            self.assertEqual(find_section(p, "不存在"), "")


class TestCharacterBaseTemplate(unittest.TestCase):
    def test_template_uses_style_block_for_character(self):
        system, _ = load_template("基础提示词模板-人物.md")
        self.assertIn("{style_block}", system)
        self.assertNotIn("{render_block}", system)

    def test_template_respects_visual_description_costume(self):
        system, _ = load_template("基础提示词模板-人物.md")
        self.assertIn("视觉描述含服装/妆发/配饰", system)
        self.assertIn("直接以视觉描述为准", system)
        self.assertIn("不得改写为素色布衣", system)
        self.assertIn("玄幻/仙侠/神魔=女性默认华丽仙气长裙", system)
        self.assertIn("高跟鞋", system)
        self.assertIn("男性按身份任选华丽仙甲", system)
        self.assertIn("拒绝低跟/平底保守古装", system)

    def test_skill_doc_requires_crop_settlement_and_recheck(self):
        """裁头版与结算后复检的要求必须仍可查（阶段 8.1 后细则在核心反推契约里）。"""
        root = Path(__file__).resolve().parents[2]
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        core = (root / "references" / "核心反推契约.md").read_text(encoding="utf-8")
        self.assertIn("是否同时需要裁头版人物提示词", core)
        self.assertIn("--view-mode crop", core)
        self.assertIn("复跑步骤 7", core)
        self.assertIn("references/核心反推契约.md", skill, "主入口必须能路由到核心契约")

    def test_person_template_requires_render_lock_verbatim(self):
        system, _ = load_template("基础提示词模板-人物.md")
        self.assertIn("渲染锁定段（代码已按画风卡逐字组装", system)
        self.assertIn("渲染锁定：", system)

    def test_template_uses_three_panel_board(self):
        system, _ = load_template("基础提示词模板-人物.md")
        self.assertIn("三格图板", system)
        self.assertIn("正面特写", system)
        self.assertIn("正视图", system)
        self.assertIn("后视图", system)
        self.assertIn("{view_mode}", system)
        self.assertNotIn("四视图", system)
        self.assertNotIn("NPR/赛璐璐", system)

    def test_deriv_template_uses_three_panel_board_and_view_mode(self):
        system, _ = load_template("衍生提示词模板.md")
        self.assertIn("三格图板", system)
        self.assertIn("正面特写", system)
        self.assertIn("正视图", system)
        self.assertIn("后视图", system)
        self.assertIn("{view_mode}", system)
        self.assertNotIn("双图锚点", system)

    def test_deriv_template_requires_render_lock_verbatim(self):
        system, _ = load_template("衍生提示词模板.md")
        self.assertIn("渲染锁定段（代码已按画风卡逐字组装", system)
        self.assertIn("渲染锁定：", system)

    def test_template_negative_rejects_cel_shading(self):
        system, _ = load_template("基础提示词模板-人物.md")
        self.assertIn("无赛璐璐描边", system)
        self.assertIn("无平涂感", system)

    def test_template_requires_pure_chinese_output(self):
        system, _ = load_template("基础提示词模板-人物.md")
        self.assertIn("全程纯中文", system)
        self.assertIn("禁止出现英文单词", system)
        self.assertNotIn("中文为主可含英文标签", system)
        self.assertNotIn("PBR材质", system)

    def test_prop_template_uses_dynamic_view_planning(self):
        system, _ = load_template("基础提示词模板-道具.md")
        self.assertIn("动态视图规划", system)
        self.assertIn("正面图", system)
        self.assertIn("侧面图", system)
        self.assertIn("背面图", system)
        self.assertNotIn("四宫格", system)

    def test_scene_and_prop_templates_are_pure_chinese(self):
        for fname in ("基础提示词模板-场景.md", "基础提示词模板-道具.md", "衍生提示词模板.md"):
            system, _ = load_template(fname)
            self.assertIn("全程纯中文", system)
            self.assertNotIn("PBR材质", system)
            self.assertNotIn("NPR", system)


class TestPromptHeaderAndP3Skip(unittest.TestCase):
    """ADR-0084 提示词节头与 P3 排除（工单 6）。"""

    def test_header_uses_chinese_keys_only(self):
        import re
        header = format_prompt_header({
            "asset_id": "CH_林姑娘",
            "stable_key": "CHR:00001:1",
            "fact_level": "剧本事实",
            "priority": "P1",
            "dependencies": ["CO_淡蓝长裙"],
            "evidence": [{"chapter": "第1章", "segment": "段01", "line": "00002", "quote": "林姑娘走进茶楼"}],
        })
        self.assertIn("资产编号", header)
        self.assertIn("稳定键", header)
        self.assertIsNone(re.search(r"[A-Za-z]{2,}", header.replace("CH_林姑娘", "").replace("CHR:00001:1", "").replace("CO_淡蓝长裙", "")))

    def test_p3_still_generates_base_prompt(self):
        # 工单 4：P3 不再吞掉基础提示词（单次出现/优先级都不作为跳过理由）
        ok, reason = should_generate_base({"priority": "P3", "fact_level": "剧本事实", "story_function": "背景陈设"})
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_pending_conflict_produces_blocked_draft(self):
        # 待定冲突产出「带阻断标识的草案」而非无声跳过
        ok, note = should_generate_base({"priority": "P1", "fact_level": "待定冲突"})
        self.assertTrue(ok)
        self.assertIn("阻断", note)

    def test_non_asset_or_cancel_is_only_legal_skip(self):
        ok, reason = should_generate_base({"exclude_reason": "非资产：天气现象", "type": "道具"})
        self.assertFalse(ok)
        self.assertIn("非资产", reason)
        ok2, _ = should_generate_base({"exclude_reason": "用户取消", "type": "场景"})
        self.assertFalse(ok2)

    def test_normal_asset_generates(self):
        ok, _ = should_generate_base({"priority": "P1", "fact_level": "剧本事实"})
        self.assertTrue(ok)
        ok2, _ = should_generate_base(None)
        self.assertTrue(ok2)

    def test_schedule_parses_master_columns(self):
        text = "| 类型 | 资产 | 变体 | 备注 | 画风 | 母版编号 | 仅允许变化 |\n| 场景 | 茶楼 | 黄昏 | 剧本驱动 | | SC_茶楼 | 仅时段变化 |"
        items = parse_schedule_list(text)
        self.assertEqual(items[0].get("母版编号"), "SC_茶楼")
        self.assertEqual(items[0].get("仅允许变化"), "仅时段变化")

    def test_default_schedule_has_master_columns(self):
        schedule = build_default_schedule(FINAL_TEXT, "古风")
        self.assertIn("母版编号", schedule)
        self.assertIn("仅允许变化", schedule)
        items = parse_schedule_list(schedule)
        scenes = [it for it in items if it["类型"] == "场景"]
        self.assertTrue(scenes)
        # 工单C：场景行默认填母版 ID，非空且与资产名对应
        for it in scenes:
            self.assertTrue(it.get("母版编号", "").startswith("SC_"))
            self.assertIn(it["资产"], it.get("母版编号", ""))

    def test_scene_master_block_injected(self):
        import tempfile
        from pathlib import Path
        from generate_prompts import _build_scene_master_block
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / "连续性").mkdir(parents=True)
            (lib / "连续性" / "场景母版.md").write_text(
                "## SC_茶楼 茶楼\n| 字段 | 内容 |\n|---|---|"
                "\n| 资产ID | SC_茶楼 |\n| 空间结构 | 木构楼阁（第1章段01行00001） |\n"
                "| 固定锚点 | 临街栏杆（第1章段01行00002） |\n| 入口出口 | 未说明 |\n"
                "| 连接点 | 未说明 |\n| 材质与光源 | 未说明 |\n",
                encoding="utf-8")
            block = _build_scene_master_block(
                lib, {"资产": "茶楼", "变体": "黄昏", "母版编号": "SC_茶楼", "仅允许变化": "仅时段变化"})
            self.assertIn("SC_茶楼", block)
            self.assertIn("临街栏杆", block)
            self.assertIn("仅时段变化", block)
            self.assertIn("未说明", block)

    def test_scene_master_missing_blocks(self):
        import tempfile
        from pathlib import Path
        from generate_prompts import _build_scene_master_block
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            # 旧库/旧单测无母版：降级为空约束（新流程聚合必写母版，check 侧仍要求存在）
            block = _build_scene_master_block(
                lib, {"资产": "茶楼", "变体": "黄昏", "母版编号": "SC_茶楼", "仅允许变化": ""})
            self.assertIn("SC_茶楼", block)
            self.assertIn("未说明", block)


class TestBaseSkipListContract(unittest.TestCase):
    """F4.2 追更发现：跳过清单行首必须是资产ID，check_schedule 按ID认合法排除。"""

    def test_skip_entry_starts_with_asset_id(self):
        from generate_prompts import format_skip_entry
        line = format_skip_entry("SC_林姑娘走进客栈", "场景", "林姑娘走进客栈", "P3 跳过：普通陈设/背景")
        self.assertTrue(line.startswith("SC_林姑娘走进客栈"))
        self.assertIn("P3 跳过", line)
        # 状态机与 check_schedule 的匹配方式：aid in text
        self.assertIn("SC_林姑娘走进客栈", line)

    def test_load_base_skip_text(self):
        import tempfile
        from check_schedule import load_base_skip_text
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self.assertEqual(load_base_skip_text(base), "")
            (base / "跳过清单.md").write_text("- SC_茶楼 场景「茶楼」P3 跳过" + chr(10), encoding="utf-8")
            self.assertIn("SC_茶楼", load_base_skip_text(base))


class TestCoverageReadsGeneratorSkipList(unittest.TestCase):
    """覆盖率门必须读得懂**生成侧写的**跳过清单（实测：绑定待确认的服装被误判为违规）。

    生成侧 `format_skip_entry` 写的是行首资产 ID 的短横条目；`check_asset_coverage.load_skipped`
    历史上只认表格行 → 生成侧写的豁免本门完全看不见，一律判「无基础提示词且无明确豁免原因」。
    """

    def _lib(self, tmp: str, line: str) -> Path:
        lib = Path(tmp)
        base = lib / "提示词" / "基础"
        base.mkdir(parents=True, exist_ok=True)
        (base / "跳过清单.md").write_text(line, encoding="utf-8")
        return lib

    def test_bullet_entry_is_parsed(self):
        import tempfile
        from check_asset_coverage import load_skipped
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(td, "- CO_猩红战袍 服装「猩红战袍」绑定待确认：暂无可挂载的人物" + chr(10))
            skipped = load_skipped(str(lib))
            self.assertIn("CO_猩红战袍", skipped)
            self.assertIn("待确认", skipped["CO_猩红战袍"])

    def test_table_entry_still_parsed(self):
        """老格式（表格行）不得因为新增解析而失效。"""
        import tempfile
        from check_asset_coverage import load_skipped
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(td, "| CO_月白长裙 | 用户取消 |" + chr(10))
            self.assertEqual(load_skipped(str(lib)).get("CO_月白长裙"), "用户取消")

    def test_unbound_costume_is_not_a_violation(self):
        import tempfile
        from check_asset_coverage import evaluate, load_skipped
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(td, "- CO_猩红战袍 服装「猩红战袍」绑定待确认：暂无可挂载的人物" + chr(10))
            ledger = [{"asset_id": "CO_猩红战袍", "asset_type": "服装", "canonical_name": "猩红战袍",
                       "ledger_status": "formal", "required_outputs": ["base_prompt"]}]
            report = evaluate(ledger, set(), load_skipped(str(lib)))
            self.assertEqual(report["violations"], [], report["violations"])
            self.assertEqual(report["counts"]["pending_or_blocked"], 1)

    def test_unknown_reason_still_counts_as_violation(self):
        """**反例（不得放宽）**：理由不含合法标记时照旧判违规。"""
        import tempfile
        from check_asset_coverage import evaluate, load_skipped
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(td, "- CO_猩红战袍 服装「猩红战袍」回头再说" + chr(10))
            ledger = [{"asset_id": "CO_猩红战袍", "asset_type": "服装", "canonical_name": "猩红战袍",
                       "ledger_status": "formal", "required_outputs": ["base_prompt"]}]
            report = evaluate(ledger, set(), load_skipped(str(lib)))
            self.assertEqual(len(report["violations"]), 1, report)


class TestSkipListSingleParser(unittest.TestCase):
    """F19：跳过清单此前有**三个读者、两种文法、一套子串匹配**，收成一份解析层。

    实测（2026-09-21 全库扫查）：
    ① 表格行只有覆盖率门认，`check_schedule.legal_skip_ids` 看不见 → 覆盖率门说没问题、
       verify 报「缺基础提示词」，两边都"各自正确"却白跑一整趟真端点；
    ② `_match_skip` 的双向子串让豁免外溢：清单只有 `PR_剑`，无任何产物的 `PR_剑鞘` 判 PASS。
    """

    def _lib(self, tmp, line: str) -> Path:
        lib = Path(tmp)
        base = lib / "提示词" / "基础"
        base.mkdir(parents=True, exist_ok=True)
        (base / "跳过清单.md").write_text(line, encoding="utf-8")
        return lib

    def test_table_entry_is_visible_to_both_gates(self):
        """同一份产物，两条门必须读同一份解析结果（表格行是旧库合法格式）。"""
        import tempfile
        from check_asset_coverage import load_skipped
        from check_schedule import legal_skip_ids
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(td, "| CH_张三 | 非资产：泛指路人 |" + chr(10))
            self.assertIn("CH_张三", load_skipped(str(lib)))
            self.assertIn("CH_张三", legal_skip_ids(lib / "提示词" / "基础"))

    def test_fullwidth_colon_entry_parses_id_exactly(self):
        """无空格的「- CH_x：理由」必须取到干净 ID（旧的 \\S+ 会把理由一起吃进 key）。"""
        import tempfile
        from check_asset_coverage import load_skipped
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(td, "- CH_张三：非资产，泛指人群" + chr(10))
            self.assertEqual(list(load_skipped(str(lib))), ["CH_张三"])

    def test_prefix_id_must_not_exclude_sibling(self):
        """反例（判据不削弱）：清单有 `CH_侍卫甲`，不得放行缺提示词的 `CH_侍卫`。"""
        import tempfile
        from check_asset_coverage import evaluate, load_skipped
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(td, "- CH_侍卫甲 非资产：群演不单独建档" + chr(10))
            ledger = [{"asset_id": "CH_侍卫", "asset_type": "人物", "canonical_name": "侍卫",
                       "ledger_status": "formal", "required_outputs": ["base_prompt"]}]
            report = evaluate(ledger, set(), load_skipped(str(lib)))
            self.assertEqual(report["counts"]["violations"], 1, report)

    def test_listed_asset_is_still_excluded(self):
        """反面（不得矫枉过正）：清单里的 `CH_侍卫甲` 自己仍然合法排除。"""
        import tempfile
        from check_asset_coverage import evaluate, load_skipped
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(td, "- CH_侍卫甲 非资产：群演不单独建档" + chr(10))
            ledger = [{"asset_id": "CH_侍卫甲", "asset_type": "人物", "canonical_name": "侍卫甲",
                       "ledger_status": "formal", "required_outputs": ["base_prompt"]}]
            report = evaluate(ledger, set(), load_skipped(str(lib)))
            self.assertEqual(report["violations"], [], report)


class TestMergedSectionParserSingleSource(unittest.TestCase):
    """F33：合并文档的分节解析此前在生成端与检查器各有一份**逐字节相同**的实现。

    两份同时正确、同时被改坏才出事，所以它不会报错，只会悄悄分叉：生成端按新规则写节名、
    检查器按旧规则读，产物立刻被判成"缺项"，而且看起来像是模型没生成。收成一份后只剩一个改点。
    """

    def test_both_modules_share_one_parser_object(self):
        """生成端与检查器必须拿到**同一个函数对象**，不是两份同源代码。"""
        import check_schedule
        import engine.prompt_rules as pr
        self.assertIs(gp.parse_merged_sections, check_schedule.parse_merged_sections)
        self.assertIs(gp.parse_merged_sections, pr.parse_merged_sections)

    def test_re_export_still_parses_sections(self):
        """再导出不得改行为：三处解析同一份文本结果必须一致。"""
        text = ("# 标题" + chr(10) * 2 + "## 001. 张三" + chr(10) + "正文甲" + chr(10) + "---" + chr(10) +
                "## 002. 李四" + chr(10) + "正文乙" + chr(10))
        from check_schedule import parse_merged_sections as from_checker
        expected = {"张三": "正文甲", "李四": "正文乙"}
        self.assertEqual(from_checker(text), expected)
        self.assertEqual(gp.parse_merged_sections(text), expected)


class TestPromptCoverageExactMatch(unittest.TestCase):
    """F19 连带：状态机的提示词覆盖判定也不许用子串（索引「李寻」被「李寻欢」的节放行）。"""

    def _lib(self, td: str, prompt_title: str, name: str, alias: str) -> Path:
        lib = Path(td)
        (lib / "提示词" / "基础").mkdir(parents=True)
        (lib / "提示词" / "基础" / "人物提示词.md").write_text(
            f"## 001. {prompt_title}\n正面特写\n", encoding="utf-8")
        (lib / ".process").mkdir(parents=True, exist_ok=True)
        (lib / ".process" / "资产索引.json").write_text(json.dumps({"assets": [
            {"asset_id": f"CH_{name}", "name": name, "type": "人物", "priority": "P1",
             "fact_level": "剧本事实", "alias_display": alias}]}, ensure_ascii=False), encoding="utf-8")
        (lib / "提示词" / "基础" / "跳过清单.md").write_text("", encoding="utf-8")
        return lib

    def test_substring_name_does_not_satisfy_coverage(self):
        import tempfile
        from engine.pipeline_state import _check_prompt_coverage
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(td, "李寻欢", "李寻", "")
            result = _check_prompt_coverage(lib, lib / ".process" / "资产索引.json")
            self.assertIsNotNone(result, "「李寻」不得被「李寻欢」的节当成已覆盖")
            self.assertEqual(result[0], "BASE_PROMPT_MISSING")

    def test_registered_alias_still_satisfies_coverage(self):
        """反例（不得矫枉过正）：节标题用索引登记的别名时仍算覆盖。"""
        import tempfile
        from engine.pipeline_state import _check_prompt_coverage
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(td, "林小姐", "林姑娘", "林小姐")
            self.assertIsNone(_check_prompt_coverage(lib, lib / ".process" / "资产索引.json"))


class TestUnboundCostumeLeavesTrace(unittest.TestCase):
    """绑定悬空的服装必须在跳过清单里留痕，否则覆盖率门报「既无产物又无原因」。"""

    # 合规人物桩（硬要求齐全；否则桩输出会被生成闸判不合格而走重试路径）
    PERSON_PROMPT = (
        "国漫三维角色三格图板设定图。左面人物正面特写，右面正视图与后视图，"
        "素灰背景#B8B8B8，均匀柔光，物理渲染材质，国漫三维。全身立像。图中不要有任何文字。"
        "身份锁：鹅蛋脸清秀，杏眼温和，鼻梁挺直，黑发高束。"
        "负面提示词：后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。"
    )

    FINAL = (
        "## 一、人物资产提取表\n| 资产ID | 人物姓名 | 别名/称呼 | 出现位置 | 重要等级 | 备注 |\n"
        "|---|---|---|---|---|---|\n| CH_林姑娘 | 林姑娘 |  |  | S |  |\n\n"
        "## 五、服装妆造资产提取表\n| 资产ID | 造型名称 | 绑定人物 | 出现位置 | 重要等级 | 备注 |\n"
        "|---|---|---|---|---|---|\n"
        "| CO_月白长裙 | 月白长裙 | 林姑娘 |  | A | 常服 |\n"
        "| CO_猩红战袍 | 猩红战袍 | 待确认 |  | B | 未见穿戴者 |\n"
    )

    def test_unbound_costume_recorded_with_reason(self):
        import tempfile
        from unittest import mock

        import generate_prompts as gp
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=lambda *a, **k: (self.PERSON_PROMPT, {})):
                gp.generate_base([("人物", ["CH_林姑娘", "林姑娘", "", "第1章", "S", ""])],
                                 "古风", lib, False, text=self.FINAL)
            skip = (lib / "提示词" / "基础" / "跳过清单.md").read_text(encoding="utf-8")
            self.assertIn("CO_猩红战袍", skip)
            self.assertIn("待确认", skip)
            self.assertNotIn("CO_月白长裙", skip, "已绑定（林姑娘）的造型不该进跳过清单")


if __name__ == "__main__":
    unittest.main()
