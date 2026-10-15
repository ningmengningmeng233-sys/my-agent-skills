#!/usr/bin/env python3
"""Unit tests for check_schedule parsing and content checks (no LLM calls)."""
import json
import tempfile
import unittest
from pathlib import Path

from style import parse_style_card

from check_schedule import (
    DERIV_CHECKS,
    check_ai_flavor,
    check_emphasis_format,
    check_episode_refs,
    check_scene_duplicates,
    check_start_state,
    format_content_bad_line,
    PLACEHOLDER_NAMES,
    is_pure_fantasy,
    build_style_lock_checks,
    check_content,
    check_prompt_content,
    deriv_prompt_gaps,
    load_index_aliases,
    parse_asset_names,
    parse_merged_sections,
    parse_pending_non_assets,
    parse_project_genre,
    split_names,
)


class TestContentBadReporting(unittest.TestCase):
    """报告层：黑名单**命中**不得写成「缺」。

    实跑报文原样是「人物提示词.md#渡口撑船的阿锦: 缺抽象空洞词·极致」——
    读的人会去找一个不存在的缺失项，而真实含义是正文里出现了违禁词。
    """

    def test_abstract_word_hit_reads_as_hit(self):
        labels = check_ai_flavor("## 001. 甲\n他的眼神极致华丽，气质唯美。\n")
        line = format_content_bad_line("人物提示词.md#甲", labels)
        self.assertIn("命中", line)
        self.assertIn("抽象空洞词", line)
        self.assertIn("极致", line)
        self.assertNotIn("缺", line, f"命中项不得写成「缺」：{line}")

    def test_missing_marker_still_reads_as_missing(self):
        """反例（不得把真缺失也写成命中）。"""
        line = format_content_bad_line("场景提示词.md#乙", ["镜头体系", "纯中文"])
        self.assertEqual(line, "场景提示词.md#乙: 缺镜头体系、纯中文")

    def test_both_kinds_are_listed_separately(self):
        line = format_content_bad_line("道具提示词.md#丙", ["三视图"] + check_ai_flavor("气势震撼。"))
        self.assertIn("缺三视图", line)
        self.assertIn("命中抽象空洞词·震撼", line)
        self.assertLess(line.index("缺三视图"), line.index("命中"))


_FINAL_TEMPLATE = """## 人物资产提取表
| 资产ID | 名称 | 别名 | 出现章节 | 重要等级 | 备注 |
|---|---|---|---|---|---|
| CH_林姑娘 | 林姑娘 | 林小姐 | 第1章 | S | 女主 |

## 分集/章节资产使用表
| 章节 | 剧情概要 | 出场人物 | 主要场景 | 关键道具 | 服装造型 |
|---|---|---|---|---|---|
| 第五章 身世 | 众人登楼远眺。 | {cast} | | | |

## 待确认附录（出处未验证/违规名，未进入正式表）
| 名称 | 类型 | 说明 |
|---|---|---|
{appendix}"""


def _final_md(cast: str, appendix: str) -> str:
    return _FINAL_TEMPLATE.format(cast=cast, appendix=appendix)


