# -*- coding: utf-8 -*-
"""数量守恒覆盖门（工单 2）。

守恒式：`正式资产 = 已产基础提示词 + 明确待审 + 明确阻断`。
任何"既无产物、又无明确原因"的正式人物/场景/道具都记为违规，令质量门非零退出并点名资产 ID。
后期元素不进生图、不计入此守恒。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Set

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "engine"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from engine import asset_ledger as al  # noqa: E402

LEGITIMATE_MARKERS = ("非资产", "用户取消", "取消", "待审", "阻断", "出处未验证", "待确认")


def evaluate(ledger: List[Dict[str, Any]], prompted_names: Set[str],
             skipped: Dict[str, str]) -> Dict[str, Any]:
    """按守恒式判定覆盖，返回漏斗、违规清单与退出码。"""
    formal = [e for e in ledger
              if e.get("ledger_status") == "formal" and "base_prompt" in e.get("required_outputs", [])]
    violations: List[Dict[str, str]] = []
    prompted = 0
    pending_or_blocked = 0
    funnel: Dict[str, Dict[str, int]] = {}

    for e in formal:
        t = e["asset_type"]
        cell = funnel.setdefault(t, {"formal": 0, "prompted": 0, "pending_or_blocked": 0})
        cell["formal"] += 1
        name = e.get("canonical_name", "")
        aid = e.get("asset_id", "")
        if name in prompted_names or aid in prompted_names:
            prompted += 1
            cell["prompted"] += 1
            continue
        # 跳过理由：ID 走 `_match_skip`（唯一的精确匹配入口），再退回名称。
        # 原先写成 `skipped.get(aid) or skipped.get(name) or _match_skip(skipped, aid)`——
        # 第一项与第三项查的是同一个 key，第三项恒为第一项的子集，纯属重复。
        reason = _match_skip(skipped, aid) or skipped.get(name)
        if reason and any(k in reason for k in LEGITIMATE_MARKERS):
            pending_or_blocked += 1
            cell["pending_or_blocked"] += 1
            continue
        violations.append({
            "asset_id": aid, "name": name, "type": t,
            "reason": "无基础提示词且无明确豁免原因（疑似被 P3/优先级静默跳过）",
            "suggestion": f"为该{t}生成基础提示词，或给出明确非资产/用户取消理由",
        })

    total = len(formal)
    coverage = 1.0 if total == 0 else prompted / total
    return {
        "coverage": round(coverage, 4),
        "violations": violations,
        "counts": {
            "formal": total,
            "prompted": prompted,
            "pending_or_blocked": pending_or_blocked,
            "violations": len(violations),
        },
        "funnel_by_type": funnel,
        "exit_code": 0 if not violations else 1,
    }


def _match_skip(skipped: Dict[str, str], asset_id: str) -> str:
    """跳过理由只按**精确 ID** 取。

    这里曾用双向子串（`asset_id in k or k in asset_id`），于是清单里写了 `PR_剑`，
    毫无产物的 `PR_剑鞘` 也拿到豁免——本门是签发门之一，等于假放行（实测 violations=0）。
    """
    return skipped.get(asset_id, "")


# ---------------- 产物装载 ----------------

def load_prompted_names(lib: str) -> Set[str]:
    base_dir = os.path.join(lib, "提示词", "基础")
    names: Set[str] = set()
    if not os.path.isdir(base_dir):
        return names
    for fn in os.listdir(base_dir):
        if not fn.endswith(".md") or fn == "跳过清单.md":
            continue
        text = open(os.path.join(base_dir, fn), encoding="utf-8").read()
        for m in re.finditer(r"^##\s*\d{3}\.\s*(.+)$", text, re.M):
            names.add(m.group(1).strip())
    return names


def load_skipped(lib: str) -> Dict[str, str]:
    """读跳过清单 → {资产ID: 理由}；解析统一走 `engine.skip_list`（短横条目 + 表格行）。

    三个读者（本门 / check_schedule / pipeline_state）必须读同一份解析结果：
    生成侧写短横条目、旧库留表格行，各认一半就会出现"覆盖率说没问题、verify 说缺提示词"。
    """
    from engine.skip_list import parse_skip_entries
    path = os.path.join(lib, "提示词", "基础", "跳过清单.md")
    if not os.path.isfile(path):
        return {}
    return parse_skip_entries(open(path, encoding="utf-8").read())


def load_ledger_or_index(lib: str) -> List[Dict[str, Any]]:
    ledger = al.load_ledger(lib)
    if ledger:
        return ledger
    idx_path = os.path.join(lib, ".process", "资产索引.json")
    if os.path.isfile(idx_path):
        index = json.loads(open(idx_path, encoding="utf-8").read())
        return al.build_ledger(index)
    return []


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="资产数量守恒覆盖门")
    ap.add_argument("--lib", required=True, help="资产库根目录")
    ap.add_argument("--json", action="store_true", help="stdout 输出单个 JSON")
    args = ap.parse_args(list(argv) if argv is not None else None)

    ledger = load_ledger_or_index(args.lib)
    report = evaluate(ledger, load_prompted_names(args.lib), load_skipped(args.lib))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        c = report["counts"]
        print(f"正式资产 {c['formal']}｜已产提示词 {c['prompted']}｜待审/阻断 {c['pending_or_blocked']}｜"
              f"违规 {c['violations']}｜覆盖率 {report['coverage']:.1%}")
        for v in report["violations"]:
            print(f"  ❌ [{v['type']}] {v['asset_id']} {v['name']}：{v['reason']}")
            print(f"     建议：{v['suggestion']}")
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
