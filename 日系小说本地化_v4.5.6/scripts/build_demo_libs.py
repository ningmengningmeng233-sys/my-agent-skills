#!/usr/bin/env python3
"""示范库骨架生成：从索引库按场景标签提取候选示范段（原文摘录+出处）。

三步分工第 1 步（机械）：脚本出"候选骨架" → AI 补拆解 → 盲审验收。
脚本只负责"找材料"：定位场景 + 逐字摘录，不加工、不拆解。

用法:
  python build_demo_libs.py --type <类型> [--lib <索引库>] [--out <输出目录>]
  python build_demo_libs.py --list            # 列出类型清单与状态
  python build_demo_libs.py --type 动作戏 --force   # 强制重建

约束（用户确认）:
  1. 幂等：目标存在跳过（--force 重建）
  2. 增量：按类型只生成缺失的
  3. 零命中：退出码 1 + 提示换标签
  4. 摘录逐字性：按原文提取，不加工（含扫描痕迹原样）
  5. 原子性：临时文件 + 校验后落位
  6. 段落定位（2026-08-17 改进）：标签 → 索引「包含元素」组名 → 爽点/看点
     条目行号（如 行86-104）→ 只摘该范围段；索引无行号时回退全文行序

退出码: 0=完成, 1=零命中/错误
"""
import argparse
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_LIB = str(Path(__file__).resolve().parent.parent / "索引库")
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "references" / "示范库"

# 类型清单（6 先行 + 3 预留）：标签 → 每类目标段数 → 段字数范围
# 标签须能命中索引 frontmatter「包含元素」词表（如「日常过渡」「越级杀敌」），
# 组名（该行首个词）用于在爽点/看点区定位行号
TYPES: dict[str, dict] = {
    "对白":    {"tags": ["高张力对白", "金句", "打脸"], "n": 6, "min": 150, "max": 300},
    "叙述者":  {"tags": ["氛围", "大场面"], "n": 5, "min": 150, "max": 300},
    "章末":    {"tags": ["金句", "悬念"], "n": 6, "min": 80, "max": 200},
    "动作戏":  {"tags": ["战斗对决", "越级杀敌", "反杀"], "n": 4, "min": 150, "max": 300},
    "转场":    {"tags": ["日常过渡", "衔接"], "n": 4, "min": 80, "max": 200},
    "情绪":    {"tags": ["虐心", "紧张", "热血"], "n": 4, "min": 100, "max": 250},
    "环境":    {"tags": ["氛围"], "n": 3, "min": 80, "max": 200},
    "修辞":    {"tags": ["金句"], "n": 3, "min": 100, "max": 250},
    "吐槽":    {"tags": ["轻松", "搞笑"], "n": 3, "min": 100, "max": 250},
}


def find_idx_files(lib: Path) -> list[Path]:
    idx_dir = lib / "章节索引"
    if not idx_dir.is_dir():
        raise FileNotFoundError(f"章节索引目录不存在: {idx_dir}")
    return sorted(idx_dir.glob("*-idx.md"))


def match_tag_group(frontmatter: str, tags: list[str]) -> tuple[str, str] | None:
    """「包含元素」区匹配标签 → (组名, 标签)。组名=该行首个词（爽点条目以组名开头）"""
    for line in frontmatter.splitlines():
        if not line.startswith("  - "):
            continue
        if hit := next((t for t in tags if t in line), None):
            return line[4:].split(" | ", 1)[0], hit
    return None


def find_line_range(body: str, group: str) -> tuple[str, int, int] | None:
    """定位含组名的条目 → (定位方式, lo, hi)。
    条目带行号取条目行号；无行号回退所在章节范围（章节标题自带行号）；
    两者都无返回 None（摘录回退全文行序）"""
    chapter: tuple[int, int] | None = None
    fallback: tuple[int, int] | None = None
    for line in body.splitlines():
        if cm := re.search(r"^##\s+\S+.*[（(]行号[:：]?\s*(\d+)\s*[-~～]\s*(\d+)", line):
            chapter = (int(cm.group(1)), int(cm.group(2)))
        if group not in line:
            continue
        if m := re.search(r"[（(]行(\d+)(?:[-~～](\d+))?[）)]", line):
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            return "爽点条目", lo, hi  # 优先精确条目行号
        if fallback is None and chapter:
            fallback = chapter
    if fallback:
        return "章节范围", *fallback
    return None


