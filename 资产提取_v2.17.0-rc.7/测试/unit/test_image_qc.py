#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单03-Task5：结构化视觉质量检查（规格 8）。

问题码必须覆盖老罗失败反例：风格漂移、比例异常、断颈残留、背面残头、禁止拼版、
正背不一致、主体裁切。阻断级问题一律不得进入 ACCEPTED；
检查响应缺证据、引用错误任务或输入哈希不一致时返回 QC_UNTRUSTWORTHY。
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine import image_qc, visual_schema  # noqa: E402
from scripts.engine.image_qc import (  # noqa: E402
    BLOCKING,
    ISSUE_CODES,
    PROCESS_CODES,
    RUBRIC_DIR,
    QcIssue,
    QcResult,
    build_qc_request,
    load_rubric,
    parse_qc_response,
    qc_result_to_toml,
    rubric_for_kind,
    write_qc_result,
)
from scripts.engine.visual_schema import AttemptRecord, QcDecision, VisualTask  # noqa: E402

TASK_ID = "CH_林姑娘-服装独立背面-v01"


def _task(kind="服装独立背面", **kw):
    base = dict(task_id=f"CH_林姑娘-{kind}-v01", asset_id="CH_林姑娘", kind=kind,
                status="GENERATED", aspect_ratio="2:3", style_slug="仙侠国风电影CG",
                style_version="1.0.0", style_hash="a" * 64,
                fact_source=".process/资产索引.json#CH_林姑娘")
    base.update(kw)
    return VisualTask(**base)


def _attempt():
    return AttemptRecord(round=1, image="视觉资产/任务/x/attempt-01/image.png",
                         image_sha256="c" * 64)


def _response(**kw):
    base = {
        "task_id": TASK_ID,
        "decision": "NEEDS_REVISION",
        "checker": "gemini:qc-model",
        "checked_at": "2026-09-16T00:00:00Z",
        "input_hashes": ["c" * 64],
        "issues": [
            {"code": "STYLE_MISMATCH", "severity": BLOCKING,
             "evidence": "皮肤呈瓷亮高光，与干燥哑光基调不符",
             "repair_instruction": "重注入风格参考，压低高光并恢复哑光表面"},
            {"code": "NECK_STUMP_VISIBLE", "severity": BLOCKING,
             "evidence": "领口上方可见肉色颈桩",
             "repair_instruction": "改用隐形人体支撑，隐藏全部皮肤残端"},
        ],
    }
    base.update(kw)
    return base


# --- 老罗失败码覆盖 ---------------------------------------------------------------

def test_required_issue_codes_present():
    for code in ("STYLE_MISMATCH", "BODY_PROPORTION_ERROR", "NECK_STUMP_VISIBLE",
                 "HEAD_REMAINS_IN_COSTUME_VIEW", "FORBIDDEN_MULTI_PANEL",
                 "FRONT_BACK_INCONSISTENT", "SUBJECT_CROPPED"):
        assert code in ISSUE_CODES, code


def test_process_codes_are_separate_from_art_codes():
    for code in ("QC_UNTRUSTWORTHY", "UNKNOWN_ISSUE_CODE"):
        assert code in PROCESS_CODES
        assert code not in ISSUE_CODES


def test_laoluo_failure_is_parsed_with_all_codes():
    data = _response(issues=[
        {"code": "STYLE_MISMATCH", "severity": BLOCKING, "evidence": "风格漂移",
         "repair_instruction": "重注入风格参考"},
        {"code": "BODY_PROPORTION_ERROR", "severity": BLOCKING, "evidence": "头身比失衡",
         "repair_instruction": "回到七头身"},
        {"code": "NECK_STUMP_VISIBLE", "severity": BLOCKING, "evidence": "可见颈桩",
         "repair_instruction": "隐形支撑"},
        {"code": "HEAD_REMAINS_IN_COSTUME_VIEW", "severity": BLOCKING, "evidence": "背面残头",
         "repair_instruction": "移除头部"},
        {"code": "FORBIDDEN_MULTI_PANEL", "severity": BLOCKING, "evidence": "出现三格",
         "repair_instruction": "改为单张单幅"},
    ])
    result = parse_qc_response(data, expected={"task_id": TASK_ID, "input_hashes": ["c" * 64]})
    assert result.decision == QcDecision.NEEDS_REVISION.value
    assert {i.code for i in result.issues} == {
        "STYLE_MISMATCH", "BODY_PROPORTION_ERROR", "NECK_STUMP_VISIBLE",
        "HEAD_REMAINS_IN_COSTUME_VIEW", "FORBIDDEN_MULTI_PANEL"}
    assert result.blocking_issues


