#!/usr/bin/env python3
"""Unit tests for aggregate verdict parsing (no LLM calls)."""
import io
import sys
import unittest

from aggregate import build_asset_index, parse_verdict, render_final, write_asset_index


def _person_groups():
    """两个人物分组的通用夹具（段级成员备注留空，状态标签走裁决备注）。"""
    groups = {}
    for gid, name, num in (("G01", "公子", "00001"), ("G02", "断臂公子", "00002")):
        member = {"段文件": "段01.md", "出现章节": "第1章", "摘句": name + "登场", "行号": num, "名称": name, "别名": ""}
        groups[gid] = {"组ID": gid, "类型": "人物", "候选主名": name, "出处校验": {"失败": 0}, "违规名": [], "成员": [member]}
    return groups


class TestParseVerdict(unittest.TestCase):
    def test_parse_basic_verdict(self):
        text = """## 项目基础信息
| 字段 | 内容 |
| 项目名称 | 测试小说 |
| 作者 | 测试作者 |

## 资产裁决
| 组ID | 类型 | 主名 | 等级 | 合并 | 别名 | 绑定 | 处理 | 备注 |
| g1 | 人物 | 林姑娘 | 主角 | 是 | 林小姐 | | 正式 | 女主 |
| g2 | 场景 | 茶楼 | 重要 | 否 | | | 正式 | 主要场景 |

## 待确认
| 名称 | 类型 | 说明 |
| 神秘人 | 人物 | 身份待确认 |
"""
        verdicts, info, pending, bad = parse_verdict(text)
        self.assertEqual(bad, 0)
        self.assertEqual(info.get("项目名称"), "测试小说")
        self.assertEqual(info.get("作者"), "测试作者")
        self.assertEqual(len(verdicts), 2)
        self.assertEqual(verdicts[0]["主名"], "林姑娘")
        self.assertEqual(verdicts[1]["类型"], "场景")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["名称"], "神秘人")

    def test_placeholder_formal_rows_are_silently_skipped(self):
        """Formal rows with placeholder names must not count as bad rows."""
        placeholders = ["", "（空）", "空", "无", "(无)", "（无）", "None", "—"]
        rows = "\n".join(
            f"| g{i} | 人物 | {name} | 配角 | 否 | | | 正式 | |"
            for i, name in enumerate(placeholders)
        )
        text = f"## 资产裁决\n| 组ID | 类型 | 主名 | 等级 | 合并 | 别名 | 绑定 | 处理 | 备注 |\n{rows}"
        verdicts, _info, pending, bad = parse_verdict(text)
        self.assertEqual(bad, 0)
        self.assertEqual(len(verdicts), 0)
        self.assertEqual(len(pending), 0)

    def test_placeholder_appendix_rows_become_pending(self):
        """Appendix rows with placeholder names should fall back to group ID in pending."""
        text = """## 资产裁决
| 组ID | 类型 | 主名 | 等级 | 合并 | 别名 | 绑定 | 处理 | 备注 |
| g1 | 人物 | (无) | 配角 | 否 | | | 附录 | 待确认 |
"""
        verdicts, _info, pending, bad = parse_verdict(text)
        self.assertEqual(bad, 0)
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["处理"], "附录")
        self.assertEqual(verdicts[0]["主名"], "(无)")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["名称"], "g1")
        self.assertEqual(pending[0]["说明"], "待确认")

    def test_malformed_rows_count_as_bad(self):
        text = """## 资产裁决
| 组ID | 类型 | 主名 | 等级 | 合并 | 别名 | 绑定 | 处理 | 备注 |
| g1 | 人物 | 林姑娘 | 主角 | 否 | | | 正式 |
| g2 | 场景 | 茶楼 | 重要 | 否 | | | 正式 | 备注够 |
| g3 | 未知 | 某物 | 配角 | 否 | | | 正式 | 类型出格 |
"""
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            verdicts, _info, _pending, bad = parse_verdict(text)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(bad, 2)
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["组ID"], "g2")
        self.assertIn("列数不足", captured.getvalue())
        self.assertIn("类型出格", captured.getvalue())

    def test_pending_deduplication(self):
        text = """## 待确认
| 名称 | 类型 | 说明 |
| 神秘人 | 人物 | 身份不明 |
| 神秘人 | 人物 | 重复项 |
| 无名场景 | 场景 | 位置不明 |
"""
        _verdicts, _info, pending, bad = parse_verdict(text)
        self.assertEqual(bad, 0)
        self.assertEqual(len(pending), 2)
        names = [p["名称"] for p in pending]
        self.assertIn("神秘人", names)
        self.assertIn("无名场景", names)


