#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词确定性编译器（工单 6，设计方案 §7）。

- `compile_prompt`：把 `PromptIR` 的九分区按固定顺序渲染成文本，**相同输入两次编译逐字节一致**；
  元数据落 `input_fingerprint`、规则来源（按优先级排序）、编译器版本。
- `validate_prompt`：机器判定风格冲突（复用美术总则冲突轴）与「无证据破损」，基础提示词不得混入叙事动作/站位/镜头。
- `shadow_report`：只写新旧差异，**永不回写正式产物**。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .art_constitution import CONFLICT_AXES, load_constitution, precedence_ranks
from .asset_ledger import TYPE_KEY, load_ledger
from .prompt_ir import (
    BASE_FORBIDDEN_HINTS,
    DAMAGE_WORDS,
    SECTION_ORDER,
    PromptIR,
    build_prompt_ir,
)

COMPILER_VERSION = "1.0.0"
_NON_AFFIRMATIVE = ("禁止项", "证据元数据")
SHADOW_REPORT_NAME = "shadow-提示词差异.md"
_OFFICIAL_DIR = ("提示词", "基础")
_LEDGER_TYPE = {v: k for k, v in TYPE_KEY.items()}
_IMPORTANCE_TO_PRIORITY = {"main": "P0", "supporting": "P1", "minor": "P2"}


@dataclass(frozen=True)
class CompiledPrompt:
    text: str
    negative: str
    metadata: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class PromptIssue:
    code: str
    section: str
    message: str


def _canonical(ir: PromptIR) -> str:
    payload = {
        "asset_id": ir.asset_id, "name": ir.name, "asset_type": ir.asset_type,
        "is_narrative": ir.is_narrative, "variant": ir.variant, "views": list(ir.views),
        "sections": [[k, v] for k, v in ir.ordered_sections()],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def input_fingerprint(ir: PromptIR) -> str:
    return "sha256:" + hashlib.sha256(_canonical(ir).encode("utf-8")).hexdigest()[:16]


def _ordered_rule_sources(ir: PromptIR) -> list[str]:
    ranks = precedence_ranks()
    used = {src for src in ir.rule_sources.values() if src}
    return sorted(used, key=lambda s: (ranks.get(s, 99), s))


def compile_prompt(ir: PromptIR, *, target: str = "reverse") -> CompiledPrompt:
    """确定性渲染：分区顺序固定，负面词单列，不含时间戳等任何非确定量。"""
    body = "\n\n".join(f"【{name}】\n{content}".rstrip() for name, content in ir.ordered_sections())
    sections = dict(ir.ordered_sections())
    negative = sections.get("禁止项", "")
    metadata = {
        "input_fingerprint": input_fingerprint(ir),
        "compiler_version": COMPILER_VERSION,
        "rule_sources": _ordered_rule_sources(ir),
        "sections": ir.ordered_sections(),
        "is_narrative": ir.is_narrative,
        "asset_type": ir.asset_type,
        "target": target,
    }
    return CompiledPrompt(text=body, negative=negative, metadata=metadata)


def _affirmative(sections: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in sections.items() if k not in _NON_AFFIRMATIVE}


def _conflicts_in(text: str) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    for axis, lefts, rights in CONFLICT_AXES:
        a = next((w for w in lefts if w and w in text), "")
        b = next((w for w in rights if w and w in text), "")
        if a and b:
            hits.append((axis, a, b))
    return hits


def validate_prompt(prompt: CompiledPrompt) -> list[PromptIssue]:
    """机器校验：只报事实与规则结论，不做主观评分。"""
    issues: list[PromptIssue] = []
    raw = dict(prompt.metadata.get("sections") or ())
    if not raw:
        raw = {"请求正文": prompt.text}
    for key in SECTION_ORDER:
        if key not in raw:
            issues.append(PromptIssue("MISSING_SECTION", key, f"缺少固定分区「{key}」"))

    affirmative = _affirmative(raw)
    evidence = raw.get("原文事实", "")

    # 1) 风格冲突：正向区内不得同时出现相斥表达（禁止项/证据元数据不参与）
    joined = "\n".join(affirmative.values())
    for axis, a, b in _conflicts_in(joined):
        owner = next((k for k, v in affirmative.items() if a in v and b in v), "风格")
        issues.append(PromptIssue("STYLE_CONFLICT", owner, f"{axis} 冲突：「{a}」与「{b}」不可共存"))

    # 2) 无证据破损：破损类设定必须能在原文事实里找到出处
    for key, value in affirmative.items():
        if key == "原文事实":
            continue
        for word in DAMAGE_WORDS:
            if word in value and word not in evidence:
                issues.append(PromptIssue("UNEVIDENCED_DAMAGE", key,
                                          f"「{word}」缺少原文证据，默认服装应完整合体整洁"))

    # 3) 基础提示词只描述资产，叙事元素属于概念图
    if not prompt.metadata.get("is_narrative"):
        for hint in BASE_FORBIDDEN_HINTS:
            for key, value in affirmative.items():
                if key == "原文事实":
                    continue
                if hint in value:
                    issues.append(PromptIssue("NARRATIVE_IN_BASE", key,
                                              f"基础提示词不得含叙事元素「{hint}」"))
    return issues


def shadow_report(asset_id: str, *, official_text: str, new_text: str) -> str:
    """shadow 模式差异报告：只读比较，正式产物未改写（不覆盖正式）。"""
    old_lines = (official_text or "").splitlines()
    new_lines = (new_text or "").splitlines()
    old_only = [ln for ln in old_lines if ln and ln not in new_lines]
    new_only = [ln for ln in new_lines if ln and ln not in old_lines]
    same = not old_only and not new_only
    parts = [
        f"## {asset_id}",
        f"结论：{'一致' if same else '存在差异'}；正式产物未改写（shadow 只写差异报告，不覆盖正式）",
        "",
        "### 旧（正式产物）",
        official_text.rstrip() or "（无正式产物）",
        "",
        "### 新（确定性编译）",
        new_text.rstrip(),
        "",
        "### 差异",
        ("仅旧有：" + " ⧸ ".join(old_only) if old_only else "仅旧有：无"),
        ("仅新有：" + " ⧸ ".join(new_only) if new_only else "仅新有：无"),
    ]
    return "\n".join(parts) + "\n"


def _ledger_asset(entry: Mapping) -> dict:
    """总账条目 → build_prompt_ir 需要的资产视图（只读派生，不改总账）。"""
    return {
        "asset_id": str(entry.get("asset_id") or "").strip(),
        "name": str(entry.get("canonical_name") or "").strip(),
        "type": _LEDGER_TYPE.get(entry.get("asset_type"), ""),
        "priority": _IMPORTANCE_TO_PRIORITY.get(entry.get("narrative_importance"), "P2"),
        "stable_key": str(entry.get("stable_key") or "").strip(),
        "fact_level": str(entry.get("fact_level") or "").strip(),
    }


def _evidence_by_asset(lib) -> dict:
    """从资产索引读回每个资产的证据摘句（总账只存证据 ID，不存正文）。"""
    path = Path(lib) / ".process" / "资产索引.json"
    if not path.is_file():
        return {}
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"资产索引不可读，shadow 无法取回原文证据：{exc}") from exc
    out = {}
    for asset in index.get("assets") or []:
        aid = str(asset.get("asset_id") or "").strip()
        if aid:
            out[aid] = list(asset.get("evidence") or [])
    return out


