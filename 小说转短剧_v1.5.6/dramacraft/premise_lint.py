# -*- coding: utf-8 -*-
"""单集前提纪要预审门 P1~P9（v1.2.5 选题会硬核闸）。

在将用户的零散灵感/选题孵化为前提纪要时，必须通过 P1~P9 九道确定性规则，
确保欲望具备不可逆后果、首尾具备纯物理画面、人物受控，杜绝无病呻吟与文学病。
可独立运行：python -m dramacraft.premise_lint <ep###.json>
"""
import json
import os
import sys
from typing import Any, Dict, List, Optional

VALID_TARGET_SECONDS = {60, 90, 180}
VALID_DECIDED_BY = {"user_confirmed", "user_custom", "inferred"}

# 载入文学病禁词
BANNED_WORDS = [
    "若有所思", "神色复杂", "气氛尴尬", "气氛微妙", "睫毛微颤", "睫毛颤动",
    "微微一怔", "心中一震", "眼神闪烁", "嘴角勾起一抹", "眼底闪过一丝",
    "陷入了沉思", "仿佛在思考着什么"
]
_patch_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills_runtime", "dramacraft_patch", "banned_words.json"))
if os.path.exists(_patch_file):
    try:
        with open(_patch_file, "r", encoding="utf-8") as fp:
            BANNED_WORDS = json.load(fp)
    except Exception:
        pass


def lint_premise(data: Dict[str, Any], card_ledger: Optional[Any] = None) -> List[str]:
    """返回违规清单，空列表 = P1~P9 全绿通过"""
    bad: List[str] = []

    # 1. P1: 核心四件套确权 (desire/obstacle/twist/cost)
    four = data.get("four", {})
    if not isinstance(four, dict):
        bad.append("[P1] four 字段缺失或非对象：核心四件套必须存在")
        four = {}

    for key in ("desire", "obstacle", "twist", "cost"):
        item = four.get(key)
        if not item or not isinstance(item, dict) or not str(item.get("summary", "")).strip():
            bad.append(f"[P1] 核心四件套缺失: four.{key}.summary 必须非空")
        else:
            d_by = item.get("decided_by")
            if d_by not in VALID_DECIDED_BY:
                bad.append(f"[P1] four.{key}.decided_by 非法或缺失: {d_by} (必须为 {VALID_DECIDED_BY})")

    # 2. P2: 不可逆后果硬核闸
    desire = four.get("desire", {})
    if isinstance(desire, dict):
        irr = str(desire.get("irreversible_consequence", "")).strip()
        if not irr:
            bad.append("[P2] desire 缺少具体 irreversible_consequence（不可逆后果）：主角完不成将面临何种绝境？")

    # 3. P3: 人物池溯源与规模限制 (<=4人)
    cast = data.get("cast", [])
    if not isinstance(cast, list) or not cast:
        bad.append("[P3] cast 角色列表缺失或为空")
    elif len(cast) > 4:
        bad.append(f"[P3] 单集出场核心人物过多 ({len(cast)} > 4)：短剧单集必须聚焦 ≤4 人")
    else:
        for c in cast:
            if not isinstance(c, dict) or not str(c.get("name", "")).strip():
                bad.append("[P3] 存在缺少 name 的非法角色条目")

    # 4. P4: 首尾物理动作画面 (hook 与 cliff 禁文学病抽象词)
    hook = str(data.get("hook", "")).strip()
    cliff = str(data.get("cliff", "")).strip()

    if not hook:
        bad.append("[P4] hook 前5秒抓人钩子缺失")
    else:
        for bw in BANNED_WORDS:
            if bw in hook:
                bad.append(f"[P4] hook 包含文学病形容词「{bw}」，必须替换为纯物理动作")

    if not cliff:
        bad.append("[P4] cliff 结尾卡点悬念画面缺失")
    else:
        for bw in BANNED_WORDS:
            if bw in cliff:
                bad.append(f"[P4] cliff 包含文学病形容词「{bw}」，必须替换为纯物理动作")

    # 5. P5: 离散标准档位 (60s, 90s, 180s)
    ts = data.get("target_seconds")
    if ts not in VALID_TARGET_SECONDS:
        bad.append(f"[P5] target_seconds ({ts}) 非标准节奏档位 (必须严格等于 {VALID_TARGET_SECONDS})")

    # 6. P6: 视听基调与结局类型
    tone = str(data.get("tone", "")).strip()
    if not tone:
        bad.append("[P6] tone 情绪视听基调未指定")
    ending = str(data.get("ending_type", "")).strip()
    if not ending:
        bad.append("[P6] ending_type 结局收尾类型未指定")

    # 7. P7: 卡池状态合法溯源
    cards_used = data.get("cards_used", [])
    if card_ledger is not None and isinstance(cards_used, list):
        for cid in cards_used:
            card = card_ledger.get_card(cid)
            if not card:
                bad.append(f"[P7] cards_used 引用了不存在的卡片: {cid}")
            elif card.get("status") == "spent":
                bad.append(f"[P7] 卡片 {cid} 状态为 spent (已被之前集数消耗)，不能重复消费")

    # 8. P8: 暗门自由回答回锚验证
    has_custom = any(
        isinstance(item, dict) and item.get("decided_by") == "user_custom"
        for item in four.values()
    )
    custom_anchors = data.get("custom_anchors", [])
    if has_custom and (not isinstance(custom_anchors, list) or not custom_anchors):
        bad.append("[P8] 检测到 user_custom 自定义输入，但 custom_anchors 为空：自由回答必须回锚校验")

    # 9. P9: 明确通过授权
    verdict = data.get("verdict")
    if verdict != "pass":
        bad.append(f"[P9] 纪要未获得用户明确拍板通过 (verdict='{verdict}' != 'pass')")

    return bad


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("用法: python -m dramacraft.premise_lint <ep###.json>")
        sys.exit(0)

    fpath = sys.argv[1]
    if not os.path.exists(fpath):
        print(f"❌ 文件不存在: {fpath}")
        sys.exit(1)

    with open(fpath, "r", encoding="utf-8") as fp:
        data = json.load(fp)

    violations = lint_premise(data)
    if violations:
        print("\n================== 前提纪要预审门未通过 ==================")
        for v in violations:
            print(f"✗ {v}")
        print("==========================================================")
        sys.exit(1)

    print("✓ 前提纪要 P1~P9 预审门全部通过！可以转译大纲或进入剧本编写。")


if __name__ == "__main__":
    main()