class TestEpisodeRefExclusions(unittest.TestCase):
    """分集引用的合法排除（工单 F7）。

    实测：裁决已把「众人」判为「泛指人群代词，**不单独建档**」并落进待确认附录——
    「不建档」是**已有结论**，不是待决问题；而引用检查只认"是否在资产表"，
    于是 verify 永远判 `第五章 人物「众人」` 未建档，签不出去。
    """

    NON_ASSET_ROW = "| 众人 | 人物 | 泛指人群代词，不单独建档 |"
    PENDING_ROW = "| 众人 | 人物 | 出处未验证，需人工裁决 |"

    def test_pending_non_asset_is_a_legal_exclusion(self):
        ref_missing, legal_skips = check_episode_refs(
            _final_md("林姑娘、众人", self.NON_ASSET_ROW))
        self.assertEqual(ref_missing, [], f"已判定非资产的名字不该算缺口：{ref_missing}")
        self.assertTrue(any("众人" in s for s in legal_skips), "放行必须留明细，不能静默")
        self.assertIn("不单独建档", legal_skips[0])

    def test_unknown_name_still_blocks(self):
        """**反例（不得放宽）**：既不在正式表也不在待确认附录的名字照旧阻断。"""
        ref_missing, legal_skips = check_episode_refs(
            _final_md("林姑娘、张三", self.NON_ASSET_ROW))
        self.assertEqual(legal_skips, [])
        self.assertTrue(any("张三" in m and "未在资产表建档" in m for m in ref_missing), ref_missing)

    def test_pending_with_other_reason_still_blocks(self):
        """**反例**：附录里写着别的理由（出处未验证）的项照旧阻断，并保留人工出口提示。"""
        ref_missing, legal_skips = check_episode_refs(
            _final_md("林姑娘、众人", self.PENDING_ROW))
        self.assertEqual(legal_skips, [])
        self.assertTrue(any("众人" in m and "待确认附录" in m for m in ref_missing), ref_missing)

    def test_alias_still_counts_as_registered(self):
        """别名引用照旧通过（不得因为新分支而把别名路径弄丢）。"""
        ref_missing, _ = check_episode_refs(_final_md("林小姐", self.NON_ASSET_ROW))
        self.assertEqual(ref_missing, [])

    def test_pending_non_asset_parser_only_reads_the_appendix(self):
        """只有待确认附录的说明列参与判定，分集表与资产表里的同名文字不算。"""
        text = _final_md("林姑娘", self.NON_ASSET_ROW)
        self.assertEqual(parse_pending_non_assets(text), {"众人"})
        self.assertEqual(parse_pending_non_assets(_final_md("林姑娘", self.PENDING_ROW)), set())


class TestEpisodeRefIndexAliases(unittest.TestCase):
    """被合并的旧名：场景/道具表**没有别名列**，旧名只落在资产索引上（工单 T9）。

    实测：裁决把「衙门口」并入 `SC_守备前厅`（裁决表的别名列也写了），
    章节表按旧名引用仍被判「未在资产表建档」——因为 Markdown 里那列根本没渲染，
    而引用检查只看 Markdown。索引里有结构化字段，读它不算"猜自然语言备注"。
    """

    def _lib_with_alias(self, tmp: Path, alias: str = "衙门口") -> Path:
        lib = Path(tmp)
        (lib / ".process").mkdir(parents=True, exist_ok=True)
        (lib / ".process" / "资产索引.json").write_text(json.dumps({
            "assets": [
                {"asset_id": "SC_守备前厅", "type": "场景", "name": "守备前厅", "alias_display": alias},
                {"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘", "alias_display": ""},
            ]}, ensure_ascii=False), encoding="utf-8")
        return lib

    def test_index_alias_resolves_the_old_name(self):
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib_with_alias(td)
            aliases = load_index_aliases(lib)
            self.assertEqual(aliases, {"衙门口"})
            ref_missing, _ = check_episode_refs(_final_md("林姑娘、衙门口", "| 甲 | 人物 | 其他 |"),
                                                extra_names=aliases)
            self.assertEqual(ref_missing, [], f"索引里已登记的别名不该算缺口：{ref_missing}")

    def test_index_alias_must_match_exactly(self):
        """W1（评审发现）：索引别名只能**精确**命中，不得用子串放行别的名字。

        实测：索引别名写了「府」，章节表写「摄政王府」，模糊匹配（`n in a or a in n`）
        就把后者一并放行——引用缺口被静默吃掉，而这条检查是 verify 的门之一。
        """
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib_with_alias(td, alias="府")
            aliases = load_index_aliases(lib)
            ref_missing, _ = check_episode_refs(_final_md("林姑娘、摄政王府", "| 甲 | 人物 | 其他 |"),
                                                extra_names=aliases)
            self.assertTrue(any("摄政王府" in r for r in ref_missing),
                            f"短别名不得放行不相关名称：{ref_missing}")

    def test_without_the_index_behaviour_is_unchanged(self):
        """**反例（不得放宽）**：索引缺失时退回原行为——名字照旧判缺口。"""
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_index_aliases(Path(td)), set())
            ref_missing, _ = check_episode_refs(_final_md("林姑娘、衙门口", "| 甲 | 人物 | 其他 |"))
            self.assertTrue(any("衙门口" in m for m in ref_missing), ref_missing)

    def test_unreadable_index_degrades_quietly(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / ".process").mkdir(parents=True, exist_ok=True)
            (lib / ".process" / "资产索引.json").write_text("{ 不是 JSON", encoding="utf-8")
            self.assertEqual(load_index_aliases(lib), set())

    def test_unknown_name_still_blocks_with_index_present(self):
        """**反例**：索引在，但名字既不是资产名也不是别名 → 照旧阻断。"""
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib_with_alias(td)
            ref_missing, _ = check_episode_refs(_final_md("林姑娘、张老三", "| 甲 | 人物 | 其他 |"),
                                                extra_names=load_index_aliases(lib))
            self.assertTrue(any("张老三" in m and "未在资产表建档" in m for m in ref_missing), ref_missing)


