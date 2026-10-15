#!/usr/bin/env python3
"""交付渲染：从数据库生成「主文件 + 原始记录」双文件。

- **主文件**：元数据 + AI 摘要 + 当前正文（沿用 `TranscriptDB.render_txt` 的既有版式与命名）；
- **原始记录**：全部版本（原始稿 + 校对版…）的来源文件、内容哈希、时间与全文，用于追溯。

交付的唯一事实源是数据库：主文件与原始记录都能随时重建。

    .venv/Scripts/python.exe scripts/render_delivery.py --bvid BV1xxxxxxxxx --json
    .venv/Scripts/python.exe scripts/render_delivery.py --bvid BV1xxxxxxxxx --page 3 --json
    .venv/Scripts/python.exe scripts/render_delivery.py --all --limit 5 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from transcript_db import DB_PATH, TranscriptDB  # noqa: E402
from logger import info  # noqa: E402

SOURCE = "render_delivery.py"
SEP = "=" * 80
KIND_LABEL = {"raw": "原始稿", "corrected": "校对版"}


def sha256_file(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def render_manifest(results: list[dict], manifest_path: Path) -> Path | None:
    """生成交接给下游索引的标准 source-manifest.json 契约文件。"""
    items = []
    base_dir = manifest_path.parent
    for r in results:
        if not r.get("ok") or not r.get("main_file"):
            continue
        mf = Path(r["main_file"])
        if not mf.is_file():
            continue
        bvid = r["bvid"]
        page = int(r.get("page", 1))
        sid = f"{bvid}_p{page}" if page > 1 else bvid
        try:
            txt_rel = str(mf.relative_to(base_dir).as_posix())
        except ValueError:
            txt_rel = str(mf.as_posix())
        ev = Path(r["evidence_file"]) if r.get("evidence_file") else None
        ev_rel = None
        if ev and ev.is_file():
            try:
                ev_rel = str(ev.relative_to(base_dir).as_posix())
            except ValueError:
                ev_rel = str(ev.as_posix())

        items.append({
            "source_id": sid,
            "platform": "bilibili",
            "source_url": f"https://www.bilibili.com/video/{bvid}" + (f"?p={page}" if page > 1 else ""),
            "part_index": page,
            "txt_path": txt_rel,
            "evidence_path": ev_rel,
            "sha256": sha256_file(mf),
            "status": "completed",
            "has_summary": bool(r.get("delivered_version")),
        })
    if items:
        manifest_data = {
            "schema_version": "1.0.0",
            "generator": "B站转录_v5.5.4",
            "generated_at": datetime.now().astimezone().isoformat(),
            "items": items,
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest_path
    return None


def versions_of(conn, bvid: str, page: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT version_no, kind, source_file, source_hash, transcript_text,
               summary, is_current, created_at
        FROM transcript_versions
        WHERE bvid=? AND page=? ORDER BY version_no
        """,
        (bvid, page),
    ).fetchall()
    return [
        {
            "version_no": row[0], "kind": row[1], "source_file": row[2],
            "source_hash": row[3], "text": row[4] or "", "summary": row[5] or "",
            "is_current": row[6], "created_at": row[7],
        }
        for row in rows
    ]


def evidence_text(record: dict, versions: list[dict]) -> str:
    lines = [
        SEP,
        "B站视频转录 · 原始记录（可追溯证据）",
        SEP,
        "",
        f"📹 视频标题：{record.get('title', '')}",
        f"🔗 B站链接：{record.get('url', '')}",
        f"👤 作者：{record.get('author', '')}",
        f"📅 发布时间：{record.get('upload_date', '')}",
        f"⏱️  视频时长：{record.get('duration', '')}",
        f"🎬 分P序号：P{int(record.get('page') or 1):02d}",
        f"📑 分P标题：{record.get('part_title') or '（单P视频或无分P标题）'}",
        f"🗂️  作品标识：{record.get('bvid', '')}",
        "",
        "本文件由数据库重建，记录该作品的全部版本与来源，供核对与追溯。",
        "主文件是可读交付物；本文件是证据层，不参与阅读流程。",
        "",
        SEP,
        "第一部分：版本清单",
        SEP,
        "",
    ]
    if versions:
        lines += [
            "| 版本 | 类型 | 字数 | 内容哈希 | 版本时间 | 来源文件 |",
            "|---|---|---|---|---|---|",
        ]
        for item in versions:
            label = KIND_LABEL.get(item["kind"], item["kind"])
            if item["is_current"]:
                label += "（当前）"
            lines.append(
                f"| v{item['version_no']} | {label} | {len(item['text'])} | "
                f"{item['source_hash'] or '—'} | {item['created_at']} | "
                f"`{Path(item['source_file']).name if item['source_file'] else '—'}` |"
            )
    else:
        lines.append("版本表为空（可先运行 `scripts/migrations.py --apply`）。")

    lines += ["", SEP, "第二部分：各版本全文", SEP, ""]
    for item in versions:
        label = KIND_LABEL.get(item["kind"], item["kind"])
        lines += [
            "",
            "-" * 80,
            f"v{item['version_no']} · {label} · {item['created_at']}"
            f" · 来源：{item['source_file'] or '（无）'}",
            "-" * 80,
            "",
            item["text"],
            "",
        ]
        if item["summary"]:
            lines += ["", "摘要：", "", item["summary"], ""]

    lines += [
        "",
        SEP,
        f"记录生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "文档结束",
        SEP,
    ]
    return "\n".join(lines) + "\n"


