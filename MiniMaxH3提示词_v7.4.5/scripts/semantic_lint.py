# -*- coding: utf-8 -*-
"""Production Semantic Linter for MiniMax H3 Video Prompts.

Evaluates:
  1. Causal Action Chain (Closed-loop physical action without keyword stacking)
  2. Spatial Dynamic Locking (Screen-relative anchors, vectors & visual invariant locks)
"""

from __future__ import annotations

import re


def audit_causal_chain(text: str) -> tuple[str, str]:
    """Audit physical action causal chain with multi-tier grading (PASS/WARN/FAIL)."""
    clean_text = text.strip()
    if not clean_text:
        return ("FAIL", "动作描写内容为空")

    # 1. 拦截机械标签堆砌假阳性 (例如 部位: 右手; 方向: 向前; 速度: 极快...)
    mechanical_tags = re.findall(
        r"(?:部位|方向|速度|力度|惯性|接触|后果)\s*[:：]",
        clean_text
    )
    if len(mechanical_tags) >= 3:
        return ("FAIL", "检测到伪代码化机械动作标签堆砌，必须采用自然连贯的叙事语言表达物理过程")

    # 拦截伪描述式假阳性 (例如 '动作部位是右手，速度飞快，力度很大，但是茶杯静止不动')
    if ("部位是" in clean_text or "速度飞快" in clean_text) and ("静止不动" in clean_text or "但是" in clean_text):
        return ("FAIL", "动作描述存在伪因果堆砌或因果脱节矛盾")

    # 2. 拦截严重因果断裂与虚假跳跃
    # 例如：看了一眼 -> 杯子碎了一地；拔剑 -> 倒地流血（缺少致因接触动作）
    if re.search(r"看了一[眼眼眼]|瞪了一[眼眼眼]|瞄了一[眼眼眼]", clean_text) and re.search(r"碎了一地|爆炸|飞了出去", clean_text):
        return ("FAIL", "存在超自然因果断裂：无物理动量传递而直接跳跃至破坏性后果")
    if ("拔剑" in clean_text or "出鞘" in clean_text) and ("倒地" in clean_text or "流血" in clean_text) and not any(
        k in clean_text for k in ["刺", "斩", "挥", "砍", "击", "挑", "劈", "刃", "交击", "中剑"]
    ):
        return ("FAIL", "动作因果链断裂：从动作发起直接跳跃至受击结果，缺少中间攻击与物理接触过程")

    # 3. 检查物理闭环三要素：
    # A. 动作发起与身体驱动 (肩、臂、腕、手、腿、步、挥、推、挑、迈、转、冲、跃等)
    has_action = bool(re.search(
        r"(?:肩|臂|腕|手|腿|脚|腰|掌|拳|步|指|身|转|挥|推|挑|按|冲|拔|迈|移|跨|疾|跃|掠|踏)",
        clean_text
    ))

    # B. 接触与受力传递 (撞上、碰、交击、挑开、触、按住、击中、抓、切入、砸、拂)
    has_contact = bool(re.search(
        r"(?:撞|击|碰|按|挑开|抓|握|拂|触|踏实|扣|削|挡|落于|交击|切入|擦过|嵌入)",
        clean_text
    ))

    # C. 可见后果反馈与动量惯性 (碎裂、震颤、火花、断成、晃动、掉落、飞溅、倒落、变形、声响)
    has_result = bool(re.search(
        r"(?:震颤|掉落|断成|迸出|火花|晃动|弹开|落地|碎|倾|变形|激起|飞溅|散落|声波|灰尘|下沉)",
        clean_text
    ))

    if not has_action:
        return ("FAIL", "演进中缺少具体的身体部位与物理动作发起")

    # 简短动作只有动作而缺失受力后果（例如 '沈昭向前猛推木门。'）
    if has_action and not has_result:
        return ("WARN", "动作具有物理动作但缺失明确的可观察力学后果反馈（如形变、震颤或位移惯性）")

    if has_action and (has_contact or has_result):
        return ("PASS", "动作描写形成完整物理因果闭环（驱动-接触-动量可见后果）")

    return ("WARN", "物理因果闭环信息偏弱")


def audit_spatial_lock(text: str) -> tuple[str, str]:
    """Audit spatial dynamic locking with multi-tier grading (PASS/WARN/FAIL)."""
    clean_text = text.strip()
    if not clean_text:
        return ("FAIL", "空间描述内容为空")

    # 1. 拦截机械坐标标签堆砌 (例如 屏幕象限: 左上; 相对方位: 对角; 景深层级: 前景...)
    mechanical_coords = re.findall(
        r"(?:屏幕象限|相对方位|景深层级|轴线朝向|机位坐标)\s*[:：]",
        clean_text
    )
    if len(mechanical_coords) >= 3:
        return ("FAIL", "检测到坐标轴标签机械堆砌，必须采用画面内可观察空间关系自然描述")

    # 2. 拦截完全悬空、缺乏锚点的虚浮描写
    if "走来走去" in clean_text and "地方" in clean_text and not any(
        k in clean_text for k in ["景", "机位", "侧", "前", "后", "左", "右", "面向", "站立", "对角"]
    ):
        return ("FAIL", "空间描写缺乏屏幕象限与不可移动参照物，主体处于虚浮悬空状态")

    # 3. 检查空间动态锁定四要素：
    # A. 画面屏幕相对方位/象限 (左侧、右侧、左前、右后、居中、画面边缘等)
    has_quadrant = bool(re.search(
        r"(?:左侧|右侧|左前|右前|左后|右后|中央|边缘|正前方|前景|中景|远景|特写)",
        clean_text
    ))

    # B. 环境参照基准与机位锚点 (神台、破庙、桌、门、机位、视线、身位、两米)
    has_anchor = bool(re.search(
        r"(?:机位|神台|破庙|殿|门|台|墙|柱|屏风|路|两米|身位|侧影|边缘|地面)",
        clean_text
    ))

    # C. 朝向向量与轴线关系 (面向、背对、相对、对话轴、180度、切入、对准)
    has_vector = bool(re.search(
        r"(?:面向|背对|对准|切入|相对|走位|对话轴|180度|朝向|仰拍|俯拍|平视|转动)",
        clean_text
    ))

    # D. 主体视觉不变量锁定 (严禁串色、保持服饰、保持原有站位、不颠倒等)
    has_invariant = bool(re.search(
        r"(?:保持.*独立|严禁串色|不颠倒|锁定|保持原有|视觉一致|独立服饰)",
        clean_text
    ))

    # 纯简短方位只有沈昭在左、陆青在右，无动态向量和无基准（例如 '沈昭在左侧中景，陆青在右侧前景。'）
    if has_quadrant and not has_anchor and not has_vector and not has_invariant:
        return ("WARN", "空间定位仅有粗略景深象限，缺失不可移动基准参照物与相对朝向向量")

    # 包含机位与两主体朝向交互形成轴线（如用例 10）
    if has_quadrant and has_anchor and has_vector:
        return ("PASS", "空间关系明确，具备机位锚点、象限位置与明确的相对朝向轴线")

    # 复杂走位保持不颠倒（如用例 12）或带有不变量锁定（如用例 7）
    if has_quadrant and (has_invariant or (has_anchor and has_vector)):
        return ("PASS", "空间锁定完备，包含象限锚点与主体连续性不变量声明")

    return ("WARN", "空间动态锁定要素不充分")