class TestDerivRequirementChannelRule(unittest.TestCase):
    """F32：检查器不得索要"没有衍生通道的类型"的衍生提示词。

    实测（2026-09-21 全新库）：模型把两件服装写成调度行 `| 道具 | 月白长裙 | 湿边 |`；
    生成侧已按索引识别为服装并记名略过（F31），而 check_schedule 照旧索要 → 失败只是从
    第 9 步搬到了 verify（CHAIN_RC=1 停在步骤 10）。规则只能有一份。
    """

    def _lib(self, tmp: str, schedule_row: str) -> Path:
        lib = Path(tmp)
        (lib / ".process").mkdir(parents=True)
        (lib / ".process" / "资产索引.json").write_text(json.dumps({"assets": [
            {"asset_id": "CO_月白长裙", "type": "服装", "name": "月白长裙", "alias_display": ""},
            {"asset_id": "PR_琴", "type": "道具", "name": "琴", "alias_display": ""},
        ]}, ensure_ascii=False), encoding="utf-8")
        (lib / "提示词" / "基础").mkdir(parents=True)
        (lib / "提示词" / "衍生").mkdir(parents=True)
        for t in ("人物", "场景", "道具"):
            (lib / "提示词" / "基础" / f"{t}提示词.md").write_text(f"## 001. 已有{t}\n甲\n", encoding="utf-8")
        (lib / "提示词" / "调度清单.md").write_text(
            "# 衍生调度清单\n\n| 类型 | 资产 | 变体 | 备注 | 画风 | 母版编号 | 仅允许变化 |\n"
            "|---|---|---|---|---|---|---|\n" + schedule_row + "\n", encoding="utf-8")
        return lib

    def test_costume_row_is_not_demanded(self):
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(td, "| 道具 | 月白长裙 | 湿边 | 剧本驱动（第3章） | | | |")
            issues = deriv_prompt_gaps(lib, lib / "提示词" / "基础", lib / "提示词" / "衍生", False)
        self.assertEqual(issues, [], f"服装行不该被索要衍生：{issues}")

    def test_real_prop_row_is_still_demanded(self):
        """反例（判据不削弱）：真道具缺衍生提示词时照旧报缺。"""
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib(td, "| 道具 | 琴 | 破损 | 剧本驱动（第3章） | | | |")
            issues = deriv_prompt_gaps(lib, lib / "提示词" / "基础", lib / "提示词" / "衍生", False)
        self.assertTrue(any("琴" in i for i in issues), issues)


