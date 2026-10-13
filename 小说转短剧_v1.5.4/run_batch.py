#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DramaCraft Engine — 工业级小说转短剧双轨制批量流水线主控入口
串联：万能输入适配 (ingestor) -> 多节点网关 (gateway) -> 质量门自愈 (self_healing) -> 多格式导出 (exporter)
"""

import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import math
import os
import sys
import threading
from typing import Any, List, Optional

# 注入根目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from dramacraft.ingestor import load_novel
from dramacraft.gateway import LLMGateway, BudgetExhausted
from dramacraft.self_healing import (run_quality_gate, generate_episode_with_healing,
                                     build_context_block, HealingBlocked)
from dramacraft.exporter import export_all, EXPORT_STYLE_CHECKLIST
from dramacraft.outline_lint import lint_outline, compute_outline_hash
from dramacraft.ledger import Ledger, load_events, summarize
from dramacraft.continuity_lint import (
    build_naming_seeds,
    ep_content_hash,
    ep_level_checks,
    final_foreshadow_sweep,
    final_naming_sweep,
    foreshadow_open_overview,
    foreshadow_todo_for_ep,
    lint_foreshadow_states,
    lint_registration,
    load_ep_hashes,
    register_from_outline,
    register_paid_claims,
    replay,
)
from dramacraft.doctor import check_node_available, run_doctor


def safe_write_json(path: str, data: Any):
    """原子写 JSON：先写入线程/进程唯一临时文件，flush+fsync 后原子 replace 替换目标文件。
    彻底避免断电/中断/多线程竞争写出半截坏 JSON 污染断点缓存。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def safe_write_text(path: str, text: str):
    """原子写文本文件"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fp:
            fp.write(text)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _write_production_log(ledger, workdir, outline_data):
    """写入面向用户的白话生产小结（Markdown 表格，无开发者黑话）"""
    if ledger:
        events = load_events(ledger.path)
        summary_md = summarize(events, outline_data)
        out_path = os.path.join(workdir, "output", "production_log.md")
        try:
            safe_write_text(out_path, summary_md)
        except OSError as e:
            print(f"⚠ 写入生产记录失败: {e}")


def _one_line(text: Any) -> str:
    """把定位摘要压成单行——换行/制表符会把终端排版打乱，用户在终端里就没法读了。"""
    return " ".join(str(text or "").split())


def _default_budget(pending: int, retries: int = 3) -> Optional[int]:
    """默认熔断保险丝公式：pending*(1+retries)*1.1 + 10。
    pending=0 时返回 None（全量已缓存时不装闸）。"""
    if pending <= 0:
        return None
    return math.ceil(pending * (1 + retries) * 1.1) + 10


def render_resume_card(workdir, completed_eps, pending_eps, title, f2_unresolved=0):
    """断点恢复卡（4 行白话）：数字全部来自本次实扫磁盘缓存 + 账本重放，禁估算。

    照搬 fbs `ux-optimization-rules.md §A` 恢复卡四行模板（书→剧、阶段→进度换词）。
    """
    completed = list(completed_eps or [])
    pending = list(pending_eps or [])
    total = len(completed) + len(pending)
    states = replay(workdir, set(completed), load_ep_hashes(workdir))
    n_open = sum(1 for s in states.values() if s.get("status") == "open")

    if pending:
        next_step = f"原命令重跑即可，从第 {min(pending)} 集接着跑（已完成集不重复计费）"
    else:
        next_step = "全部集数已就绪，可直接汇编导出交付"

    line2 = f"· 已完成：{len(completed)}/{total} 集（本次实扫磁盘缓存）"
    line3 = f"· 未回收伏笔：{n_open} 条"
    if f2_unresolved:
        line3 += f" ｜ 认领待补证据：{f2_unresolved} 条"
    return "\n".join([f"《{title}》—— 续跑前先看一眼", line2, line3, f"· 下一步：{next_step}"])


def _downstream_stale_eps(workdir, eps) -> dict:
    """哪些集的规划/镜头/监督已经跟不上现在的剧本了（改稿下游失效链，v1.5.0 T4）。

    只报不做：本 CLI 只产剧本，重跑下游是工作流（或人）的事——**不因此重做剧本**，
    否则手改过的剧本会被无谓覆盖、白烧额度。
    """
    from dramacraft.artifact_chain import stale_stages
    stale = {}
    for ep_n in eps:
        try:
            stages = stale_stages(workdir, ep_n)
        except Exception as err:                      # 坏件/半截 JSON 不许把跑批炸掉
            stale[ep_n] = [f"下游件读不了（{err}）——按需重做"]
            continue
        if stages:
            stale[ep_n] = stages
    return stale


def _enforce_outline_gate(outline_data, source_path, workdir=None):
    """改编的根必须先合格：大纲不过 O1-O6 预审门 + F1 伏笔登记门，任何生成/校验/汇编一律拒跑。

    传 workdir 时（跑批主路径）顺带把大纲伏笔幂等入册到伏笔账本；单集校验模式无台账语境，
    只做 F1 判定不落账。
    """
    violations = lint_outline(outline_data)
    violations.extend(lint_registration(outline_data))
    if violations:
        print(f"❌ 大纲预审门未通过（{source_path}）：")
        for v in violations:
            print(f"   ✗ {v}")
        print("   大纲是所有质量门的对账基准，先修大纲再跑剧本。")
        sys.exit(2)
    if workdir is not None:
        registered = register_from_outline(workdir, outline_data)
        if registered:
            print(f"[伏笔登记] 大纲伏笔入册 {registered} 条，后续按计划集数追踪回收。")


def stamp_ep_meta(ep_data: dict, outline_hash: str) -> dict:
    """给单集缓存补 `_meta`（缓存安检三查的凭据：有 _meta / schemaVersion ≥ 2 / outlineHash 对上大纲）。

    引擎自己生成剧本时在这里打章；**工作流派工人在外面写的单集件也走这里**
    （`python run_batch.py --stamp-only <单集件> --outline <大纲>`）——
    否则 `--assemble-only` 的断点安检会把 12 门全过的件当成「旧版本缓存」拒收（exit 2）。
    """
    ep_data["_meta"] = {
        "engineVersion": "1.2.1",
        "schemaVersion": 2,
        "outlineHash": outline_hash,
        "gatesPassedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    return ep_data["_meta"]


EP_FLOW_TYPES = ("action", "dialogue")


def lint_ep_shape(ep_obj: dict) -> List[str]:
    """剧本件的拍/场形状契约（`references/WORKFLOW.md` 二「剧本 ep###.json 契约」）。

    12 门按 `line` 是否字符串认对白，对「场缺 `location` / 拍缺 `type`」这类**契约字段缺失**是漏放的；
    而下游导演规划按契约读对白与地点锚点（plan_lint 的 H2/H3/H4），缺字段要等两个相位之后才炸——
    真跑实测（2026-09-23 T6 一集）：写手件缺这两个字段，12 门全绿放行、导演规划拿到 22 条硬伤
    「台词在原剧本里找不到」，一整轮白跑。所以形状闸挂在这里：**写手过门之前、入库补章之前都要过**。
    """
    hard: List[str] = []
    scenes = ep_obj.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        if isinstance(ep_obj.get("episodes"), list):
            return ["[E1] 这是「整剧脚本」形状（含 episodes），请传**单个单集件**（库里 state/episodes/epNNN.json 那种）"]
        return ["[E1] 没有场（scenes 为空）：单集件必须按场写 flow 拍子流"]
    for i, sc in enumerate(scenes, 1):
        if not isinstance(sc, dict):
            hard.append(f"[E1] 第 {i} 场不是对象")
            continue
        sid = str(sc.get("sceneId") or f"第 {i} 场")
        if not str(sc.get("location") or "").strip():
            hard.append(f"[E1] {sid} 缺 location：每场必须写地点（如「顾家大厅·夜」）——镜头组与地点锚点靠它")
        flow = sc.get("flow")
        if not isinstance(flow, list) or not flow:
            hard.append(f"[E2] {sid} 没有拍（flow 为空）")
            continue
        for j, beat in enumerate(flow, 1):
            if not isinstance(beat, dict):
                hard.append(f"[E2] {sid} 第 {j} 拍不是对象")
                continue
            btype = beat.get("type")
            if btype not in EP_FLOW_TYPES:
                hard.append(f"[E2] {sid} 第 {j} 拍 type 缺失或非法（只能 action 或 dialogue，实得 {btype!r}）")
                continue
            if btype == "action":
                if not str(beat.get("action") or "").strip():
                    hard.append(f"[E3] {sid} 第 {j} 拍是动作拍却没写 action")
            else:
                if not str(beat.get("speaker") or "").strip():
                    hard.append(f"[E3] {sid} 第 {j} 拍是对白拍却没写 speaker")
                if not str(beat.get("line") or "").strip():
                    hard.append(f"[E3] {sid} 第 {j} 拍是对白拍却没写 line")
    if len(hard) > 24:                      # 回炉指令要能读：先修前 24 条，别一次灌爆
        hard = hard[:24] + [f"……另有 {len(hard) - 24} 条同类问题（先修上面这些）"]
    return hard


def main():
    parser = argparse.ArgumentParser(description="DramaCraft 工业级小说转短剧剧本双轨制引擎")
    parser.add_argument("--input", help="小说源文件路径（txt/md）、章节目录或节选文本")
    parser.add_argument("--outline", help="大纲 outline.json 路径（必填，无默认值——大纲与参数是向用户确认后产出的业务件）")
    parser.add_argument("--nodes", help="模型节点池配置文件 (默认按三级发现链自动读取 llm_nodes.json)")
    parser.add_argument("--workdir", default="workspace", help="运行期状态缓存与产物工作区")
    parser.add_argument("--target-seconds", type=int, help="单集目标时长秒数（必填；全剧统一，须与用户确认，常见 90~150）")
    parser.add_argument("--max-calls", type=int, help="模型调用总预算上限（熔断保险丝）。缺省自动按待跑集数装闸；触闸可续跑止损 exit 3")
    parser.add_argument("--stale-cache", choices=["ask", "redo", "trust"], default="ask",
                        help="旧版本/哈希失配缓存处置策略: ask(默认,拒跑并提示)/redo(删除重跑)/trust(信任放行)")
    parser.add_argument("--art", help="美术设定 art.json 路径（可选；提供后门10 refs-scenes 实扫场景/光照/道具对账）")
    parser.add_argument("--concurrency", type=int, default=3,
                        help="分集并发执行工作线程数（默认 3；设为 1 为纯串行模式）")
    parser.add_argument("--formats", nargs="+", default=["markdown", "csv", "json", "html"], help="导出的格式列表")
    parser.add_argument("--assemble-only", action="store_true", help="仅聚合已有集数并导出多格式产物")
    parser.add_argument("--validate-only", help="仅对指定剧本 json 运行 12 道门质量检测")
    parser.add_argument("--stamp-only", help="仅给单集缓存补 _meta 元数据（工作流派工人在外面写件时用；须配 --outline）")
    parser.add_argument("--stamped-by", help="配 --stamp-only：在库里登记「这份稿是谁写进来的」（工作流派工写件记「工作流写手」）")
    parser.add_argument("--check-ep", help="只查单集件的拍/场形状契约（location / type / speaker / line），合规 exit 0、违规 exit 1")
    parser.add_argument("--merge-into", help="配 --check-ep：把形状硬伤并进这份已有报告（熔断器读的就是它）")
    parser.add_argument("--json", dest="json_path", help="配 --check-ep：形状检查报告保存路径（JSON）")
    parser.add_argument("--doctor", action="store_true", help="执行运行环境、依赖与模型配置健康体检")
    parser.add_argument("--probe-api", action="store_true", help="配合 --doctor 时向配置的首个模型发送极简请求探测连通性")
    args = parser.parse_args()

    # 0. 环境健康体检模式
    if args.doctor:
        passed, report = run_doctor(nodes_path=args.nodes, probe_api=args.probe_api)
        print(report)
        sys.exit(0 if passed else 1)

    # 0.1 核心依赖跑前 0.01 秒静默拦截（防质检崩溃与白花额度）
    node_ok, node_msg = check_node_available()
    if not node_ok:
        print("❌ 环境异常：未检测到 Node.js 运行时！")
        print(f"   {node_msg}")
        sys.exit(2)

    # 0.2 单集件形状契约检查（工作流在写手过门之前调它；见 lint_ep_shape 的注释）
    if args.check_ep:
        ep_path = os.path.abspath(args.check_ep)
        if not os.path.exists(ep_path):
            print(f"❌ 错误: 单集件不存在: {args.check_ep}")
            sys.exit(1)
        try:
            with open(ep_path, "r", encoding="utf-8") as fp:
                ep_obj = json.load(fp)
        except (OSError, ValueError) as e:
            print(f"❌ 错误: 单集件读不出来: {args.check_ep}（{e}）")
            sys.exit(1)
        hard = lint_ep_shape(ep_obj)
        if args.merge_into and os.path.exists(args.merge_into):
            # 并进 12 门报告：熔断器读的是那一份，形状硬伤必须让它看见（否则「过门」是假的）
            try:
                with open(args.merge_into, "r", encoding="utf-8") as fp:
                    doc = json.load(fp)
            except (OSError, ValueError):
                doc = {}
            if isinstance(doc, dict):
                doc["hard"] = list(doc.get("hard") or []) + hard
                doc["shapeChecked"] = True
                safe_write_json(os.path.abspath(args.merge_into), doc)
        if args.json_path:
            safe_write_json(os.path.abspath(args.json_path),
                            {"ep": ep_obj.get("ep"), "ok": not hard, "hard": hard, "warn": []})
        if hard:
            print(f"✗ 第 {ep_obj.get('ep')} 集剧本件不合拍/场契约（{len(hard)} 条）：")
            for h in hard:
                print(f"   ✗ {h}")
            print("   契约见 references/WORKFLOW.md 二：每场必须有 location，每一拍必须有 type（action/dialogue）。")
            sys.exit(1)
        print(f"✓ 第 {ep_obj.get('ep')} 集剧本件拍/场契约齐备（每场有地点、每拍有类型）")
        sys.exit(0)

    # 1. 单独校验模式
    if args.validate_only:
        script_path = os.path.abspath(args.validate_only)
        outline_path = None
        if args.outline:
            if not os.path.exists(args.outline):
                print(f"❌ 错误: 大纲文件不存在: {args.outline}")
                sys.exit(1)
            outline_path = os.path.abspath(args.outline)
        script_data = json.load(open(script_path, "r", encoding="utf-8"))
        outline_data = json.load(open(outline_path, "r", encoding="utf-8")) if outline_path else None
        if outline_data is not None:
            _enforce_outline_gate(outline_data, outline_path)
        print("\n【创作纵深哨兵（单集校验模式）】")
        print("  跳过（单集校验模式）：伏笔回收/术语/命名/节奏这些账本类检查需要运行期工作区，"
              "本次不参与判定，退出码只由 12 道质量门决定。")
        art_data = None
        if args.art:
            if not os.path.exists(args.art):
                print(f"❌ 错误: 美术设定文件不存在: {args.art}")
                sys.exit(1)
            art_data = json.load(open(args.art, "r", encoding="utf-8"))
        passed, report, failed = run_quality_gate(script_data, outline_data, art_data)
        print(report)
        sys.exit(0 if passed else 1)

    # 1.1 单集件补 _meta 模式：工作流模式下剧本由子代理写在外面，
    #     而缓存安检（有 _meta / schemaVersion≥2 / outlineHash 对上）是 --assemble-only 的硬前提。
    if args.stamp_only:
        if not args.outline:
            print("❌ 错误: --stamp-only 必须配 --outline（_meta.outlineHash 要对上大纲，否则缓存安检照样拒收）")
            sys.exit(1)
        if not os.path.exists(args.stamp_only):
            print(f"❌ 错误: 单集件不存在: {args.stamp_only}")
            sys.exit(1)
        if not os.path.exists(args.outline):
            print(f"❌ 错误: 大纲文件不存在: {args.outline}")
            sys.exit(1)
        outline_data = json.load(open(args.outline, "r", encoding="utf-8"))
        ep_obj = json.load(open(args.stamp_only, "r", encoding="utf-8"))
        # 补章＝发通行证：不合拍/场契约的件一律不许入库（真跑实测：放过去的件会在两个相位之后炸）
        shape_hard = lint_ep_shape(ep_obj)
        if shape_hard:
            print(f"✗ 第 {ep_obj.get('ep')} 集剧本件不合拍/场契约，不能补章入库（{len(shape_hard)} 条）：")
            for h in shape_hard:
                print(f"   ✗ {h}")
            print("   先按 references/WORKFLOW.md 二 的契约把字段补齐（每场 location、每拍 type），再补章。")
            sys.exit(1)
        meta = stamp_ep_meta(ep_obj, compute_outline_hash(outline_data))
        safe_write_json(os.path.abspath(args.stamp_only), ep_obj)
        # 入库登记（v1.5.1 T2）：这份稿是外面写进来的（工作流派工写件/人工手改），引擎账本没有
        # 它的生成记录。登记一笔，生产记录的来源栏才写得出「外部入库」，而不是靠猜。
        ep_abs = os.path.abspath(args.stamp_only)
        ep_dir = os.path.dirname(ep_abs)
        if os.path.basename(ep_dir) == "episodes" and os.path.basename(os.path.dirname(ep_dir)) == "state":
            ws = os.path.dirname(os.path.dirname(ep_dir))
            fields = {"path": os.path.relpath(ep_abs, ws)}
            if args.stamped_by:
                fields["by"] = args.stamped_by
            Ledger(os.path.join(ws, "state", "ledger.jsonl")).emit(
                "episode_imported", ep=ep_obj.get("ep"), **fields)
        print(f"✓ 已补 _meta：第 {ep_obj.get('ep')} 集（outlineHash {meta['outlineHash']}）")
        sys.exit(0)

    # 业务参数硬闸：目标时长与大纲必须显式提供。
    # 静默默认值曾让跑批绕过「向用户确认参数」直接开工（2026-09-21 分发事故），这里从代码层面堵死。
    if not args.outline:
        parser.error("缺少 --outline：先按 SKILL.md 向用户确认时长/集数/导出格式，产出大纲后显式指定其路径。引擎不提供默认大纲（fixtures 样例仅作格式参考）。")
    if args.target_seconds is None:
        parser.error("缺少 --target-seconds：单集目标时长是全剧统一的业务参数，必须与用户确认后显式指定（常见 90~150）。")

    workdir = os.path.abspath(args.workdir)
    episodes_dir = os.path.join(workdir, "state", "episodes")
    output_dir = os.path.join(workdir, "output")
    os.makedirs(episodes_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    # 2. 加载大纲
    outline_path = os.path.abspath(args.outline)
    if not os.path.exists(outline_path):
        print(f"❌ 错误: 大纲文件不存在: {outline_path}")
        sys.exit(1)
    outline_data = json.load(open(outline_path, "r", encoding="utf-8"))
    _enforce_outline_gate(outline_data, outline_path, workdir=workdir)
    title = outline_data.get("title", "未命名短剧")
    outline_hash = compute_outline_hash(outline_data)

    # 反塞防线：目标时长是全剧统一的业务参数。逐集乱填 targetSeconds（例如为了
    # 让时长门通过把后几集目标缩到 17 秒）是凑数门禁的典型指纹，直接拒跑。
    mismatched = [
        ep.get("ep") for ep in outline_data.get("episodes", [])
        if ep.get("targetSeconds") and ep["targetSeconds"] != args.target_seconds
    ]
    if mismatched:
        print(f"❌ 大纲第 {mismatched} 集的 targetSeconds 与命令行统一目标 {args.target_seconds} 秒不一致。")
        print("   目标时长必须全剧统一且来自用户确认——为过时长门逐集反塞 targetSeconds 属违规操作。")
        print("   正确做法：时长不达标就加厚/删水剧本本身；或拿用户确认后的新数值重建大纲。")
        sys.exit(2)

    art_data = None
    if args.art:
        if not os.path.exists(args.art):
            print(f"❌ 错误: 美术设定文件不存在: {args.art}")
            sys.exit(1)
        art_data = json.load(open(args.art, "r", encoding="utf-8"))
        print(f"[美术设定] 已装载 {args.art}，门10 refs-scenes 将实扫场景/道具对账")

    naming_seeds = build_naming_seeds(outline_data, art_data)

    print(f"================================================================")
    print(f"🎬 DramaCraft Engine — 双轨制短剧自动化流水线启动")
    print(f"剧作标题: 《{title}》 | 规划集数: {len(outline_data.get('episodes', []))} 集")
    print(f"单集时长: {args.target_seconds} 秒 | 导出格式: {args.formats}")
    print(f"工作区: {workdir}")
    print(f"================================================================\n")

    # 3. 输入小说处理（如果指定）
    chapters = []
    if args.input:
        print(f"[输入适配] 正在解析小说输入: {args.input} ...")
        chapters = load_novel(args.input)
        total_chars = sum(c.char_count for c in chapters)
        print(f"✓ 解析完成: 共识别出 {len(chapters)} 个章节分块，累计字数: {total_chars} 字。\n")

    # 4. 检查已完成集数（断点安检三查）
    completed_eps = []
    stale_eps = []
    for ep_item in outline_data.get("episodes", []):
        ep_n = ep_item.get("ep")
        c_path = os.path.join(episodes_dir, f"ep{ep_n:03d}.json")
        if os.path.exists(c_path):
            try:
                with open(c_path, "r", encoding="utf-8") as fp:
                    ep_obj = json.load(fp)
            except Exception:
                stale_eps.append((ep_n, "缓存文件损坏无法解析", c_path))
                continue

            meta = ep_obj.get("_meta")
            if not meta or not isinstance(meta, dict):
                stale_eps.append((ep_n, "缺少 _meta 元数据（旧版本缓存）", c_path))
            elif meta.get("schemaVersion", 0) < 2:
                stale_eps.append((ep_n, f"schemaVersion 过低 ({meta.get('schemaVersion')})", c_path))
            elif meta.get("outlineHash") != outline_hash:
                stale_eps.append((ep_n, "大纲哈希失配（大纲人物/节拍已修改）", c_path))
            else:
                completed_eps.append(ep_n)

    # 处置过期或旧版缓存
    if stale_eps:
        if args.stale_cache == "ask":
            print(f"\n❌ 发现 {len(stale_eps)} 集过期或旧版本缓存（断点安检未通过）：")
            for ep_n, reason, _ in stale_eps:
                print(f"   - 第 {ep_n} 集: {reason}")
            print("\n   请显式指定 --stale-cache 处置策略：")
            print("      --stale-cache redo   : 删除旧缓存并重新自愈生成（推荐）")
            print("      --stale-cache trust  : 信任并沿用旧缓存（标记为 legacy）")
            print("   若是主动改了大纲结构，建议先照 references/大纲变更单模板.md 登记一张变更单留痕（集数/人物/伏笔改动与处置策略），交付时随生产记录一并给用户。")
            ledger = Ledger(os.path.join(workdir, "state", "ledger.jsonl"))
            for ep_n, reason, _ in stale_eps:
                ledger.emit("stale_cache", ep=ep_n, reason=reason, action="ask")
            sys.exit(2)
        elif args.stale_cache == "redo":
            print(f"[缓存清理] 按 --stale-cache redo 策略，自动删除 {len(stale_eps)} 集旧缓存：{[s[0] for s in stale_eps]}")
            ledger = Ledger(os.path.join(workdir, "state", "ledger.jsonl"))
            for ep_n, reason, p in stale_eps:
                if os.path.exists(p):
                    os.remove(p)
                ledger.emit("stale_cache", ep=ep_n, reason=reason, action="redo")
        elif args.stale_cache == "trust":
            print(f"[缓存信任] 按 --stale-cache trust 策略，放行 {len(stale_eps)} 集旧缓存：{[s[0] for s in stale_eps]}")
            ledger = Ledger(os.path.join(workdir, "state", "ledger.jsonl"))
            for ep_n, reason, _ in stale_eps:
                completed_eps.append(ep_n)
                ledger.emit("stale_cache", ep=ep_n, reason=reason, action="trust")

    pending_eps = len(outline_data.get("episodes", [])) - len(completed_eps)
    budget_cap = args.max_calls if args.max_calls is not None else _default_budget(pending_eps)

    # 断点恢复卡：既有安检结果直接渲染成 4 行白话（不新建扫描，数字=实扫磁盘+账本重放）
    pending_ep_nums = [ep.get("ep") for ep in outline_data.get("episodes", [])
                       if ep.get("ep") not in completed_eps]
    f2_open = [v for v in lint_foreshadow_states(
        replay(workdir, set(completed_eps), load_ep_hashes(workdir)), None,
        set(completed_eps)) if v.startswith("[F2]")]
    print()
    print(render_resume_card(workdir, completed_eps, pending_ep_nums, title,
                             f2_unresolved=len(f2_open)))
    # 改稿下游失效链（v1.5.0 T4）：剧本被重做/手改后，旧规划/旧镜头/旧监督不得复用——
    # 这里把待重跑的集报出来（本 CLI 只产剧本；重跑下游由工作流或人接手）。
    _stale_downstream = _downstream_stale_eps(workdir, [ep_item.get("ep") for ep_item in outline_data.get("episodes", [])])
    if _stale_downstream:
        from dramacraft.artifact_chain import STAGE_LABELS
        preview = "、".join(
            f"第 {n} 集（{'、'.join(STAGE_LABELS.get(s, s) for s in stages)}）"
            for n, stages in sorted(_stale_downstream.items())[:8])
        more = f" 等 {len(_stale_downstream)} 集" if len(_stale_downstream) > 8 else ""
        print(f"· 后续环节待重做：{preview}{more}")
        print("  剧本改过之后，这些集的导演规划/镜头组/监督必须重做，旧的不会被自动沿用。")
        print("  下一步：把这一集剧本定下来后，跑一遍产线工作流即可补齐这些环节（剧本本身不用重做）。")
    ledger = Ledger(os.path.join(workdir, "state", "ledger.jsonl"))
    ledger.emit("run_start",
                episodes_total=len(outline_data.get("episodes", [])),
                cached=len(completed_eps),
                target_seconds=args.target_seconds,
                outline_hash=outline_hash,
                budget_cap=budget_cap)

    # 逐集安检结果入账（v1.5.1 T1）：生产记录必须由「本轮逐集事件」判定。
    # 只写汇总的 cached=N 会被误读成「前 N 集就绪」——只缓存第 3 集时不能推断第 1 集在库。
    for _ep_n in completed_eps:
        ledger.emit("episode_cached", ep=_ep_n, verified="cache_check",
                    path=os.path.relpath(os.path.join(episodes_dir, f"ep{_ep_n:03d}.json"), workdir))

    # 5. 批量推进（若非 assemble-only 且有待跑集数）
    f2_warnings: list = []
    blocked_eps: list = []          # 统一熔断未收敛（escalate/abort）→ 未交付集
    blocked_lock = threading.Lock()
    pending_items = [ep for ep in outline_data.get("episodes", []) if ep.get("ep") not in completed_eps]
    if not args.assemble_only and pending_items:
        actual_workers = max(1, min(args.concurrency, len(pending_items)))
        gateway = LLMGateway(config_path=args.nodes, max_calls=budget_cap)
        if hasattr(gateway, "ledger"):
            gateway.ledger = ledger
        worker_str = f"，启动 {actual_workers} 线程并发跑批" if actual_workers > 1 else "，启动单线程串行跑批"
        print(f"[网关就绪] 已装载 {len(getattr(gateway, 'nodes', []))} 个 LLM 节点"
              + (f"，预算保险丝 {budget_cap} 次调用" if budget_cap else "（未装闸）")
              + f"{worker_str}...")

        stop_event = threading.Event()
        f2_lock = threading.Lock()
        if naming_seeds is None:
            print("[命名哨兵] 未提供人物/美术种子，术语与命名检查本剧跳过。")

        def _worker_task(ep_item):
            ep_n = ep_item.get("ep")
            if stop_event.is_set():
                return
            c_path = os.path.join(episodes_dir, f"ep{ep_n:03d}.json")
            if os.path.exists(c_path):
                return
            print(f"\n>>> [第 {ep_n} 集] 开始自愈式剧本生成 (目标 {args.target_seconds} 秒)...")
            ep_meta = dict(ep_item)
            ep_meta["targetSeconds"] = args.target_seconds
            gateway.current_ep = ep_n

            # 对应原文章节参考（如果有输入小说）
            source_ctx = None
            if chapters:
                target_ch_idx = ep_item.get("chapterIndex", ep_n)
                matched_ch = next((c for c in chapters if c.index == target_ch_idx), None)
                if matched_ch:
                    source_ctx = f"章节标题: {matched_ch.title}\n{matched_ch.content[:2000]}"

            # 写手上下文包：原著章节 + 本集应回收 + 全剧未回收概览（初稿与回炉同源）
            fh_states = replay(workdir, set(completed_eps), load_ep_hashes(workdir))
            ctx_block = build_context_block(source_ctx, foreshadow_todo_for_ep(fh_states, ep_n),
                                            foreshadow_open_overview(fh_states))
            t_gate = (lambda d: ep_level_checks(d, naming_seeds)) if naming_seeds else None

            try:
                ep_data = generate_episode_with_healing(
                    ep_meta=ep_meta,
                    gateway=gateway,
                    outline_data=outline_data,
                    max_retries=3,
                    art_data=art_data,
                    ledger=ledger,
                    source_context=source_ctx,
                    context_block=ctx_block,
                    t_gate_fn=t_gate,
                    workdir=workdir
                )
            except HealingBlocked as hb:
                # 统一熔断收束（v1.5.0 T1）：人不在场 → 本集标未交付，产线继续跑其余集
                with blocked_lock:
                    blocked_eps.append({"ep": ep_n, "verdict": hb.verdict, "detail": hb.detail})
                ledger.emit("episode_undelivered", ep=ep_n, verdict=hb.verdict,
                            rounds=hb.rounds, detail_digest=hb.detail[:500])
                reason = "连续两轮阻塞项未减少，需人工拍板" if hb.verdict == "escalate" else "达到回炉硬顶"
                print(f"⚠ [第 {ep_n} 集] 回炉未收敛（{reason}）——本集未交付，产线继续其余集。")
                return
            stamp_ep_meta(ep_data, outline_hash)
            safe_write_json(c_path, ep_data)
            ledger.emit("episode_cached", ep=ep_n, path=os.path.relpath(c_path, workdir))
            print(f"✓ [第 {ep_n} 集] 100% 通过 12 道质量门，已安全缓存落盘！")

            # F2 认领对账（落盘后跑；⚠ 只进报告，不进回炉指令——防为过闸硬塞假填坑）
            warns = register_paid_claims(
                workdir, ep_data, ep_n,
                replay(workdir, set(completed_eps) | {ep_n}, load_ep_hashes(workdir)))
            if warns:
                with f2_lock:
                    f2_warnings.extend(warns)

        first_budget_error = None
        first_runtime_error = None
        failed_ep = None

        if actual_workers == 1:
            for ep_item in pending_items:
                ep_n = ep_item.get("ep")
                try:
                    _worker_task(ep_item)
                except BudgetExhausted as be:
                    first_budget_error = be
                    break
                except Exception as e:
                    first_runtime_error = e
                    failed_ep = ep_n
                    break
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=actual_workers) as executor:
                future_to_ep = {
                    executor.submit(_worker_task, ep_item): ep_item.get("ep")
                    for ep_item in pending_items
                }
                for future in concurrent.futures.as_completed(future_to_ep):
                    ep_n = future_to_ep[future]
                    try:
                        future.result()
                    except BudgetExhausted as be:
                        stop_event.set()
                        if not first_budget_error:
                            first_budget_error = be
                    except Exception as e:
                        stop_event.set()
                        if not first_runtime_error:
                            first_runtime_error = e
                            failed_ep = ep_n

        # 真异常优先于预算收束（评审 Should#1）：两者并存时，"充值重跑即可"是误导，
        # 必须先把真实故障浮出来。
        if first_runtime_error:
            ledger.emit("run_finish", status="gen_failed", failed_ep=failed_ep,
                        error=str(first_runtime_error)[:200])
            _write_production_log(ledger, workdir, outline_data)
            print(f"❌ 第 {failed_ep} 集自愈生成失败: {first_runtime_error}")
            if first_budget_error:
                print(f"   另：本批同时触到预算上限（{first_budget_error}）——修好上面的问题后再续跑。")
            sys.exit(1)

        if first_budget_error:
            calls_used = getattr(gateway, "call_count", -1)
            ledger.emit("budget_stop", calls_used=calls_used, cap=gateway.max_calls)
            ledger.emit("run_finish", status="budget", calls_used=calls_used, cap=gateway.max_calls)
            _write_production_log(ledger, workdir, outline_data)
            print(f"\n⛔ {first_budget_error}——已按预算止损停批。")
            print(f"   已完成 {len([f for f in os.listdir(episodes_dir) if f.endswith('.json')])} 集全部在缓存中。")
            print("   充值/换节点后原命令重跑即断点续跑，已完成集不重复计费。")
            sys.exit(3)

    # 5.4 回炉未收敛（统一熔断 escalate/abort，v1.5.0 T1）：本集未交付，但整批不中断
    # 语义：其余集已全部在缓存中 → 修好受阻集后原命令重跑即可补齐并汇编交付（不重复计费）。
    if blocked_eps:
        ready = len([f for f in os.listdir(episodes_dir) if f.endswith(".json")])
        print("\n【自动修改未达标 · 未交付集清单】")
        for b in sorted(blocked_eps, key=lambda x: x["ep"]):
            reason = ("连续两轮修改后违规项没有减少，需要人来定稿"
                      if b["verdict"] == "escalate" else "已达最大修改轮次，本轮放弃这一集")
            print(f"   ✗ 第 {b['ep']} 集：{reason}")
            print(f"     问题摘要：{_one_line(b['detail'])[:300]}")
        print(f"   其余 {ready} 集剧本已合格并存在缓存里（{os.path.relpath(episodes_dir, workdir)}），不需重做。")
        print("   下一步：照上面的摘要把问题改掉（改大纲或改分集剧情都行），再重新执行刚才那条命令，")
        print("   系统会自动补齐这几集，并继续汇编导出交付物。")
        ledger.emit("run_finish", status="partial", blocked_eps=[b["ep"] for b in blocked_eps],
                    ready_eps=ready)
        _write_production_log(ledger, workdir, outline_data)
        sys.exit(2)

    # 5.5 伏笔认领对账告警（F2：只进报告，不进回炉指令）
    if f2_warnings:
        print("\n【伏笔认领对账告警（不计入 12 门，不触发回炉）】")
        for w in dict.fromkeys(f2_warnings):
            print(f"⚠ {w}")

    # 5.6 缺集防空（评审 Must#1）：大纲规划了但库里没有的集，一律不许悄悄汇编交付。
    # 依据方案 §11「全剧终验运行：大纲全部集数在库后才执行戳章导出；缺集则终验红并列出缺号」。
    planned_eps = [ep_item.get("ep") for ep_item in outline_data.get("episodes", [])]
    missing_eps = [ep_n for ep_n in planned_eps
                   if not os.path.exists(os.path.join(episodes_dir, f"ep{ep_n:03d}.json"))]
    if missing_eps:
        print("\n【缺集 · 本轮不出交付物】")
        print(f"   大纲规划 {len(planned_eps)} 集，库里只有 {len(planned_eps) - len(missing_eps)} 集，"
              f"缺：{'、'.join(f'第 {n} 集' for n in missing_eps)}")
        print("   缺集不汇编、不盖交付戳（汇编半部剧本会被误当成全剧成品）。")
        print("   下一步：原命令重跑补上这几集（已完成集不重复计费），或先确认大纲集数是否改过。")
        ledger.emit("run_finish", status="missing_episodes", missing_eps=missing_eps)
        _write_production_log(ledger, workdir, outline_data)
        sys.exit(2)

    # 6. 全量聚合与多格式导出
    print(f"\n[全量聚合] 正在汇编全剧完整剧本并导出交付物...")
    merged_script = {
        "source": title,
        "version": "3.0.0",
        "mode": outline_data.get("mode", "serial"),
        "config": {
            "targetSeconds": args.target_seconds
        },
        "episodes": []
    }
    merged_ep_nums = []
    merged_ep_hashes = {}
    for ep_item in outline_data.get("episodes", []):
        ep_n = ep_item.get("ep")
        c_path = os.path.join(episodes_dir, f"ep{ep_n:03d}.json")
        if os.path.exists(c_path):
            ep_obj = json.load(open(c_path, "r", encoding="utf-8"))
            if "_meta" in ep_obj:
                ep_obj = dict(ep_obj)
                del ep_obj["_meta"]
            merged_script["episodes"].append(ep_obj)
            merged_ep_nums.append(ep_n)
            merged_ep_hashes[ep_n] = ep_content_hash(ep_obj)

    # 终验序（v1.4.0 定案）：12 门汇编终验 → 创作纵深终扫（F3+T 同场硬闸）→ 导出四件套
    passed, report, failed = run_quality_gate(merged_script, outline_data, art_data)
    print(report)
    if not passed:
        ledger.emit("run_finish", status="gate_failed", failed_items=len(failed))
        _write_production_log(ledger, workdir, outline_data)
        print("❌ 汇编剧本终验未通过，请检查具体报错！")
        sys.exit(1)

    # 全绿不豁免：12 门过后照样跑创作纵深终扫，未回收伏笔/命名残留一律拦在交付前。
    # 有效缓存集取「本次真正汇编进交付物的集号」——本轮新产出的集也据此生效。
    f3_residual = final_foreshadow_sweep(workdir, set(merged_ep_nums), outline_data,
                                         merged_ep_hashes)
    t_residual = final_naming_sweep(merged_script, naming_seeds)
    if f3_residual or t_residual:
        print("\n【创作纵深终扫未通过】")
        for v in f3_residual + t_residual:
            print(f"   ✗ {v}")
        print("   12 道质量门全绿不等于可以交付——埋了的伏笔必须收，禁用称呼必须改。")
        ledger.emit("run_finish", status="continuity_failed",
                    foreshadow_residual=len(f3_residual), naming_residual=len(t_residual))
        _write_production_log(ledger, workdir, outline_data)
        sys.exit(1)

    # 导出四件套
    exported = export_all(
        script_data=merged_script,
        outline_data=outline_data,
        output_dir=output_dir,
        formats=args.formats
    )
    ledger.emit("run_finish", status="passed", episodes=len(merged_script["episodes"]))
    _write_production_log(ledger, workdir, outline_data)
    print("================================================================")
    print("🎉 全流程批量跑批成功！交付物清单如下：")
    for fmt, fpath in exported.items():
        print(f"  - [{fmt.upper()}] -> {fpath} ({os.path.getsize(fpath)} bytes)")
    print("================================================================")
    print(EXPORT_STYLE_CHECKLIST)


if __name__ == "__main__":
    main()
