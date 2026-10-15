"""S 门族：镜头组机读执法（v1.5.0 T3；方案 §3.2 + Omni 剧本转镜头组卡 v1.2.0）。

机读镜头组 schema（T3 定版，T4/T7 只读不扩）::

    {"ep": 1, "schemaVersion": 1,
     "groups": [{"gid": "EP01-SG01", "secs": "11-13", "scene": "顾家客厅·暴雨压城",
                 "cast": ["C01", "C03"], "content": "……", "planSceneId": "Sc1"}]}

硬闸（非空 → 该集回炉；CLI 退出码 2）：
- **SG1 编号连续**：`EP{ep:02d}-SG{n:02d}`，n 从 1 起连续、无洞无重、集号与文件一致
- **SG2 单组时长**：可解析且端点都在 4~15s 内；**禁** `00:00-00:25` 这类绝对时间段（卡 :50）
- **SG3 全集时长对账**：Σ组中值 ∈ 目标秒数 ±15%（单组合规防不了整体敷衍）
- **SG4 承载**：规划件每一场至少被一个组承载；组的 `planSceneId` 必须在规划件里存在
- **SG5 人物不越台**：`cast` ⊆ 本集登记角色
- **SG6 五字段齐**：编号/时长/场景/内容非空；人物可为空列表（= 无人物，卡 :52 写「无」）
- **SG7 编号可解析**：`F###` 伏笔号必须能在伏笔账本（`state/foreshadow_log.jsonl`）里查到
  （没给 `--workdir` 时不硬判，**转成告警**，绝不静默放行）；`C##` 角色码必须在本集登记
  ——下游件不得自造编号

告警（进报告不回炉）：**抽象心理词表**（卡 :139-146 的反面写法）命中 `content` → ⚠。

CLI（与门族同合同）::

    python -m dramacraft.shot_lint <镜头组.json> --plan <plan.json> --script <剧本.json>
                                   --target-seconds 90 --json <报告.json> [--workdir <工作区>]

退出码：0 = 硬闸全绿；2 = 有硬伤；1 = 用法错或输入不可用（**不落报告文件**）。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dramacraft.plan_lint import PlanInputError, _read_json, resolve_episode

# 抽象心理词表（单源：镜头组规范卡附录只引常量名，与卡 :139-146 反面写法同源）
ABSTRACT_PSY_LEXICON: Tuple[str, ...] = (
    "表现出", "表现紧张", "内心", "命运感", "气氛很好", "很震惊", "若有所思",
    "情绪复杂", "心情复杂", "感到紧张", "显得很", "透出几分",
)

_GID_RE = re.compile(r"^EP(\d+)-SG(\d+)$")
# 前后不得紧贴 ASCII 字母数字：Python 的 `\b` 把汉字也算 word 字符，「回收伏笔F999」曾整段漏判
_FID_RE = re.compile(r"(?<![A-Za-z0-9])F(\d{2,})(?!\d)")
_CID_RE = re.compile(r"(?<![A-Za-z0-9])C(\d{2,})(?!\d)")
# 合法时长写法：`8`、`6-8`、`6~8`、`6—8`，可带 s/秒 后缀（解析不许吃 -6-8 / 1e1-12 / nan）
_SECS_RE = re.compile(r"^(\d+(?:\.\d+)?)(?:\s*[-~—–]\s*(\d+(?:\.\d+)?))?[sS秒]?$")

SECS_MIN, SECS_MAX = 4.0, 15.0
TOTAL_TOLERANCE = 0.15


class ShotInputError(PlanInputError):
    """镜头组输入不可用——CLI exit 1（继承 PlanInputError：同一类输入纪律）。"""


def _parse_secs(text: Any) -> Optional[Tuple[float, float]]:
    """解析时长建议 → (起点, 终点)。只认 `11-13` / `11~13` / `11—13` / `12`（可带 s、秒）。

    `00:00-00:25`（绝对时间段）、`-6-8`、`1e1-12`、`6-8-`、`nan`、`inf` 一律解析不出来，
    返回 None 交由 SG2 报违规——**不做「顺手修好笔误」的事**（评审 W2/W3）。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    m = _SECS_RE.match(raw)
    if not m:
        return None
    start = float(m.group(1))
    end = float(m.group(2)) if m.group(2) is not None else start
    if not (math.isfinite(start) and math.isfinite(end)) or end < start:
        return None
    return (start, end)


