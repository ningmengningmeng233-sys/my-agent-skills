#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧视觉产物识别与非破坏迁移（规格 10，工单04-Task1）。

原则：
- **绝不覆盖、绝不删除**旧提示词/旧图片/旧清单；迁移前先备份，迁移后逐文件哈希不变。
- 旧图片登记为 `legacy_unreviewed`（默认不可作为参考）；只有通过新版检查才可复用。
- 新视觉计划只依据**当前资产索引**建立，绝不从旧提示词反推事实。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .style_registry import resolve_style_record
from .visual_planner import PlannerError, plan_visual_tasks
from .visual_schema import load_visual_plan, write_visual_plan

VISUAL_DIR = ("视觉资产",)
LEGACY_MARKER = "旧版产物登记.json"
PLAN_REL = ("视觉资产", "计划.toml")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
LEGACY_UNREVIEWED = "legacy_unreviewed"
SOURCE_UNVERIFIABLE = "SOURCE_UNVERIFIABLE"
STYLE_MAP_UNCONFIRMED = "STYLE_MAP_UNCONFIRMED"

# 旧到新的风格映射（旧编号 → 新中文正式名）；**仅提示，需用户确认**
LEGACY_STYLE_SUGGESTIONS = {
    "H7": "仙侠国风电影CG",
    "E1": "",  # 视频向；仅在题材确认为仙侠且用户显式选择时才建议
}
CROP_HEAD_WORDS = ("裁头", "裁除", "无头身体", "切掉头部", "头部裁除")
PANEL_WORDS = ("三格", "三宫格", "三视图拼版", "多格")
FANTASY_GENRES = ("玄幻", "仙侠", "神魔", "修真", "奇幻")


class MigrationError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scan_legacy_visuals(lib: Path) -> dict:
    """只读扫描旧视觉产物（不写盘、不改文件）。"""
    lib = Path(lib)
    items: list[dict] = []
    warnings: list[str] = []

    state = {}
    state_path = lib / ".start-state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
    else:
        warnings.append(SOURCE_UNVERIFIABLE)
    legacy_style = str(state.get("style") or "").strip()
    genre = str(state.get("genre") or "").strip()

    # 旧提示词：裁头版文件与含三格/裁头描述的人物节
    prompt_root = lib / "提示词"
    if prompt_root.is_dir():
        for path in sorted(prompt_root.rglob("*.md")):
            rel = path.relative_to(lib).as_posix()
            name = path.name
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "裁头" in name:
                items.append({"kind": "裁头版提示词", "path": rel, "sha256": _sha256(path)})
            if any(w in text for w in CROP_HEAD_WORDS) or any(w in text for w in PANEL_WORDS):
                words = sorted({w for w in (*CROP_HEAD_WORDS, *PANEL_WORDS) if w in text})
                items.append({"kind": "三格/裁头提示词节", "path": rel,
                              "sha256": _sha256(path), "matched": words})

    # 旧生成图片与旧清单：只在明确的旧视觉产物目录/命名下识别，避免把普通图片误判
    for path in sorted(lib.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        rel = path.relative_to(lib).as_posix()
        if any(part in (".process", "视觉资产") for part in Path(rel).parts):
            continue
        parent = Path(rel).parts[0] if Path(rel).parts else ""
        name = path.name
        looks_visual = ("图片" in parent or "生成" in parent or "提示词" in parent
                        or any(w in name for w in PANEL_WORDS + CROP_HEAD_WORDS)
                        or bool(re.search(r"_(正脸|后脑|全身|服装|场景|道具)", name)))
        if not looks_visual:
            continue
        flagged = any(w in name for w in PANEL_WORDS)
        items.append({"kind": "旧生成图片", "path": rel, "sha256": _sha256(path),
                      "panel_suspect": flagged,
                      "reason": "文件名含多格/拼版关键词" if flagged else "位于旧视觉产物命名空间"})
    for path in sorted(lib.rglob("*清单*.md")) + sorted(lib.rglob("*清单*.json")):
        if not path.is_file():
            continue
        rel = path.relative_to(lib).as_posix()
        text = ""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(s in text for s in IMAGE_SUFFIXES) or any(w in text for w in PANEL_WORDS):
            items.append({"kind": "旧图片清单", "path": rel, "sha256": _sha256(path)})
    if legacy_style:
        items.append({"kind": "旧风格状态", "path": ".start-state.json",
                      "sha256": _sha256(state_path), "style": legacy_style})

    suggested = ""
    if legacy_style in LEGACY_STYLE_SUGGESTIONS and LEGACY_STYLE_SUGGESTIONS[legacy_style]:
        suggested = LEGACY_STYLE_SUGGESTIONS[legacy_style]
    elif legacy_style == "E1" and any(g in genre for g in FANTASY_GENRES):
        suggested = "仙侠国风电影CG（需用户显式确认；E1 属电影感写实向，非自动替换）"

    return {
        "lib": lib.as_posix(),
        "items": items,
        "warnings": warnings,
        "legacy_style": legacy_style,
        "genre": genre,
        "suggested_style": suggested,
        # 旧风格到新风格的映射必须由用户确认后才生效，绝不静默改写
        "style_map_confirmed": False,
        "counts": {
            "裁头版提示词": sum(1 for i in items if i["kind"] == "裁头版提示词"),
            "三格/裁头提示词节": sum(1 for i in items if i["kind"] == "三格/裁头提示词节"),
            "旧生成图片": sum(1 for i in items if i["kind"] == "旧生成图片"),
            "旧图片清单": sum(1 for i in items if i["kind"] == "旧图片清单"),
        },
    }


def _backup(lib: Path) -> Path:
    """迁移前备份（复用 maintenance.do_backup 的命名与 zip-slip 口径）。"""
    import os
    import subprocess
    import sys

    skill = Path(__file__).resolve().parent.parent.parent
    out_dir = lib.parent / "资产库备份"
    # 路径一律走 argv 传参：把它们插值进 -c 源码会让引号/反斜杠把合同写歪。
    script = (
        "import sys, pathlib;"
        "sys.path.insert(0, sys.argv[1]);"
        "from scripts.maintenance import do_backup;"
        "do_backup(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))"
    )
    # 这个孙进程由本模块创建，所以它的输出编码也归本模块负责：默认 GBK 控制台下
    # do_backup 打印 `✅ 备份完成…` 会抛 UnicodeEncodeError，被上层读成 BACKUP_FAILED——
    # 备份其实已成功，失败码却是假的（实测：不设 UTF-8 时 9 红，设了 20 全绿）。
    # 只复制继承环境再补两个编码变量，不碰宿主全局环境，也不改其它字段。
    child_env = dict(os.environ)
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, "-X", "utf8", "-c", script,
                           str(skill), str(lib), str(out_dir)],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=child_env)
    if proc.returncode != 0:
        raise MigrationError("BACKUP_FAILED", (proc.stdout + proc.stderr)[-300:])
    zips = sorted(out_dir.glob("资产库备份-*.zip"))
    if not zips:
        raise MigrationError("BACKUP_FAILED", "备份未生成")
    return zips[-1]


