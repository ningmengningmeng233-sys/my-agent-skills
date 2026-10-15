#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 6 工单 6.3：可选双审核子代理的证据合并（规格九 / 十）。

- 子代理**默认关闭**；开启后美术代理只提供画风/身份/服装/场景与跨图连续性证据，
  技术代理只提供手部/比例/接触/构图/道具数与伪影证据。
- 二者**没有签发权**：合并产物只是给当前主模型看证据的草稿，不得带判定、不得发布；
  主模型必须重新看图并对冲突证据做终裁。
- 不得把 Gemini 当子审核代理。
"""
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.image_qc import ISSUE_CODES, QcIssue  # noqa: E402
from scripts.engine.project_settings import (  # noqa: E402
    ProjectSettings,
    write_project_settings,
)
from scripts.engine.review_merge import (  # noqa: E402
    ART_CODES,
    SUBAGENT_MERGE_NAME,
    TECH_CODES,
    MergeError,
    SubagentReport,
    load_subagent_report,
    merge_for_library,
    merge_subagent_reports,
)

TASK_ID = "CH_林姑娘-正脸-v01"
IMAGE_SHA = "c" * 64
ART = "美术"
TECH = "技术"


def _issue(code, severity="blocking", evidence="可见证据"):
    return {"code": code, "severity": severity, "evidence": f"{code}：{evidence}",
            "repair_instruction": f"按 {code} 定向修复"}


def _report_dict(role, issues=(), **kw):
    payload = {"role": role, "checker": f"子代理-{role}", "task_id": TASK_ID,
               "image_sha256": IMAGE_SHA, "checked_at": "2026-09-18T00:00:00Z",
               "notes": "", "issues": [dict(row) for row in issues]}
    payload.update(kw)
    return payload


def _report(role, issues=(), **kw):
    return load_subagent_report(_report_dict(role, issues, **kw), expected_role=role)


# --- 域划分 ---------------------------------------------------------------------

def test_art_and_tech_domains_partition_every_issue_code():
    assert not (ART_CODES & TECH_CODES), "两个角色的证据域不得重叠"
    assert ART_CODES | TECH_CODES == set(ISSUE_CODES), (
        "每个问题码必须恰好归属一个角色，否则会出现无人负责或重复负责")


def test_domains_match_the_spec_families():
    for code in ("STYLE_MISMATCH", "MATERIAL_MISMATCH", "IDENTITY_DRIFT",
                 "COSTUME_INCOMPLETE", "COSTUME_FRONT_BACK_MISMATCH",
                 "FRONT_BACK_INCONSISTENT", "SPACE_ANCHOR_MISSING",
                 "SCENE_STRUCTURE_MISMATCH", "REFERENCE_CHAIN_INVALID"):
        assert code in ART_CODES, f"{code} 属美术代理证据域"
    for code in ("HAND_FINGER_DEFECT", "BODY_PROPORTION_DEFECT", "CROP_MISMATCH",
                 "POSITION_MISMATCH", "VIEWPOINT_MISMATCH", "EYELINE_MISMATCH",
                 "AXIS_CROSSING", "OCCLUSION_MISMATCH", "PROP_COUNT_MISMATCH",
                 "PROP_STATE_MISMATCH", "EXTRA_SUBJECT", "TEXT_OR_WATERMARK"):
        assert code in TECH_CODES, f"{code} 属技术代理证据域"


# --- 报告契约 -------------------------------------------------------------------

def test_art_report_cannot_carry_technical_codes():
    with pytest.raises(MergeError) as excinfo:
        _report(ART, [_issue("HAND_FINGER_DEFECT")])
    assert excinfo.value.code == "MERGE_DOMAIN_VIOLATION"


def test_tech_report_cannot_carry_art_codes():
    with pytest.raises(MergeError) as excinfo:
        _report(TECH, [_issue("IDENTITY_DRIFT")])
    assert excinfo.value.code == "MERGE_DOMAIN_VIOLATION"


def test_subagent_report_must_not_carry_a_decision():
    """子代理没有签发权：报告里出现 decision 一律拒绝。"""
    with pytest.raises(MergeError) as excinfo:
        load_subagent_report(_report_dict(ART, decision="ACCEPTED"), expected_role=ART)
    assert excinfo.value.code == "MERGE_DECISION_FORBIDDEN"


def test_gemini_cannot_be_a_subagent():
    with pytest.raises(MergeError) as excinfo:
        load_subagent_report(_report_dict(ART, checker="Gemini 图片质检"),
                             expected_role=ART)
    assert excinfo.value.code == "MERGE_SUBAGENT_GEMINI_FORBIDDEN"


def test_report_with_wrong_role_is_rejected():
    with pytest.raises(MergeError) as excinfo:
        load_subagent_report(_report_dict(TECH), expected_role=ART)
    assert excinfo.value.code == "MERGE_ROLE_MISMATCH"


def test_report_with_unknown_code_is_rejected():
    with pytest.raises(MergeError) as excinfo:
        _report(ART, [_issue("NOT_A_REAL_CODE")])
    assert excinfo.value.code == "MERGE_CODE_UNKNOWN"


# --- 合并产物 -------------------------------------------------------------------

def test_complementary_reports_union_their_evidence():
    art = _report(ART, [_issue("STYLE_MISMATCH")])
    tech = _report(TECH, [_issue("HAND_FINGER_DEFECT")])
    draft = merge_subagent_reports(art, tech)
    assert {i.code for i in draft.issues} == {"STYLE_MISMATCH", "HAND_FINGER_DEFECT"}
    assert draft.conflicts == ()
    assert draft.requires_main_model is True, "最终判定只能由当前主模型给出"


def test_draft_has_no_signing_authority():
    draft = merge_subagent_reports(_report(ART, [_issue("STYLE_MISMATCH")]),
                                   _report(TECH))
    payload = draft.to_dict()
    assert "decision" not in payload, "合并产物不得携带判定"
    assert payload["requires_main_model"] is True
    assert payload["subagent_count"] == 2


def test_same_code_with_different_severity_is_flagged_for_the_main_model():
    """防御性检查：调用方绕过加载器直接构造报告时，同码不同严重度必须暴露为冲突。

    走 `load_subagent_report` 的报告受域隔离约束（同码不可能跨角色出现），
    这里覆盖的是库调用方直接构造 `SubagentReport` 的路径：不得取其一，必须交给主模型裁决。
    """
    art = SubagentReport(role=ART, checker="子代理-美术", task_id=TASK_ID,
                         image_sha256=IMAGE_SHA,
                         issues=(QcIssue(code="STYLE_MISMATCH", severity="minor",
                                         evidence="美术代理认为轻微",
                                         repair_instruction="微调"),))
    tech = SubagentReport(role=TECH, checker="子代理-技术", task_id=TASK_ID,
                          image_sha256=IMAGE_SHA,
                          issues=(QcIssue(code="STYLE_MISMATCH", severity="blocking",
                                          evidence="技术代理认为严重",
                                          repair_instruction="重做"),))
    draft = merge_subagent_reports(art, tech)
    assert draft.conflicts, "同码不同严重度必须暴露为冲突"
    assert draft.conflicts[0].code == "STYLE_MISMATCH"
    assert "主模型" in draft.conflicts[0].resolution_hint, "冲突只能由主模型裁决"
    assert draft.requires_main_model is True
    assert {i.code for i in draft.issues} == {"STYLE_MISMATCH"}, "证据只保留一条"


def test_identical_findings_are_deduplicated():
    """防御性：两份报告指向同一事实时只留一条，并标记为两代理一致。"""
    def _one(role):
        return SubagentReport(
            role=role, checker=f"子代理-{role}", task_id=TASK_ID,
            image_sha256=IMAGE_SHA,
            issues=(QcIssue(code="STYLE_MISMATCH", severity="blocking",
                            evidence="同样观察", repair_instruction="同样的修法"),))

    draft = merge_subagent_reports(_one(ART), _one(TECH))
    assert len(draft.issues) == 1
    assert draft.agreed_codes == ("STYLE_MISMATCH",)
    assert draft.conflicts == ()


def test_task_and_image_mismatch_between_reports_is_rejected():
    art = _report(ART)
    other_task = replace(_report(TECH), task_id="CH_掌柜-正脸-v01")
    with pytest.raises(MergeError) as excinfo:
        merge_subagent_reports(art, other_task)
    assert excinfo.value.code == "MERGE_TASK_MISMATCH"
    other_image = replace(_report(TECH), image_sha256="d" * 64)
    with pytest.raises(MergeError) as excinfo:
        merge_subagent_reports(art, other_image)
    assert excinfo.value.code == "MERGE_IMAGE_MISMATCH"


def test_draft_serialization_is_deterministic():
    art = _report(ART, [_issue("IDENTITY_DRIFT"), _issue("SPACE_ANCHOR_MISSING")])
    tech = _report(TECH, [_issue("CROP_MISMATCH")])
    first = merge_subagent_reports(art, tech).to_dict()
    second = merge_subagent_reports(art, tech).to_dict()
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == \
        json.dumps(second, ensure_ascii=False, sort_keys=True)
    codes = [row["code"] for row in first["issues"]]
    assert codes == sorted(codes), "证据必须按码排序，保证产物稳定"


# --- 默认关闭与写盘 -------------------------------------------------------------

def _lib(tmp_path):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    return lib


def _paths(tmp_path, art_issues=(), tech_issues=()):
    art_path = tmp_path / "art.json"
    tech_path = tmp_path / "tech.json"
    art_path.write_text(json.dumps(_report_dict(ART, art_issues), ensure_ascii=False),
                        encoding="utf-8")
    tech_path.write_text(json.dumps(_report_dict(TECH, tech_issues), ensure_ascii=False),
                         encoding="utf-8")
    return art_path, tech_path


def test_subagents_are_disabled_by_default(tmp_path):
    lib = _lib(tmp_path)
    art_path, tech_path = _paths(tmp_path, [_issue("STYLE_MISMATCH")])
    with pytest.raises(MergeError) as excinfo:
        merge_for_library(lib, TASK_ID, art_path, tech_path)
    assert excinfo.value.code == "MERGE_SUBAGENTS_DISABLED"


def test_enabled_subagents_write_a_merge_draft_for_the_main_model(tmp_path):
    lib = _lib(tmp_path)
    write_project_settings(lib, ProjectSettings(review_subagents=True))
    art_path, tech_path = _paths(tmp_path, [_issue("STYLE_MISMATCH")],
                                 [_issue("HAND_FINGER_DEFECT")])
    draft = merge_for_library(lib, TASK_ID, art_path, tech_path)
    target = lib / "视觉资产" / "审核请求" / TASK_ID / SUBAGENT_MERGE_NAME
    assert target.is_file(), "合并草稿必须落在审核请求目录里供主模型读取"
    stored = json.loads(target.read_text(encoding="utf-8"))
    assert stored == draft.to_dict()
    assert stored["requires_main_model"] is True
    assert "decision" not in stored


def test_merge_draft_file_is_byte_stable(tmp_path):
    lib = _lib(tmp_path)
    write_project_settings(lib, ProjectSettings(review_subagents=True))
    art_path, tech_path = _paths(tmp_path, [_issue("STYLE_MISMATCH")])
    merge_for_library(lib, TASK_ID, art_path, tech_path)
    target = lib / "视觉资产" / "审核请求" / TASK_ID / SUBAGENT_MERGE_NAME
    first = target.read_bytes()
    merge_for_library(lib, TASK_ID, art_path, tech_path)
    assert target.read_bytes() == first


# --- 契约文档 -------------------------------------------------------------------

@pytest.mark.parametrize("name", ("审核子代理-美术.md", "审核子代理-技术.md"))
def test_subagent_contract_docs_exist_and_forbid_signing(name):
    path = REPO_ROOT / "references" / name
    assert path.is_file(), f"缺少子代理契约文档：{path}"
    text = path.read_text(encoding="utf-8")
    assert "默认关闭" in text
    assert "签发" in text, "必须写明子代理没有签发权"
    assert "Gemini" in text, "必须写明不得用 Gemini 当子审核代理"
    assert "主模型" in text, "必须写明最终判定由当前主模型给出"
