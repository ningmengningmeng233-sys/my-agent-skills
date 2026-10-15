#!/usr/bin/env python3
"""文风蒸馏：轻量风格卡生成与完整蒸馏扩展接口。"""
import argparse
import os
import re
import shlex
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

if __name__ == "__main__":  # 只有 CLI 直跑才改；被导入时不得动宿主 stdio（会吞掉测试框架的汇总行）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from _common import parse_frontmatter, read_text_auto, resolve_under
    from _logging import get_logger
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _common import parse_frontmatter, read_text_auto, resolve_under
    from _logging import get_logger

logger = get_logger("style_distillation")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_MAX_LIGHTWEIGHT_SEGMENTS = 10


def _safe_path(root: Path, relative: str) -> Path | None:
    resolved = resolve_under(str(root), relative)
    return Path(resolved) if resolved else None


def _extract_sections(text: str) -> dict[str, dict[str, Any]]:
    headings = []
    for m in _HEADING_RE.finditer(text):
        headings.append((len(m.group(1)), m.group(2).strip(), m.start(), m.end()))

    sections: dict[str, dict[str, Any]] = {}
    i = 0
    n = len(headings)
    while i < n:
        lvl, title, _, body_start = headings[i]
        if lvl != 2:
            i += 1
            continue
        next_i = i + 1
        section_end = headings[next_i][2] if next_i < n else len(text)
        subs: dict[str, str] = {}
        j = i + 1
        while j < n and headings[j][0] > lvl:
            if headings[j][0] == 3:
                sub_title = headings[j][1]
                k = j + 1
                while k < n and headings[k][0] > 3:
                    k += 1
                sub_end = headings[k][2] if k < n else section_end
                subs[sub_title] = text[headings[j][3]:sub_end].strip()
            j += 1
        sections[title] = {"body": text[body_start:section_end].strip(), "subs": subs}
        i = j
    return sections


def _bullet_lines(content: str) -> list[str]:
    lines = []
    for raw in content.splitlines():
        s = raw.strip()
        if s.startswith(("- ", "* ", "+ ")):
            lines.append(s[2:].strip())
        elif s.startswith("-") and len(s) > 1 and s[1] != "-":
            lines.append(s[1:].strip())
    return [ln for ln in lines if ln]


def _first_line(content: str) -> str:
    for line in content.splitlines():
        s = line.strip()
        if s:
            return s
    return ""


def _sample_segments(paths: list[Path]) -> tuple[list[Path], int]:
    total = len(paths)
    if total <= _MAX_LIGHTWEIGHT_SEGMENTS:
        return paths, total

    scored: list[tuple[Path, int]] = []
    for p in paths:
        score = 0
        try:
            meta, _ = parse_frontmatter(read_text_auto(str(p)))
        except Exception:
            meta = {}
        tags = " ".join(meta.get("包含元素") or [])
        if "金句" in tags or "名台词" in tags:
            score += 3
        if "爽点" in tags or "名场面" in tags:
            score += 2
        scored.append((p, score))

    scored.sort(key=lambda x: (-x[1], x[0].name))
    selected = {p for p, _ in scored[:_MAX_LIGHTWEIGHT_SEGMENTS]}
    selected.add(paths[0])
    selected.add(paths[-1])
    if len(selected) < _MAX_LIGHTWEIGHT_SEGMENTS:
        for p, _ in scored:
            selected.add(p)
            if len(selected) >= _MAX_LIGHTWEIGHT_SEGMENTS:
                break
    return sorted(selected), total


