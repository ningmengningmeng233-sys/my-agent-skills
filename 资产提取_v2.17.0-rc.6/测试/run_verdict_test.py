#!/usr/bin/env python3
"""Offline integration test for verdict parsing and final rendering."""
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "scripts" / "engine"))

from aggregate import (
    build_chunk_input,
    cross_check_by_type,
    group_records,
    load_original_texts,
    parse_products,
    parse_verdict,
    render_final,
)

BASE = SKILL / "测试" / "样例"
prod_dir = BASE / "段产物" / "全流程测试-流测"
orig_dir = BASE / "原文段" / "全流程测试-流测"

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} {detail}")
        sys.exit(1)


def main() -> int:
    summaries, records, needs, unparsed = parse_products(prod_dir)
    check("段产物解析无未解析行", not unparsed, str(unparsed))
    check("摘要表非空", len(summaries) > 0, f"{len(summaries)}")
    check("资产记录非空", len(records) > 0, f"{len(records)}")
    texts = load_original_texts(orig_dir)
    groups = group_records(records, texts)
    check("预聚合分组非空", len(groups) > 0, f"{len(groups)} 组")
    by_type = cross_check_by_type(summaries, groups)

    for i, g in enumerate(groups, 1):
        g["组ID"] = f"G{i:02d}"
    for chunk in ("人物", "场景", "道具", "服装", "基础信息"):
        txt = build_chunk_input(chunk, "全流程测试", "流测", summaries, groups, by_type, len(summaries))
        check(f"分块输入生成: {chunk}", len(txt) > 50, f"{len(txt)} 字符")
    for chunk in ("人物", "场景", "道具", "服装"):
        txt = build_chunk_input(chunk, "全流程测试", "流测", summaries, groups, by_type, len(summaries))
        check(
            f"非基础信息块禁止重复项目基础信息: {chunk}",
            "仅输出【资产裁决】与【待确认】表；【项目基础信息】表只在「基础信息」块输出，本块禁止重复输出。" in txt,
        )
    txt_base = build_chunk_input("基础信息", "全流程测试", "流测", summaries, groups, by_type, len(summaries))
    check("基础信息块仍要求输出项目基础信息", "输出【项目基础信息】表" in txt_base)

    verdict_text = (BASE / "裁决_模拟.md").read_text(encoding="utf-8")
    verdicts, info, pending, n_dropped = parse_verdict(verdict_text)
    formal = [v for v in verdicts if v["处理"] != "附录"]
    check("裁决解析:正式资产非空", len(formal) > 0, f"{len(formal)}")
    check("裁决解析:基础信息解析", info.get("项目名称") in ("测试全流程小说",), str(info.get("项目名称")))
    types = {v["类型"] for v in formal}
    check("裁决类型覆盖(人物/场景/道具)", {"人物", "场景", "道具"} <= types, str(types))

    dirty = """## 资产裁决
| 组ID | 类型 | 主名 | 等级 | 合并 | 别名 | 绑定 | 处理 | 备注 |
|---|---|---|---|---|---|---|---|---|
| G01 | 人物 | (无) | | | | | 保留 | 测试 |
| G05 | 场景 | (无) | | | | | 附录 | 全部名称未在原文出现 |
## 项目基础信息
| 项目名称 | 测试 |
"""
    v2, i2, p2, n2 = parse_verdict(dirty)
    check("(无) 正式行被 dropped", len([v for v in v2 if v["处理"] != "附录"]) == 0)
    check("(无) 附录行归一化为组ID", any(p["名称"] == "G05" for p in p2))

    dirty2 = """## 资产裁决
| 组ID | 类型 | 主名 | 等级 | 合并 | 别名 | 绑定 | 处理 | 备注 |
|---|---|---|---|---|---|---|---|---|
| G01 | 人物 | 张三 | B | | | | 保留 | |
## 项目基础信息
| 项目名称 | 测试 |
## 待确认
| 名称 | 类型 | 说明 |
|---|---|---|
| （无） |  |  |
| （空） |  |  |
| — |  |  |
| 天台 | 场景 | 出处未验证 |
"""
    v3, i3, p3, n3 = parse_verdict(dirty2)
    placeholders = {"（无）", "（空）", "(无)", "无", "—", ""}
    check("待确认节占位行被过滤", not any(p["名称"] in placeholders for p in p3), str(p3))
    check("待确认节正常行保留", any(p["名称"] == "天台" for p in p3))

    groups_by_id = {g["组ID"]: g for g in groups}
    final = render_final("全流程测试", "流测", verdicts, info, pending, groups_by_id, summaries, len(summaries), needs=needs)
    modules = [
        "项目基础信息",
        "人物资产提取表",
        "场景资产提取表",
        "道具资产提取表",
        "服装妆造资产提取表",
        "分集",
        "导入资产总表",
        "文件夹结构",
        "衍生需求汇总",
        "合并统计",
        "待确认附录",
        "交付物说明",
    ]
    for m in modules:
        check(f"渲染含模块: {m}", m in final)

    dup_verdicts = [dict(v) for v in verdicts] + [{
        "组ID": "G99", "类型": "道具", "主名": "音响", "等级": "B",
        "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": "补建",
    }]
    dup_pending = list(pending) + [{"名称": "音响", "类型": "道具", "说明": "LLM 重复列举"}]
    dup_final = render_final(
        "全流程测试", "流测", dup_verdicts, info, dup_pending,
        groups_by_id, summaries, len(summaries), needs=needs,
    )
    appendix = dup_final.split("## 待确认附录")[1] if "## 待确认附录" in dup_final else ""
    check("正式表/附录去重(音响不进附录)", "音响" not in appendix, appendix[:100])

    print(f"\n✅ 裁决解析与渲染全部通过（{PASS} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
