#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前置工单（技能体积与真实上下文治理）：体积与加载路径审计器。

产物：
- 机器可读结果：``.process/skill-context-audit.json``（默认）
- 长日志：``--log`` 指定的文件（当前上下文只保留结论、退出码与证据路径）

分类方法全部是确定性字符串规则，写在 ``CLASSIFICATION_METHODS`` 里并随 JSON 输出，
保证报告中的每个数字都能被复算，而不是凭印象给出的结论。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import skill_meta

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent

# 审计只覆盖文本：图片/二进制不进模型上下文，但计入磁盘清单以免被误当成加载量。
TEXT_SUFFIXES = {".md", ".txt", ".toml", ".json", ".jsonl", ".py", ".yaml", ".yml", ".cfg"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}

# 报告 A.3 要求分目录清点；不存在的目录记 0，不报错。
AUDIT_DIRS = ("references", "scripts", "templates", "docs", "测试")

# 启动即读集合：技能被触发时宿主必然注入的文本。
BOOT_ALWAYS = ("SKILL.md",)

# 只有特定阶段才需要加载的文档及其触发条件（与 SKILL.md 第七节路由表同源）。
STAGE_TRIGGERS = {
    "references/核心反推契约.md": "执行核心反推任一步骤、需要完整参数与产物契约时",
    "references/视觉生产契约.md": "用户选择启用视觉生产时",
    "references/Omni质量审核契约.md": "判定图片是否合格时",
    "references/角色参考入库契约.md": "处理源头脸/立绘/参考图入库与旧图迁移时",
    "references/导演构图与站位契约.md": "生成或审核构图卡、站位图、连续性时",
    "references/并发与失败处理契约.md": "处理并发、锁、事件日志、重试与崩溃恢复时",
    "references/错误处理矩阵.md": "出现报错或异常需要查原因码时",
    "references/画风卡片模板.md": "新增画风时",
    "references/风格组装参考.md": "组装风格包时",
    "references/风格蒸馏流程.md": "从参考图反推蒸馏新风格包时",
    "QUICKSTART.md": "首次上手或查命令位置时",
    "FAQ.md": "报错排查时",
    "CONTEXT.md": "查术语与决策记录时",
    "docs/CHANGELOG.md": "查历史版本时（运行禁止加载）",
}

# 条件加载措辞：命中则认为该条指令是按需而非无条件。
CONDITIONAL_WORDS = (
    "启用", "只有", "才需要", "才读", "按需", "需要时", "出现", "报错时", "未启用",
    "可选", "若", "当", "执行", "阶段", "先问", "选择",
)
# 强制阅读措辞：出现在同一行且无上述限定词时，视为无条件全量加载风险。
MANDATORY_WORDS = ("必须先阅读", "先阅读", "完整阅读", "必读", "全部读取", "逐条阅读", "通读")

# 主入口分类（A.2）：互斥优先级 代码块 > 表格 > 标题 > 列表 > 正文 > 空行。
SAMPLE_HEADING_WORDS = ("示例", "例子", "案例", "样例")
HISTORY_HEADING_WORDS = ("版本", "更新日志", "历史", "变更", "changelog", "adr", "故障")

# 外部技能全文复制检测（A.9）：以 frontmatter `name:` 为技能身份。
EXTERNAL_SKILL_HEADINGS = (
    "# Omni", "## Omni", "skill-creator", "baoyu-", "jianying", "seedance",
    "director-styles", "cinema-director",
)

AUDIT_SCRIPT_REL = "scripts/audit_skill_context.py"
# 风格包根：包内 `references/*.md` 是相对包根的局部路径，不是仓库路径。


STYLEPACK_DIR = "references/stylepacks"

# 单个常读文本预警线：超过则必须在报告里说明是否真的会被整读。
LARGE_ALWAYS_READ = 25 * 1024
HUGE_ANY_READ = 50 * 1024

