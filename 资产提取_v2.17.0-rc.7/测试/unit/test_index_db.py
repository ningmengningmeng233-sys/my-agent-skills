#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 9：断点库的节点指纹语义。

覆盖：
- 内容未变 **且** 节点指纹未变才跳过；
- 换模型 / 改 max_tokens 会让断点失效（否则「换了模型却复用旧产物」会被静默当成正确）；
- 旧记录没有节点指纹时按变更重新生成（安全策略），不得当成命中；
- 指纹不得包含密钥。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.index_db import (  # noqa: E402
    IndexDB,
    current_model_summary,
    file_hash,
    task_node_fingerprint,
)

RULES_SOURCE = REPO_ROOT / "scripts" / "engine" / "prompt_rules.py"


def _tmp_root() -> Path:
    """测试临时根：一律不落 C 盘。

    优先显式 `ASSET_TEST_TMP`，否则跟随 gate runner 给进程隔离出来的 TEMP/TMP
    （本仓跑门时它在 F 盘任务根内）。
    """
    base = os.environ.get("ASSET_TEST_TMP")
    return Path(base) if base else Path(tempfile.gettempdir())


class TestRegistryFingerprintIncludesTemplate(unittest.TestCase):
    """W5（评审发现）：登记（候选主名表）断点同样要把模板算进指纹。

    与 `extract_segments` 刚修的那处完全同类：`scan_registry` 用 `候选主名表模板.md` 组请求，
    却调用不带 `template_version` 的 `task_node_fingerprint` → 改模板后候选表以「断点跳过」
    沿用旧产物，模板改动静默失效。
    """

    def test_candidate_template_edit_changes_registry_fingerprint(self) -> None:
        import tempfile

        from scan_registry import segment_task_fingerprint

        with tempfile.TemporaryDirectory() as td:
            tpl = Path(td) / "候选主名表模板.md"
            tpl.write_text("## 系统提示\n甲\n## 用户输入模板\n{正文}\n", encoding="utf-8")
            seg = Path(td) / "段01.md"
            seg.write_text("林姑娘走进茶楼", encoding="utf-8")
            with patch("scan_registry.TMPL_CANDIDATE", tpl):
                first = segment_task_fingerprint(seg)
                self.assertEqual(segment_task_fingerprint(seg), first, "同一模板必须稳定")
                tpl.write_text("## 系统提示\n乙\n## 用户输入模板\n{正文}\n", encoding="utf-8")
                self.assertNotEqual(segment_task_fingerprint(seg), first,
                                    "改了候选表模板就必须重跑登记")


class TestExtractionFingerprintIncludesTemplate(unittest.TestCase):
    """提取断点必须把**段级提取模板**算进指纹。

    `task_node_fingerprint` 早就接受 `template_version` 参数，文档也写着"模板版本变了就不再命中"，
    但 `extract_segments` 调用时**没传**——于是改了模板，旧产物仍以「已完成（断点跳过）」的身份
    被当成正确。实测代价：模板示例 `[状态:受损]` 改成正确契约后重跑，提取被整段跳过，
    索引里那条 `STATE_OF→受损` 照样把 verify 卡在 `BLOCKED`。
    """

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lib = Path(self._tmp.name)
        self.seg = self.lib / "段01.md"
        self.seg.write_text("林姑娘走进茶楼", encoding="utf-8")
        self.tpl = self.lib / "段级提取模板.md"
        self.tpl.write_text("## 系统提示\n甲\n## 用户输入模板\n{正文}\n", encoding="utf-8")

    def test_template_edit_changes_the_extraction_fingerprint(self) -> None:
        from extract_segments import segment_task_fingerprint

        with patch("extract_segments.TEMPLATE_FILE", self.tpl):
            first = segment_task_fingerprint(self.seg)
            self.assertEqual(segment_task_fingerprint(self.seg), first, "同一模板必须稳定")
            self.tpl.write_text("## 系统提示\n乙\n## 用户输入模板\n{正文}\n", encoding="utf-8")
            self.assertNotEqual(segment_task_fingerprint(self.seg), first,
                                "改了模板就必须重跑提取，否则旧产物被静默复用")

    def test_template_edit_invalidates_the_checkpoint(self) -> None:
        from extract_segments import segment_task_fingerprint

        out = self.lib / "候选表.md"
        out.write_text("产物", encoding="utf-8")
        with patch("extract_segments.TEMPLATE_FILE", self.tpl):
            fp = segment_task_fingerprint(self.seg)
            with IndexDB(self.lib) as db:
                db.mark_done(self.seg, out, seg_hash=None, node_fp=fp)
                self.assertTrue(db.should_skip(self.seg, out, node_fp=fp))
            self.tpl.write_text("## 系统提示\n乙\n## 用户输入模板\n{正文}\n", encoding="utf-8")
            newer = segment_task_fingerprint(self.seg)
            with IndexDB(self.lib) as db:
                self.assertFalse(db.should_skip(self.seg, out, node_fp=newer),
                                 "模板更新后不得再判「已完成（断点跳过）」")


