#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 P1-1：视觉状态与签发语义。

红测目标（修复前必须失败）：
- `verify-visual` 在空库上返回 rc=0（把「视觉未启用」误当成「视觉交付通过」）；
- 计划存在但权威包缺失时未用 BLOCKED 阻断；
- 历史有效清单在新验证失败时被覆盖 / status 不显示失效原因。

验收矩阵：无计划、计划损坏、风格包缺失、部分任务、全部任务、上游变化、
参考图变化、权威包变化各有测试。核心 verify 行为保持不变（资料线独立）。
"""
import pytest
import json
import sys
from pathlib import Path
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.check_visual_delivery import (  # noqa: E402
    DELIVERY_MANIFEST_NAME,
    build_visual_delivery,
)
from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_planner import plan_visual_tasks  # noqa: E402
from scripts.engine.visual_schema import (  # noqa: E402
    VisualTask,
    load_visual_plan,
    write_visual_plan,
)

STYLE = resolve_style_record("仙侠国风电影CG")
VISUAL_GATE = REPO_ROOT / "scripts" / "check_visual_delivery.py"
MAINTENANCE = REPO_ROOT / "scripts" / "maintenance.py"
EXIT_NOT_ENABLED = 3


def _index():
    return {
        "schema_version": 1, "source_fingerprint": "sha256:demo",
        "assets": [{"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘", "priority": "P0",
                    "fact_level": "剧本事实",
                    "evidence": [{"segment": "段01.md", "line": "00001", "quote": "林姑娘走进茶楼"}],
                    "relations": [], "dependencies": [], "status": "CONFIRMED"}],
    }


def _lib(tmp_path, plan=True):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(_index(), ensure_ascii=False), encoding="utf-8")
    if plan:
        write_visual_plan(plan_visual_tasks(_index(), STYLE), lib / "视觉资产" / "计划.toml")
    return lib


def _handwrite_accept_all(lib):
    """**绕过发布器**手写状态 + 直接放图片（用于验证交付门不再认可这条路径）。"""
    import hashlib

    from scripts.engine.visual_schema import AttemptRecord
    from scripts.engine.visual_store import attempt_dir

    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    plan_path = lib / "视觉资产" / "计划.toml"
    for task in load_visual_plan(plan_path).tasks:
        rel = f"{attempt_dir(task.task_id, 1)}/image.png"
        target = lib / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png)
        plan = load_visual_plan(plan_path)
        task_now = plan.by_id(task.task_id)
        attempt = AttemptRecord(round=1, image=rel,
                                image_sha256=hashlib.sha256(png).hexdigest(),
                                model="manual", provider="manual")
        write_visual_plan(plan.with_task(VisualTask(
            **{**task_now.__dict__, "status": "READY_AS_REFERENCE", "attempts": (attempt,)})),
            plan_path)


def _accept_all(lib):
    """走发布器的正式通过（阶段 2 起「已通过」必须来自发布器）。"""
    from conftest import publish_all_with_reviews

    return publish_all_with_reviews(lib)


def _cli(script, *args):
    import os

    return run_py(
            [str(script), *[str(a) for a in args]],
            cwd=str(REPO_ROOT),
            env={**os.environ, "ASSET_WORKERS": "4"},
    )


# --- 1. 无计划：必须 NOT_ENABLED 且非零 -------------------------------------------

def test_no_plan_is_not_enabled_and_nonzero(tmp_path):
    lib = _lib(tmp_path, plan=False)
    r = _cli(VISUAL_GATE, "--lib", lib, "--json")
    payload = json.loads(r.stdout)
    assert payload["status"] == "NOT_ENABLED"
    assert payload["reason_code"] == "VISUAL_NOT_ENABLED"
    assert r.returncode == EXIT_NOT_ENABLED, f"空库必须非零退出，实际 rc={r.returncode}"


def test_no_plan_never_signs_manifest(tmp_path):
    lib = _lib(tmp_path, plan=False)
    _cli(VISUAL_GATE, "--lib", lib)
    assert not (lib / "视觉资产" / DELIVERY_MANIFEST_NAME).is_file()


def test_maintenance_verify_visual_nonzero_on_empty_lib(tmp_path):
    lib = _lib(tmp_path, plan=False)
    r = _cli(MAINTENANCE, "verify-visual", "--lib", lib, "--json")
    assert r.returncode != 0, "空库 verify-visual 不得以 0 退出"
    payload = json.loads(r.stdout)
    assert payload["reason_code"] == "VISUAL_NOT_ENABLED"


# --- 2. 计划损坏：BLOCKED ---------------------------------------------------------

def test_broken_plan_is_blocked(tmp_path):
    lib = _lib(tmp_path)
    (lib / "视觉资产" / "计划.toml").write_text("这不是合法 TOML [[[ ", encoding="utf-8")
    result = build_visual_delivery(lib, write=False)
    assert result["status"] == "BLOCKED"
    assert result["blocking"]


def test_broken_plan_cli_nonzero(tmp_path):
    lib = _lib(tmp_path)
    (lib / "视觉资产" / "计划.toml").write_text("破坏", encoding="utf-8")
    r = _cli(VISUAL_GATE, "--lib", lib)
    assert r.returncode != 0


# --- 3. 风格包缺失：BLOCKED（不得用 NOT_ENABLED 逃逸） -----------------------------

def test_missing_stylepack_is_blocked_not_not_enabled(tmp_path, monkeypatch):
    """权威包缺图时必须是 BLOCKED，不得用 NOT_ENABLED 逃逸。"""
    import engine.visual_request_chain as gate_chain

    lib = _lib(tmp_path)
    monkeypatch.setattr(gate_chain, "stylepack_integrity_issues",
                        lambda root, require_images=True: ["清单文件缺失：references/face.png"])
    result = build_visual_delivery(lib, write=False)
    assert result["status"] == "BLOCKED"
    assert result["status"] != "NOT_ENABLED", "参考包缺失不得用 NOT_ENABLED 逃逸"
    assert any("风格包" in item or "权威" in item for item in result["blocking"])


def test_stylepack_hash_mismatch_blocks(tmp_path):
    """权威包内容变化（哈希不符）必须阻断。"""
    lib = _lib(tmp_path)
    plan_path = lib / "视觉资产" / "计划.toml"
    plan = load_visual_plan(plan_path)
    tampered = type(plan)(**{**plan.__dict__, "style_hash": "0" * 64})
    write_visual_plan(tampered, plan_path)
    result = build_visual_delivery(lib, write=False)
    assert result["status"] == "BLOCKED"
    assert any("哈希" in item for item in result["blocking"])


# --- 4. 部分任务 / 全部任务 -------------------------------------------------------

def test_partial_tasks_block(tmp_path):
    lib = _lib(tmp_path)
    result = build_visual_delivery(lib, write=False)
    assert result["status"] == "BLOCKED"
    assert result["passed"] < result["tasks"]


@pytest.mark.requires_private_stylepack
def test_all_tasks_deliver_and_sign(tmp_path):
    lib = _lib(tmp_path)
    _accept_all(lib)
    result = build_visual_delivery(lib)
    assert result["status"] == "DELIVERED", result["blocking"]
    assert (lib / "视觉资产" / DELIVERY_MANIFEST_NAME).is_file()


@pytest.mark.requires_private_stylepack
def test_delivered_cli_returns_zero(tmp_path):
    lib = _lib(tmp_path)
    _accept_all(lib)
    r = _cli(VISUAL_GATE, "--lib", lib)
    assert r.returncode == 0, r.stdout + r.stderr


# --- 5. 上游变化 / 参考图变化 ------------------------------------------------------

def test_upstream_change_blocks(tmp_path):
    """上游被置回未通过时，已通过的下游必须报依赖过期。"""
    lib = _lib(tmp_path)
    _accept_all(lib)
    plan_path = lib / "视觉资产" / "计划.toml"
    plan = load_visual_plan(plan_path)
    target = next(t for t in plan.tasks if t.depends_on)
    tasks = tuple(VisualTask(**{**t.__dict__, "status": "PLANNED"})
                  if t.task_id == target.depends_on[0] else t for t in plan.tasks)
    write_visual_plan(type(plan)(**{**plan.__dict__, "tasks": tasks}), plan_path)
    result = build_visual_delivery(lib, write=False)
    assert result["status"] == "BLOCKED"
    assert any("上游" in item or "依赖" in item for item in result["blocking"])


def test_reference_image_change_is_detected(tmp_path):
    """已通过产物图片内容变化（哈希漂移）必须被检出并阻断签发。"""
    import hashlib

    from scripts.engine.visual_schema import AttemptRecord
    from scripts.engine.visual_store import attempt_dir

    lib = _lib(tmp_path)
    plan_path = lib / "视觉资产" / "计划.toml"
    plan = load_visual_plan(plan_path)
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    for task in plan.tasks:
        rel = f"{attempt_dir(task.task_id, 1)}/image.png"
        target = lib / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png)
        attempt = AttemptRecord(round=1, image=rel,
                                image_sha256=hashlib.sha256(png).hexdigest(),
                                model="manual", provider="manual")
        plan = load_visual_plan(plan_path)
        task_now = plan.by_id(task.task_id)
        write_visual_plan(plan.with_task(VisualTask(
            **{**task_now.__dict__, "status": "READY_AS_REFERENCE", "attempts": (attempt,)})),
            plan_path)

    # 换掉一张已通过图片的字节（哈希与记录不再一致）
    victim = load_visual_plan(plan_path).tasks[0]
    (lib / victim.latest_attempt.image).write_bytes(png + b"tampered")
    result = build_visual_delivery(lib, write=False)
    assert result["status"] == "BLOCKED"
    assert any("哈希" in item or "参考" in item or "产物" in item
               for item in result["blocking"]), result["blocking"]


# --- 6. 权威包变化 ---------------------------------------------------------------

def test_authority_pack_change_blocks(tmp_path):
    """权威包内容变化必须使计划过期（哈希不再匹配登记表）。"""
    lib = _lib(tmp_path)
    _accept_all(lib)
    plan_path = lib / "视觉资产" / "计划.toml"
    plan = load_visual_plan(plan_path)
    write_visual_plan(type(plan)(**{**plan.__dict__, "style_hash": "f" * 64}), plan_path)
    result = build_visual_delivery(lib, write=False)
    assert result["status"] == "BLOCKED"
    assert any("哈希" in item for item in result["blocking"])


# --- 7. 历史清单不被覆盖，但 status 显示失效原因 -----------------------------------

@pytest.mark.requires_private_stylepack
def test_failed_revalidation_keeps_history_and_reports_reason(tmp_path):
    lib = _lib(tmp_path)
    _accept_all(lib)
    build_visual_delivery(lib)
    manifest_path = lib / "视觉资产" / DELIVERY_MANIFEST_NAME
    assert manifest_path.is_file()
    original = manifest_path.read_bytes()

    # 让计划失效（上游回退）
    plan_path = lib / "视觉资产" / "计划.toml"
    plan = load_visual_plan(plan_path)
    target = next(t for t in plan.tasks if t.depends_on)
    tasks = tuple(VisualTask(**{**t.__dict__, "status": "PLANNED"})
                  if t.task_id == target.depends_on[0] else t for t in plan.tasks)
    write_visual_plan(type(plan)(**{**plan.__dict__, "tasks": tasks}), plan_path)

    result = build_visual_delivery(lib, write=True)
    assert result["status"] == "BLOCKED"
    assert manifest_path.read_bytes() == original, "失败验证不得覆盖历史有效清单"
    assert result["reason"] or result["blocking"], "必须显示失效原因"


def test_status_json_shows_visual_invalid_reason(tmp_path):
    """status 必须显示视觉线失效原因（不是只报 NOT_ENABLED）。"""
    lib = _lib(tmp_path)
    _accept_all(lib)
    build_visual_delivery(lib)
    plan_path = lib / "视觉资产" / "计划.toml"
    plan = load_visual_plan(plan_path)
    write_visual_plan(type(plan)(**{**plan.__dict__, "style_hash": "f" * 64}), plan_path)
    r = _cli(MAINTENANCE, "status", "--lib", lib, "--json")
    payload = json.loads(r.stdout)
    visual = payload["visual_delivery"]
    assert visual["state"] == "BLOCKED"
    assert visual.get("reason") or visual.get("detail")


# --- 8. 核心 verify 不受影响 ------------------------------------------------------

def test_core_verify_does_not_call_visual_gate(tmp_path):
    """资料线核心 verify 不得**直接依赖**视觉门（两线独立）。

    工单 13 起视觉审核成为签发契约里的一项独立检查（engine.delivery_checks.
    check_visual_review），只在产品结果含生图时参与；本用例钉住「do_verify 本体
    不碰任何视觉入口」，行为面见 test_asset_compiler_cli.py。
    """
    import re

    src = (REPO_ROOT / "scripts" / "maintenance.py").read_text(encoding="utf-8")
    parts = re.split(r"^def do_verify\(", src, flags=re.M)
    assert len(parts) == 2
    body = parts[1].split("\ndef ", 1)[0]
    for forbidden in ("check_visual_delivery", "build_visual_delivery", "visual_scheduler",
                      "visual_production", "load_visual_plan", "visual_delivery_state"):
        assert forbidden not in body, f"do_verify 不得直接依赖 {forbidden}"


# --- 工单 0.2 红测：复制图片 / 手改状态不得绕过发布器 ------------------------------

def test_copied_image_and_handwritten_status_cannot_pass_gate(tmp_path):
    """把图片直接放进轮次目录并把计划状态改成「已通过」不能换来交付签发。

    阶段 2 工单 2.3/2.4 已实现：交付门交叉验证正式版本清单，缺发布记录即阻断。
    """
    lib = _lib(tmp_path)
    _handwrite_accept_all(lib)
    result = build_visual_delivery(lib, write=False)
    assert result["status"] != "DELIVERED", "手写状态 + 直接放图不得通过交付门"
    assert any("PUBLISH_RELEASE_MISSING" in item or "发布" in item
               for item in result["blocking"]), result["blocking"]


def test_markdown_report_cannot_change_machine_state(tmp_path):
    """Markdown 报告只由机器状态渲染，反向修改 Markdown 不产生任何事实。

    这是「不得用 Markdown 的『通过』反向修改机器状态」的常驻回归守卫。
    """
    lib = _lib(tmp_path)
    plan_path = lib / "视觉资产" / "计划.toml"
    before = plan_path.read_bytes()

    md = lib / "视觉资产" / "计划阅读版.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("# 视觉计划\n\n全部任务已通过，视觉交付完成。\n", encoding="utf-8")

    assert plan_path.read_bytes() == before, "手改 Markdown 不得改动计划 TOML"
    result = build_visual_delivery(lib, write=False)
    assert result["status"] != "DELIVERED"
    assert result["passed"] == 0, "Markdown 里的『已通过』不改变机器判定"