class TestSplitNames(unittest.TestCase):
    def test_basic_split(self):
        self.assertEqual(split_names("张三/李四、王五"), {"张三", "李四", "王五"})

    def test_placeholders_dropped(self):
        # F20：占位字是**一份**共享集合（三个读者此前各写一份，「（空）」这类写法一边认一边不认）
        for p in ("无", "无出场", "未知", "未出现", "-", "—", "（空）", "空", "(无)", "（无）", "None", "无别名"):
            self.assertEqual(split_names(f"张三、{p}"), {"张三"}, p)

    def test_placeholder_set_is_shared_across_readers(self):
        """反例（不许再分叉）：三个读同一张表的模块必须持有同一个集合对象。"""
        from engine import prompt_rules
        import aggregate
        import scan_registry
        self.assertIs(prompt_rules.PLACEHOLDER_NAMES, PLACEHOLDER_NAMES)
        self.assertIs(prompt_rules.PLACEHOLDER_NAMES, aggregate.PLACEHOLDER_NAMES)
        self.assertIs(prompt_rules.PLACEHOLDER_NAMES, scan_registry.PLACEHOLDER_ALIAS)

    def test_empty_returns_empty(self):
        self.assertEqual(split_names(None), set())
        self.assertEqual(split_names(""), set())


class TestParseAssetNames(unittest.TestCase):
    def test_extracts_main_and_alias_names(self):
        text = """## 人物资产提取表
| 资产ID | 人物姓名 | 别名/称呼 | 出现位置 | 重要等级 | 备注 |
| p1 | 林姑娘 | 林小姐、林姑娘 | 第1章 | 主角 | |

## 场景资产提取表
| 资产ID | 场景名称 | 出现位置 | 重要等级 | 备注 |
| s1 | 茶楼 | 第2章 | 重要 | |

## 待确认附录
| 名称 | 类型 | 说明 |
| 神秘人 | 人物 | 身份不明 |
"""
        names, pending, alias = parse_asset_names(text)
        self.assertIn("林姑娘", names["人物"])
        self.assertIn("茶楼", names["场景"])
        self.assertIn("林小姐", alias)
        self.assertIn("神秘人", pending)


class TestCheckPromptContent(unittest.TestCase):
    def test_missing_label_reported(self):
        checks = [("无文字", ("图中不要有任何文字",)), ("素灰背景", ("素灰",))]
        missing = check_prompt_content(Path("nonexistent"), checks)
        self.assertEqual(missing, ["文件不可读"])

    def test_existing_file_passes(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.md"
            p.write_text("图中不要有任何文字，素灰背景。", encoding="utf-8")
            checks = [("无文字", ("图中不要有任何文字",)), ("素灰背景", ("素灰",))]
            self.assertEqual(check_prompt_content(p, checks), [])

    def test_latin_words_reported_as_not_pure_chinese(self):
        checks = [("无文字", ("图中不要有任何文字",))]
        self.assertIn("纯中文", check_content("国漫3D，PBR材质。图中不要有任何文字", checks))

    def test_color_code_allowed_in_pure_chinese(self):
        checks = [("无文字", ("图中不要有任何文字",))]
        self.assertEqual(check_content("素灰背景 #B8B8B8。图中不要有任何文字", checks), [])

    def test_each_render_lock_layer_is_required_separately(self):
        checks = [
            ("画风锁定段-建模层", ("1. 建模层：高精度建模",)),
            ("画风锁定段-渲染层", ("2. 渲染层：写实三维渲染",)),
            ("画风锁定段-场景/背景", ("3. 场景/背景：浅灰影棚",)),
            ("画风锁定段-镜头", ("4. 镜头：三格图板",)),
        ]
        content = "1. 建模层：高精度建模。2. 渲染层：写实三维渲染。图中不要有任何文字"
        missing = check_content(content, checks)
        self.assertIn("画风锁定段-场景/背景", missing)
        self.assertIn("画风锁定段-镜头", missing)
        self.assertNotIn("画风锁定段-建模层", missing)
        self.assertNotIn("画风锁定段-渲染层", missing)

    def test_person_checks_require_left_and_right_panels(self):
        labels = [label for label, *_ in DERIV_CHECKS["人物"]]
        self.assertIn("左面特写格", labels)
        self.assertIn("右面双视图格", labels)
        content = "三格图板：正面特写，正视图，后视图，右面正视图与后视图。图中不要有任何文字。负面提示词：无文字"
        self.assertIn("左面特写格", check_content(content, DERIV_CHECKS["人物"]))

    def test_deriv_checks_keep_render_quality_labels(self):
        for type_key in ("人物", "场景", "道具"):
            labels = [label for label, *_ in DERIV_CHECKS[type_key]]
            self.assertIn("三维渲染", labels)
        person_labels = [label for label, *_ in DERIV_CHECKS["人物"]]
        self.assertIn("三格图板", person_labels)


class TestEmphasisAndStartState(unittest.TestCase):
    def test_emphasis_format_valid(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / "强调清单.md").write_text(
                "# 强调清单\n## 必须强化\n- 服装更华丽\n## 必须避免\n- 避免现代元素\n",
                encoding="utf-8",
            )
            self.assertEqual(check_emphasis_format(lib), [])

    def test_emphasis_format_reports_bad_line(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / "强调清单.md").write_text(
                "## 必须强化\n服装更华丽\n",
                encoding="utf-8",
            )
            self.assertEqual(len(check_emphasis_format(lib)), 1)

    def test_start_state_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / ".start-state.json").write_text(
                '{"input_source":"a","genre":"玄幻"}',
                encoding="utf-8",
            )
            issues = check_start_state(lib)
            self.assertTrue(any("缺字段" in x for x in issues))
            (lib / ".start-state.json").write_text(
                '{"input_source":"a","genre":"玄幻","style":"H7","emphasis_hash":"x","output_root":"r","last_confirmed_at":"t"}',
                encoding="utf-8",
            )
            self.assertEqual(check_start_state(lib), [])


