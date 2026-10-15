import re
from pathlib import Path
from typing import Any

from .audit import append_audit, now_iso
from .hashing import sha256_bytes, sha256_file
from .jsonio import write_json_atomic
from .paths import RULES_PATH, STRUCTURAL_RULES_PATH
from .schemas import validate_manifest


def _empty_project_decisions() -> dict[str, Any]:
    return {
        "schema_version": "v4-project-decisions-1",
        "revision": 1,
        "entries": [],
    }


def _empty_output() -> dict[str, Any]:
    return {
        "path": "output/localized.md",
        "format": "markdown正文",
        "last_built_from_revision": 0,
    }


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _safe_project_id(name: str) -> str:
    """把工作区目录名转成合法 project_id；中文/空格目录名用稳定 ASCII 摘要。"""
    if _ID_RE.match(name):
        return name
    ascii_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")
    if _ID_RE.match(ascii_slug):
        return ascii_slug
    return "novel-" + sha256_bytes(name.encode("utf-8"))[:12]


def create_workspace(
    workspace: Path,
    *,
    title: str,
    author: str = "",
    input_language: str = "unknown",
) -> Path:
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"工作区必须为空: {workspace}")
    for relative in ("source", "chapters", "output"):
        (workspace / relative).mkdir(parents=True, exist_ok=True)

    decisions_path = workspace / "project-decisions.json"
    write_json_atomic(decisions_path, _empty_project_decisions())
    manifest = {
        "schema_version": "v4-core-1",
        "project_id": _safe_project_id(workspace.name),
        "project": {
            "title": title,
            "author": author,
            "input_language": input_language,
            "product_kind": "中国化中文小说成品",
            "legacy_source": "none",
        },
        "source": {
            "status": "pending",
            "kind": "file",
            "files": [],
            "boundary_status": "awaiting_confirmation",
        },
        "project_decisions": {
            "path": "project-decisions.json",
            "sha256": sha256_file(decisions_path),
            "revision": 1,
        },
        "rules": {
            "core_version": "v4-core-4",
            "dictionary_path": "v4/rules/dictionary.json",
            "dictionary_sha256": sha256_file(RULES_PATH),
        },
        "enhancements": {
            "reference_assets": {
                "enabled": False,
                "used": False,
                "description": "未启用索引库参考资产",
            },
            "blind_review": {
                "enabled": False,
                "required_for_pass": False,
            },
            "platform_review": {
                "enabled": False,
                "platforms": [],
            },
        },
        "chapters": [],
        "output": _empty_output(),
        "manifest_revision": 1,
        "updated_at": now_iso(),
    }
    validate_manifest(manifest)
    write_json_atomic(workspace / "manifest.json", manifest)
    (workspace / "audit.jsonl").touch()
    append_audit(
        workspace / "audit.jsonl",
        "workspace_created",
        project_id=_safe_project_id(workspace.name),
        manifest_revision=1,
        rules_sha256=manifest["rules"]["dictionary_sha256"],
        structural_rules_sha256=sha256_file(STRUCTURAL_RULES_PATH),
    )
    return workspace
