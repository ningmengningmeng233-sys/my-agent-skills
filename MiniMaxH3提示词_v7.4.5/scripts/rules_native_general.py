#!/usr/bin/env python3
"""Native General Verification Rules for MiniMax General Video Prompts.

Rules:
- R-ZG01: Structure & Channel Separation (reject drama brackets, reject ref2va tags)
- R-ZG02: Template & Placeholder Detection ({{var}}, ${VAR}, [待补充], TODO, etc.)
- R-ZG03: Subject & Action Dynamics (must have explicit visual subject and movement verb)
- R-ZG04: Quality Slop & Forbidden Buzzwords (filter vague quality buzzwords, guide physical detail)
- R-ZG05: Camera Motion & Composition Sanity (reasonable camera moves, no spatial contradictions)
"""

from __future__ import annotations

import re
from parser_native import ParsedNative, parse_native_prompt
from result import RuleResult


DRAMA_HEADINGS = ["【环境光影】", "【空间基准】", "【角色声音签名】", "【画面构成】", "【运镜设计】", "【音画协同】"]
FORBIDDEN_PLACEHOLDER_PATTERNS = [
    r"\{\{[^}]+\}\}",
    r"\$\{[^}]+\}",
    r"\[待[^\]]+\]",
    r"<待[^>]+>",
    r"【待[^】]+】",
    r"\bTODO\b",
    r"\bFIXME\b",
    r"\bXXX\b",
]

QUALITY_SLOP_BUZZWORDS = [
    "虚幻引擎渲染",
    "逼真极了",
    "顶级特效",
    "大师级神作",
    "超高清8k电影质感",
    "无噪点",
    "完美画质",
]

MOTION_KEYWORDS = [
    "走", "跑", "跳", "转身", "凝视", "飞翔", "漂浮", "流淌", "滴落", "掠过",
    "穿过", "升起", "落下", "涌动", "推近", "拉远", "摇移", "俯瞰", "仰望",
    "旋转", "移动", "挥动", "回眸", "抬头", "低头", "吹拂", "弥漫", "闪烁",
    "绽放", "消散", "奔向", "迈步", "穿行", "环顾", "倒映", "滑落", "展开",
    "夹取", "装入", "走动", "打磨", "雕刻", "操作", "触摸", "拿起", "放下", "摆放",
    "夹起", "放入",
    "颠锅", "翻炒", "翻腾", "切入", "作响", "迸发", "跃出", "穿梭", "挥舞", "倾泻",
    "塞进", "夹出", "抡锤", "连击", "淬火", "锻打", "掀起", "翻起", "进站", "开门",
    "熄灭", "收网", "拖行", "提灯", "抵住", "点头", "奔跑", "冲刺", "跟随", "泼洒",
]


def rule_r_zg01_structure(parsed: ParsedNative) -> RuleResult:
    """Check general structure: allows standard cinematic paragraph or shot blocks."""
    text = parsed.raw_text.strip()
    if not text:
        return RuleResult(rule_id="R-ZG01", status="FAIL", message="提示词内容不能为空")

    # Reject drama series fixed brackets in general mode to ensure profile separation
    has_drama = any(h in text for h in DRAMA_HEADINGS)
    if has_drama:
        return RuleResult(
            rule_id="R-ZG01",
            status="FAIL",
            message="检测到短剧固定六段式章节，请使用 native-drama-series 规则集进行审核",
        )

    # Reject ref2va english tags
    if "subject_definitions:" in text or "<Subject " in text:
        return RuleResult(
            rule_id="R-ZG01",
            status="FAIL",
            message="通用中文提示词中混入了英文 Ref2VA 标签，通道混用冲突",
        )

    return RuleResult(rule_id="R-ZG01", status="PASS", message="通用中文提示词结构规范")


def rule_r_zg02_placeholders(parsed: ParsedNative) -> RuleResult:
    """Check for forbidden templates and notes."""
    text = parsed.raw_text
    found_placeholders = []
    for pat in FORBIDDEN_PLACEHOLDER_PATTERNS:
        matches = re.findall(pat, text, re.IGNORECASE)
        if matches:
            found_placeholders.extend(matches)

    if found_placeholders:
        return RuleResult(
            rule_id="R-ZG02",
            status="FAIL",
            message=f"存在未替换的模板变量或草稿占位符: {found_placeholders[:3]}",
        )
    return RuleResult(rule_id="R-ZG02", status="PASS", message="无未替换占位符")


def rule_r_zg03_subject_and_action(parsed: ParsedNative) -> RuleResult:
    """Ensure prompt contains explicit visual subject and movement or action dynamics."""
    text = parsed.raw_text.strip()
    if not text:
        return RuleResult(rule_id="R-ZG03", status="FAIL", message="无有效描述文本")

    # Check for presence of action/motion dynamics
    has_motion = any(k in text for k in MOTION_KEYWORDS)
    if not has_motion:
        # Check if text is purely static noun or pure quality buzzwords
        return RuleResult(
            rule_id="R-ZG03",
            status="FAIL",
            message="缺失明确的动作动态或物理运动描述，视频提示词必须具备时态演变",
        )

    return RuleResult(rule_id="R-ZG03", status="PASS", message="具备明确的主体动态与动作演变")


def rule_r_zg04_quality_buzzwords(parsed: ParsedNative) -> RuleResult:
    """Warn or fail on empty quality buzzword slop."""
    text = parsed.raw_text
    found_slop = [w for w in QUALITY_SLOP_BUZZWORDS if w in text]
    if found_slop:
        return RuleResult(
            rule_id="R-ZG04",
            status="WARN",
            message=f"包含抽象空洞画质词 ({found_slop})，建议替换为具体材质纹理与真实物理光照描述",
        )
    return RuleResult(rule_id="R-ZG04", status="PASS", message="物理质感描述规范，无空洞画质堆砌")


def rule_r_zg05_camera_sanity(parsed: ParsedNative) -> RuleResult:
    """Verify camera movement instructions don't contain blatant spatial contradictions."""
    text = parsed.raw_text
    # Check conflicting camera directions simultaneously (e.g. 镜头同时推近并急速后拉)
    has_push = "推近" in text or "推进" in text or "前进" in text
    has_pull = "拉远" in text or "后拉" in text or "倒退" in text
    if "镜头同时" in text and has_push and has_pull:
        return RuleResult(
            rule_id="R-ZG05",
            status="FAIL",
            message="存在物理矛盾的运镜指令（镜头无法在同一时刻同时推近并拉远）",
        )

    return RuleResult(rule_id="R-ZG05", status="PASS", message="运镜与空间机位描述合理")


def audit_native_general_rules(text: str) -> list[RuleResult]:
    parsed = parse_native_prompt(text)
    return [
        rule_r_zg01_structure(parsed),
        rule_r_zg02_placeholders(parsed),
        rule_r_zg03_subject_and_action(parsed),
        rule_r_zg04_quality_buzzwords(parsed),
        rule_r_zg05_camera_sanity(parsed),
    ]
