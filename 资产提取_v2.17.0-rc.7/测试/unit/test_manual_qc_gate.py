#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 S5 / Task D2：十项人工画质表 + 「机器签发 ≠ 内容正确」发布闸门。

背景事实（不许美化）：
- `docs/验收/人工画质表.json` 十项（画风/身份/年龄/构图/裁切/比例/材质/光影/禁项/系列一致性）
  **全部为「待人工」**，本机没有任何出图密钥（六节点池 key 全为字面量 `REPLACE`），
  真实两图冒烟只能是 `BLOCKED-CRED`；
- 因此 `SKILL.md` 必须停在 `2.17.0-rc.7`，任何文档都不许正面宣称「正式版已完成」。

本文件钉死两件事：
1. 真实表 + 真实版本 → 校验器认为口径自洽（返回空列表），CLI 退出码 0，
   且 stdout 明确打印「拒绝出正式版」与原因（不是静默通过）；
2. 每一种「假绿灯」构造（无真人签署的「通过」、证据路径不存在、十项没签齐却写正式版号、
   冒烟仍阻断却升正式版、缺解锁条件……）都必须被 `validate()` 拒掉。

用例全部对**构造出来的 dict** 跑校验器，绝不改动仓库里的真实 `SKILL.md` 与画质表。
"""
import copy
import json
import sys
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_manual_qc as gate  # noqa: E402

TABLE_PATH = REPO_ROOT / "docs" / "验收" / "人工画质表.json"
ALL_IDS = list(gate.EXPECTED_IDS)
RC_VER = "2.17.0-rc.7"
REL_VER = "2.17.0"


def real_table() -> dict:
    return json.loads(TABLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def table() -> dict:
    """真实表的深拷贝：只改要测的那一处，其余保持仓内真实口径。"""
    return copy.deepcopy(real_table())


def sign(table: dict, item_id: str, *, reviewer="张某某", review_date="2026-09-23",
         evidence=("SKILL.md",), verdict="通过", note="") -> None:
    """把某项签成人工结论（默认带真人评审人 + 合法日期 + 真实存在的产物）。"""
    table["reviewer"] = reviewer
    table["review_date"] = review_date
    for item in table["items"]:
        if item["id"] == item_id:
            item["判定"] = verdict
            item["证据路径"] = list(evidence)
            item["备注"] = note


def sign_all(table: dict, **kw) -> None:
    for item_id in ALL_IDS:
        sign(table, item_id, **kw)


def has(reasons: list, *keys: str) -> bool:
    """reasons 中是否存在一条同时含所有关键字（用精确断言代替「非空就行」）。"""
    return any(all(k in r for k in keys) for r in reasons)


# --- 仓内真实状态：口径自洽，但闸门必须明确拒绝出正式版 -------------------------------

def test_real_table_is_clean_under_current_rc_version():
    assert gate.validate(real_table(), gate.skill_version()) == []


def test_real_table_rejects_release_version_number():
    """把版本号换成正式版号（不真的改 SKILL.md）必须立刻判红。"""
    assert has(gate.validate(real_table(), REL_VER), "升版条件未满足")


def test_cli_exits_zero_and_refuses_release_on_stdout():
    proc = run_py([str(REPO_ROOT / "scripts" / "check_manual_qc.py")], cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
    assert "拒绝出正式版" in proc.stdout, proc.stdout
    assert "BLOCKED-CRED" in proc.stdout, proc.stdout
    assert "0/10" in proc.stdout, proc.stdout


def test_real_table_still_marks_everything_pending():
    """登记事实：十项一项都没签，冒烟仍被凭据阻断——有人偷偷预填即红。"""
    data = real_table()
    assert [i["判定"] for i in data["items"]] == ["待人工"] * 10
    assert not str(data.get("reviewer", "")).strip()
    assert data["smoke"]["真实API两图冒烟"]["status"] == "BLOCKED-CRED"
    assert gate.skill_version() == RC_VER


# --- 「通过」但没有真人证据 --------------------------------------------------------
def test_pass_without_reviewer_rejected(table):
    sign(table, "style", reviewer="")
    assert has(gate.validate(table, RC_VER), "真人评审人")


def test_pass_with_whitespace_only_reviewer_rejected(table):
    sign(table, "style", reviewer="   ")
    assert has(gate.validate(table, RC_VER), "真人评审人")


def test_pass_without_review_date_rejected(table):
    sign(table, "style", review_date="")
    assert has(gate.validate(table, RC_VER), "评审日期")


@pytest.mark.parametrize("bad_date", ["2026/09/23", "23-09-2026", "2026-9-3", "昨天"])
def test_pass_with_malformed_review_date_rejected(table, bad_date):
    """日期格式不对等于没签：闸门不认「差不多对」的日期。"""
    sign(table, "style", review_date=bad_date)
    assert has(gate.validate(table, RC_VER), "评审日期"), bad_date


def test_pass_without_evidence_path_rejected(table):
    sign(table, "style", evidence=())
    assert has(gate.validate(table, RC_VER), "产物路径")


def test_pass_with_missing_evidence_path_rejected(table):
    sign(table, "style", evidence=("docs/验收/根本不存在的产物.png",))
    assert has(gate.validate(table, RC_VER), "产物路径不存在")


def test_genuinely_signed_item_is_accepted(table):
    """真有人、有日期、产物真实存在 → 该项不得再报错（防止闸门只会误杀）。"""
    sign(table, "style")
    assert gate.validate(table, RC_VER) == []


# --- 表结构：缺项 / 乱序 / 非法判定 / schema / policy --------------------------------

def test_missing_item_rejected(table):
    table["items"] = [i for i in table["items"] if i["id"] != "crop"]
    assert has(gate.validate(table, RC_VER), "十项必须齐全")


def test_reordered_items_rejected(table):
    table["items"] = [table["items"][1], table["items"][0]] + table["items"][2:]
    assert has(gate.validate(table, RC_VER), "十项必须齐全")


def test_extra_unknown_item_rejected(table):
    table["items"].append({"id": "whatever", "判定": "通过"})
    assert has(gate.validate(table, RC_VER), "十项必须齐全")


@pytest.mark.parametrize("verdict", ["已验收", "PASS", "", "通过 ", "未验收"])
def test_illegal_verdict_rejected(table, verdict):
    table["items"][0]["判定"] = verdict
    assert has(gate.validate(table, RC_VER), "判定非法"), verdict


def test_negative_verdict_without_note_rejected(table):
    sign(table, "age", verdict="不通过", note="")
    assert has(gate.validate(table, RC_VER), "必须写备注")


def test_negative_verdict_with_note_accepted(table):
    sign(table, "age", verdict="不通过", note="手指数量错误")
    assert gate.validate(table, RC_VER) == []


def test_negative_verdict_also_blocks_release(table):
    """任何一项判「不通过」都不许去掉 -rc。"""
    sign(table, "age", verdict="不通过", note="材质塑料感")
    assert has(gate.validate(table, REL_VER), "十项未全签")


def test_unsupported_schema_rejected(table):
    table["schema"] = "asset-extract-manual-qc/0"
    assert has(gate.validate(table, RC_VER), "schema")


@pytest.mark.parametrize("policy", ["", "十项由真人逐项签署。"])
def test_policy_must_state_machine_is_not_content(table, policy):
    table["policy"] = policy
    assert has(gate.validate(table, RC_VER), "机器签发不等于内容正确"), repr(policy)


# --- 升版闸门：十项未签齐 / 冒烟未过，都不许去掉 -rc ---------------------------------

def test_partial_signature_blocks_release_version(table):
    sign_all(table)
    table["items"][9]["判定"] = "待人工"
    table["items"][9]["证据路径"] = []
    assert has(gate.validate(table, REL_VER), "十项未全签")


def test_release_version_requires_smoke_to_pass(table):
    """十项全签但冒烟仍 BLOCKED-CRED → 升正式版号依旧是假绿灯（解锁条件是两件事）。"""
    sign_all(table)
    reasons = gate.validate(table, REL_VER)
    assert has(reasons, "冒烟"), reasons
    assert not has(reasons, "十项未全签"), reasons


def test_release_version_accepted_when_smoke_passes(table):
    sign_all(table)
    table["smoke"]["真实API两图冒烟"] = {
        "status": "PASS", "date": "2026-09-23", "artifact_paths": ["SKILL.md"]}
    assert gate.validate(table, REL_VER) == []


@pytest.mark.parametrize("bad_path", ["", "docs/验收/没有这个文件.png"])
def test_smoke_pass_requires_existing_artifacts(table, bad_path):
    sign_all(table)
    table["smoke"]["真实API两图冒烟"] = {
        "status": "PASS", "date": "2026-09-23", "artifact_paths": [bad_path]}
    reasons = gate.validate(table, REL_VER)
    assert has(reasons, "冒烟产物"), reasons


def test_smoke_pass_without_date_or_artifact_rejected(table):
    table["smoke"]["真实API两图冒烟"] = {"status": "PASS", "date": "", "artifact_paths": []}
    reasons = gate.validate(table, RC_VER)
    assert has(reasons, "冒烟声称 PASS 但没有日期"), reasons
    assert has(reasons, "但没有产物路径"), reasons


def test_table_reviewing_another_version_line_rejected(table):
    """画质表审的是 2.17.0 这条线，版本号却跳到 2.18.0 → 旧签署不得沿用。"""
    assert has(gate.validate(table, "2.18.0-rc.1"), "不是同一版")


def test_table_must_declare_which_version_it_reviews(table):
    table["version_under_review"] = ""
    assert has(gate.validate(table, RC_VER), "version_under_review")


# --- 冒烟登记：BLOCKED-CRED 必须说清原因、解锁条件、复核命令 --------------------------

@pytest.mark.parametrize("field", ["reason", "unlock_condition", "recheck_command"])
def test_blocked_smoke_requires_the_triple(table, field):
    table["smoke"]["真实API两图冒烟"][field] = ""
    assert has(gate.validate(table, RC_VER), f"必须写 {field}")


def test_missing_smoke_record_rejected(table):
    table["smoke"] = {}
    assert has(gate.validate(table, RC_VER), "缺失")


def test_unknown_smoke_status_rejected(table):
    """状态写个不认识的词不得等于「没登记过失败」——那也是假绿灯。"""
    table["smoke"]["真实API两图冒烟"]["status"] = "基本能用"
    assert has(gate.validate(table, RC_VER), "状态非法")


def test_blocked_smoke_must_say_why_credentials_missing():
    """本机事实：六节点池 key 全为 REPLACE 占位，原因里必须点明是凭据/密钥缺失。"""
    reason = real_table()["smoke"]["真实API两图冒烟"]["reason"]
    assert "REPLACE" in reason or "密钥" in reason, reason


def test_blocked_smoke_recheck_command_is_runnable_form(table):
    """复核命令必须真的指向本仓闸门，否则解锁时无人可照做。"""
    cmd = real_table()["smoke"]["真实API两图冒烟"]["recheck_command"]
    assert "check_manual_qc.py" in cmd, cmd
    assert "PYTHONIOENCODING" in cmd, cmd


# --- 文档口径：正面宣称「正式版已完成」判红，反面表述放行 ------------------------------

def fake_repo(tmp_path: Path, doc_line: str) -> Path:
    (tmp_path / "docs" / "验收").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "验收" / "报告.md").write_text(
        "# 报告\n\n" + doc_line + "\n", encoding="utf-8")
    (tmp_path / "SKILL.md").write_text("version: " + RC_VER + "\n", encoding="utf-8")
    return tmp_path


def test_docs_positive_claim_rejected(tmp_path, table, monkeypatch):
    monkeypatch.setattr(gate, "REPO", fake_repo(
        tmp_path, "本轮验收结论：正式版已完成。"))
    assert has(gate.validate(table, RC_VER), "禁用口径")


def test_docs_negative_wording_is_allowed(tmp_path, table, monkeypatch):
    """含「禁止/不得/不可/勿」的行是合规的反面表述，不得误杀。"""
    monkeypatch.setattr(gate, "REPO", fake_repo(
        tmp_path, "在人工十项全签之前，**禁止**写「正式版已完成」。"))
    assert gate.validate(table, RC_VER) == []


def test_evidence_paths_resolve_against_repo_root(tmp_path, table, monkeypatch):
    """证据路径按仓库根解析：换一个根，同一条路径就应当判不存在。"""
    sign(table, "style", evidence=("pyproject.toml",))
    monkeypatch.setattr(gate, "REPO", fake_repo(tmp_path, "无宣称"))
    assert has(gate.validate(table, RC_VER), "产物路径不存在")


# --- 闸门必须接进既有总门，而不是另起一套 --------------------------------------------

def test_gate_wired_into_run_all_checks():
    text = (REPO_ROOT / "测试" / "run_all_checks.py").read_text(encoding="utf-8")
    assert "check_manual_qc.py" in text, "人工画质表闸门必须进既有总门"
    assert '("manual qc gate"' in text, "总门里必须能按名字找到该项"
    assert text.count("CHECKS = [") == 1, "不许出现第二套平行总门"


def test_table_points_at_a_real_instruction_doc():
    """画质表必须把「怎么签」的说明书指针带上，且路径真实存在（改名即红）。"""
    doc = str(real_table().get("填写说明", "")).strip()
    assert doc, "画质表缺少 填写说明 指针"
    assert (REPO_ROOT / doc).is_file(), f"填写说明不存在：{doc}"


def test_checker_is_flake8_clean():
    proc = run_py(
            ["-m", "flake8", str(REPO_ROOT / "scripts" / "check_manual_qc.py"), __file__],
            cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
