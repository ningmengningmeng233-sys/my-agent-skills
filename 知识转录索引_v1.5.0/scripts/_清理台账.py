# -*- coding: utf-8 -*-
"""转录索引 · 台账清理（孤儿记录 + bv_map 去重）

2026-08-16 落地：台账「只增不减」的副作用——文件删除或内容重切后，旧 hash 键成为死记录
（manifest 重复注册 / bv_map 重复映射 / 台账孤儿）。本脚本按当前源文件快照清理：
1. files 中 hash 不在当前扫描的 → 删除（孤儿）
2. bv_map 只保留指向现存 hash 的引用并去重

用法: python _清理台账.py [--dry-run]
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (IDX_DIR, PROOF_DIR, artifact_key, load_manifest,
                    remove_artifacts, save_manifest, scan_sources)


def main():
    dry = "--dry-run" in sys.argv
    scans = scan_sources()
    scan_hash = {s["hash"] for s in scans}
    mf = load_manifest()
    files = mf.get("files", {})

    dead = [k for k in files if k not in scan_hash]
    for k in dead:
        v = files[k]
        print(f"{'[dry] ' if dry else ''}✂ 孤儿: {v.get('name', '')[:50]}（hash {k[:12]}…）")
        key = v.get("artifact_key") or artifact_key(v.get("rel", v.get("name", "")), k)
        idx_path = IDX_DIR / f"{key}-idx.md"
        proof_path = PROOF_DIR / f"{key}_校对版.txt"
        if idx_path.exists() or proof_path.exists():
            print(f"  {'[dry] ' if dry else ''}✂ 产物: {key}")
            if not dry:
                remove_artifacts(key)
    if not dry:
        for k in dead:
            del files[k]

    bv = mf.get("bv_map", {})
    removed_bv = 0
    for bvno, lst in list(bv.items()):
        kept = []
        for h in lst:
            if h in files and h not in kept:
                kept.append(h)
        if kept:
            removed_bv += len(lst) - len(kept)
            if len(lst) != len(kept):
                print(f"{'[dry] ' if dry else ''}✂ bv_map {bvno}: {len(lst)} → {len(kept)}")
            bv[bvno] = kept
        else:
            removed_bv += len(lst)
            print(f"{'[dry] ' if dry else ''}✂ bv_map {bvno} 整组失效删除（{len(lst)} 条）")
            del bv[bvno]

    # 兼容没有 manifest 记录的旧/孤立产物：按 frontmatter 源文件检查并清理。
    live_rels = {s["rel"] for s in scans}
    orphan_idx = []
    for p in IDX_DIR.glob("*-idx.md"):
        text = p.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^源文件: (.+)$", text, re.M)
        key_m = re.search(r"^产物键: (.+)$", text, re.M)
        key = key_m.group(1).strip() if key_m else p.name[:-len("-idx.md")]
        if m and m.group(1).strip() not in live_rels:
            orphan_idx.append(p)
            print(f"{'[dry] ' if dry else ''}✂ 孤立索引产物: {p.name}")
            if not dry:
                remove_artifacts(key)

    print(f"\n孤儿 {len(dead)} 条 | 孤立产物 {len(orphan_idx)} 个 | bv_map 清理 {removed_bv} 条")
    if not dry:
        save_manifest(mf)
        print(f"台账已保存: files {len(files)} 键, bv_map {len(bv)} 组")
    else:
        print("dry-run 未写盘")


if __name__ == "__main__":
    main()
