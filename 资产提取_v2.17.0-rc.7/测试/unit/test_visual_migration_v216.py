#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 7 工单 7.1：第一集旧库非破坏迁移预演器（规格十二）。

三条命令：

- `scan`：只读登记现有图片（路径/字节/哈希/MIME/尺寸），为每项任务算候选匹配分数；
  歧义不自动绑定。
- `apply`：只对唯一匹配的候选做**复制**（进任务轮次目录、登记为历史候选），
  绝不覆盖、移动、删除用户原文件，也不自动通过。
- `verify`：与迁移前清单比对，确认原始路径与哈希未变、副本与原图一致。
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest
from PIL import Image
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.visual_schema import (  # noqa: E402
    VisualPlan,
    VisualTask,
    load_visual_plan,
    write_visual_plan,
)
from scripts.migrate_visual_library import (  # noqa: E402
    MigrationError,
    apply_proposal,
    scan_library,
    verify_migration,
)

STYLE = dict(style_slug="仙侠国风电影CG", style_version="1.0.0", style_hash="a" * 64)
TASK_ID = "CH_老罗-正脸-v01"


def _png(seed: int, size=(8, 8)) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", size, (seed % 256, (seed * 5) % 256, (seed * 11) % 256)).save(
        buffer, format="PNG")
    return buffer.getvalue()


def _task(task_id=TASK_ID, **kw):
    base = dict(task_id=task_id, asset_id="CH_老罗", kind="正脸", status="PLANNED",
                aspect_ratio="1:1", fact_source=".process/资产索引.json#CH_老罗",
                **STYLE)
    base.update(kw)
    return VisualTask(**base)


def _lib(tmp_path, tasks=None, images=None):
    """造一个「旧库」：图片放在 视觉资产/ 与 参考输入/ 下，另有一份视觉计划。"""
    lib = tmp_path / "资产反推_第01集"
    (lib / ".process").mkdir(parents=True)
    for rel, seed in (images or {}).items():
        target = lib / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_png(seed))
    write_visual_plan(VisualPlan(tasks=tuple(tasks or (_task(),))),
                      lib / "视觉资产" / "计划.toml")
    return lib


def _state_file(rel: str) -> bool:
    """库状态文件（按设计会被 apply 重写）；非破坏承诺保护的是用户原图与参考输入。"""
    return rel.endswith("计划.toml") or rel.endswith("迁移记录.json")


def _tree_state(root: Path) -> dict:
    """目录快照（相对路径 → 哈希），用于证明「只读」与「非破坏」。"""
    state = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            state[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return state


# --- scan ------------------------------------------------------------------------

def test_scan_registers_images_and_decides_each_task(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1,
                                 "参考输入/人物/老罗-身份参考.png": 2})
    proposal = scan_library(lib)
    assert proposal["scan_mode"] == "read_only"
    assert proposal["counts"]["images"] == 2
    assert proposal["buckets"] == {"视觉资产": 1, "参考输入": 1}
    row = next(f for f in proposal["files"] if f["path"] == "视觉资产/老罗/老罗-正脸.png")
    assert row["sha256"] == hashlib.sha256(_png(1)).hexdigest()
    assert row["bytes"] == len(_png(1)) and row["mime"] == "image/png"
    assert row["width"] == 8 and row["height"] == 8
    task_row = proposal["tasks"][0]
    assert task_row["task_id"] == TASK_ID
    assert task_row["decision"] == "MATCHED"
    assert task_row["chosen"] == "视觉资产/老罗/老罗-正脸.png"


def test_scan_is_read_only(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1})
    before = _tree_state(lib)
    scan_library(lib)
    assert _tree_state(lib) == before, "扫描不得改动资产库内任何文件"


