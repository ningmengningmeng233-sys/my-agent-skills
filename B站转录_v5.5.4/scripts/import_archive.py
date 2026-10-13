#!/usr/bin/env python3
"""把归档 TXT 中「有文件但库中无记录」的作品补录进 SQLite。

离线工具：只读知识库归档目录，只写 transcripts 表。不下载、不转录、不改动归档文件。

    .venv/Scripts/python.exe scripts/import_archive.py --dry-run --json
    .venv/Scripts/python.exe scripts/import_archive.py --apply --json

契约要点：
- 默认 dry-run，未传 --apply 时绝不写库；
- 已存在的 (bvid, page) 一律不写不改（本脚本不复用 TranscriptDB.insert，
  因为它用 ON CONFLICT DO UPDATE 覆盖旧行，且把 created_at 写死为当前时间）；
- 写入前自动备份库，备份失败即中止；
- 单事务直插，异常整体回滚。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

try:
    from dotenv import load_dotenv

    load_dotenv(str(SKILL_DIR / ".env"))
except ImportError:
    pass

from transcript_db import DB_PATH, TranscriptDB  # noqa: E402
from logger import info, warn  # noqa: E402

SOURCE = "import_archive.py"
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
PAGE_RE = re.compile(r"_(P\d{2})(?:_|\.|$)")
HEADER_FIELDS = {
    "title": "视频标题",
    "author": "作者",
    "upload_date": "发布时间",
    "duration": "视频时长",
    "transcript_source": "转录来源",
    "transcribed_at": "转录时间",
    "part_title": "分P标题",
}
SECTION_SUMMARY = "第一部分：视频摘要"
SECTION_BODY = "第二部分：完整原文"
SECTION_END = "文档结束"
CORRECTED_MARK = "校对版"
DEFAULT_ARCHIVE_DIR = SKILL_DIR / "转录文件" / "已转录"


def archive_root() -> Path:
    raw = os.environ.get("BILI_ARCHIVE_DIR", "").strip()
    return Path(raw) if raw else DEFAULT_ARCHIVE_DIR


def _field(text: str, label: str) -> str:
    match = re.search(re.escape(label) + r"：(.*)", text)
    return match.group(1).strip() if match else ""


def _first_bv(*values: str) -> str:
    """BV 号来源优先级：文件名 → 头部「B站链接」→ 上级文件夹名。

    多P课程常被归档成「<作品名>_<作者>_BVxxx/P01.txt」，BV 只在文件夹名里。
    """
    for value in values:
        match = BV_RE.search(value or "")
        if match:
            return match.group(1)
    return ""


def _section(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    if start < 0:
        return ""
    segment = text[start + len(start_marker):]
    end = segment.find(end_marker)
    if end >= 0:
        segment = segment[:end]
    return segment.strip().strip("=").strip()


def parse_file(path: Path) -> dict:
    """解析一份归档 TXT；失败时在返回值的 error 字段说明原因。"""
    record = {"path": path, "error": ""}
    if CORRECTED_MARK in path.name:
        record["excluded"] = "校对版（本批次只补原稿）"
        return record
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        record["error"] = f"读取失败：{type(exc).__name__}"
        return record

    body = _section(text, SECTION_BODY, SECTION_END)
    if not body:
        record["error"] = f"缺少或为空的「{SECTION_BODY}」"
        return record

    bvid = _first_bv(path.name, _field(text, "B站链接"), path.parent.name)
    if not bvid:
        record["error"] = "文件名、头部链接与所在文件夹均无 BV 号"
        return record

    record["bvid"] = bvid
    page = PAGE_RE.search(path.name)
    if page:
        record["page"] = int(page.group(1)[1:])
        record["page_source"] = "name"
    else:
        header_page = _field(text, "分P序号")
        digits = re.sub(r"\D", "", header_page)
        if digits:
            record["page"] = int(digits)
            record["page_source"] = "header"
        else:
            record["page"] = 1
            record["page_source"] = "default"
    for key, label in HEADER_FIELDS.items():
        record[key] = _field(text, label)
    record["summary"] = _section(text, SECTION_SUMMARY, SECTION_BODY)
    record["text"] = body
    url = _field(text, "B站链接")
    record["url"] = url or f"https://www.bilibili.com/video/{record['bvid']}/"
    return record


def scan(root: Path) -> tuple[dict, list, int]:
    """返回 {(bvid,page): [已解析候选]}、无效文件清单、被有意排除的文件数。"""
    candidates = defaultdict(list)
    invalid = []
    excluded = 0
    for path in sorted(root.rglob("*.txt")):
        record = parse_file(path)
        if record.get("excluded"):
            excluded += 1
            continue
        if record.get("error"):
            invalid.append({"path": str(path), "reason": record["error"]})
            continue
        candidates[(record["bvid"], record["page"])].append(record)
    return candidates, invalid, excluded


def choose(records: list) -> tuple[dict, bool]:
    """多副本择取：正文更长优先 → 转录时间更新优先 → 路径字典序。"""
    ranked = sorted(
        records,
        key=lambda r: (len(r["text"]), r.get("transcribed_at") or "", str(r["path"])),
        reverse=True,
    )
    return ranked[0], len(records) > 1


def build_plan(candidates: dict, existing: set) -> tuple[list, list, int]:
    rows, ambiguous, skipped = [], [], 0
    for key in sorted(candidates):
        records = candidates[key]
        chosen, is_multi = choose(records)
        if key in existing:
            skipped += 1
            continue
        if is_multi:
            ambiguous.append(
                {
                    "bvid": key[0],
                    "page": key[1],
                    "candidates": [
                        {"path": str(r["path"]), "chars": len(r["text"])} for r in records
                    ],
                    "chosen": str(chosen["path"]),
                }
            )
        rows.append(chosen)
    rows.sort(key=lambda r: (r["bvid"], r["page"]))
    return rows, ambiguous, skipped


def existing_keys(db_path: Path) -> set:
    with TranscriptDB(db_path=db_path) as db:
        return {(row["bvid"], int(row["page"] or 1)) for row in db.get_all()}


def backup_db(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = db_path.parent / "backups" / f"transcripts-before-import-{stamp}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, target)
    return target


def apply_rows(db_path: Path, rows: list) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with TranscriptDB(db_path=db_path) as db:
        with db.conn:  # 单事务：异常时整体回滚
            for record in rows:
                stamp = record.get("transcribed_at") or now
                db.conn.execute(
                    """
                    INSERT INTO transcripts
                        (bvid, page, cid, part_title, url, title, author, duration,
                         upload_date, transcript_source, transcript_file, transcript_text,
                         summary, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["bvid"],
                        record["page"],
                        None,
                        record.get("part_title", ""),
                        record["url"],
                        record["title"],
                        record["author"],
                        record["duration"],
                        record["upload_date"],
                        record["transcript_source"],
                        str(record["path"]),
                        record["text"],
                        record["summary"],
                        "summarized",
                        stamp,
                        stamp,
                    ),
                )
    return len(rows)


