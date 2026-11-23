#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入规范化入口（规格 4）：叙事文本 → 带来源定位的 UTF-8 Markdown。

用法：
    python scripts/normalize_sources.py <源1> [源2 ...] --lib <资产库> [--order-file <清单>]

产物写入 `<资产库>/.process/normalized_sources/`：
- `规范化原文.md`：真实拆段输入
- `来源清单.json`：原文件哈希、顺序清单哈希、规范化文件哈希
原文件只读，命令可重复执行且结果逐字节一致。
"""
import argparse
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))

from engine.source_normalizer import (  # noqa: E402
    SourceNormalizeError,
    normalize_sources,
    normalized_dir,
    write_normalized,
)


def run_normalize(sources: list[Path], lib: Path, order_file: Path | None,
                  quiet: bool = False) -> tuple[int, dict]:
    """执行规范化并写盘。返回 (退出码, 产物路径字典)。"""
    try:
        result = normalize_sources(sources, order_file)
    except SourceNormalizeError as e:
        print(f"❌ 输入规范化失败（{e.code}）：{e.detail}")
        return 1, {}
    paths = write_normalized(result, normalized_dir(lib))
    if not quiet:
        print(f"✅ 规范化完成: {len(result.units)} 个段落 / {len(result.manifest['sources'])} 个来源")
        print(f"   顺序来源: {result.manifest['order_source']}")
        for meta in result.manifest["sources"]:
            print(f"   [{meta['order']}] {meta['name']}（{meta['format']}，{meta['bytes']} 字节，"
                  f"{meta['unit_count']} 段）sha256:{meta['sha256'][:16]}…")
        print(f"   规范化原文: {paths['markdown']}")
        print(f"   来源清单: {paths['manifest']}")
    return 0, paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="输入规范化：TXT/Markdown/DOCX/EPUB 与多文件项目 → 可追溯 UTF-8 Markdown")
    parser.add_argument("sources", nargs="+", help="源文件路径（按传入顺序；多文件请配合 --order-file）")
    parser.add_argument("--lib", required=True, help="资产库根目录（产物写 .process/normalized_sources/）")
    parser.add_argument("--order-file", help="顺序清单：每行一个相对路径，相对清单所在目录解析")
    args = parser.parse_args()

    rc, _paths = run_normalize(
        [Path(s) for s in args.sources],
        Path(args.lib),
        Path(args.order_file) if args.order_file else None,
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
