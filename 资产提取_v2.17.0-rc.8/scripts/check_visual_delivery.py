#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉交付门（规格 9，工单03-Task7；语义修正见终验工单 P1-1）。

与资料交付**完全独立**：`资料 verify` 不调用本门；本门只在用户显式执行
`maintenance.py verify-visual` 或 `check_visual_delivery.py` 时运行。

退出码契约（P1-1）：
- `0`：仅当视觉交付**已签发**（DELIVERED）；
- `3`：`VISUAL_NOT_ENABLED`——尚未创建视觉计划。视觉未启用是**合法状态**，
  但它**不是**「视觉交付通过」，自动化不得据此认为视觉线已完成；
- `1`：`BLOCKED`——有计划但未完成/被阻断（风格包缺失、模型未验证、产物失效等）。

签发条件：计划内**必需**任务全部通过、依赖新鲜、风格包指纹一致、参考可核验。
可选任务（拼版等）失败不阻断签发，但必须列入未完成项。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))

from compose_reference_board import board_is_fresh  # noqa: E402
from engine.style_registry import load_registry, resolve_style_record  # noqa: E402
from engine.visual_schema import VisualTaskStatus, load_visual_plan  # noqa: E402

PLAN_REL = ("视觉资产", "计划.toml")
DELIVERY_MANIFEST_NAME = "视觉交付清单.json"
OPTIONAL_KINDS = ("拼版",)

REASON_NOT_ENABLED = "VISUAL_NOT_ENABLED"
EXIT_DELIVERED = 0
EXIT_BLOCKED = 1
EXIT_NOT_ENABLED = 3


def exit_code_for(status: str) -> int:
    """状态 → 退出码：只有 DELIVERED 算通过。"""
    if status == "DELIVERED":
        return EXIT_DELIVERED
    if status == "NOT_ENABLED":
        return EXIT_NOT_ENABLED
    return EXIT_BLOCKED


def _is_optional(task) -> bool:
    return task.kind in OPTIONAL_KINDS


def _prior_manifest_state(lib: Path) -> dict:
    """读取历史视觉清单（用于报告「已签发但现已失效」）。"""
    path = lib / PLAN_REL[0] / DELIVERY_MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def build_visual_delivery(lib: Path, write: bool = True) -> dict:
    """评估视觉交付状态；全绿时（write=True）签发视觉交付清单。

    验证失败时**绝不覆盖**历史有效清单，只如实报告失效原因。
    """
    lib = Path(lib)
    plan_path = lib / PLAN_REL[0] / PLAN_REL[1]
    if not plan_path.is_file():
        return {"status": "NOT_ENABLED", "reason": "未创建视觉计划",
                "reason_code": REASON_NOT_ENABLED,
                "blocking": [], "incomplete": [], "counts": {}, "tasks": 0,
                "prior_manifest": _prior_manifest_state(lib)}
    try:
        plan = load_visual_plan(plan_path)
    except (OSError, ValueError) as e:
        return {"status": "BLOCKED", "reason": f"视觉计划不可解析：{e}",
                "reason_code": "VISUAL_PLAN_UNREADABLE",
                "blocking": ["视觉计划不可解析"], "incomplete": [], "counts": {}, "tasks": 0,
                "prior_manifest": _prior_manifest_state(lib)}

    record = resolve_style_record(plan.style_slug)
    blocking: list[str] = []
    incomplete: list[str] = []

    if record is None:
        blocking.append(f"风格 {plan.style_slug} 不在登记表中")
    else:
        if record.pack_version != plan.style_version:
            blocking.append(f"风格包版本不一致：计划 {plan.style_version} ≠ 登记 {record.pack_version}")
        if record.pack_hash and record.pack_hash != plan.style_hash:
            blocking.append("风格包内容哈希与登记表不一致（权威包已变化，需显式升级项目）")
        # 权威包必须真实存在且完整：缺图/漂移一律阻断（不得用 NOT_ENABLED 逃逸）
        pack_problems = _pack_blocking_issues(record)
        blocking.extend(pack_problems)
    if not load_registry().records:
        blocking.append("风格登记表缺失（无法核验风格包指纹）")

    # 已通过但产物无法核验（文件缺失/被换）：不得当作有效参考
    for task_id, code, detail in _unverifiable_accepted(plan, lib):
        blocking.append(f"{task_id} 的产物无法核验（{code}：{detail}）")

    # 交叉验证正式版本清单（阶段 2 工单 2.4）：计划说「已通过」必须能在发布器清单里对上
    for task_id, code, detail in _release_problems(lib, plan):
        blocking.append(f"{task_id or '正式版本清单'}（{code}）：{detail}")

    by_id = {t.task_id: t for t in plan.tasks}
    for task in plan.tasks:
        if task.is_accepted:
            continue
        label = f"{task.task_id}（{task.status}）"
        (incomplete if _is_optional(task) else blocking).append(label)
    for task in plan.tasks:
        if not task.is_accepted:
            continue
        for dep in task.depends_on:
            target = by_id.get(dep)
            if target is None:
                continue
            if not target.is_accepted:
                blocking.append(f"{task.task_id} 的上游 {dep} 未通过（依赖过期）")
            elif target.status == VisualTaskStatus.NEEDS_REVIEW.value:
                blocking.append(f"{task.task_id} 的上游 {dep} 需要重新检查")

    boards_stale = [t.task_id for t in plan.tasks
                    if t.kind in OPTIONAL_KINDS and t.is_accepted
                    and not board_is_fresh(lib, t.task_id)]
    for task_id in boards_stale:
        incomplete.append(f"{task_id}（拼版已过期，需重新合成）")

    fresh = not blocking
    prior = _prior_manifest_state(lib)
    manifest = {
        "schema_version": 1,
        "status": "DELIVERED" if fresh else "BLOCKED",
        "reason_code": "" if fresh else "VISUAL_BLOCKED",
        "reason": "" if fresh else "；".join(blocking[:3]),
        "style": plan.style_name,
        "style_slug": plan.style_slug,
        "style_version": plan.style_version,
        "style_hash": plan.style_hash,
        "counts": plan.counts_by_status(),
        "tasks": len(plan.tasks),
        "passed": sum(1 for t in plan.tasks if t.is_accepted),
        "blocking": blocking,
        "incomplete": incomplete,
        "generated_at": _now(),
        "note": "视觉交付与资料交付互相独立；本清单不改变资产资料交付状态",
    }
    if prior:
        manifest["prior_manifest"] = {
            "status": prior.get("status", ""),
            "generated_at": prior.get("generated_at", ""),
        }
        if not fresh:
            manifest["note"] += "；本次验证未通过，历史有效清单未被覆盖"
    if fresh and write:
        out = lib / PLAN_REL[0] / DELIVERY_MANIFEST_NAME
        out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8", newline="\n")
    return manifest


