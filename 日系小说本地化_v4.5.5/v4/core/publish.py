"""发布拆章：把 delivered 章节按平台目录拆成单章文件。

只读取已交付章节，不修改 manifest 状态；输出
``output/publish/<platform>/NNN_标题.md`` 与 ``index.json``。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .audit import append_audit, now_iso
from .hashing import sha256_file
from .jsonio import write_json_atomic, write_text_atomic
from .manifest import ManifestStore

_ILLEGAL_FILENAME = re.compile(r'[<>:"/\\|?*\r\n\t]')


def _safe_title(title: str, limit: int = 40) -> str:
    cleaned = _ILLEGAL_FILENAME.sub("_", title).strip(" .")
    return cleaned[:limit] or "未命名"


def split_publish(
    workspace: Path | str,
    *,
    platform: str = "generic",
    allow_incomplete: bool = False,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 passed/delivered 章节输出拆到平台发布目录。

    返回 {ok, out_dir, files, issues}。默认拒绝任何不可交付章节。
    """
    workspace = Path(workspace)
    store = ManifestStore(workspace)
    manifest = manifest or store.load()
    out_dir = workspace / "output" / "publish" / _safe_title(platform, 20)
    chapters = sorted(manifest.get("chapters", []), key=lambda c: c.get("ordinal", 1))
    issues: list[str] = []
    ready: list[dict[str, Any]] = []
    for chapter in chapters:
        if chapter["state"] not in {"passed", "delivered"}:
            issues.append(f"{chapter['id']}: state={chapter['state']} 不可发布")
        elif not (workspace / chapter["paths"]["output"]).is_file():
            issues.append(f"{chapter['id']}: output.md 缺失")
        else:
            ready.append(chapter)
    if issues and not allow_incomplete:
        return {"ok": False, "out_dir": out_dir, "files": [], "issues": issues}

    out_dir.mkdir(parents=True, exist_ok=True)

    # 交付完整性：失败时目录必须与发布前**逐字节一致**。
    # 原先做法是「先删旧 *.md，再逐个写」，中途写失败就只剩半本，而旧 index.json
    # 仍在声称有全量文件——发布方按目录取件会拿到残缺的一本书（实测确认）。
    # 现在：先快照现有文件 → 全部新文件原子写入 → 全部成功后才清陈旧文件并写 index；
    # 任一步失败则回滚快照（写回被覆盖的、删除新增的）。
    snapshot: dict[str, bytes] = {
        item.name: item.read_bytes() for item in out_dir.glob("*") if item.is_file()
    }
    written: list[str] = []
    files: list[dict[str, str]] = []
    try:
        for chapter in ready:
            kind = chapter.get("kind", "body")
            prefix = "附录_" if kind == "appendix" else ""
            filename = f"{chapter['ordinal']:03d}_{prefix}{_safe_title(chapter.get('title', ''))}.md"
            text = (workspace / chapter["paths"]["output"]).read_text(encoding="utf-8")
            target = out_dir / filename
            write_text_atomic(target, text)
            written.append(filename)
            files.append(
                {
                    "chapter_id": chapter["id"],
                    "ordinal": chapter["ordinal"],
                    "kind": kind,
                    "filename": filename,
                    "sha256": sha256_file(target),
                }
            )
    except Exception:
        for name in written:
            if name not in snapshot:
                (out_dir / name).unlink(missing_ok=True)
            else:
                try:
                    (out_dir / name).write_bytes(snapshot[name])
                except OSError:
                    pass
        raise

    # 全部新文件就绪后才清陈旧章节文件（失败路径不再破坏上一次的可用发布）
    fresh = set(written)
    for stale in out_dir.glob("*.md"):
        if stale.name not in fresh:
            stale.unlink()

    index = {
        "schema_version": "v4-publish-1",
        "generated_at": now_iso(),
        "platform": platform,
        "allow_incomplete": allow_incomplete,
        "files": files,
    }
    write_json_atomic(out_dir / "index.json", index)
    append_audit(
        workspace / "audit.jsonl",
        "publish_split",
        platform=platform,
        file_count=len(files),
        out_dir=str(out_dir),
    )
    return {"ok": True, "out_dir": out_dir, "files": files, "issues": issues}
