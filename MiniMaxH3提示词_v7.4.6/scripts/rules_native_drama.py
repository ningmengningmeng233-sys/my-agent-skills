#!/usr/bin/env python3
"""Native Drama Series Verification Rules implementing P0-08, P1-01 ~ P1-03."""

from __future__ import annotations

import re
from parser_native import ParsedNative, parse_native_prompt, DRAMA_REQUIRED_SECTIONS
from result import RuleResult
from semantic_lint import audit_causal_chain, audit_spatial_lock

MODE_A_REQUIRED_ENDING = "写实真人摄影质感；真实皮肤质感；自然真实的人类面部特征，带有轻微的生理不完美。"


def rule_r_z01_sections(parsed: ParsedNative) -> RuleResult:
    """Check Drama sections presence, non-emptiness, and exact order."""
    text = parsed.raw_text
    found_order = []
    pattern = re.compile(
        r"(?m)^(【(?:环境光影|空间基准|角色声音签名|画面演进|声音设计|画面风格)】)"
    )
    for m in pattern.finditer(text):
        found_order.append(m.group(1))

    # All required sections must be present
    for req in DRAMA_REQUIRED_SECTIONS:
        if req not in parsed.sections:
            return RuleResult(rule_id="R-Z01", status="FAIL", message=f"缺少必须章节: {req}")
        if not parsed.sections[req].strip():
            return RuleResult(rule_id="R-Z01", status="FAIL", message=f"章节内容不能为空: {req}")

    # P1-08: Exact full section order validation
    valid_order_with_voice = [
        "【环境光影】",
        "【空间基准】",
        "【角色声音签名】",
        "【画面演进】",
        "【声音设计】",
        "【画面风格】",
    ]
    valid_order_no_voice = [
        "【环境光影】",
        "【空间基准】",
        "【画面演进】",
        "【声音设计】",
        "【画面风格】",
    ]
    if found_order != valid_order_with_voice and found_order != valid_order_no_voice:
        return RuleResult(
            rule_id="R-Z01",
            status="FAIL",
            message=f"章节顺序错乱，当前顺序为: {found_order}。合法顺序必须严格为: {valid_order_with_voice}",
            evidence=str(found_order),
        )

    return RuleResult(rule_id="R-Z01", status="PASS", message="中文固定章节完整、内容非空且顺序严格规范")


def rule_r_z02_placeholders(parsed: ParsedNative) -> RuleResult:
    """Check for forbidden templates, variables, and bracketed internal notes."""
    text = parsed.raw_text
    var_m = re.findall(r"\{\{[^}]+\}\}", text)
    if var_m:
        return RuleResult(rule_id="R-Z02", status="FAIL", message=f"存在未替换的模板变量: {var_m[:3]}")

    risk_m = re.findall(r"\[(?:RISK|ALERT|TODO|FIXME|NOTE|WARN)[^\]]*\]", text, flags=re.IGNORECASE)
    if risk_m:
        return RuleResult(rule_id="R-Z02", status="FAIL", message=f"存在未清理的内部标签: {risk_m[:3]}")

    cn_bracket_m = re.findall(r"【(?:此处|待补充|变量|替换)[^】]*】", text)
    if cn_bracket_m:
        return RuleResult(rule_id="R-Z02", status="FAIL", message=f"存在未填写的占位标记: {cn_bracket_m[:3]}")

    return RuleResult(rule_id="R-Z02", status="PASS", message="无未替换占位符或内部标签")


def rule_r_z03_picture_references(parsed: ParsedNative) -> RuleResult:
    """P1-01: Picture line formatting and observable attribute check without length bypass."""
    pics = parsed.picture_refs
    if not pics:
        return RuleResult(rule_id="R-Z03", status="PASS", message="未声明参考图，按纯文本提示词处理")

    indices = [p.index for p in pics]

    # Check 1: Sequence starts at 1 and increases monotonically without gaps or duplicates
    expected = list(range(1, len(pics) + 1))
    if indices != expected:
        return RuleResult(
            rule_id="R-Z03",
            status="FAIL",
            message=f"参考图编号必须从 1 连续递增且不重复，当前为: {indices} (期望: {expected})",
            evidence=str(indices),
        )

    # Check 2: Observable attribute validation (P1-01 - delete len(body) >= 10 exemption)
    # Attribute must describe observable features, NOT arbitrary random strings like abcdefghij
    for p in pics:
        if p.is_grid:
            # Grid needs layout/storyboard indication
            continue

        # Check if asset has real observable attributes
        clean_name = p.asset_name.strip()
        if not p.has_attribute:
            return RuleResult(
                rule_id="R-Z03",
                status="FAIL",
                message=f"参考图 {p.raw_line} 未绑定可观察视觉属性（需满足：图N = 名称，锁定身份/服装/面容/材质/道具等具体属性）",
                evidence=p.raw_line,
                line=p.line_number,
            )

        # Disallow nonsensical string without observable keywords
        has_semantic_prop = bool(re.search(
            r"(?:锁定|保持|特征|身穿|手持|面戴|发型|长袍|劲装|青铜|鳞甲|战甲|短剑|刀柄|头冠|容貌|眉眼|色调|材质|纹理|质感|骨质|服饰)",
            p.raw_line,
        ))
        if not has_semantic_prop:
            return RuleResult(
                rule_id="R-Z03",
                status="FAIL",
                message=f"参考图 {p.raw_line} 属性缺少可观察视觉特征词（身份/服装/面容/材质/道具），禁止无意义字符绕过",
                evidence=p.raw_line,
                line=p.line_number,
            )

    # Check 3: Grid reference positioning: if grid exists, must be the last picture
    grid_indices = [i for i, p in enumerate(pics) if p.is_grid]
    if grid_indices and grid_indices[-1] != len(pics) - 1:
        return RuleResult(
            rule_id="R-Z03",
            status="FAIL",
            message="宫格参考图（本镜头组宫格/STORYBOARD PLAN ONLY）必须位于所有参考图声明的最末一行",
        )

    return RuleResult(rule_id="R-Z03", status="PASS", message="参考图声明格式规范连续且绑定具体可观察属性")