def _collect_material(lib: Path, novel: str, author: str) -> dict[str, Any]:
    key = f"{novel}-{author}"
    novel_path = _safe_path(lib, f"小说索引/{key}.md")
    chapter_dir = _safe_path(lib, "章节索引")

    novel_meta: dict[str, Any] = {}
    novel_sections: dict[str, dict[str, Any]] = {}
    if novel_path and novel_path.exists():
        meta, body = parse_frontmatter(read_text_auto(str(novel_path)))
        novel_meta = meta
        novel_sections = _extract_sections(body)
    else:
        logger.warning("未找到小说索引: %s", novel_path)

    chapter_paths: list[Path] = []
    if chapter_dir and chapter_dir.is_dir():
        chapter_paths = sorted(p for p in chapter_dir.glob(f"{key}-*-idx.md") if p.is_file())

    sampled, total = _sample_segments(chapter_paths)
    segments: list[dict[str, Any]] = []
    for p in sampled:
        try:
            meta, body = parse_frontmatter(read_text_auto(str(p)))
            segments.append({
                "path": p,
                "meta": meta,
                "sections": _extract_sections(body),
            })
        except Exception as e:
            logger.warning("读取章节索引失败 %s: %s", p, e)

    return {
        "novel_meta": novel_meta,
        "novel_sections": novel_sections,
        "segments": segments,
        "total_segments": total,
        "sampled_segments": len(sampled),
    }


def _render_card(material: dict[str, Any], novel: str, author: str) -> str:
    meta = material["novel_meta"]
    novel_sections = material["novel_sections"]
    segments = material["segments"]
    total = material["total_segments"]
    sampled = material["sampled_segments"]

    novel_name = meta.get("小说名称") or f"《{novel}》"
    author_name = meta.get("作者") or author
    index_range = meta.get("章节范围") or "未知"

    anchors: list[str] = []
    narrative: list[str] = []
    language: list[str] = []
    plot: list[str] = []
    character: list[str] = []
    motif: list[str] = []

    for seg in segments:
        for title, sec in seg["sections"].items():
            subs = sec.get("subs", {})
            is_chapter = "章" in title or title.startswith("第")
            if is_chapter:
                anchors.extend(_bullet_lines(subs.get("金句/名场面", "")))
                narrative.extend(_bullet_lines(subs.get("创作手法分析", "")))
                plot.extend(_bullet_lines(subs.get("爽点/看点", "")))
                motif.append(_first_line(subs.get("剧情概要", "")))
            elif title == "本段总结":
                narrative.extend(_bullet_lines(subs.get("情绪弧线", "")))
                plot.extend(_bullet_lines(subs.get("可迁移的创作手法", "")))
            elif title == "本段概况":
                motif.append(_first_line(sec.get("body", "")))

    highlights = novel_sections.get("全书亮点速查", {}).get("subs", {})
    plot.extend(_bullet_lines(highlights.get("爽点密集区", "")))
    motif.extend(_bullet_lines(highlights.get("名场面", "")))
    anchors.extend(_bullet_lines(highlights.get("金句梗", "")))

    if "人物关系链" in novel_sections:
        for line in novel_sections["人物关系链"]["body"].splitlines():
            s = line.strip()
            if s.startswith("|") and "人物" not in s and "-" not in s:
                character.append(s)

    today = date.today().isoformat()
    lines = [
        "---",
        f"作品: {novel_name}",
        f"作者: {author_name}",
        f"蒸馏日期: {today}",
        f"基于索引范围: {index_range}",
        f"抽样: {sampled}/{total} 段",
        "蒸馏方式: 轻量蒸馏",
        "完整版: 未生成（需配置 INDEX_STYLE_SKILL 调用外部 skill）",
        "---",
        "",
        f"# {novel_name} 轻量风格卡",
        "",
        "> 本卡由小说索引库材料自动提炼，仅反映抽样段落观察，不构成全书结论。",
        "",
        "## 叙事模型",
    ]
    lines.extend(f"- {x}" for x in (narrative[:8] or ["索引中未收录足够叙事模型描述"]))

    lines.extend(["", "## 语言指纹"])
    lines.extend(f"- {x}" for x in (anchors[:6] or ["索引中未收录足够金句/名场面"]))

    lines.extend(["", "## 母题意象"])
    lines.extend(f"- {x}" for x in (motif[:8] or ["索引中未收录足够母题意象"]))

    lines.extend(["", "## 人物塑造"])
    lines.extend(f"- {x}" for x in (character[:6] or ["索引中未收录足够人物关系描述"]))

    lines.extend(["", "## 情节技法"])
    lines.extend(f"- {x}" for x in (plot[:8] or ["索引中未收录足够情节技法"]))

    lines.extend(["", "## 不可写"])
    lines.extend([
        "- 不将抽样结论夸大为全书风格定论",
        "- 不虚构索引中未出现的原文、行号或创作手法",
        "- 不做段落级复述、情节复述或长文摘抄",
    ])

    lines.extend(["", "## 诚实边界"])
    lines.extend([
        f"- 基于章节索引抽样 {sampled}/{total} 段",
        f"- 小说索引覆盖范围：{index_range}",
        "- 轻量蒸馏只读取索引 metadata 与已索引小节，不扫描全文",
        "- 完整文风蒸馏需配置 INDEX_STYLE_SKILL 并显式调用外部 skill",
    ])

    return "\n".join(lines) + "\n"