def _pack_blocking_issues(record) -> list[str]:
    """权威包缺失/不完整 → 阻断项（有计划就必须能核验风格源）。"""
    from engine.style_registry import pack_root
    from engine.visual_request_chain import stylepack_integrity_issues

    root = Path(pack_root(record.pack_slug, record.pack_version))
    if not root.is_dir():
        return [f"权威风格包不存在：{record.pack_slug}@{record.pack_version}"]
    issues = stylepack_integrity_issues(root, require_images=True)
    return [f"权威风格包不完整：{item}" for item in issues]


def _unverifiable_accepted(plan, lib: Path):
    """已通过但产物无法核验的任务（缺失/被换），复用编译链的同一判定。"""
    from engine.visual_request_chain import unverifiable_accepted

    return unverifiable_accepted(plan, Path(lib))


def _release_problems(lib: Path, plan) -> list[tuple[str, str, str]]:
    """计划 + 正式版本清单 + 审核绑定三者交叉验证（阶段 2 工单 2.4）。"""
    from engine.visual_publisher import validate_release

    return validate_release(Path(lib), plan)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="视觉交付门（独立于资料交付）")
    parser.add_argument("--lib", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = build_visual_delivery(Path(args.lib))
    if args.json:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    else:
        print(f"🎬 视觉交付检查：{result['status']}"
              + (f"（{result.get('reason_code', '')}）" if result.get("reason_code") else ""))
        if result["tasks"]:
            print(f"   任务 {result['passed']}/{result['tasks']} 已通过")
        if result.get("reason"):
            print(f"   {result['reason']}")
        for item in result["blocking"]:
            print(f"   ❌ {item}")
        for item in result["incomplete"]:
            print(f"   ⚠️ 未完成（可选，不阻断）：{item}")
        if result["status"] == "DELIVERED":
            print(f"   📦 已签发：{(Path(args.lib) / PLAN_REL[0] / DELIVERY_MANIFEST_NAME)}")
        elif result["status"] == "NOT_ENABLED":
            print("   视觉未启用是合法状态，但它不等于「视觉交付通过」；")
            print("   如需视觉交付，请先执行：python scripts/maintenance.py visual-plan "
                  "--lib <资产库> --style <中文风格名>")
    return exit_code_for(result["status"])


if __name__ == "__main__":
    sys.exit(main())
