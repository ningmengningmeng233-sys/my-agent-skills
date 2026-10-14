from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Dict, FrozenSet, List, Optional, Tuple, Any

from dramacraft.breaker import (
    DEFAULT_HEAL_ROUND_CAP,
    VERDICT_ABORT,
    VERDICT_ESCALATE,
    VERDICT_PASS,
    Problem,
    append_round,
    breaker_decision,
    from_failed_items,
)

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VALIDATOR_PATH = os.path.join(ROOT_DIR, "skills_runtime", "dramacraft_patch", "validate.mjs")
AI_TONE_EXAMPLES_PATH = os.path.join(ROOT_DIR, "skills_runtime", "dramacraft_patch",
                                     "ai_tone_examples.json")

_vocab_hits_lock = threading.Lock()

# 正向写作纪律（humanizer PERSONALITY/SOUL 节的短剧台词版）——「只除味不注入人味」只算半件事
HUMAN_SOUL_DISCIPLINE = """【写出人味】——合格≠有人味，以下四条是硬要求：
1. 句长要有起伏：短句砸下去（三五个字），再让长句慢慢铺开；全篇一个节奏就是机器稿。
2. 人物要有立场与犹疑：关键选择上让他犹豫半拍，写出来。全知全能、斩钉截铁的是说明书不是人。
3. 话留半句：台词别把意思说满，剩下的让动作与沉默去接；把道理讲透的对白最假。
4. 具体压过抽象：写「手在抖」「杯子没端稳」，不写「内心复杂」「若有所思」。"""

DEFAULT_SYSTEM_PROMPT = ("你是短剧编剧专家，严格按照节拍流输出合规的剧本 JSON。\n\n"
                         + HUMAN_SOUL_DISCIPLINE)


def render_ai_tone_examples(hits: Optional[List[Dict[str, Any]]], limit: int = 3) -> str:
    """把本轮命中的 AI 腔类别渲染成「改前 → 改后」示范段（humanizer Changes made 口径）。

    读 ai_tone_examples.json；文件缺失或损坏则静默返回空串（不得成为新的故障点）。
    """
    if not hits:
        return ""
    try:
        with open(AI_TONE_EXAMPLES_PATH, "r", encoding="utf-8") as fp:
            pack = json.load(fp)
    except (OSError, ValueError):
        return ""
    if not isinstance(pack, dict):
        return ""

    hit_names = {str(h.get("pattern", "")) for h in hits if isinstance(h, dict)}
    hit_names.discard("")
    if not hit_names:
        return ""

    blocks: List[str] = []
    for cid, item in pack.items():
        if str(cid).startswith("_") or not isinstance(item, dict):
            continue
        if not (set(item.get("patterns") or []) & hit_names):
            continue
        blocks.append(f"· {item.get('name', '')}（{item.get('why', '')}）\n"
                      f"  改前：{item.get('before', '')}\n"
                      f"  改后：{item.get('after', '')}")
        if len(blocks) >= limit:
            break
    if not blocks:
        return ""
    return "【AI 味改写示范】照这个路子改，别只做同义替换：\n" + "\n".join(blocks)


