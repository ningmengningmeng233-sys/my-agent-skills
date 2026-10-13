#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 12：旧库迁移、回滚与影子运行。

覆盖：
- `scan` **只读**：旧交付文件哈希完全不变（完成条件）；
- `apply` 前自动备份、写新总账但**不覆盖旧 Markdown**，并产对照文件；
- 保留 stable key、旧 ID、人工场景母版字段、人工关系与已通过的视觉审核；
- 无证据资产**不得伪造摘句**：标为 legacy proposal 并阻断正式签发；
- `rollback` 恢复并逐文件核对哈希；
- 迁移 → 回滚 → 再迁移**幂等**（完成条件）；
- 影子模式只跑副本并输出差集与预计节省，**不覆盖原库**。
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="module")
def mig():
    from scripts.engine import ledger_migration as module

    return module


def _library(tmp_path: Path) -> Path:
    """一个最小旧库：索引 + 四表 + 基础提示词 + 人工场景母版 + 已通过审核。"""
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    (lib / "提示词" / "基础").mkdir(parents=True)
    (lib / "连续性").mkdir(parents=True)
    index = {"schema_version": 1, "source_fingerprint": "sha256:x", "assets": [
        {"asset_id": "CH_林姑娘", "stable_key": "CHR:00012:1", "type": "人物", "name": "林姑娘",
         "priority": "P0", "fact_level": "剧本事实", "status": "CONFIRMED", "aliases": [],
         "evidence": [{"line": "00012", "quote": "林姑娘走进茶楼，青衫束发"}]},
        {"asset_id": "PR_无证据物", "stable_key": "PRP:00099:1", "type": "道具", "name": "无证据物",
         "priority": "P2", "fact_level": "强推断", "status": "CONFIRMED", "aliases": [],
         "evidence": []},
    ]}
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    four = lib / "反推_最终.md"
    four.write_text("# 反推\n\n## 人物资产提取表\n| 资产ID | 人物姓名 | 别名/称呼 | 出现位置 | 重要等级 | 备注 |\n"
                    "|---|---|---|---|---|---|\n| CH_林姑娘 | 林姑娘 |  | 第1章 | S |  |\n",
                    encoding="utf-8")
    (lib / "提示词" / "基础" / "基础提示词.md").write_text("# 基础\n## CH_林姑娘\n正文\n", encoding="utf-8")
    (lib / "连续性" / "场景母版.md").write_text(
        "# 场景空间母版\n\n## SC_茶楼 茶楼\n| 字段 | 内容 |\n|---|---|\n| 固定锚点 | 人工补的柜台 |\n",
        encoding="utf-8")
    (lib / "视觉资产").mkdir()
    (lib / "视觉资产" / "正式版本.json").write_text(
        json.dumps({"schema_version": 1, "releases": [
            {"task_id": "CH_林姑娘-正脸", "decision": "ACCEPTED", "image_sha256": "abc"}]},
            ensure_ascii=False), encoding="utf-8")
    return lib


# --- scan 只读 -----------------------------------------------------------------

def test_scan_is_read_only(mig, tmp_path):
    lib = _library(tmp_path)
    before = mig.library_hashes(lib)
    report = mig.scan(lib)
    assert mig.library_hashes(lib) == before, "scan 阶段旧交付文件哈希必须完全不变"
    assert report["wrote"] == []
    assert report["proposal"]["total"] == 2


def test_scan_reports_evidence_less_assets_as_legacy_proposals(mig, tmp_path):
    lib = _library(tmp_path)
    report = mig.scan(lib)
    ids = [p["asset_id"] for p in report["ambiguities"]]
    assert ids == ["PR_无证据物"]
    entry = report["ambiguities"][0]
    assert entry["reason"] == mig.LEGACY_PROPOSAL
    # 无证据不得伪造摘句
    assert not entry.get("quote")


# --- apply --------------------------------------------------------------------