def _midpoint(pair: Tuple[float, float]) -> float:
    return round((pair[0] + pair[1]) / 2, 4)


def _ep_characters(ep: Dict[str, Any]) -> set:
    return {str(c) for sc in (ep.get("scenes") or []) if isinstance(sc, dict)
            for c in (sc.get("characters") or [])}


def _resolve_ep_num(sg: Dict[str, Any], plan: Dict[str, Any]) -> int:
    """集号必须是整数：非整数的集号会让 SG1 的组号顺序闸静默失效（评审 W6）。"""
    raw = sg.get("ep", plan.get("ep"))
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ShotInputError(f"镜头组件缺集号或集号不是整数：ep={raw!r}（集号必须写数字，如 1）")
    if raw < 1:
        raise ShotInputError(f"集号必须从 1 起：ep={raw}")
    return raw


def lint_shot_groups(sg: Any, plan: Any, script: Any, target_seconds: Any,
                     workdir: Optional[str] = None) -> Dict[str, Any]:
    """跑 S 门族。返回 {"hard": [...], "warn": [...], "groupMids": [...], "totalSeconds": float, ...}。"""
    if not isinstance(sg, dict):
        raise ShotInputError("镜头组件文件损坏或结构无法识别")
    groups = sg.get("groups")
    if not isinstance(groups, list):
        raise ShotInputError("镜头组件里没有镜头组内容（缺 groups 分组）")
    if not isinstance(plan, dict) or not isinstance(plan.get("scenes"), list):
        raise ShotInputError("规划件文件损坏或没有场次内容（缺 scenes 场次）")

    ep_num = _resolve_ep_num(sg, plan)
    ep = resolve_episode(script, ep_num)
    label = f"ep{ep_num}"
    try:
        target = float(target_seconds)
    except (TypeError, ValueError):
        raise ShotInputError(f"目标秒数无法解析：{target_seconds}")
    if not math.isfinite(target) or target <= 0:
        raise ShotInputError(f"目标秒数必须是正数：{target_seconds}")

    plan_scenes = [sc for sc in plan["scenes"] if isinstance(sc, dict)]
    plan_ids = {str(sc.get("planSceneId") or "") for sc in plan_scenes}
    # 「本集登记」= 剧本场次里的角色码 ∪ 规划件登记的 cast（规划件自身不越台由 D 门 H4 负责）
    roster_codes = _ep_characters(ep) | {str(c) for sc in plan_scenes for c in (sc.get("cast") or [])}

    hard: List[str] = []
    warn: List[str] = []
    mids: List[float] = []

    # ---- SG6 五字段齐（先跑：缺字段后面算不出） ----
    if not groups:
        hard.append(f"SG4 {label} 一个镜头组都没有（整集没有可拍内容，无从对账）")
    for idx, g in enumerate(groups, start=1):
        if not isinstance(g, dict):
            hard.append(f"SG6 {label} 第 {idx} 组格式有误，读不出这一组的内容")
            continue
        gid = str(g.get("gid") or "").strip() or f"(第{idx}组)"
        for field, cn in (("gid", "编号"), ("secs", "时长建议"), ("scene", "场景"), ("content", "内容")):
            if not str(g.get(field) or "").strip():
                hard.append(f"SG6 {label} {gid} 缺「{cn}」（五字段必须齐全）")
        if not isinstance(g.get("cast"), (list, tuple)):
            hard.append(f"SG6 {label} {gid} 的「人物」格式有误（本组没有出场人物就留空，导出时自动写「无」）")
        else:
            for c in g.get("cast") or []:
                if not isinstance(c, str):
                    hard.append(f"SG6 {label} {gid} 的「人物」里有非文字项 {c!r}"
                                f"（人物写角色码字符串，如 C01）")

    # ---- SG1 编号连续 ----
    # 集号已由 _resolve_ep_num 保证为正整数，prefix 恒为非空字符串
    prefix = f"EP{ep_num:02d}-SG"
    gids = [str(g.get("gid") or "").strip() for g in groups if isinstance(g, dict)]
    expected = [f"{prefix}{i:02d}" for i in range(1, len(gids) + 1)]
    malformed = [gid for gid in gids if not _GID_RE.match(gid)]
    if malformed:
        hard.append(f"SG1 {label} 编号不合规范（应为 {prefix}01 这种形式）："
                    f"{'、'.join(malformed[:3])}")
    nums = [int(_GID_RE.match(gid).group(2)) for gid in gids if _GID_RE.match(gid)]
    wrong_ep = [gid for gid in gids if _GID_RE.match(gid) and _GID_RE.match(gid).group(1) != f"{ep_num:02d}"]
    if wrong_ep:
        hard.append(f"SG1 {label} 编号里的集号和本集对不上：{'、'.join(wrong_ep[:3])}")
    dupes = sorted({n for n in nums if nums.count(n) > 1})
    missing = [n for n in range(1, len(gids) + 1) if n not in nums]
    if dupes:
        hard.append(f"SG1 {label} 组号重复：{'、'.join(f'SG{n:02d}' for n in dupes)}")
    if missing:
        hard.append(f"SG1 {label} 组号断号：缺 {'、'.join(f'SG{n:02d}' for n in missing)}"
                    f"（请按顺序从 SG01 连续编）")
    if not malformed and not dupes and not missing and gids != expected:
        hard.append(f"SG1 {label} 组号顺序不对：应为 {'→'.join(expected[:2])}… 依次排列")

    # ---- SG2 单组时长 + SG3 总时长对账 ----
    for idx, g in enumerate(groups, start=1):
        if not isinstance(g, dict):
            continue
        gid = str(g.get("gid") or "").strip() or f"(第{idx}组)"
        pair = _parse_secs(g.get("secs"))
        if pair is None:
            raw = str(g.get("secs") or "").strip()
            hint = "（不许写绝对时间段，要写 4-15s 范围内的建议时长，如 6-8）" \
                if re.search(r"\d+\s*:\s*\d+", raw) else "（写 4-15s 范围内的建议时长，如 6-8）"
            hard.append(f"SG2 {label} {gid} 的时长「{raw}」读不出来{hint}")
            continue
        mids.append(_midpoint(pair))
        if pair[0] < SECS_MIN or pair[1] > SECS_MAX:
            hard.append(f"SG2 {label} {gid} 的时长 {pair[0]:g}-{pair[1]:g}s 超出 {SECS_MIN:g}~{SECS_MAX:g}s")

    total = round(sum(mids), 4)
    if not mids and groups:
        hard.append(f"SG3 {label} 一组时长都读不出来，没法跟目标 {target:g}s 对账")
    elif mids:
        low, high = target * (1 - TOTAL_TOLERANCE), target * (1 + TOTAL_TOLERANCE)
        # 浮点边界要留余量：90*1.15 实际是 103.49999999999999，恰好在边界的合法件不许被误杀（评审 W1）
        if total < low - 1e-9 or total > high + 1e-9:
            hard.append(f"SG3 {label} 全集时长对不上：各组中值合计 {total:g}s，"
                        f"目标 {target:g}s（允许 {low:.1f}~{high:.1f}s）——按 4-15s/组重切组数")

    # ---- SG4 承载 ----
    carried = set()
    for idx, g in enumerate(groups, start=1):
        if not isinstance(g, dict):
            continue
        pid = str(g.get("planSceneId") or "").strip()
        gid = str(g.get("gid") or "").strip() or f"(第{idx}组)"
        if not pid:
            hard.append(f"SG4 {label} {gid} 没写它对应的导演规划场次编号")
            continue
        if pid not in plan_ids:
            hard.append(f"SG4 {label} {gid} 对应的导演规划场次 {pid} 在规划件里不存在")
            continue
        carried.add(pid)
    for pid in sorted(plan_ids - carried):
        hard.append(f"SG4 {label} 规划场{pid} 没有任何镜头组承载（漏拍）")

    # ---- SG5 人物不越台 ----
    for idx, g in enumerate(groups, start=1):
        if not isinstance(g, dict) or not isinstance(g.get("cast"), (list, tuple)):
            continue
        gid = str(g.get("gid") or "").strip() or f"(第{idx}组)"
        for c in g.get("cast") or []:
            code = str(c).strip()
            if code and code not in roster_codes:
                hard.append(f"SG5 {label} {gid} 出现本集没登记的人物 {code}（登记："
                            f"{'、'.join(sorted(roster_codes)) or '无'}）")

    # ---- SG7 令牌可解析 ----
    ledger_ids: Optional[set] = None
    if workdir is not None:
        from dramacraft.continuity_lint import replay
        ledger_ids = set(replay(workdir).keys())
    seen_f: Dict[str, str] = {}
    for idx, g in enumerate(groups, start=1):
        if not isinstance(g, dict):
            continue
        gid = str(g.get("gid") or "").strip() or f"(第{idx}组)"
        blob = " ".join([str(g.get("scene") or ""), str(g.get("content") or "")])
        for m in _FID_RE.finditer(blob):
            fid = f"F{m.group(1)}"
            seen_f[fid] = gid
            if ledger_ids is not None and fid not in ledger_ids:
                hard.append(f"SG7 {label} {gid} 引用的伏笔编号 {fid} 没有在伏笔清单里登记"
                            f"（镜头组不能私自捏造编号）")
        for m in _CID_RE.finditer(blob):
            cid = f"C{m.group(1)}"
            if cid not in roster_codes:
                hard.append(f"SG7 {label} {gid} 引用的角色码 {cid} 本集没登记（可用："
                            f"{'、'.join(sorted(roster_codes)) or '无'}）")
    if ledger_ids is None and seen_f:
        for fid, gid in sorted(seen_f.items()):
            warn.append(f"SG7 {label} {gid} 引用了伏笔编号 {fid}，但本次没给账本（--workdir），"
                        f"没法核对是否真实存在")

    # ---- 抽象心理告警 ----
    for idx, g in enumerate(groups, start=1):
        if not isinstance(g, dict):
            continue
        gid = str(g.get("gid") or "").strip() or f"(第{idx}组)"
        content = str(g.get("content") or "")
        hits = [w for w in ABSTRACT_PSY_LEXICON if w in content]
        if hits:
            warn.append(f"W2 {label} {gid} 的「内容」出现抽象写法「{'、'.join(hits)}」——"
                        f"请改写成看得见、听得见的动作、表情、道具或具体反应")

    return {
        "hard": hard,
        "warn": warn,
        "ep": ep_num,
        "groupMids": mids,
        "totalSeconds": total,
        "targetSeconds": target,
        "planSceneCovered": sorted(carried),
    }


