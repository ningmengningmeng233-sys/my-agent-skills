#!/usr/bin/env python3

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from transcript_db import DB_PATH, TranscriptDB  # noqa: E402

OUTPUT_DIR = SKILL_DIR / "output"


def inspect_db_readonly() -> dict:
    info = {"exists": DB_PATH.exists()}
    if not info["exists"]:
        return info
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            cols = [
                r[1] for r in conn.execute("PRAGMA table_info(transcripts)").fetchall()
            ]
            info["columns"] = cols
            info["total_rows"] = conn.execute(
                "SELECT COUNT(*) FROM transcripts"
            ).fetchone()[0]
            info["has_page"] = "page" in cols
            old_unique = False
            for idx in conn.execute("PRAGMA index_list(transcripts)").fetchall():
                if idx[2] == 1:
                    idx_cols = [
                        r[2]
                        for r in conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()
                    ]
                    if idx_cols == ["bvid"]:
                        old_unique = True
                        break
            info["old_unique_bvid"] = old_unique
        finally:
            conn.close()
    except Exception as e:
        info["read_error"] = str(e)
    return info


def scan_rename_candidates() -> list[tuple[Path, Path]]:
    candidates = []
    if not OUTPUT_DIR.is_dir():
        return candidates
    for p in OUTPUT_DIR.rglob("*.txt"):
        if not re.search(r"BV[a-zA-Z0-9]+", p.name):
            continue
        if re.search(r"_P\d{1,4}(_|\.txt$)", p.name):
            continue
        candidates.append((p, p.with_name(f"{p.stem}_P01{p.suffix}")))
    return sorted(candidates)


def main():
    parser = argparse.ArgumentParser(description="v5.2 → v5.3 分P迁移助手")
    parser.add_argument("--apply", action="store_true", help="备份 DB 并执行自动迁移")
    parser.add_argument(
        "--rename-txt",
        action="store_true",
        help="把旧 TXT 重命名为带 _P01（需 --apply）",
    )
    args = parser.parse_args()

    info = inspect_db_readonly()
    print("=" * 60)
    print("分P迁移检查")
    print("=" * 60)
    if not info.get("exists"):
        print("⚠️ 未找到 transcripts.db，无需迁移（首次使用将自动建新表）")
    else:
        print(f"数据库: {DB_PATH}")
        print(f"记录数: {info.get('total_rows', '?')}")
        print(f"含 page 列: {info.get('has_page', False)}")
        print(f"旧 UNIQUE(bvid) 约束: {info.get('old_unique_bvid', '?')}")
        if info.get("read_error"):
            print(f"⚠️ 只读检查失败: {info['read_error']}")

    candidates = scan_rename_candidates()
    print(f"\n待重命名旧 TXT（无 _PNN 标记）: {len(candidates)} 个")
    for old, new in candidates[:20]:
        print(f"   {old.name}  →  {new.name}")
    if len(candidates) > 20:
        print(f"   ... 其余 {len(candidates) - 20} 个")

    if not args.apply:
        print("\n[DRY RUN] 未写任何文件。确认后用 --apply 执行。")
        return 0

    if info.get("exists"):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = DB_PATH.with_name(f"{DB_PATH.name}.bak-{ts}")
        shutil.copy2(DB_PATH, backup)
        print(f"\n✅ 数据库已备份: {backup}")

    print("🔄 触发数据库自动迁移（旧记录 page=1，重建 (bvid, page) 唯一约束）...")
    with TranscriptDB() as db:
        s = db.stats()
        total_bvids = db.conn.execute(
            "SELECT COUNT(DISTINCT bvid) FROM transcripts"
        ).fetchone()[0]
        print(f"   迁移后记录: {s['total']}（覆盖 {total_bvids} 个 BV）")
        print(f"   已有摘要: {s['with_summary']} | 待补摘要: {s['pending_summary']}")

    if args.rename_txt:
        renamed = 0
        with TranscriptDB() as db:
            for old, new in candidates:
                try:
                    old.rename(new)
                    db.conn.execute(
                        "UPDATE transcripts SET transcript_file=? "
                        "WHERE transcript_file=?",
                        (str(new), str(old)),
                    )
                    db.conn.commit()
                    renamed += 1
                    print(f"   ✅ {old.name} → {new.name}")
                except OSError as e:
                    print(f"   ❌ 重命名失败 {old}: {e}")
        print(f"\n重命名完成: {renamed}/{len(candidates)}")

    print("\n✅ 迁移完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
