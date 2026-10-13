"""命令行入口：
  python -m renderplan plan --storyboard S.json --out .lemon/render_plan.json
  python -m renderplan run --plan .lemon/render_plan.json --stage video [--retry-failed] --out .lemon/run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .plan import build_plan, load_json, StoryboardError
from .contract import ContractError, check_plan
from . import ledger as L
from . import stages as ST
from .adapters import StubVideoAdapter, StubTtsAdapter, HttpVideoAdapter, HttpTtsAdapter


def _eprint(msg):
    print(msg, file=sys.stderr)


def _default_ledger(out_path):
    """文档命令是 `--out .lemon/run`：产物已在 .lemon 下就就近写，别再套一层。"""
    p = Path(out_path)
    for parent in p.parents:
        if parent.name == ".lemon":
            return parent / "run-ledger.jsonl"
    return p.parent / ".lemon" / "run-ledger.jsonl"


def cmd_plan(args):
    try:
        storyboard = load_json(args.storyboard)
    except FileNotFoundError:
        _eprint(f"找不到分镜文件：{args.storyboard}")
        return 2
    except json.JSONDecodeError:
        _eprint(f"分镜文件不是合法 JSON：{args.storyboard}")
        return 2
    dubbing = ledger = None
    try:
        if args.dubbing:
            dubbing = load_json(args.dubbing)
        if args.asset_ledger:
            ledger = load_json(args.asset_ledger)
    except FileNotFoundError as exc:
        _eprint(f"找不到输入文件：{exc}")
        return 2

    try:
        plan = build_plan(storyboard, dubbing=dubbing, asset_ledger=ledger)
    except StoryboardError as exc:
        _eprint(f"分镜不符合 storyboard 契约，已拒绝生成工单：{exc}")
        return 2

    out = Path(args.out)
    ST._write_json(args.out, plan)
    lp = Path(args.ledger) if args.ledger else _default_ledger(out)
    L.append(L.make_record(stage="plan", status="SUCCEEDED", succeeded=len(plan["shots"]),
                           failed=0, skipped=0, artifacts=[str(out)],
                           note=f"由 {os.path.basename(str(args.storyboard))} 推导 {len(plan['shots'])} 个镜头"),
               lp)
    print(f"已写出成片工单：{out}（{len(plan['shots'])} 个镜头）")
    return 0


def _adapters():
    if os.environ.get("RENDER_USE_HTTP", "").strip() in {"1", "true", "yes"}:
        return HttpVideoAdapter(), HttpTtsAdapter()
    return StubVideoAdapter(), StubTtsAdapter()


def cmd_run(args):
    try:
        plan = load_json(args.plan)
    except FileNotFoundError:
        _eprint(f"找不到工单文件：{args.plan}（请先运行 plan 命令）")
        return 2
    except json.JSONDecodeError:
        _eprint(f"工单文件不是合法 JSON：{args.plan}")
        return 2

    try:
        check_plan(plan)
    except ContractError as exc:
        _eprint(f"工单不符合 render_plan 契约，已拒绝执行（文件：{args.plan}）：{exc}")
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    lp = Path(args.ledger) if args.ledger else _default_ledger(out_dir)
    stage = args.stage

    try:
        if stage == "video":
            v, _ = _adapters()
            res = ST.run_video_stage(plan, out_dir, v, retry_failed=args.retry_failed, ledger_path=lp)
        elif stage == "tts":
            _, t = _adapters()
            thr = args.drift_threshold if args.drift_threshold is not None else ST.DEFAULT_TTS_DRIFT
            res = ST.run_tts_stage(plan, out_dir, t, threshold=thr, ledger_path=lp)
        elif stage == "assemble":
            res = ST.run_assemble_stage(plan, out_dir, ledger_path=lp)
        elif stage == "lip-sync":
            res = ST.run_lipsync_stage(plan, mode=args.lip_sync, out_dir=out_dir, ledger_path=lp)
        else:  # pragma: no cover - argparse 已限制
            _eprint(f"未知阶段：{stage}")
            return 2
    except ContractError as exc:
        # SC-01：阶段内的契约问题（脏断点等）同样收敛为纯中文错误 + exit 2，
        # 与 plan 侧 StoryboardError 同一口径。
        _eprint(f"断点状态不合法，已拒绝执行：{exc}")
        return 2

    print(f"[{stage}] 状态={res['status']} 成功={res.get('succeeded')} "
          f"失败={res.get('failed')} 跳过={res.get('skipped')}")
    if stage == "assemble" and res["jianying"] == "NOT_RUN":
        print(f"[assemble] {res['note']}")
    status = res.get("status", "")
    if status == "SUCCEEDED":
        return 0
    if status == "FAILED":
        return 2
    if status in ("PARTIAL", "SKIPPED", "NOT_RUN"):
        return 1
    return 1


def build_parser():
    p = argparse.ArgumentParser(prog="renderplan", description="成片合成：分镜 → 工单 → 视频/配音/合成/口型")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("plan", help="由分镜推导逐镜工单")
    pp.add_argument("--storyboard", required=True)
    pp.add_argument("--dubbing", help="配音对齐单 json")
    pp.add_argument("--asset-ledger", help="投产包/资产总账 json")
    pp.add_argument("--out", required=True)
    pp.add_argument("--ledger", help="运行台账路径，默认 <out 同级>/.lemon/run-ledger.jsonl")
    pp.set_defaults(func=cmd_plan)

    rp = sub.add_parser("run", help="按阶段执行")
    rp.add_argument("--plan", required=True)
    rp.add_argument("--stage", required=True,
                    choices=["video", "tts", "assemble", "lip-sync"])
    rp.add_argument("--out", required=True)
    rp.add_argument("--retry-failed", action="store_true", help="只重跑 failed_items.json 里的镜头")
    rp.add_argument("--lip-sync", default="off", choices=["off", "on"], help="口型同步模式，默认 off")
    rp.add_argument("--drift-threshold", type=float, default=None, help="TTS 时长漂移阈值（秒），默认 0.35")
    rp.add_argument("--ledger")
    rp.set_defaults(func=cmd_run)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)
