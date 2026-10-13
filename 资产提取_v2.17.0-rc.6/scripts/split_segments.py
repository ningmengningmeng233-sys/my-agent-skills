#!/usr/bin/env python3
"""Split a novel source file into indexed segment files."""
import argparse
import os
import re
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
try:
    from engine.common import parse_frontmatter, load_env, read_text_auto

    load_env()
except ImportError:
    pass

ASSET_LIB_DIR = Path(os.environ.get("ASSET_LIB_DIR", "").strip() or SKILL_DIR / "资产库")

# 章节标题（含无编号：序章/楔子/番外等）
# v4.5: 字符类补「千/万」；无编号词后必须跟空白/标点/行尾，避免正文行误判。
# v2.4: 行首数字列表仅在编号后仍有章节标记时才识别。
# v2.15: 剧本场次标记「第N场」纳入识别，但必须有分隔符（第一场雪不是场次标题）。
SCENE_DELIM = r"(?=[ \t：:，。．、\-—]|$)"
CHAPTER_RE = re.compile(
    r"^第[一二三四五六七八九十百零万千0-9]+[章节回卷话集][^\n]{0,40}"
    r"|^第[一二三四五六七八九十百零万千0-9]+场" + SCENE_DELIM + r"[^\n]{0,40}"
    r"|^(?:Chapter|Book|Volume|Episode|Part)\s*\d+[^\n]{0,40}"
    r"|^\d+[\.\、．]\s*(?:(?:第?[一二三四五六七八九十百零万千0-9]+[章节回卷话集])|(?:Chapter|Book|Volume|Episode|Part)\s*\d+|(?:序章|楔子|终章|番外|间章|尾声|后记))(?=[ \t：:，。．]|$)[^\n]{0,40}"
    r"|^(?:序章|楔子|终章|番外|间章|尾声|后记)(?=[ \t：:，。．]|$)[^\n]{0,40}",
    re.M,
)
CH_NAME_RE = re.compile(
    r"第?[一二三四五六七八九十百零万千0-9]+[章回话节卷集]"
    r"|第?[一二三四五六七八九十百零万千0-9]+场" + SCENE_DELIM
    + r"|序章|楔子|终章|番外|间章|尾声|后记|Chapter\s*\d+",
    re.I,
)

CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def cn2ar(s: str) -> int:
    """Chinese numerals → integer (up to 10k)."""
    if s.isdigit():
        return int(s)
    if len(s) > 1 and all(ch in CN_DIGITS for ch in s):
        n = 0
        for ch in s:
            n = n * 10 + CN_DIGITS[ch]
        return n
    total, num = 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            num = CN_DIGITS[ch]
        elif ch.isdigit():
            num = num * 10 + int(ch)
        elif ch == "十":
            total += num * 10 if num else 10
            num = 0
        elif ch == "百":
            total += num * 100 if num else 100
            num = 0
        elif ch == "千":
            total += num * 1000 if num else 1000
            num = 0
        elif ch == "万":
            total = (total + num) * 10000
            num = 0
    return total + num


def normalize_ch_name(raw: str) -> str:
    """Normalize chapter title to '第X章' form（剧本场次归一为「第X场」）。"""
    m = re.search(
        r"(?:第)?([一二三四五六七八九十百零万千0-9]+)([章回话节卷集]|场(?=[ \t：:，。．、\-—]|$))"
        r"|(序章|楔子|终章|番外|间章|尾声|后记)",
        raw,
    )
    if not m:
        m2 = re.search(r"Chapter\s*(\d+)", raw, re.I)
        return f"第{m2.group(1)}章" if m2 else raw
    if m.group(1):
        return f"第{cn2ar(m.group(1))}{m.group(2)}"
    return f"第{m.group(3)}"


def norm_name(name: str) -> str:
    """File-name safe version of a novel/author name."""
    return re.sub(r'[\\/:*?"<>|\s《》]', "", name or "")


def find_chapter_lines(body_lines: list[str]) -> list[tuple[int, str, str]]:
    """Return [(1-based line, title, normalized chapter name)]."""
    out = []
    for i, line in enumerate(body_lines, 1):
        m = CHAPTER_RE.match(line.strip())
        if not m:
            continue
        title = m.group(0).strip()
        nm = CH_NAME_RE.search(title)
        out.append((i, title, normalize_ch_name(nm.group(0)) if nm else f"第{i}节"))
    return out


