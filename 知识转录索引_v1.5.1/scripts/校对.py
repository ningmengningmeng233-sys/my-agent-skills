# -*- coding: utf-8 -*-
"""转录索引 · 校对（规则双轨之一）

ADR-0003 校对双轨：本脚本产出「校对版」（阅读终点）——忠实、可复现、零成本。
步骤：opencc 繁转简 → 术语表替换 → Whisper 断行合并成段落（累计字符断段）。
每段头标注原始行号范围，实现「双坐标」：校对版段落 ↔ 原始文件行号。

用法: python 校对.py [文件名相对源目录]   # 缺省 = 全库增量
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (PROOF_DIR, TERM_TABLE, artifact_key, artifact_paths,
                    assign_artifact_keys, classify_new, ensure_dirs, load_manifest,
                    read_lines, refresh_moved, save_manifest, scan_sources)

SEG_CHARS = 260          # 段落累计字符阈值
HEADER_SEP = "======"    # 元数据分隔线


def load_term_table():
    """读术语纠错表：每行 `错字 → 正确`，返回 [(错, 对), ...]（按错字长度降序，避免部分替换）。"""
    if not TERM_TABLE.exists():
        return []
    pairs = []
    for line in TERM_TABLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "→" not in line:
            continue
        wrong, right = line.split("→", 1)
        pairs.append((wrong.strip(), right.strip()))
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def fix_terms(text, pairs):
    """按词界替换术语；英文源词不允许嵌入更长单词，避免 Agen→Agent 等子串污染。"""
    for wrong, right in pairs:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", wrong):
            pattern = rf"(?<![A-Za-z0-9]){re.escape(wrong)}(?![A-Za-z0-9])"
            text = re.sub(pattern, right, text)
        else:
            text = text.replace(wrong, right)
    return text


def segmentize(items):
    """items: [(原始行号, 文本), ...] → 返回 [(start, end, text), ...]（行号 1 起，闭区间）。"""
    segs, cur, cur_len, s = [], [], 0, items[0][0]
    for lineno, t in items:
        cur.append(t)
        cur_len += len(t)
        if cur_len >= SEG_CHARS:
            segs.append((s, lineno, "".join(cur)))
            cur, cur_len, s = [], 0, lineno + 1
    if cur:
        segs.append((s, items[-1][0], "".join(cur)))
    return segs


def build_proof(src_abs, term_pairs):
    """读原始转录 → 返回 (段列表, 校对版全文)。"""
    lines = read_lines(src_abs)

    # 定位正文起点：「第二部分：完整原文」之后，跳过空行与纯分隔线（如 ====）
    body_start = 0
    for i, line in enumerate(lines):
        if "第二部分" in line:
            body_start = i + 1
            break
    while body_start < len(lines):
        t = lines[body_start].strip()
        if t and set(t) <= {"=", "-"}:
            body_start += 1
            continue
        if not t:
            body_start += 1
            continue
        break

    # 头部元数据也转简体（保留原样除行首空格）
    head = [fix_terms(opencc_convert(l), term_pairs).strip() for l in lines[:body_start]]
    # 正文保留真实行号，过滤纯分隔线/文档结束标记（段号与原始行号严格对应）
    body = []
    for idx, l in enumerate(lines[body_start:], start=body_start):
        t = fix_terms(opencc_convert(l), term_pairs).strip()
        if not t or set(t) <= {"=", "-"} or "文档结束" in t:
            continue
        body.append((idx + 1, t))

    segs = segmentize(body)

    out = head + [""]
    for idx, (s, e, text) in enumerate(segs, 1):
        out.append(f"> 第{idx}段 [原 第{s}-{e}行]")
        out.append(text)
        out.append("")
    out.append("")
    out.append(HEADER_SEP)
    out.append("文档结束")
    return segs, "\n".join(out)


def opencc_convert(text):
    """繁转简（opencc t2s 转换器惰性单例）。"""
    if not opencc_convert.converter:
        from opencc import OpenCC
        opencc_convert.converter = OpenCC("t2s")
    return opencc_convert.converter.convert(text)


opencc_convert.converter = None


def process_one(rel, abs_path, term_pairs, digest=None, artifact=None):
    segs, text = build_proof(abs_path, term_pairs)
    key = artifact or artifact_key(rel, digest or "unknown")
    _, out_path = artifact_paths(key)
    out_path.write_text(text, encoding="utf-8")
    return segs, out_path, key


def main():
    force = "--force" in sys.argv
    only = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    ensure_dirs()
    mf = load_manifest()
    scans = scan_sources()
    if only:
        scans = [s for s in scans if s["rel"] == only]
    if "--dry-run" in sys.argv:
        cls, _ = classify_new(mf, scans)
        skipped = __import__("common").scan_skips()
        print(f"预览：新增 {len(cls['new'])} / 变更 {len(cls['changed'])} / 移动 {len(cls['moved'])} / 跳过 {len(cls['skip'])} / 边界跳过 {len(skipped)}")
        for group in ("new", "changed", "moved"):
            for item in cls[group]: print(f"  {group}: {item['rel']}")
        return
    if force:
        todo = scans
        assign_artifact_keys(todo, mf)
    else:
        cls, mf = classify_new(mf, scans)
        refresh_moved(cls["moved"], mf)
        save_manifest(mf)
        todo = cls["new"] + cls["changed"]
    term_pairs = load_term_table()

    if not todo:
        if force:
            print("校对：强制模式，无匹配文件")
        else:
            print(f"校对：无新文件需处理（新增 {len(cls['new'])} / 跳过 {len(cls['skip'])}）")
        return
    if not force:
        print(f"校对：{len(todo)} 个文件（新增 {len(cls['new'])}，变更 {len(cls['changed'])}，移动 {len(cls['moved'])}）")
        for item in cls["dup_hash"]:
            print(f"  ⚠ 同内容副本跳过：{item['rel']}")
        for bv, names in cls["dup_bv"]:
            print(f"  ⚠ 同 BV 重复（仅提示）：{bv}：{'、'.join(names)}")
    else:
        print(f"校对：强制重跑 {len(todo)} 个文件")
    failed = 0
    for s in todo:
        try:
            segs, out_path, key = process_one(s["rel"], s["abs"], term_pairs, s["hash"], s.get("artifact_key"))
            print(f"  ✓ {s['name']} → {len(segs)} 段 → {out_path.name}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {s['name']}: {e}")
    if failed:
        print(f"校对失败：{failed} 个文件，已停止后续索引阶段")
        sys.exit(1)


if __name__ == "__main__":
    main()