def test_ambiguous_match_is_not_auto_bound(tmp_path):
    """两个同分候选 → 歧义：不自动绑定，必须留给人工/主模型决定。"""
    lib = _lib(tmp_path, images={"视觉资产/老罗/正脸-甲.png": 1,
                                 "视觉资产/老罗/正脸-乙.png": 2})
    proposal = scan_library(lib)
    row = proposal["tasks"][0]
    assert row["decision"] == "AMBIGUOUS", row
    assert row["chosen"] == ""
    assert len(row["candidates"]) == 2


def test_unmatched_task_has_no_chosen_candidate(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/茶楼/茶楼-全景.png": 1})
    proposal = scan_library(lib)
    row = proposal["tasks"][0]
    assert row["decision"] == "UNMATCHED"
    assert row["chosen"] == ""


def test_scan_uses_existing_attempt_records_as_strongest_evidence(tmp_path):
    """计划里已经引用过的图片是最强证据（老库常见形态）。"""
    lib = _lib(tmp_path, images={"视觉资产/任务/CH_老罗-正脸-v01/attempt-01/image.png": 3})
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    from scripts.engine.visual_schema import AttemptRecord

    task = plan.by_id(TASK_ID)
    image_rel = "视觉资产/任务/CH_老罗-正脸-v01/attempt-01/image.png"
    write_visual_plan(plan.with_task(VisualTask(**{
        **task.__dict__,
        "status": "WAITING_REVIEW",
        "attempts": (AttemptRecord(round=1, image=image_rel,
                                   image_sha256=hashlib.sha256(_png(3)).hexdigest()),),
    })), lib / "视觉资产" / "计划.toml")
    proposal = scan_library(lib)
    row = proposal["tasks"][0]
    assert row["decision"] == "MATCHED"
    assert row["chosen"].endswith("attempt-01/image.png")


def test_scan_without_plan_reports_no_tasks(tmp_path):
    lib = tmp_path / "资产库"
    (lib / "视觉资产").mkdir(parents=True)
    (lib / "视觉资产" / "老罗-正脸.png").write_bytes(_png(1))
    proposal = scan_library(lib)
    assert proposal["tasks"] == []
    assert proposal["counts"]["images"] == 1


# --- apply -----------------------------------------------------------------------

def _apply(lib, proposal):
    return apply_proposal(lib, proposal)


def test_apply_copies_only_matched_candidates(tmp_path):
    lib = _lib(tmp_path, tasks=(_task(), _task(task_id="CH_茶楼-场景母版-v01",
                                               asset_id="CH_茶楼", kind="场景母版")),
               images={"视觉资产/人物/老罗/老罗_EP01正脸身份近景_3x4.png": 1,
                       "视觉资产/场景/茶楼_场景母版_16x9.png": 2})
    source_before = _tree_state(lib)
    proposal = scan_library(lib)
    summary = _apply(lib, proposal)
    assert summary["copied"] == 2, summary
    for task_id in (TASK_ID, "CH_茶楼-场景母版-v01"):
        task = load_visual_plan(lib / "视觉资产" / "计划.toml").by_id(task_id)
        assert len(task.attempts) == 1
        record = task.attempts[0]
        copied = lib / record.image
        assert copied.is_file(), "候选必须复制进任务轮次目录"
        index = [t["task_id"] for t in proposal["tasks"]].index(task_id)
        source_rel = proposal["tasks"][index]["chosen"]
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == \
            hashlib.sha256((lib / source_rel).read_bytes()).hexdigest(), "副本必须与原图一致"
        assert task.status == "NEEDS_REVIEW", "迁移登记后必须等主模型复审，不得自动通过"
    after = _tree_state(lib)
    for rel, digest in source_before.items():
        if rel.startswith("视觉资产/任务/") or _state_file(rel):
            continue
        assert after.get(rel) == digest, f"原文件被改动：{rel}"


def test_apply_never_touches_source_files_or_reference_inputs(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1,
                                 "参考输入/人物/老罗-身份参考.png": 2})
    before = _tree_state(lib)
    proposal = scan_library(lib)
    _apply(lib, proposal)
    after = _tree_state(lib)
    for rel, digest in before.items():
        if "/任务/" in rel or _state_file(rel):
            continue
        assert after.get(rel) == digest, f"非破坏承诺被破坏：{rel}"


