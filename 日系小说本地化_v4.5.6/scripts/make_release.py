#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布打包：默认剔除样本正文（版权），保留风格卡与规则库。

用法：
  python scripts/make_release.py --out <输出目录>              # 默认剔除样本正文
  python scripts/make_release.py --out <输出目录> --dry-run     # 只报账不落盘
  python scripts/make_release.py --out <输出目录> --include-samples  # 仅供内部留档

打包后自动跑正文泄漏校验：逐样本取正文指纹串，在整个发布目录全文搜索，命中必须为 0。
"""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "references" / "样本库"
KEEP_IN_SAMPLES = {"SOURCE-NOTES.md"}

# 运行期/环境产物，一律不进发布包
EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".idea",
                "node_modules", ".process", "state", "output",
                "原文", "书导航", "段索引", ".semantic_index"}
EXCLUDE_FILES = {".index_state.db", ".env", "audit.jsonl", "qm.json", "accept-final.log"}
TEXT_EXT = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".ps1", ".bat", ".csv"}


def ignored(dir_path, names):
    out = []
    for n in names:
        p = Path(dir_path) / n
        if n in EXCLUDE_DIRS or n in EXCLUDE_FILES:
            out.append(n)
        elif p.is_file() and p.suffix == ".pyc":
            out.append(n)
    return out


def sample_fingerprints(min_len=48):
    """每份样本取一段正文指纹（跳过标题行），用于发布目录泄漏扫描。"""
    fps = []
    for p in sorted(SAMPLES.glob("*.md")):
        if p.name in KEEP_IN_SAMPLES:
            continue
        body = [l.strip() for l in p.read_text(encoding="utf-8", errors="replace").splitlines()[1:]]
        body = [l for l in body if len(l) >= min_len]
        if body:
            fps.append((p.name, body[len(body) // 2][:80]))
    return fps


def scan_leak(root):
    hits = []
    fps = sample_fingerprints()
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_EXT:
            continue
        t = p.read_text(encoding="utf-8", errors="replace")
        for name, fp in fps:
            if fp in t:
                hits.append((name, p.relative_to(root).as_posix()))
    return hits, len(fps)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-samples", action="store_true",
                    help="连样本正文一起打包（仅内部留档用，默认关闭）")
    a = ap.parse_args()

    def skip(dir_path, names):
        drop = ignored(dir_path, names)
        if not a.include_samples and Path(dir_path) == SAMPLES:
            drop += [n for n in names if n.endswith(".md") and n not in KEEP_IN_SAMPLES]
        return drop

    files = [p for p in ROOT.rglob("*")
             if p.is_file() and not any(part in EXCLUDE_DIRS for part in p.parts)]
    if not a.include_samples:
        files = [p for p in files if not (p.parent == SAMPLES and p.name not in KEEP_IN_SAMPLES)]
        excluded = [p for p in SAMPLES.glob("*.md") if p.name not in KEEP_IN_SAMPLES]
    else:
        excluded = []
    print(f"[release] 源文件 {len(files)} 个；剔除样本正文 {len(excluded)} 份"
          f"（{'含样本' if a.include_samples else '不含样本'}）")
    if a.dry_run:
        for p in excluded:
            print("   -", p.relative_to(ROOT).as_posix())
        print("[release] DRY_RUN：未写盘")
        return 0

    if a.out.exists():
        print(f"[release] 目标已存在，请先人工清理：{a.out}")
        return 2
    shutil.copytree(ROOT, a.out, ignore=skip)

    hits, n = scan_leak(a.out)
    print(f"[release] 泄漏扫描：指纹 {n} 条 → 命中 {len(hits)}")
    for name, where in hits[:10]:
        print("   !!", name, "→", where)
    print("SAMPLE_BODY_LEAK=0" if not hits else "SAMPLE_BODY_LEAK>0")
    return 0 if not hits else 1


if __name__ == "__main__":
    sys.exit(main())