# --- 状态守卫 -------------------------------------------------------------------

def test_blocking_issue_cannot_be_accepted():
    data = _response(decision="ACCEPTED")
    result = parse_qc_response(data, expected={"task_id": TASK_ID, "input_hashes": ["c" * 64]})
    assert result.decision != QcDecision.ACCEPTED.value
    assert result.decision == QcDecision.NEEDS_REVISION.value


def test_clean_response_can_be_accepted():
    data = _response(decision="ACCEPTED", issues=[])
    result = parse_qc_response(data, expected={"task_id": TASK_ID, "input_hashes": ["c" * 64]})
    assert result.decision == QcDecision.ACCEPTED.value
    assert result.untrustworthy_reason == ""


def test_missing_evidence_is_untrustworthy():
    data = _response(issues=[{"code": "STYLE_MISMATCH", "severity": BLOCKING,
                              "evidence": "", "repair_instruction": "x"}])
    result = parse_qc_response(data, expected={"task_id": TASK_ID, "input_hashes": ["c" * 64]})
    assert result.decision == QcDecision.UNTRUSTWORTHY.value
    assert "证据" in result.untrustworthy_reason


def test_wrong_task_reference_is_untrustworthy():
    data = _response(task_id="CH_别人-正脸-v01")
    result = parse_qc_response(data, expected={"task_id": TASK_ID, "input_hashes": ["c" * 64]})
    assert result.decision == QcDecision.UNTRUSTWORTHY.value
    assert "任务" in result.untrustworthy_reason


def test_input_hash_mismatch_is_untrustworthy():
    data = _response(input_hashes=["d" * 64])
    result = parse_qc_response(data, expected={"task_id": TASK_ID, "input_hashes": ["c" * 64]})
    assert result.decision == QcDecision.UNTRUSTWORTHY.value
    assert "哈希" in result.untrustworthy_reason


def test_unknown_code_is_untrustworthy():
    data = _response(issues=[{"code": "我觉得不好看", "severity": BLOCKING,
                              "evidence": "主观", "repair_instruction": "重画"}])
    result = parse_qc_response(data, expected={"task_id": TASK_ID, "input_hashes": ["c" * 64]})
    assert result.decision == QcDecision.UNTRUSTWORTHY.value


def test_non_mapping_response_rejected():
    with pytest.raises(ValueError):
        parse_qc_response("decision = ACCEPTED\n")
    with pytest.raises(ValueError):
        parse_qc_response(["ACCEPTED"])


def test_json_string_is_rejected():
    """只接受结构化映射；不允许把自由文本当检查结果。"""
    with pytest.raises(ValueError):
        parse_qc_response(json.dumps(_response()))


def test_manual_check_requires_operator_label():
    data = _response(decision="ACCEPTED", issues=[], checker="", manual=True)
    result = parse_qc_response(data, expected={"task_id": TASK_ID, "input_hashes": ["c" * 64]})
    assert result.decision == QcDecision.UNTRUSTWORTHY.value


def test_manual_check_records_operator_and_note():
    data = _response(decision="ACCEPTED", issues=[], checker="人工:柠檬",
                     note="目视通过", manual=True)
    result = parse_qc_response(data, expected={"task_id": TASK_ID, "input_hashes": ["c" * 64]})
    assert result.decision == QcDecision.ACCEPTED.value
    assert result.checker == "人工:柠檬"
    assert result.note == "目视通过"


