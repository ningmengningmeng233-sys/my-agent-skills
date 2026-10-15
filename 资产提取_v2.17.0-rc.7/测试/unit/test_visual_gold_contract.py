#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单04-Task3：跨类型金标准契约（规格 11）。

只验证**计划与 rubric**：任务集合、依赖链、画幅、参考职责与禁止项是否与规格一致；
不伪造任何人工视觉分数——`human_visual_review` 必须留空，由人工填写。
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.image_qc import (  # noqa: E402
    BLOCKING,
    ISSUE_CODES,
    QcIssue,
    parse_qc_response,
)
from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_planner import plan_visual_tasks  # noqa: E402
from scripts.engine.visual_schema import ASPECT_BY_KIND, validate_visual_plan  # noqa: E402

GOLD = REPO_ROOT / "测试" / "fixtures" / "visual_gold"
STYLE = resolve_style_record("仙侠国风电影CG")
CASES = ("老罗", "女性长发", "复杂甲胄", "宽袍披风", "场景母版", "代表道具")
PLAN_CASES = ("老罗", "女性长发", "复杂甲胄", "宽袍披风", "场景母版", "代表道具")


def _load_case(name):
    base = GOLD / name
    asset = json.loads((base / "asset.json").read_text(encoding="utf-8"))
    plan_text = (base / "expected_plan.toml").read_text(encoding="utf-8")
    rubric_text = (base / "rubric.toml").read_text(encoding="utf-8")
    return asset, plan_text, rubric_text


def _expected_kinds(text):
    import re

    m = re.search(r"expected_kinds = \[(.*?)\]", text)
    return [item.strip().strip('"') for item in m.group(1).split(",") if item.strip()]


def _expected_tasks(text):
    import re

    tasks = []
    for block in text.split("[[expected_tasks]]")[1:]:
        kind = re.search(r'kind = "([^"]+)"', block).group(1)
        aspect = re.search(r'aspect_ratio = "([^"]+)"', block).group(1)
        deps = re.findall(r'"([^"]+)"', block.split("depends_on = [")[1].split("]")[0])
        tasks.append((kind, aspect, deps))
    return tasks


@pytest.mark.parametrize("name", CASES)
def test_fixture_files_exist(name):
    base = GOLD / name
    for filename in ("asset.json", "expected_plan.toml", "rubric.toml"):
        assert (base / filename).is_file(), f"{name}/{filename}"


@pytest.mark.parametrize("name", PLAN_CASES)
def test_fixture_asset_has_evidence(name):
    asset, _plan, _rubric = _load_case(name)
    assert asset["fact_level"] == "剧本事实"
    assert asset["evidence"], "事实夹具必须带原文摘录"
    quote = asset["evidence"][0]["quote"]
    assert quote and asset["evidence"][0]["line"], "摘句与行号缺一不可"


@pytest.mark.parametrize("name", PLAN_CASES)
def test_planned_tasks_match_gold_contract(name):
    asset, plan_text, _rubric = _load_case(name)
    index = {"schema_version": 1, "source_fingerprint": "sha256:gold",
             "assets": [asset]}
    plan = plan_visual_tasks(index, STYLE)
    planned_kinds = sorted(t.kind for t in plan.tasks)
    expected_kinds = sorted(_expected_kinds(plan_text))
    assert planned_kinds == expected_kinds, (planned_kinds, expected_kinds)


@pytest.mark.parametrize("name", PLAN_CASES)
def test_dependency_chain_and_aspect_ratio_match_gold_contract(name):
    asset, plan_text, _rubric = _load_case(name)
    index = {"schema_version": 1, "source_fingerprint": "sha256:gold", "assets": [asset]}
    plan = plan_visual_tasks(index, STYLE)
    actual = {t.kind: (t.aspect_ratio, sorted(t.depends_on)) for t in plan.tasks}
    for kind, aspect, deps in _expected_tasks(plan_text):
        assert kind in actual, kind
        got_aspect, got_deps = actual[kind]
        assert got_aspect == aspect == ASPECT_BY_KIND[kind], (kind, got_aspect, aspect)
        assert got_deps == sorted(deps), (kind, got_deps, deps)