def test_apply_is_idempotent(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1})
    proposal = scan_library(lib)
    first = _apply(lib, proposal)
    assert first["copied"] == 1
    second = _apply(lib, proposal)
    assert second["copied"] == 0 and second["already"] == 1
    task = load_visual_plan(lib / "视觉资产" / "计划.toml").by_id(TASK_ID)
    assert len(task.attempts) == 1, "重复执行不得新增轮次"


def test_apply_skips_ambiguous_and_unmatched(tmp_path):
    lib = _lib(tmp_path, tasks=(_task(), _task(task_id="CH_茶楼-场景母版-v01",
                                               asset_id="CH_茶楼", kind="场景母版")),
               images={"视觉资产/老罗/正脸-甲.png": 1, "视觉资产/老罗/正脸-乙.png": 2})
    proposal = scan_library(lib)
    summary = _apply(lib, proposal)
    assert summary["copied"] == 0
    assert summary["skipped"], "歧义与未匹配必须在报告里列出"
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    for task in plan.tasks:
        assert task.attempts == (), "歧义候选不得自动绑定"
        assert task.status == "PLANNED"


def test_apply_writes_a_machine_readable_migration_log(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1})
    proposal = scan_library(lib)
    _apply(lib, proposal)
    log_path = lib / "视觉资产" / "迁移记录.json"
    assert log_path.is_file(), "迁移必须有机器可读记录（供阶段 7.3 报告渲染）"
    log = json.loads(log_path.read_text(encoding="utf-8"))
    assert log["applied"][0]["task_id"] == TASK_ID
    assert log["applied"][0]["source"] == "视觉资产/老罗/老罗-正脸.png"
    assert log["applied"][0]["round"] == 1


def test_apply_rejects_a_proposal_without_scan_mode(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1})
    with pytest.raises(MigrationError) as excinfo:
        _apply(lib, {"tasks": []})
    assert excinfo.value.code == "MIGRATION_PROPOSAL_UNREADABLE"


# --- verify ----------------------------------------------------------------------

def test_verify_passes_on_untouched_library(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1})
    baseline = scan_library(lib)
    proposal = scan_library(lib)
    _apply(lib, proposal)
    issues = verify_migration(lib, baseline)
    assert issues == [], issues


def test_verify_detects_a_modified_source_image(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1})
    baseline = scan_library(lib)
    (lib / "视觉资产" / "老罗" / "老罗-正脸.png").write_bytes(_png(99))
    issues = verify_migration(lib, baseline)
    assert [i["code"] for i in issues] == ["MIGRATION_SOURCE_CHANGED"]
    assert issues[0]["path"] == "视觉资产/老罗/老罗-正脸.png"


def test_verify_detects_a_missing_source_image(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1})
    baseline = scan_library(lib)
    (lib / "视觉资产" / "老罗" / "老罗-正脸.png").unlink()
    issues = verify_migration(lib, baseline)
    assert [i["code"] for i in issues] == ["MIGRATION_SOURCE_MISSING"]


def test_verify_detects_a_tampered_copy(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1})
    baseline = scan_library(lib)
    proposal = scan_library(lib)
    _apply(lib, proposal)
    task = load_visual_plan(lib / "视觉资产" / "计划.toml").by_id(TASK_ID)
    (lib / task.attempts[0].image).write_bytes(_png(77))
    issues = verify_migration(lib, baseline)
    assert "MIGRATION_COPY_MISMATCH" in [i["code"] for i in issues]


def test_verify_rejects_an_unreadable_baseline(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1})
    with pytest.raises(MigrationError) as excinfo:
        verify_migration(lib, tmp_path / "不存在.json")
    assert excinfo.value.code == "MIGRATION_BASELINE_UNREADABLE"


# --- CLI 接线（子进程，真实退出码） -----------------------------------------------

MIGRATE_CLI = REPO_ROOT / "scripts" / "migrate_visual_library.py"