def make_seg_content(fname: str, frontmatter: dict, body: str) -> str:
    fm = (
        "---\n"
        f"小说名称: 《{frontmatter['novel']}》\n"
        f"作者: {frontmatter['author']}\n"
        f"章节范围: {frontmatter['chapter_range']}\n"
        f"对应产物: {fname.replace('.md', '-资产记录.md')}\n"
        f"分段方式: {frontmatter['split_mode']}\n"
        f"源文件: {frontmatter.get('source_file', '')}\n"
    )
    # 规范化输入（DOCX/EPUB/多文件）额外记录原始来源定位与来源清单（规格 4）
    if frontmatter.get("source_locator"):
        fm += f"原始来源定位: {frontmatter['source_locator']}\n"
    if frontmatter.get("source_manifest"):
        fm += f"来源清单: {frontmatter['source_manifest']}\n"
    return fm + "---\n" + body


def _ch_no(name: str) -> int | None:
    m = re.search(r"第(\d+)[章回话节卷集]", name or "")
    return int(m.group(1)) if m else None


def range_label(start_name: str, last_name: str) -> str:
    """Segment range label; swaps if chapter numbers are descending."""
    if start_name == last_name:
        return start_name
    a, b = _ch_no(start_name), _ch_no(last_name)
    if a is not None and b is not None and a > b:
        start_name, last_name = last_name, start_name
    return f"{start_name}~{last_name}"


def write_seg_if_needed(seg_dir: Path, fname: str, frontmatter: dict, body: str, force: bool) -> str:
    """Write segment file if missing, unchanged, or forced. Returns 'written'/'skipped'/'conflicted'."""
    content = make_seg_content(fname, frontmatter, body)
    out = seg_dir / fname
    if out.exists() and not force:
        if out.read_text(encoding="utf-8") == content:
            return "skipped"
        return "conflicted"
    out.write_text(content, encoding="utf-8")
    return "written"


def split_by_chapters(
    body_lines: list[str],
    chapters: list[tuple[int, str, str]],
    per_seg: int,
    novel: str,
    author: str,
    seg_dir: Path,
    force: bool,
    seg_no_start: int = 0,
    source_file: str = "",
    overlap: int = 0,
    provenance=None,
) -> tuple[int, int, int, list[str]]:
    """Split by chapters; returns (created, conflicts, last_seg_no, planned_names)."""
    created = conflicts = 0
    planned: list[str] = []
    seg_no = seg_no_start
    for idx in range(0, len(chapters), per_seg):
        seg_no += 1
        grp = chapters[idx : idx + per_seg]
        start_line = grp[0][0]
        if overlap > 0 and idx > 0:
            start_line = chapters[max(0, idx - overlap)][0]
        end_line = chapters[idx + per_seg][0] - 1 if idx + per_seg < len(chapters) else len(body_lines)
        start_name = chapters[max(0, idx - overlap)][2] if (overlap > 0 and idx > 0) else grp[0][2]
        last_name = grp[-1][2]
        ch_range = range_label(start_name, last_name)
        fname = f"{novel}-{author}-{ch_range}-{seg_no:03d}.md"
        planned.append(fname)
        body = "\n".join(body_lines[start_line - 1 : end_line])
        st = write_seg_if_needed(
            seg_dir,
            fname,
            {
                "novel": novel,
                "author": author,
                "chapter_range": ch_range,
                "split_mode": "按章节切分",
                "source_file": source_file,
                "source_locator": provenance.span(start_line, end_line) if provenance else "",
                "source_manifest": provenance.manifest_rel if provenance else "",
            },
            body,
            force,
        )
        if st == "skipped":
            print(f"  ⏭️  {fname}: 已存在且内容一致（跳过）")
            continue
        if st == "conflicted":
            print(f"  ⚠️  {fname}: 已存在但内容不同——未覆盖（源文件已变更或分卷章号重名）")
            conflicts += 1
            continue
        print(f"  ✅ {fname}: 行 {start_line}~{end_line}（{grp[0][1]} → {grp[-1][1]}）")
        created += 1
    return created, conflicts, seg_no, planned


def list_existing_segs(seg_dir: Path) -> list[tuple[int, str]]:
    out = []
    for path in seg_dir.iterdir():
        if not path.is_file() or path.suffix != ".md":
            continue
        m = re.search(r"-(\d{3})\.md$", path.name)
        if m:
            out.append((int(m.group(1)), path.name))
    return sorted(out)