class TestIsPureFantasy(unittest.TestCase):
    def test_pure_fantasy_variants(self):
        for genre in ("玄幻", "仙侠", "玄幻/仙侠", "仙侠奇幻", "玄幻仙侠"):
            self.assertTrue(is_pure_fantasy(genre), genre)

    def test_mixed_fantasy_is_not_pure(self):
        for genre in ("都市玄幻", "古代仙侠", "现代奇幻", "科幻修真"):
            self.assertFalse(is_pure_fantasy(genre), genre)

    def test_empty_is_not_pure(self):
        self.assertFalse(is_pure_fantasy(""))


class TestParseProjectGenre(unittest.TestCase):
    def test_extracts_genre_from_basic_info(self):
        text = """## 项目基础信息
| 项目名称 | 测试小说 |
| 题材类型 | 玄幻/仙侠 |
"""
        self.assertEqual(parse_project_genre(text), "玄幻/仙侠")

    def test_missing_section_returns_empty(self):
        self.assertEqual(parse_project_genre("# 无项目信息\n"), "")


class TestStyleLockChecks(unittest.TestCase):
    def test_person_requires_four_layers_and_content_language_for_fantasy(self):
        card = parse_style_card("H7")
        checks = build_style_lock_checks(card, "人物", "玄幻")
        labels = [label for label, _ in checks]
        self.assertIn("画风锁定段-建模层", labels)
        self.assertIn("画风锁定段-渲染层", labels)
        self.assertIn("画风锁定段-场景/背景", labels)
        self.assertIn("画风锁定段-镜头", labels)
        self.assertIn("画风锁定段-内容语言", labels)

    def test_person_skips_content_language_outside_fantasy(self):
        card = parse_style_card("H7")
        checks = build_style_lock_checks(card, "人物", "都市")
        labels = [label for label, _ in checks]
        self.assertNotIn("画风锁定段-内容语言", labels)
        # 4 分层 + 视图定性段 + 负面段含画风负面（H7 挂包，ADR-0077/0080）
        self.assertEqual(len(labels), 6)
        self.assertIn("视图定性段", labels)
        self.assertIn("负面段含画风负面", labels)

    def test_prop_requires_only_model_and_render_layers(self):
        card = parse_style_card("H7")
        checks = build_style_lock_checks(card, "道具", "玄幻")
        labels = [label for label, _ in checks]
        self.assertIn("画风锁定段-建模层", labels)
        self.assertIn("画风锁定段-渲染层", labels)
        self.assertNotIn("画风锁定段-场景/背景", labels)
        self.assertNotIn("画风锁定段-镜头", labels)
        self.assertNotIn("画风锁定段-内容语言", labels)


