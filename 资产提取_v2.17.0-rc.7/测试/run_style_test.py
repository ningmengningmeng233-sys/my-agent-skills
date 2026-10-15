# -*- coding: utf-8 -*-
"""style 模块解析回归测试（离线，不调 LLM）——画风库解析/匹配/锁定块契约。

覆盖：
1. parse_style_card：标准卡字段完整 / A1 分层提取 / 字段名兼容（B3 中文/英文）/ 找不到兜底 A1
2. find_card_by_name：精确 / 完整词前缀 / 不误吞（「仙侠奇幻风 但要更写实」→ free）
3. resolve_style：题材映射命中 / 名称命中 / 自由描述 / 未命中题材 → A1 兜底
4. build_style_block / build_render_block：A1/A2 均含分层基线（ADR-0076 全卡分层化）
5. load_style_map：多题材拆分 / 组合 ID 取首个

用法: python run_style_test.py
"""
import os
import sys

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL, "scripts"))
sys.path.insert(0, os.path.join(SKILL, "scripts", "engine"))

from engine import style as s

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} {detail}")
        sys.exit(1)


def main():
    # ---- 1. parse_style_card ----
    a1 = s.parse_style_card("A1")
    check("A1 解析:名称", a1["name"].startswith("国漫三维基础风"), a1["name"])
    check("A1 解析:中文定性非空", len(a1["zh"]) > 50)
    check("A1 解析:英文标签块非空", "PBR" in a1["en"] and "no cel-shading" in a1["en"])
    check("A1 解析:负面非空", "无网红脸" in a1["negative"])
    check("A1 分层提取(建模+渲染层)", any("建模层" in l for l in a1["layers"]) and any("渲染层" in l for l in a1["layers"]))
    b3 = s.parse_style_card("B3")  # 字段名兼容:「- **中文**/「- **英文**」
    check("B3 字段名兼容:中文非空", len(b3["zh"]) > 20, b3["zh"][:20])
    check("B3 字段名兼容:英文非空", len(b3["en"]) > 20)
    card = s.parse_style_card("ZZ99")  # 不存在 → 兜底 A1
    check("不存在卡片兜底 A1", card is not None and card["id"] == "A1")

    # ---- 2. find_card_by_name ----
    c = s.find_card_by_name("中式国漫三维 基础风")  # 精确（标题带括号后缀时用前缀）
    check("名称精确匹配", c is not None and c["id"] == "A1")
    c = s.find_card_by_name("国风水墨")  # 完整词前缀 → B1
    check("名称前缀匹配", c is not None and c["id"] == "B1")
    # ---- 3.5 别名匹配（ADR-0076 直白命名兼容层：旧名/俗称仍可命中） ----
    for alias, expected in (("3D渲染v8", "H18"), ("儿童插画", "H12"), ("水墨", "B1"),
                            ("西幻史诗", "H6"), ("纸人", "H11"), ("皮克斯", "D3"),
                            ("中式国漫三维 基础风", "A1")):
        c2 = s.find_card_by_name(alias)
        check(f"别名 {alias} → {expected}", c2 is not None and c2["id"] == expected)
    c = s.find_card_by_name("仙侠奇幻风 但要更写实")  # name 是标题前缀的继续 → 不误吞
    check("不误吞卡片(自由描述)", c is None)

    # ---- 3. resolve_style ----
    card, mode = s.resolve_style(None, "仙侠奇幻三维渲染")
    check("名称 → H1 卡片", mode == "card" and card["id"] == "H1")
    card, mode = s.resolve_style(None, "仙侠奇幻风")
    check("精确名 仙侠奇幻风 → A2", mode == "card" and card["id"] == "A2")
    card, mode = s.resolve_style(None, "仙侠奇幻")
    check("中文简写 仙侠奇幻 → A2 卡片（不再落为自由描述）", mode == "card" and card["id"] == "A2")
    card, mode = s.resolve_style(None, "自由描述 测试")
    check("自由描述模式", mode == "free" and card is None)
    card, mode = s.resolve_style("玄幻")
    check("题材映射 玄幻 → 仙侠国风电影CG(H7)", mode == "card" and card["id"] == "H7"
          and card["name"] == "仙侠国风电影CG")
    card, mode = s.resolve_style("仙侠")
    check("题材映射 仙侠 → 仙侠国风电影CG(H7)", mode == "card" and card["id"] == "H7"
          and card["name"] == "仙侠国风电影CG")
    card, mode = s.resolve_style("不存在的题材XYZ")
    check("未命中题材 → A1 兜底", mode == "card" and card["id"] == "A1")

    # ---- 4. 锁定块 / 渲染层契约 ----
    block = s.build_style_block(a1)
    check("A1 全量锁定块含中文定性+中文锁定句+禁止句", "【风格锁定】" in block and "风格锁定句" in block and "Style lock" not in block and "禁止" in block)
    render = s.build_render_block(a1)
    check("A1 渲染层块含建模/渲染层", "建模层" in render and "渲染层" in render)
    check("A1 渲染层块不含色调层", "色彩层" not in render)
    a2 = s.parse_style_card("A2")
    render_a2 = s.build_render_block(a2)
    check("A2 渲染层块含建模/渲染层(ADR-0076 分层基线)", "建模层" in render_a2 and "渲染层" in render_a2)
    check("A2 渲染层块为纯中文渲染基调", "渲染基调" in render_a2 and "Render style lock" not in render_a2)

    # ---- 5. load_style_map ----
    m = s.load_style_map()
    check("映射:玄幻 → 仙侠国风电影CG（中文正式名，v2.15.0）", m.get("玄幻") == "仙侠国风电影CG")
    check("映射:仙侠 → 仙侠国风电影CG（中文正式名，v2.15.0）", m.get("仙侠") == "仙侠国风电影CG")
    check("映射:多题材拆分(权谋/宫斗)", m.get("宫斗") == "A3")
    check("映射:组合 ID 取首个", m.get("田园") in ("A4",))
    check("映射:括号内斜杠不生成垃圾键", "职场）" not in m)
    check("映射:括号内完整题材保留", m.get("现代都市（都市情感/职场）") == "C6")
    check("映射:现代都市 → C6 日常风", m.get("现代都市") == "C6")
    check("映射:赛博朋克 → C10 保留", m.get("赛博朋克") == "C10")

    print(f"\n✅ style 解析回归测试全部通过（{PASS} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
