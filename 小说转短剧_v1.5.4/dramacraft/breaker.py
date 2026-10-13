"""统一熔断执行器（v1.5.0 T1；方案 §3.3）——全线唯一的熔断算术实现。

判定顺序（钉死，不可调换）：
1. 本轮阻塞集为空 → `pass`（**成功优先**：先于一切熔断判定，[0,0,0] 直接放行）
2. `round_no >= cap` → `abort`（该集部分交付/未交付，产线继续）
3. 最近两轮均 `len(cur) >= len(prev)` 且 cur 非空 → `escalate`（随附新/未解/已解三差集）
4. 其余 → `continue`

两个消费方共用本模块，**工作流侧零复刻**：
- CLI / run_batch 路径：进程内直调 `breaker_decision`，每轮用 `append_round` 落台账
- 工作流路径：`python -m dramacraft.breaker --workdir W --ep N --round R
  --gate-report <本轮门报告> [--gate-report ...] [--cap 3] --json <裁决报告>`

CLI 合同（与门族 CLI 一致的双判定纪律）：
- stdout 只输出裸词 `pass|continue|escalate|abort`；机读真值在 `--json` 报告文件
- 退出码：0 = pass/continue；2 = escalate/abort；1 = 用法错或输入不可用
- **输入缺失/不可解析绝不落报告文件**（exit 0 但报告缺失是工作流侧的硬失败信号）

台账（跨进程唯一真值）：`<workdir>/state/healing/ep{NNN}.json`
`{"ep": N, "rounds": [{"round", "verdict", "problems": [[kind, where, digest], ...], "pids", "ts"}]}`
同轮重写按 `round` 覆盖（upsert），不追加重影——工作流 Amend 续跑重放时幂等。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

VERDICT_PASS = "pass"
VERDICT_CONTINUE = "continue"
VERDICT_ESCALATE = "escalate"
VERDICT_ABORT = "abort"

# healRoundCap（方案 §3.3）：回炉硬顶。工作流侧经 --cap 传入，两侧同一默认值。
DEFAULT_HEAL_ROUND_CAP = 3

# 门报告里允许出现在失败条目行的前缀（与 self_healing 既有 granularity 对齐）
_ITEM_PREFIXES = ("✗ [违规]", "↳ 详情:", "- ", "• ", "* ")

# 机读门报告可识别的键：hard（plan/shot lint）/ gates（validate.mjs）/ items（通用）
_REPORT_KEYS = ("hard", "gates", "items")

_PIPE_FORM = re.compile(r"^([A-Za-z][\w.\-]*)\|([^|]*)\|(.+)$")


class BreakerInputError(Exception):
    """入参/输入件不可用——CLI 一律 exit 1 且不落报告。"""


@dataclass(frozen=True)
class Problem:
    """一条阻塞项。`pid` 是跨轮稳定的内容指纹（同内容同 id，供集合比对）。"""

    kind: str
    where: str
    digest: str

    @property
    def pid(self) -> str:
        raw = f"{self.kind}|{self.where}|{self.digest}".encode("utf-8")
        return hashlib.md5(raw).hexdigest()[:12]

    def as_tuple(self) -> Tuple[str, str, str]:
        return (self.kind, self.where, self.digest)

    def render(self) -> str:
        return f"{self.kind}|{self.where}|{self.digest}"


def _normalize_item(raw: Any) -> str:
    text = str(raw).strip()
    changed = True
    while changed:
        changed = False
        for prefix in _ITEM_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                changed = True
    return re.sub(r"\s+", " ", text).strip()


def from_failed_items(items: Iterable[Any]) -> FrozenSet[Problem]:
    """把失败条目行归一成 Problem 集合。

    `A|B|C` 形态解析为 (kind, where, digest)；其余整行进 `raw` 类，digest = 归一化整行。
    同内容跨轮必得同一个 Problem（pid 稳定是「不降」判定的前提）。
    """
    problems = set()
    for raw in items or []:
        text = _normalize_item(raw)
        if not text:
            continue
        m = _PIPE_FORM.match(text)
        if m:
            problems.add(Problem(m.group(1), m.group(2), m.group(3)))
        else:
            problems.add(Problem("raw", "", text))
    return frozenset(problems)


def breaker_decision(history: Sequence[Iterable[Problem]], round_no: int,
                     cap: int = DEFAULT_HEAL_ROUND_CAP) -> str:
    """按四律判定。`history` 含本轮（末位即本轮阻塞集）。"""
    hist = [frozenset(h) for h in (history or [])]
    cur = hist[-1] if hist else frozenset()
    if not cur:
        return VERDICT_PASS
    if round_no >= cap:
        return VERDICT_ABORT
    if len(hist) >= 2 and len(cur) >= len(hist[-2]):
        return VERDICT_ESCALATE
    return VERDICT_CONTINUE


def breaker_detail(prev: Iterable[Problem], cur: Iterable[Problem]) -> Dict[str, set]:
    """三差集（元素为 (kind, where, digest) 元组）：unresolved = 两轮都在，new = 本轮新出，resolved = 本轮修好。"""
    p, c = frozenset(prev or ()), frozenset(cur or ())
    return {
        "unresolved": {x.as_tuple() for x in (c & p)},
        "new": {x.as_tuple() for x in (c - p)},
        "resolved": {x.as_tuple() for x in (p - c)},
    }


# ------------------------------------------------------------------ 台账

def journal_path(workdir: Any, ep: int) -> Path:
    return Path(workdir) / "state" / "healing" / f"ep{int(ep):03d}.json"


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def _read_journal(path: Path, ep: int) -> Dict[str, Any]:
    """读台账。缺文件 = 空台账；**存在但不可解析 = 隔离坏件后当空台账**。

    台账是派生件（跨进程镜像，内存里的本轮 history 才是权威），坏掉不该把整条产线拖成
    硬失败（评审 Must#3 实证：坏台账曾把「部分交付 exit 2」降级成「整批失败 exit 1」）。
    坏件改名留证（`.broken-<ts>`），不静默删。
    """
    if not path.exists():
        return {"ep": int(ep), "rounds": []}
    try:
        doc = json.loads(path.read_text("utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("rounds"), list):
            raise ValueError("台账结构不合法")
    except (OSError, ValueError) as err:
        stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        broken = path.with_suffix(path.suffix + f".broken-{stamp}")
        try:
            path.replace(broken)
            print(f"⚠ 回炉台账读不出来（{err}）——坏件已改名留证：{broken.name}，按空台账继续。",
                  file=sys.stderr)
        except OSError:
            print(f"⚠ 回炉台账读不出来（{err}）——按空台账继续。", file=sys.stderr)
        return {"ep": int(ep), "rounds": []}
    return doc


def append_round(workdir: Any, ep: int, round_no: int, problems: Iterable[Problem],
                 verdict: str, ts: Optional[str] = None) -> Path:
    """落一轮台账。写侧由调用方串行保证（run_batch 单线程 / 工作流 await）。

    轮次语义：**round 1 = 一次新尝试的开头**，写入时丢弃更早那次的全部残留；同时丢弃
    比本轮更大的轮次（跨运行残留会让下一轮的「连续两轮不降」判定凭空成立——评审 Must#2）。
    """
    if int(round_no) < 1:
        raise BreakerInputError(f"轮次必须从 1 起：{round_no}")
    path = journal_path(workdir, ep)
    doc = _read_journal(path, ep)
    probs = sorted(frozenset(problems or ()), key=lambda p: p.pid)
    rounds = [] if int(round_no) == 1 else [
        r for r in doc["rounds"] if int(r.get("round", 0)) < int(round_no)]
    rounds.append({
        "round": int(round_no),
        "verdict": str(verdict),
        "problems": [list(p.as_tuple()) for p in probs],
        "pids": [p.pid for p in probs],
        "ts": ts or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    })
    rounds.sort(key=lambda r: int(r.get("round", 0)))
    doc["ep"] = int(ep)
    doc["rounds"] = rounds
    _atomic_write_json(path, doc)
    return path


def load_history(workdir: Any, ep: int, before_round: Optional[int] = None,
                 ) -> List[FrozenSet[Problem]]:
    """按轮次升序返回阻塞集序列。`before_round` 给定则只取轮次更小的（重跑幂等用）。"""
    doc = _read_journal(journal_path(workdir, ep), ep)
    hist: List[FrozenSet[Problem]] = []
    for rec in doc["rounds"]:
        try:
            rno = int(rec.get("round", 0))
        except (TypeError, ValueError):
            continue
        # 轮次从 1 起：round < 1 是旧版/上一轮残留，留着会让本轮 round 1 凭空"有上一轮可比"（评审 Must#2）
        if rno < 1:
            continue
        if before_round is not None and rno >= int(before_round):
            continue
        probs = set()
        for item in rec.get("problems") or []:
            if isinstance(item, (list, tuple)) and len(item) == 3:
                probs.add(Problem(str(item[0]), str(item[1]), str(item[2])))
        hist.append(frozenset(probs))
    return hist


# ------------------------------------------------------------------ 门报告 → 阻塞集

def problems_from_report(path: Any) -> FrozenSet[Problem]:
    """解析一份机读门报告为阻塞集。

    - `gates`（validate.mjs）：失败门 → `✗ [违规] <label>` + detail 按「；」拆条目
    - `hard`（plan/shot lint）：逐条进；`warn` 不入熔断（告警不否决交付）
    - `items`（通用）：逐条进
    文件缺失 / JSON 不可解析 / 三键皆无 → BreakerInputError。
    """
    p = Path(str(path))
    if not p.exists():
        raise BreakerInputError(f"门报告不存在：{path}")
    try:
        doc = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError) as err:
        raise BreakerInputError(f"门报告不可解析：{path}（{err}）")

    if not isinstance(doc, dict):
        raise BreakerInputError(f"门报告结构无法识别（需为 JSON 对象）：{path}")
    if not any(k in doc for k in _REPORT_KEYS):
        raise BreakerInputError(f"门报告无可识别键 {_REPORT_KEYS}：{path}")

    items: List[str] = []
    for key in ("hard", "items"):
        if key in doc:
            if not isinstance(doc[key], list):
                raise BreakerInputError(f"门报告 {key} 字段非列表：{path}")
            items.extend(str(x) for x in doc[key])
    if "gates" in doc:
        if not isinstance(doc["gates"], list):
            raise BreakerInputError(f"门报告 gates 字段非列表：{path}")
        if not doc["gates"]:
            # 门集合为空 = 这份报告证明不了「门跑过且没问题」，不许当"没有违规"放行（评审 Should#3）
            raise BreakerInputError(f"门报告里一道门都没有（gates 为空），无法判定：{path}")
        for g in doc["gates"]:
            if not isinstance(g, dict) or g.get("ok"):
                continue
            items.append(f"✗ [违规] {g.get('label')}")
            for frag in str(g.get("detail") or "").split("；"):
                frag = frag.strip()
                if frag:
                    items.append(frag)
    return from_failed_items(items)


# ------------------------------------------------------------------ CLI

class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # 用法错一律 exit 1（argparse 默认 2 与本模块语义冲突）
        self.print_usage(sys.stderr)
        print(f"breaker: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _build_parser() -> _Parser:
    ap = _Parser(prog="python -m dramacraft.breaker",
                 description="统一熔断执行器：读回炉台账 + 本轮门报告，出裁决（stdout 裸词 + 机读报告）")
    ap.add_argument("--workdir", required=True, help="工作目录（台账落在 <workdir>/state/healing/）")
    ap.add_argument("--ep", type=int, required=True, help="集号")
    ap.add_argument("--round", type=int, required=True, dest="round_no",
                    help="本轮回炉轮次（从 1 起；1 = 一次新尝试的开头）")
    ap.add_argument("--gate-report", action="append", default=None, metavar="PATH",
                    help="本轮门报告路径，可重复（validate.mjs / plan_lint / shot_lint 机读报告）")
    ap.add_argument("--cap", type=int, default=DEFAULT_HEAL_ROUND_CAP,
                    help=f"回炉硬顶（healRoundCap，默认 {DEFAULT_HEAL_ROUND_CAP}，至少 1）")
    ap.add_argument("--json", required=True, dest="json_path", metavar="PATH",
                    help="裁决报告落盘路径（机读真值；与退出码双判定）")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(list(argv) if argv is not None else None)

    if not args.gate_report:
        ap.error("至少需要一个 --gate-report（无法判定不得默认放行）")
    if args.round_no < 1:
        ap.error(f"--round 必须从 1 起（收到 {args.round_no}）：轮次 1 表示一次新尝试的开头")
    if args.cap < 1:
        ap.error(f"--cap 必须至少 1（收到 {args.cap}）：回炉硬顶不能是 {args.cap}")

    try:
        cur: FrozenSet[Problem] = frozenset()
        for rep in args.gate_report:
            cur = cur | problems_from_report(rep)
        prior = load_history(args.workdir, args.ep, before_round=args.round_no)
        prev = prior[-1] if prior else frozenset()
        verdict = breaker_decision(list(prior) + [cur], round_no=args.round_no, cap=args.cap)
        diff = breaker_detail(prev, cur)
        journal = append_round(args.workdir, args.ep, args.round_no, cur, verdict)
    except BreakerInputError as err:
        print(f"breaker: {err}", file=sys.stderr)
        return 1

    payload = {
        "ep": int(args.ep),
        "round": int(args.round_no),
        "verdict": verdict,
        "cap": int(args.cap),
        "problemCount": len(cur),
        "unresolved": sorted("|".join(t) for t in diff["unresolved"]),
        "new": sorted("|".join(t) for t in diff["new"]),
        "resolved": sorted("|".join(t) for t in diff["resolved"]),
        "journal": str(journal),
    }
    _atomic_write_json(Path(str(args.json_path)), payload)
    print(verdict)
    return 0 if verdict in (VERDICT_PASS, VERDICT_CONTINUE) else 2


if __name__ == "__main__":
    raise SystemExit(main())