def lint_authoritative_fields(
    script_data: Dict[str, Any],
    outline_data: Optional[Dict[str, Any]] = None,
    expected_target_seconds: Optional[int] = None,
    expected_ep: Optional[int] = None,
) -> List[str]:
    """DR-02：权威集号与时长绑定检查，禁信模型自报 ep/targetSeconds。

    - 缺失 targetSeconds / 非正整数 → 硬伤；
    - expected_ep / expected_target_seconds 给定时与模型值比对，不一致 → 硬伤；
    - outline 给定时集号必须在大纲规划内，且大纲载明 targetSeconds 的集必须值一致。
    返回硬伤条目（空 = 通过）。
    """
    hard: List[str] = []
    eps = script_data.get("episodes", []) if isinstance(script_data, dict) else []
    outline_eps: Dict[int, Dict[str, Any]] = {}
    if isinstance(outline_data, dict):
        for e in outline_data.get("episodes", []) or []:
            if isinstance(e, dict) and isinstance(e.get("ep"), int) and not isinstance(e.get("ep"), bool):
                outline_eps[e["ep"]] = e
    for ep in eps:
        if not isinstance(ep, dict):
            hard.append("集条目不是对象（禁止信任模型输出形状）")
            continue
        ep_n = ep.get("ep")
        tgt = ep.get("targetSeconds")
        label = f"第 {ep_n} 集"
        if expected_ep is not None and ep_n != expected_ep:
            hard.append(f"{label}集号与权威集号 {expected_ep} 不一致（禁止信任模型自报 ep）")
        if outline_eps and isinstance(ep_n, int) and not isinstance(ep_n, bool):
            if ep_n not in outline_eps:
                hard.append(f"{label}集号 {ep_n} 不在大纲规划内（禁止模型自报集号）")
        if not (isinstance(tgt, int) and not isinstance(tgt, bool) and tgt > 0):
            hard.append(f"{label}缺 targetSeconds（须与大纲/启动参数一致，禁止模型自报缺失）")
            continue
        if expected_target_seconds is not None and tgt != expected_target_seconds:
            hard.append(
                f"{label}的 targetSeconds {tgt}s 与权威 {expected_target_seconds}s 不一致"
                "（禁止信任模型自报）"
            )
        elif outline_eps and isinstance(ep_n, int) and ep_n in outline_eps:
            otgt = outline_eps[ep_n].get("targetSeconds")
            if isinstance(otgt, int) and not isinstance(otgt, bool) and otgt > 0 and tgt != otgt:
                hard.append(
                    f"{label}的 targetSeconds {tgt}s 与大纲权威 {otgt}s 不一致"
                    "（禁止信任模型自报）"
                )
    return hard


