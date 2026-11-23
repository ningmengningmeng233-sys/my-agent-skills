#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""风格治理命令（规格 5/10，工单02-Task1 / 工单04-Task2）。

用法：
    python scripts/manage_styles.py list [--json] [--all] [--include-legacy]
    python scripts/manage_styles.py deprecate|disable --style <中文名>
    python scripts/manage_styles.py scan-usage --style <中文名> [--root <目录>] [--json]
    python scripts/manage_styles.py remove --style <中文名> [--root <目录>]
        [--force-unreferenced] [--confirm]

`list`/`scan-usage` 只读；`deprecate`/`disable` 只改登记表；
`remove` **只生成待删除清单，绝不物理删除任何文件**（删除是独立人工操作）。
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

from engine.style_registry import (  # noqa: E402
    LEGACY_VISIBLE,
    LIFECYCLE,
    REGISTRY_PATH,
    LifecycleError,
    load_registry,
    scan_style_usage,
    set_style_status,
    style_name_report,
)


def cmd_list(as_json: bool, include_all: bool, include_legacy: bool) -> int:
    registry = load_registry()
    if not registry.records:
        print(f"❌ 风格登记表缺失或为空: {registry.path}（请先恢复 references/styles/registry.toml）")
        return 1
    rows = style_name_report(registry)
    if not include_all:
        allowed = ["active"] + (list(LEGACY_VISIBLE) if include_legacy else [])
        rows = [r for r in rows if r["生命周期"] in allowed and r["类型"] == "style"]
    if as_json:
        sys.stdout.write(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
        return 0
    print("🎨 风格清单（用户面向正式名称；内部编号仅用于兼容与诊断）")
    print(f"  登记表: {registry.path}")
    verified = [r for r in rows if r["验证等级"] == "omni_verified"]
    if verified:
        print("  ✅ 已验证风格（视觉母版可回溯）:")
        for r in verified:
            print(f"     {r['正式名称']}｜{r['风格包'] or '（未挂包）'}"
                  f"｜{'可分享' if r['可分享'] else '私人不可分享'}")
    legacy = [r for r in rows if r["验证等级"] != "omni_verified"]
    if legacy:
        print(f"  ⚠️ 旧版未验证风格 {len(legacy)} 项（可用但未经视觉母版重建验证）:")
        for r in legacy:
            print(f"     {r['正式名称']}（旧编号 {r['旧编号']}）")
    hidden = [r for r in style_name_report(registry) if r not in rows]
    if hidden and not include_all:
        print(f"  ℹ️ 另有 {len(hidden)} 项未列出（速查表 / 已停用）；加 --all 查看全部")
    return 0


def _usage_flags(args) -> int:
    """scan-usage：扫描项目引用（只读，不自动改写任何项目）。"""
    usages = scan_style_usage(Path(args.root), args.style, args.pack_hash or "")
    if args.json:
        sys.stdout.write(json.dumps(usages, ensure_ascii=False, indent=2) + "\n")
        return 0
    print(f"🔎 风格「{args.style}」引用扫描：{len(usages)} 处")
    for usage in usages:
        print(f"   {usage['source']}｜{usage['path']}｜版本 {usage['style_version'] or '（未记）'}"
              f"｜最后使用 {usage['last_used']}")
    if not usages:
        print("   （无引用：可进入停用/移除流程）")
    return 0


def _advance(args, target: str) -> int:
    """推进生命周期（只改登记表，绝不触碰项目文件）。"""
    try:
        new_status = set_style_status(Path(args.registry), args.style, target)
    except LifecycleError as e:
        print(f"❌ 生命周期操作被拒（{e.code}）：{e.detail}")
        return 1
    print(f"✅ 风格「{args.style}」→ {new_status}")
    print(f"   登记表: {args.registry}")
    if new_status == "disabled":
        print("   ℹ️ 停用风格不再进入新项目列表；旧项目仍可按固定版本读取")
    return 0


def _remove(args) -> int:
    """remove：先扫描引用；有引用即拒绝；否则只生成待删除清单。"""
    usages = scan_style_usage(Path(args.root), args.style, args.pack_hash or "")
    if usages:
        print(f"❌ 风格「{args.style}」仍被 {len(usages)} 个项目引用，拒绝物理删除：")
        for usage in usages[:5]:
            print(f"   {usage['source']}｜{usage['path']}")
        print("   被引用的风格只能停用（disable），不能删除。")
        return 1
    if not args.force_unreferenced:
        print("❌ remove 需要显式 --force-unreferenced（防止误删）")
        return 1
    registry = load_registry(Path(args.registry))
    record = registry.resolve(args.style)
    if record is None:
        print(f"❌ 风格「{args.style}」不在登记表中")
        return 1
    if record.status != "removable":
        print(f"❌ 风格「{args.style}」当前 {record.status}，必须先按 "
              f"{'→'.join(LIFECYCLE)} 依次推进到 removable 才能进入移除流程")
        return 1
    pack_line = (f"- 风格包：{record.pack_slug}@{record.pack_version}"
                 f"（哈希 {record.pack_hash[:12]}…）" if record.pack_slug
                 else "- 风格包：（无内置包）")
    entry_line = '- 登记表中 `id = "' + record.id + '"` 的条目'
    lines = [
        "# 风格待删除清单（本文件只是待办；工具不会自动删除任何文件）",
        "",
        f"- 风格：{record.name}（内部编号 {record.id}）",
        f"- 生命周期：{record.status}",
        pack_line,
        "- 引用扫描：0 处（无项目引用）",
        "",
        "## 待人工确认后删除的路径",
        "",
        f"- `references/styles/{record.pack_slug}/{record.pack_version}/`（如存在）",
        entry_line,
        "",
        "> 删除为**独立人工操作**：确认备份与归档后手工执行；本工具只产出清单。",
        "",
    ]
    plan_path = Path(args.root) / "风格待删除清单.md"
    plan_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"✅ 已生成待删除清单: {plan_path}")
    print("   ⚠️ 本工具不执行物理删除；请人工确认备份后另行处理。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="风格治理：登记表查询与生命周期管理")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="列出风格（中文正式名 / 旧编号 / 验证等级 / 生命周期）")
    p_list.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    p_list.add_argument("--all", action="store_true", help="包含速查表与已停用项")
    p_list.add_argument("--include-legacy", action="store_true", help="包含已弃用（deprecated）风格")
    for name, help_text in (("deprecate", "标记为已弃用（旧项目仍可读）"),
                            ("disable", "停用：不再进入新项目列表"),
                            ("scan-usage", "扫描项目对该风格的引用（只读）"),
                            ("remove", "生成待删除清单（需零引用且已 removable）")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--style", required=True, help="风格中文正式名")
        p.add_argument("--root", default=".", help="项目根目录（扫描引用用；默认当前目录）")
        p.add_argument("--registry", default=str(REGISTRY_PATH), help="登记表路径")
        p.add_argument("--pack-hash", default="", help="只统计该风格包哈希的项目")
        if name == "remove":
            p.add_argument("--force-unreferenced", action="store_true",
                           help="显式确认该风格零引用")
            p.add_argument("--confirm", action="store_true", help="确认执行")
        if name == "scan-usage":
            p.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.cmd == "list":
        return cmd_list(args.json, args.all, args.include_legacy)
    if args.cmd == "scan-usage":
        return _usage_flags(args)
    if args.cmd == "deprecate":
        return _advance(args, "deprecated")
    if args.cmd == "disable":
        return _advance(args, "disabled")
    if args.cmd == "remove":
        return _remove(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
