#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：跨平台流水线状态机（ADR-0084 / 工单 7）。"""
import json
import unittest
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.pipeline_state import derive_pipeline_state


def _write(p: Path, content: str = "内容"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class TestPipelineState(unittest.TestCase):
    def test_empty_lib_is_waiting_input(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "资产库"
            lib.mkdir()
            s = derive_pipeline_state(lib)
            self.assertEqual(s["state"], "WAITING_INPUT")

    def test_half_done_catalog_requires_index(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "资产库"
            _write(lib / "原文段" / "小说-作者" / "段01.md", "# 第一章\n正文")
            _write(lib / "测试-登记表.md", "| 主名 |\n| 张三 |")
            _write(lib / "段产物" / "小说-作者" / "段01-资产记录.md", "| 类型 | 名称 |\n| 人物 | 张三 |")
            _write(lib / "反推_最终.md", "# 反推\n## 人物资产提取表\n| 资产ID | x |\n| CH_张三 | 张三 |")
            s = derive_pipeline_state(lib)
            # 缺资产索引 → 回退到 EXTRACTED，不伪装 CATALOGED
            self.assertEqual(s["state"], "EXTRACTED")
            self.assertIn("next_command", s)

    def test_prompts_ready_without_verify(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "资产库"
            _write(lib / "原文段" / "小说-作者" / "段01.md", "# 第一章\n正文")
            _write(lib / "测试-登记表.md", "| 主名 |\n| 张三 |")
            _write(lib / "段产物" / "小说-作者" / "段01-资产记录.md", "| 类型 | 名称 |\n| 人物 | 张三 |")
            _write(lib / "反推_最终.md", "# 反推")
            index = {"schema_version": 1, "source_fingerprint": "sha256:x", "assets": []}
            _write(lib / ".process" / "资产索引.json", json.dumps(index, ensure_ascii=False))
            _write(lib / "提示词" / "基础" / "人物提示词.md", "## 001. 张三\n\n正文内容\n")
            s = derive_pipeline_state(lib)
            self.assertEqual(s["state"], "PROMPTS_READY")

    def test_verify_failed_is_blocked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "资产库"
            _write(lib / "原文段" / "小说-作者" / "段01.md", "# 第一章\n正文")
            _write(lib / "测试-登记表.md", "| 主名 |\n| 张三 |")
            _write(lib / "段产物" / "小说-作者" / "段01-资产记录.md", "| 类型 | 名称 |\n| 人物 | 张三 |")
            _write(lib / "反推_最终.md", "# 反推")
            index = {"schema_version": 1, "source_fingerprint": "sha256:x", "assets": []}
            _write(lib / ".process" / "资产索引.json", json.dumps(index, ensure_ascii=False))
            _write(lib / "提示词" / "基础" / "人物提示词.md", "## 001. 张三\n\n正文内容\n")
            s = derive_pipeline_state(lib, verify_results=[("check_schedule", 1)], style="H7")
            self.assertEqual(s["state"], "BLOCKED")

    def test_pending_conflict_needs_review(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "资产库"
            _write(lib / "原文段" / "小说-作者" / "段01.md", "# 第一章\n正文")
            _write(lib / "测试-登记表.md", "| 主名 |\n| 张三 |")
            _write(lib / "段产物" / "小说-作者" / "段01-资产记录.md", "| 类型 | 名称 |\n| 人物 | 张三 |")
            _write(lib / "反推_最终.md", "# 反推")
            index = {
                "schema_version": 1,
                "source_fingerprint": "sha256:x",
                "assets": [{
                    "asset_id": "CH_神秘人", "stable_key": "CHR:00001:1", "type": "人物",
                    "name": "神秘人", "priority": "P2", "fact_level": "待定冲突",
                    "evidence": [], "dependencies": [], "relations": [], "status": "PENDING",
                }],
            }
            _write(lib / ".process" / "资产索引.json", json.dumps(index, ensure_ascii=False))
            _write(lib / "提示词" / "基础" / "人物提示词.md", "## 001. 张三\n\n正文内容\n")
            s = derive_pipeline_state(lib)
            self.assertEqual(s["state"], "NEEDS_REVIEW")


def _make_delivered_lib(root: Path, style: str = "H7") -> Path:
    """构造完整可交付库（含有效交付清单），供工单B闭环用例复用。

    构造逻辑集中在 测试/unit/conftest.py，避免同一交付夹具在两处漂移。
    """
    import conftest

    return conftest.make_delivered_lib(root, style)


def _full_check_list() -> list:
    """签发契约的必需检查项名单（读唯一来源；契约扩项时本文件自动跟随）。"""
    from scripts.engine.pipeline_state import REQUIRED_DELIVERY_CHECKS

    return [{"name": n, "rc": 0} for n in REQUIRED_DELIVERY_CHECKS]


class TestDeliveryLoop(unittest.TestCase):
    """工单B：status --json 必须读出 DELIVERED；关键输入变化立即失效。"""

    def test_delivered_reachable_from_manifest(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = _make_delivered_lib(Path(td))
            s = derive_pipeline_state(lib)
            self.assertEqual(s["state"], "DELIVERED")
            self.assertEqual(s["reason_code"], "DELIVERY_VALID")

    def test_style_change_invalidates(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = _make_delivered_lib(Path(td))
            seg = lib / "原文段" / "小说-作者" / "段01.md"
            _write(lib / ".start-state.json", json.dumps({"style": "A1", "input_source": str(seg)}, ensure_ascii=False))
            s = derive_pipeline_state(lib)
            self.assertNotEqual(s["state"], "DELIVERED")
            self.assertEqual(s["reason_code"], "STYLE_CHANGED")

    def test_source_change_invalidates(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = _make_delivered_lib(Path(td))
            _write(lib / "原文段" / "小说-作者" / "段01.md", "# 第一章\n张三进茶楼改了")
            s = derive_pipeline_state(lib)
            self.assertNotEqual(s["state"], "DELIVERED")
            self.assertEqual(s["reason_code"], "SOURCE_CHANGED")

    def test_missing_base_section_falls_back(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = _make_delivered_lib(Path(td))
            _write(lib / "提示词" / "基础" / "人物提示词.md", "# 空标题\n\n无节\n")
            s = derive_pipeline_state(lib)
            self.assertIn(s["state"], ("CATALOGED", "VERIFIED"))

    def test_bad_manifest_never_delivered(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = _make_delivered_lib(Path(td))
            from scripts.engine.pipeline_state import _current_fingerprints
            cur = _current_fingerprints(lib)
            bad = {
                "schema_version": 1, "source_fingerprint": cur["source_fingerprint"],
                "style_fingerprint": cur["style_fingerprint"], "style": "H7",
                "asset_index_hash": cur["asset_index_hash"], "artifact_hashes": cur["artifact_hashes"],
                "counts": {}, "checks": [{"name": "x", "rc": 1}],
                "generated_at": "t", "status": "DELIVERED",
            }
            _write(lib / "交付清单.json", json.dumps(bad, ensure_ascii=False))
            s = derive_pipeline_state(lib)
            self.assertNotEqual(s["state"], "DELIVERED")

    def test_single_check_manifest_cannot_impersonate_delivery(self):
        # R3b：只写一个 check_asset_index 的清单不得冒充完整签发（必需检查集合）
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = _make_delivered_lib(Path(td))
            from scripts.engine.pipeline_state import _current_fingerprints
            cur = _current_fingerprints(lib)
            fake = {
                "schema_version": 1, "source_fingerprint": cur["source_fingerprint"],
                "style_fingerprint": cur["style_fingerprint"], "style": "H7",
                "asset_index_hash": cur["asset_index_hash"], "artifact_hashes": cur["artifact_hashes"],
                "counts": {}, "checks": [{"name": "check_asset_index", "rc": 0}],
                "generated_at": "t", "status": "DELIVERED",
            }
            _write(lib / "交付清单.json", json.dumps(fake, ensure_ascii=False))
            s = derive_pipeline_state(lib)
            self.assertNotEqual(s["state"], "DELIVERED")
            self.assertEqual(s["reason_code"], "DELIVERY_CHECKS_INCOMPLETE")

    def test_failed_task_blocks(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lib = _make_delivered_lib(Path(td))
            conn = sqlite3.connect(lib / ".asset_state.db")
            conn.execute(
                "CREATE TABLE segment_tasks (segment_path TEXT PRIMARY KEY, out_path TEXT, status TEXT, "
                "retries INTEGER, error TEXT, seg_hash TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, "
                "created_at INTEGER, updated_at INTEGER)")
            conn.execute("INSERT INTO segment_tasks (segment_path, status) VALUES ('段01.md', 'failed')")
            conn.commit()
            conn.close()
            s = derive_pipeline_state(lib)
            self.assertEqual(s["state"], "BLOCKED")
            self.assertEqual(s["reason_code"], "TASK_FAILED")


class TestSigningGate(unittest.TestCase):
    """E1 签发门：启动证据（状态文件 + 原始输入 + 画风一致）不可核验时拒签。"""

    def test_validate_accepts_healthy_evidence(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            from scripts.engine.pipeline_state import validate_start_evidence
            lib = _make_delivered_lib(Path(td))
            ok, reason, src, state_style = validate_start_evidence(lib, "H7")
            self.assertTrue(ok, reason)
            self.assertEqual(reason, "EVIDENCE_OK")
            self.assertTrue(src.is_file())
            self.assertEqual(state_style, "H7")

    def test_missing_start_state_is_source_unverifiable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            from scripts.engine.pipeline_state import validate_start_evidence
            lib = _make_delivered_lib(Path(td))
            (lib / ".start-state.json").unlink()
            ok, reason, _src, _style = validate_start_evidence(lib, "H7")
            self.assertFalse(ok)
            self.assertEqual(reason, "SOURCE_UNVERIFIABLE")

    def test_missing_input_source_is_source_unverifiable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            from scripts.engine.pipeline_state import validate_start_evidence
            lib = _make_delivered_lib(Path(td))
            gone = lib / "原文段" / "小说-作者" / "原文已删除.txt"
            _write(lib / ".start-state.json",
                   json.dumps({"style": "H7", "input_source": str(gone)}, ensure_ascii=False))
            ok, reason, src, _style = validate_start_evidence(lib, "H7")
            self.assertFalse(ok)
            self.assertEqual(reason, "SOURCE_UNVERIFIABLE")
            self.assertEqual(src, gone)

    def test_style_mismatch_between_state_and_explicit_style(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            from scripts.engine.pipeline_state import validate_start_evidence
            lib = _make_delivered_lib(Path(td), style="H12")
            ok, reason, _src, state_style = validate_start_evidence(lib, "A2")
            self.assertFalse(ok)
            self.assertEqual(reason, "STYLE_MISMATCH")
            self.assertEqual(state_style, "H12")

    def test_style_alias_of_same_card_is_accepted(self):
        # A2 与「仙侠奇幻风」是同一张画风卡的编号/卡名，必须识别为等价
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            from scripts.engine.pipeline_state import validate_start_evidence
            lib = _make_delivered_lib(Path(td), style="仙侠奇幻风")
            for expected in ("A2", "仙侠奇幻风", "仙侠奇幻"):
                ok, reason, _src, _style = validate_start_evidence(lib, expected)
                self.assertTrue(ok, f"{expected} → {reason}")

    def test_manifest_signed_without_source_cannot_be_delivered(self):
        # 复现 P0：源文件已缺失时签发的清单曾能被判为 DELIVERY_VALID
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            from scripts.engine.pipeline_state import _current_fingerprints
            lib = _make_delivered_lib(Path(td))
            (lib / ".start-state.json").unlink()
            cur = _current_fingerprints(lib)
            unsigned_source = {
                "schema_version": 1, "source_fingerprint": cur["source_fingerprint"],
                "style_fingerprint": cur["style_fingerprint"], "style": "H7",
                "asset_index_hash": cur["asset_index_hash"], "artifact_hashes": cur["artifact_hashes"],
                "counts": {}, "checks": _full_check_list(),
                "generated_at": "t", "status": "DELIVERED",
            }
            _write(lib / "交付清单.json", json.dumps(unsigned_source, ensure_ascii=False))
            s = derive_pipeline_state(lib)
            self.assertNotEqual(s["state"], "DELIVERED")
            self.assertEqual(s["reason_code"], "SOURCE_UNVERIFIABLE")

    def test_build_manifest_refuses_when_start_state_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            from scripts.engine.pipeline_state import DeliveryRefused, build_delivery_manifest
            lib = _make_delivered_lib(Path(td))
            (lib / ".start-state.json").unlink()
            with self.assertRaises(DeliveryRefused) as ctx:
                build_delivery_manifest(lib, "H7", [("check_final", 0)])
            self.assertEqual(ctx.exception.reason_code, "SOURCE_UNVERIFIABLE")

    def test_build_manifest_refuses_conflicting_style(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            from scripts.engine.pipeline_state import DeliveryRefused, build_delivery_manifest
            lib = _make_delivered_lib(Path(td), style="H12")
            with self.assertRaises(DeliveryRefused) as ctx:
                build_delivery_manifest(lib, "A2", [("check_final", 0)])
            self.assertEqual(ctx.exception.reason_code, "STYLE_MISMATCH")

    def test_build_manifest_signs_with_equivalent_style(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            from scripts.engine.pipeline_state import build_delivery_manifest
            lib = _make_delivered_lib(Path(td), style="仙侠奇幻风")
            manifest = build_delivery_manifest(lib, "A2", [("check_final", 0)])
            self.assertEqual(manifest["status"], "DELIVERED")


class TestSigningGateContract(unittest.TestCase):
    """E1 计划契约字面用例：build_delivery_manifest 必须对三类证据缺失抛 ValueError。"""

    def test_manifest_rejects_missing_start_state(self):
        import tempfile
        from scripts.engine.pipeline_state import build_delivery_manifest
        with tempfile.TemporaryDirectory() as td:
            lib = _make_delivered_lib(Path(td))
            (lib / ".start-state.json").unlink()
            with self.assertRaises(ValueError):
                build_delivery_manifest(lib, "H7", _full_check_list())

    def test_manifest_rejects_missing_input_file(self):
        import json
        import tempfile
        from scripts.engine.pipeline_state import build_delivery_manifest
        with tempfile.TemporaryDirectory() as td:
            lib = _make_delivered_lib(Path(td))
            state = json.loads((lib / ".start-state.json").read_text(encoding="utf-8"))
            Path(state["input_source"]).unlink()
            with self.assertRaises(ValueError):
                build_delivery_manifest(lib, "H7", _full_check_list())

    def test_manifest_rejects_conflicting_style(self):
        import tempfile
        from scripts.engine.pipeline_state import build_delivery_manifest
        with tempfile.TemporaryDirectory() as td:
            lib = _make_delivered_lib(Path(td), style="H12")
            with self.assertRaises(ValueError):
                build_delivery_manifest(lib, "A2", _full_check_list())

    def test_manifest_style_mismatch_beats_fingerprints(self):
        # 状态 H12 + 清单自称 A2（指纹自洽）不得判 DELIVERY_VALID
        import tempfile
        from scripts.engine.pipeline_state import _current_fingerprints
        with tempfile.TemporaryDirectory() as td:
            lib = _make_delivered_lib(Path(td), style="H12")
            cur = _current_fingerprints(lib)
            forged = {
                "schema_version": 1, "source_fingerprint": cur["source_fingerprint"],
                "style_fingerprint": cur["style_fingerprint"], "style": "A2",
                "asset_index_hash": cur["asset_index_hash"], "artifact_hashes": cur["artifact_hashes"],
                "counts": {}, "checks": _full_check_list(),
                "generated_at": "t", "status": "DELIVERED",
            }
            _write(lib / "交付清单.json", json.dumps(forged, ensure_ascii=False))
            s = derive_pipeline_state(lib)
            self.assertNotEqual(s["state"], "DELIVERED")
            self.assertEqual(s["reason_code"], "STYLE_MISMATCH")


if __name__ == "__main__":
    unittest.main()