def run_quality_gate(script_data: Dict[str, Any], outline_data: Optional[Dict[str, Any]] = None,
                     art_data: Optional[Dict[str, Any]] = None,
                     hits_collector: Optional[List[Dict[str, Any]]] = None,
                     gates_collector: Optional[List[Dict[str, Any]]] = None,
                     expected_target_seconds: Optional[int] = None,
                     expected_ep: Optional[int] = None) -> Tuple[bool, str, List[str]]:
    """
    运行本地 12 道确定性质量门检测（art_data 提供时门10 refs-scenes 实扫，否则跳过）。
    优先通过 validate.mjs --json 提取机器结构化违规项与命中词，淘汰脆弱的纯文本行正则解析。
    :param gates_collector: 传入时把 validate.mjs 的原始 gates 数组原样收进去（单集验证入口要转交熔断器）
    :param expected_target_seconds: 权威单集时长（CLI --target-seconds），给定时与模型自报比对
    :param expected_ep: 权威集号（单集生成时的大纲集号），给定时与模型自报比对
    :return: (是否全绿通过, 完整终端报告文本, 违规条目列表)
    """
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f_sc:
        json.dump(script_data, f_sc, ensure_ascii=False, indent=2)
        sc_path = f_sc.name

    out_path = None
    if outline_data is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f_out:
            json.dump(outline_data, f_out, ensure_ascii=False, indent=2)
            out_path = f_out.name

    art_path = None
    if art_data is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f_art:
            json.dump(art_data, f_art, ensure_ascii=False, indent=2)
            art_path = f_art.name

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f_json:
        json_path = f_json.name

    try:
        cmd = ["node", VALIDATOR_PATH, sc_path, "--json", json_path]
        if out_path:
            cmd.extend(["--outline", out_path])
        if art_path:
            cmd.extend(["--art", art_path])

        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        output = res.stdout + "\n" + res.stderr
        passed = (res.returncode == 0)

        failed_items = []
        hits = []
        if os.path.exists(json_path) and os.path.getsize(json_path) > 0:
            try:
                with open(json_path, "r", encoding="utf-8") as jf:
                    jdata = json.load(jf)
                hits = jdata.get("hits", [])
                if hits_collector is not None:
                    hits_collector.extend(hits)
                for g in jdata.get("gates", []):
                    if gates_collector is not None:
                        gates_collector.append(g)
                    if not g.get("ok"):
                        failed_items.append(f"✗ [违规] {g.get('label')}")
                        if g.get("detail"):
                            # 详情拆分为条目，保持与旧版自愈 prompt 相同粒度
                            for d in str(g.get("detail")).split("；"):
                                d_str = d.strip()
                                if d_str:
                                    failed_items.append(d_str)
            except Exception:
                pass

        if not failed_items and not passed:
            # 降级容错：若 json 未能成功解析但进程非 0
            in_fail_block = False
            for line in output.splitlines():
                line_str = line.strip()
                if line_str.startswith("✗ [违规]"):
                    in_fail_block = True
                    failed_items.append(line_str)
                elif line_str.startswith("✓ [通过]"):
                    in_fail_block = False
                elif line_str.startswith("↳ 详情:") and in_fail_block:
                    failed_items.append(line_str.replace("↳ 详情:", "").strip())

        # DR-02：权威绑定硬伤与 12 门结果合并——模型自报不一致即判失败，
        # 不因 12 门全绿而放行；同时保留 12 门明细供回炉定位。
        auth_hard = lint_authoritative_fields(
            script_data, outline_data, expected_target_seconds, expected_ep
        )
        if auth_hard:
            if gates_collector is not None:
                gates_collector.append({
                    "id": "auth-binding",
                    "label": "权威集号与时长绑定（ep/targetSeconds）",
                    "ok": False,
                    "detail": "；".join(auth_hard),
                })
            failed_items = ["✗ [违规] 权威集号与时长绑定（ep/targetSeconds）"] + auth_hard + failed_items
            output += "\n✗ [违规] 权威集号与时长绑定（ep/targetSeconds）\n"
            for h in auth_hard:
                output += f"    ↳ 详情: {h}\n"
            passed = False

        return passed, output, failed_items

    finally:
        for p in (sc_path, out_path, art_path, json_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def build_context_block(source_context: Optional[str] = None,
                        foreshadow_todo: Optional[List[Dict[str, Any]]] = None,
                        foreshadow_overview: Optional[List[str]] = None) -> str:
    """拼装写手上下文包：原著章节参考 + 本集应回收 + 全剧未回收概览。

    **初稿与每一轮回炉共用同一个返回值**——回炉轮不再丢失上下文（同时根治 v1.3.0
    「原文只在初稿、回炉即蒸发」的挂接缺口）。三者全空返回空串。
    """
    parts: List[str] = []

    src = str(source_context or "").strip()
    if src:
        parts.append("【原著对应章节参考】\n" + src
                     + "\n请充分吸收原著的人物行动、冲突情境与对白细节进行剧本改编。")

    todo = [t for t in (foreshadow_todo or []) if isinstance(t, dict)]
    if todo:
        lines = [f"- {t.get('id')}「{t.get('text', '')}」（埋于第 {t.get('plantedInEp')} 集）"
                 for t in todo]
        parts.append("【本集应回收】\n" + "\n".join(lines)
                     + "\n以上伏笔按大纲计划在本集兑现：请在对白或动作里给出可定位的核销点"
                       "（第几场、谁认领），并登记到输出 JSON 的 foreshadowsPaid 字段。")

    overview = [str(x) for x in (foreshadow_overview or []) if str(x).strip()]
    if overview:
        parts.append("【全剧未回收概览】\n" + "\n".join(f"- {x}" for x in overview))

    return "\n\n".join(parts)


def build_healing_prompt(ep_data: Dict[str, Any], failed_items: List[str],
                         context_block: str = "", examples_block: str = "") -> str:
    """拼装精准的外科手术式自愈修复 Prompt（缺省空参 = 向后兼容旧调用方）"""
    err_text = "\n".join([f"- {item}" for item in failed_items])
    current_json = json.dumps(ep_data, ensure_ascii=False, indent=2)
    ctx = f"\n{context_block}\n" if str(context_block or "").strip() else ""
    ex = f"\n{examples_block}\n" if str(examples_block or "").strip() else ""

    prompt = f"""【质量门拦截打回 · 自愈修改指令】
你上一轮输出的剧本未能通过工业级 12 道质量门硬检验，具体定位到的违规项如下：
{err_text}

【修复硬性原则】：
1. 若提示“包含文学病禁词（如若有所思、神色复杂、睫毛微颤等）”：坚决替换为具体的常见物理动作（如：抓紧茶杯、猛地站起身、后退半步、反锁房门）；
2. 若提示“命中翻案腔/辩论腔（不是...而是...等）”：改为单向陈述句，禁止在对白中争辩大道理，话只说三分；
3. 若提示“时长欠缺/超时”：按照【台词4.5字/秒、动作2.5秒/拍】的物理公式增补冲突动作或修剪冗余；
4. 保持已有合格的场景和人物设定不变，直接输出修复后的合法完整 JSON（仅输出合法的 JSON 对象，不包含任何外部 Markdown 或问候语）。
{ex}{ctx}
【待修复的当前剧本数据】：
{current_json}
"""
    return prompt


class HealingBlocked(RuntimeError):
    """回炉未收敛（v1.5.0 T1 统一熔断）：该集**未交付**，调用方不得落盘当成品。

    - `verdict == "escalate"`：连续两轮阻塞项不缩小 → 需人拍板（CLI 侧人不在场即标未交付）
    - `verdict == "abort"`：达到回炉硬顶 → 该集放弃本次生成

    `delivered` 恒为 False 是刻意的：任何把异常吞掉当成功的调用方都是假成功。
    """

    def __init__(self, ep: Any, verdict: str, detail: str,
                 problems: Optional[List[Any]] = None, rounds: int = 0):
        self.ep = ep
        self.verdict = verdict
        self.detail = detail
        self.problems = list(problems or [])
        self.rounds = rounds
        super().__init__(f"第 {ep} 集回炉未收敛（{verdict}，共 {rounds} 轮）: {detail[:200]}")

    @property
    def delivered(self) -> bool:
        return False


def _clean_json_output(raw: str) -> Dict[str, Any]:
    """清洗模型输出，兼容被 markdown 代码块包裹的 json"""
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        text = match.group(1)
    else:
        # 尝试寻找最外层的 {}
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]

    return json.loads(text)


