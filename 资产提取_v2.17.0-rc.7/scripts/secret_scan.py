#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库级密钥扫描（阶段 8 工单 8.3 发布级自动门）。

- 判定规则**复用** `scripts/build_release.py` 里的发布级正则与占位值白名单（单一事实源），
  不另立一套标准。
- **只输出计数与相对路径，绝不回显疑似密钥内容**（命中时也只给长度与位置）。
- 默认跳过 `.git`、`资产库`、`测试/e2e`、`references/legacy` 等非交付内容。

用法：
    python scripts/secret_scan.py [--root 路径] [--quiet]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from build_release import (  # noqa: E402
    SECRET_PATTERNS,
    TEXT_SCAN_SUFFIXES,
    _looks_like_placeholder,
)

SKIP_PARTS = {"__pycache__", ".git", ".pytest_cache", ".idea", ".vscode", "node_modules"}
SKIP_REL_PREFIXES = ("资产库", "测试/e2e", "references/legacy", "docs/验收")


def scan(root: Path) -> tuple[int, list[str]]:
    """扫描文本文件，返回（命中条数, 命中的相对路径清单）。"""
    hits: list[str] = []
    scanned = 0
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SCAN_SUFFIXES:
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_PARTS for part in path.parts[len(Path(root).parts):]):
            continue
        if any(rel.startswith(prefix) for prefix in SKIP_REL_PREFIXES):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                if _looks_like_placeholder(match.group(0)):
                    continue
                hits.append(rel)
                break
    return scanned, sorted(set(hits))


def scan_source(root: Path) -> int:
    """供自动门调用的入口：命中即非零，只打印计数与路径。"""
    scanned, hits = scan(root)
    if hits:
        print(f"❌ 密钥扫描：扫描 {scanned} 个文本文件，命中 {len(hits)} 处（不回显内容）")
        for rel in hits:
            print(f"   - {rel}")
        return 1
    print(f"✅ 密钥扫描：扫描 {scanned} 个文本文件，命中 0")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="仓库级密钥扫描（只计数不回显）")
    parser.add_argument("--root", default=str(SKILL_DIR))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    scanned, hits = scan(Path(args.root))
    if args.quiet and not hits:
        return 0
    print(f"扫描 {scanned} 个文本文件，命中 {len(hits)} 处（不回显内容）")
    for rel in hits:
        print(f"  - {rel}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
