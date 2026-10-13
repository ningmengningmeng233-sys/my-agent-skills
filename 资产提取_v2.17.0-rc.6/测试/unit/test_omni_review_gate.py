#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 10：Omni 参考链、主模型审核与返工闭环的**缺口**部分。

覆盖（只测本轮新增/接线项；16 项结构化硬门与三轮返工状态机在阶段 6 已由既有测试锁定）：
- 参考来源优先级：用户提供的主体参考 > 已审核通过的项目参考 > 同资产已通过视图 > 风格参考；
- 审核失败图**永不**进入参考链；未经审核的生成图也不得当参考；
- `AWAITING_VISUAL_REVIEW`：当前环境不能看图时的状态，且**不得签发**。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="module")
def schema():
    from scripts.engine import visual_schema as module

    return module


@pytest.fixture(scope="module")
def router():
    from scripts.engine import reference_router as module

    return module


# --- 参考来源优先级与资格 ------------------------------------------------------

def test_reference_origin_priority_is_frozen(router):
    assert router.ORIGIN_PRIORITY == (router.ORIGIN_USER, router.ORIGIN_REVIEWED_PROJECT,
                                      router.ORIGIN_APPROVED_VIEW, router.ORIGIN_STYLE)
    ranks = [router.origin_rank(o) for o in router.ORIGIN_PRIORITY]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)


def test_user_supplied_reference_is_always_eligible(router):
    ok, reason = router.is_reference_eligible(origin=router.ORIGIN_USER)
    assert ok is True and reason == ""


def test_style_reference_is_always_eligible(router):
    ok, _ = router.is_reference_eligible(origin=router.ORIGIN_STYLE)
    assert ok is True


@pytest.mark.parametrize("origin_attr", ["ORIGIN_REVIEWED_PROJECT", "ORIGIN_APPROVED_VIEW"])
def test_reviewed_origins_require_a_passing_decision(router, origin_attr):
    origin = getattr(router, origin_attr)
    assert router.is_reference_eligible(origin=origin, review_decision="ACCEPTED")[0] is True
    assert router.is_reference_eligible(origin=origin, review_decision="PASS_WITH_NOTES")[0] is True


@pytest.mark.parametrize("decision", ["NEEDS_REVISION", "NEEDS_HUMAN_DECISION", "QC_UNTRUSTWORTHY"])
def test_failed_review_image_never_enters_the_reference_chain(router, decision):
    for origin in (router.ORIGIN_REVIEWED_PROJECT, router.ORIGIN_APPROVED_VIEW):
        ok, reason = router.is_reference_eligible(origin=origin, review_decision=decision)
        assert ok is False
        assert reason == router.REFERENCE_FROM_FAILED_REVIEW


def test_unreviewed_generated_image_cannot_be_used_as_reference(router):
    ok, reason = router.is_reference_eligible(origin=router.ORIGIN_APPROVED_VIEW)
    assert ok is False
    assert reason == router.REFERENCE_UNREVIEWED


def test_references_are_sorted_by_origin_before_role(router):
    entries = [
        {"origin": router.ORIGIN_STYLE, "role": "identity"},
        {"origin": router.ORIGIN_APPROVED_VIEW, "role": "structure", "review_decision": "ACCEPTED"},
        {"origin": router.ORIGIN_USER, "role": "structure"},
        {"origin": router.ORIGIN_REVIEWED_PROJECT, "role": "material", "review_decision": "ACCEPTED"},
    ]
    ordered = router.select_references(entries)
    assert [e["origin"] for e in ordered] == [
        router.ORIGIN_USER, router.ORIGIN_REVIEWED_PROJECT,
        router.ORIGIN_APPROVED_VIEW, router.ORIGIN_STYLE]


def test_select_references_drops_failed_and_unreviewed_entries(router):
    entries = [
        {"origin": router.ORIGIN_APPROVED_VIEW, "role": "identity", "review_decision": "NEEDS_REVISION"},
        {"origin": router.ORIGIN_APPROVED_VIEW, "role": "identity"},
        {"origin": router.ORIGIN_REVIEWED_PROJECT, "role": "style", "review_decision": "ACCEPTED"},
    ]
    ordered = router.select_references(entries)
    assert [e["role"] for e in ordered] == ["style"]


# --- AWAITING_VISUAL_REVIEW：不能看图时不得签发 -------------------------------

def test_awaiting_visual_review_status_exists_with_label(schema):
    assert "AWAITING_VISUAL_REVIEW" in schema.VisualTaskStatus.valid_values()
    assert schema.USER_STATUS_LABELS["AWAITING_VISUAL_REVIEW"]
    assert "AWAITING_VISUAL_REVIEW" in schema.PRODUCED_STATUSES


def test_awaiting_status_is_not_publishable(schema):
    assert "AWAITING_VISUAL_REVIEW" not in schema.PUBLISHABLE_STATUSES


def test_review_provider_reports_host_cannot_read_images():
    from scripts.engine.review_provider import REVIEW_HOST_CANNOT_READ_IMAGES

    assert REVIEW_HOST_CANNOT_READ_IMAGES


def test_publisher_refuses_to_sign_when_awaiting_visual_review(schema):
    """生成端点不得自签：停在 AWAITING_VISUAL_REVIEW 的任务一律拒签。"""
    from scripts.engine.visual_publisher import (
        PUBLISH_AWAITING_VISUAL_REVIEW,
        PublishError,
        ensure_publishable_status,
    )

    class _Task:
        status = "AWAITING_VISUAL_REVIEW"

    with pytest.raises(PublishError) as ctx:
        ensure_publishable_status(_Task())
    assert ctx.value.code == PUBLISH_AWAITING_VISUAL_REVIEW

    class _Accepted:
        status = schema.VisualTaskStatus.ACCEPTED

    ensure_publishable_status(_Accepted())     # 已通过的状态不得被这条守卫误伤
