#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 11：性能基准报告的门禁语义。

覆盖：
- 报告区分本地耗时、排队耗时与模型耗时；
- 速度门与质量门**互相独立**，任何一门失败都不得晋级；
- 任一质量指标低于 v2.16 基线即失败，**即便速度目标全部达成**；
- 缺测项一律记 unknown 并且**不算通过**（禁止伪报）。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "测试"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture(scope="module")
def bench():
    import run_asset_compiler_benchmark as module

    return module


def _metrics(*, text_calls=100, endpoint_ms=1000, local_ms=1000, queue_ms=0,
             unrelated=0, total_rerun=10, quality=None):
    return {
        "text_calls": text_calls,
        "timing": {"endpoint_ms": endpoint_ms, "local_ms": local_ms, "queue_ms": queue_ms},
        "effective_ratio": 0.9,
        "cache_hits": 0,
        "rerun_tasks": {"total": total_rerun, "unrelated": unrelated},
        "coverage": {"person": 1, "scene": 1, "prop": 1, "costume": 0},
        "quality": quality or {"fact_coverage": 1.0, "evidence_complete": 1.0,
                               "relations_valid": 1.0, "style_conflicts": 0,
                               "prompt_complete": 1.0, "review_contract": 1.0},
    }


# --- 耗时分项 -----------------------------------------------------------------

def test_report_separates_local_queue_and_endpoint_time(bench):
    report = bench.build_report(_metrics(), _metrics(text_calls=10, endpoint_ms=100, local_ms=50))
    timing = report["timing"]
    for chain in ("old", "new"):
        assert set(timing[chain]) >= {"endpoint_ms", "local_ms", "queue_ms"}
    assert timing["old"]["endpoint_ms"] == 1000
    assert timing["new"]["local_ms"] == 50


# --- 速度门 -------------------------------------------------------------------

def test_speed_targets_are_the_frozen_numbers(bench):
    assert bench.SPEED_TARGETS["text_calls_reduction"] == 0.60
    assert bench.SPEED_TARGETS["text_wall_reduction"] == 0.50
    assert bench.SPEED_TARGETS["unrelated_rerun_reduction"] == 0.80


def test_speed_gate_passes_when_reductions_meet_targets(bench):
    old = _metrics(text_calls=100, endpoint_ms=1000, local_ms=1000, total_rerun=10, unrelated=0)
    new = _metrics(text_calls=40, endpoint_ms=500, local_ms=500, total_rerun=10, unrelated=0)
    report = bench.build_report(old, new)
    assert report["speed"]["text_calls_reduction"]["passed"] is True
    assert report["speed"]["text_wall_reduction"]["passed"] is True
    assert report["promoted"] is True


def test_unrelated_rerun_gate_uses_the_rerun_counts(bench):
    old = _metrics(total_rerun=10, unrelated=10)
    new = _metrics(total_rerun=10, unrelated=1)
    report = bench.build_report(old, new)
    assert report["speed"]["unrelated_rerun_reduction"]["passed"] is True
    worse = bench.build_report(old, _metrics(total_rerun=10, unrelated=5))
    assert worse["speed"]["unrelated_rerun_reduction"]["passed"] is False


# --- 质量门独立 ---------------------------------------------------------------

def test_quality_below_baseline_fails_even_when_speed_targets_pass(bench):
    old = _metrics(text_calls=100, endpoint_ms=1000, local_ms=1000)
    degraded = {"fact_coverage": 1.0, "evidence_complete": 0.5, "relations_valid": 1.0,
                "style_conflicts": 0, "prompt_complete": 1.0, "review_contract": 1.0}
    new = _metrics(text_calls=10, endpoint_ms=100, local_ms=100, quality=degraded)
    report = bench.build_report(old, new)
    assert all(gate["passed"] for gate in report["speed"].values()), "速度门应当全过"
    assert report["quality"]["evidence_complete"]["passed"] is False
    assert report["promoted"] is False, "质量不达标不得晋级"


def test_more_style_conflicts_than_baseline_fails(bench):
    old = _metrics(quality={"fact_coverage": 1.0, "evidence_complete": 1.0, "relations_valid": 1.0,
                            "style_conflicts": 0, "prompt_complete": 1.0, "review_contract": 1.0})
    new = _metrics(quality={"fact_coverage": 1.0, "evidence_complete": 1.0, "relations_valid": 1.0,
                            "style_conflicts": 2, "prompt_complete": 1.0, "review_contract": 1.0})
    report = bench.build_report(old, new)
    assert report["quality"]["style_conflicts"]["passed"] is False
    assert report["promoted"] is False


def test_shorter_output_is_not_automatically_better(bench):
    """「更短」不等于「更好」：提示词完整度下降必须被质量门抓住。"""
    old = _metrics(quality={"fact_coverage": 1.0, "evidence_complete": 1.0, "relations_valid": 1.0,
                            "style_conflicts": 0, "prompt_complete": 1.0, "review_contract": 1.0})
    new = _metrics(text_calls=10, endpoint_ms=10, local_ms=10,
                   quality={"fact_coverage": 1.0, "evidence_complete": 1.0, "relations_valid": 1.0,
                            "style_conflicts": 0, "prompt_complete": 0.4, "review_contract": 1.0})
    report = bench.build_report(old, new)
    assert report["quality"]["prompt_complete"]["passed"] is False
    assert report["promoted"] is False


# --- 禁止伪报 -----------------------------------------------------------------

def test_missing_measurements_are_unknown_and_never_pass(bench):
    old = _metrics()
    new = _metrics()
    new["text_calls"] = None
    new["timing"]["endpoint_ms"] = None
    report = bench.build_report(old, new)
    assert report["speed"]["text_calls_reduction"]["value"] == "unknown"
    assert report["speed"]["text_calls_reduction"]["passed"] is False
    assert report["promoted"] is False
    assert "unknown" in report["not_run"] or report["speed"]["text_calls_reduction"]["value"] == "unknown"


def test_report_never_claims_success_without_both_gates(bench):
    old = _metrics()
    new = _metrics(text_calls=None)
    report = bench.build_report(old, new)
    assert report["promoted"] is False
    assert report["verdict"] in ("FAILED", "NOT_RUN")