class TestBuildAssetIndex(unittest.TestCase):
    """ADR-0084 资产索引：旧表不变 + 索引结构化（TDD 工单 4）。"""

    def _groups(self):
        return {
            "G01": {
                "组ID": "G01",
                "成员": [
                    {"段文件": "段01.md", "出现章节": "第1章", "摘句": "林姑娘走进茶楼", "行号": "00002", "别名": ""},
                ],
            },
            "G02": {
                "组ID": "G02",
                "成员": [
                    {"段文件": "段01.md", "出现章节": "第1章", "摘句": "林姑娘走进茶楼", "行号": "00003", "别名": ""},
                ],
            },
        }

    def _summaries(self):
        return [{"章节": "第1章", "概要": "进茶楼", "人物": "林姑娘", "场景": "茶楼", "道具": "", "服装": ""}]

    def test_index_stable_and_repeatable(self):
        verdicts = [
            {"组ID": "G01", "类型": "人物", "主名": "林姑娘", "等级": "S", "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": ""},
            {"组ID": "G02", "类型": "场景", "主名": "茶楼", "等级": "A", "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": "[子类:室内空间]"},
        ]
        idx1 = build_asset_index(verdicts, self._groups(), self._summaries(), 1, source_fingerprint="sha256:test")
        idx2 = build_asset_index(verdicts, self._groups(), self._summaries(), 1, source_fingerprint="sha256:test")
        self.assertEqual(idx1["schema_version"], 1)
        self.assertEqual(len(idx1["assets"]), 2)
        by_id = {a["asset_id"]: a for a in idx1["assets"]}
        self.assertEqual(by_id["CH_林姑娘"]["fact_level"], "剧本事实")
        self.assertEqual(by_id["CH_林姑娘"]["priority"], "P0")
        self.assertEqual(by_id["SC_茶楼"]["subtype"], "室内空间")
        kinds = [r["kind"] for r in by_id["CH_林姑娘"]["relations"]]
        self.assertIn("APPEARS_IN", kinds)
        # 同一输入两次输出稳定（stable_key 与关系一致）
        self.assertEqual(
            [(a["asset_id"], a["stable_key"]) for a in idx1["assets"]],
            [(a["asset_id"], a["stable_key"]) for a in idx2["assets"]],
        )

    def test_legacy_tables_render_from_ledger_in_verdict_order(self):
        """工单 2 L139(b)：旧四表由总账渲染，行序＝裁决文件顺序，不再各自维护名单。

        旧渲染按 verdicts 原序遍历；若改用 asset_id 字典序，甲/乙 会互换（乙 U+4E59 < 甲 U+7532）。
        """

        def _group(gid, name, line_no):
            member = {"段文件": "段01.md", "出现章节": "第1章", "摘句": name + "登场",
                      "行号": line_no, "名称": name, "别名": ""}
            return {"组ID": gid, "类型": "人物", "候选主名": name, "成员": [member],
                    "出处校验": {"失败": 0}, "违规名": []}

        verdicts = [
            {"组ID": "G01", "类型": "人物", "主名": "甲", "等级": "S", "合并": "", "别名": "",
             "绑定": "", "处理": "保留", "备注": ""},
            {"组ID": "G02", "类型": "人物", "主名": "乙", "等级": "B", "合并": "", "别名": "",
             "绑定": "", "处理": "保留", "备注": ""},
        ]
        groups = {"G01": _group("G01", "甲", "00001"), "G02": _group("G02", "乙", "00002")}
        text = render_final("源", "作者", verdicts, {}, [], groups, self._summaries(), 1)
        table = text.split("## 人物资产提取表")[1].split("\n## ", 1)[0]
        rows = [ln for ln in table.splitlines() if ln.startswith("| CH_")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].startswith("| CH_甲 |"), rows)
        self.assertTrue(rows[1].startswith("| CH_乙 |"), rows)

    def test_state_tag_lands_on_parent_variant_needs_without_self_loop(self):
        """工单 3 L171/L172：[状态:…] 经 entity_resolution 裁决为 STATE_OF，写母资产变体列表。"""
        verdicts = [
            {"组ID": "G01", "类型": "人物", "主名": "公子", "等级": "S", "合并": "", "别名": "",
             "绑定": "", "处理": "保留", "备注": ""},
            {"组ID": "G02", "类型": "人物", "主名": "断臂公子", "等级": "B", "合并": "", "别名": "",
             "绑定": "", "处理": "保留", "备注": "[状态:公子]"},
        ]
        idx = build_asset_index(verdicts, _person_groups(), self._summaries(), 1,
                                source_fingerprint="sha256:test")
        by_id = {a["asset_id"]: a for a in idx["assets"]}
        needs = by_id["CH_公子"]["variant_needs"]
        self.assertTrue(any("状态：公子" == n["需求"] for n in needs), needs)
        # 绝不造 STATE_OF 自环（RELATION_MATRIX 禁止）
        for asset in idx["assets"]:
            self.assertNotIn("STATE_OF", [r["kind"] for r in asset["relations"]])

    def test_ambiguous_state_parent_goes_to_unresolved(self):
        """状态母资产命中非唯一 → 进 unresolved 待人工裁决，不得静默任选。"""
        verdicts = [
            {"组ID": "G01", "类型": "人物", "主名": "公子甲", "等级": "S", "合并": "", "别名": "公子",
             "绑定": "", "处理": "保留", "备注": ""},
            {"组ID": "G02", "类型": "人物", "主名": "公子乙", "等级": "S", "合并": "", "别名": "公子",
             "绑定": "", "处理": "保留", "备注": ""},
            {"组ID": "G03", "类型": "人物", "主名": "断臂公子", "等级": "B", "合并": "", "别名": "",
             "绑定": "", "处理": "保留", "备注": "[状态:公子]"},
        ]
        groups = {}
        for gid, name, num in (("G01", "公子甲", "00001"), ("G02", "公子乙", "00002"), ("G03", "断臂公子", "00003")):
            member = {"段文件": "段01.md", "出现章节": "第1章", "摘句": name + "登场", "行号": num, "名称": name, "别名": ""}
            groups[gid] = {"组ID": gid, "类型": "人物", "候选主名": name, "出处校验": {"失败": 0}, "违规名": [], "成员": [member]}
        idx = build_asset_index(verdicts, groups, self._summaries(), 1, source_fingerprint="sha256:test")
        self.assertTrue(any(u["kind"] == "STATE_OF" for u in idx["unresolved_relations"]),
                        idx["unresolved_relations"])

    def test_dangling_relations_never_enter_index(self):
        verdicts = [
            {"组ID": "G01", "类型": "服装", "主名": "淡蓝长裙", "等级": "B", "合并": "", "别名": "", "绑定": "不存在的人", "处理": "保留", "备注": ""},
        ]
        groups = {"G01": {"组ID": "G01", "成员": [{"段文件": "段01.md", "出现章节": "第1章", "摘句": "淡蓝长裙曳地", "行号": "00005", "别名": ""}]}}
        idx = build_asset_index(verdicts, groups, [], 1, source_fingerprint="sha256:test")
        self.assertEqual(idx["assets"][0]["relations"], [])
        self.assertEqual(idx["assets"][0]["dependencies"], [])
        # 工单D：悬空关系必须进 unresolved，不无声消失
        self.assertTrue(idx.get("unresolved_relations"))
        self.assertTrue(any(u["raw"] == "不存在的人" for u in idx["unresolved_relations"]))

    def test_pending_conflict_blocked_from_formal_facts(self):
        verdicts = [
            {"组ID": "G01", "类型": "人物", "主名": "神秘人", "等级": "B", "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": ""},
        ]
        groups = {"G01": {"组ID": "G01", "成员": [{"段文件": "段01.md", "出现章节": "第1章", "摘句": "神秘人一闪而过", "行号": "00009", "别名": ""}]}}
        pending = [{"名称": "神秘人", "类型": "人物", "说明": "身份存疑"}]
        idx = build_asset_index(verdicts, groups, [], 1, source_fingerprint="sha256:test", pending=pending)
        self.assertEqual(idx["assets"][0]["fact_level"], "待定冲突")

    def test_write_asset_index_roundtrip(self):
        import json
        import tempfile
        from pathlib import Path
        verdicts = [
            {"组ID": "G01", "类型": "人物", "主名": "林姑娘", "等级": "S", "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": ""},
        ]
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            p = write_asset_index(lib, verdicts, self._groups(), self._summaries(), 1, source_fingerprint="sha256:test")
            self.assertTrue(p.is_file())
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertIn("stable_key_mapping", data)
            # 追更复用：第二次写入保持同一 stable_key
            p2 = write_asset_index(lib, verdicts, self._groups(), self._summaries(), 1, source_fingerprint="sha256:test")
            data2 = json.loads(p2.read_text(encoding="utf-8"))
            self.assertEqual(data["assets"][0]["stable_key"], data2["assets"][0]["stable_key"])

    def test_mapping_covers_every_stable_key(self):
        # 工单D：每个 assets[*].stable_key 必须在 mapping 中且指向同一实体
        import json
        import tempfile
        from pathlib import Path
        verdicts = [
            {"组ID": "G01", "类型": "人物", "主名": "林姑娘", "等级": "S", "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": ""},
            {"组ID": "G02", "类型": "场景", "主名": "茶楼", "等级": "A", "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": ""},
        ]
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            p = write_asset_index(lib, verdicts, self._groups(), self._summaries(), 1, source_fingerprint="sha256:test")
            data = json.loads(p.read_text(encoding="utf-8"))
            keys = data["stable_key_mapping"]["keys"]
            for a in data["assets"]:
                self.assertIn(a["stable_key"], keys)
                self.assertEqual(keys[a["stable_key"]]["asset_id"], a["asset_id"])

    def test_insert_ahead_keeps_stable_key(self):
        # 工单D：前面插入新段/行号变化，原实体沿用 stable_key（R4a 后直调构建器）
        def mk(seg, ch, q, ln):
            return {"段文件": seg, "出现章节": ch, "摘句": q, "行号": ln, "别名": "", "备注": ""}
        verdicts = [{"组ID": "G01", "类型": "人物", "主名": "张三", "等级": "B", "合并": "",
                     "别名": "", "绑定": "", "处理": "保留", "备注": ""}]
        g1 = {"G01": {"组ID": "G01", "成员": [mk("段02.md", "第2章", "张三进门", "00010")]}}
        idx1 = build_asset_index(verdicts, g1, [], 1, source_fingerprint="x")
        k1 = idx1["assets"][0]["stable_key"]
        g2 = {"G01": {"组ID": "G01", "成员": [mk("段03.md", "第3章", "张三进门", "00050")]}}
        idx2 = build_asset_index(verdicts, g2, [], 1, source_fingerprint="x",
                                 existing_key_store=idx1["stable_key_mapping"])
        self.assertEqual(idx2["assets"][0]["stable_key"], k1)

    def test_needs_explicit_no_leak(self):
        # 工单D：needs 为显式参数，相邻两次构建不串值

        def mk(seg, ch, q, ln):
            return {"段文件": seg, "出现章节": ch, "摘句": q, "行号": ln, "别名": "", "备注": ""}
        verdicts = [{"组ID": "G01", "类型": "场景", "主名": "茶楼", "等级": "B", "合并": "",
                     "别名": "", "绑定": "", "处理": "保留", "备注": ""}]
        g = {"G01": {"组ID": "G01", "成员": [mk("段01.md", "第1章", "走进茶楼", "00001")]}}
        idx_need = build_asset_index(verdicts, g, [], 1, source_fingerprint="x",
                                     needs=[{"资产": "茶楼", "需求": "黄昏", "章节": "第1章"}])
        idx_plain = build_asset_index(verdicts, g, [], 1, source_fingerprint="x")
        self.assertEqual(idx_need["assets"][0]["story_function"], "剧本驱动变体载体")
        self.assertEqual(idx_plain["assets"][0]["story_function"], "普通陈设/背景")

    def test_variant_needs_no_self_loop(self):
        # R4b：需求记录保留为 variant_needs（衍生需求），禁止制造母资产 STATE_OF 自环
        def mk(seg, ch, q, ln):
            return {"段文件": seg, "出现章节": ch, "摘句": q, "行号": ln, "别名": "", "备注": ""}
        verdicts = [{"组ID": "G01", "类型": "场景", "主名": "茶楼", "等级": "B", "合并": "",
                     "别名": "", "绑定": "", "处理": "保留", "备注": ""}]
        g = {"G01": {"组ID": "G01", "成员": [mk("段01.md", "第1章", "走进茶楼", "00001")]}}
        idx = build_asset_index(verdicts, g, [], 1, source_fingerprint="x",
                                needs=[{"资产": "茶楼", "需求": "黄昏", "章节": "第1章"}])
        a = idx["assets"][0]
        self.assertEqual([r["kind"] for r in a["relations"] if r["kind"] == "STATE_OF"], [])
        self.assertEqual(a["variant_needs"], [{"需求": "黄昏", "章节": "第1章"}])
        # story_function/P 级推导不受影响
        self.assertEqual(a["story_function"], "剧本驱动变体载体")

    def test_build_returns_mapping_directly(self):
        # R4a：构建器显式返回 mapping，废除函数对象 last_allocator 侧信道
        def mk(seg, ch, q, ln):
            return {"段文件": seg, "出现章节": ch, "摘句": q, "行号": ln, "别名": "", "备注": ""}
        verdicts = [{"组ID": "G01", "类型": "人物", "主名": "张三", "等级": "B", "合并": "",
                     "别名": "", "绑定": "", "处理": "保留", "备注": ""}]
        g = {"G01": {"组ID": "G01", "成员": [mk("段01.md", "第1章", "张三登场", "00002")]}}
        idx = build_asset_index(verdicts, g, [], 1, source_fingerprint="x")
        self.assertIn("stable_key_mapping", idx)
        ids = {d["asset_id"] for d in idx["stable_key_mapping"]["keys"].values()}
        self.assertEqual(ids, {"CH_张三"})
        self.assertNotIn("last_allocator", build_asset_index.__dict__)

    def test_interleaved_build_no_cross_mapping(self):
        # R4a：并发构建两个资产库，mapping 只指向本库 ID（侧信道废除后的回归守卫）
        import threading

        def mk_person(name, line):
            verdicts = [{"组ID": "G01", "类型": "人物", "主名": name, "等级": "B", "合并": "",
                         "别名": "", "绑定": "", "处理": "保留", "备注": ""}]
            groups = {"G01": {"组ID": "G01", "成员": [
                {"段文件": "段01.md", "出现章节": "第1章", "摘句": f"{name}登场", "行号": line, "别名": ""}]}}
            return verdicts, groups

        va, ga = mk_person("张三", "00002")
        vb, gb = mk_person("李四", "00003")
        results = {}
        barrier = threading.Barrier(2)

        def worker(tag, v, g):
            barrier.wait(5)
            results[tag] = build_asset_index(v, g, [], 1, source_fingerprint=tag)

        ta = threading.Thread(target=worker, args=("a", va, ga), daemon=True)
        tb = threading.Thread(target=worker, args=("b", vb, gb), daemon=True)
        ta.start()
        tb.start()
        ta.join(10)
        tb.join(10)
        self.assertEqual({d["asset_id"] for d in results["a"]["stable_key_mapping"]["keys"].values()},
                         {"CH_张三"})
        self.assertEqual({d["asset_id"] for d in results["b"]["stable_key_mapping"]["keys"].values()},
                         {"CH_李四"})


