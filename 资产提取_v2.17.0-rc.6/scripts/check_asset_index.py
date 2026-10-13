#!/usr/bin/env python3
"""资产索引与场景母版校验（ADR-0084 工单C/D）：ID/关系矩阵/事实摘句/母版继承/P3 说明。"""
import argparse
import json
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
sys.path.insert(0, str(SKILL_DIR))

from engine.asset_schema import AssetSchemaValidator
from engine.continuity import (
    check_connects_consistency,
    check_master_drift,
    check_relation_types,
)


def _norm_text(s: str) -> str:
    """与 aggregate.norm_text 同口径的归一化；aggregate 不可导入时退化为 strip。"""
    try:
        from aggregate import norm_text
    except ImportError:
        return (s or "").strip()
    return norm_text(s)


def _load_texts(lib: Path) -> dict[str, str]:
    """加载段原文（归一化），供剧本事实摘句核对；缺段文件返回空。"""
    from engine.common import parse_frontmatter

    texts: dict[str, str] = {}
    seg_root = lib / "原文段"
    if seg_root.is_dir():
        for path in seg_root.rglob("*.md"):
            if not path.is_file() or "资产记录" in path.name:
                continue
            try:
                meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            texts[path.name] = _norm_text(body)
    return texts


def _check_fact_quotes(index: dict, texts: dict[str, str]) -> list[str]:
    """剧本事实摘句核对：quote 非空且在所指段文件/行号中；缺段文件标 NEEDS_REVIEW。"""
    errors: list[str] = []
    has_segments = bool(texts)
    for a in index.get("assets", []):
        if not isinstance(a, dict) or a.get("fact_level") != "剧本事实":
            continue
        aid = a.get("asset_id", "")
        evidence = a.get("evidence", []) or []
        quotes = [e for e in evidence if isinstance(e, dict) and (e.get("quote", "") or "").strip()]
        if not quotes:
            errors.append(f"{aid}: 事实等级为'剧本事实'但无原文摘句")
            continue
        if not has_segments:
            errors.append(f"{aid}: 缺段文件，剧本事实待复核（NEEDS_REVIEW，老库请先补段原文或降级）")
            continue
        for e in quotes:
            seg = (e.get("segment", "") or "").strip()
            quote = _norm_text(e.get("quote", ""))
            seg_text = texts.get(seg, "")
            if not seg:
                errors.append(f"{aid}: 剧本事实证据缺段文件名")
            elif not seg_text:
                errors.append(f"{aid}: 剧本事实所指段文件缺失 '{seg}'")
            elif quote and quote not in seg_text:
                errors.append(f"{aid}: 剧本事实摘句不在段文件 '{seg}' 中（假摘句阻断）")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="资产索引与母版校验")
    parser.add_argument("--lib", required=True, help="资产库根目录")
    parser.add_argument("--allow-missing-master", action="store_true",
                        help="明确标记为待迁移老库时才允许缺母版（仍不签发 DELIVERED）")
    args = parser.parse_args()
    lib = Path(args.lib)
    index_path = lib / ".process" / "资产索引.json"
    if not index_path.is_file():
        print(f"❌ 资产索引不存在: {index_path}（先运行 aggregate.py 或 maintenance.py migrate）")
        return 1
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"❌ 资产索引无法解析: {e}")
        return 1
    errors: list[str] = []
    result = AssetSchemaValidator().validate_index(index)
    errors.extend(result.errors)
    errors.extend(check_connects_consistency(index))
    errors.extend(check_relation_types(index))
    errors.extend(_check_fact_quotes(index, _load_texts(lib)))
    # 母版：新项目缺母版失败；仅明确待迁移老库给兼容提示（仍失败，不签发 DELIVERED）
    master_path = lib / "连续性" / "场景母版.md"
    has_scene = any(isinstance(a, dict) and a.get("type") == "场景" for a in index.get("assets", []))
    if master_path.is_file():
        try:
            errors.extend(check_master_drift(index, master_path.read_text(encoding="utf-8")))
        except OSError as e:
            errors.append(f"场景母版不可读: {e}")
    elif has_scene:
        if args.allow_missing_master:
            errors.append(f"场景母版缺失（待迁移老库兼容提示，不签发 DELIVERED）: {master_path}")
        else:
            errors.append(f"场景母版缺失（新项目必须存在）: {master_path}，请重跑聚合生成")
    # stable_key_mapping 一致性：每个 stable_key 必须在 mapping 中且指向同一实体
    mapping = index.get("stable_key_mapping", {}) or {}
    keys = mapping.get("keys", {}) if isinstance(mapping, dict) else {}
    for a in index.get("assets", []):
        if not isinstance(a, dict):
            continue
        sk = a.get("asset_id", "") and a.get("stable_key", "")
        if sk and sk not in keys:
            errors.append(f"{a.get('asset_id')}: stable_key '{sk}' 不在 stable_key_mapping 中")
        elif sk and keys.get(sk, {}).get("asset_id") != a.get("asset_id"):
            errors.append(f"{a.get('asset_id')}: stable_key_mapping 指向 {keys.get(sk, {}).get('asset_id')}（漂移）")
    # P3 排除说明：P3 资产应有故事功能说明
    for a in index.get("assets", []):
        if isinstance(a, dict) and a.get("priority") == "P3" and not (a.get("story_function") or "").strip():
            errors.append(f"{a.get('asset_id')}: P3 资产缺故事功能说明")
    if errors:
        print(f"❌ 资产索引/母版校验未通过（{len(errors)} 项）:")
        for e in errors[:15]:
            print(f"  - {e}")
        return 1
    print(f"✅ 资产索引/母版校验通过: {index_path}（{len(index.get('assets', []))} 项资产）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
