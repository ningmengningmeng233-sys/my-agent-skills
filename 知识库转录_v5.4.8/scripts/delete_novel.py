#!/usr/bin/env python3
"""删除资料/长书的所有索引产物、语义缓存与断点记录。
支持独立文档集两级删除（整集删除 vs 单篇文档删除）。
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from _common import book_key
from _logging import add_log_format_argument, configure_logging, get_logger
from index_db import IndexDB


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="删除一本书或资料集的索引产物与断点记录")
    parser.add_argument("--novel", required=True, help="资料/书籍名称（如「独立文档集」）")
    parser.add_argument("--author", default="佚名", help="作者名称（默认佚名）")
    parser.add_argument("--doc", help="单篇独立文档名（仅在删除独立文档集中的单篇时使用）")
    parser.add_argument("--lib", required=True, help="索引库根目录")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过二次确认直接删除")
    add_log_format_argument(parser)
    return parser.parse_args()


def _prefixed_files(directory: Path, prefix: str, suffix: str | None = None) -> list[Path]:
    """字面量前缀匹配，替代 glob。"""
    if not directory.is_dir():
        return []
    out = []
    for p in directory.iterdir():
        if not p.is_file() or not p.name.startswith(prefix):
            continue
        if suffix is None or p.name.endswith(suffix):
            out.append(p)
    return sorted(out)


def _collect_paths(lib: Path, key: str, doc: str | None = None) -> list[Path]:
    paths: list[Path] = []

    if doc:
        # 单篇独立文档删除
        candidate_dirs = [lib / "原文" / key, lib / "原文" / "独立文档集"]
        for r_dir in candidate_dirs:
            if r_dir.is_dir():
                for p in r_dir.iterdir():
                    if p.is_file() and (p.name.startswith(f"{key}-{doc}-") or p.name.startswith(f"独立文档集-{doc}-") or p.name.startswith(f"{doc}-")):
                        paths.append(p)
        idx_dir = lib / "段索引"
        for prefix in (f"{key}-{doc}-", f"独立文档集-{doc}-", f"独立文档集-佚名-{doc}-", f"{doc}-"):
            paths.extend(_prefixed_files(idx_dir, prefix, "-idx.md"))
        semantic_dir = lib / ".semantic_index"
        for prefix in (f"{key}-{doc}-", f"独立文档集-{doc}-", f"独立文档集-佚名-{doc}-", f"{doc}-"):
            paths.extend(_prefixed_files(semantic_dir, prefix, ".npz"))
        return sorted(list(set(paths)))

    raw_dirs = [lib / "原文" / key, lib / "原文" / "独立文档集"] if "独立文档集" in key else [lib / "原文" / key]
    for r_dir in raw_dirs:
        if r_dir.exists() and r_dir not in paths:
            paths.append(r_dir)

    idx_dir = lib / "段索引"
    paths.extend(_prefixed_files(idx_dir, key + "-", "-idx.md"))

    nav_file = lib / "书导航" / f"{key}.md"
    if nav_file.exists():
        paths.append(nav_file)

    semantic_dir = lib / ".semantic_index"
    paths.extend(_prefixed_files(semantic_dir, key + "-", ".npz"))

    return sorted(paths)


def _prune_semantic_manifest(lib: Path, key: str) -> int:
    """移除 manifest 中属于该书的条目，返回移除条数。"""
    manifest_path = lib / ".semantic_index" / "manifest.json"
    if not manifest_path.exists():
        return 0
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    segments = manifest.get("segments", [])
    kept = [
        s for s in segments
        if not (str(s.get("source_index", "")).startswith(key + "-")
                or str(s.get("npz", "")).startswith(key + "-"))
    ]
    removed = len(segments) - len(kept)
    if removed:
        manifest["segments"] = kept
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return removed


def _confirm(novel: str, author: str, paths: list[Path], doc: str | None = None) -> bool:
    if novel == "独立文档集" and not doc:
        print("⚠️【高危操作警告】即将删除整套《独立文档集》及其所有独立文档产物！")
    elif doc:
        print(f"即将删除独立文档《{doc}》（属于 {novel}）的相关产物：")
    else:
        print(f"即将删除《{novel}》- {author} 的所有产物：")
    for p in paths:
        print(f"  - {p}")
    answer = input("确认删除？(yes/no): ").strip().lower()
    return answer in ("yes", "y")


def main() -> int:
    args = _parse_args()
    configure_logging(fmt=args.log_format)
    logger = get_logger("delete_novel")

    try:
        key = book_key(args.novel, args.author)
    except ValueError as e:
        logger.error("%s", e)
        return 1

    lib = Path(args.lib).resolve()
    if not lib.exists():
        logger.error("索引库不存在: %s", lib)
        return 1

    paths = _collect_paths(lib, key, doc=args.doc)
    if not paths:
        target_desc = f"独立文档《{args.doc}》" if args.doc else f"《{args.novel}》- {args.author}"
        logger.warning("未找到 %s 的任何产物", target_desc)
        return 0

    if not args.yes and not _confirm(args.novel, args.author, paths, doc=args.doc):
        logger.info("用户取消删除")
        return 0

    failed = 0
    for p in paths:
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            logger.info("已删除: %s", p)
        except OSError as e:
            failed += 1
            logger.error("删除失败 %s: %s", p, e)

    filter_prefix = f"{key}-{args.doc}" if args.doc else key
    try:
        removed = _prune_semantic_manifest(lib, filter_prefix)
        if removed:
            logger.info("已从语义索引清单移除 %d 条记录", removed)
    except OSError as e:
        logger.warning("清理语义索引清单失败: %s", e)

    try:
        with IndexDB(str(lib)) as db:
            db_rows = db.delete_by_prefix(filter_prefix)
            logger.info("已清理 %d 条断点记录", db_rows)
    except Exception as e:
        failed += 1
        logger.error("清理断点库失败: %s", e)

    # 若删除了独立文档单篇，且该独立文档集还存在剩余索引，则自动刷新书导航；若已无剩余则清理空导航
    if args.doc:
        remaining = list((lib / "段索引").glob("独立文档集-*-idx.md"))
        if remaining:
            from build_novel_index import build_novel_index
            build_novel_index(args.novel, args.author, str(lib), force=True)
        else:
            for nav_file in [(lib / "书导航" / "独立文档集.md"), (lib / "书导航" / f"{key}.md")]:
                if nav_file.exists():
                    nav_file.unlink()
                    logger.info("已清理空虚拟书书导航: %s", nav_file)

    if failed:
        logger.error("删除完成但有 %d 个失败项", failed)
        return 1
    logger.info("删除完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