def generate_episode_with_healing(
    ep_meta: Dict[str, Any],
    gateway: Any,
    outline_data: Optional[Dict[str, Any]] = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    max_retries: int = DEFAULT_HEAL_ROUND_CAP,
    art_data: Optional[Dict[str, Any]] = None,
    ledger: Optional[Any] = None,
    source_context: Optional[str] = None,
    context_block: str = "",
    t_gate_fn: Optional[Any] = None,
    workdir: Optional[str] = None
) -> Dict[str, Any]:
    """
    单集自愈生成控制器：初稿 -> 质量门 -> 失败则携带错误自愈 -> 成功落盘

    :param context_block: 写手上下文包（原著章节 + 待回收伏笔）。**初稿与每一轮回炉共用**
        ——回炉轮不再丢上下文。缺省时按 source_context 现拼（向后兼容旧调用方）。
    :param t_gate_fn: 创作纵深 T 门钩子 `fn(ep_data) -> list[str]`。每轮 12 门全绿后进程内直调；
        它报出的违规与 12 门违规**同一落点**进统一熔断判定（v1.5.0 T1：全线一个口径）。
        注意时序是短路串行（12 门没过就不跑 T 门），不是把两批条目并行取并集。
    :param max_retries: 回炉硬顶（= healRoundCap，默 3）。轮次语义由统一熔断执行器裁决。
    :param workdir: 给定则每轮把阻塞集落 `state/healing/epNNN.json` 台账（跨进程真值，工作流侧同读）。
    :raises HealingBlocked: 连续两轮不缩小（escalate）或触硬顶（abort）——该集未交付。
    """
    base_prompt = f"请编写第 {ep_meta.get('ep', 1)} 集剧本，目标时长 {ep_meta.get('targetSeconds', 90)} 秒。"
    ctx = str(context_block or "").strip() or build_context_block(source_context)
    prompt = base_prompt + (("\n\n" + ctx) if ctx else "")
    start_time = time.time()

    ep_n = ep_meta.get("ep")
    cap = max(1, int(max_retries))
    history: List[FrozenSet[Problem]] = []
    blockers: FrozenSet[Problem] = frozenset()
    for attempt in range(1, cap + 1):
        try:
            raw_reply = gateway.call_llm(prompt=prompt, system_prompt=system_prompt, ep=ep_n)
        except TypeError:
            raw_reply = gateway.call_llm(prompt=prompt, system_prompt=system_prompt)
        ep_data = _clean_json_output(raw_reply)

        # 组装完整的待测结构
        test_script = {
            "source": outline_data.get("title", "短剧") if outline_data else "短剧",
            "episodes": [ep_data]
        }

        round_hits = []
        # DR-02：回炉前先绑权威集号/时长（直接 lint，不经 run_quality_gate kwargs，
        # 保持既有 monkeypatch 假门签名兼容；硬伤与 12 门结果同落点进熔断）。
        auth_hard = lint_authoritative_fields(
            test_script, outline_data,
            ep_meta.get("targetSeconds"), ep_meta.get("ep"),
        )
        passed, report, failed_items = run_quality_gate(test_script, outline_data, art_data, hits_collector=round_hits)
        if auth_hard:
            failed_items = ["✗ [违规] 权威集号与时长绑定（ep/targetSeconds）"] + auth_hard + list(failed_items)
            passed = False
        failed_items = list(failed_items)
        if t_gate_fn is not None and passed:
            for v in (t_gate_fn(ep_data) or []):
                v = str(v).strip()
                if v and v not in failed_items:
                    failed_items.append(v)
            if failed_items:
                passed = False

        blockers = from_failed_items(failed_items)
        if not passed and not blockers:
            # 非零退出但违规条目不可解析：绝不静默当通过（假成功防线，工单审查 #2 同类）
            blockers = from_failed_items(["gate|validate|质量门非零退出且违规条目不可解析（见报告文本）"])
        history.append(blockers)
        verdict = breaker_decision(history, round_no=attempt, cap=cap)

        if verdict == VERDICT_PASS:
            elapsed = round(time.time() - start_time, 2)
            node_name = getattr(raw_reply, "node", None) or getattr(gateway, "last_node", None)
            if not node_name and hasattr(gateway, "nodes") and gateway.nodes:
                node_name = gateway.nodes[0].name
            if workdir:
                append_round(workdir, ep_n, attempt, blockers, verdict)
            if ledger:
                ledger.emit("episode_generated", ep=ep_meta.get("ep"), node=node_name or "-",
                            attempt=attempt, seconds_elapsed=elapsed)
            return ep_data

        if ledger:
            failed_gates = [item.split(":")[0].strip() if ":" in item else item for item in failed_items]
            detail_digest = " | ".join(failed_items)[:500]
            ledger.emit("gate_fail", ep=ep_meta.get("ep"), attempt=attempt,
                        failed_gates=failed_gates, detail_digest=detail_digest)
            if round_hits and hasattr(ledger, "path"):
                vocab_hits_path = os.path.join(os.path.dirname(ledger.path), "vocab_hits.jsonl")
                try:
                    os.makedirs(os.path.dirname(vocab_hits_path), exist_ok=True)
                    with _vocab_hits_lock:
                        with open(vocab_hits_path, "a", encoding="utf-8") as fp:
                            now_ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
                            for h in round_hits:
                                h_rec = dict(h)
                                h_rec["ts"] = now_ts
                                fp.write(json.dumps(h_rec, ensure_ascii=False) + "\n")
                except OSError:
                    pass

        if workdir:
            append_round(workdir, ep_n, attempt, blockers, verdict)

        if verdict in (VERDICT_ESCALATE, VERDICT_ABORT):
            detail = " | ".join(sorted(p.render() for p in blockers))[:500]
            if ledger:
                ledger.emit("healing_blocked", ep=ep_n, verdict=verdict, round=attempt,
                            detail_digest=detail)
            raise HealingBlocked(ep_n, verdict, detail,
                                 sorted(blockers, key=lambda p: p.pid), rounds=attempt)

        prompt = build_healing_prompt(ep_data, failed_items, ctx,
                                      render_ai_tone_examples(round_hits))

    # 防御性兜底：cap 轮必在末轮 abort，正常不可达
    raise HealingBlocked(ep_n, VERDICT_ABORT, "回炉轮次已尽",
                         sorted(blockers, key=lambda p: p.pid), rounds=cap)