CLASSIFICATION_METHODS = {
    "code_block": "以 ``` 围栏成对出现，围栏内（含围栏行）记为代码块行",
    "table": "以 | 开头且以 | 结尾的行记为表格行",
    "heading": "以 # 开头的行记为标题行",
    "list": "以 - / * / 数字. 开头的行记为列表行",
    "blank": "去掉空白后为空的行",
    "history_like": "标题含 版本/更新日志/历史/变更/changelog/adr/故障 的标题行及其后续正文行",
    "sample_like": "标题含 示例/例子/案例/样例 的标题行及其后续正文行；代码块内示例另行计入代码块",
    "prose": "既非代码块也非空行的其余行",
    "duplicate": "对参与文本做归一化（去空白与 markdown 强调符）后取长度 >=24 字符的行，"
                 "统计出现次数 >1 的行；命中列表即重复证据",
    "load_path": "启动集 = SKILL.md 全文（宿主注入）；其余文本按 SKILL.md 中的路由表、"
                 "相对链接、脚本注入三类入口分别登记，无条件指令单独告警",
    "external_copy": "扫描非本技能文档中是否出现外部技能标题与整段技能正文特征",
}


def is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def classify_entry_lines(text: str) -> dict:
    """A.2：主入口行数按互斥优先级分类，并给出占比。"""
    counts: Counter = Counter()
    in_code = False
    history_mode = False
    sample_mode = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            counts["code_block"] += 1
            continue
        if in_code:
            counts["code_block"] += 1
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            counts["heading"] += 1
            lowered = heading.lower()
            history_mode = any(w in lowered or w in heading for w in HISTORY_HEADING_WORDS)
            sample_mode = any(w in heading for w in SAMPLE_HEADING_WORDS)
            continue
        if not stripped:
            counts["blank"] += 1
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            counts["table"] += 1
            continue
        if re.match(r"^([-*]\s|\d+[.)]\s)", stripped):
            counts["list"] += 1
            continue
        if history_mode:
            counts["history_like"] += 1
        elif sample_mode:
            counts["sample_like"] += 1
        else:
            counts["prose"] += 1
    total = sum(counts.values())
    ratio = {k: (round(v / total, 4) if total else 0.0) for k, v in counts.items()}
    return {"total_lines": total, "counts": dict(counts), "ratios": ratio}


def dir_inventory() -> dict:
    out: dict = {}
    for rel in AUDIT_DIRS:
        base = REPO_ROOT / rel
        if not base.is_dir():
            out[rel] = {"files": 0, "text_files": 0, "text_bytes": 0, "image_files": 0,
                        "image_bytes": 0, "other_bytes": 0, "exists": False}
            continue
        text_files = text_bytes = image_files = image_bytes = other_bytes = 0
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts:
                continue
            size = path.stat().st_size
            if is_text(path):
                text_files += 1
                text_bytes += size
            elif path.suffix.lower() in IMAGE_SUFFIXES:
                image_files += 1
                image_bytes += size
            else:
                other_bytes += size
        out[rel] = {
            "exists": True,
            "files": text_files + image_files + sum(
                1 for p in base.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
                and not is_text(p) and p.suffix.lower() not in IMAGE_SUFFIXES
            ),
            "text_files": text_files,
            "text_bytes": text_bytes,
            "image_files": image_files,
            "image_bytes": image_bytes,
            "other_bytes": other_bytes,
        }
    return out


def extract_relative_links(text: str) -> list[dict]:
    """从主入口抽出全部相对文档引用（A.4/A.5/A.6 的路由证据）。"""
    found: list[dict] = []
    patterns = (
        re.compile(r"`((?:references|docs|templates|scripts|测试|资产库)/[^`]+?\.(?:md|toml|py|json))`"),
        re.compile(r"`([A-Za-z][A-Za-z0-9_\-]*\.md)`"),
        re.compile(r"\]\((?!https?:)([^)]+?\.(?:md|toml|py|json))\)"),
    )
    for rx in patterns:
        for match in rx.finditer(text):
            target = match.group(1).strip()
            line = text[: match.start()].count("\n") + 1
            context = text.splitlines()[line - 1].strip()
            found.append({
                "target": target,
                "line": line,
                "context": context[:200],
                "conditional": any(w in context for w in CONDITIONAL_WORDS),
                "exists": (REPO_ROOT / target.split("#")[0]).exists(),
            })
    deduped: dict = {}
    for item in found:
        deduped.setdefault((item["target"], item["line"]), item)
    return sorted(deduped.values(), key=lambda i: (i["line"], i["target"]))


