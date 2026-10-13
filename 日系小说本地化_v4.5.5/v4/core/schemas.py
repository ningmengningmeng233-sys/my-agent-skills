from pathlib import Path
from typing import Any

from .jsonio import read_json
from .paths import ARTIFACT_SCHEMA, MANIFEST_SCHEMA

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:
    raise RuntimeError(
        "v4 Core 需要 jsonschema；请运行 `uv sync` 或安装 v4 的核心依赖"
    ) from exc


class SchemaError(ValueError):
    pass


def _ascii_safe_id(value: Any) -> Any:
    """把 schema `$id` 中的非 ASCII 字符替换为 ``_``。

    设计文档的 ``$id`` 含中文路径（如 ``https://example.invalid/日系小说本地化/...``），
    在安装 rfc3986-validator 的环境中会让 metaschema 的 ``uri-reference`` 格式校验失败。
    ``$id`` 只是本地标识符，替换为 ASCII 不影响本项目的引用解析与结构校验。
    """
    if not isinstance(value, str):
        return value
    return "".join(ch if 0x20 <= ord(ch) < 0x7F else "_" for ch in value)


def _load_schema(path: Path) -> dict[str, Any]:
    schema = read_json(path)
    if isinstance(schema, dict) and "$id" in schema:
        schema["$id"] = _ascii_safe_id(schema["$id"])
    Draft202012Validator.check_schema(schema)
    return schema


def validate_manifest(value: dict[str, Any]) -> None:
    schema = _load_schema(MANIFEST_SCHEMA)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
    runtime_errors: list[str] = []
    for idx, chapter in enumerate(value.get("chapters", [])):
        state = chapter.get("state")
        if state not in {"passed", "approved", "delivered"}:
            continue
        if chapter.get("blockers"):
            runtime_errors.append(
                f"chapters[{idx}].state: passed/delivered 章节不得带有 blockers"
            )
        decisions = chapter.get("decisions") or {}
        if decisions.get("required_open"):
            runtime_errors.append(
                f"chapters[{idx}].decisions: passed/delivered 章节不得有未决开放决策"
            )
    if errors or runtime_errors:
        details = [f"{list(error.path)}: {error.message}" for error in errors[:5]]
        details.extend(runtime_errors[:5])
        raise SchemaError(f"manifest 校验失败: {'; '.join(details)}")


def artifact_schema(name: str) -> dict[str, Any]:
    schema = _load_schema(ARTIFACT_SCHEMA)
    try:
        return schema["$defs"][name]
    except KeyError as exc:
        raise SchemaError(f"未知旁车 schema: {name}") from exc


def validate_artifact(value: dict[str, Any], name: str) -> None:
    schema = artifact_schema(name)
    schema = dict(schema)
    schema["$defs"] = read_json(ARTIFACT_SCHEMA)["$defs"]
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    if errors:
        detail = "; ".join(f"{list(error.path)}: {error.message}" for error in errors[:5])
        raise SchemaError(f"{name} 校验失败: {detail}")