class TestCheckpointFingerprint(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lib = Path(self._tmp.name)
        self.seg = self.lib / "段01.md"
        self.seg.write_text("林姑娘走进茶楼", encoding="utf-8")
        self.out = self.lib / "候选表.md"

    def _db(self) -> IndexDB:
        return IndexDB(self.lib)

    def test_skip_requires_both_content_and_fingerprint_to_match(self) -> None:
        with self._db() as db:
            fp = task_node_fingerprint(self.seg)
            self.out.write_text("产物", encoding="utf-8")
            db.mark_done(self.seg, self.out, seg_hash=None, node_fp=fp)
            self.assertTrue(db.should_skip(self.seg, self.out, node_fp=fp))

    def test_model_change_invalidates_the_checkpoint(self) -> None:
        with patch.dict(os.environ, {"SUMMARY_API_MODEL": "model-a"}, clear=False):
            with self._db() as db:
                fp = task_node_fingerprint(self.seg)
                self.out.write_text("产物", encoding="utf-8")
                db.mark_done(self.seg, self.out, seg_hash=None, node_fp=fp)
                self.assertTrue(db.should_skip(self.seg, self.out, node_fp=fp))
        with patch.dict(os.environ, {"SUMMARY_API_MODEL": "model-b"}, clear=False):
            other = task_node_fingerprint(self.seg)
            with self._db() as db:
                self.assertNotEqual(other, fp, "换模型必须改变节点指纹")
                self.assertFalse(db.should_skip(self.seg, self.out, node_fp=other))

    def test_max_tokens_change_invalidates_the_checkpoint(self) -> None:
        with patch.dict(os.environ, {"SUMMARY_MAX_TOKENS": "8000"}, clear=False):
            first = task_node_fingerprint(self.seg)
        with patch.dict(os.environ, {"SUMMARY_MAX_TOKENS": "16000"}, clear=False):
            self.assertNotEqual(task_node_fingerprint(self.seg), first)

    def test_content_change_invalidates_the_checkpoint(self) -> None:
        with self._db() as db:
            fp = task_node_fingerprint(self.seg)
            self.out.write_text("产物", encoding="utf-8")
            db.mark_done(self.seg, self.out, seg_hash=None, node_fp=fp)
            self.seg.write_text("林姑娘走出茶楼", encoding="utf-8")
            self.assertFalse(db.should_skip(self.seg, self.out, node_fp=fp))

    def test_legacy_row_without_fingerprint_is_not_a_hit(self) -> None:
        """旧 done 记录没有节点指纹：不得当成命中，按变更重新生成。"""
        with self._db() as db:
            self.out.write_text("产物", encoding="utf-8")
            db.mark_done(self.seg, self.out, seg_hash=None)
            fp = task_node_fingerprint(self.seg)
            self.assertFalse(db.should_skip(self.seg, self.out, node_fp=fp))
            # 不传指纹时保持旧行为（向后兼容）
            self.assertTrue(db.should_skip(self.seg, self.out))

    def test_fingerprint_excludes_secrets(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "占位-甲"}, clear=False):
            first = task_node_fingerprint(self.seg)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "占位-乙"}, clear=False):
            self.assertEqual(task_node_fingerprint(self.seg), first,
                             "密钥不得参与节点指纹")


