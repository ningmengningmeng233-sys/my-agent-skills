"""正式合并与实验版隔离（v4 spec §13 / §11 / §5.2 / C-080~C-087）。

- 只读 manifest 判定：只选择 state ∈ {approved, delivered} 的章节，绝不从文件名、
  frontmatter 或报告文字推断（R14 / C-082）；passed 状态必须经由 approve 才能合并；
- 完整哈希复核：合并前重新计算每个候选章节的 source/events/decisions/rules/output/
  checks 指纹并与 manifest 记录比对，任一项不一致整次合并失败，旧正式输出保留
  （R16 / R17 / C-085 / F-018）；
- 禁止风险/缺章/旧原文混入：带 `experimental_risk` 的章节不得合并；候选章节序号
  必须连续（C-084）；合并前对每份 output.md 做正文纯度复查（C-083 / F-001 / F-002）；
- 原子写：``output/localized.md`` 通过临时文件原子替换；任一步失败不覆盖旧输出；
  状态更新由 ``ManifestStore.update`` 持项目锁完成；
- 实验版隔离：``write_experimental`` 只写 ``output/experimental/`` 独立路径并在
  manifest 登记 `experimental_output`/`experimental_risk`，永不参与正式合并（R18 / C-087）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from . import checks, fingerprints
from .audit import append_audit, now_iso
from .hashing import sha256_file
from .jsonio import read_json, write_text_atomic
from .manifest import ManifestStore
from .state_machine import require_transition


def _dedupe_repeated_opening_title(text: str) -> str:
    """合并前防御：标题行下方若重复出现同一标题文本，只保留一次。

    审查报告 P1：部分章节 output.md 出现「# 第二卷 X章」后正文首段又写一遍
    「第二卷 X章」，合并产物重复 9 处。此处只剥离完全相同的连续重复标题。
    """
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if len(paragraphs) < 2:
        return text
    first = paragraphs[0]
    heading = re.match(r"^#{1,6}\s+(.+)$", first)
    title = heading.group(1).strip() if heading else first.strip()
    second = re.sub(r"^#{1,6}\s+", "", paragraphs[1]).strip()
    if second and second == title:
        kept = [first] + paragraphs[2:]
        return "\n\n".join(kept) + ("\n" if text.endswith("\n") else "")
    return text


def _find_chapter(manifest: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    chapter = next(
        (item for item in manifest["chapters"] if item["id"] == chapter_id), None
    )
    if chapter is None:
        raise KeyError(f"章节不存在: {chapter_id}")
    return chapter


def _verify_chapter_hashes(
    workspace: Path, chapter: dict[str, Any], manifest: dict[str, Any]
) -> list[str]:
    """完整哈希复核：当前文件指纹必须与 manifest 记录逐项一致。"""
    recorded = chapter.get("fingerprints", {})
    current = {
        "source": fingerprints.hash_optional_file(workspace / chapter["paths"]["source"]),
        "events": fingerprints.hash_optional_file(workspace / chapter["paths"]["events"]),
        "project_decisions": fingerprints.hash_optional_file(
            workspace / manifest["project_decisions"]["path"]
        ),
        "chapter_decisions": fingerprints.hash_optional_file(
            workspace / chapter["paths"]["decisions"]
        ),
        "rules": fingerprints.rules_fingerprint(),
        "output": fingerprints.hash_optional_file(workspace / chapter["paths"]["output"]),
        "checks": fingerprints.hash_optional_file(workspace / chapter["paths"]["checks"]),
    }
    issues: list[str] = []
    for key in (
        "source", "events", "project_decisions", "chapter_decisions",
        "rules", "output", "checks",
    ):
        if recorded.get(key) != current[key]:
            issues.append(
                f"{key} 指纹不一致（记录 {str(recorded.get(key))[:12]}…，"
                f"当前 {current[key][:12]}…）"
            )
    return issues


def _approved_version_text(workspace: Path, chapter: dict[str, Any]) -> str:
    """读取章节批准版本的锁定正文（v4.5.0-dev.3 Task 5.3）。

    - 只信 ``current.json`` 的 ``approved_version`` 指针，绝不读未锁定的 output.md 草稿；
    - 版本正文实测哈希必须与 ``approved_sha256`` 一致（批准后文件被篡改即失败）。
    """
    chapter_dir = (workspace / chapter["paths"]["output"]).parent
    current_path = chapter_dir / "current.json"
    if not current_path.is_file():
        raise FileNotFoundError(
            f"{chapter['id']}: 缺少批准指针（current.json），请先 novel approve"
        )
    current = read_json(current_path)
    number = int(current.get("approved_version") or 0)
    if number <= 0:
        raise ValueError(f"{chapter['id']}: 无批准版本指针，请先 novel approve")
    path = chapter_dir / "versions" / f"v{number:03d}.md"
    if not path.is_file():
        raise FileNotFoundError(f"{chapter['id']}: 批准版本文件缺失: {path.name}")
    text = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != current.get("approved_sha256"):
        raise ValueError(
            f"{chapter['id']}: 批准版本 v{number:03d} 正文哈希与批准指针不一致，"
            "版本可能在批准后被篡改"
        )
    return text


def merge_output(
    workspace: Path | str, *, allow_incomplete: bool = False
) -> dict[str, Any]:
    """正式合并 passed/delivered 章节到 ``output/localized.md``。

    ``allow_incomplete=False``（默认）：只要存在任何不可合并章节即整体拒绝；
    ``allow_incomplete=True``：允许合并候选前缀（1..k），缺失章节在报告中明确列出，
    绝不静默跳过（C-081）。候选章节序号必须连续，否则合并失败（C-084）。
    """
    workspace = Path(workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    sanitized = store.sanitize_tampered_states()
    if sanitized:
        manifest = store.load()
    history_issues = store.validate_state_history(manifest)
    report: dict[str, Any] = {
        "ok": False,
        "merged": [],
        "excluded": [],
        "issues": [],
        "output_sha256": None,
        "manifest_revision": None,
    }

    chapters = manifest["chapters"]
    if history_issues:
        report["issues"].extend(history_issues)
        return report
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for chapter in chapters:
        if chapter["state"] in {"approved", "delivered"}:
            if chapter.get("experimental_risk"):
                excluded.append(
                    {
                        "id": chapter["id"],
                        "reason": "带风险版（experimental_risk）不得并入正式成品",
                    }
                )
            else:
                candidates.append(chapter)
        else:
            reason = f"state={chapter['state']} 不可合并"
            if chapter["state"] == "passed":
                reason += "（草稿需先经由 novel approve 审批通过）"
            excluded.append(
                {"id": chapter["id"], "reason": reason}
            )
    if not candidates:
        report["excluded"] = excluded
        if chapters:
            report["issues"].append(
                "没有可合并章节，全部被排除："
                + "；".join(f"{item['id']}({item['reason']})" for item in excluded[:10])
            )
        else:
            report["issues"].append("工作区无任何章节")
        return report
    if excluded and not allow_incomplete:
        report["excluded"] = excluded
        report["issues"].append(
            "存在不可合并章节：" + "、".join(item["id"] for item in excluded[:10])
            + "；正式合并拒绝（allow_incomplete=False）"
        )
        return report

    # 章节序号连续性（C-084）
    ordinals = sorted(chapter["ordinal"] for chapter in candidates)
    if ordinals != list(range(1, len(ordinals) + 1)):
        gaps = sorted(set(range(1, ordinals[-1] + 1)) - set(ordinals))
        report["issues"].append(f"章节序号缺口：{gaps[:20]}；正式合并拒绝")
        return report

    # 完整哈希复核 + output 存在性 + 合并前纯度复查
    for chapter in candidates:
        if chapter.get("checks", {}).get("hard_gate") != "pass":
            report["issues"].append(f"{chapter['id']}: checks.hard_gate 不是 pass")
        if chapter.get("checks", {}).get("event_comparison") != "pass":
            report["issues"].append(f"{chapter['id']}: checks.event_comparison 不是 pass")
        report["issues"].extend(
            f"{chapter['id']}: {issue}"
            for issue in _verify_chapter_hashes(workspace, chapter, manifest)
        )
        output_path = workspace / chapter["paths"]["output"]
        if not output_path.is_file():
            report["issues"].append(f"{chapter['id']}: output.md 缺失")
            continue
        purity = checks.purity_issues(output_path.read_text(encoding="utf-8"))
        if purity:
            report["issues"].append(
                f"{chapter['id']}: 合并前纯度复查失败：{'；'.join(purity[:5])}"
            )
        # v4.5.0-dev.3：批准指针与版本文件完整性核验（Task 5.3）
        try:
            _approved_version_text(workspace, chapter)
        except (OSError, ValueError) as exc:
            report["issues"].append(str(exc))
    if report["issues"]:
        return report

    # 只拼接 approved_version 的章节标题+正文（spec §13 step 7 / Task 5.3）
    ordered = sorted(candidates, key=lambda chapter: chapter["ordinal"])
    parts: list[str] = []
    for chapter in ordered:
        text = _approved_version_text(workspace, chapter)
        text = _dedupe_repeated_opening_title(text)
        if chapter.get("kind") == "appendix" and parts:
            parts.append("---")
        parts.append(text.strip("\n"))
    localized = "\n\n".join(parts) + "\n"
    out_path = workspace / manifest["output"]["path"]
    # 交付完整性：成品文件与 manifest 记录必须成对生效。先记下旧状态，若随后
    # manifest 写入失败则回滚文件——否则会留下「成品已是新内容、manifest 未记录」
    # 的孤儿交付物（按 manifest 校验成品会失败，也无从判断它属于哪次合并）。
    previous_exists = out_path.is_file()
    previous_text = out_path.read_text(encoding="utf-8") if previous_exists else None
    write_text_atomic(out_path, localized)

    def mutate(updated: dict[str, Any]) -> None:
        candidate_ids = {chapter["id"] for chapter in ordered}
        for chapter in updated["chapters"]:
            if chapter["id"] not in candidate_ids:
                continue
            if chapter["state"] == "approved":
                require_transition("approved", "delivered")
                chapter["state"] = "delivered"
            chapter["delivery_revision"] = int(chapter.get("delivery_revision", 0)) + 1
            chapter["updated_at"] = now_iso()
        updated["output"]["sha256"] = sha256_file(out_path)
        # 下一次 manifest revision 即为本次合并的版本
        updated["output"]["last_built_from_revision"] = (
            updated["manifest_revision"] + 1
        )

    try:
        updated = store.update(mutate, event="merge_success")
    except Exception:
        # manifest 未生效 → 把成品文件回滚到合并前状态，避免留下孤儿交付物
        try:
            if previous_exists and previous_text is not None:
                write_text_atomic(out_path, previous_text)
            else:
                out_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    append_audit(
        store.audit_path,
        "merge_detail",
        chapters=[chapter["id"] for chapter in ordered],
        output_path=updated["output"]["path"],
        output_sha256=updated["output"]["sha256"],
        last_built_from_revision=updated["output"]["last_built_from_revision"],
        manifest_revision=updated["manifest_revision"],
        allow_incomplete=allow_incomplete,
    )
    report.update(
        ok=True,
        merged=[chapter["id"] for chapter in ordered],
        excluded=excluded,
        output_sha256=updated["output"]["sha256"],
        manifest_revision=updated["manifest_revision"],
    )
    return report


def write_experimental(
    workspace: Path | str, chapter_id: str, content: str, risk_reason: str
) -> Path:
    """带风险版写入独立实验路径并登记 manifest，永不覆盖正式输出。

    - 只写 ``output/experimental/<chapter_id>.md``；
    - manifest 章节记录 `experimental_output` 与 `experimental_risk`；
    - 不改变章节状态，不参与正式合并（R18 / C-087）。
    """
    workspace = Path(workspace)
    if not isinstance(risk_reason, str) or not risk_reason.strip():
        raise ValueError("实验版必须登记风险原因（experimental_risk）")
    purity = checks.purity_issues(content)
    if purity:
        raise ValueError("实验版正文同样不得混入内部元数据：" + "；".join(purity[:5]))
    store = ManifestStore(workspace)
    manifest = store.load()
    _find_chapter(manifest, chapter_id)  # 校验章节存在（不存在则抛错）
    relative = f"output/experimental/{chapter_id}.md"
    path = workspace / relative
    # 与 merge_output 同一交付完整性约定：文件与 manifest 登记必须成对生效。
    # 先记旧状态，登记失败则回滚文件，避免留下未登记的孤儿实验版。
    previous_exists = path.is_file()
    previous_text = path.read_text(encoding="utf-8") if previous_exists else None
    write_text_atomic(path, content)

    def mutate(updated: dict[str, Any]) -> None:
        current = next(
            item for item in updated["chapters"] if item["id"] == chapter_id
        )
        current["experimental_output"] = relative
        current["experimental_risk"] = risk_reason
        current["updated_at"] = now_iso()

    try:
        updated = store.update(mutate, event="experimental_output_written")
    except Exception:
        try:
            if previous_exists and previous_text is not None:
                write_text_atomic(path, previous_text)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    append_audit(
        store.audit_path,
        "experimental_detail",
        chapter_id=chapter_id,
        output_path=relative,
        risk=risk_reason,
        manifest_revision=updated["manifest_revision"],
    )
    return path
