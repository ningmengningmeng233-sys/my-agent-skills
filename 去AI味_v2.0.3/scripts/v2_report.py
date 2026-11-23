#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_report.py - 规则级决策与中文解释报告（alpha.1 P05）。

合同（设计 5/10 节；用户要求 13/36/37 节）：

  1. **decide 只出三档规则级建议**：keep（不建改）/ review（交裁判）/
     minimal_fix（低风险机械修复）——永不返回 rewrite/delete 之类的修改
     授权。KEEP 是一等结果：弱词单独命中、有叙事功能的重复、无永久意图的
     锁定动作（FP01 修正例）、构成情节回应的点头与收信（FP05 修正例）一律
     keep；语义与功能判断本体在 P08 的裁判合同里，本模块只封住"规则层
     越权"的口子。
  2. **十三类问题映射旧规则层，不独立重判**：THIRTEEN_KINDS 的完整中文
     判据（触发证据、保留豁免、与 E/ED/FP 的对应）由
     references/v2-candidate-rules-zh.md 承载；ED01 共用判据不让普通文稿
     强制加载小说层（设计 5 节）。
  3. **报告必须可解释**：判定保留、证据不足、实际修改三数分开；保留展示
     理由；coverage 的未覆盖类别与停用组件必须披露——"当前规则与裁判体系
     未发现剩余高置信度问题"这类话术不得掩盖能力缺口；禁止"AI 痕迹 0"
     与准确率百分比。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

# 十三类问题（v2.0.0 用户要求与设计方案第 5 节的完整枚举）。
THIRTEEN_KINDS: tuple[str, ...] = (
    "RESULT_ECHO",                # 结果复述
    "LOW_INFORMATION_COMPARISON",  # 低价值比较
    "STATE_ANNOUNCEMENT",         # 模糊状态宣告（X 了）
    "ABSTRACT_MOTION",            # 抽象感觉实体化运动
    "MICRO_ACTION_CHAIN",         # 无效微动作链
    "MECHANICAL_TEMPORAL_CHAIN",  # 机械时间链
    "SENSORY_TRANSMISSION_CHAIN",  # 感官传导链
    "SEMANTIC_ECHO",              # 语义回声
    "TEMPLATE_REUSE",             # 模板换皮
    "EXPLANATION_AFTER_ACTION",   # 动作后重复解释
    "ENDING_SUMMARY",             # 章末无效总结
    "SYNTACTIC_UNIFORMITY",       # 连续同构句
    "SYNONYM_ONLY_REWRITE",       # 同义词假优化
)

# 弱信号词表（用户要求第 13 节）：单独命中永不产生修改建议。
WEAK_WORDS: tuple[str, ...] = (
    "赫然", "猛然", "骤然", "刹那", "一瞬", "片刻", "一寸", "半寸",
    "如同", "宛如", "顺着", "随后", "紧接着", "下意识",
)

_OPERATION_NAMES = {
    "delete": "删除",
    "merge": "合并",
    "local_rewrite": "局部修复",
    "paragraph_rewrite": "段内重组",
}

_JUDGMENT_NAMES = {
    "keep": "判定保留",
    "insufficient_evidence": "证据不足",
    "rewrite": "判定可改",
}

# 结构化已核标志词表（补正方案 7 节）：decide 只消费明确结构化、已经证据层
# 核验的标志，不解析 evidence 字符串作语义定案。词表封死——未知标志一律拒绝，
# 与 v2_contracts 的"未知字段拒绝"同一纪律。
VERIFIED_FLAGS: tuple[str, ...] = (
    "no_permanent_intent",           # FP01 豁免：经核验无永久意图
    "plot_response",                 # FP05 豁免：点头/收信等情节回应
    "true_negation_or_information",  # ED01 ①③：真实纠错/事实对照/责任条件因果
    "independent_information",       # ①：两项独立信息、两项约束
    "character_voice_or_conflict",   # ②：人物声线·关系·冲突
)