class TestFingerprintIncludesRulesetVersion(unittest.TestCase):
    """R09-D07：`rule_version` 在生产路径恒空——文档承诺「规则版本变了就不再命中」是假的。

    `task_node_fingerprint` 收 `rule_version`，`node_fingerprint` 也确实拼了 `rule:` 段，
    但 `extract_segments` 与 `scan_registry` 两处调用都不传 ⇒ 改了规则单一真相源
    `scripts/engine/prompt_rules.py`（PLACEHOLDER_NAMES / style_lock_layer_checks /
    generation_requirements / check_compliance）后，已跑的段与候选表照旧以
    「已完成（断点跳过）」的身份被复用。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=_tmp_root())
        self.addCleanup(self._tmp.cleanup)
        self.lib = Path(self._tmp.name)
        self.seg = self.lib / "段01.md"
        self.seg.write_text("林姑娘走进茶楼", encoding="utf-8")
        self.tpl = self.lib / "段级提取模板.md"
        self.tpl.write_text("## 系统提示\n甲\n## 用户输入模板\n{正文}\n", encoding="utf-8")
        self.tmpl_registry = self.lib / "候选主名表模板.md"
        self.tmpl_registry.write_text("## 系统提示\n甲\n## 用户输入模板\n{正文}\n", encoding="utf-8")

    def _extract_fp(self) -> str:
        from extract_segments import segment_task_fingerprint

        with patch("extract_segments.TEMPLATE_FILE", self.tpl):
            return segment_task_fingerprint(self.seg)

    def _scan_fp(self) -> str:
        from scan_registry import segment_task_fingerprint

        with patch("scan_registry.TMPL_CANDIDATE", self.tmpl_registry):
            return segment_task_fingerprint(self.seg)

    def test_extract_passes_a_nonempty_ruleset_digest(self) -> None:
        # 旧口径（rule 段为空）必须与生产指纹不相等
        legacy = task_node_fingerprint(self.seg, template_version=file_hash(self.tpl) or "")
        self.assertNotEqual(self._extract_fp(), legacy,
                            "提取断点必须把规则摘要传进 node_fp（R09-D07）")

    def test_scan_passes_a_nonempty_ruleset_digest(self) -> None:
        legacy = task_node_fingerprint(self.seg,
                                       template_version=file_hash(self.tmpl_registry) or "")
        self.assertNotEqual(self._scan_fp(), legacy,
                            "登记断点必须把规则摘要传进 node_fp（R09-D07）")

    def test_ruleset_digest_edit_changes_both_productions(self) -> None:
        """改规则摘要 ⇒ 两个生产入口的 node_fp 都必须跟着变（旧实现里根本没人传它）。"""
        import extract_segments
        import scan_registry

        first = self._extract_fp()
        with patch.object(extract_segments, "RULESET_VERSION",
                          getattr(extract_segments, "RULESET_VERSION", "") + "|规则改了",
                          create=True):
            self.assertNotEqual(self._extract_fp(), first, "改了规则摘要就必须重跑提取")

        scan_first = self._scan_fp()
        with patch.object(scan_registry, "RULESET_VERSION",
                          getattr(scan_registry, "RULESET_VERSION", "") + "|规则改了",
                          create=True):
            self.assertNotEqual(self._scan_fp(), scan_first, "改了规则摘要就必须重跑登记")

    def test_existing_done_rows_recompute_once_digest_is_wired(self) -> None:
        """规则摘要接入后，存量 done 行的指纹必然失配 ⇒ 重跑一次（设计意图，成本须在报告点名）。"""
        legacy = task_node_fingerprint(self.seg, template_version=file_hash(self.tpl) or "")
        current = self._extract_fp()
        out = self.lib / "候选表.md"
        out.write_text("产物", encoding="utf-8")
        with IndexDB(self.lib) as db:
            db.mark_done(self.seg, out, seg_hash=None, node_fp=legacy)
            self.assertTrue(db.should_skip(self.seg, out, node_fp=legacy),
                            "旧行按旧指纹仍应命中（内容+指纹都一致才跳过）")
            # 不传指纹时保持旧行为（向后兼容）
            self.assertTrue(db.should_skip(self.seg, out))
            self.assertFalse(db.should_skip(self.seg, out, node_fp=current),
                             "带规则摘要的新指纹必须让旧行重跑（重算成本，已知且必要）")

    def test_ruleset_digest_is_content_hash_of_rule_source(self) -> None:
        """规则摘要必须是规则源文件的内容哈希（非密钥、可复算、不得为空）。"""
        from scripts.engine import index_db

        digest = getattr(index_db, "RULESET_VERSION", "")
        self.assertTrue(digest, "规则摘要不得为空（恒空即 R09-D07 缺口）")
        self.assertEqual(digest, file_hash(RULES_SOURCE), "规则摘要必须是 prompt_rules.py 的内容哈希")


class TestFingerprintIncludesChannel(unittest.TestCase):
    """R09-D05：池化多节点盲区——`current_model_summary()` 只认 SUMMARY_* 两个字段。

    有 `llm_nodes.json` 时真正执行的是节点自带的 name/base_url/model（`llm.py` 自己
    都按 `usage.get("model") or API_MODEL` 记账，承认二者可不等）。切换通道而模型名不变时，
    旧 done 段仍以「已完成（断点跳过）」复用。红线：节点的 `api_key` 绝不能进指纹。

    夹具全在临时目录 + 临时环境变量里，绝不读真实 `.asset_state.db` / 真实节点配置 / 真实凭据。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=_tmp_root())
        self.addCleanup(self._tmp.cleanup)
        self.lib = Path(self._tmp.name)
        self.seg = self.lib / "段01.md"
        self.seg.write_text("林姑娘走进茶楼", encoding="utf-8")
        self.out = self.lib / "候选表.md"
        self.out.write_text("产物", encoding="utf-8")
        # ASSET_ENV_PATH 的父目录就是池配置（llm_nodes.json）的查找起点
        self.env_file = self.lib / ".env"
        self.env_file.write_text("", encoding="utf-8")
        self.cfg = self.lib / "llm_nodes.json"

    def _write_nodes(self, *nodes: dict) -> None:
        self.cfg.write_text(json.dumps({"nodes": list(nodes)}, ensure_ascii=False), encoding="utf-8")

    def _node(self, name: str, base_url: str, api_key: str) -> dict:
        return {"name": name, "proto": "openai", "base_url": base_url,
                "model": "同一模型名", "api_key": api_key, "max_tokens": 8000}

    def _fp(self) -> str:
        return task_node_fingerprint(self.seg)

    def test_channel_switch_invalidates_the_checkpoint(self) -> None:
        self._write_nodes(self._node("通道甲", "http://127.0.0.1:9/v1", "密钥-甲"))
        with patch.dict(os.environ, {"ASSET_ENV_PATH": str(self.env_file),
                                     "SUMMARY_API_MODEL": "同一模型名"}):
            first = self._fp()
            self.assertEqual(self._fp(), first, "同一通道集合的指纹必须稳定")
            with IndexDB(self.lib) as db:
                db.mark_done(self.seg, self.out, seg_hash=None, node_fp=first)
                self.assertTrue(db.should_skip(self.seg, self.out, node_fp=first))
            # 换一条通道：模型名一字未改，只有 name/base_url 变了
            self._write_nodes(self._node("通道乙", "http://127.0.0.1:19/v1", "密钥-甲"))
            newer = self._fp()
            self.assertNotEqual(newer, first, "切换执行通道必须改变节点指纹（R09-D05）")
            with IndexDB(self.lib) as db:
                self.assertFalse(db.should_skip(self.seg, self.out, node_fp=newer),
                                 "换了通道后旧产物不得再判「已完成（断点跳过）」")

    def test_node_api_key_change_does_not_change_the_fingerprint(self) -> None:
        """红线：改密钥不得改变指纹（否则等于把凭据拼进断点，且会白白重烧钱）。"""
        self._write_nodes(self._node("通道甲", "http://127.0.0.1:9/v1", "密钥-甲"))
        with patch.dict(os.environ, {"ASSET_ENV_PATH": str(self.env_file),
                                     "SUMMARY_API_MODEL": "同一模型名"}):
            first = self._fp()
            self._write_nodes(self._node("通道甲", "http://127.0.0.1:9/v1", "密钥-乙"))
            self.assertEqual(self._fp(), first, "节点密钥不得参与节点指纹")

    def test_model_summary_without_nodes_is_byte_identical_to_legacy(self) -> None:
        """无池配置时摘要必须与旧口径逐字节一致（新字段缺省不进 payload，免存量指纹额外失效）。"""
        from scripts.engine.artifact_graph import canonical_payload

        with patch.dict(os.environ, {"ASSET_ENV_PATH": str(self.env_file),
                                     "SUMMARY_API_MODEL": "model-a",
                                     "SUMMARY_MAX_TOKENS": "8000"}):
            self.assertEqual(current_model_summary(),
                             canonical_payload({"model": "model-a", "max_tokens": "8000"}),
                             "没有节点配置时摘要不得变化")


if __name__ == "__main__":
    unittest.main()