def render_one(db: TranscriptDB, bvid: str, page: int, output_dir: Path | None,
               *, dry_run: bool) -> dict:
    record = db.get_by_bvid(bvid, page)
    if not record:
        return {"bvid": bvid, "page": page, "ok": False, "error": "库中无此记录"}
    versions = versions_of(db.conn, bvid, page)

    # 主文件给最优文本：有校对版用最新校对版，否则用库中原稿；
    # 原始稿始终留在原始记录里，不被覆盖。
    corrected = [v for v in versions if v["kind"] == "corrected"]
    best = max(corrected, key=lambda v: v["version_no"]) if corrected else None
    note = ""
    if best:
        note = (f"正文为校对版 v{best['version_no']}；原始稿 v1 见同名「_原始记录.txt」")

    if dry_run:
        known = record.get("transcript_file") or ""
        main = Path(known) if known else None
        return {
            "bvid": bvid, "page": page, "ok": True, "dry_run": True,
            "main_file": str(main) if main else "（库中未记录文件路径，将新建）",
            "evidence_file": str(main.with_name(main.stem + "_原始记录.txt"))
            if main else "（库中未记录文件路径，将新建）",
            "versions": len(versions),
            "delivered_version": f"v{best['version_no']}（校对版）" if best else "v1（原始稿）",
        }

    main_path = db.render_txt(
        bvid, page=page,
        output_dir=str(output_dir) if output_dir else None,
        text_override=best["text"] if best else None,
        note=note,
    )
    if not main_path:
        return {"bvid": bvid, "page": page, "ok": False, "error": "主文件渲染失败"}
    main_path = Path(main_path)
    evidence_path = main_path.with_name(main_path.stem + "_原始记录.txt")
    evidence_path.write_text(evidence_text(record, versions), encoding="utf-8")
    return {
        "bvid": bvid, "page": page, "ok": True, "dry_run": False,
        "main_file": str(main_path), "evidence_file": str(evidence_path),
        "versions": len(versions),
        "delivered_version": f"v{best['version_no']}（校对版）" if best else "v1（原始稿）",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成「主文件 + 原始记录」双文件")
    parser.add_argument("--bvid", help="作品 BV 号")
    parser.add_argument("--page", type=int, default=1, help="分P序号（默认 1）")
    parser.add_argument("--all", action="store_true", help="渲染库中全部分P")
    parser.add_argument("--limit", type=int, default=0, help="与 --all 搭配，最多处理 N 个")
    parser.add_argument("--out", help="输出目录（默认按发布年月落在 output/）")
    parser.add_argument("--dry-run", action="store_true", help="只报告不写文件")
    parser.add_argument("--json", action="store_true", help="输出一行 JSON")
    parser.add_argument("--db", help="数据库路径（默认技能库）")
    parser.add_argument("--manifest", nargs="?", const="", help="同时输出 source-manifest.json 契约文件（可选指定路径，默认在输出目录下）")
    args = parser.parse_args(argv)

    if not args.bvid and not args.all:
        parser.error("需要 --bvid 或 --all")

    db_path = Path(args.db) if args.db else Path(DB_PATH)
    if not db_path.is_file():
        print(f"❌ 数据库不存在：{db_path}", file=sys.stderr)
        return 2
    output_dir = Path(args.out) if args.out else None

    with TranscriptDB(db_path=db_path) as db:
        if args.all:
            keys = [(row["bvid"], int(row["page"] or 1)) for row in db.get_all()]
            if args.limit:
                keys = keys[: args.limit]
        else:
            keys = [(args.bvid, int(args.page or 1))]
        results = []
        for bvid, page in keys:
            results.append(render_one(db, bvid, page, output_dir,
                                      dry_run=args.dry_run))

    report = {
        "dry_run": args.dry_run,
        "requested": len(results),
        "rendered": sum(1 for r in results if r["ok"]),
        "failed": [r for r in results if not r["ok"]],
        "results": results if len(results) <= 50 else results[:50],
    }
    if not args.dry_run and report["rendered"]:
        info(SOURCE, f"交付渲染完成：{report['rendered']} 个键")
        if args.manifest is not None:
            base_out = output_dir or (SKILL_DIR / "output")
            mf_path = Path(args.manifest) if args.manifest else base_out / "source-manifest.json"
            written = render_manifest(results, mf_path)
            if written:
                report["manifest"] = str(written)
                info(SOURCE, f"转录交付清单已生成：{written}")

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        mode = "预览" if args.dry_run else "写入"
        print(f"📄 交付渲染（{mode}）：成功 {report['rendered']} / 请求 {report['requested']}")
        for item in results[:10]:
            if item["ok"]:
                print(f"   {item['bvid']} P{int(item['page']):02d} 版本 {item['versions']}"
                      f"  →  {item['main_file']}")
            else:
                print(f"   {item['bvid']} P{int(item['page']):02d}  失败：{item['error']}")
        if len(results) > 10:
            print(f"   … 其余 {len(results) - 10} 条见 --json")
    return 0 if report["rendered"] == report["requested"] else 1


if __name__ == "__main__":
    sys.exit(main())