class TestDerivSummaryTypeAuthority(unittest.TestCase):
    """F29：衍生需求汇总的「类型」列必须以总账为准，不得照抄模型写的类型。

    2026-09-21 全新库实测：模型把两件**服装**写成「道具」，这张表是交付物的一部分——
    错类型一路流到调度清单（`| 道具 | 月白长裙 | 湿边 |`），衍生步骤按「道具」去找父资产的
    基础提示词，找不到就抛错停链（`道具「月白长裙」基础提示词缺失`）。
    """

    def _render_deriv_row(self, declared: str, asset: str, real_type: str, group_type: str) -> str:
        member = {"段文件": "段01.md", "出现章节": "第3章", "摘句": "登场",
                  "行号": "00001", "名称": asset, "别名": ""}
        groups = {"G01": {"组ID": "G01", "类型": group_type, "候选主名": asset,
                          "名称频次": {asset: 1}, "成员": [member],
                          "出处校验": {"失败": 0}, "违规名": []}}
        verdicts = [{"组ID": "G01", "类型": group_type, "主名": asset, "等级": "A", "合并": "",
                     "别名": "", "绑定": "", "处理": "保留", "备注": ""}]
        summaries = [{"章节": "第3章", "概要": "x", "人物": "", "场景": "",
                      "道具": asset if real_type == "道具" else "",
                      "服装": asset if real_type == "服装" else ""}]
        needs = [{"类型": declared, "资产": asset, "需求": "湿边", "章节": "第3章"}]
        return render_final("源", "作者", verdicts, {}, [], groups, summaries, 3, needs=needs)

    def test_declared_prop_for_a_costume_is_corrected(self):
        text = self._render_deriv_row("道具", "月白长裙", real_type="服装", group_type="服装")
        row = [ln for ln in text.splitlines() if "湿边" in ln]
        self.assertTrue(row, "衍生需求汇总缺行")
        self.assertTrue(row[0].startswith("| 服装 |"), row)

    def test_matching_declared_type_is_left_alone(self):
        """反例（不得过度改写）：声明类型与总账一致时一个字都不动。"""
        text = self._render_deriv_row("道具", "灯笼", real_type="道具", group_type="道具")
        row = [ln for ln in text.splitlines() if "湿边" in ln]
        self.assertTrue(row, "衍生需求汇总缺行")
        self.assertTrue(row[0].startswith("| 道具 | 灯笼 |"), row)


