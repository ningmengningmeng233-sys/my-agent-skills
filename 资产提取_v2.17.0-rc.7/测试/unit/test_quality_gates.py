# -*- coding: utf-8 -*-
"""防偷懒绕过与 AI 味拦截的单元测试（ADR-0080）。"""
import unittest
from pathlib import Path

from engine.style import parse_style_card
from check_schedule import (
    AI_FLAVOR_STARTERS,
    build_style_lock_checks,
    check_ai_flavor,
)
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口


class TestAiFlavor(unittest.TestCase):
    def test_starter_detected(self):
        issues = check_ai_flavor("说白了，这个角色很强。")
        self.assertIn("命中·AI味措辞", issues)

    def test_fan_an_tone_detected(self):
        issues = check_ai_flavor("真正的重心不是铠甲，而是气势。")
        self.assertIn("命中·AI味措辞", issues)

    def test_dunhao_density_detected(self):
        line = "、".join(f"要素{i}" for i in range(12))
        issues = check_ai_flavor(line)
        self.assertIn("命中·顿号罗列过密", issues)

    def test_negative_line_exempt_from_dunhao(self):
        line = "负面提示词：" + "、".join(f"无要素{i}" for i in range(30))
        self.assertEqual(check_ai_flavor(line), [])

    def test_clean_content_passes(self):
        content = "渲染锁定：\n1. 建模层：次世代高精度建模。左面人物正面特写：正面，中性表情。"
        self.assertEqual(check_ai_flavor(content), [])


class TestBraceSemantics(unittest.TestCase):
    """冒烟钉死语义：str.format 的值参数不会再被扫描——描述/资产表含花括号安全。"""

    def test_values_with_braces_are_literal(self):
        out = "题材: {genre}\n{style_block}".format(genre="古风", style_block="定性{坏}括号")
        self.assertIn("题材: 古风", out)
        self.assertIn("定性{坏}括号", out)
        self.assertNotIn("{{", out)  # 值不被转义也不被翻倍

    def test_generate_flow_survives_braces_in_description(self):
        import sys
        import tempfile
        from unittest import mock
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
        import generate_prompts as gp

        def fake_llm(system, user, *a, **k):
            return ("角色视觉描述。{奇怪的}花括号：面部骨相鹅蛋脸，肤色白皙。"
                    "专属负面限制：避免写实噪点。"), {}

        rows = [{"type": "人物", "name": "花括号角色", "level": "主角", "note": ""}]
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / "描述").mkdir(parents=True)
            with mock.patch.object(gp, "call_llm", side_effect=fake_llm), \
                    mock.patch.object(gp, "read_emphasis", return_value="无"):
                rc = gp.generate_enrich(rows, "古风", lib, False, style_block="定性")
            self.assertEqual(rc, 0)
            written = (lib / "描述" / "人物描述.md").read_text(encoding="utf-8")
        self.assertIn("{奇怪的}花括号", written)  # 花括号字面落盘，不炸模板不翻倍


class TestNegativeItemsCheck(unittest.TestCase):
    def test_block_first_items_added_to_checks(self):
        card = parse_style_card("A2")
        checks = build_style_lock_checks(card, "人物", "古风")
        # A2 无包：组装块前 3 项 = 无现代元素、无科技感（any 命中即过）
        neg = [c for c in checks if c[0] == "负面段含画风负面"]
        self.assertEqual(len(neg), 1)
        self.assertTrue(any("无现代元素" in w or "无科技感" in w for w in neg[0][1]))

    def test_pack_negatives_flow_into_block(self):
        card = parse_style_card("H7")
        checks = build_style_lock_checks(card, "人物", "玄幻")
        neg = [c for c in checks if c[0] == "负面段含画风负面"]
        self.assertEqual(len(neg), 1)
        required = neg[0][1]
        self.assertTrue(required)
        # 契约：必查的负面项必须取自画风负面并集（措辞随画风卡走，不写死具体词）
        from engine.style import build_negative_block, load_stylepack
        union = build_negative_block(card, load_stylepack(str(card.get("pack", "") or "")), "人物")
        for word in required:
            self.assertIn(word, union, f"必查负面项 {word} 不在画风负面并集中")


