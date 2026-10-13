#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉计划阅读稿渲染（工单02-Task7，规格 9）。

用法：
    python scripts/render_visual_plan.py --lib <资产库>            # 由 计划.toml 渲染阅读稿
    python scripts/render_visual_plan.py --lib <资产库> --style <中文名>   # 先规划再渲染

口径：TOML 是唯一机器事实源；`计划阅读版.md` 由 TOML 自动生成，**改它不改变任何状态**。
"""
import argparse
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))

from engine.visual_schema import (  # noqa: E402
    MAX_AUTO_ROUNDS,
    VisualPlan,
    load_visual_plan,
    user_decision_label,
    user_status_label,
    write_visual_plan,
)

PLAN_REL = ("视觉资产", "计划.toml")
READING_COPY_NAME = "计划阅读版.md"

HEADER = """# 视觉生产计划（阅读版·自动生成）

> 本文件由 `视觉资产/计划.toml` 自动生成，**不要手工修改**——下一次渲染会整体覆盖。
> 机器事实源是 TOML；本文件只供人阅读，改它不会改变任何状态。

"""


def _dep_line(task) -> str:
    if not task.depends_on:
        return "无（起点任务）"
    return "、".join(task.depends_on)


def _ref_lines(task) -> list[str]:
    if not task.references:
        return ["  - （无参考图）"]
    from engine.visual_schema import ReferenceRole

    role_cn = {
        ReferenceRole.IDENTITY.value: "身份",
        ReferenceRole.STRUCTURE.value: "结构",
        ReferenceRole.STYLE.value: "风格",
        ReferenceRole.MATERIAL.value: "材质",
        ReferenceRole.LIGHTING.value: "光影",
        ReferenceRole.SPACE.value: "空间",
    }
    return [f"  - {role_cn.get(r.role, r.role)}：`{r.path}`"
            + (f"（{r.note}）" if r.note else "") for r in task.references]


def render_visual_plan(plan: VisualPlan) -> str:
    """把视觉计划渲染为中文阅读稿（状态/依赖/参考职责/下一步命令齐备）。"""
    lines = [HEADER.rstrip("\n"), ""]
    lines.append("## 计划概览")
    lines.append("")
    lines.append(f"- 风格：{plan.style_name or '（未登记）'}@{plan.style_version or '（未固定）'}")
    if plan.style_hash:
        lines.append(f"- 风格包内容哈希：`{plan.style_hash[:12]}…`（母版更新只提示，不自动改变本项目）")
    lines.append(f"- 引擎：{plan.engine_profile}")
    lines.append(f"- 资产索引指纹：`{plan.asset_index_hash or '（未记录）'}`")
    lines.append(f"- 任务总数：{len(plan.tasks)}｜自动返工上限：{MAX_AUTO_ROUNDS} 轮/项")
    counts = plan.counts_by_status()
    if counts:
        detail = "、".join(f"{user_status_label(k)} {v}" for k, v in counts.items())
        lines.append(f"- 状态分布：{detail}")
    lines.append("")

    lines.append("## 任务清单（按任务 ID 排序）")
    for task in plan.sorted_tasks():
        lines.append("")
        lines.append(f"### {task.task_id}")
        lines.append("")
        lines.append(f"- 资产：{task.asset_id}｜种类：{task.kind}｜画幅：{task.aspect_ratio}")
        lines.append(f"- 状态：{user_status_label(task.status)}")
        lines.append(f"- 依赖：{_dep_line(task)}")
        lines.append(f"- 风格包：{task.style_slug}@{task.style_version}"
                     f"（哈希 `{task.style_hash[:12]}…`）")
        if task.materials:
            lines.append(f"- 材质：{'、'.join(task.materials)}")
        lines.append(f"- 事实来源：{task.fact_source}")
        if task.attempts:
            history = "、".join(
                f"第{a.round}轮 {user_decision_label(a.qc) if a.qc else '未质检'}"
                for a in task.attempts)
            lines.append(f"- 已生成轮次：{task.round_count}/{task.max_rounds}（{history}）")
        else:
            lines.append(f"- 已生成轮次：0/{task.max_rounds}")
        if task.user_locked:
            lines.append(f"- 人工锁定字段：{'、'.join(task.user_locked)}（重规划不覆盖）")
        if task.notes:
            lines.append(f"- 备注：{task.notes}")
        lines.append("- 参考图职责：")
        lines.extend(_ref_lines(task))
    lines.append("")
    lines.append("## 下一步")
    lines.append("")
    lines.append("```powershell")
    lines.append("# 重新规划（幂等：只新增缺失任务，保留已完成记录与人工锁定字段）")
    lines.append("python scripts\\create_visual_plan.py --lib <资产库> --style " +
                 (plan.style_name or "<中文风格名>"))
    lines.append("# 生成手工任务包（无 API 时）")
    lines.append("python scripts\\visual_production.py export --lib <资产库> --task <任务ID>")
    lines.append("# 导入手工结果并质检")
    lines.append("python scripts\\visual_production.py import --lib <资产库> --task <任务ID> --image <图片>")
    lines.append("# 视觉交付门（独立于资料交付）")
    lines.append("python scripts\\maintenance.py verify-visual --lib <资产库>")
    lines.append("```")
    lines.append("")
    lines.append("> 视觉生产是**可选下游模块**：未启用、部分完成或失败都不影响"
                 "「资产资料已交付」（两条状态线互不阻塞，见 docs/adr/0085）。")
    return "\n".join(lines).rstrip("\n") + "\n"


def write_reading_copy(plan: VisualPlan, lib: Path) -> Path:
    """把阅读稿写到 `<资产库>/视觉资产/计划阅读版.md`（原子覆盖）。"""
    import os

    target = Path(lib) / PLAN_REL[0] / READING_COPY_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render_visual_plan(plan), encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="由视觉计划 TOML 渲染中文阅读稿")
    parser.add_argument("--lib", required=True, help="资产库根目录")
    parser.add_argument("--style", help="给定时先规划/续建计划再渲染（中文正式名）")
    parser.add_argument("--assets", help="只规划这些资产 ID（逗号分隔）")
    args = parser.parse_args()

    lib = Path(args.lib)
    if args.style:
        from engine.style_registry import StyleAliasConflict, resolve_style_record
        from engine.visual_planner import PlannerError, plan_visual_tasks

        index_path = lib / ".process" / "资产索引.json"
        if not index_path.is_file():
            print(f"❌ 缺少资产索引 {index_path}（视觉计划不得脱离已签发事实凭空推断）")
            return 1
        import json

        try:
            asset_index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"❌ 资产索引不可解析：{e}")
            return 1
        try:
            style = resolve_style_record(args.style)
        except StyleAliasConflict as e:
            print(f"❌ 风格解析冲突（{e.code}）：{e.detail}")
            return 1
        if style is None:
            print(f"❌ 风格「{args.style}」不在登记表中")
            return 1
        plan_path = lib / PLAN_REL[0] / PLAN_REL[1]
        existing = load_visual_plan(plan_path) if plan_path.is_file() else None
        scope = {s.strip() for s in (args.assets or "").replace("，", ",").split(",") if s.strip()}
        try:
            plan = plan_visual_tasks(asset_index, style, scope=scope or None, existing=existing)
        except PlannerError as e:
            print(f"❌ 规划失败（{e.code}）：{e.detail}")
            return 1
        write_visual_plan(plan, plan_path)
        print(f"✅ 视觉计划已写入: {plan_path}")
    else:
        plan_path = lib / PLAN_REL[0] / PLAN_REL[1]
        if not plan_path.is_file():
            print(f"❌ 视觉计划不存在: {plan_path}（先跑 create_visual_plan.py 或加 --style）")
            return 1
        try:
            plan = load_visual_plan(plan_path)
        except (OSError, ValueError) as e:
            print(f"❌ 视觉计划不可解析：{e}")
            return 1

    path = write_reading_copy(plan, lib)
    print(f"✅ 阅读稿已生成: {path}（由 TOML 渲染，改它不改变状态）")
    print(f"   风格: {plan.style_name or '（未登记）'}@{plan.style_version or '（未固定）'}"
          f"｜任务 {len(plan.tasks)} 项")
    counts = plan.counts_by_status()
    if counts:
        print("   状态分布: " + "、".join(f"{user_status_label(k)} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