def append_new_chapters(
    body_lines: list[str],
    chapters: list[tuple[int, str, str]],
    seg_dir: Path,
    novel: str,
    author: str,
    per_seg: int,
    source_file: str,
) -> tuple[int, int, list[str]] | None:
    """Incremental append: only new chapters after the last segment's last chapter."""
    segs = list_existing_segs(seg_dir)
    if not segs:
        print("  ❌ --append 需要已有段文件（请先不带 --append 跑一次全量拆段）")
        return None
    last_no, last_fname = segs[-1]
    last_path = seg_dir / last_fname
    last_meta, last_body = parse_frontmatter(last_path.read_text(encoding="utf-8"))

    anchor = None
    for m in CHAPTER_RE.finditer(last_body):
        anchor = m.group(0).strip()
    if not anchor:
        print(f"  ❌ 末段 {last_fname} 内未识别到章节标题，无法增量；请用 --force 全量重切")
        return None

    pos = None
    for i, (_, title, _) in enumerate(chapters):
        if title.strip() == anchor:
            pos = i
    if pos is None:
        print(f"  ❌ 锚点章节「{anchor}」在源文件中不存在；源文件可能已整体更换，请用 --force 全量重切")
        return None

    new_chs = chapters[pos + 1 :]
    if not new_chs:
        print(f"  ℹ️  无新增章节（末段末章「{anchor}」已是全书最后一章）")
        return 0, 0, []

    created = merged = 0
    conflicts: list[str] = []
    if len(new_chs) >= per_seg:
        for idx in range(0, len(new_chs), per_seg):
            grp = new_chs[idx : idx + per_seg]
            seg_no = last_no + idx // per_seg + 1
            start_line = grp[0][0]
            end_line = new_chs[idx + per_seg][0] - 1 if idx + per_seg < len(new_chs) else len(body_lines)
            start_name, last_name = grp[0][2], grp[-1][2]
            ch_range = range_label(start_name, last_name)
            fname = f"{novel}-{author}-{ch_range}-{seg_no:03d}.md"
            body = "\n".join(body_lines[start_line - 1 : end_line])
            st = write_seg_if_needed(
                seg_dir,
                fname,
                {
                    "novel": novel,
                    "author": author,
                    "chapter_range": ch_range,
                    "split_mode": "按章节切分",
                    "source_file": source_file,
                },
                body,
                force=False,
            )
            if st == "conflicted":
                conflicts.append(fname)
                print(f"  ⚠️  {fname}: 已存在但内容不同（未覆盖）")
            else:
                print(f"  ✅ {fname}: 行 {start_line}~{end_line}（新增段）")
                created += 1
        return created, merged, conflicts

    first_new = new_chs[0][0]
    merged_body = "\n".join(body_lines[first_new - 1 :])
    new_body = last_body.rstrip("\n") + "\n" + merged_body
    old_first = None
    for m in CHAPTER_RE.finditer(last_body):
        old_first = normalize_ch_name(m.group(0).strip())
        break
    new_last = new_chs[-1][2]
    ch_range = f"{old_first}~{new_last}" if old_first and old_first != new_last else (old_first or new_last)
    content = make_seg_content(
        last_fname,
        {
            "novel": novel,
            "author": author,
            "chapter_range": ch_range,
            "split_mode": last_meta.get("分段方式") or "按章节切分",
            "source_file": source_file,
        },
        new_body,
    )
    last_path.write_text(content, encoding="utf-8")
    print(f"  ✅ {last_fname}: 并入 {len(new_chs)} 章（{new_chs[0][1]} → {new_chs[-1][1]}，章节范围 {ch_range}）")
    print("     ℹ️  段文件名保持原样（防断点失效重跑全书），章节范围以 frontmatter 为准")
    return created, 1, conflicts


