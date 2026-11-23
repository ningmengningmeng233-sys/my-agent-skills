#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：场景空间连续性母版（ADR-0084 / 工单 5）。"""
import unittest
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.continuity import (
    build_scene_master,
    check_connects_consistency,
    check_deriv_anchors,
    check_master_drift,
    check_relation_types,
    parse_master_sections,
    write_scene_master,
)


def _index():
    return {
        "schema_version": 1,
        "source_fingerprint": "sha256:test",
        "assets": [
            {
                "asset_id": "SC_走廊",
                "stable_key": "SCN:00002:1",
                "type": "场景",
                "subtype": "室内空间",
                "name": "走廊",
                "story_function": "通往302房间的入口",
                "priority": "P1",
                "fact_level": "剧本事实",
                "evidence": [{"segment": "段01.md", "chapter": "第1章", "line": "00002", "quote": "走进昏暗的走廊"}],
                "dependencies": [],
                "relations": [{"kind": "CONNECTS_TO", "target": "SC_302房间"}],
                "status": "CONFIRMED",
            },
            {
                "asset_id": "SC_302房间",
                "stable_key": "SCN:00003:1",
                "type": "场景",
                "subtype": "室内空间",
                "name": "302房间",
                "story_function": "核心密室",
                "priority": "P0",
                "fact_level": "剧本事实",
                "evidence": [{"segment": "段01.md", "chapter": "第1章", "line": "00005", "quote": "推开302房间的门"}],
                "dependencies": [],
                "relations": [{"kind": "CONNECTS_TO", "target": "SC_走廊"}],
                "status": "CONFIRMED",
            },
        ],
    }


class TestContinuity(unittest.TestCase):
    def test_master_contains_unknown_slots(self):
        text = build_scene_master(_index())
        self.assertIn("## SC_走廊", text)
        self.assertIn("## SC_302房间", text)
        self.assertIn("未说明", text)
        sections = parse_master_sections(text)
        self.assertIn("SC_走廊", sections)
        self.assertIn("SC_302房间", sections)

    def test_connects_consistency_pass(self):
        self.assertEqual(check_connects_consistency(_index()), [])

    def test_dangling_connects_reported(self):
        idx = _index()
        idx["assets"][0]["relations"] = [{"kind": "CONNECTS_TO", "target": "SC_不存在"}]
        errors = check_connects_consistency(idx)
        self.assertTrue(any("SC_不存在" in e for e in errors))

    def test_master_drift_detected(self):
        idx = _index()
        text = build_scene_master(idx)
        # 手工篡改母版连接点应被检出
        tampered = text.replace("SC_302房间", "SC_别处")
        errors = check_master_drift(idx, tampered)
        self.assertTrue(errors)

    def test_space_facts_written_with_evidence(self):
        idx = _index()
        idx["assets"][0]["space_facts"] = {
            "space_structure": [{"value": "走廊尽头右侧为木门", "segment": "段01.md",
                                 "chapter": "第1章", "line": "00001", "quote": "走廊尽头右侧为木门"}],
            "anchor": [{"value": "门内有台灯", "segment": "段01.md",
                        "chapter": "第1章", "line": "00001", "quote": "门内有台灯"}],
        }
        text = build_scene_master(idx)
        # 有证据只写有证据的锚点，未写门把手方向就保留未说明
        self.assertIn("走廊尽头右侧为木门", text)
        self.assertIn("门内有台灯", text)
        self.assertIn("未说明", text)
        self.assertNotIn("门把手", text)

    def test_single_direction_reported(self):
        idx = _index()
        idx["assets"][1]["relations"] = []
        errors = check_connects_consistency(idx)
        self.assertTrue(any("单向" in e for e in errors))

    def test_non_scene_target_rejected(self):
        idx = _index()
        idx["assets"].append({
            "asset_id": "PR_钥匙", "stable_key": "PRP:00001:1", "type": "道具",
            "name": "钥匙", "story_function": "开门", "priority": "P2", "fact_level": "剧本事实",
            "evidence": [{"segment": "段01.md", "line": "00001", "quote": "拿起钥匙"}],
            "dependencies": [], "relations": [], "status": "CONFIRMED",
        })
        idx["assets"][0]["relations"] = [{"kind": "CONNECTS_TO", "target": "PR_钥匙"}]
        errors = check_connects_consistency(idx)
        self.assertTrue(any("必须为场景" in e for e in errors))

    def test_relation_matrix_rejects_wrong_source(self):
        idx = _index()
        idx["assets"][0]["relations"] = [{"kind": "WEARS", "target": "SC_302房间"}]
        errors = check_relation_types(idx)
        self.assertTrue(errors)

    def test_state_of_self_loop_reported(self):
        # R4b：母资产 STATE_OF 自环必须报错（旧索引不得被默默重解释）
        idx = _index()
        idx["assets"][0]["relations"] = [{"kind": "STATE_OF", "target": "SC_走廊"}]
        errors = check_relation_types(idx)
        self.assertTrue(any("自环" in e for e in errors), f"应报自环: {errors}")

    def test_state_of_cross_type_rejected(self):
        # R4b：四类契约下无变体节点，任何 STATE_OF 边都按矩阵裁决为非法
        idx = _index()
        idx["assets"][0]["relations"] = [{"kind": "STATE_OF", "target": "SC_302房间"}]
        errors = check_relation_types(idx)
        self.assertTrue(errors)

    def test_master_merge_keeps_manual_fields(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            write_scene_master(lib, _index())
            master = lib / "连续性" / "场景母版.md"
            text = master.read_text(encoding="utf-8")
            manual = text.replace("| 固定锚点 | 未说明 |", "| 固定锚点 | 美术确认吊灯在中央（人工） |", 1)
            master.write_text(manual, encoding="utf-8")
            write_scene_master(lib, _index())
            merged = master.read_text(encoding="utf-8")
            self.assertIn("美术确认吊灯在中央", merged)
            self.assertTrue((lib / "连续性" / "母版冲突待审.md").is_file() or "美术确认吊灯在中央" in merged)

    def test_deriv_anchor_check(self):
        idx = _index()
        idx["assets"][0]["space_facts"] = {
            "anchor": [{"value": "门内有台灯", "segment": "段01.md",
                        "chapter": "第1章", "line": "00001", "quote": "门内有台灯"}],
        }
        text = build_scene_master(idx)
        self.assertEqual(check_deriv_anchors(text, "黄昏走廊，门内有台灯亮着", "SC_走廊"), [])
        self.assertTrue(check_deriv_anchors(text, "黄昏走廊，一片空旷", "SC_走廊"))


if __name__ == "__main__":
    unittest.main()
