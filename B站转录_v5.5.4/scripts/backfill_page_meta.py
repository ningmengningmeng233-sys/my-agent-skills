#!/usr/bin/env python3
"""一次性回填旧 P1 记录缺失的 cid/part_title，并就地修正旧 TXT 头部（不改文件名）。"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from transcript_db import TranscriptDB  # noqa: E402

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}


def fetch_page_meta(bvid: str) -> dict | None:
    try:
        req = urllib.request.Request(f"{VIEW_API}?bvid={bvid}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != 0:
            return None
        pages = (data.get("data") or {}).get("pages") or []
        for p in pages:
            if p.get("page") == 1:
                return {"cid": p.get("cid"), "part_title": p.get("part") or ""}
    except Exception as e:
        print(f"   ⚠️ API 失败 {bvid}: {e}", file=sys.stderr)
    return None


def patch_txt_header(path: str, part_title: str) -> bool:
    p = Path(path)
    if not p.is_file():
        return False
    content = p.read_text(encoding="utf-8")
    if "🎬 分P序号：" in content or "📑 分P标题：" in content:
        return False
    lines = content.splitlines()
    out = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and line.startswith("⏱️  视频时长："):
            out.append("🎬 分P序号：P01")
            out.append(f"📑 分P标题：{part_title or '（无分P标题）'}")
            inserted = True
    if not inserted:
        return False
    p.write_text(
        "\n".join(out) + ("\n" if content.endswith("\n") else ""), encoding="utf-8"
    )
    return True


def main():
    parser = argparse.ArgumentParser(description="回填旧 P1 记录的 cid/part_title")
    parser.add_argument(
        "--apply", action="store_true", help="实际写入 DB 与 TXT；缺省为 dry-run"
    )
    args = parser.parse_args()

    with TranscriptDB() as db:
        rows = db.conn.execute("""
            SELECT bvid, title, transcript_file FROM transcripts
            WHERE page = 1 AND cid IS NULL
              AND bvid IN (
                  SELECT bvid FROM transcripts
                  GROUP BY bvid
                  HAVING COUNT(*) > 1
              )
            ORDER BY bvid
        """).fetchall()

    print(f"待回填的旧 P1 记录: {len(rows)} 个")
    updated = 0
    for row in rows:
        bvid, filepath = row["bvid"], row["transcript_file"]
        meta = fetch_page_meta(bvid)
        if not meta or meta["cid"] is None:
            print(f"   ⏭️ {bvid} 获取失败，跳过")
            continue
        txt_patch = "（TXT 不存在或已有分P行，跳过）"
        if filepath and meta["part_title"]:
            txt_patch = (
                "（TXT 将就地补两行）" if Path(filepath).is_file() else "（TXT 缺失）"
            )
        print(
            f"   {bvid} | cid={meta['cid']} | "
            f"part={meta['part_title'][:30]} {txt_patch}"
        )

        if args.apply:
            with TranscriptDB() as db:
                db.conn.execute(
                    "UPDATE transcripts SET cid=?, part_title=? "
                    "WHERE bvid=? AND page=1",
                    (meta["cid"], meta["part_title"], bvid),
                )
                db.conn.commit()
            if filepath and meta["part_title"]:
                patch_txt_header(filepath, meta["part_title"])
            updated += 1
        time.sleep(0.5)

    print(f"\n{'=' * 50}")
    if args.apply:
        print(f"✅ 已回填 {updated}/{len(rows)} 条（TXT 文件名未变，只补头部两行）")
    else:
        print("[DRY RUN] 未写入。确认后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
