#!/usr/bin/env python3
"""Fail-Closed Version Gate and Git State Validation for MiniMax H3 Craft."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from typing import Any, Dict, Optional

SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<buildmetadata>[0-9A-Za-z.-]+))?$"
)

# Duration policy contract constants (validation whitelists, NOT default values).
DURATION_POLICY_SCHEMA_VERSION = 1
DURATION_STATUSES = frozenset(
    {"PASS_EXACT", "PASS_WITH_TRIM", "REVIEW_REQUIRED", "FAIL"}
)
PADDING_MODES = frozenset({"selective"})
DURATION_RANGE_FIELDS = ("clip_seconds", "speech_chars_per_second")


class VersionValidationError(RuntimeError):
    """Raised when version.json or git state fails validation."""
    pass


def parse_semver(version_str: str) -> Dict[str, Any]:
    """Parse and validate Semantic Version string.
    
    Raises:
        VersionValidationError: If version_str is not valid SemVer.
    """
    if not isinstance(version_str, str) or not version_str.strip():
        raise VersionValidationError(f"Invalid SemVer string: {version_str!r}")
    m = SEMVER_PATTERN.match(version_str.strip())
    if not m:
        raise VersionValidationError(
            f"Version {version_str!r} does not adhere to Semantic Versioning (MAJOR.MINOR.PATCH[-PRERELEASE])"
        )
    return m.groupdict()


def load_version_info(
    repo_root: Optional[str] = None,
    enforce_git: bool = False,
    allow_dirty: bool = False,
    require_clean_git: bool = False,
) -> Dict[str, Any]:
    """Load config/version.json with Fail-Closed semantics.
    
    NEVER falls back to default/cached version on error.
    
    Args:
        repo_root: Repository root path (default: infer from script location).
        enforce_git: If True, validate git repository state (repo, branch, commit).
        allow_dirty: If True and enforce_git is True, allow uncommitted changes.
        require_clean_git: If True, fail if working tree is dirty.
        
    Returns:
        Dict containing version info and optional git metadata:
        {
            "version": "7.1.3",
            "release_name": "v7.1.3-stabilization",
            "updated_at": "...",
            "release_notes": "...",
            "git_commit": "...",
            "git_branch": "...",
            "git_dirty": bool,
        }
        
    Raises:
        VersionValidationError: If file missing, corrupted, invalid SemVer, or git mismatch.
    """
    if repo_root is None:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    version_path = os.path.join(repo_root, "config", "version.json")
    if not os.path.isfile(version_path):
        raise VersionValidationError(f"Required version configuration file missing: {version_path}")

    try:
        with open(version_path, "r", encoding="utf-8") as f:
            raw_content = f.read()
    except UnicodeDecodeError as exc:
        raise VersionValidationError(f"version.json is not valid UTF-8: {exc}") from exc
    except Exception as exc:
        raise VersionValidationError(f"Failed to read version.json: {exc}") from exc

    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise VersionValidationError(f"Corrupted version.json (invalid JSON syntax): {exc}") from exc

    if not isinstance(data, dict):
        raise VersionValidationError("version.json top-level structure must be a JSON object")

    version = data.get("version")
    if not version or not isinstance(version, str):
        raise VersionValidationError("version.json missing required non-empty string field 'version'")

    parse_semver(version)

    release_name = data.get("release_name")
    if not release_name or not isinstance(release_name, str):
        raise VersionValidationError("version.json missing required non-empty string field 'release_name'")

    result: Dict[str, Any] = {
        "version": version,
        "release_name": release_name,
        "updated_at": data.get("updated_at", ""),
        "release_notes": data.get("release_notes", ""),
    }

    if enforce_git:
        git_info = get_git_state(repo_root)
        if git_info.get("is_detached"):
            raise VersionValidationError("Git HEAD is detached; releases require a checked-out branch")

        branch = git_info.get("git_branch")
        if branch != release_name:
            raise VersionValidationError(
                f"Active git branch '{branch}' does not match config/version.json release_name '{release_name}'"
            )

        if require_clean_git and not allow_dirty and git_info.get("is_dirty"):
            raise VersionValidationError(
                "Git working tree has uncommitted changes. Releases require a clean working tree."
            )

        result.update(git_info)

    return result


def _is_finite_number(value: Any) -> bool:
    """True only for real, finite ints/floats (bool and NaN/Infinity excluded)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _reject_json_constant(token: str) -> Any:
    """json.loads hook rejecting NaN/Infinity/-Infinity literals outright."""
    raise VersionValidationError(
        f"config/duration-policy.json contains forbidden non-finite JSON literal: {token}"
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    """json.loads hook rejecting duplicate keys instead of keeping the last one.

    A single source of truth must not silently resolve two contradicting
    declarations of the same key in favour of whichever appears last.
    """
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VersionValidationError(
                f"duration-policy.json declares key {key!r} more than once; "
                "every key must appear exactly once"
            )
        result[key] = value
    return result


def _validate_duration_policy_document(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a parsed duration policy document; returns the same object.

    Raises:
        VersionValidationError: If any field is missing, mistyped, or out of range.
    """
    schema_version = data.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise VersionValidationError(
            "duration-policy.json field 'schema_version' must be an integer, "
            f"got {schema_version!r}"
        )
    if schema_version != DURATION_POLICY_SCHEMA_VERSION:
        raise VersionValidationError(
            f"Unsupported duration-policy.json schema_version {schema_version!r}; "
            f"supported version is {DURATION_POLICY_SCHEMA_VERSION}"
        )

    for field in DURATION_RANGE_FIELDS:
        value = data.get(field)
        if not isinstance(value, dict):
            raise VersionValidationError(
                f"duration-policy.json field '{field}' must be a JSON object, "
                f"got {type(value).__name__}"
            )
        keys = set(value.keys())
        if keys != {"min", "max"}:
            raise VersionValidationError(
                f"duration-policy.json field '{field}' must contain exactly the keys "
                f"'min' and 'max', got {sorted(str(k) for k in keys)}"
            )
        minimum = value["min"]
        maximum = value["max"]
        if not _is_finite_number(minimum):
            raise VersionValidationError(
                f"duration-policy.json field '{field}.min' must be a finite number, "
                f"got {minimum!r}"
            )
        if not _is_finite_number(maximum):
            raise VersionValidationError(
                f"duration-policy.json field '{field}.max' must be a finite number, "
                f"got {maximum!r}"
            )
        if not 0 < minimum <= maximum:
            raise VersionValidationError(
                f"duration-policy.json field '{field}' requires 0 < min <= max, "
                f"got min={minimum!r}, max={maximum!r}"
            )

    ratio = data.get("overage_review_ratio")
    if not _is_finite_number(ratio):
        raise VersionValidationError(
            "duration-policy.json field 'overage_review_ratio' must be a finite number, "
            f"got {ratio!r}"
        )
    if not 0 < ratio < 1:
        raise VersionValidationError(
            "duration-policy.json field 'overage_review_ratio' must satisfy 0 < ratio < 1, "
            f"got {ratio!r}"
        )

    padding = data.get("padding_policy")
    if not isinstance(padding, dict):
        raise VersionValidationError(
            "duration-policy.json field 'padding_policy' must be a JSON object, "
            f"got {type(padding).__name__}"
        )
    mode = padding.get("mode")
    if not isinstance(mode, str) or not mode.strip():
        raise VersionValidationError(
            "duration-policy.json field 'padding_policy.mode' must be a non-empty string, "
            f"got {mode!r}"
        )
    if mode not in PADDING_MODES:
        raise VersionValidationError(
            f"Unsupported duration-policy.json padding_policy.mode {mode!r}; "
            f"supported modes: {sorted(PADDING_MODES)}"
        )
    padding_min = padding.get("min_seconds")
    padding_max = padding.get("max_seconds")
    if not _is_finite_number(padding_min):
        raise VersionValidationError(
            "duration-policy.json field 'padding_policy.min_seconds' must be a finite number, "
            f"got {padding_min!r}"
        )
    if not _is_finite_number(padding_max):
        raise VersionValidationError(
            "duration-policy.json field 'padding_policy.max_seconds' must be a finite number, "
            f"got {padding_max!r}"
        )
    if not 0 <= padding_min <= padding_max:
        raise VersionValidationError(
            "duration-policy.json field 'padding_policy' requires 0 <= min_seconds <= max_seconds, "
            f"got min_seconds={padding_min!r}, max_seconds={padding_max!r}"
        )

    statuses = data.get("statuses")
    if not isinstance(statuses, list) or not statuses:
        raise VersionValidationError(
            "duration-policy.json field 'statuses' must be a non-empty JSON list, "
            f"got {statuses!r}"
        )
    seen = set()
    for item in statuses:
        if not isinstance(item, str) or not item.strip():
            raise VersionValidationError(
                f"duration-policy.json field 'statuses' must contain non-empty strings, "
                f"got {item!r}"
            )
        if item in seen:
            raise VersionValidationError(
                f"duration-policy.json field 'statuses' contains duplicate entry {item!r}"
            )
        seen.add(item)
    missing_statuses = DURATION_STATUSES - seen
    if missing_statuses:
        raise VersionValidationError(
            "duration-policy.json field 'statuses' is missing required entries: "
            f"{sorted(missing_statuses)}"
        )

    return data


def load_duration_policy(repo_root: Optional[str] = None) -> Dict[str, Any]:
    """Load config/duration-policy.json: the single source of truth for duration policy.

    Fail-Closed: the values are ALWAYS read from disk. No Python-side defaults are
    ever injected, merged, or returned as a fallback.

    Args:
        repo_root: Repository root path (default: infer from script location).

    Returns:
        The parsed JSON document, validated against the frozen contract.

    Raises:
        VersionValidationError: If the file is missing, not valid UTF-8, corrupted,
            not a JSON object, missing/mistyped/out-of-range fields, contains
            NaN/Infinity literals, declares the same key twice, nests so deeply
            that the JSON parser overflows, or uses an unsupported schema_version.
    """
    if repo_root is None:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    policy_path = os.path.join(repo_root, "config", "duration-policy.json")
    if not os.path.isfile(policy_path):
        raise VersionValidationError(
            f"Required duration policy file missing: {policy_path}"
        )

    try:
        with open(policy_path, "r", encoding="utf-8") as f:
            raw_content = f.read()
    except UnicodeDecodeError as exc:
        raise VersionValidationError(
            f"duration-policy.json is not valid UTF-8: {exc}"
        ) from exc
    except Exception as exc:
        raise VersionValidationError(
            f"Failed to read duration-policy.json: {exc}"
        ) from exc

    try:
        data = json.loads(
            raw_content,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise VersionValidationError(
            f"Corrupted duration-policy.json (invalid JSON syntax): {exc}"
        ) from exc
    except RecursionError as exc:
        raise VersionValidationError(
            "Corrupted duration-policy.json: JSON nesting is too deep to parse"
        ) from exc

    if not isinstance(data, dict):
        raise VersionValidationError(
            "duration-policy.json top-level structure must be a JSON object, "
            f"got {type(data).__name__}"
        )

    return _validate_duration_policy_document(data)


def get_git_state(repo_root: str) -> Dict[str, Any]:
    """Inspect git repository state at repo_root.
    
    Raises:
        VersionValidationError: If not a git repository or git command fails.
    """
    try:
        # Check inside work tree
        in_tree = subprocess.check_output(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_root,
            stderr=subprocess.PIPE,
            text=True,
        ).strip()
        if in_tree != "true":
            raise VersionValidationError(f"Directory is not inside a git work tree: {repo_root}")
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise VersionValidationError(f"Directory is not a valid git repository: {repo_root}") from exc

    # Commit SHA
    try:
        commit_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.PIPE,
            text=True,
        ).strip()
        if len(commit_sha) != 40 or not all(c in "0123456789abcdef" for c in commit_sha.lower()):
            raise VersionValidationError(f"Invalid git commit SHA: {commit_sha}")
    except subprocess.SubprocessError as exc:
        raise VersionValidationError("Failed to extract git commit SHA from HEAD") from exc

    # Branch name / detached
    try:
        branch = subprocess.check_output(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.PIPE,
            text=True,
        ).strip()
        is_detached = False
    except subprocess.CalledProcessError:
        # HEAD is detached
        branch = "DETACHED_HEAD"
        is_detached = True

    # Dirty status
    try:
        status_out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            stderr=subprocess.PIPE,
            text=True,
        ).strip()
        is_dirty = bool(status_out)
    except subprocess.SubprocessError as exc:
        raise VersionValidationError("Failed to check git working tree status") from exc

    return {
        "git_commit": commit_sha,
        "git_branch": branch,
        "is_detached": is_detached,
        "is_dirty": is_dirty,
    }


def get_version(repo_root: Optional[str] = None) -> str:
    """Convenience helper returning validated SemVer string."""
    info = load_version_info(repo_root=repo_root, enforce_git=False)
    return info["version"]


def _frontmatter_version(skill_text: str) -> Optional[str]:
    parts = skill_text.split("---", 2)
    if len(parts) < 3:
        return None
    match = re.search(r"^\s*version:\s*[\"']?([^\"'\s]+)[\"']?\s*$",
                      parts[1], re.MULTILINE)
    return match.group(1).strip() if match else None


def collect_version_declarations(repo_root: str) -> Dict[str, Any]:
    """Collect every in-package version declaration for the MM-01 guard.

    Single source of truth is ``config/version.json`` (``version`` field).
    Compared declarations: SKILL.md frontmatter ``version``, SKILL.md title
    ``(vX)``, package directory suffix (``..._vX``) and every
    ``pipeline-contract`` block's ``skill_version``.
    """
    info = load_version_info(repo_root=repo_root, enforce_git=False)
    authoritative = info["version"]
    skill_path = os.path.join(repo_root, "SKILL.md")
    if not os.path.isfile(skill_path):
        raise VersionValidationError(f"SKILL.md missing: {skill_path}")
    with open(skill_path, "r", encoding="utf-8") as handle:
        skill_text = handle.read()
    frontmatter = _frontmatter_version(skill_text)
    title_match = re.search(r"\(v([^)]+)\)", skill_text)
    title_version = title_match.group(1).strip() if title_match else None
    base = os.path.basename(os.path.abspath(repo_root))
    dir_match = re.search(
        r"_v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?(?:\+[0-9A-Za-z.\-]+)?)$", base)
    dir_version = dir_match.group(1) if dir_match else None
    contract_versions = sorted(set(
        match.group(1).strip()
        for match in re.finditer(
            r"^\s*skill_version:\s*[\"']?([^\"'\s#]+)[\"']?", skill_text,
            re.MULTILINE)))
    return {
        "version": authoritative,
        "frontmatter": frontmatter,
        "title": title_version,
        "directory": dir_version,
        "contracts": contract_versions,
    }


def check_single_source_version(repo_root: Optional[str] = None) -> str:
    """Fail-closed MM-01 guard: every declaration must equal version.json.

    Returns the authoritative version string. Raises
    VersionValidationError on any drift so a release/evidence report can
    never be stamped with a version the package does not honestly declare.
    """
    if repo_root is None:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    declared = collect_version_declarations(repo_root)
    authoritative = declared["version"]
    problems: list[str] = []
    for key in ("frontmatter", "title", "directory"):
        value = declared.get(key)
        if value is None:
            problems.append(f"{key} declaration missing")
        elif value != authoritative:
            problems.append(f"{key} declares {value!r} != {authoritative!r}")
    for contract_version in declared.get("contracts") or []:
        if contract_version != authoritative:
            problems.append(
                f"pipeline-contract skill_version {contract_version!r} "
                f"!= {authoritative!r}")
    if problems:
        raise VersionValidationError(
            "Single-source version drift: config/version.json "
            f"{authoritative!r} vs " + "; ".join(problems))
    return authoritative


if __name__ == "__main__":
    try:
        info = load_version_info(enforce_git=True)
        print(f"Version: {info['version']}")
        print(f"Release Name: {info['release_name']}")
        print(f"Git Commit: {info.get('git_commit', 'N/A')}")
        print(f"Git Branch: {info.get('git_branch', 'N/A')}")
        print(f"Git Dirty: {info.get('is_dirty', False)}")
        print("OK: Version and Git state validated.")
    except VersionValidationError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