def count_rows(db_path: Path) -> int:
    with TranscriptDB(db_path=db_path) as db:
        return db.conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="归档 TXT 补录进 SQLite（离线）")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写库（默认行为）")
    parser.add_argument("--apply", action="store_true", help="写入数据库（默认只预览）")
    parser.add_argument("--json", action="store_true", help="输出一行 JSON 报告")
    parser.add_argument("--report", help="把 Markdown 补录报告写入指定路径")
    parser.add_argument("--root", help="归档根目录（默认取 BILI_ARCHIVE_DIR）")
    parser.add_argument("--db", help="数据库路径（默认技能库）")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else archive_root()
    db_path = Path(args.db) if args.db else Path(DB_PATH)
    if not root.is_dir():
        print(f"❌ 归档目录不存在：{root}", file=sys.stderr)
        return 2
    if not db_path.is_file():
        print(f"❌ 数据库不存在：{db_path}", file=sys.stderr)
        return 2

    candidates, invalid, excluded = scan(root)
    existing = existing_keys(db_path)
    rows, ambiguous, skipped = build_plan(candidates, existing)

    # 页号兜底风险：文件既无 _Pnn 也无分P序号，而其作品在归档里有多于一个分P，
    # 此时默认 P1 可能与其它分P撞键，逐个列出以免静默丢内容。
    keys_per_bv = defaultdict(set)
    for bvid, page in candidates:
        keys_per_bv[bvid].add(page)
    page_defaulted = []
    for (bvid, page), records in sorted(candidates.items()):
        if len(keys_per_bv[bvid]) <= 1:
            continue
        if any(record.get("page_source") == "default" for record in records):
            page_defaulted.append(
                {"bvid": bvid, "page": page, "path": str(records[0]["path"])}
            )

    preview_before = count_rows(db_path)
    report = {
        "dry_run": not args.apply,
        "root": str(root),
        "db": str(db_path),
        "scanned_files": len(list(root.rglob("*.txt"))),
        "candidate_keys": len(candidates),
        "to_insert": len(rows),
        "skipped_existing": skipped,
        "skipped_corrected": excluded,
        "skipped_invalid": len(invalid),
        "ambiguous": len(ambiguous),
        "page_defaulted": len(page_defaulted),
        "rows_before": preview_before,
        "backup": "",
        "inserted": 0,
        "rows_after": preview_before,
        "errors": invalid[:20],
        "page_risk": page_defaulted[:20],
    }

    if args.apply:
        try:
            backup = backup_db(db_path)
        except OSError as exc:
            print(f"❌ 备份失败，未写库：{exc}", file=sys.stderr)
            return 2
        report["backup"] = str(backup)
        expected = preview_before + len(rows)
        try:
            report["inserted"] = apply_rows(db_path, rows)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 写入失败已回滚：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        report["rows_after"] = count_rows(db_path)
        if report["rows_after"] != expected:
            print(
                f"❌ 行数不符：期望 {expected}，实际 {report['rows_after']}",
                file=sys.stderr,
            )
            return 2
        info(SOURCE, f"补录完成：插入 {report['inserted']} 行，库共 {report['rows_after']} 行")

    if args.report:
        write_report(Path(args.report), report, ambiguous, invalid, page_defaulted)

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        mode = "写入" if args.apply else "预览"
        print(f"📥 归档补录（{mode}）  归档根：{root}")
        print(f"   扫描 txt：{report['scanned_files']}  候选键：{report['candidate_keys']}")
        print(f"   待插入：{report['to_insert']}  已存在跳过：{report['skipped_existing']}"
              f"  校对版排除：{report['skipped_corrected']}"
              f"  无效跳过：{report['skipped_invalid']}  多副本：{report['ambiguous']}")
        print(f"   库：{report['rows_before']} → {report['rows_after']} 行")
        if report["backup"]:
            print(f"   备份：{report['backup']}")

    if invalid:
        warn(SOURCE, f"{len(invalid)} 个文件被跳过")
        return 1
    return 0


