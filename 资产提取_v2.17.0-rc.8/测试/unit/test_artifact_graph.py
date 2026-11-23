#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 8：依赖感知的工件依赖图与失效传播。

覆盖：
- 六类节点（源事实/总账项/美术总则/提示词/图片任务/审核结果）与有向依赖边；
- 节点指纹由规范化输入 + 规则版本 + 模板版本 + 模型配置摘要组成，且**不含密钥**；
- 单资产变更只牵连该资产及其下游；美术总则变更牵连全部视觉提示词与图像；审核意见只牵连相关返工；
- 无关任务复跑比例 ≤ 20%（完成条件）；
- 落盘/载入往返稳定。

模块按 pytest fixture 惰性导入：红测阶段模块尚未实现时只让本文件红，不中断整个回归。
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="module")
def ag():
    from scripts.engine import artifact_graph as module

    return module


def _build_library_graph(ag):
    """两个资产的完整链：源事实→总账项→提示词→图片任务→审核结果，外加共享的美术总则。"""
    g = ag.ArtifactGraph()
    g.set_node(ag.KIND_ART_CONSTITUTION, "项目", "sha256:const")
    for asset in ("甲", "乙"):
        g.set_node(ag.KIND_SOURCE_FACT, asset, f"sha256:fact-{asset}")
        g.set_node(ag.KIND_LEDGER_ITEM, asset, f"sha256:ledger-{asset}")
        g.set_node(ag.KIND_PROMPT, asset, f"sha256:prompt-{asset}")
        g.set_node(ag.KIND_IMAGE_TASK, asset, f"sha256:task-{asset}")
        g.set_node(ag.KIND_REVIEW, asset, f"sha256:review-{asset}")
        g.add_edge(ag.KIND_SOURCE_FACT, asset, ag.KIND_LEDGER_ITEM, asset)
        g.add_edge(ag.KIND_LEDGER_ITEM, asset, ag.KIND_PROMPT, asset)
        g.add_edge(ag.KIND_PROMPT, asset, ag.KIND_IMAGE_TASK, asset)
        g.add_edge(ag.KIND_IMAGE_TASK, asset, ag.KIND_REVIEW, asset)
        g.add_edge(ag.KIND_ART_CONSTITUTION, "项目", ag.KIND_PROMPT, asset)
    return g


# --- 节点与边 -----------------------------------------------------------------

def test_six_node_kinds_are_fixed(ag):
    assert ag.NODE_KINDS == (ag.KIND_SOURCE_FACT, ag.KIND_LEDGER_ITEM, ag.KIND_ART_CONSTITUTION,
                             ag.KIND_PROMPT, ag.KIND_IMAGE_TASK, ag.KIND_REVIEW)


def test_node_id_namespaces_by_kind(ag):
    assert ag.node_id(ag.KIND_PROMPT, "甲") == "prompt:甲"
    assert ag.node_id(ag.KIND_IMAGE_TASK, "甲") != ag.node_id(ag.KIND_PROMPT, "甲")


def test_graph_records_nodes_and_edges(ag):
    g = _build_library_graph(ag)
    assert g.fingerprint(ag.KIND_SOURCE_FACT, "甲") == "sha256:fact-甲"
    assert ag.node_id(ag.KIND_LEDGER_ITEM, "甲") in g.dependencies(ag.KIND_PROMPT, "甲")


# --- 指纹：组成与不含密钥 ------------------------------------------------------

def test_fingerprint_is_stable_and_input_sensitive(ag):
    a = ag.node_fingerprint({"text": "甲"}, rule_version="r1", template_version="t1", model_summary="m1")
    b = ag.node_fingerprint({"text": "甲"}, rule_version="r1", template_version="t1", model_summary="m1")
    c = ag.node_fingerprint({"text": "乙"}, rule_version="r1", template_version="t1", model_summary="m1")
    assert a == b and a != c
    assert a.startswith("sha256:")


def test_fingerprint_changes_with_rule_template_and_model_versions(ag):
    base = ag.node_fingerprint({"text": "甲"}, rule_version="r1", template_version="t1", model_summary="m1")
    assert ag.node_fingerprint({"text": "甲"}, rule_version="r2",
                               template_version="t1", model_summary="m1") != base
    assert ag.node_fingerprint({"text": "甲"}, rule_version="r1",
                               template_version="t2", model_summary="m1") != base
    assert ag.node_fingerprint({"text": "甲"}, rule_version="r1",
                               template_version="t1", model_summary="m2") != base


def test_fingerprint_never_contains_secrets(ag):
    with_secret = ag.node_fingerprint({"text": "甲", "api_key": "sk-live-1234567890",
                                       "Authorization": "Bearer abcdef"})
    without = ag.node_fingerprint({"text": "甲"})
    assert with_secret == without, "疑似密钥字段必须被剔除，指纹不得随密钥变化"


def test_canonical_payload_is_sorted_and_secret_free(ag):
    text = ag.canonical_payload({"b": "2", "a": "1", "token": "xyz"})
    assert text.index('"a"') < text.index('"b"')
    assert "xyz" not in text and "token" not in text