class TestRelationResolution(unittest.TestCase):
    """R1：摘要场景名解析契约——正例可解析，歧义/未登记不得静默映射。"""

    def _mk_verdicts(self):
        return [
            {"组ID": "G01", "类型": "人物", "主名": "林姑娘", "等级": "S", "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": ""},
            {"组ID": "G02", "类型": "场景", "主名": "茶楼", "等级": "A", "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": ""},
            {"组ID": "G03", "类型": "道具", "主名": "青瓷茶壶", "等级": "B", "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": ""},
        ]

    def _groups(self):
        return {
            "G01": {"组ID": "G01", "成员": [{"段文件": "段01.md", "出现章节": "第1章", "摘句": "林姑娘走进茶楼", "行号": "00002", "别名": ""}]},
            "G02": {"组ID": "G02", "成员": [{"段文件": "段01.md", "出现章节": "第1章", "摘句": "茶楼临街而立", "行号": "00003", "别名": ""}]},
            "G03": {"组ID": "G03", "成员": [{"段文件": "段01.md", "出现章节": "第1章", "摘句": "青瓷茶壶冒着热气", "行号": "00004", "别名": ""}]},
        }

    def test_summary_scene_main_name_resolves(self):
        summaries = [{"章节": "第1章", "概要": "进茶楼", "人物": "林姑娘", "场景": "茶楼", "道具": "青瓷茶壶", "服装": ""}]
        idx = build_asset_index(self._mk_verdicts(), self._groups(), summaries, 1, source_fingerprint="x")
        self.assertEqual(idx["unresolved_relations"], [])
        by_id = {a["asset_id"]: a for a in idx["assets"]}
        targets = [r["target"] for r in by_id["CH_林姑娘"]["relations"] if r["kind"] == "APPEARS_IN"]
        self.assertIn("SC_茶楼", targets)
        located = [r["target"] for r in by_id["PR_青瓷茶壶"]["relations"] if r["kind"] == "LOCATED_IN"]
        self.assertIn("SC_茶楼", located)

    def test_backfilled_scene_name_resolves(self):
        # R1c 核心：登记表回填名带（脚本补入）后缀，必须仍解析到正式主名
        summaries = [{"章节": "第1章", "概要": "进茶楼", "人物": "林姑娘",
                      "场景": "茶楼（脚本补入）", "道具": "青瓷茶壶（脚本补入）", "服装": ""}]
        idx = build_asset_index(self._mk_verdicts(), self._groups(), summaries, 1, source_fingerprint="x")
        self.assertEqual(idx["unresolved_relations"], [])
        by_id = {a["asset_id"]: a for a in idx["assets"]}
        targets = [r["target"] for r in by_id["CH_林姑娘"]["relations"] if r["kind"] == "APPEARS_IN"]
        self.assertIn("SC_茶楼", targets)
        located = [r["target"] for r in by_id["PR_青瓷茶壶"]["relations"] if r["kind"] == "LOCATED_IN"]
        self.assertIn("SC_茶楼", located)

    def test_narrative_scene_stays_unresolved(self):
        # R1b 负例：叙述句不得静默绑定到任何场景
        summaries = [{"章节": "第1章", "概要": "进茶楼", "人物": "林姑娘",
                      "场景": "林姑娘走进茶楼大堂深处", "道具": "", "服装": ""}]
        idx = build_asset_index(self._mk_verdicts(), self._groups(), summaries, 1, source_fingerprint="x")
        hits = [u for u in idx["unresolved_relations"]
                if u["kind"] == "APPEARS_IN" and u["raw"] == "林姑娘走进茶楼大堂深处"]
        self.assertTrue(hits)
        by_id = {a["asset_id"]: a for a in idx["assets"]}
        targets = [r["target"] for r in by_id["CH_林姑娘"]["relations"] if r["kind"] == "APPEARS_IN"]
        self.assertNotIn("SC_茶楼", targets)

    def test_substring_scene_no_fuzzy_bind(self):
        # R1b 负例：字符串包含（茶楼大堂 ⊃ 茶楼）禁止自动绑定
        summaries = [{"章节": "第1章", "概要": "进茶楼", "人物": "林姑娘",
                      "场景": "茶楼大堂", "道具": "", "服装": ""}]
        idx = build_asset_index(self._mk_verdicts(), self._groups(), summaries, 1, source_fingerprint="x")
        hits = [u for u in idx["unresolved_relations"]
                if u["kind"] == "APPEARS_IN" and u["raw"] == "茶楼大堂"]
        self.assertTrue(hits)
        by_id = {a["asset_id"]: a for a in idx["assets"]}
        targets = [r["target"] for r in by_id["CH_林姑娘"]["relations"] if r["kind"] == "APPEARS_IN"]
        self.assertEqual(targets, [])

    def test_ambiguous_alias_not_silently_mapped(self):
        # R1b 负例：别名同命中两个场景 → 必须待审，不得映射到任一
        verdicts = [
            {"组ID": "G01", "类型": "人物", "主名": "林姑娘", "等级": "S", "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": ""},
            {"组ID": "G02", "类型": "场景", "主名": "东街茶楼", "等级": "A", "合并": "", "别名": "茶楼", "绑定": "", "处理": "保留", "备注": ""},
            {"组ID": "G03", "类型": "场景", "主名": "西街茶楼", "等级": "B", "合并": "", "别名": "茶楼", "绑定": "", "处理": "保留", "备注": ""},
        ]
        groups = {
            "G01": {"组ID": "G01", "成员": [{"段文件": "段01.md", "出现章节": "第1章", "摘句": "林姑娘进城", "行号": "00002", "别名": ""}]},
            "G02": {"组ID": "G02", "成员": [{"段文件": "段01.md", "出现章节": "第1章", "摘句": "东街茶楼临河", "行号": "00003", "别名": ""}]},
            "G03": {"组ID": "G03", "成员": [{"段文件": "段02.md", "出现章节": "第2章", "摘句": "西街茶楼靠市", "行号": "00006", "别名": ""}]},
        }
        summaries = [{"章节": "第1章", "概要": "进城", "人物": "林姑娘", "场景": "茶楼", "道具": "", "服装": ""}]
        idx = build_asset_index(verdicts, groups, summaries, 2, source_fingerprint="x")
        amb = [u for u in idx["unresolved_relations"] if u["kind"] == "APPEARS_IN" and "歧义" in u["reason"]]
        self.assertTrue(amb, f"歧义场景应待审: {idx['unresolved_relations']}")
        by_id = {a["asset_id"]: a for a in idx["assets"]}
        targets = [r["target"] for r in by_id["CH_林姑娘"]["relations"] if r["kind"] == "APPEARS_IN"]
        self.assertEqual(targets, [])
        # 两个独立实体不得因共享别名而合并 stable_key
        keys = {by_id["SC_东街茶楼"]["stable_key"], by_id["SC_西街茶楼"]["stable_key"]}
        self.assertEqual(len(keys), 2)


