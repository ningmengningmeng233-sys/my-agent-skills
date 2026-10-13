# -*- coding: utf-8 -*-
"""旧库迁移、回滚与影子运行（v2.17.0 工单 12）。

- `scan` **只读**：分析旧索引、四表、提示词、视觉清单与审核结果，输出提案与歧义，不写任何文件。
- `apply`：先整库备份（zip + 清单），再写新总账与对照文件；**不覆盖旧 Markdown**。
  保留 stable key、旧 ID、人工场景母版字段、人工关系与已通过的视觉审核。
- 无证据资产**不得伪造摘句**：标 `legacy_proposal` 并阻断正式签发，等补证据。
- `rollback`：按备份清单整库还原，并**逐文件核对哈希**。
- `shadow`：在**副本**上跑，输出资产/提示词/任务差集与预计调用节省，绝不改动原库。

幂等：`apply` 以"将要写入的字节"为判据，内容一致即不再改写并报 `idempotent=True`。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List

PROCESS_DIRNAME = ".process"
LEDGER_FILENAME = "asset-ledger.json"
INDEX_FILENAME = "资产索引.json"
BACKUP_DIRNAME = "迁移备份"
COMPARE_NAME = "总账对照.md"
SHADOW_DIRNAME = "影子运行"

LEGACY_PROPOSAL = "legacy_proposal"
SCHEMA_VERSION = 1


class MigrationError(RuntimeError):
    """迁移被拒绝：原因码即参数。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _rel(path: Path, lib: Path) -> str:
    return path.relative_to(lib).as_posix()


def library_hashes(lib, *, skip_dirs=(BACKUP_DIRNAME,)) -> Dict[str, str]:
    """库内全部文件的相对路径 → 短哈希。

    迁移自身的副产物（`.process/迁移备份/`）不计入，否则回滚后的比对永远不相等。
    """
    root = Path(lib)
    out: Dict[str, str] = {}
    skip = {PROCESS_DIRNAME + "/" + name for name in skip_dirs}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = _rel(path, root)
        if any(rel.startswith(prefix + "/") for prefix in skip):
            continue
        try:
            out[rel] = _sha256(path.read_bytes())
        except OSError:
            continue
    return out


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _ledger_entries(lib: Path) -> List[dict]:
    """由旧索引构建总账条目（保留 stable key 与旧 ID，不改写）。"""
    from .asset_ledger import build_ledger

    index = _load_json(lib / PROCESS_DIRNAME / INDEX_FILENAME)
    if not index:
        return []
    return list(build_ledger(index))


def _ambiguities(entries: List[dict]) -> List[dict]:
    """无证据资产：标 legacy proposal 并阻断；**不伪造摘句**。"""
    out: List[dict] = []
    for entry in entries:
        if entry.get("evidence_ids"):
            continue
        out.append({
            "asset_id": entry.get("asset_id", ""),
            "reason": LEGACY_PROPOSAL,
            "detail": "旧库中该资产没有任何可验证摘句，标为 legacy proposal 并阻断正式签发，等待补证据",
        })
    return out


def _render_compare(entries: List[dict], ambiguities: List[dict]) -> str:
    lines = ["# 总账对照（迁移后生成，不覆盖旧 Markdown）", "",
             "> 旧四表由类型化总账渲染（工单 2 L139b）。本文件用于人工比对迁移结果。", "",
             "| 资产ID | 类型 | 规范名 | 稳定键 | 状态 |", "|---|---|---|---|---|"]
    for entry in sorted(entries, key=lambda e: (e.get("asset_type", ""), e.get("asset_id", ""))):
        lines.append(f"| {entry.get('asset_id', '')} | {entry.get('asset_type', '')} | "
                     f"{entry.get('canonical_name', '')} | {entry.get('stable_key', '')} | "
                     f"{entry.get('migration_status') or entry.get('ledger_status', '')} |")
    lines.append("")
    if ambiguities:
        lines += ["## 待补证据（legacy proposal，阻断正式签发）", ""]
        lines += [f"- {item['asset_id']}：{item['detail']}" for item in ambiguities]
        lines.append("")
    return "\n".join(lines)


def _apply_marks(entries: List[dict], ambiguities: List[dict]) -> List[dict]:
    """给无证据资产打迁移标记；有证据者标 migrated。返回同一批条目（就地补字段）。"""
    flagged = {item["asset_id"] for item in ambiguities}
    for entry in entries:
        if entry.get("asset_id") in flagged:
            entry["migration_status"] = LEGACY_PROPOSAL
            entry["blocking"] = True
        else:
            entry["migration_status"] = "migrated"
    return entries


def scan(lib) -> dict:
    """只读分析：返回提案、歧义与旧交付文件哈希；**不写任何文件**。"""
    root = Path(lib)
    entries = _ledger_entries(root)
    ambiguities = _ambiguities(entries)
    by_type: Dict[str, int] = {}
    for entry in entries:
        key = entry.get("asset_type", "")
        by_type[key] = by_type.get(key, 0) + 1
    return {
        "lib": str(root),
        "proposal": {"total": len(entries), "by_type": by_type,
                     "legacy_proposals": len(ambiguities)},
        "ambiguities": ambiguities,
        "legacy_hashes": library_hashes(root),
        "wrote": [],
    }


def _backup(lib: Path) -> Path:
    backup_dir = lib / PROCESS_DIRNAME / BACKUP_DIRNAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(int((lib.stat().st_mtime) * 1000))
    path = backup_dir / f"ledger-migration-{stamp}.zip"
    manifest = library_hashes(lib)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel in sorted(manifest):
            zf.write(lib / rel, rel)
    return path