def _cli(*args):
    import os

    env = {**os.environ}
    env.pop("GEMINI_API_KEY", None)
    return run_py([str(MIGRATE_CLI), *[str(a) for a in args]], cwd=str(REPO_ROOT), env=env)


def test_cli_scan_writes_proposal_and_leaves_library_untouched(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1})
    before = _tree_state(lib)
    out = tmp_path / "迁移提案.json"
    proc = _cli("scan", "--lib", lib, "--out", out)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.is_file() and "只读扫描完成" in proc.stdout
    assert _tree_state(lib) == before
    proposal = json.loads(out.read_text(encoding="utf-8"))
    assert proposal["counts"]["matched"] == 1


def test_cli_apply_then_verify_round_trip(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1})
    baseline = tmp_path / "迁移前清单.json"
    baseline.write_text(json.dumps(scan_library(lib), ensure_ascii=False),
                        encoding="utf-8")
    proposal = tmp_path / "迁移提案.json"
    assert _cli("scan", "--lib", lib, "--out", proposal).returncode == 0
    applied = _cli("apply", "--lib", lib, "--proposal", proposal)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "复制 1 项" in applied.stdout
    verified = _cli("verify", "--lib", lib, "--baseline", baseline)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "校验通过" in verified.stdout


def test_cli_verify_reports_source_change_with_nonzero_exit(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/老罗/老罗-正脸.png": 1})
    baseline = tmp_path / "迁移前清单.json"
    baseline.write_text(json.dumps(scan_library(lib), ensure_ascii=False),
                        encoding="utf-8")
    (lib / "视觉资产" / "老罗" / "老罗-正脸.png").write_bytes(_png(42))
    proc = _cli("verify", "--lib", lib, "--baseline", baseline)
    assert proc.returncode == 1
    assert "MIGRATION_SOURCE_CHANGED" in proc.stdout


# --- 方向感知与身份证据（对着真实老库命名固化） ---------------------------------

def test_asset_name_must_be_on_a_name_boundary(tmp_path):
    """`栓子` 不得匹配 `栓子娘` 的文件（子串不是身份证据）。"""
    lib = _lib(tmp_path, tasks=(_task(task_id="CH_栓子-正脸-v01", asset_id="CH_栓子"),),
               images={"视觉资产/人物/栓子/栓子_EP01正脸身份近景_1x1.png": 1,
                       "视觉资产/人物/栓子娘/栓子娘_EP01正脸身份近景_1x1.png": 2})
    row = scan_library(lib)["tasks"][0]
    assert row["decision"] == "MATCHED"
    assert "栓子娘" not in row["chosen"], "不得把别人娘亲的图当成栓子的正脸"


def test_candidate_without_asset_evidence_cannot_be_auto_bound(tmp_path):
    """只有「同类图」（路径里没有该资产名）时不得自动绑定，只能记未匹配。"""
    task = _task(task_id="CH_村民-人物全身正面-v01", asset_id="CH_村民",
                 kind="人物全身正面")
    lib = _lib(tmp_path, tasks=(task,),
               images={"视觉资产/人物/二柱/二柱_EP01高精度CG全身立绘_2x3.png": 1})
    row = scan_library(lib)["tasks"][0]
    assert row["decision"] == "UNMATCHED", row
    assert row["chosen"] == ""


def test_direction_conflict_drops_the_candidate(tmp_path):
    """方向冲突词必须把候选压到下限以下：全身正面任务不该绑定背面图。"""
    task = _task(task_id="CH_老罗-人物全身正面-v01", asset_id="CH_老罗",
                 kind="人物全身正面")
    lib = _lib(tmp_path, tasks=(task,),
               images={"视觉资产/人物/老罗/老罗_EP01白衣背面正式立绘_2x3.png": 1})
    row = scan_library(lib)["tasks"][0]
    assert row["decision"] == "UNMATCHED", row
    assert row["candidates"][0]["score"] < 30


def test_formal_variant_wins_over_plain_variant(tmp_path):
    """同名多代产物：带「正式／高精度CG」的版本分更高，可唯一绑定。"""
    task = _task(task_id="CH_村民-人物全身正面-v01", asset_id="CH_村民",
                 kind="人物全身正面")
    lib = _lib(tmp_path, tasks=(task,),
               images={"视觉资产/人物/村民/村民_EP01高精度CG通用正式正面立绘_2x3.png": 1,
                       "视觉资产/人物/村民/村民_EP01高精度CG通用立绘_2x3.png": 2,
                       "视觉资产/人物/村民/村民_EP01立绘_2x3.png": 3})
    row = scan_library(lib)["tasks"][0]
    assert row["decision"] == "MATCHED", row
    assert row["chosen"].endswith("村民_EP01高精度CG通用正式正面立绘_2x3.png")


# --- 真实库只读提案夹具（第一集） ------------------------------------------------

FROZEN_PROPOSAL = (REPO_ROOT / "测试" / "fixtures" / "visual_regression"
                   / "第一集-迁移提案.json")


def test_frozen_first_episode_proposal_is_self_consistent():
    """夹具完整性：计数与逐条数据一致、匹配路径都来自登记清单、无异机绝对路径。"""
    text = FROZEN_PROPOSAL.read_text(encoding="utf-8")
    assert ":\\" not in text and ":/" not in text, "夹具里不得出现绝对路径"
    data = json.loads(text)
    assert data["scan_mode"] == "read_only"
    assert data["file_count"] == len(data["files"])
    assert data["counts"]["images"] == len(data["files"])
    assert data["counts"]["tasks"] == len(data["tasks"])
    known = {row["path"] for row in data["files"]}
    for row in data["tasks"]:
        if row["decision"] == "MATCHED":
            assert row["chosen"] in known, row["task_id"]
        else:
            assert row["chosen"] == ""
    assert data["counts"]["matched"] + data["counts"]["ambiguous"] + \
        data["counts"]["unmatched"] == data["counts"]["tasks"]


def test_frozen_proposal_records_the_two_genuine_ambiguities():
    """真实库只读扫描的结论：31 项唯一匹配、2 项「同名两代产物」必须留给人决定。"""
    data = json.loads(FROZEN_PROPOSAL.read_text(encoding="utf-8"))
    ambiguous = sorted(t["task_id"] for t in data["tasks"] if t["decision"] == "AMBIGUOUS")
    assert ambiguous == ["CH_二柱-人物全身正面-v01", "CH_刁三-人物全身正面-v01"], ambiguous
    assert data["counts"]["unmatched"] == 0


# --- 验收报告渲染（工单 7.3 的工程侧） -------------------------------------------

sys.path.insert(0, str(REPO_ROOT / "测试"))
import run_visual_migration_acceptance as acceptance  # noqa: E402


def test_acceptance_report_renders_from_machine_state(tmp_path):
    lib = _lib(tmp_path, images={"视觉资产/人物/老罗/老罗_EP01正脸身份近景_3x4.png": 1})
    proposal = tmp_path / "提案.json"
    proposal.write_text(json.dumps(scan_library(lib), ensure_ascii=False),
                        encoding="utf-8")
    baseline = tmp_path / "清单.json"
    baseline.write_text(json.dumps(scan_library(lib), ensure_ascii=False),
                        encoding="utf-8")
    state = acceptance.collect_state(lib, proposal, baseline)
    report = acceptance.render_report(state)
    assert "第一集视觉迁移验收报告" in report
    assert "自动唯一匹配 | 1" in report
    assert "NOT_RUN" in report, "内容审核未做时必须如实标注"
    assert "视觉内容待审核" in report
    assert "工程候选" in report
    assert state["orphan_images"] == [] and state["verify_issues"] == []


def test_acceptance_writes_report_and_exits_zero(tmp_path):
    import os

    lib = _lib(tmp_path, images={"视觉资产/人物/老罗/老罗_EP01正脸身份近景_3x4.png": 1})
    proposal = tmp_path / "提案.json"
    proposal.write_text(json.dumps(scan_library(lib), ensure_ascii=False),
                        encoding="utf-8")
    baseline = tmp_path / "清单.json"
    baseline.write_text(json.dumps(scan_library(lib), ensure_ascii=False),
                        encoding="utf-8")
    out = tmp_path / "验收.md"
    env = {**os.environ}
    env.pop("GEMINI_API_KEY", None)
    proc = run_py(
            [str(REPO_ROOT / "测试" / "run_visual_migration_acceptance.py"), "--lib", str(lib), "--proposal", str(proposal), "--baseline", str(baseline), "--out", str(out)],
            cwd=str(REPO_ROOT),
            env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "NOT_RUN" in text and "工程侧自动门" in text


def test_acceptance_flags_a_modified_source_image(tmp_path):
    import os

    lib = _lib(tmp_path, images={"视觉资产/人物/老罗/老罗_EP01正脸身份近景_3x4.png": 1})
    proposal = tmp_path / "提案.json"
    proposal.write_text(json.dumps(scan_library(lib), ensure_ascii=False),
                        encoding="utf-8")
    baseline = tmp_path / "清单.json"
    baseline.write_text(json.dumps(scan_library(lib), ensure_ascii=False),
                        encoding="utf-8")
    (lib / "视觉资产/人物/老罗/老罗_EP01正脸身份近景_3x4.png").write_bytes(_png(55))
    env = {**os.environ}
    env.pop("GEMINI_API_KEY", None)
    proc = run_py(
            [str(REPO_ROOT / "测试" / "run_visual_migration_acceptance.py"), "--lib", str(lib), "--proposal", str(proposal), "--baseline", str(baseline), "--check"],
            cwd=str(REPO_ROOT),
            env=env,
    )
    assert proc.returncode == 1, "原图被改必须非零"
    assert "原始图片哈希变化 | 不通过" in proc.stdout


def test_acceptance_cli_takes_lib_from_env_when_flag_absent(tmp_path):
    """施工单 8.5 的验收命令不带 `--lib`：必须能吃 ASSET_LIB_DIR（仓库其余 CLI 的既有约定）。"""
    import os

    lib = _lib(tmp_path, images={"视觉资产/人物/老罗/老罗_EP01正脸身份近景_3x4.png": 1})
    proposal = tmp_path / "提案.json"
    proposal.write_text(json.dumps(scan_library(lib), ensure_ascii=False),
                        encoding="utf-8")
    baseline = tmp_path / "清单.json"
    baseline.write_text(json.dumps(scan_library(lib), ensure_ascii=False),
                        encoding="utf-8")
    out = tmp_path / "验收.md"
    env = {**os.environ, "ASSET_LIB_DIR": str(lib)}
    env.pop("GEMINI_API_KEY", None)
    proc = run_py(
            [str(REPO_ROOT / "测试" / "run_visual_migration_acceptance.py"), "--proposal", str(proposal), "--baseline", str(baseline), "--out", str(out)],
            cwd=str(REPO_ROOT),
            env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.is_file()
    assert "NOT_RUN" in out.read_text(encoding="utf-8")


def test_acceptance_cli_refuses_a_library_that_is_not_there(tmp_path):
    """库路径不存在时**不得**渲染出「全部通过」：那会在零数据上假通过。"""
    import os

    missing = tmp_path / "不存在的资产库"
    env = {**os.environ}
    env.pop("GEMINI_API_KEY", None)
    env.pop("ASSET_LIB_DIR", None)
    proc = run_py(
            [str(REPO_ROOT / "测试" / "run_visual_migration_acceptance.py"), "--lib", str(missing), "--check"],
            cwd=str(REPO_ROOT),
            env=env,
    )
    text = proc.stdout + proc.stderr
    assert proc.returncode == 2, text
    assert "ASSET_LIB_DIR" in text, "必须给出可照做的配置指令"
    assert "全部通过" not in text