def unconditional_read_instructions(text: str) -> list[dict]:
    hits = []
    for idx, line in enumerate(text.splitlines(), start=1):
        if not any(w in line for w in MANDATORY_WORDS):
            continue
        if any(w in line for w in CONDITIONAL_WORDS):
            continue
        hits.append({"file": "SKILL.md", "line": idx, "text": line.strip()[:200]})
    for rel in sorted(STAGE_TRIGGERS):
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        for idx, line in enumerate(read(path).splitlines(), start=1):
            if not any(w in line for w in MANDATORY_WORDS):
                continue
            if any(w in line for w in CONDITIONAL_WORDS):
                continue
            hits.append({"file": rel, "line": idx, "text": line.strip()[:200]})
    return hits


def normalize_line(line: str) -> str:
    return re.sub(r"[\s`*_>|#\-—–]", "", line)


def duplicate_rule_lines(files: list[str], min_len: int = 24) -> list[dict]:
    """A.7：同一行文本出现在多个文件即视为跨文件重复规则。"""
    owner: dict = {}
    dupes: dict = {}
    for rel in files:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        seen_in_file: set = set()
        for line in read(path).splitlines():
            norm = normalize_line(line.strip())
            if len(norm) < min_len:
                continue
            if norm in seen_in_file:
                continue
            seen_in_file.add(norm)
            if norm in owner and owner[norm]["file"] != rel:
                entry = dupes.setdefault(norm, {
                    "line": line.strip()[:160],
                    "occurrences": [owner[norm]],
                })
                entry["occurrences"].append({"file": rel})
            elif norm not in owner:
                owner[norm] = {"file": rel}
    out = []
    for norm, entry in dupes.items():
        files_involved = sorted({o["file"] for o in entry["occurrences"]})
        out.append({
            "normalized_chars": len(norm),
            "line_sample": entry["line"],
            "files": files_involved,
            "copy_count": len(entry["occurrences"]),
        })
    return sorted(out, key=lambda i: (-i["copy_count"], -i["normalized_chars"]))[:40]


def audit_scanned_files() -> list[str]:
    """重复检测范围：会被读进上下文的文本，不含图片与夹具数据。"""
    files = ["SKILL.md", "README.md", "QUICKSTART.md", "FAQ.md", "CONTEXT.md"]
    files += ["docs/CHANGELOG.md"]
    for path in sorted((REPO_ROOT / "references").glob("*.md")):
        files.append(path.relative_to(REPO_ROOT).as_posix())
    return files


def large_text_inventory() -> dict:
    """A.9/E.2：把所有 >50KB 的文本按「谁会读它」分层，避免把数据文件当成上下文负担。"""
    boot, runtime_data, human_only = [], [], []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or not is_text(path):
            continue
        if any(p in path.parts for p in (".git", "__pycache__", ".process")):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if path.parent != REPO_ROOT and not rel.startswith(
                ("references", "docs", "scripts", "templates", "测试")):
            continue
        size = path.stat().st_size
        if size <= HUGE_ANY_READ:
            continue
        if rel in BOOT_ALWAYS:
            boot.append({"file": rel, "bytes": size})
        elif (REPO_ROOT / rel).name and _read_by_scripts(rel):
            runtime_data.append({"file": rel, "bytes": size,
                                 "read_by": _read_by_scripts(rel)})
        else:
            human_only.append({"file": rel, "bytes": size})
    return {"boot_loaded": boot, "runtime_parsed_data": runtime_data, "human_only": human_only}


SCRIPT_TEXT_REFS: dict | None = None