class TestStyleAdoption(unittest.TestCase):
    def test_resolve_style_from_state(self):
        import tempfile
        from maintenance import resolve_style_from_state
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            self.assertIsNone(resolve_style_from_state(lib))
            (lib / ".start-state.json").write_text(
                '{"style": "仙侠奇幻风", "genre": "古风"}', encoding="utf-8"
            )
            self.assertEqual(resolve_style_from_state(lib), "仙侠奇幻风")

    def test_atmosphere_card_negatives_not_lost(self):
        """冒烟回归：A3-A6/C2/C4 的氛围类负面曾被类型过滤器整条丢光（ADR-0081）。"""
        from engine.style import build_negative_block
        for sid in ("A3", "A4", "A5", "A6", "C2", "C4"):
            card = parse_style_card(sid)
            for t in ("人物", "场景", "道具"):
                block = build_negative_block(card, None, t)
                self.assertTrue(block, f"{sid}/{t} 负面块为空")

    def test_abstract_words_detected(self):
        from check_schedule import check_ai_flavor
        issues = check_ai_flavor("绝美的宫殿在夜色中震撼登场。")
        self.assertTrue(any(i.startswith("命中·抽象空洞词") for i in issues))
        self.assertTrue(any("震撼" in i or "绝美" in i for i in issues))

    def test_ellipsis_and_transitions(self):
        from check_schedule import check_ai_flavor
        self.assertIn("命中·过度省略号", check_ai_flavor("他……回头看……庭院已空。庭院已空无人声。"))
        self.assertIn("命中·转折堆砌", check_ai_flavor("然而灯灭了。但是月升了。不过云散了。可风还在。"))

    def test_negative_line_exempt_from_abstract(self):
        from check_schedule import check_ai_flavor
        # 负面提示词行里的「无绝美」等是禁令，不算命中
        line = "负面提示词：无绝美滤镜、无震撼运镜、无梦幻光效。" + "其余正常描述，无误用词。"
        self.assertNotIn("命中·AI味措辞", check_ai_flavor(line))
        self.assertEqual([i for i in check_ai_flavor(line) if i.startswith("命中·抽象空洞词")], [])

    def test_judge_status_degrades_without_style(self):
        from maintenance import judge_status
        self.assertEqual(judge_status([("a", 0), ("b", 0)], "H7"), ("DONE", 0))
        self.assertEqual(judge_status([("a", 0), ("b", 0)], None), ("NEEDS_REVIEW", 1))
        self.assertEqual(judge_status([("a", 0), ("b", 1)], "H7"), ("BLOCKED", 1))

    def test_ai_starters_list_sane(self):
        self.assertIn("说白了", AI_FLAVOR_STARTERS)
        self.assertIn("值得一提的是", AI_FLAVOR_STARTERS)