def read_official_base_prompts(lib) -> str:
    """只读拼接 `提示词/基础` 下的正式产物，作为 shadow 的「旧」一侧。"""
    base = Path(lib).joinpath(*_OFFICIAL_DIR)
    if not base.is_dir():
        return ""
    blocks = [f"--- 文件：{p.name} ---\n{p.read_text(encoding='utf-8')}"
              for p in sorted(base.glob("*.md"))]
    return "\n\n".join(blocks)


def render_corpus_shadow(official_text: str, compiled: Mapping[str, str]) -> str:
    """整库 shadow 报告：旧正式产物与新确定性编译并列，逐资产列出新正文。

    旧侧是合并 Markdown，无法可靠切回单资产，因此整库单列而不是伪造逐资产配对。
    """
    new_lines = sum(len(text.splitlines()) for text in compiled.values())
    lines = [
        "# shadow 提示词差异报告（不覆盖正式）",
        "",
        "> shadow 只写差异报告，**不覆盖正式产物**：本模式不改写 `提示词/` 下任何文件。",
        "> 旧侧是合并 Markdown 正式产物，无法可靠切回单资产，故按整库正文单列供人工核对。",
        "",
        f"- 新编译资产数：{len(compiled)}",
        f"- 正式产物行数：{len(official_text.splitlines())}",
        f"- 新编译行数：{new_lines}",
        "",
        "## 旧（正式产物全文）",
        "",
        official_text.rstrip() or "（无正式产物）",
        "",
        "## 新（确定性编译，逐资产）",
        "",
    ]
    for aid in sorted(compiled):
        lines += [f"### {aid}", "", compiled[aid].rstrip(), ""]
    return "\n".join(lines)


def run_shadow(lib) -> str:
    """shadow 模式：同一总账同时产旧（正式产物）与新（确定性编译），写差异报告但不覆盖正式。"""
    lib_path = Path(lib)
    ledger = load_ledger(lib_path)
    constitution = load_constitution(lib_path)
    evidence = _evidence_by_asset(lib_path)

    compiled: dict = {}
    for entry in ledger:
        if "base_prompt" not in (entry.get("required_outputs") or []):
            continue
        asset = _ledger_asset(entry)
        if not asset["asset_id"]:
            continue
        ir = build_prompt_ir(asset, evidence.get(asset["asset_id"]), constitution)
        compiled[asset["asset_id"]] = compile_prompt(ir, target="reverse").text

    report = render_corpus_shadow(read_official_base_prompts(lib_path), compiled)
    out = lib_path / ".process" / SHADOW_REPORT_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n", encoding="utf-8")
    return str(out)


# --- 并入正式提示词（工单 13：显式 --apply 才走这条路） ------------------------------