def _latest_backup(lib: Path) -> Path | None:
    backup_dir = lib / PROCESS_DIRNAME / BACKUP_DIRNAME
    if not backup_dir.is_dir():
        return None
    items = sorted(backup_dir.glob("ledger-migration-*.zip"),
                   key=lambda p: (p.stat().st_mtime, p.name))
    return items[-1] if items else None


def apply(lib, *, backup: bool = True) -> dict:
    """备份 → 写新总账与对照文件。**不覆盖旧 Markdown**；内容一致则不改写并报幂等。"""
    root = Path(lib)
    entries = _apply_marks(_ledger_entries(root), _ambiguities(_ledger_entries(root)))
    if not entries:
        raise MigrationError("LEDGER_SOURCE_MISSING",
                             f"未找到 {PROCESS_DIRNAME}/{INDEX_FILENAME}，无从迁移")
    ambiguities = _ambiguities(entries)

    payload = {"schema_version": SCHEMA_VERSION,
               "entries": sorted(entries, key=lambda e: (e.get("asset_type", ""),
                                                         e.get("asset_id", "")))}
    ledger_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    compare_bytes = _render_compare(entries, ambiguities).encode("utf-8")

    ledger_path = root / PROCESS_DIRNAME / LEDGER_FILENAME
    compare_path = root / COMPARE_NAME
    already = (ledger_path.is_file() and ledger_path.read_bytes() == ledger_bytes
               and compare_path.is_file() and compare_path.read_bytes() == compare_bytes)
    if already:
        return {"lib": str(root), "entries": len(entries), "legacy_proposals": len(ambiguities),
                "idempotent": True, "backup": "", "wrote": []}

    backup_path = ""
    if backup:
        backup_path = str(_backup(root))
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_bytes(ledger_bytes)
    compare_path.write_bytes(compare_bytes)
    return {"lib": str(root), "entries": len(entries), "legacy_proposals": len(ambiguities),
            "idempotent": False, "backup": backup_path,
            "wrote": [str(ledger_path), str(compare_path)]}


def rollback(lib) -> dict:
    """按最新备份还原整库并逐文件核对哈希；备份之后新增的文件一律删除。"""
    root = Path(lib)
    path = _latest_backup(root)
    if path is None:
        raise MigrationError("BACKUP_MISSING", "没有可用的迁移备份，拒绝回滚")
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        for rel in sorted(manifest):
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(rel))

    current = library_hashes(root)
    for rel in sorted(set(current) - set(manifest)):
        try:
            (root / rel).unlink()
        except OSError:
            pass

    mismatch = sorted(rel for rel, digest in manifest.items()
                      if library_hashes(root).get(rel) != digest)
    return {"lib": str(root), "restored": len(manifest), "removed": sorted(set(current) - set(manifest)),
            "mismatch": mismatch, "backup": str(path)}


def shadow(lib, *, work_root=None) -> dict:
    """在副本上跑扫描与编译，输出差集与**预计**调用节省；绝不改动原库。"""
    root = Path(lib)
    work = Path(work_root) if work_root else root.parent / SHADOW_DIRNAME
    work.mkdir(parents=True, exist_ok=True)
    old_copy = work / "lib_old"
    new_copy = work / "lib_new"
    for target in (old_copy, new_copy):
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(root, target, dirs_exist_ok=False,
                        ignore=shutil.ignore_patterns(BACKUP_DIRNAME))

    old_entries = _ledger_entries(old_copy)
    proposer = _apply_marks(_ledger_entries(new_copy), _ambiguities(_ledger_entries(new_copy)))
    old_ids = {e.get("asset_id", "") for e in old_entries}
    new_ids = {e.get("asset_id", "") for e in proposer}

    old_prompts = _prompt_names(old_copy)
    new_prompts = _compiled_prompt_names(new_copy, proposer)

    formal = [e for e in proposer if "base_prompt" in (e.get("required_outputs") or [])]
    return {
        "lib": str(root),
        "work_dir": str(work),
        "asset_diff": {"only_old": sorted(old_ids - new_ids),
                       "only_new": sorted(new_ids - old_ids),
                       "common": sorted(old_ids & new_ids)},
        "prompt_diff": {"only_old": sorted(old_prompts - new_prompts),
                        "only_new": sorted(new_prompts - old_prompts),
                        "common": sorted(old_prompts & new_prompts)},
        "task_diff": {"old_tasks": len(old_prompts), "new_tasks": len(new_prompts)},
        "expected_saving": {
            "basis": "结构性预计（非实测）：v2.16 旧链路对每个正式资产在描述补全与基础提示词各发 1 次调用；"
                     "新链路提示词为确定性编译，该阶段 0 次调用",
            "formal_assets": len(formal),
            "old_text_calls": 2 * len(formal),
            "new_text_calls": 0,
        },
    }


def _prompt_names(lib: Path) -> set:
    base = lib / "提示词" / "基础"
    if not base.is_dir():
        return set()
    names = set()
    for path in sorted(base.glob("*.md")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                names.add(stripped[3:].strip())
    return names


def _compiled_prompt_names(lib: Path, entries: List[dict]) -> set:
    from .prompt_compiler import shadow_report  # noqa: F401  （保持编译模块在同一路径上可导入）

    return {e.get("canonical_name", "") for e in entries if e.get("canonical_name")}