def migrate_legacy_visuals(lib: Path, style_name: str, backup: bool = True) -> dict:
    """非破坏迁移：备份 → 登记旧产物 → 依当前资产索引新建视觉计划。"""
    lib = Path(lib)
    record = resolve_style_record(style_name)
    if record is None:
        raise MigrationError("STYLE_UNKNOWN", f"风格 {style_name!r} 不在登记表中")
    if not record.pack_version or not record.pack_hash:
        raise MigrationError("STYLE_UNPACKED",
                             f"风格 {record.name} 未固定包版本与内容哈希（先同步风格包）")

    report = scan_legacy_visuals(lib)
    marker_path = lib / VISUAL_DIR[0] / LEGACY_MARKER
    if marker_path.is_file():
        try:
            existing = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if existing.get("legacy_index_hash"):
            return {
                "migrated": False, "already_migrated": True,
                "legacy_index_hash": existing["legacy_index_hash"],
                "backup": str(existing.get("backup", "")),
                "style_version": record.pack_version, "style_hash": record.pack_hash,
                "legacy_style": report["legacy_style"],
            }

    plan_path = lib / PLAN_REL[0] / PLAN_REL[1]
    existing_plan = load_visual_plan(plan_path) if plan_path.is_file() else None

    backup_path = ""
    if backup and report["items"]:
        backup_path = str(_backup(lib))

    # 1) 旧产物登记（只登记旧图片与其哈希，默认不可作为参考）
    images = [{"path": i["path"], "sha256": i["sha256"], "kind": i["kind"],
               "status": LEGACY_UNREVIEWED, "usable_as_reference": False}
              for i in report["items"] if i["kind"] == "旧生成图片"]
    index = {
        "schema_version": 1,
        "note": "旧版视觉产物登记：默认不可作为参考；只有通过新版检查后才可复用",
        "legacy_style": report["legacy_style"],
        "suggested_style": report["suggested_style"],
        "style_map_confirmed": False,
        "backup": backup_path,
        "images": images,
        "prompts": [{"path": i["path"], "sha256": i["sha256"], "kind": i["kind"]}
                    for i in report["items"]
                    if i["kind"] in ("裁头版提示词", "三格/裁头提示词节")],
        "scanned_at": "",
    }

    # 2) 新计划只依据当前资产索引（缺索引即拒绝，不从旧提示词反推事实）
    index_path = lib / ".process" / "资产索引.json"
    if not index_path.is_file():
        raise MigrationError(
            "ASSET_INDEX_REQUIRED",
            f"缺少资产索引 {index_path}：新视觉计划必须基于当前资产索引建立，"
            "不从旧提示词反推事实（可先跑聚合或 maintenance.py migrate）")
    try:
        asset_index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MigrationError("ASSET_INDEX_UNREADABLE", str(e)) from e
    try:
        plan = plan_visual_tasks(asset_index, record, existing=existing_plan)
    except PlannerError as e:
        raise MigrationError(e.code, e.detail) from e
    write_visual_plan(plan, plan_path)
    index["asset_index_hash"] = plan.asset_index_hash

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
    digest = _sha256(marker_path)
    data = json.loads(marker_path.read_text(encoding="utf-8"))
    data["legacy_index_hash"] = digest
    marker_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
    return {
        "migrated": True, "already_migrated": False,
        "legacy_index_hash": digest,
        "backup": backup_path,
        "legacy_style": report["legacy_style"],
        "suggested_style": report["suggested_style"],
        "style_name": record.name,
        "style_version": record.pack_version,
        "style_hash": record.pack_hash,
        "legacy_images": len(images),
        "plan_tasks": len(plan.tasks),
        "warnings": report["warnings"],
    }
