#!/usr/bin/env python3
"""Bridge v2 pipeline output into the costume design module (ADR-0006)."""
import argparse
import re
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SKILL_DIR = Path(__file__).resolve().parent.parent
GENRE_MAP = SKILL_DIR / "references" / "题材-剧种映射.md"


def load_genre_map() -> dict[str, tuple[str, bool]]:
    """Parse 题材-剧种映射.md → {genre: (drama_type, approximate)}."""
    out = {}
    for line in GENRE_MAP.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or "v2 题材" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3 or not cells[0]:
            continue
        approx = len(cells) > 2 and ("近似" in cells[2] or (len(cells) > 3 and "近似" in cells[3]))
        for g in re.split(r"[/、,，]", cells[0]):
            out[g.strip()] = (cells[1], approx)
    return out


def resolve_genres(genre: str, gmap: dict[str, tuple[str, bool]]) -> tuple[str | None, bool, list[str]]:
    """Resolve primary drama type plus distinct secondary types for mixed genres."""
    genre = (genre or "").strip()
    hits = []
    for k in gmap:
        if k and k in genre:
            hits.append((genre.index(k), -len(k), k, gmap[k]))
    hits.sort(key=lambda x: (x[0], x[1]))
    if genre in gmap:
        hits.insert(0, (-1, -len(genre), genre, gmap[genre]))
    if not hits:
        return None, False, []
    gname, approx = hits[0][3]
    seen = {gname}
    secondary = []
    for h in hits[1:]:
        if h[3][0] not in seen:
            secondary.append(h[3][0])
            seen.add(h[3][0])
    return gname, approx, secondary


def parse_table_section(text: str, title: str) -> list[list[str]]:
    """Parse a table section from the final reverse-derived file."""
    m = re.search(rf"##\s+{re.escape(title)}.*?\n(.*?)(?=\n##\s|\Z)", text, re.S)
    if not m:
        return []
    rows = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and not cells[0].startswith("资产ID"):
            rows.append(cells)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="服饰设计衔接:反推 → 角色清单 + 剧种")
    parser.add_argument("final", help="反推_最终.md 路径")
    parser.add_argument("--genre", required=True, help="题材（用于剧种判定）")
    parser.add_argument("--lib", required=True, help="资产库根目录")
    parser.add_argument("--title", default="", help="剧名（默认取反推文件项目名称，不带书名号）")
    args = parser.parse_args()

    text = Path(args.final).read_text(encoding="utf-8")
    people = parse_table_section(text, "人物资产提取表")
    outfits = parse_table_section(text, "服装妆造资产提取表")
    if not people:
        print(f"❌ 反推文件未解析到「人物资产提取表」: {args.final}")
        print("   请确认反推文件包含人物表（步骤 3 聚合产物），或检查文件路径。")
        return 1

    title = args.title
    if not title:
        m = re.search(r"项目名称\s*\|\s*《?([^》|\n]+)》?", text)
        if m:
            title = m.group(1).strip()
    if not title:
        title = Path(args.final).stem

    gmap = load_genre_map()
    genre = (args.genre or "").strip()
    gname, approx, secondary = resolve_genres(genre, gmap)

    lines = [
        f"# 《{title}》角色服饰清单（v2 流水线自动生成，衔接服饰设计）",
        "",
        f"剧种: {gname or '（未命中映射，需用户指定）'}" + (f"（近似:原题材「{genre}」→ 剧种 {gname}）" if approx and gname else ""),
    ]
    if secondary:
        lines.append(f"副剧种: {'、'.join(secondary)}")
        lines.append("> 混合题材融合规则：白名单交集、黑名单并集、配色以主剧种为基准（服饰设计 ADR-0002）。")
    lines += [
        f"来源: v2 反推产物 {Path(args.final).name} | 人物 {len(people)} / 已有造型 {len(outfits)}",
        "",
        "> 身份/性格字段缺失时按服饰设计 2.1 规则「按剧种默认补全并标注」。",
        "",
        "| 角色名 | 身份 | 性格关键词 | 特殊设定 |",
        "|---|---|---|---|",
    ]
    for p in people:
        name = p[1] if len(p) > 1 else ""
        note = p[5] if len(p) > 5 else ""
        level = p[4] if len(p) > 4 else ""
        special = [f"等级:{level}"] if level else []
        bindings = [o[2] for o in outfits if len(o) > 2 and o[2] == name]
        if bindings:
            special.append("已有造型:" + "/".join(bindings))
        lines.append(f"| {name} | {note} |  | {'；'.join(special)} |")
    lines.append("")
    lines.append("> 生成后按服饰设计流程: 标准库 → 逐角色条目 → check_costumes.py 校验 → 落盘。")
    content = "\n".join(lines)

    out_dir = Path(args.lib) / "服饰设计" / title
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "角色清单.md"
    out.write_text(content, encoding="utf-8")

    print(f"✅ 角色清单已生成: {out}")
    print(f"  剧种: {gname or '未命中(需用户指定)'} | 人物 {len(people)} 名 | 已有造型 {len(outfits)} 个")
    if secondary:
        print(f"  副剧种: {'、'.join(secondary)}（白名单交集 / 黑名单并集 / 配色以主剧种为基准）")
    print("  下一步: 编排器按 服饰设计/SKILL.md 执行(标准先行 → 逐角色生成 → 校验)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