# ------------------------------------------------------------------ CLI

class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # 用法错一律 exit 1（与门族合同一致）
        self.print_usage(sys.stderr)
        print(f"shot_lint: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = _Parser(prog="python -m dramacraft.shot_lint",
                 description="镜头组检查工具：对照剧本与规划，核对编号、时长、场次承载、出场人物与五字段")
    ap.add_argument("shots", help="镜头组件 JSON 路径")
    ap.add_argument("--plan", required=True, help="规划件 JSON 路径（上游导演规划的产物）")
    ap.add_argument("--script", required=True, help="定稿剧本 JSON 路径（整本剧本或单集件都行）")
    ap.add_argument("--target-seconds", required=True, type=float, help="单集目标时长秒数")
    ap.add_argument("--workdir", help="工作区目录（给了就核对伏笔编号是否在账本里）")
    ap.add_argument("--json", required=True, dest="json_path",
                    help="质检报告保存路径（JSON；程序按「退出码 + 报告文件」两道一起判，缺一不算通过）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    try:
        shots = _read_json(args.shots, "镜头组件")
        plan = _read_json(args.plan, "规划件")
        script = _read_json(args.script, "剧本件")
        result = lint_shot_groups(shots, plan, script, args.target_seconds, workdir=args.workdir)
    except (ShotInputError, PlanInputError) as err:
        print(f"shot_lint: {err}", file=sys.stderr)
        return 1

    # 先落报告再报喜：报告写不下去时，stdout 里绝不能先出现「合格」（评审 Block-2 同类）
    payload = {"ep": result["ep"], "hard": result["hard"], "warn": result["warn"],
               "groupCount": len(result["groupMids"]), "totalSeconds": result["totalSeconds"],
               "targetSeconds": result["targetSeconds"],
               "planSceneCovered": result["planSceneCovered"]}
    try:
        Path(str(args.json_path)).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8")
    except OSError as err:
        print(f"shot_lint: 报告写不进去：{args.json_path}（{err}）", file=sys.stderr)
        return 1

    print(f"===== 镜头组检查（第 {shots.get('ep')} 集） =====")
    for h in result["hard"]:
        print(f"✗ [必须改] {h}")
    for w in result["warn"]:
        print(f"⚠ [提醒] {w}")
    if not result["hard"]:
        print("✓ 镜头组合格：编号连续、单组时长合规、全集时长对账一致、规划场全部有组承载、出场人物均已登记。")
    print(f"（共 {len(result['groupMids'])} 组，中值合计 {result['totalSeconds']:g}s / "
          f"目标 {result['targetSeconds']:g}s）")
    return 2 if result["hard"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