# --- rubric 与请求 ---------------------------------------------------------------

@pytest.mark.parametrize("kind,rubric_name", [
    ("正脸", "人物"), ("服装独立背面", "人物"), ("人物全身正面", "人物"),
    ("场景母版", "场景"), ("道具", "道具"),
])
def test_rubric_files_exist_and_load(kind, rubric_name):
    assert (RUBRIC_DIR / f"{rubric_name}.toml").is_file()
    rubric = load_rubric(rubric_name)
    assert rubric["subject"] == rubric_name
    assert rubric["checks"]
    assert rubric_for_kind(kind)["subject"] == rubric_name


def test_person_rubric_covers_required_checks():
    rubric = load_rubric("人物")
    checks = " ".join(str(c) for c in rubric["checks"])
    for word in ("身份", "风格", "比例", "裁切", "正背", "服装", "拼版"):
        assert word in checks, word


def test_scene_rubric_covers_anchors_and_space():
    rubric = load_rubric("场景")
    checks = " ".join(str(c) for c in rubric["checks"])
    for word in ("锚点", "空间", "连接"):
        assert word in checks, word


def test_prop_rubric_covers_silhouette_material_state():
    rubric = load_rubric("道具")
    checks = " ".join(str(c) for c in rubric["checks"])
    for word in ("轮廓", "材质", "比例", "状态"):
        assert word in checks, word


def test_qc_request_uses_different_prompt_than_generation():
    req = build_qc_request(_task(), _attempt(), load_rubric("人物"),
                           generation_prompt="【任务目标】正脸特写")
    assert "质检" in req["prompt"]
    assert "【任务目标】" not in req["prompt"]
    assert req["image"].endswith("image.png")
    assert req["task_id"] == TASK_ID


def test_qc_request_requires_image_and_rubric():
    with pytest.raises(ValueError):
        build_qc_request(_task(), AttemptRecord(round=1, image=""), load_rubric("人物"))
    with pytest.raises(ValueError):
        build_qc_request(_task(), _attempt(), {})


def test_rubric_lists_blocking_codes():
    for name in ("人物", "场景", "道具"):
        rubric = load_rubric(name)
        assert rubric["blocking_codes"]
        for code in rubric["blocking_codes"]:
            assert code in ISSUE_CODES, (name, code)


# --- 写回 ------------------------------------------------------------------------

def test_qc_result_written_as_toml(tmp_path):
    record = _attempt()
    result = parse_qc_response(_response(),
                               expected={"task_id": TASK_ID, "input_hashes": ["c" * 64]})
    path = write_qc_result(tmp_path, record, result)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "NEEDS_REVISION" in text
    assert "NECK_STUMP_VISIBLE" in text
    assert "\\u" not in text


def test_qc_result_toml_is_deterministic():
    result = parse_qc_response(_response(),
                               expected={"task_id": TASK_ID, "input_hashes": ["c" * 64]})
    assert qc_result_to_toml(result) == qc_result_to_toml(result)


def test_qc_result_is_immutable():
    result = QcResult(decision="ACCEPTED")
    with pytest.raises(Exception):
        result.decision = "NEEDS_REVISION"  # type: ignore[misc]


def test_issue_severity_whitelist():
    with pytest.raises(ValueError):
        QcIssue(code="STYLE_MISMATCH", severity="超级严重")
    assert QcIssue(code="STYLE_MISMATCH", severity=BLOCKING).is_blocking


def test_no_vendor_sdk_needed_for_qc():
    source = (REPO_ROOT / "scripts" / "engine" / "image_qc.py").read_text(encoding="utf-8")
    assert "google" not in source and "genai" not in source


# --- 阶段 6 工单 6.2：Omni 结构化硬门（16 个问题码 + 四种判定） ---------------------