class TestAssetIndexGates(unittest.TestCase):
    """ADR-0084 质量门：索引合法性/事实等级/母版漂移/P3 说明（工单 8）。"""

    def _index(self, assets):
        return {"schema_version": 1, "source_fingerprint": "sha256:t", "assets": assets}

    def _asset(self, aid="CH_张三", **kw):
        base = {
            "asset_id": aid, "stable_key": "CHR:00001:1", "type": "人物",
            "name": "张三", "priority": "P1", "fact_level": "剧本事实",
            "evidence": [{"segment": "段01", "line": "00001", "quote": "张三进门"}],
            "dependencies": [], "relations": [], "status": "CONFIRMED",
        }
        base.update(kw)
        return base

    def test_dangling_binding_blocked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / ".process").mkdir()
            index = self._index([self._asset(relations=[{"kind": "WEARS", "target": "CO_不存在"}])])
            (lib / ".process" / "资产索引.json").write_text(
                __import__("json").dumps(index, ensure_ascii=False), encoding="utf-8")
            r = run_py(
                    [str(Path(__file__).resolve().parents[2] / "scripts" / "check_asset_index.py"), "--lib", str(lib)],
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("CO_不存在", (r.stdout or "") + (r.stderr or ""))

    def test_script_fact_without_quote_blocked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / ".process").mkdir()
            index = self._index([self._asset(evidence=[])])
            (lib / ".process" / "资产索引.json").write_text(
                __import__("json").dumps(index, ensure_ascii=False), encoding="utf-8")
            r = run_py(
                    [str(Path(__file__).resolve().parents[2] / "scripts" / "check_asset_index.py"), "--lib", str(lib)],
            )
            self.assertNotEqual(r.returncode, 0)

    def test_master_drift_blocked(self):
        from engine.continuity import build_scene_master, check_master_drift
        index = self._index([
            {**self._asset(aid="SC_走廊", type="场景", name="走廊", stable_key="SCN:00001:1",
                           relations=[{"kind": "CONNECTS_TO", "target": "SC_大厅"}]),
             "evidence": [{"segment": "段01", "line": "00001", "quote": "走进走廊"}]},
            {**self._asset(aid="SC_大厅", type="场景", name="大厅", stable_key="SCN:00002:1",
                           relations=[{"kind": "CONNECTS_TO", "target": "SC_走廊"}]),
             "evidence": [{"segment": "段01", "line": "00002", "quote": "走进大厅"}]},
        ])
        text = build_scene_master(index)
        tampered = text.replace("SC_大厅", "SC_别处")
        self.assertTrue(check_master_drift(index, tampered))

    def test_p3_without_function_blocked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / ".process").mkdir()
            index = self._index([self._asset(aid="PR_摆件", type="道具", name="摆件",
                                             priority="P3", story_function="")])
            (lib / ".process" / "资产索引.json").write_text(
                __import__("json").dumps(index, ensure_ascii=False), encoding="utf-8")
            r = run_py(
                    [str(Path(__file__).resolve().parents[2] / "scripts" / "check_asset_index.py"), "--lib", str(lib)],
            )
            self.assertNotEqual(r.returncode, 0)

    def test_fake_quote_blocked(self):
        # 工单D：quote 非空但不在所指段文件中 → 假摘句阻断
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / "原文段" / "N-A").mkdir(parents=True)
            (lib / "原文段" / "N-A" / "s1.md").write_text(
                "---\n小说名称: T\n作者: A\n---\n张三进茶楼喝茶", encoding="utf-8")
            (lib / ".process").mkdir()
            asset = self._asset(evidence=[{"segment": "s1.md", "chapter": "第1章",
                                           "line": "00001", "quote": "张三根本没说过这句话"}])
            index = self._index([asset])
            index["stable_key_mapping"] = {
                "keys": {"CHR:00001:1": {"asset_id": "CH_张三", "type": "人物",
                                         "aliases": [], "first_evidence": None}},
                "type_counters": {}}
            (lib / ".process" / "资产索引.json").write_text(
                json.dumps(index, ensure_ascii=False), encoding="utf-8")
            r = run_py(
                    [str(Path(__file__).resolve().parents[2] / "scripts" / "check_asset_index.py"), "--lib", str(lib)],
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("假摘句", (r.stdout or "") + (r.stderr or ""))

    def test_mapping_drift_blocked(self):
        # 工单D：mapping 指向另一实体 → 漂移阻断
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / ".process").mkdir()
            asset = self._asset()
            asset["evidence"] = []
            asset["fact_level"] = "强推断"
            index = self._index([asset])
            index["stable_key_mapping"] = {
                "keys": {"CHR:00001:1": {"asset_id": "CH_别人", "type": "人物",
                                         "aliases": [], "first_evidence": None}},
                "type_counters": {}}
            (lib / ".process" / "资产索引.json").write_text(
                json.dumps(index, ensure_ascii=False), encoding="utf-8")
            r = run_py(
                    [str(Path(__file__).resolve().parents[2] / "scripts" / "check_asset_index.py"), "--lib", str(lib)],
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("漂移", (r.stdout or "") + (r.stderr or ""))


if __name__ == "__main__":
    unittest.main()