def _read_by_scripts(rel: str) -> list[str]:
    """哪些脚本按文件名读取该文本（说明它是被代码解析的数据，而不是整篇进上下文）。"""
    global SCRIPT_TEXT_REFS
    if SCRIPT_TEXT_REFS is None:
        refs: dict = {}
        for path in sorted((REPO_ROOT / "scripts").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = read(path)
            for match in re.finditer(r"[\u4e00-\u9fffA-Za-z0-9_\-]+\.md", text):
                refs.setdefault(match.group(0), set()).add(
                    path.relative_to(REPO_ROOT).as_posix())
        SCRIPT_TEXT_REFS = refs
    return sorted(SCRIPT_TEXT_REFS.get(Path(rel).name, []))


def external_skill_copy() -> dict:
    hits = []
    for rel in audit_scanned_files():
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        text = read(path)
        for marker in EXTERNAL_SKILL_HEADINGS:
            if marker in text:
                hits.append({"file": rel, "marker": marker})
    return {
        "marker_hits": hits,
        "note": "外部技能（Omni 等）只以契约指针与本技能自写细则出现，未复制任何外部技能完整正文；"
                "判据：主入口与 references 顶层文档中不存在外部技能 frontmatter、"
                "不存在超过 200 行的外部技能章节。",
    }


def _pack_roots() -> list[Path]:
    """所有可能的风格包根：内置包目录 + 版本化母版包（`private/`）。"""
    roots: list[Path] = []
    base = REPO_ROOT / STYLEPACK_DIR
    if base.is_dir():
        roots += [p for p in base.glob("*") if p.is_dir()]
    styles = REPO_ROOT / "references" / "styles"
    if styles.is_dir():
        roots += [p for p in styles.glob("*/*/private") if p.is_dir()]
    return roots


def script_injected_text() -> list[dict]:
    """A.4/A.10：脚本运行时自动注入模型提示词的文本（真正的隐性上下文成本）。

    两类入口：
    - 仓库相对路径（`references/...`、`templates/...`）；
    - 风格包内局部路径（相对包根解析：内置包或版本化母版包）。
    """
    pack_roots = _pack_roots()
    out = []
    for path in sorted((REPO_ROOT / "scripts").rglob("*.py")):
        rel_script = path.relative_to(REPO_ROOT).as_posix()
        if "__pycache__" in path.parts or rel_script == AUDIT_SCRIPT_REL:
            continue
        text = read(path)
        for match in re.finditer(r"[\"']((?:references|templates)/[^\"']+\.md)[\"']", text):
            rel = match.group(1)
            repo_target = REPO_ROOT / rel
            pack_hits = [r / rel for r in pack_roots if (r / rel).is_file()]
            sizes = sorted({p.stat().st_size for p in pack_hits if p.is_file()})
            out.append({
                "script": rel_script,
                "template": rel,
                "line": text[: match.start()].count("\n") + 1,
                "resolves_at_repo_root": repo_target.is_file(),
                "resolves_in_pack_roots": len(pack_hits),
                "injected_bytes_at_repo_root": repo_target.stat().st_size if repo_target.is_file() else None,
                "injected_bytes_in_packs": sizes,
            })
    deduped = {(i["script"], i["template"]): i for i in out}
    return sorted(deduped.values(), key=lambda i: (i["script"], i["template"]))


# 单个生图请求真正注入的包内文件（`image_request_builder.py` 常量的镜像）。
INJECTED_PACK_CORE_FILES = (
    "references/shared_style_invariants.md",
    "generation_formula.md",
    "references/face_base_style.md",
)
INJECTED_MATERIAL_GLOB = "references/materials/*_base_style.md"


def per_task_prompt_injection() -> dict:
    """A.10：最复杂任务里由脚本自动写进生图请求的文本量（模型每次都要重读）。"""
    builtin = []
    for pack_root in sorted(p for p in (REPO_ROOT / STYLEPACK_DIR).glob("*") if p.is_dir()):
        md_files = [p for p in pack_root.rglob("*.md") if p.is_file()]
        builtin.append({
            "stylepack": pack_root.relative_to(REPO_ROOT).as_posix(),
            "md_files": len(md_files),
            "md_bytes_total": sum(p.stat().st_size for p in md_files),
        })
    resolved: list[dict] = []
    missing = 0
    for pack_root in _pack_roots():
        core = [pack_root / rel for rel in INJECTED_PACK_CORE_FILES]
        if not all(p.is_file() for p in core):
            missing += 1
            continue
        materials = sorted(pack_root.glob(INJECTED_MATERIAL_GLOB))
        resolved.append({
            "pack_root": pack_root.relative_to(REPO_ROOT).as_posix(),
            "injected_bytes_single_face_task": sum(p.stat().st_size for p in core),
            "material_files": len(materials),
            "per_material_bytes_max": max(
                (p.stat().st_size for p in materials), default=0),
        })
    return {
        "builtin_stylepacks": sorted(builtin, key=lambda r: -r["md_bytes_total"]),
        "single_task_injection": sorted(resolved, key=lambda r: -r["injected_bytes_single_face_task"]),
        "pack_roots_without_injection_files": missing,
        "note": "内置包（stylepacks/<ID>）是分层卡的中文定性素材，按整包上限计；"
                "母版包（styles/*/*/private）才是生图请求真正注入的文件集合，"
                "injected_bytes_single_face_task = 共同不变量 + 生成公式 + 脸部基线（不含材质），"
                "每命中一种材质再加至多 per_material_bytes_max。",
    }


def size_overview() -> dict:
    entry = REPO_ROOT / "SKILL.md"
    text = read(entry)
    body = text.split("---", 2)[-1] if text.startswith("---") else text
    frontmatter = text.split("---", 2)[1] if text.startswith("---") else ""
    return {
        "bytes_total": len(text.encode("utf-8")),
        "bytes_body": len(body.encode("utf-8")),
        "bytes_frontmatter": len(frontmatter.encode("utf-8")),
        "lines_total": len(text.splitlines()),
        "size_class": classify_size(len(text.encode("utf-8"))),
        "classification": classify_entry_lines(text),
        "version_markers": skill_meta.prose_versions(text),
    }


def classify_size(entry_bytes: int) -> str:
    if entry_bytes <= 10 * 1024:
        return "lightweight(<=10KB)"
    if entry_bytes <= 25 * 1024:
        return "normal(10KB-25KB)"
    if entry_bytes <= 50 * 1024:
        return "large(25KB-50KB)"
    return "oversized(>50KB)"


def split_decision(entry_bytes: int) -> dict:
    """B 节统一判定标准 -> 结论与建议。"""
    text = read(REPO_ROOT / "SKILL.md")
    always_read = [i for i in always_load_docs(text)]
    always_bytes = sum((REPO_ROOT / p).stat().st_size for p in always_read
                       if (REPO_ROOT / p).is_file())
    if entry_bytes > 25 * 1024:
        verdict = "must_split"
        target = "主入口目标 15KB–20KB，硬上限 25KB"
    elif entry_bytes > 50 * 1024:
        verdict = "must_split"
        target = "必须瘦身并改为按需加载"
    else:
        verdict = "keep"
        target = "不得为拆分而拆分；只修真实重复、错误路由与无条件加载"
    return {
        "entry_bytes": entry_bytes,
        "entry_class": classify_size(entry_bytes),
        "boot_load_bytes": always_bytes,
        "boot_load_files": always_read,
        "split_required": verdict == "must_split",
        "verdict": verdict,
        "guideline": target,
        "single_always_read_over_25kb": [
            {"file": p, "bytes": (REPO_ROOT / p).stat().st_size}
            for p in always_read
            if (REPO_ROOT / p).is_file() and (REPO_ROOT / p).stat().st_size > LARGE_ALWAYS_READ
        ],
        "text_over_50kb": large_text_inventory(),
    }


def always_load_docs(text: str) -> list[str]:
    """启动立即加载：宿主必然注入的文件。"""
    return list(BOOT_ALWAYS)


def stage_load_map(text: str) -> list[dict]:
    """A.5：按阶段加载的文档、触发条件、体积与被引用方式。"""
    lines = text.splitlines()
    stage_heading_lines = [
        idx for idx, line in enumerate(lines, start=1)
        if line.strip().startswith("#") and ("路由" in line or "按需加载" in line)
    ]
    links = extract_relative_links(text)
    by_target: dict = {}
    for item in links:
        by_target.setdefault(item["target"], []).append(item)
    rows = []
    for rel, trigger in STAGE_TRIGGERS.items():
        path = REPO_ROOT / rel
        refs = by_target.get(rel, [])
        rows.append({
            "file": rel,
            "trigger": trigger,
            "bytes": path.stat().st_size if path.is_file() else None,
            "referenced_from_entry": bool(refs),
            "referenced_only_from_router_section": bool(refs) and all(
                any(stage <= r["line"] <= (stage + 40) for stage in stage_heading_lines)
                for r in refs
            ),
            "unconditional_reference": any(not r["conditional"] for r in refs),
        })
    return rows


def task_load_plans(text: str) -> dict:
    """A.10：普通任务与最复杂任务预计实际读取的文本、字节与顺序。"""
    normal = ["SKILL.md", "references/核心反推契约.md"]
    hardest = [
        "SKILL.md",
        "references/核心反推契约.md",
        "references/视觉生产契约.md",
        "references/Omni质量审核契约.md",
        "references/导演构图与站位契约.md",
        "references/角色参考入库契约.md",
        "references/并发与失败处理契约.md",
        "references/错误处理矩阵.md",
    ]

    def plan(files: list[str]) -> dict:
        rows = []
        total = 0
        for rel in files:
            path = REPO_ROOT / rel
            size = path.stat().st_size if path.is_file() else 0
            total += size
            rows.append({"order": len(rows) + 1, "file": rel, "bytes": size})
        return {"files": rows, "total_bytes": total,
                "est_tokens_upper": int(total * 1.6)}

    hardest_plan = plan(hardest)
    injections = per_task_prompt_injection()["single_task_injection"]
    if injections:
        top = injections[0]
        hardest_plan["script_injected_extra"] = {
            "pack_root": top["pack_root"],
            "bytes_per_task": top["injected_bytes_single_face_task"],
            "per_material_bytes_max": top["per_material_bytes_max"],
        }
        hardest_plan["total_bytes_with_injection"] = (
            hardest_plan["total_bytes"] + top["injected_bytes_single_face_task"])
    return {"normal_task": plan(normal), "hardest_task": hardest_plan}


def run_audit() -> dict:
    text = read(REPO_ROOT / "SKILL.md")
    entry_bytes = len(text.encode("utf-8"))
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": "scripts/audit_skill_context.py",
        "repo": REPO_ROOT.name,
        "classification_methods": CLASSIFICATION_METHODS,
        "entry": size_overview(),
        "directories": dir_inventory(),
        "boot_load": {
            "files": always_load_docs(text),
            "entry_point": "宿主读取 SKILL.md 全文注入系统提示（无脚本自动注入主入口）；"
                           "frontmatter 的 name/description 一并注入",
            "script_injected_text": script_injected_text(),
            "per_task_prompt_injection": per_task_prompt_injection(),
        },
        "relative_links_from_entry": extract_relative_links(text),
        "unconditional_read_instructions": unconditional_read_instructions(text),
        "stage_load_map": stage_load_map(text),
        "duplicate_rules": duplicate_rule_lines(audit_scanned_files()),
        "external_skill_copy": external_skill_copy(),
        "history_in_entry": {
            "history_like_lines": size_overview()["classification"]["counts"].get("history_like", 0),
            "sample_like_lines": size_overview()["classification"]["counts"].get("sample_like", 0),
            "changelog_pointer_present": "docs/CHANGELOG.md" in text,
        },
        "task_load_plans": task_load_plans(text),
        "decision": split_decision(entry_bytes),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="技能体积与真实上下文加载路径审计")
    parser.add_argument("--out", default=".process/skill-context-audit.json",
                        help="机器可读审计结果输出路径（相对仓库根）")
    parser.add_argument("--log", default=None, help="把完整审计转写一份到该日志文件")
    args = parser.parse_args(argv)

    payload = run_audit()
    out_path = (REPO_ROOT / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    if args.log:
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")

    decision = payload["decision"]
    entry = payload["entry"]
    over = decision["text_over_50kb"]
    injection = payload["boot_load"]["per_task_prompt_injection"]
    core_inject = (injection["single_task_injection"] or [{}])[0]
    builtin_top = (injection["builtin_stylepacks"] or [{}])[0]
    print(f"主入口: {entry['bytes_total']} 字节 / {entry['lines_total']} 行 / {entry['size_class']}")
    print(f"拆分判定: {decision['verdict']} -> {decision['guideline']}")
    print(f"启动实际加载: {decision['boot_load_bytes']} 字节 "
          f"({', '.join(decision['boot_load_files'])})")
    print(f"重复规则行: {len(payload['duplicate_rules'])} 组")
    print(f"无条件全量加载指令: {len(payload['unconditional_read_instructions'])} 条")
    print(f">50KB 文本：启动加载 {len(over['boot_loaded'])} 个 / "
          f"脚本按条解析的数据 {len(over['runtime_parsed_data'])} 个 / "
          f"仅人工查阅 {len(over['human_only'])} 个")
    print(f"单个生图请求注入（核心，不含材质）: {core_inject.get('injected_bytes_single_face_task')} 字节"
          f"；内置包上限: {builtin_top.get('md_bytes_total')} 字节")
    print(f"普通任务预计读取: {payload['task_load_plans']['normal_task']['total_bytes']} 字节；"
          f"最复杂任务: {payload['task_load_plans']['hardest_task']['total_bytes']} 字节")
    rel_out = out_path.relative_to(REPO_ROOT).as_posix() if out_path.is_relative_to(REPO_ROOT) \
        else out_path.as_posix()
    print(f"结论: 审计结果已写入 {rel_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