# ------------------------------------------------------------------ CLI：单集验证的唯一入口

def _cli_main(argv=None) -> int:
    """单集件怎么验证，只在这里定义一次（工作流/脚本一律调它，不在别处复刻包壳）。

    `python -m dramacraft.self_healing --ep <裸单集件> --outline <大纲> [--art <美术>] --json <报告>`
    退出码：0 = 12 门全绿；2 = 有违规；1 = 用法错或输入不可用（**不落报告**）。
    报告形状与 validate.mjs 的机读报告同构（gates/hard），供 breaker 的 --gate-report 直接吃。
    """
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(prog="python -m dramacraft.self_healing",
                                 description="按引擎口径验证单集剧本（裸单集件自动包壳后过 12 门）")
    ap.add_argument("--ep", required=True, help="裸单集件路径（run_batch 落盘的那种）")
    ap.add_argument("--outline", help="大纲 JSON 路径")
    ap.add_argument("--art", help="美术设定 JSON 路径（给了就实扫门 10）")
    ap.add_argument("--json", required=True, dest="json_path", help="报告保存路径（JSON）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    def _load(path):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except (OSError, ValueError) as err:
            print(f"self_healing: 读不了 {path}（{err}）", file=sys.stderr)
            raise SystemExit(1)

    ep_doc = _load(args.ep)
    outline = _load(args.outline) if args.outline else None
    art = _load(args.art) if args.art else None
    # DR-02：文件名 epNNN 为权威集号（run_batch 落盘命名），与模型自报 ep 比对。
    expected_ep = None
    m = re.search(r"ep(\d{3})\.json$", os.path.basename(args.ep))
    if m:
        try:
            expected_ep = int(m.group(1))
        except ValueError:
            expected_ep = None
    # 裸单集件按引擎口径包壳（self_healing 的 test_script 组装就是这一句；validate.mjs 只认 episodes）
    wrapped = {"source": (outline or {}).get("title", "短剧"), "episodes": [ep_doc]}
    gates: List[Dict[str, Any]] = []
    passed, _report, failed_items = run_quality_gate(wrapped, outline, art,
                                                    gates_collector=gates,
                                                    expected_ep=expected_ep)
    if not gates:
        # 校验器一道门都没给出（路径不存在、node 起不来、报告被吞）——没有门结果 ≠ 全绿，
        # 而且下游熔断器拒吃空 gates。按「报告不可用」硬失败，不落报告（与 exit 1 的既有纪律一致）。
        print("self_healing: 校验器没给出任何门结果（报告缺失或不可解析）——无法判定，按硬失败处理",
              file=sys.stderr)
        return 1
    payload = {"ep": ep_doc.get("ep"), "gates": gates, "hard": list(failed_items), "warn": [],
               "passed": bool(passed)}
    try:
        Path(args.json_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                                   allow_nan=False), encoding="utf-8")
    except OSError as err:
        print(f"self_healing: 报告写不进去：{args.json_path}（{err}）", file=sys.stderr)
        return 1
    if passed:
        print("✓ 12 道质量门全过")
        return 0
    print(f"✗ 有 {len(failed_items)} 条违规（见报告）")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli_main())
