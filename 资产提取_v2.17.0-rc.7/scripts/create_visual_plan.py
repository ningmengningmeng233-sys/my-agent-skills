#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉计划入口（工单02-Task5）。

用法：
    python scripts/create_visual_plan.py --lib <资产库> --style 仙侠国风电影CG
        [--assets CH_001,CH_002] [--costume-assets CH_002] [--back-assets CH_003]

只读取已签发或可验证的资产索引，绝不重新推断人物事实；重复执行幂等追加。
"""
import argparse
import json
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))

from engine.style_registry import StyleAliasConflict, resolve_style_record  # noqa: E402
from engine.visual_planner import PlannerError, plan_visual_tasks  # noqa: E402
from engine.visual_schema import (  # noqa: E402
    load_visual_plan,
    user_status_label,
    validate_visual_plan,
    write_visual_plan,
)

PLAN_REL = ("视觉资产", "计划.toml")
INDEX_REL = (".process", "资产索引.json")


def _split_ids(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.replace("，", ",").split(",") if item.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description="按资产索引生成/续建视觉生产计划（TOML）")
    parser.add_argument("--lib", required=True, help="资产库根目录")
    parser.add_argument("--style", required=True, help="风格中文正式名（如 仙侠国风电影CG）")
    parser.add_argument("--assets", help="只规划这些资产 ID（逗号分隔；默认全部）")
    parser.add_argument("--costume-assets",
                        help="剧情需要服装独立图的资产 ID（P2 通常不含服装正背面）")
    parser.add_argument("--back-assets", help="存在背面镜头的资产 ID（P3 补全身背面）")
    args = parser.parse_args()

    lib = Path(args.lib)
    index_path = lib / INDEX_REL[0] / INDEX_REL[1]
    if not index_path.is_file():
        print(f"❌ 缺少资产索引 {index_path}（请先跑聚合或迁移生成索引；"
              "视觉计划不得脱离已签发事实凭空推断）")
        return 1
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
        print(f"❌ 风格「{args.style}」不在登记表中（先登记再规划）")
        return 1
    if not style.pack_slug or not style.pack_version or not style.pack_hash:
        print(f"❌ 风格「{style.name}」尚未固定风格包版本与内容哈希"
              "（先 python scripts/sync_stylepack.py 同步并登记 pack_hash）")
        return 1

    plan_path = lib / PLAN_REL[0] / PLAN_REL[1]
    existing = None
    if plan_path.is_file():
        try:
            existing = load_visual_plan(plan_path)
        except (OSError, ValueError) as e:
            print(f"❌ 既有视觉计划不可解析：{e}")
            return 1

    try:
        plan = plan_visual_tasks(
            asset_index, style,
            scope=_split_ids(args.assets) or None,
            existing=existing,
            extra_costume=_split_ids(args.costume_assets),
            extra_back=_split_ids(args.back_assets),
        )
    except PlannerError as e:
        print(f"❌ 规划失败（{e.code}）：{e.detail}")
        return 1

    issues = validate_visual_plan(plan, asset_index)
    if issues:
        print(f"❌ 视觉计划校验未通过（{len(issues)} 项）：")
        for issue in issues[:10]:
            print(f"   {issue}")
        return 1

    write_visual_plan(plan, plan_path)
    counts = plan.counts_by_status()
    print(f"✅ 视觉计划已写入: {plan_path}")
    print(f"   风格: {plan.style_name}@{plan.style_version}（哈希 {plan.style_hash[:12]}…）")
    print(f"   任务: {len(plan.tasks)} 项"
          + (f"｜{'、'.join(f'{user_status_label(k)} {v}' for k, v in counts.items())}" if counts else ""))
    if existing is not None:
        before = {t.task_id for t in existing.tasks}
        added = [t.task_id for t in plan.tasks if t.task_id not in before]
        print(f"   本次新增: {len(added)} 项" + (f"（{'、'.join(added[:5])}…）" if added else ""))
    needs_review = [t.task_id for t in plan.tasks if t.status == "NEEDS_REVIEW"]
    if needs_review:
        print(f"   ⚠️ 事实变化待重查: {len(needs_review)} 项（{'、'.join(needs_review[:5])}）")
    print("   下一步: python scripts/render_visual_plan.py --lib <资产库>（生成中文阅读稿）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
