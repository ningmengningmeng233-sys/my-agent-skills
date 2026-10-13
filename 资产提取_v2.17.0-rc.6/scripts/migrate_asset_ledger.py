#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧库迁移入口（v2.17.0 工单 12）。

    python scripts/migrate_asset_ledger.py scan     --lib <资产库>
    python scripts/migrate_asset_ledger.py apply    --lib <资产库> [--no-backup]
    python scripts/migrate_asset_ledger.py rollback --lib <资产库>
    python scripts/migrate_asset_ledger.py shadow   --lib <资产库> [--work <目录>]

`scan` 只读；`apply` 前自动备份且不覆盖旧 Markdown；`rollback` 按清单还原并逐文件核对哈希；
`shadow` 只在副本上跑，绝不改动原库。
"""
import argparse
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))

from engine.ledger_migration import (  # noqa: E402
    MigrationError,
    apply,
    rollback,
    scan,
    shadow,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="旧库迁移与影子运行")
    parser.add_argument("action", choices=("scan", "apply", "rollback", "shadow"))
    parser.add_argument("--lib", required=True, help="资产库根目录")
    parser.add_argument("--no-backup", action="store_true", help="apply 时跳过备份（不推荐）")
    parser.add_argument("--work", default="", help="shadow 的工作目录（默认在库同级）")
    parser.add_argument("--json", action="store_true", help="只输出一个合法 JSON 到 stdout")
    args = parser.parse_args()

    lib = Path(args.lib)
    if not lib.is_dir():
        print(f"❌ 资产库不存在：{lib}", file=sys.stderr)
        return 1
    try:
        if args.action == "scan":
            report = scan(lib)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            print(f"🔍 迁移扫描（只读）：共 {report['proposal']['total']} 项")
            for kind, count in sorted(report["proposal"]["by_type"].items()):
                print(f"   {kind}: {count}")
            for item in report["ambiguities"]:
                print(f"   ⚠️ {item['asset_id']}：{item['detail']}")
            return 0
        if args.action == "apply":
            result = apply(lib, backup=not args.no_backup)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if result["idempotent"]:
                print(f"⏭️  总账已是当前内容（{result['entries']} 项），未改写")
                return 0
            print(f"✅ 迁移完成：{result['entries']} 项（legacy proposal {result['legacy_proposals']} 项）")
            print(f"   备份：{result['backup']}")
            print("   旧 Markdown 未被覆盖；对照文件见 总账对照.md")
            return 0
        if args.action == "rollback":
            result = rollback(lib)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if result["mismatch"]:
                print(f"❌ 回滚后哈希不一致：{'、'.join(result['mismatch'])}", file=sys.stderr)
                return 1
            print(f"✅ 已回滚：还原 {result['restored']} 个文件，移除 {len(result['removed'])} 个迁移新增文件")
            return 0
        result = shadow(lib, work_root=args.work or None)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"🌗 影子运行（原库未改动）：{result['work_dir']}")
        print(f"   资产差集：仅旧 {len(result['asset_diff']['only_old'])} / 仅新 {len(result['asset_diff']['only_new'])}"
              f" / 共同 {len(result['asset_diff']['common'])}")
        saving = result["expected_saving"]
        print(f"   预计文本调用：{saving['old_text_calls']} → {saving['new_text_calls']}"
              f"（正式资产 {saving['formal_assets']} 项）")
        return 0
    except MigrationError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