def locate_segments(idx_files: list[Path], tags: list[str], n: int, lib: Path) -> list[tuple[Path, str, str, tuple[str, int, int] | None]]:
    """标签定位段文件 → (源文件, 标签, 组名, (定位方式, lo, hi)|None)。
    只在 frontmatter「包含元素」匹配（防正文剧情词误命中）；
    定位缺失时摘录回退全文行序（拆解时另选并标注）"""
    found: list[tuple[Path, str, str, tuple[str, int, int] | None]] = []
    for idx in idx_files:
        text = idx.read_text(encoding="utf-8", errors="ignore")
        frontmatter, _, body = text.partition("\n---")
        group_hit = match_tag_group(frontmatter, tags)
        if not group_hit:
            continue
        group, tag = group_hit
        m = re.search(r"源文件: (.+)", frontmatter)
        if not m:
            continue
        src = lib / m.group(1).strip()
        if not src.exists():
            continue
        found.append((src, tag, group, find_line_range(body, group)))
        if len(found) >= n:
            break
    return found


def extract_excerpts(src: Path, min_len: int, max_len: int,
                     line_range: tuple[int, int] | None = None) -> list[str]:
    """提取候选摘录：原文每段一行；有行号范围时优先只摘该范围段（回退全文行序）。
    下限 80 字软过滤；超长行逐字截前 max_len（摘录纪律：不加工）"""
    text = src.read_text(encoding="utf-8", errors="ignore")
    if text.startswith("---"):
        text = text.split("\n---", 1)[1]
    lines = [ln.strip() for ln in text.splitlines()]
    if line_range:
        lo, hi = line_range
        ranged = lines[lo - 1:hi]
        if any(len(ln) >= min(min_len, 80) for ln in ranged):
            lines = ranged
    out: list[str] = []
    for line in lines:
        if len(line) < min(min_len, 80):
            continue
        out.append(line[:max_len] if len(line) > max_len else line)
        if len(out) >= 3:
            break
    return out


def write_atomic(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)  # 原子落位


def build_one(demo_type: str, lib: Path, out_dir: Path, force: bool) -> int:
    spec = TYPES[demo_type]
    target = out_dir / f"{demo_type}示范库-候选.md"
    if target.exists() and not force:
        print(f"⏭️  {demo_type}示范库已存在（--force 重建）")
        return 0

    idx_files = find_idx_files(lib)
    segs = locate_segments(idx_files, spec["tags"], spec["n"], lib)
    if not segs:
        print(f"❌ 零命中: 标签 {spec['tags']} 在索引库无匹配——换同义标签重试")
        return 1

    lines = [f"# {demo_type}示范库·候选骨架（{len(segs)} 段，生成 {Path(__file__).name}）",
             "",
             "> 三步分工第 1 步产物：原文逐字摘录+出处。AI 补拆解时",
             "> 精读原文段 → 选 4-6 段 → 写「为什么活」拆解（规则实例化，",
             "> 3-5 条，指句为证）→ 盲审验收（准确性/指句真实性/教学价值）。",
             "> 摘录纪律：逐字原样（含扫描痕迹），禁加工。",
             "> 段落定位：索引「包含元素」组名 → 爽点/看点条目行号；",
             "> 无行号时回退全文行序（拆解时另选须标注）。", ""]
    for i, (src, tag, group, rng) in enumerate(segs, 1):
        if rng:
            kind, lo, hi = rng
            loc = f"{kind} 行号 {lo}-{hi}"
        else:
            loc = "全文行序（索引无定位）"
        lines.append(f"## 候选 {i}（标签: {tag}）")
        lines.append(f"出处: {src.relative_to(lib)}")
        lines.append(f"定位: {loc}")
        lines.append("")
        for ex in extract_excerpts(src, spec["min"], spec["max"], (lo, hi) if rng else None):
            lines.append(ex)
            lines.append("")
        lines.append("【拆解位】")
        lines.append("")
    write_atomic(target, "\n".join(lines))
    print(f"✅ {demo_type}示范库骨架已生成: {target}（{len(segs)} 段候选）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="示范库骨架生成")
    ap.add_argument("--type", choices=sorted(TYPES), help="示范库类型")
    ap.add_argument("--lib", default=DEFAULT_LIB, help="索引库路径")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--force", action="store_true", help="强制重建")
    ap.add_argument("--list", action="store_true", help="列出类型与状态")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.list:
        # 正式版文件名可能与类型 key 不一致（如"叙述者"→"叙述者人格示范库.md"），
        # 用前缀匹配而非精确拼接
        finals = {p.stem for p in out_dir.glob("*示范库.md")}
        for t, spec in TYPES.items():
            cand = out_dir / f"{t}示范库-候选.md"
            if any(n.startswith(t) for n in finals):
                status = "✅ 已有正式版"
            elif cand.exists():
                status = "🆕 候选骨架（待拆解）"
            else:
                status = "⏳ 待生成"
            print(f"  {t}: {status}（标签 {spec['tags']}）")
        return 0

    if not args.type:
        ap.print_help()
        return 2

    try:
        return build_one(args.type, Path(args.lib), out_dir, args.force)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