def split_by_chars(
    body_lines: list[str],
    chars_per_seg: int,
    novel: str,
    author: str,
    seg_dir: Path,
    force: bool,
    seg_no_start: int = 0,
    source_file: str = "",
    provenance=None,
) -> tuple[int, int, int, list[str]]:
    """Split by character count when no chapter headings exist."""
    planned: list[str] = []
    paras, cur = [], []
    line_of_para: list[tuple[int, int]] = []
    start = 0
    for i, line in enumerate(body_lines, 1):
        if line.strip() == "" and cur:
            paras.append("\n".join(cur))
            line_of_para.append((start, i - 1))
            cur = []
        else:
            if not cur:
                start = i
            cur.append(line)
    if cur:
        paras.append("\n".join(cur))
        line_of_para.append((start, len(body_lines)))

    created = conflicts = 0
    segs, buf, buf_len = [], [], 0
    seg_spans: list[tuple[int, int]] = []
    buf_start = 1
    for p, (p_start, p_end) in zip(paras, line_of_para):
        if not buf:
            buf_start = p_start
        buf.append(p)
        buf_len += len(p) + 1
        if buf_len >= chars_per_seg:
            segs.append("\n\n".join(buf))
            seg_spans.append((buf_start, p_end))
            buf, buf_len = [], 0
    if buf:
        segs.append("\n\n".join(buf))
        seg_spans.append((buf_start, len(body_lines)))

    total_chars = sum(len(p) + 1 for p in paras)
    seg_no = seg_no_start
    cumulative = 0
    for i, seg in enumerate(segs, 1):
        seg_no += 1
        cumulative += len(seg)
        seg_start = (cumulative - len(seg)) // 10000 + 1
        seg_end = cumulative // 10000
        if seg_end < seg_start:
            seg_end = seg_start
        fname = f"{novel}-{author}-第{seg_start}万字~第{seg_end}万字-{seg_no:03d}.md"
        planned.append(fname)
        lo, hi = seg_spans[i - 1]
        st = write_seg_if_needed(
            seg_dir,
            fname,
            {
                "novel": novel,
                "author": author,
                "chapter_range": f"第{seg_start}万字~第{seg_end}万字",
                "split_mode": "按字数切分",
                "source_file": source_file,
                "source_locator": provenance.span(lo, hi) if provenance else "",
                "source_manifest": provenance.manifest_rel if provenance else "",
            },
            seg,
            force,
        )
        if st == "skipped":
            print(f"  ⏭️  {fname}: 已存在且内容一致（跳过）")
            continue
        if st == "conflicted":
            print(f"  ⚠️  {fname}: 已存在但内容不同——未覆盖（源文件已变更）")
            conflicts += 1
            continue
        print(f"  ✅ {fname}: 段 {i}/{len(segs)}（约 {len(seg)} 字符）")
        created += 1
    print(f"  ℹ️  原文约 {total_chars} 字符 → {len(segs)} 段（每段 ≥ {chars_per_seg} 字符按段落边界）")
    return created, conflicts, seg_no, planned


class Provenance:
    """规范化输入的来源定位映射（规格 4）：把段行区间映射回原始文件/段落。"""

    def __init__(self, normalized, manifest_rel: str):
        self._normalized = normalized
        self.manifest_rel = manifest_rel

    def span(self, start_line: int, end_line: int) -> str:
        from engine.source_normalizer import locator_span_for_lines

        return locator_span_for_lines(self._normalized, start_line, end_line)


def maybe_normalize(sources: list[str], lib: Path, order_file: str | None) -> tuple[bool, list[str], Provenance | None]:
    """非 TXT/MD 或多文件项目先规范化（规格 4）。

    返回 (是否已规范化, 真实拆段输入列表, 来源定位映射)。
    单文件 TXT/Markdown 且无顺序清单时保持既有直读路径不变。
    """
    from engine.source_normalizer import (
        SourceNormalizeError, needs_normalization, normalize_sources, normalized_dir, write_normalized,
    )

    paths = [Path(s) for s in sources]
    order_path = Path(order_file) if order_file else None
    if not needs_normalization(paths, order_path):
        return False, list(sources), None
    try:
        result = normalize_sources(paths, order_path)
    except SourceNormalizeError as e:
        print(f"❌ 输入规范化失败（{e.code}）：{e.detail}")
        raise SystemExit(1)
    out = normalized_dir(lib)
    paths_out = write_normalized(result, out)
    manifest_rel = _rel_posix(Path(paths_out["manifest"]), lib)
    print(f"🔤 已规范化 {len(result.manifest['sources'])} 个来源 → {len(result.units)} 个段落"
          f"（顺序来源: {result.manifest['order_source']}）")
    print(f"   规范化原文: {paths_out['markdown']}")
    return True, [paths_out["markdown"]], Provenance(result, manifest_rel)