def rule_r_z04_dialogue_soundscape(parsed: ParsedNative) -> RuleResult:
    """Check dialogue voice signatures and strict BGM isolation."""
    text = parsed.raw_text
    sound = parsed.sections.get("【声音设计】", "")

    # BGM isolation check: diegetic sound only, disallow BGM keywords unless negative statement
    sound_clean = re.sub(
        r"(?:无|没有|禁止|全程无|不含|不得包含|无任何|全场全程无)\s*(?:任何)?\s*(?:背景音乐|BGM|配乐|伴奏|主题曲|插曲)",
        "",
        sound,
        flags=re.IGNORECASE,
    )
    if re.search(r"(?:背景音乐|BGM|配乐|伴奏|主题曲|插曲)", sound_clean, flags=re.IGNORECASE):
        return RuleResult(
            rule_id="R-Z04",
            status="FAIL",
            message="【声音设计】严禁混入背景音乐(BGM/配乐/伴奏)，中文通道声音设计仅限同期环境声与物理音效",
        )

    # Dialogue signature binding
    dialogues = parsed.dialogues
    roles = parsed.voice_roles

    if dialogues and not roles:
        return RuleResult(
            rule_id="R-Z04",
            status="FAIL",
            message="画面演进中存在角色对白台词，但【角色声音签名】未定义任何角色声音",
        )

    # Verify each dialogue belongs to a defined voice role
    evolution = parsed.sections.get("【画面演进】", "")
    for dm in re.finditer(r"[“\"]([^”\"]+)[”\"]", evolution):
        quote_text = dm.group(1)
        pre_text = evolution[: dm.start()]
        last_punct = max(
            pre_text.rfind("。"),
            pre_text.rfind("\n"),
            pre_text.rfind("！"),
            pre_text.rfind("？"),
            0,
        )
        sentence = pre_text[last_punct:].strip()
        search_scope = sentence[-35:] if len(sentence) > 35 else sentence

        has_valid_speaker = any(role in search_scope for role in roles)
        if not has_valid_speaker:
            return RuleResult(
                rule_id="R-Z04",
                status="FAIL",
                message=f"台词“{quote_text[:8]}...”所在句子未包含任何已在【角色声音签名】中定义的角色（当前已定义: {roles}）",
            )

    return RuleResult(rule_id="R-Z04", status="PASS", message="台词角色声音签名完备，声音设计合规无混入BGM")