SPEC_HARD_GATES = (
    "HAND_FINGER_DEFECT", "BODY_PROPORTION_DEFECT", "IDENTITY_DRIFT", "STYLE_MISMATCH",
    "CROP_MISMATCH", "POSITION_MISMATCH", "VIEWPOINT_MISMATCH", "EYELINE_MISMATCH",
    "AXIS_CROSSING", "OCCLUSION_MISMATCH", "PROP_COUNT_MISMATCH", "PROP_STATE_MISMATCH",
    "SCENE_STRUCTURE_MISMATCH", "EXTRA_SUBJECT", "TEXT_OR_WATERMARK",
    "REFERENCE_CHAIN_INVALID",
)
SPEC_DECISIONS = ("ACCEPTED", "PASS_WITH_NOTES", "NEEDS_REVISION", "NEEDS_HUMAN_DECISION")


def _gate_issue(code, severity="minor"):
    """硬门问题即使被标成 minor，也必须能阻断「通过」与「通过但有备注」。"""
    return {"code": code, "severity": severity, "evidence": f"{code} 的可见证据",
            "repair_instruction": f"按 {code} 定向修复"}


def _result(decision, issues=()):
    return {"task_id": "CH_林姑娘-正脸-v01", "checker": "单元测试主模型",
            "checked_at": "2026-09-18T00:00:00Z", "decision": decision,
            "issues": [dict(row) for row in issues]}


def test_hard_gate_codes_match_the_spec_list():
    assert tuple(image_qc.HARD_GATE_CODES) == SPEC_HARD_GATES, (
        "硬门码表必须与施工单 6.2 列出的 16 个码逐一对应且顺序一致")
    assert len(set(SPEC_HARD_GATES)) == 16
    assert all(code.isupper() for code in SPEC_HARD_GATES)


def test_mandated_decisions_are_the_four_review_verdicts():
    assert tuple(image_qc.MANDATED_DECISIONS) == SPEC_DECISIONS
    assert set(SPEC_DECISIONS) == set(visual_schema.REVIEW_DECISIONS)


def test_all_hard_gate_codes_are_known_issue_codes():
    unknown = [code for code in SPEC_HARD_GATES if code not in image_qc.ISSUE_CODES]
    assert not unknown, f"这些硬门码还不被质检接受：{unknown}"


@pytest.mark.parametrize("code", SPEC_HARD_GATES)
def test_hard_gate_blocks_accepted(code):
    result = parse_qc_response(_result("ACCEPTED", [_gate_issue(code)]))
    assert result.decision == "NEEDS_REVISION", f"{code} 存在时不得判为通过"


@pytest.mark.parametrize("code", SPEC_HARD_GATES)
def test_hard_gate_blocks_pass_with_notes(code):
    result = parse_qc_response(_result("PASS_WITH_NOTES", [_gate_issue(code)]))
    assert result.decision == "NEEDS_REVISION", (
        f"{code} 属于硬门，存在时任一句法都不得放行（含「通过但有备注」）")


@pytest.mark.parametrize("code", SPEC_HARD_GATES)
def test_hard_gate_is_forced_to_blocking_severity(code):
    """硬门码的严重度由码本身决定，不被检查器给的 minor 降级。"""
    result = parse_qc_response(_result("ACCEPTED", [_gate_issue(code, severity="minor")]))
    assert result.blocking_issues, f"{code} 必须成为阻断级问题"
    assert result.issues[0].severity == image_qc.BLOCKING


def test_soft_notes_still_pass_with_notes():
    """非硬门的小问题仍允许「通过但有备注」（硬门收紧不得误伤软问题）。"""
    result = parse_qc_response(_result("PASS_WITH_NOTES", [
        {"code": "LOW_DETAIL", "severity": "minor", "evidence": "远景细节偏简",
         "repair_instruction": "补细节"},
    ]))
    assert result.decision == "PASS_WITH_NOTES"
    assert result.issues[0].severity == "minor"


