# -*- coding: utf-8 -*-
"""T4（v1.4.0 创作纵深）：终验定序 + F3/T 终扫硬闸 + 恢复卡 + validate-only 跳过。

定案（Spec §二① F3 行 / §二④）：
- 终验序 = 12 门汇编终验通过 → continuity 终扫（F3+T 同场，各自 exit 1 点名）→ 导出四件套；
  **F3 全绿不豁免**：调用点不看 12 门结果，直接执行（照搬 fbs「严禁高分掩盖」）；
- 恢复卡 = 把既有断点检测的输出渲染成 4 行白话卡（不新建扫描，数字全来自实扫磁盘 + 账本重放）。
"""
import json
import os
import subprocess

from dramacraft.continuity_lint import emit_event, final_foreshadow_sweep, replay
from dramacraft.ingestor import _read_file_safe
from conftest import run_py

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _outline_with_foreshadow():
    outline = json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_outline.json")))
    outline["episodes"][0]["foreshadows"] = [{"text": "碎瓷片上的血迹", "payoffInEp": 3}]
    return outline


def _seed(tmp_path, outline, name="ws"):
    """按大纲哈希播种 3 集合法缓存。返回 (workdir, outline_path)。"""
    from dramacraft.outline_lint import compute_outline_hash
    workdir = os.path.join(str(tmp_path), name)
    eps_dir = os.path.join(workdir, "state", "episodes")
    os.makedirs(eps_dir, exist_ok=True)
    sample = json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_script.json")))
    h = compute_outline_hash(outline)
    for ep in sample["episodes"]:
        c = dict(ep)
        c["_meta"] = {"engineVersion": "1.4.0", "schemaVersion": 2, "outlineHash": h}
        with open(os.path.join(eps_dir, f"ep{ep['ep']:03d}.json"), "w", encoding="utf-8") as fp:
            json.dump(c, fp, ensure_ascii=False)
    outline_path = os.path.join(str(tmp_path), "outline.json")
    with open(outline_path, "w", encoding="utf-8") as fp:
        json.dump(outline, fp, ensure_ascii=False)
    return workdir, outline_path


def _run(workdir, outline_path=None, extra=None):
    cmd = ["run_batch.py", "--target-seconds", "90", "--workdir", workdir]
    if outline_path:
        cmd += ["--outline", outline_path]
    cmd += (extra or ["--assemble-only"])
    return run_py(cmd, cwd=ROOT)


# ---------- ① 终验序：12 门全绿也不豁免 F3 ----------

def test_terminal_sweep_blocks_on_unfilled_foreshadow(tmp_path):
    outline = _outline_with_foreshadow()
    workdir, outline_path = _seed(tmp_path, outline)
    res = _run(workdir, outline_path)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "未回收伏笔" in res.stdout or "[F3]" in res.stdout, res.stdout
    assert "F001" in res.stdout
    # 终验未过 → 不得导出交付物
    assert not os.path.exists(os.path.join(workdir, "output", "script.md"))


def _seed_paid_claim(workdir, outline, ep_n, fid, evidence):
    """走真实认领通路把伏笔记成已回收（先按大纲入册，再合成绑定内容指纹的 pay 事件）。"""
    from dramacraft.continuity_lint import register_from_outline, register_paid_claims, replay
    register_from_outline(workdir, outline)
    ep_path = os.path.join(workdir, "state", "episodes", f"ep{ep_n:03d}.json")
    ep_obj = json.load(open(ep_path, encoding="utf-8"))
    ep_obj["foreshadowsPaid"] = [{"id": fid, "evidence": evidence}]
    with open(ep_path, "w", encoding="utf-8") as fp:
        json.dump(ep_obj, fp, ensure_ascii=False)      # 保留 _meta：缓存安检三查要过
    register_paid_claims(workdir, ep_obj, ep_n, replay(workdir, {ep_n}))