def test_apply_backs_up_writes_ledger_and_keeps_legacy_markdown(mig, tmp_path):
    lib = _library(tmp_path)
    four = lib / "反推_最终.md"
    before_four = four.read_bytes()
    prompts = lib / "提示词" / "基础" / "基础提示词.md"
    before_prompts = prompts.read_bytes()

    result = mig.apply(lib)
    assert Path(result["backup"]).is_file()
    assert (lib / ".process" / "asset-ledger.json").is_file()
    assert (lib / mig.COMPARE_NAME).is_file()
    # 不覆盖旧 Markdown
    assert four.read_bytes() == before_four
    assert prompts.read_bytes() == before_prompts


def test_apply_preserves_stable_keys_and_manual_master_fields(mig, tmp_path):
    lib = _library(tmp_path)
    mig.apply(lib)
    ledger = json.loads((lib / ".process" / "asset-ledger.json").read_text(encoding="utf-8"))
    keys = {e["asset_id"]: e["stable_key"] for e in ledger["entries"]}
    assert keys["CH_林姑娘"] == "CHR:00012:1"
    master = (lib / "连续性" / "场景母版.md").read_text(encoding="utf-8")
    assert "人工补的柜台" in master, "人工场景母版字段必须保留"


def test_apply_preserves_passed_visual_reviews(mig, tmp_path):
    lib = _library(tmp_path)
    mig.apply(lib)
    releases = json.loads((lib / "视觉资产" / "正式版本.json").read_text(encoding="utf-8"))
    assert releases["releases"][0]["decision"] == "ACCEPTED"


def test_apply_marks_evidence_less_assets_as_legacy_proposal_not_formal(mig, tmp_path):
    lib = _library(tmp_path)
    mig.apply(lib)
    ledger = json.loads((lib / ".process" / "asset-ledger.json").read_text(encoding="utf-8"))
    entry = next(e for e in ledger["entries"] if e["asset_id"] == "PR_无证据物")
    assert entry["migration_status"] == mig.LEGACY_PROPOSAL
    assert entry["evidence_ids"] == []
    # 待补证据的资产不得进入正式签发
    assert entry["ledger_status"] != "formal" or entry["blocking"]


# --- 幂等 ---------------------------------------------------------------------

def test_apply_twice_is_idempotent(mig, tmp_path):
    lib = _library(tmp_path)
    mig.apply(lib)
    first = (lib / ".process" / "asset-ledger.json").read_bytes()
    second = mig.apply(lib)
    assert second["idempotent"] is True
    assert (lib / ".process" / "asset-ledger.json").read_bytes() == first


# --- rollback ------------------------------------------------------------------

def test_rollback_restores_and_verifies_hashes(mig, tmp_path):
    lib = _library(tmp_path)
    before = mig.library_hashes(lib)
    mig.apply(lib)
    # 迁移后人为改动旧 Markdown，回滚必须把它恢复回来
    (lib / "反推_最终.md").write_text("被改坏了", encoding="utf-8")
    result = mig.rollback(lib)
    assert result["mismatch"] == []
    assert mig.library_hashes(lib) == before


def test_migrate_rollback_migrate_is_idempotent(mig, tmp_path):
    lib = _library(tmp_path)
    mig.apply(lib)
    first = (lib / ".process" / "asset-ledger.json").read_bytes()
    mig.rollback(lib)
    result = mig.apply(lib)
    assert result["idempotent"] is False          # 回滚后总账已不在，这次是真写
    assert (lib / ".process" / "asset-ledger.json").read_bytes() == first


# --- 影子模式 -----------------------------------------------------------------

def test_shadow_runs_on_a_copy_and_never_touches_the_original(mig, tmp_path):
    lib = _library(tmp_path)
    before = mig.library_hashes(lib)
    result = mig.shadow(lib, work_root=tmp_path / "影子")
    assert mig.library_hashes(lib) == before, "影子模式不得改动原库"
    for key in ("asset_diff", "prompt_diff", "task_diff", "expected_saving"):
        assert key in result, key
    assert result["expected_saving"]["old_text_calls"] >= result["expected_saving"]["new_text_calls"]


def test_shadow_reports_asset_diff_between_old_and_new(mig, tmp_path):
    lib = _library(tmp_path)
    result = mig.shadow(lib, work_root=tmp_path / "影子")
    assert "PR_无证据物" in result["asset_diff"]["only_new"] or \
           "PR_无证据物" in result["asset_diff"]["only_old"] or \
           result["asset_diff"]["common"]