@pytest.mark.parametrize("name", PLAN_CASES)
def test_gold_plan_passes_validator(name):
    asset, _plan, _rubric = _load_case(name)
    index = {"schema_version": 1, "source_fingerprint": "sha256:gold", "assets": [asset]}
    plan = plan_visual_tasks(index, STYLE)
    assert validate_visual_plan(plan, index) == []


@pytest.mark.parametrize("name", CASES)
def test_fixture_rubric_codes_are_known(name):
    _asset, _plan, rubric_text = _load_case(name)
    import re

    for block in re.findall(r"blocking_codes = \[(.*?)\]", rubric_text, re.S):
        for code in re.findall(r'"([A-Z_]+)"', block):
            assert code in ISSUE_CODES, f"{name}: 未知问题码 {code}"


@pytest.mark.parametrize("name", CASES)
def test_human_visual_review_is_blank(name):
    _asset, _plan, rubric_text = _load_case(name)
    import re

    m = re.search(r'human_visual_review = "(.*?)"', rubric_text)
    assert m is not None, f"{name}: 缺少人工验收栏"
    assert m.group(1).strip() == "", "人工视觉结论必须留空，不得代填"


@pytest.mark.parametrize("name", CASES)
def test_rubric_matches_shipped_rubric_subject(name):
    from scripts.engine.image_qc import load_rubric

    _asset, _plan, rubric_text = _load_case(name)
    import re

    subject = re.search(r'subject = "([^"]+)"', rubric_text).group(1)
    shipped = load_rubric(subject)
    assert shipped["checks"], subject


# --- 老罗失败证据 ----------------------------------------------------------------

def test_laoluo_failure_codes_are_all_covered():
    """老罗错误图必须能被项目内全部问题码覆盖（风格/比例/断颈/残头/拼版）。"""
    _asset, _plan, rubric_text = _load_case("老罗")
    for code in ("STYLE_MISMATCH", "BODY_PROPORTION_ERROR", "NECK_STUMP_VISIBLE",
                 "HEAD_REMAINS_IN_COSTUME_VIEW", "FORBIDDEN_MULTI_PANEL"):
        assert code in rubric_text, code
        assert code in ISSUE_CODES, code


def test_laoluo_external_image_hash_is_honest():
    """外部输入哈希：要么是真实采集值，要么显式标注不可用——绝不填假哈希。"""
    _asset, _plan, rubric_text = _load_case("老罗")
    import re

    digest = re.search(r'external_image_hash = "([^"]*)"', rubric_text).group(1)
    status = re.search(r'external_image_status = "([^"]*)"', rubric_text).group(1)
    if digest:
        assert len(digest) == 64 and status == "VERIFIED"
    else:
        assert status == "NOT_AVAILABLE"