def test_legacy_synonym_codes_are_treated_as_hard_gates():
    """同义旧码必须与新码同等阻断（否则老检查器能绕过新硬门）。"""
    aliases = {"BODY_PROPORTION_ERROR": "BODY_PROPORTION_DEFECT",
               "SUBJECT_CROPPED": "CROP_MISMATCH"}
    for legacy, canonical in aliases.items():
        assert image_qc.CODE_ALIASES.get(legacy) == canonical
        result = parse_qc_response(_result("PASS_WITH_NOTES", [_gate_issue(legacy)]))
        assert result.decision == "NEEDS_REVISION", f"旧码 {legacy} 不得放行"


@pytest.mark.parametrize("code", SPEC_HARD_GATES)
def test_new_hard_gate_codes_require_evidence_and_repair(code):
    bare = {"code": code, "severity": "blocking", "evidence": "", "repair_instruction": ""}
    result = parse_qc_response(_result("ACCEPTED", [bare]))
    assert result.decision == "QC_UNTRUSTWORTHY", f"{code} 缺证据/修复指令必须不可信"


def test_person_rubric_covers_per_hand_checks():
    data = image_qc.load_rubric("人物")
    text = "；".join(str(c) for c in data["checks"])
    for token in ("每只", "手指数", "分离", "关节", "抓握", "遮挡"):
        assert token in text, f"人物清单必须逐只手检查「{token}」"
    assert "HAND_FINGER_DEFECT" in text


def test_person_rubric_covers_position_and_camera_semantics():
    data = image_qc.load_rubric("人物")
    text = "；".join(str(c) for c in data["checks"])
    for token in ("左", "中", "右", "前", "后", "距离", "朝向", "视线", "遮挡", "轴线"):
        assert token in text, f"人物清单必须检查「{token}」的语义关系"
    assert "不要求像素坐标" in text, "容差口径必须写明：不追求像素级坐标一致"
    for code in ("POSITION_MISMATCH", "VIEWPOINT_MISMATCH", "EYELINE_MISMATCH",
                 "AXIS_CROSSING", "OCCLUSION_MISMATCH"):
        assert code in text, f"人物清单必须包含 {code}"


# 每类资产**不适用**的硬门码（其余硬门都必须在同类清单的检查项里出现）
_NOT_APPLICABLE_GATES = {
    "人物": (
        "PROP_COUNT_MISMATCH",
        "PROP_STATE_MISMATCH",
        "SCENE_STRUCTURE_MISMATCH",
    ),
    "场景": (
        "HAND_FINGER_DEFECT",
        "BODY_PROPORTION_DEFECT",
        "IDENTITY_DRIFT",
        "POSITION_MISMATCH",
        "EYELINE_MISMATCH",
        "AXIS_CROSSING",
        "OCCLUSION_MISMATCH",
        "PROP_COUNT_MISMATCH",
        "PROP_STATE_MISMATCH",
    ),
    "道具": (
        "HAND_FINGER_DEFECT",
        "BODY_PROPORTION_DEFECT",
        "IDENTITY_DRIFT",
        "POSITION_MISMATCH",
        "VIEWPOINT_MISMATCH",
        "EYELINE_MISMATCH",
        "AXIS_CROSSING",
        "SCENE_STRUCTURE_MISMATCH",
    ),
}


@pytest.mark.parametrize("subject", ("人物", "场景", "道具"))
def test_each_rubric_declares_its_applicable_hard_gates(subject):
    data = image_qc.load_rubric(subject)
    blocking = set(data["blocking_codes"])
    text = "；".join(str(c) for c in data["checks"])
    assert blocking <= image_qc.ISSUE_CODES, "清单里的阻断码必须都是已知问题码"
    assert set(image_qc.HARD_GATE_CODES) <= blocking, (
        f"{subject} 清单必须把全部硬门码列为阻断（硬门是全局口径）")
    applicable = set(SPEC_HARD_GATES) - set(_NOT_APPLICABLE_GATES[subject])
    missing = [code for code in sorted(applicable) if code not in text]
    assert not missing, f"{subject} 清单的检查项未覆盖这些硬门：{missing}"
