from pathlib import Path
from typing import Any

from .hashing import sha256_file, sha256_json
from .paths import RULES_PATH, TONE_RULES_PATH


def hash_optional_file(path: Path) -> str:
    return sha256_file(path) if path.exists() else "0" * 64


def rules_fingerprint() -> str:
    """规则指纹 = dictionary.json 与 tone-scan.json 的联合哈希。

    任何一份规则文件变化都会让已交付章节过期，避免腔调规则更新后旧成品被误认为有效。
    """
    return sha256_json(
        {
            "dictionary": hash_optional_file(RULES_PATH),
            "tone_scan": hash_optional_file(TONE_RULES_PATH),
        }
    )


def chapter_fingerprints(
    *,
    source: Path,
    events: Path,
    project_decisions: Path,
    chapter_decisions: Path,
    rules: Path,
    output: Path,
    checks: Path,
) -> dict[str, str]:
    values = {
        "source": hash_optional_file(source),
        "events": hash_optional_file(events),
        "project_decisions": hash_optional_file(project_decisions),
        "chapter_decisions": hash_optional_file(chapter_decisions),
        "rules": rules_fingerprint(),
        "output": hash_optional_file(output),
        "checks": hash_optional_file(checks),
    }
    values["effective"] = sha256_json(values)
    return values


def changed_inputs(
    recorded: dict[str, str], current: dict[str, str], *, include_output: bool = False
) -> set[str]:
    keys = set(recorded) | set(current)
    if not include_output:
        keys -= {"output", "checks", "effective"}
    return {key for key in keys if recorded.get(key) != current.get(key)}


def stale_input_chapters(
    manifest: dict[str, Any], workspace: Path | str
) -> list[tuple[str, set[str]]]:
    """返回输入指纹与 manifest 记录不一致的章节列表。"""
    workspace = Path(workspace)
    rules_sha256 = rules_fingerprint()
    project_decisions_path = workspace / manifest["project_decisions"]["path"]
    stale: list[tuple[str, set[str]]] = []
    for chapter in manifest.get("chapters", []):
        recorded = chapter.get("fingerprints", {})
        current = {
            "source": hash_optional_file(workspace / chapter["paths"]["source"]),
            "events": hash_optional_file(workspace / chapter["paths"]["events"]),
            "project_decisions": hash_optional_file(project_decisions_path),
            "chapter_decisions": hash_optional_file(
                workspace / chapter["paths"]["decisions"]
            ),
            "rules": rules_sha256,
        }
        state = chapter.get("state")
        if state in {"awaiting_checks", "awaiting_review", "passed", "delivered"}:
            current["output"] = hash_optional_file(
                workspace / chapter["paths"]["output"]
            )
            current["checks"] = hash_optional_file(
                workspace / chapter["paths"]["checks"]
            )
        changed = {key for key in current if recorded.get(key) != current[key]}
        if changed:
            stale.append((chapter["id"], changed))
    return stale