def decide(cand: dict) -> str:
    """规则级建议：keep / review / minimal_fix（值域封死，永不授权改写）。

    补正 Q06（独立复核 R06）：豁免只消费 ``verified`` 里的**结构化已核标志**
    （词表 VERIFIED_FLAGS，未知标志拒绝）——evidence 字符串里写"无永久意图"
    "情节回应"等关键词**不构成**语义定案。规则层无法判断（豁免未经核验、
    未知功能）一律返回 review 交裁判，不静默 keep；只有弱词单独命中与低风险
    机械修复是规则层自己能定的。未知规则 fail-safe 仍为 keep——规则层不知道
    的东西不授权。
    """
    rule_id = str(cand.get("rule_id", ""))
    verified = cand.get("verified")
    if verified is None:
        verified = {}
    if not isinstance(verified, Mapping):
        raise ValueError(f"verified 必须是映射: {verified!r}")
    unknown_flags = sorted(set(verified) - set(VERIFIED_FLAGS))
    if unknown_flags:
        raise ValueError(f"verified 含未登记标志: {','.join(unknown_flags)}")
    if rule_id == "WEAK_WORD":
        return "keep"
    if rule_id == "LOW_RISK_MECHANICAL":
        return "minimal_fix"
    # 豁免只认结构化已核标志（evidence 文本内容不参与判级）。
    if verified.get("no_permanent_intent") is True:
        return "keep"
    if verified.get("plot_response") is True:
        return "keep"
    if verified.get("true_negation_or_information") is True:
        return "keep"
    if verified.get("independent_information") is True:
        return "keep"
    if verified.get("character_voice_or_conflict") is True:
        return "keep"
    # 无法判断：交裁判（KEEP 由裁判定，不由规则层猜）。
    if rule_id in THIRTEEN_KINDS or rule_id in ("ED01", "FP01", "FP05"):
        return "review"
    return "keep"


def rule_load_plan(scene: str = "normal", rule_ids=(),
                   cluster_or_scene_hit: bool = False, *,
                   v2_flow: bool = False) -> tuple:
    """规则加载清单（补正 Q06 / alpha.2 v2 档）：实际要读哪些参考。

    组合逻辑与集合权威都在 ``runtime_contract.rule_load_plan``——本模块只是 v2 报告侧
    的入口，不在此重抄一份允许清单。normal 永不出现小说层文件；命中 ED 类（含 ED01
    判序）时普通文稿追加共用编辑层；长文/技术文档等按既有合同可读聚集与场景档；
    ``v2_flow=True`` 再追加交互合同与候选判据两份 v2 必读。
    """
    from runtime_contract import rule_load_plan as _contract_rule_load_plan
    return _contract_rule_load_plan(scene, rule_ids, cluster_or_scene_hit,
                                    v2_flow=v2_flow)


def render_report(candidates: tuple, judgments: tuple, patches: tuple, *,
                  coverage: dict) -> str:
    """渲染中文解释报告：统计、理由与能力披露。"""
    judgment_counts = Counter(j.status for j in judgments)
    kept = judgment_counts.get("keep", 0)
    insufficient = judgment_counts.get("insufficient_evidence", 0)
    flagged = judgment_counts.get("rewrite", 0)
    op_counts = Counter(p.operation for p in patches)

    lines: list[str] = []
    lines.append("## 审校报告")
    lines.append("")
    lines.append(f"- 结构与语义候选：{len(candidates)} 个")
    lines.append(f"- 已审查：{len(judgments)} 个")
    lines.append(f"- 判定保留：{kept} 个（保留理由见下）")
    lines.append(f"- 证据不足：{insufficient} 个（当前无法定案，操作保持"
                 "原文；与判定保留分开统计，不并入保留）")
    lines.append(f"- 判定可改：{flagged} 个")
    op_line = ""
    if patches:
        parts = "、".join(
            f"{_OPERATION_NAMES.get(op, op)} × {n}"
            for op, n in sorted(op_counts.items()))
        op_line = f"（{parts}）"
    lines.append(f"- 实际修改：{len(patches)} 个{op_line}")
    lines.append("")

    if kept:
        lines.append("### 保留理由（抽样）")
        for j in judgments:
            if j.status == "keep":
                claims = "；".join(j.reason_claims) or "（见候选详情）"
                lines.append(f"- 候选 {j.candidate_id}：{claims}")
        lines.append("")

    if not patches and len(candidates) >= 0:
        lines.append("### 本次结果")
        lines.append("- NO-OP：正文未做任何修改，属正常成功结果；"
                     "未产生修改不代表检查未执行。")
        lines.append("")

    uncovered = list(coverage.get("uncovered") or [])
    disabled = list(coverage.get("disabled_components") or [])
    if uncovered or disabled:
        lines.append("### 能力披露")
        if uncovered:
            lines.append(f"- 本轮未覆盖的类别：{'、'.join(uncovered)}")
        if disabled:
            lines.append(f"- 本轮停用的组件：{'、'.join(disabled)}")
        lines.append("- 以上缺口照实披露：未发现问题不等于问题不存在。")
        lines.append("")

    lines.append("### 结论口径")
    lines.append("- 本报告只描述「当前规则与裁判体系」的判断结果，"
                 "不承诺清除全部 AI 痕迹，也不提供任何百分比例。")
    return "\n".join(lines) + "\n"