class TestParseMergedSections(unittest.TestCase):
    def test_parses_numbered_sections(self):
        text = "# 人物提示词\n\n## 001. 林姑娘\n\n正文一\n\n---\n\n## 002. 船夫\n\n正文二\n\n---\n"
        self.assertEqual(parse_merged_sections(text), {"林姑娘": "正文一", "船夫": "正文二"})


class TestSceneLayerDuplicates(unittest.TestCase):
    """层次句跨节重复拦截（ADR-0077）。

    实测背景：真实产出里 10 个场景节的「景别」全部是 `远景全景` —— 那是**镜头术语的取值**，
    跨节相同是**正确的**；而判据正则 `(前景|中景|后景|远景)([^。，；;]{2,40})[，。；;]`
    会连带匹配到「景别：远景全景；」这一行，于是把 10 节全部误报成"层次复读"。
    """

    def _write(self, dir_: Path, body: str) -> Path:
        dir_.mkdir(parents=True, exist_ok=True)
        p = dir_ / "场景提示词.md"
        p.write_text(body, encoding="utf-8")
        return p

    def test_lens_declaration_is_not_layer_repetition(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "基础"
            self._write(base,
                        "## 001. 青石长街\n"
                        "景别：远景全景；角度：平视；布光：清晨冷白主光。\n"
                        "前景为微湿的青石阶梯与石栓马桩，中景为深远延伸的青石板主街道。\n"
                        "\n## 002. 渡口\n"
                        "景别：远景全景；角度：俯角；布光：低角度侧逆晨光。\n"
                        "前景为布满水渍暗苔的花岗岩垂水阶梯，中景为老杉木搭造的浮桥栈道。\n")
            issues = check_scene_duplicates(base, Path(td) / "衍生")
            self.assertEqual(issues, [], f"景别取值相同不应上报，实际：{issues}")

    def test_lens_declaration_variants_are_not_repetition(self):
        # 真实产出里三种写法都出现过：`景别：X；` / `景别为X，` / `景别为X。`
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "基础"
            self._write(base,
                        "## 001. 青石长街\n景别：远景全景；角度：平视。\n前景为青石阶梯，中景为长街。\n"
                        "\n## 002. 临河茶楼\n景别为远景全景，角度为平视。\n前景为水埠头，中景为主体茶楼。\n"
                        "\n## 003. 天井\n景别为远景全景。布光：清晨天光。\n前景为石阶，中景为天井。\n")
            issues = check_scene_duplicates(base, Path(td) / "衍生")
            self.assertEqual(issues, [], f"三种景别写法都不应上报，实际：{issues}")

    def test_real_repetition_still_caught(self):
        """反例（不得削弱）：把同一条层次句抄到另一节，必须仍被抓到。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "基础"
            line = "前景为微湿的青石阶梯与石栓马桩，"
            self._write(base,
                        f"## 001. 青石长街\n景别：远景全景；角度：平视。\n{line}中景为长街。\n"
                        f"\n## 002. 渡口\n景别：远景全景；角度：俯角。\n{line}中景为渡口。\n")
            issues = check_scene_duplicates(base, Path(td) / "衍生")
            self.assertEqual(len(issues), 1, f"真复读必须仍被抓，实际：{issues}")
            self.assertIn("前景", issues[0])

    def test_genuine_far_layer_repetition_still_caught(self):
        """反例：**真正的**远景句复读仍必须被抓（剥声明不能把远景层一起剥掉）。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "基础"
            far = "远景为苍茫云海与连绵远山轮廓，"
            self._write(base,
                        f"## 001. 青石长街\n景别：远景全景。\n{far}中景为长街。\n"
                        f"\n## 002. 渡口\n景别：远景全景。\n{far}中景为渡口。\n")
            issues = check_scene_duplicates(base, Path(td) / "衍生")
            self.assertEqual(len(issues), 1, f"远景句复读必须仍被抓，实际：{issues}")
            self.assertIn("远景", issues[0])


if __name__ == "__main__":
    unittest.main()