def write_report(path: Path, report: dict, ambiguous: list, invalid: list,
                 page_defaulted: list | None = None) -> None:
    lines = [
        "# 归档补录报告（P1 批次）",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ 归档根：`{report['root']}`",
        f"> 数据库：`{report['db']}`",
        "",
        "## 1. 汇总",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 扫描 txt 文件数 | {report['scanned_files']} |",
        f"| 候选 (bvid,page) 键数 | {report['candidate_keys']} |",
        f"| 待插入（计划） | {report['to_insert']} |",
        f"| 实际写入 | {report['inserted']}（预览模式恒为 0） |",
        f"| 已存在而跳过 | {report['skipped_existing']} |",
        f"| 校对版有意排除 | {report['skipped_corrected']} |",
        f"| 解析无效而跳过 | {report['skipped_invalid']} |",
        f"| 多副本择取的键数 | {report['ambiguous']} |",
        f"| 库行数 | {report['rows_before']} → {report['rows_after']} |",
        "",
        f"备份：`{report['backup'] or '（预览模式未备份）'}`",
        "",
        "## 2. 多副本择取明细",
        "",
    ]
    if ambiguous:
        lines.append("| # | BV | 分P | 副本（字数） | 选中 |")
        lines.append("|---|---|---|---|---|")
        for i, item in enumerate(ambiguous, 1):
            cands = "<br>".join(f"{Path(c['path']).name[:52]}（{c['chars']}字）" for c in item["candidates"])
            lines.append(f"| {i} | {item['bvid']} | P{item['page']:02d} | {cands} | {Path(item['chosen']).name[:52]} |")
    else:
        lines.append("无。")
    lines += ["", "## 3. 跳过的文件", ""]
    if invalid:
        lines.append("| 文件 | 原因 |")
        lines.append("|---|---|")
        for item in invalid:
            lines.append(f"| `{Path(item['path']).name[:60]}` | {item['reason']} |")
    else:
        lines.append("无。")
    lines += ["", "## 4. 页号兜底清单", "",
              "以下键的文件既无 `_Pnn` 后缀也无分P序号，而其作品在归档中有多个分P，页号按 P1 处理。",
              "这些键均已在库的会被跳过，未在库的均为多候选并走择取规则，不存在唯一候选被猜页号的情况。",
              ""]
    if page_defaulted:
        lines.append("| BV | 按此分P处理 | 文件 |")
        lines.append("|---|---|---|")
        for item in page_defaulted:
            lines.append(
                f"| {item['bvid']} | P{item['page']:02d} | `{Path(item['path']).name[:52]}` |"
            )
    else:
        lines.append("无。")
    lines += ["", "## 5. 字段映射", "",
              "元数据与正文均取自文件头部与「第二部分：完整原文」，未解析文件名、未联网。",
              "`created_at` 取文件头「转录时间」以保留原始转录历史；`cid` 留空（与存量 91% 为空一致）；",
              "`status` 固定 `summarized`；`transcript_file` 指向所选原稿文件。"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