def _run_lightweight(
    lib: Path,
    novel: str,
    author: str,
    output_dir: str | None,
    log: Any,
) -> dict[str, Any]:
    key = f"{novel}-{author}"
    if output_dir is None:
        card_dir = lib.parent / "风格卡" / key
    else:
        card_dir = Path(output_dir).resolve()

    card_dir.mkdir(parents=True, exist_ok=True)
    out_file = card_dir / "style-card.md"

    material = _collect_material(lib, novel, author)
    card = _render_card(material, novel, author)
    out_file.write_text(card, encoding="utf-8")
    log.info("轻量风格卡已生成: %s", out_file)
    return {"mode": "lightweight", "path": str(out_file)}


def _run_cmd(cmd: list[str], log: Any) -> dict[str, Any]:
    log.info("执行外部文风技能: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as e:
        log.error("外部文风技能启动失败: %s", e)
        return {"mode": "external", "error": str(e), "command": " ".join(cmd)}

    if result.returncode != 0:
        err = result.stderr.strip() or f"退出码 {result.returncode}"
        log.error("外部文风技能失败: %s", err)
        return {
            "mode": "external",
            "error": err,
            "command": " ".join(cmd),
            "returncode": result.returncode,
        }

    log.info("外部文风技能执行完成")
    return {"mode": "external", "returncode": result.returncode, "command": " ".join(cmd)}


def _run_external(
    lib: Path,
    novel: str,
    author: str,
    style_skill: str,
    log: Any,
) -> dict[str, Any]:
    skill_path = Path(style_skill)
    if skill_path.is_file() and skill_path.suffix == ".py":
        cmd = [
            sys.executable,
            str(skill_path.resolve()),
            "--lib", str(lib),
            "--novel", novel,
            "--author", author,
        ]
        return _run_cmd(cmd, log)

    if skill_path.is_dir():
        log.info("外部 skill 目录入口暂未实现: %s", skill_path)
        return {"mode": "external", "error": f"外部 skill 目录入口暂未实现: {skill_path}"}

    cmd = shlex.split(style_skill)
    return _run_cmd(cmd, log)


def run_style_distillation(
    lib_dir: str,
    novel: str,
    author: str,
    *,
    style_skill: str | None = None,
    output_dir: str | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    log = logger or get_logger(__name__)
    lib = Path(lib_dir).resolve()
    if not lib.is_dir():
        return {"mode": "error", "error": f"索引库不存在或不是目录: {lib}"}

    if style_skill is None:
        style_skill = os.environ.get("INDEX_STYLE_SKILL", "").strip() or None

    if style_skill:
        return _run_external(lib, novel, author, style_skill, log)
    return _run_lightweight(lib, novel, author, output_dir, log)


def main() -> int:
    parser = argparse.ArgumentParser(description="文风蒸馏接口")
    parser.add_argument("--lib", required=True, help="索引库根目录")
    parser.add_argument("--novel", required=True, help="小说名称")
    parser.add_argument("--author", required=True, help="作者名称")
    parser.add_argument("--style-skill", default=None, help="外部文风技能路径/命令")
    parser.add_argument("--output-dir", default=None, help="风格卡输出目录（默认在索引库内）")
    args = parser.parse_args()

    result = run_style_distillation(
        args.lib,
        args.novel,
        args.author,
        style_skill=args.style_skill,
        output_dir=args.output_dir,
        logger=logger,
    )
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return 1
    if result.get("path"):
        print(result["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
