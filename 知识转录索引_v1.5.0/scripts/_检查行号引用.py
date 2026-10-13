# -*- coding: utf-8 -*-
"""转录索引 · 全库行号引用体检（对应 v3.33 治理项③，可复测治理效果）

对 索引库/ 全部 *-idx.md 做行号引用体检：
- 越界检测：行号超出源文件总行数（必然错误）
- 区间-校对版重叠检测：条目区间在对应校对版段落覆盖范围内（锚点定位正确性）
- 锚点命中率统计（间接：通过区间合法性反映）
- 汇总报告：异常文件清单 + 体检结论

用法: python _检查行号引用.py [--src <目录>]
  --src <目录>: 指定检查目录（快照目录无源文件时自动进入快照模式：
    未索引/孤儿按 idx 存在性降级检查，行号体检跳过）。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import IDX_DIR, PROOF_DIR, SRC_DIR, read_lines


def _has_source_txt(dir_path):
    """目录内是否有源转录 txt（排除产物目录）——快照目录无源文件，据此判定快照模式。"""
    import os
    for root, dirs, files in os.walk(dir_path):
        dirs[:] = [d for d in dirs if d not in {"索引库", "校对版", "向量库"}]
        for f in files:
            if f.lower().endswith(".txt"):
                return True
    return False


def resolve_src(args):
    """--src <目录> → 重定向 common 产物目录（含 MANIFEST，2026-08-16 修复：
    原实现漏同步 MANIFEST，台账读生产库导致 271 条假警报）。
    快照模式按「目录无源 txt」判定（而非看 --src 传没传）。"""
    src_arg = None
    if "--src" in args:
        i = args.index("--src")
        if i + 1 < len(args):
            src_arg = Path(args[i + 1]).resolve()
    if src_arg:
        import common
        common.redirect_src(src_arg)
        snapshot = not _has_source_txt(src_arg)
        return common.IDX_DIR, common.PROOF_DIR, common.SRC_DIR, snapshot
    return IDX_DIR, PROOF_DIR, SRC_DIR, False


def total_lines_of(src_rel, src_dir):
    """源文件真实总行数（read_lines 统一行尾）。
    2026-08-16 修复：原实现引用模块级 from-import 的旧 SRC_DIR 绑定，
    --src 重定向后仍查生产库 → 目标文件解析不到 → 全库误判越界。"""
    p = src_dir / src_rel
    return len(read_lines(p)) if p.exists() else None


def proof_ranges(proof_path):
    """校对版段落覆盖范围列表 [(start, end), ...]。"""
    if not proof_path.exists():
        return []
    out = []
    for l in proof_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"> 第\d+段 \[原 第(\d+)-(\d+)行\]", l)
        if m:
            out.append((int(m.group(1)), int(m.group(2))))
    return out


def main():
    IDX_DIR, PROOF_DIR, SRC_DIR, snapshot = resolve_src(sys.argv[1:])
    as_json = "--json" in sys.argv
    idx_files = sorted(IDX_DIR.glob("*-idx.md"))
    if as_json:
        from collections import Counter
        from common import scan_sources, load_manifest
        man = load_manifest().get("files", {})
        if snapshot:
            scans = []
            idx_names = {p.name for p in idx_files}
            unindexed = [v.get("name") for v in man.values()
                         if not (IDX_DIR / f"{v.get('artifact_key') or Path(v.get('name', '')).stem}-idx.md").exists()]
        else:
            scans = scan_sources()
            idx_names = {p.name for p in idx_files}
            unindexed = [s["rel"] for s in scans if s["hash"] not in set(man)]
        dup = {k: c for k, c in Counter(v.get("name") for v in man.values()).items() if c > 1}
        bv = load_manifest().get("bv_map", {})
        bv_dup = {k: len(v) - len(set(v)) for k, v in bv.items() if len(v) != len(set(v))}
        orphan = [] if snapshot else [k for k in man if k not in {s["hash"] for s in scans}]
        total_refs = err_refs = warn_refs = 0
        bad_files = []
        if not snapshot:
            for p in idx_files:
                text = p.read_text(encoding="utf-8", errors="replace")
                src_m = re.search(r"^源文件: (.+)$", text, re.M)
                if not src_m:
                    bad_files.append((p.name, "无源文件字段")); continue
                total = total_lines_of(src_m.group(1).strip(), SRC_DIR)
                key_m = re.search(r"^产物键: (.+)$", text, re.M)
                key = key_m.group(1).strip() if key_m else Path(src_m.group(1).strip()).stem
                proof = proof_ranges(PROOF_DIR / (key + "_校对版.txt"))
                for m in re.finditer(r"原 第(\d+)-(\d+)行", text):
                    s, e = int(m.group(1)), int(m.group(2)); total_refs += 1
                    if total is None or e > total or s < 1 or s > e:
                        err_refs += 1; continue
                    if not any(not (pe < s or ps > e) for ps, pe in proof):
                        warn_refs += 1
        print(json.dumps({
            "snapshot": snapshot, "source_count": len(scans), "manifest_count": len(man),
            "idx_count": len(idx_files), "unindexed_count": len(unindexed), "unindexed": unindexed,
            "manifest_duplicate_names": len(dup), "bv_map_duplicate_count": sum(bv_dup.values()),
            "orphan_count": len(orphan), "total_refs": total_refs, "out_of_bounds": err_refs,
            "uncovered": warn_refs, "clean_files": len(idx_files) - len(bad_files), "bad_files": bad_files,
        }, ensure_ascii=False))
        return
    print(f"== 数据完整性检查（2026-08-16 扩展{' · 快照模式' if snapshot else ''}） ==")
    # A) 源文件全覆盖：源目录 txt（排除产物/测试）vs manifest 唯一文件
    from common import scan_sources, load_manifest
    if snapshot:
        # 快照模式：无源转录文件 → 按「台账 name 对应 idx 存在性」降级检查
        scans = []
        idx_names = {p.name for p in IDX_DIR.glob("*-idx.md")}
        man = load_manifest().get("files", {})
        man_names = {v.get("name") for v in man.values()}
        unindexed = [v.get("name") for v in man.values()
                     if not (IDX_DIR / f"{v.get('artifact_key') or Path(v.get('name', '')).stem}-idx.md").exists()]
        print(f"台账唯一 {len(man_names)} 个 | idx {len(idx_names)} 个 | 台账无对应 idx: {len(unindexed)}")
    else:
        scans = scan_sources()
        man = load_manifest().get("files", {})
        man_hashes = set(man)
        # 2026-08-16 修复：未索引判定改用 hash（原按 name，同名文件永不报未索引，掩盖台账折叠）
        unindexed = [s["rel"] for s in scans if s["hash"] not in man_hashes]
        print(f"源文件 {len(scans)} 个 | 台账唯一 {len(man_hashes)} 个 | 未索引: {len(unindexed)}")
    for u in unindexed:
        if not as_json:
            print(f"  ⚠ 未索引: {u}")
    # B) manifest 重复注册（同名多 hash）
    from collections import Counter
    man_names = {v.get("name") for v in man.values()}
    dup = {k: c for k, c in Counter(v.get("name") for v in man.values()).items() if c > 1}
    print(f"manifest 重复注册同名文件: {len(dup)}")
    for k, c in dup.items():
        print(f"  ⚠ {k} × {c}")
    # B2) 重复文件（同 hash 多路径，ADR-0009：仅索引第一份）——非快照模式才有源文件可比对
    if not snapshot:
        hash_cnt = Counter(s["hash"] for s in scans)
        dup_hash = [h for h, c in hash_cnt.items() if c > 1]
        print(f"重复文件（同 hash 多路径，仅索引第一份）: {len(dup_hash)} 组")
        for h in dup_hash:
            paths = sorted({s["rel"] for s in scans if s["hash"] == h})
            print(f"  ⚠ {paths[0]} 等 {len(paths)} 个路径")
    # C) bv_map 重复映射
    bv = load_manifest().get("bv_map", {})
    bv_dup = {k: len(v) - len(set(v)) for k, v in bv.items() if len(v) != len(set(v))}
    print(f"bv_map 重复映射: {sum(bv_dup.values())} 处")
    for k, c in bv_dup.items():
        print(f"  ⚠ {k} 重复 {c} 次")
    # D) 台账孤儿（manifest 有记录但源文件已不存在/内容已变——死记录，只增不减的副作用）
    if snapshot:
        print("台账孤儿: 跳过（快照模式无源文件可比对，请到运行目录体检）")
    else:
        scan_hash = {s["hash"] for s in scans}
        orphan = [k for k in man if k not in scan_hash]
        print(f"台账孤儿（源文件已删/已变）: {len(orphan)}")
        for k in orphan[:10]:
            v = man[k]
            print(f"  ⚠ {v.get('name', '')[:50]}（hash {k[:10]}…）")
    print()

    if snapshot:
        if as_json:
            print(json.dumps({"snapshot": True, "unindexed_count": len(unindexed),
                              "manifest_count": len(man), "idx_count": len(idx_names)}, ensure_ascii=False))
        else:
            print(f"== 快照模式：{SRC_DIR} 无源转录文件，行号体检跳过（请到运行目录验证行号） ==")
        return

    total_refs = err_refs = warn_refs = 0
    bad_files = []
    for p in idx_files:
        text = p.read_text(encoding="utf-8")
        src_rel = re.search(r"^源文件: (.+)$", text, re.M)
        if not src_rel:
            bad_files.append((p.name, "无源文件字段"))
            continue
        src_rel = src_rel.group(1).strip()
        total = total_lines_of(src_rel, SRC_DIR)
        artifact_m = re.search(r"^产物键: (.+)$", text, re.M)
        artifact = artifact_m.group(1).strip() if artifact_m else Path(src_rel).stem
        proof = proof_ranges(PROOF_DIR / (artifact + "_校对版.txt"))
        fe, fw = 0, 0
        for m in re.finditer(r"原 第(\d+)-(\d+)行", text):
            s, e = int(m.group(1)), int(m.group(2))
            total_refs += 1
            if total is None or e > total or s < 1 or s > e:
                err_refs += 1
                fe += 1
                continue
            # 与校对版段落覆盖重叠检测（锚点定位应落在正文内）
            covered = any(not (pe < s or ps > e) for ps, pe in proof)
            if not covered:
                warn_refs += 1
                fw += 1
        if fe or fw:
            bad_files.append((p.name, f"越界 {fe} / 未覆盖 {fw}"))
    n = len(idx_files)
    print(f"== 全库行号引用体检 ==")
    print(f"索引文件 {n} 个 | 行号引用 {total_refs} 处")
    print(f"严格越界（必然错误）: {err_refs} 处 ({err_refs / max(1, total_refs):.2%})")
    print(f"区间未覆盖校对版段落（疑似）: {warn_refs} 处 ({warn_refs / max(1, total_refs):.2%})")
    print(f"干净文件: {n - len(bad_files)} / {n}")
    if bad_files:
        print("\n异常文件清单:")
        for name, why in bad_files:
            print(f"  - {name}: {why}")
    else:
        print("\n全部干净 ✓")
    if as_json:
        print(json.dumps({
            "snapshot": False,
            "source_count": len(scans), "manifest_count": len(man),
            "unindexed_count": len(unindexed), "unindexed": unindexed,
            "manifest_duplicate_names": len(dup), "bv_map_duplicate_count": sum(bv_dup.values()),
            "orphan_count": len(orphan), "index_count": n, "total_refs": total_refs,
            "out_of_bounds": err_refs, "uncovered": warn_refs,
            "clean_files": n - len(bad_files), "bad_files": bad_files,
        }, ensure_ascii=False))
    if err_refs and not as_json:
        print("\n提示: 严格越界引用须人工修正（锚点定位不应产生越界，检查源文件是否被改）")


if __name__ == "__main__":
    main()