def _rel_posix(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(description="源文件 → 索引库段文件（支持多文件/分卷，段号跨文件连续）")
    parser.add_argument("sources", nargs="+",
                        help="源文件路径（.txt/.md/.docx/.epub/.json，可传多个，按顺序拆段）")
    parser.add_argument("--novel", required=True, help="小说名称（原始名，可含《》）")
    parser.add_argument("--author", required=True, help="作者")
    parser.add_argument("--lib", default=str(ASSET_LIB_DIR), help="索引库根目录（默认 .env ASSET_LIB_DIR）")
    parser.add_argument("--chapters-per-seg", type=int, default=5, help="每段章数（有章节时，默认 5）")
    parser.add_argument("--chars-per-seg", type=int, default=10000, help="每段字数下限（无章节时，默认 1 万）")
    parser.add_argument("--overlap", type=int, default=0, help="相邻段重叠章数（推荐 1：消灭段边界割裂）")
    parser.add_argument("--append", action="store_true", help="追更增量：只处理新增章节")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的段文件")
    parser.add_argument("--order-file", help="多文件顺序清单：每行一个相对路径，相对清单所在目录解析")
    args = parser.parse_args()

    if args.append and len(args.sources) > 1:
        print("❌ --append 只支持单个源文件（追更场景）")
        return 1
    if args.append and args.order_file:
        print("❌ --append 不支持 --order-file（追更只需单个源文件）")
        return 1

    for s in args.sources:
        if not Path(s).is_file():
            print(f"❌ 源文件不存在: {s}")
            return 1

    novel = norm_name(args.novel)
    author = norm_name(args.author)
    if not novel or not author:
        print("❌ 小说名/作者不能为空（且需为合法文件名字符）")
        return 1

    lib = Path(args.lib)
    normalized, sources, provenance = maybe_normalize(list(args.sources), lib, args.order_file)

    seg_dir = lib / "原文段" / f"{novel}-{author}"
    seg_dir.mkdir(parents=True, exist_ok=True)

    seg_no = 0
    total_created = 0
    total_conflicts = 0
    all_planned: set[str] = set()
    mode = ""
    for source in sources:
        source_name = Path(source).name
        text = read_text_auto(source)
        _, body = parse_frontmatter(text)
        body_lines = body.splitlines()
        if not body_lines or not "".join(body_lines).strip():
            print(f"⚠️  {source_name}: 正文为空，跳过")
            continue

        chapters = find_chapter_lines(body_lines)
        print(f"📖 {source_name}: 正文 {len(body_lines)} 行, 识别到 {len(chapters)} 个章节标题")

        if args.append:
            if not chapters:
                print("  ❌ --append 要求源文件有章节标题；无章节小说请用全量拆段")
                return 1
            res = append_new_chapters(
                body_lines, chapters, seg_dir, novel, author, args.chapters_per_seg, source_name
            )
            if res is None:
                return 1
            created, merged, conflicts = res
            total_created += created
            total_conflicts += len(conflicts)
            print(f"✅ 增量拆段完成: 新建 {created} 段 / 并入末段 {merged} 次 | 输出: {seg_dir}")
            if total_conflicts:
                print(f"❌ {total_conflicts} 个新增段同名但内容不同——请用 --force 确认覆盖，或检查源文件/分卷配置。")
                return 1
            return 0
        if chapters:
            created, conflicts, seg_no, planned = split_by_chapters(
                body_lines,
                chapters,
                args.chapters_per_seg,
                novel,
                author,
                seg_dir,
                args.force,
                seg_no_start=seg_no,
                source_file=source_name,
                overlap=args.overlap,
                provenance=provenance,
            )
            mode = f"按章节切分（{args.chapters_per_seg} 章/段" + (f"，重叠 {args.overlap} 章" if args.overlap else "") + "）"
        else:
            print("  ⚠️ 未识别到章节标题 → 按字数切分（段落边界）")
            created, conflicts, seg_no, planned = split_by_chars(
                body_lines,
                args.chars_per_seg,
                novel,
                author,
                seg_dir,
                args.force,
                seg_no_start=seg_no,
                source_file=source_name,
                provenance=provenance,
            )
            mode = f"按字数切分（{args.chars_per_seg} 字符/段）"
        all_planned.update(planned)
        total_created += created
        total_conflicts += conflicts

    # 过期段残留拦截（ADR-0081 冒烟发现）：源文件章节范围变化时旧段会静默残留
    stale = [name for _, name in list_existing_segs(seg_dir) if name not in all_planned] if seg_dir.exists() else []
    if stale:
        if args.force:
            for n in stale:
                (seg_dir / n).unlink()
            print(f"🗑️ 已清理 {len(stale)} 个过期段文件（章节范围已变化）: {'、'.join(stale)}")
        else:
            print(f"❌ 发现 {len(stale)} 个旧段文件与本次分段方案不符（源文件章节范围可能已变化）: {'、'.join(stale)}")
            print("   旧段残留会污染后续步骤——确认后加 --force 重跑（将自动清理过期段），或改用 --append 增量模式。")
            return 1

    if total_created == 0 and total_conflicts == 0:
        print("ℹ️  无新段生成（全部已存在且内容一致，用 --force 可覆盖）")
    print(f"✅ 拆段完成: {len(args.sources)} 个源文件 | {mode} | 段号到 {seg_no:03d} | 输出: {seg_dir}")
    if total_conflicts:
        print(
            f"❌ {total_conflicts} 个段文件同名但内容不同——源文件已变更或分卷章号重名，未覆盖。"
            f"请确认后用 --force 覆盖，或检查源文件/分卷配置。"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