def test_terminal_sweep_passes_when_paid(tmp_path):
    outline = _outline_with_foreshadow()
    workdir, outline_path = _seed(tmp_path, outline)
    _seed_paid_claim(workdir, outline, 3, "F001", "第3集第1场：瓷片特写+林晨认领")
    res = _run(workdir, outline_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert os.path.exists(os.path.join(workdir, "output", "script.md"))


def test_terminal_sweep_passes_when_abandoned_by_user(tmp_path):
    outline = _outline_with_foreshadow()
    workdir, outline_path = _seed(tmp_path, outline)
    emit_event(workdir, "abandon", ep=3, id="F001",
               abandonReason="情节线砍掉，用户确认放弃", decidedBy="user")
    res = _run(workdir, outline_path)
    assert res.returncode == 0, res.stdout + res.stderr


def test_terminal_sweep_blocks_abandon_without_user_decision(tmp_path):
    outline = _outline_with_foreshadow()
    workdir, outline_path = _seed(tmp_path, outline)
    emit_event(workdir, "abandon", ep=3, id="F001", abandonReason="模型自己判断砍了",
               decidedBy="model")
    res = _run(workdir, outline_path)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "F001" in res.stdout


def test_legacy_outline_without_foreshadows_is_never_blocked(tmp_path):
    """兼容回归（Review Focus #1）：旧大纲零新字段 → 终验零误拦，照常交付四件套。"""
    outline = json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_outline.json")))
    workdir, outline_path = _seed(tmp_path, outline)
    res = _run(workdir, outline_path)
    assert res.returncode == 0, res.stdout + res.stderr
    for name in ("script.md", "script_breakdown.csv", "script.json", "script-report.html"):
        assert os.path.exists(os.path.join(workdir, "output", name)), name


def test_final_foreshadow_sweep_reports_every_open_item(tmp_path):
    from dramacraft.continuity_lint import register_from_outline
    outline = _outline_with_foreshadow()
    register_from_outline(tmp_path, outline)          # 大纲伏笔入册（run_batch 在闸前做）
    emit_event(tmp_path, "plant", ep=3, id="F002", text="还没到期的伏笔",
               payoffPlannedEp=9, registeredBy="outline")
    bad = final_foreshadow_sweep(tmp_path, {1, 2, 3}, outline)
    assert any("F001" in v for v in bad)
    assert any("F002" in v for v in bad), "终验清零不看是否超期——open 一律点名"


def test_final_foreshadow_sweep_clean_when_nothing_open(tmp_path):
    assert final_foreshadow_sweep(tmp_path, {1, 2, 3}, {}) == []


# ---------- ② 恢复卡 ----------

def test_resume_card_shows_measured_numbers(tmp_path):
    import run_batch
    workdir = os.path.join(str(tmp_path), "ws")
    os.makedirs(os.path.join(workdir, "state"), exist_ok=True)
    emit_event(workdir, "plant", ep=1, id="F001", text="碎瓷片上的血迹", payoffPlannedEp=3)
    card = run_batch.render_resume_card(workdir, completed_eps=[1, 2], pending_eps=[3],
                                       title="战神归来", f2_unresolved=0)
    assert "《战神归来》" in card
    assert "已完成：2/3 集" in card
    assert "未回收伏笔：1 条" in card
    assert "第 3 集" in card


def test_resume_card_flags_pending_claim_evidence(tmp_path):
    import run_batch
    workdir = os.path.join(str(tmp_path), "ws")
    os.makedirs(os.path.join(workdir, "state"), exist_ok=True)
    card = run_batch.render_resume_card(workdir, [1], [2, 3], "测试剧", f2_unresolved=2)
    assert "认领待补证据" in card and "2" in card


def test_resume_card_replaces_legacy_breakpoint_line(tmp_path):
    outline = json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_outline.json")))
    workdir, outline_path = _seed(tmp_path, outline)
    res = _run(workdir, outline_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[断点检测]" not in res.stdout, "旧的断点检测长行应被恢复卡取代"
    assert "已完成：3/3 集" in res.stdout


# ---------- ③ validate-only 单集校验模式 ----------

def test_validate_only_exposes_ledger_skip_and_keeps_exit_code(tmp_path):
    outline = _outline_with_foreshadow()
    outline_path = os.path.join(str(tmp_path), "outline.json")
    with open(outline_path, "w", encoding="utf-8") as fp:
        json.dump(outline, fp, ensure_ascii=False)
    res = run_py(
        ["run_batch.py", "--outline", outline_path, "--target-seconds", "90",
         "--validate-only", os.path.join("fixtures", "sample_script.json")],
        cwd=ROOT)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "跳过（单集校验模式）" in res.stdout, res.stdout


def test_validate_only_exit_code_driven_by_gates_only(tmp_path):
    """账本里有未回收伏笔，也不影响单集校验的退出码（只由 12 门决定）。"""
    outline = _outline_with_foreshadow()
    outline_path = os.path.join(str(tmp_path), "outline.json")
    with open(outline_path, "w", encoding="utf-8") as fp:
        json.dump(outline, fp, ensure_ascii=False)
    workdir = os.path.join(str(tmp_path), "ws")
    os.makedirs(os.path.join(workdir, "state"), exist_ok=True)
    emit_event(workdir, "plant", ep=1, id="F001", text="碎瓷片上的血迹", payoffPlannedEp=3)
    res = run_py(
        ["run_batch.py", "--outline", outline_path, "--target-seconds", "90",
         "--workdir", workdir, "--validate-only", os.path.join("fixtures", "sample_script.json")],
        cwd=ROOT)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "跳过（单集校验模式）" in res.stdout


# ---------- 生产记录四要素（状态宣告原子化） ----------

def test_production_log_rows_render_four_elements(tmp_path, monkeypatch):
    import sys
    import run_batch
    from dramacraft.outline_lint import compute_outline_hash

    workdir = os.path.join(str(tmp_path), "ws")
    eps_dir = os.path.join(workdir, "state", "episodes")
    os.makedirs(eps_dir, exist_ok=True)
    outline = json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_outline.json")))
    sample = json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_script.json")))
    h = compute_outline_hash(outline)
    for ep in sample["episodes"]:
        c = dict(ep)
        c["_meta"] = {"engineVersion": "1.4.0", "schemaVersion": 2, "outlineHash": h}
        with open(os.path.join(eps_dir, f"ep{ep['ep']:03d}.json"), "w", encoding="utf-8") as fp:
            json.dump(c, fp, ensure_ascii=False)

    monkeypatch.setattr(sys, "argv", [
        "run_batch.py", "--outline", os.path.join(ROOT, "fixtures", "sample_outline.json"),
        "--target-seconds", "90", "--workdir", workdir, "--assemble-only"])
    run_batch.main()

    log = open(os.path.join(workdir, "output", "production_log.md"), encoding="utf-8").read()
    for element in ("旧→新", "因：", "门禁：", "下一步："):
        assert element in log, f"生产记录缺状态宣告要素：{element}"