def test_laoluo_image_never_committed():
    """错误图不得入库；只允许作为外部引用存在。"""
    images = [p for p in (GOLD / "老罗").rglob("*")
              if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    assert images == []


def test_laoluo_parses_to_needs_revision():
    """把老罗的期望问题码喂给检查器：判定必须是需修改，绝不能通过。"""
    _asset, _plan, _rubric = _load_case("老罗")
    issues = [
        {"code": "STYLE_MISMATCH", "severity": BLOCKING, "evidence": "皮肤呈瓷亮高光",
         "repair_instruction": "重注入风格参考，恢复哑光至柔缎"},
        {"code": "BODY_PROPORTION_ERROR", "severity": BLOCKING, "evidence": "头身比失衡",
         "repair_instruction": "回到正确头身比"},
        {"code": "NECK_STUMP_VISIBLE", "severity": BLOCKING, "evidence": "领口上方见颈桩",
         "repair_instruction": "改用隐形人体支撑"},
        {"code": "HEAD_REMAINS_IN_COSTUME_VIEW", "severity": BLOCKING,
         "evidence": "背面残留后脑勺", "repair_instruction": "移除头部结构"},
        {"code": "FORBIDDEN_MULTI_PANEL", "severity": BLOCKING, "evidence": "出现三格分格线",
         "repair_instruction": "改为单张单幅"},
    ]
    result = parse_qc_response({
        "task_id": "CH_老罗-服装独立背面-v01", "decision": "ACCEPTED",
        "checker": "gemini:qc", "checked_at": "t", "input_hashes": ["a" * 64],
        "issues": issues,
    }, expected={"task_id": "CH_老罗-服装独立背面-v01", "input_hashes": ["a" * 64]})
    assert result.decision != "ACCEPTED"
    assert {i.code for i in result.blocking_issues} == {i["code"] for i in issues}


# --- 失败反例集 ------------------------------------------------------------------

def test_failure_matrix_codes_are_known():
    data = json.loads((GOLD / "失败反例" / "case.json").read_text(encoding="utf-8"))
    assert len(data["cases"]) >= 8
    from scripts.engine.image_qc import PROCESS_CODES

    known = set(ISSUE_CODES) | set(PROCESS_CODES)
    known |= {"REFERENCE_NOT_ACCEPTED", "ROUND_LIMIT_EXCEEDED", "PROMPT_TOO_LARGE",
              "IMAGE_HASH_MISMATCH"}
    for case in data["cases"]:
        assert case["expected_codes"], case["name"]
        for code in case["expected_codes"]:
            assert code in known, f"{case['name']}: 未知码 {code}"
        assert case["why"], case["name"]


def test_failure_case_repair_instructions_exist_for_person_codes():
    """人物类失败码必须有定向修复指令（否则返工无法定向）。"""
    from scripts.engine.visual_pipeline import REPAIR_HINTS

    for code in ("STYLE_MISMATCH", "NECK_STUMP_VISIBLE", "HEAD_REMAINS_IN_COSTUME_VIEW",
                 "FORBIDDEN_MULTI_PANEL", "BODY_PROPORTION_ERROR"):
        assert REPAIR_HINTS.get(code), code


def test_qc_issue_rejects_unknown_severity():
    with pytest.raises(ValueError):
        QcIssue(code="STYLE_MISMATCH", severity="fatal")


# --- 阶段 6 工单 6.2：金标准与硬门口径一致 ---------------------------------------

def _gold_expected_codes(name):
    import re

    _asset, _plan, rubric_text = _load_case(name)
    blocks = re.findall(r'expected_codes = \[(.*?)\]', rubric_text, re.S)
    return re.findall(r'"([A-Z_]+)"', blocks[0]) if blocks else []


def _gold_subject(name):
    import re

    _asset, _plan, rubric_text = _load_case(name)
    return re.search(r'subject = "([^"]+)"', rubric_text).group(1)


@pytest.mark.parametrize("name", CASES)
def test_gold_expected_codes_are_blocking_in_shipped_rubric(name):
    """金标准用例的失败码必须仍被产品清单列为阻断（同义旧码按 CODE_ALIASES 归一）。"""
    from scripts.engine.image_qc import CODE_ALIASES, HARD_GATE_CODES, load_rubric

    codes = _gold_expected_codes(name)
    if not codes:
        pytest.skip(f"{name}: 夹具未声明 expected_codes")
    blocking = set(load_rubric(_gold_subject(name))["blocking_codes"])
    canonical = {CODE_ALIASES.get(code, code) for code in blocking}
    unresolved = [code for code in codes
                  if CODE_ALIASES.get(code, code) not in canonical
                  and code not in canonical and code not in HARD_GATE_CODES]
    assert not unresolved, f"{name}: 这些失败码已不在产品清单的阻断集合里：{unresolved}"


def test_gold_hard_gate_codes_cannot_pass():
    """老罗用例里的硬门码：检查器即便判「通过」也必须被打回需要返工。"""
    from scripts.engine.image_qc import HARD_GATE_CODES

    codes = [code for code in _gold_expected_codes("老罗") if code in HARD_GATE_CODES]
    assert codes, "老罗用例应至少包含一个硬门码"
    issues = [{"code": code, "severity": "minor", "evidence": f"{code} 证据",
               "repair_instruction": f"按 {code} 修复"} for code in codes]
    result = parse_qc_response({"task_id": "CH_老罗-服装独立背面-v01", "checker": "单元测试",
                                "decision": "ACCEPTED", "issues": issues})
    assert result.decision == "NEEDS_REVISION"
    assert all(i.is_blocking for i in result.issues), "硬门码必须被强制为阻断级"