def _scene_edges_fixture(edges):
    """按 {场景主名: [CONNECTS_TO 目标名, …]} 造裁决与分组夹具。"""
    verdicts, groups = [], {}
    for i, (name, targets) in enumerate(edges.items(), start=1):
        gid = f"G{i:02d}"
        member = {"段文件": "段01.md", "出现章节": "第1章", "摘句": name + "出现于此",
                  "行号": f"{i:05d}", "名称": name, "别名": ""}
        groups[gid] = {"组ID": gid, "类型": "场景", "候选主名": name, "出处校验": {"失败": 0},
                       "违规名": [], "成员": [member]}
        verdicts.append({
            "组ID": gid, "类型": "场景", "主名": name, "等级": "B", "合并": "", "别名": "",
            "绑定": "", "处理": "保留",
            "备注": " ".join(f"[CONNECTS_TO:{t}]" for t in targets),
        })
    return verdicts, groups


def _connects_targets(asset):
    return {r["target"] for r in asset["relations"] if r["kind"] == "CONNECTS_TO"}


class TestConnectsToSymmetry(unittest.TestCase):
    """F5：RELATION_MATRIX 定义 CONNECTS_TO 为「场景双向连通」，生产侧必须落双向边。"""

    def _index(self, edges):
        verdicts, groups = _scene_edges_fixture(edges)
        return build_asset_index(verdicts, groups, [], 1, source_fingerprint="sha256:test")

    def test_declared_once_is_materialized_both_ways(self):
        idx = self._index({"密道": ["老槐客栈"], "老槐客栈": []})
        by_id = {a["asset_id"]: a for a in idx["assets"]}
        self.assertEqual(_connects_targets(by_id["SC_密道"]), {"SC_老槐客栈"})
        self.assertEqual(_connects_targets(by_id["SC_老槐客栈"]), {"SC_密道"})
        back = [r for r in by_id["SC_老槐客栈"]["relations"] if r["kind"] == "CONNECTS_TO"]
        self.assertEqual(len(back), 1)
        self.assertIn("反向", back[0]["note"])
        # 反向边同样计入依赖（保持有序），供提示词节头与依赖图使用
        self.assertIn("SC_密道", by_id["SC_老槐客栈"]["dependencies"])

    def test_already_two_way_is_left_untouched(self):
        """幂等：两端都声明时不得重复补边、不得改 note。"""
        idx = self._index({"密道": ["老槐客栈"], "老槐客栈": ["密道"]})
        by_id = {a["asset_id"]: a for a in idx["assets"]}
        for aid in ("SC_密道", "SC_老槐客栈"):
            rels = [r for r in by_id[aid]["relations"] if r["kind"] == "CONNECTS_TO"]
            self.assertEqual(len(rels), 1, rels)
            self.assertNotIn("反向", rels[0]["note"])

    def test_chain_is_closed_without_order_dependence(self):
        """A→B、B→C 的链：两端各自补齐，结果与遍历顺序无关。"""
        idx = self._index({"密道": ["老槐客栈"], "老槐客栈": ["青石长街"], "青石长街": []})
        by_id = {a["asset_id"]: a for a in idx["assets"]}
        self.assertEqual(_connects_targets(by_id["SC_密道"]), {"SC_老槐客栈"})
        self.assertEqual(_connects_targets(by_id["SC_老槐客栈"]), {"SC_密道", "SC_青石长街"})
        self.assertEqual(_connects_targets(by_id["SC_青石长街"]), {"SC_老槐客栈"})

    def test_target_outside_index_is_not_invented(self):
        """目标不在索引中 → 不补边，照旧进 unresolved（生产侧不掩盖真问题）。"""
        idx = self._index({"密道": ["屏风后"]})
        by_id = {a["asset_id"]: a for a in idx["assets"]}
        self.assertEqual(_connects_targets(by_id["SC_密道"]), set())
        self.assertTrue(any(u["kind"] == "CONNECTS_TO" and u["raw"] == "屏风后"
                            for u in idx["unresolved_relations"]), idx["unresolved_relations"])

    def test_one_way_index_still_fails_the_gate(self):
        """ **反例（判据不得削弱）**：人工删掉反向边的索引，continuity 仍必须报「单向」。"""
        import copy

        from engine.continuity import check_connects_consistency

        idx = self._index({"密道": ["老槐客栈"], "老槐客栈": []})
        one_way = copy.deepcopy(idx)
        for a in one_way["assets"]:
            if a["asset_id"] == "SC_老槐客栈":
                a["relations"] = []
                a["dependencies"] = []
        errors = check_connects_consistency(one_way)
        self.assertTrue(any("单向" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
