#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单04-Task5：离线视觉质量门单测（规格 8/9，替代性快速回归）。

`测试/run_visual_offline.py` 是完整离线端到端；本文件把其中的关键契约固化为快速单测，
保证即使端到端脚本被改动，核心质量门行为仍被钉死。
"""
import pytest
import json
import sys
from pathlib import Path
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.check_visual_delivery import build_visual_delivery  # noqa: E402
from scripts.engine.image_qc import BLOCKING, parse_qc_response  # noqa: E402
from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_pipeline import propagate_upstream_change  # noqa: E402
from scripts.engine.visual_planner import plan_visual_tasks  # noqa: E402
from scripts.engine.visual_schema import (  # noqa: E402
    AttemptRecord,
    VisualPlan,
    VisualTask,
    load_visual_plan,
    validate_visual_plan,
    write_visual_plan,
)

STYLE = resolve_style_record("仙侠国风电影CG")
OFFLINE = REPO_ROOT / "测试" / "run_visual_offline.py"
INDEX = {
    "schema_version": 1, "source_fingerprint": "sha256:qg",
    "assets": [{"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘", "priority": "P1",
                "fact_level": "剧本事实",
                "evidence": [{"segment": "段01.md", "line": "00001", "quote": "林姑娘走进茶楼"}],
                "relations": [], "dependencies": [], "status": "CONFIRMED"}],
}


def _lib(tmp_path, accepted=False):
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    index_path = lib / ".process" / "资产索引.json"
    index_path.write_text(json.dumps(INDEX, ensure_ascii=False), encoding="utf-8")
    plan = plan_visual_tasks(INDEX, STYLE)
    write_visual_plan(plan, lib / "视觉资产" / "计划.toml")
    if accepted:
        # 已通过状态必须走发布器：交付门会交叉验证正式版本清单与图片哈希（阶段 2 工单 2.4）
        from conftest import publish_all_with_reviews

        publish_all_with_reviews(lib)
    return lib, plan


def test_offline_runner_exists_and_is_wired_into_gate():
    assert OFFLINE.is_file()
    gate = (REPO_ROOT / "测试" / "run_all_checks.py").read_text(encoding="utf-8")
    assert "run_visual_offline.py" in gate, "离线视觉门必须接入总门"


def test_offline_runner_never_uses_real_api():
    source = OFFLINE.read_text(encoding="utf-8")
    assert "mock_api" in source, "离线门必须使用结构化 mock 服务器"
    # 只允许"清除密钥环境变量"的写法，不得读取或断言密钥
    assert "api_key=" not in source and "load_gemini_config" not in source


def test_plan_requires_upstream_accepted_before_downstream():
    plan = plan_visual_tasks(INDEX, STYLE)
    issues = validate_visual_plan(plan, INDEX)
    assert issues == [], issues


def test_visual_delivery_blocks_on_pending_tasks(tmp_path):
    lib, _plan = _lib(tmp_path)
    result = build_visual_delivery(lib)
    assert result["status"] != "DELIVERED"
    assert result["blocking"]
    assert not (lib / "视觉资产" / "视觉交付清单.json").exists()


@pytest.mark.requires_private_stylepack
def test_visual_delivery_signs_when_all_reference_ready(tmp_path):
    lib, plan = _lib(tmp_path, accepted=True)
    result = build_visual_delivery(lib)
    assert result["status"] == "DELIVERED", result["blocking"]
    manifest = lib / "视觉资产" / "视觉交付清单.json"
    assert manifest.is_file()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["passed"] == len(plan.tasks)
    assert data["style_hash"] == STYLE.pack_hash


def test_visual_delivery_blocks_on_style_hash_drift(tmp_path):
    lib, _plan = _lib(tmp_path, accepted=True)
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    write_visual_plan(VisualPlan(**{**plan.__dict__, "style_hash": "f" * 64}),
                      lib / "视觉资产" / "计划.toml")
    result = build_visual_delivery(lib)
    assert result["status"] != "DELIVERED"
    assert any("哈希" in item for item in result["blocking"])


def test_upstream_change_only_touches_downstream(tmp_path):
    lib, _stale = _lib(tmp_path, accepted=True)
    # 夹具会写盘并把任务置为已通过：必须读**磁盘上的真实计划**，而非入参旧对象
    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    first = plan.tasks[0]
    updated = propagate_upstream_change(plan, first.task_id)
    assert updated.by_id(first.task_id).status == "READY_AS_REFERENCE"
    for task in plan.tasks:
        if first.task_id in task.depends_on:
            assert updated.by_id(task.task_id).status == "NEEDS_REVIEW"
        elif task.task_id != first.task_id:
            assert updated.by_id(task.task_id).status == task.status


def test_blocking_issue_never_accepted():
    result = parse_qc_response({
        "task_id": "CH_林姑娘-正脸-v01", "decision": "ACCEPTED", "checker": "m",
        "checked_at": "t", "input_hashes": ["a" * 64],
        "issues": [{"code": "FORBIDDEN_MULTI_PANEL", "severity": BLOCKING,
                    "evidence": "出现三格", "repair_instruction": "改单张"}],
    }, expected={"task_id": "CH_林姑娘-正脸-v01", "input_hashes": ["a" * 64]})
    assert not result.is_accepted
    assert result.decision == "NEEDS_REVISION"


def test_round_history_preserved_on_plan_rewrite(tmp_path):
    lib, plan = _lib(tmp_path)
    task = plan.tasks[0]
    record = AttemptRecord(round=1, image="视觉资产/任务/x/attempt-01/image.png",
                           image_sha256="c" * 64)
    write_visual_plan(plan.with_task(VisualTask(**{**task.__dict__,
                                                   "attempts": (record,),
                                                   "status": "GENERATED"})),
                      lib / "视觉资产" / "计划.toml")
    again = plan_visual_tasks(INDEX, STYLE, existing=load_visual_plan(
        lib / "视觉资产" / "计划.toml"))
    kept = again.by_id(task.task_id)
    assert kept.round_count == 1, "重规划不得丢失历史轮次"
    assert kept.status == "GENERATED"


@pytest.mark.requires_private_stylepack
def test_visual_delivery_isolated_from_data_delivery(tmp_path):
    """视觉门不改变资料线：库内没有交付清单时也不会因视觉通过而伪造资料签发。"""
    lib, _plan = _lib(tmp_path, accepted=True)
    assert build_visual_delivery(lib)["status"] == "DELIVERED"
    assert not (lib / "交付清单.json").exists(), "视觉签发不得产出资料交付清单"


@pytest.mark.requires_private_stylepack
def test_offline_runner_reports_zero_failures_when_run():
    """真实执行离线门（快速子集）：退出码必须为 0。"""
    r = run_py([str(OFFLINE)], cwd=str(REPO_ROOT), timeout=900)
    assert r.returncode == 0, (r.stdout[-800:] + r.stderr[-400:])
    assert "零真实 API 请求" in r.stdout