# --- 失效传播 -----------------------------------------------------------------

def test_changing_one_source_fact_only_hits_that_asset_chain(ag):
    g = _build_library_graph(ag)
    hit = g.invalidated_by(ag.KIND_SOURCE_FACT, "甲")
    assert ag.node_id(ag.KIND_LEDGER_ITEM, "甲") in hit
    assert ag.node_id(ag.KIND_PROMPT, "甲") in hit
    assert ag.node_id(ag.KIND_IMAGE_TASK, "甲") in hit
    assert ag.node_id(ag.KIND_REVIEW, "甲") in hit
    assert not any(n.endswith(":乙") for n in hit), hit


def test_changing_art_constitution_invalidates_all_visual_outputs(ag):
    g = _build_library_graph(ag)
    hit = g.invalidated_by(ag.KIND_ART_CONSTITUTION, "项目")
    for asset in ("甲", "乙"):
        assert ag.node_id(ag.KIND_PROMPT, asset) in hit
        assert ag.node_id(ag.KIND_IMAGE_TASK, asset) in hit


def test_changing_review_only_hits_its_own_rework_task(ag):
    g = _build_library_graph(ag)
    g.add_edge(ag.KIND_REVIEW, "甲", ag.KIND_IMAGE_TASK, "甲")   # 返工：审核意见回指该任务
    hit = g.invalidated_by(ag.KIND_REVIEW, "甲")
    assert ag.node_id(ag.KIND_IMAGE_TASK, "甲") in hit
    assert ag.node_id(ag.KIND_IMAGE_TASK, "乙") not in hit
    assert ag.node_id(ag.KIND_SOURCE_FACT, "乙") not in hit


def test_unrelated_rerun_ratio_is_within_budget(ag):
    g = _build_library_graph(ag)
    tasks = g.nodes_of_kind(ag.KIND_IMAGE_TASK)
    assert len(tasks) == 2
    hit = g.invalidated_tasks(ag.KIND_SOURCE_FACT, "甲")
    unrelated = hit - {ag.node_id(ag.KIND_IMAGE_TASK, "甲")}
    ratio = len(unrelated) / len(tasks)
    assert ratio <= 0.2, f"无关任务复跑比例 {ratio}"
    assert ratio == 0.0


def test_invalidation_is_transitive_through_multiple_hops(ag):
    g = ag.ArtifactGraph()
    for kind, key in ((ag.KIND_SOURCE_FACT, "a"), (ag.KIND_LEDGER_ITEM, "a"),
                      (ag.KIND_PROMPT, "a"), (ag.KIND_IMAGE_TASK, "a")):
        g.set_node(kind, key, "sha256:x")
    g.add_edge(ag.KIND_SOURCE_FACT, "a", ag.KIND_LEDGER_ITEM, "a")
    g.add_edge(ag.KIND_LEDGER_ITEM, "a", ag.KIND_PROMPT, "a")
    g.add_edge(ag.KIND_PROMPT, "a", ag.KIND_IMAGE_TASK, "a")
    hit = g.invalidated_by(ag.KIND_SOURCE_FACT, "a")
    assert ag.node_id(ag.KIND_IMAGE_TASK, "a") in hit and len(hit) == 4


def test_cycle_does_not_hang_invalidation(ag):
    """审核返工可能造成环；失效传播必须终止而不是无限递归。"""
    g = ag.ArtifactGraph()
    g.set_node(ag.KIND_REVIEW, "a", "sha256:r")
    g.set_node(ag.KIND_IMAGE_TASK, "a", "sha256:t")
    g.add_edge(ag.KIND_REVIEW, "a", ag.KIND_IMAGE_TASK, "a")
    g.add_edge(ag.KIND_IMAGE_TASK, "a", ag.KIND_REVIEW, "a")
    assert g.invalidated_by(ag.KIND_REVIEW, "a") == {
        ag.node_id(ag.KIND_REVIEW, "a"), ag.node_id(ag.KIND_IMAGE_TASK, "a")}


# --- 落盘往返 -----------------------------------------------------------------

def test_save_and_load_round_trip(ag, tmp_path):
    g = _build_library_graph(ag)
    path = g.save(tmp_path)
    assert Path(path).is_file()
    reloaded = ag.ArtifactGraph.load(tmp_path)
    assert reloaded.fingerprint(ag.KIND_PROMPT, "甲") == g.fingerprint(ag.KIND_PROMPT, "甲")
    assert reloaded.invalidated_by(ag.KIND_SOURCE_FACT, "甲") == g.invalidated_by(ag.KIND_SOURCE_FACT, "甲")
    assert json.loads(Path(path).read_text(encoding="utf-8"))["schema_version"] >= 1


def test_load_missing_graph_returns_empty_graph(ag, tmp_path):
    g = ag.ArtifactGraph.load(tmp_path)
    assert g.nodes() == ()
    assert g.invalidated_by(ag.KIND_PROMPT, "不存在") == set()
