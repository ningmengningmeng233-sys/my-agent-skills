# -*- coding: utf-8 -*-
"""转录索引 · 从已生成的索引 md 重建台账（恢复工具）

场景：全库切条目进程被中断（如 2026-08-14 暂停），已生成的 258 个 idx 未登记进 manifest，
下次增量会把这批文件误判为「新文件」重复切条目（重烧 token）。本脚本从 idx frontmatter
反推源文件并登记 ok=True，让增量续跑只处理真正缺失的文件。

用法: python _重建台账.py [--dry-run]
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (IDX_DIR, SRC_DIR, artifact_key, bv_from_name, file_hash,
                    load_manifest, save_manifest)

def main():
    dry = "--dry-run" in sys.argv
    mf = load_manifest()
    files = mf.setdefault("files", {})
    idx_files = sorted(IDX_DIR.glob("*-idx.md"))
    added = 0
    planned = []
    for p in idx_files:
        text = p.read_text(encoding="utf-8")
        m = re.search(r"^源文件: (.+)$", text, re.M)
        if not m:
            print(f"  ⚠ {p.name}: 无源文件字段，跳过")
            continue
        rel = m.group(1).strip()
        src = SRC_DIR / rel
        if not src.exists():
            print(f"  ⚠ {rel}: 源文件不存在，跳过")
            continue
        h = file_hash(src)
        if h in files and files[h].get("ok"):
            continue
        items_m = re.search(r"^条目数: (\d+)$", text, re.M)
        key_m = re.search(r"^产物键: (.+)$", text, re.M)
        key = key_m.group(1).strip() if key_m else p.name[:-len("-idx.md")]
        planned.append((h, rel, src.name, bv_from_name(src.name), key,
                        int(items_m.group(1)) if items_m else 0))
        if dry:
            print(f"[dry] 将登记 {rel}")
            continue
        files[h] = {
            "rel": rel, "name": src.name, "bv": bv_from_name(src.name),
            "artifact_key": key, "ok": True,
            "items": int(items_m.group(1)) if items_m else 0,
            "time": time.strftime("%Y-%m-%d %H:%M"),
        }
        if bv_from_name(src.name):
            lst = mf.setdefault("bv_map", {}).setdefault(bv_from_name(src.name), [])
            if h not in lst:
                lst.append(h)
        added += 1
    if not dry:
        save_manifest(mf)
    else:
        print("dry-run 未写盘")
    print(f"台账重建{'预览' if dry else '完成'}：将登记 {len(planned)} 个已索引文件（当前总计 {len(files)}）")
    print(f"下次运行 build.sh 将只处理真正缺失的文件")

if __name__ == "__main__":
    main()