def rule_r_z05_mode_styles_and_locks(parsed: ParsedNative) -> RuleResult:
    """P1-02, P1-03: Strict Mode A and Mode B feature locks and mutual exclusion."""
    text = parsed.raw_text
    style = parsed.sections.get("【画面风格】", "")
    lines = parsed.non_empty_lines

    # Check 1: Opening spatial lock (must be in the first non-picture line)
    opening = parsed.opening_line
    has_opening_lock = "全画幅" in opening and ("180" in opening or "对话轴" in opening)

    # P1-03 Mutual exclusion: Compute live-action and animation indicators independently
    clean_style = re.sub(r"(?:杜绝|禁止|避免|不含|非)\s*(?:真人|写实|毛孔|皮肤)", "", style)
    is_animation = any(k in clean_style for k in ["动画", "3D", "国漫", "二次元", "赛璐璐", "水墨", "CG"])
    is_live_action = any(k in clean_style for k in ["全写实", "真人", "写实真人", "真实摄影"])

    # If both are hit, mutual exclusion fails (P1-03)
    if is_animation and is_live_action:
        return RuleResult(
            rule_id="R-Z05",
            status="FAIL",
            message="风格冲突: 【画面风格】同时包含真人写实与动画/CG特征，两者严格互斥，不得混用",
        )

    if not is_animation and not is_live_action:
        return RuleResult(
            rule_id="R-Z05",
            status="FAIL",
            message="【画面风格】未识别出合法的风格模式（必须明确声明为写实真人或动画/3D/国漫 Medium Lock）",
        )

    # Mode A (Live action)
    if is_live_action:
        if not has_opening_lock:
            return RuleResult(
                rule_id="R-Z05",
                status="FAIL",
                message="真人模式 Mode A 必须在第一条正文行声明开篇空间锁: 全画幅画面及遵守对话轴/180度原则",
            )

        # P1-01 / P1-02: Mode A fixed ending exact match
        if not parsed.section_order or parsed.section_order[-1] not in ("【画面风格】", "画面风格"):
            return RuleResult(
                rule_id="R-Z05",
                status="FAIL",
                message="【画面风格】必须是提示词的最后一个章节",
            )

        style_body = (parsed.sections.get("【画面风格】") or parsed.sections.get("画面风格") or "").strip()
        if style_body != MODE_A_REQUIRED_ENDING:
            return RuleResult(
                rule_id="R-Z05",
                status="FAIL",
                message=f"真人模式 Mode A 【画面风格】正文必须精确等于官方微观特征锁: '{MODE_A_REQUIRED_ENDING}'，禁止在固定结尾后追加其他正文",
                evidence=style_body,
            )

        last_line_no, last_line_str = lines[-1]
        clean_last = re.sub(r"^【画面风格】\s*", "", last_line_str).strip()
        if clean_last != MODE_A_REQUIRED_ENDING:
            return RuleResult(
                rule_id="R-Z05",
                status="FAIL",
                message=f"真人模式 Mode A 最后一条非空行必须精确等于官方微观特征锁: '{MODE_A_REQUIRED_ENDING}'，禁止在末尾追加其他正文",
                evidence=last_line_str,
                line=last_line_no,
            )

    # Mode B (Animation)
    if is_animation:
        # Disallow real human skin/pore stacking in animation
        if re.search(r"(?:真实皮肤质感|真实哑光皮肤|人类面部特征.*生理不完美|毛孔毛发特写)", clean_style):
            return RuleResult(
                rule_id="R-Z05",
                status="FAIL",
                message="动画模式 Mode B 严禁混入真人模式专用的真实皮肤质感/毛孔特征锁",
            )

    return RuleResult(rule_id="R-Z05", status="PASS", message="模式风格与开篇/末尾特征锁规范合规")


def rule_r_z06_evolution_structure(parsed: ParsedNative) -> RuleResult:
    """Check evolution section format."""
    evolution = parsed.sections.get("【画面演进】", "")
    lines = [l.strip() for l in evolution.splitlines() if l.strip()]

    numbered_shots = [l for l in lines if re.match(r"^镜头\s*[一二三四五六七八九十\d]+[：:]", l)]
    if len(numbered_shots) >= 3:
        return RuleResult(
            rule_id="R-Z06",
            status="FAIL",
            message="【画面演进】应当为连续的自然段落描写，避免机械拆解为'镜头一：... 镜头二：...'等任务清单",
        )

    return RuleResult(rule_id="R-Z06", status="PASS", message="【画面演进】采用自然叙事段落结构")


def rule_r_z07_action_causality(parsed: ParsedNative) -> RuleResult:
    """P0-08: Integrated Action Causality Check into Main Native Audit."""
    evolution = parsed.sections.get("【画面演进】", "")
    if not evolution:
        return RuleResult(rule_id="R-Z07", status="PASS", message="无画面演进文本，跳过因果动作检查")

    # Detect physical actions: sword, punch, hit, impact, crash, fall, break
    has_physical_action = any(k in evolution for k in [
        "拔剑", "挥剑", "出拳", "撞上", "击中", "推开", "碎裂", "倒地", "跃起", "飞驰", "斩断", "砍", "打", "冲撞"
    ])
    if not has_physical_action:
        return RuleResult(rule_id="R-Z07", status="PASS", message="非剧烈物理动作镜头，动作因果检查豁免通过")

    status, msg = audit_causal_chain(evolution)
    return RuleResult(
        rule_id="R-Z07",
        status=status,
        message=f"动作因果链审核: {msg}",
        evidence=msg,
    )


def rule_r_z08_spatial_continuity(parsed: ParsedNative) -> RuleResult:
    """P0-08: Integrated Spatial Dynamic Locking into Main Native Audit."""
    space = parsed.sections.get("【空间基准】", "")
    if not space:
        return RuleResult(rule_id="R-Z08", status="PASS", message="无空间基准文本，跳过空间检查")

    status, msg = audit_spatial_lock(space)
    return RuleResult(
        rule_id="R-Z08",
        status=status,
        message=f"空间基准与动态连续性审核: {msg}",
        evidence=msg,
    )


def audit_native_drama_rules(text: str) -> list[RuleResult]:
    parsed = parse_native_prompt(text)
    return [
        rule_r_z01_sections(parsed),
        rule_r_z02_placeholders(parsed),
        rule_r_z03_picture_references(parsed),
        rule_r_z04_dialogue_soundscape(parsed),
        rule_r_z05_mode_styles_and_locks(parsed),
        rule_r_z06_evolution_structure(parsed),
        rule_r_z07_action_causality(parsed),
        rule_r_z08_spatial_continuity(parsed),
    ]

