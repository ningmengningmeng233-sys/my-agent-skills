"""全书级一致性旁车检查（只读，不修改任何章节状态）。

检查维度：
- 章节顺序与标题连续性（含章节内重复标题防御检查）；
- 项目级译名决策在全书的落实情况（残留 / 译名漂移）；
- 词典 must_replace 残留的跨章汇总；
- 台版腔/台版用词/繁体残留（v4/rules/tone-scan.json）；
- 章末钩子启发式覆盖；
- 输出文件存在性与格式纯度。

结果写入 ``workspace/consistency.json`` 与审计；命令退出码：
0 = 无 error 级问题；1 = 存在 error 级问题。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import checks
from . import localization_tone
from .audit import now_iso
from .jsonio import read_json
from .manifest import ManifestStore


@dataclass
class ConsistencyIssue:
    code: str
    severity: str  # error | warning
    chapter_id: str
    message: str
    evidence: str = ""


def _accepted_mappings(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in doc.get("entries", [])
        if isinstance(entry, dict)
        and entry.get("status") == "accepted"
        and entry.get("category") == "mapping"
    ]


def check_book_consistency(
    workspace: Path | str, manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    """全书一致性检查；返回 {schema_version, generated_at, issues, summary}。"""
    workspace = Path(workspace)
    manifest = manifest or ManifestStore(workspace).load()
    issues: list[ConsistencyIssue] = []

    project_decisions = read_json(workspace / manifest["project_decisions"]["path"])
    accepted = _accepted_mappings(project_decisions)

    chapters = sorted(manifest.get("chapters", []), key=lambda c: c.get("ordinal", 1))
    titles: list[tuple[str, str]] = []
    for chapter in chapters:
        cid = chapter.get("id", "")
        output_path = workspace / chapter["paths"]["output"]
        if not output_path.is_file():
            issues.append(
                ConsistencyIssue("MISSING_OUTPUT", "error", cid, "output.md 缺失")
            )
            continue
        output_text = output_path.read_text(encoding="utf-8")
        titles.append((cid, output_text.splitlines()[0] if output_text else ""))

        # 章节内重复标题：标题行下方正文首段不得再重复一遍同一标题
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", output_text) if part.strip()]
        if len(paragraphs) >= 2:
            first = paragraphs[0]
            heading = re.match(r"^#{1,6}\s+(.+)$", first)
            title_text = heading.group(1).strip() if heading else first
            second_text = re.sub(r"^#{1,6}\s+", "", paragraphs[1]).strip()
            if second_text and second_text == title_text:
                issues.append(
                    ConsistencyIssue(
                        "DUPLICATE_OPENING_TITLE",
                        "error",
                        cid,
                        f"章节标题「{title_text[:40]}」在正文首段重复出现（合并前应只保留一次）",
                    )
                )

        # 台版腔/台版用词/繁体残留
        tone_report = localization_tone.scan_tone(output_text)
        for finding in tone_report.hard[:10]:
            issues.append(
                ConsistencyIssue(
                    "TONE_HARD",
                    "error",
                    cid,
                    f"台翻腔/日式残留：{finding.message}",
                    f"{finding.match} …{finding.context[:60]}",
                )
            )
        for finding in tone_report.blocked[:5]:
            issues.append(
                ConsistencyIssue(
                    "TONE_DENSITY",
                    "warning",
                    cid,
                    finding.message,
                    finding.context,
                )
            )

        # 章末钩子启发式（仅正文章节；warning，不自动判死）
        if chapter.get("kind") != "appendix":
            hook = localization_tone.hook_signal_check(output_text)
            if hook.result == "warning":
                issues.append(
                    ConsistencyIssue(
                        "CHAPTER_HOOK_WEAK",
                        "warning",
                        cid,
                        hook.message,
                    )
                )

        # 标题连续性：正文首行应包含章节标题（或为空标题时允许）
        title = chapter.get("title", "")
        if title and title not in output_text:
            issues.append(
                ConsistencyIssue(
                    "TITLE_MISSING",
                    "warning",
                    cid,
                    f"正文未包含章节标题「{title}」",
                )
            )

        # 项目译名决策落实
        for entry in accepted:
            target = str(entry.get("target", ""))
            choice = str(entry.get("choice", ""))
            if not target:
                continue
            if checks.has_residue(target, output_text) and choice not in {
                "保留原文", "保留原名", "保留原词", "保留功能", "保留", "不替换",
            }:
                issues.append(
                    ConsistencyIssue(
                        "MAPPING_RESIDUE",
                        "error",
                        cid,
                        f"已接受译名决策「{target}→{choice}」未落实，正文仍含「{target}」",
                    )
                )
            if (
                choice
                and choice not in {"删除"}
                and choice not in {
                    "保留原文", "保留原名", "保留原词", "保留功能", "保留", "不替换",
                }
                and choice not in output_text
                and target in output_text
            ):
                # target 存在但目标译名不存在，属于漂移；上面已报残留，这里不再重复
                pass

    # 标题唯一性
    seen_titles: dict[str, str] = {}
    for cid, first_line in titles:
        if not first_line:
            continue
        if first_line in seen_titles:
            issues.append(
                ConsistencyIssue(
                    "DUPLICATE_TITLE",
                    "error",
                    cid,
                    f"章节标题「{first_line}」与 {seen_titles[first_line]} 重复",
                )
            )
        else:
            seen_titles[first_line] = cid

    # 章节序号连续性
    ordinals = [c.get("ordinal") for c in chapters]
    if ordinals != list(range(1, len(ordinals) + 1)):
        issues.append(
            ConsistencyIssue(
                "ORDINAL_GAP",
                "error",
                "",
                f"章节序号不连续: {ordinals}",
            )
        )

    # 词典 must_replace 残留跨章汇总
    rules = checks.load_rules_tiers()
    for token in rules.get("must_replace", []):
        source = (
            token.get("source")
            if isinstance(token, dict)
            else token
        )
        if not isinstance(source, str) or not source:
            continue
        for chapter in chapters:
            cid = chapter.get("id", "")
            output_path = workspace / chapter["paths"]["output"]
            if not output_path.is_file():
                continue
            text = output_path.read_text(encoding="utf-8")
            if checks.has_residue(source, text) and not any(
                entry.get("target") == source
                and entry.get("status") == "accepted"
                for entry in accepted
            ):
                issues.append(
                    ConsistencyIssue(
                        "DICTIONARY_RESIDUE",
                        "error",
                        cid,
                        f"必替换词「{source}」残留且无用户保留决策",
                    )
                )

    error_count = sum(1 for i in issues if i.severity == "error")
    warning_count = len(issues) - error_count
    return {
        "schema_version": "v4-consistency-1",
        "generated_at": now_iso(),
        "issues": [asdict(i) for i in issues],
        "summary": {
            "chapter_count": len(chapters),
            "error_count": error_count,
            "warning_count": warning_count,
        },
    }