# 正式产物有固定节格式 `## NNN. 名称`；JSON 节点用完全相同的匹配规则
_SECTION_RE = re.compile(r"^## (\d{3})\. (.+?)\s*$", re.M)
# 只有这三类有正式基础提示词文件；服装等类型没有对应文件，一律显式报告而不是另建格式
_PROMPT_FILE_BY_TYPE = {"人物": "人物提示词.md", "场景": "场景提示词.md", "道具": "道具提示词.md"}
BACKUP_DIRNAME = "提示词备份"


def compile_library(lib) -> dict:
    """按总账编译全部需要基础提示词的资产；返回 `{资产ID: (类型, 名称, 正文)}`。

    单个资产编译失败不吞：记进 `errors`，由调用方决定是否整体拒绝。
    """
    lib_path = Path(lib)
    ledger = load_ledger(lib_path)
    constitution = load_constitution(lib_path)
    evidence = _evidence_by_asset(lib_path)
    compiled: dict = {}
    errors: list = []
    for entry in ledger:
        if "base_prompt" not in (entry.get("required_outputs") or []):
            continue
        asset = _ledger_asset(entry)
        if not asset["asset_id"]:
            continue
        try:
            ir = build_prompt_ir(asset, evidence.get(asset["asset_id"]), constitution)
            compiled[asset["asset_id"]] = (asset["type"], asset["name"],
                                           compile_prompt(ir, target="reverse").text)
        except Exception as exc:  # noqa: BLE001 - 编译失败必须可见，不得静默少一条
            errors.append(f"{asset['asset_id']} 编译失败：{type(exc).__name__}: {exc}")
    return {"compiled": compiled, "errors": errors}


def _split_sections(text: str) -> list:
    """切出 `## NNN. 名称` 节：[(start, body_start, number, title, body)]。"""
    marks = list(_SECTION_RE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.start(), m.end(), m.group(1), m.group(2), text[m.end():end]))
    return out


def apply_compiled(lib, *, backup: bool = True) -> dict:
    """把编译结果并入 `提示词/基础/*.md`（工单 13）。

    安全边界：**只改已存在节的正文**并保留编号；缺节才按最大号追加；
    总账里没有的旧节一律原样保留并列入 `orphans`——绝不静默删除用户内容。
    内容未变化的文件不重写（幂等），因此重复执行不会制造无意义的差异。
    """
    from datetime import datetime, timezone

    lib_path = Path(lib)
    result = compile_library(lib_path)
    compiled, errors = result["compiled"], list(result["errors"])

    groups: dict = {}
    unsupported: list = []
    for asset_id, (asset_type, name, body) in compiled.items():
        filename = _PROMPT_FILE_BY_TYPE.get(asset_type)
        if filename is None:
            unsupported.append(f"{asset_id}（{asset_type}）无正式基础提示词文件，未并入")
            continue
        groups.setdefault(filename, []).append((asset_id, name, body))

    backup_dir = ""
    updated = added = 0
    files: list = []
    for filename in sorted(groups):
        path = lib_path.joinpath(*_OFFICIAL_DIR) / filename
        before = path.read_text(encoding="utf-8") if path.is_file() else ""
        sections = _split_sections(before)
        header = before[:sections[0][0]] if sections else before
        consumed: set = set()
        rebuilt: list = []
        for start, body_start, number, title, body in sections:
            hit = next(((aid, nm, bd) for aid, nm, bd in groups[filename]
                        if nm and (nm in title or title in nm)), None)
            if hit is None:
                rebuilt.append(before[start:body_start] + body)
                continue
            consumed.add(hit[0])
            new_body = f"\n\n{hit[2].rstrip()}\n"
            if body != new_body:
                updated += 1
            rebuilt.append(before[start:body_start] + new_body)
        # 未消费的编译结果 → 追加新节（编号接着最大号往后排）
        numbers = [int(s[2]) for s in sections]
        next_no = (max(numbers) + 1) if numbers else 1
        for aid, nm, bd in groups[filename]:
            if aid in consumed:
                continue
            rebuilt.append(f"## {next_no:03d}. {nm}\n\n{bd.rstrip()}\n")
            next_no += 1
            added += 1
        text = header + "".join(rebuilt)
        if text == before:
            continue
        if backup and before:
            if not backup_dir:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                target = lib_path / ".process" / BACKUP_DIRNAME / stamp
                target.mkdir(parents=True, exist_ok=True)
                backup_dir = str(target)
            (Path(backup_dir) / filename).write_bytes(before.encode("utf-8"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        files.append(str(path))

    orphans: list = []
    for filename in sorted(groups):
        path = lib_path.joinpath(*_OFFICIAL_DIR) / filename
        if not path.is_file():
            continue
        for _s, _b, _n, title, _body in _split_sections(path.read_text(encoding="utf-8")):
            if not any(nm and (nm in title or title in nm) for _a, nm, _bd in groups[filename]):
                orphans.append(f"{filename}#{title}")
    return {"updated": updated, "added": added, "files": files, "orphans": orphans,
            "backup": backup_dir, "errors": errors + unsupported}
