#!/usr/bin/env python3
"""Validation tool for references/official/source-manifest.json."""

from __future__ import annotations

import hashlib
import json
import os
import sys

REQUIRED_FIELDS = [
    "source_path",
    "normalized_path",
    "official_counterpart",
    "declared_commit",
    "commit_verified",
    "source_sha256",
    "normalized_sha256",
    "normalization",
    "provenance_status",
    "verification_note",
]

VALID_PROVENANCE_STATUSES = {
    "LOCAL_HASH_VERIFIED",
    "UPSTREAM_COMMIT_UNVERIFIED",
    "UPSTREAM_COMMIT_VERIFIED",
    "SIGNED_UPSTREAM_VERIFIED",
}


def normalize_lf(text_bytes: bytes) -> bytes:
    # Convert CRLF to LF
    return text_bytes.replace(b"\r\n", b"\n")


def validate_manifest(manifest_path: str, repo_root: str) -> tuple[bool, list[str], dict[str, str]]:
    errors = []
    statuses: dict[str, str] = {}
    if not os.path.exists(manifest_path):
        return False, [f"Manifest file not found: {manifest_path}"], {}

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, [f"Failed to parse manifest JSON: {e}"], {}

    sources = data.get("sources", [])
    if not sources:
        errors.append("Manifest sources list is empty.")

    for idx, item in enumerate(sources):
        for field in REQUIRED_FIELDS:
            if field not in item:
                errors.append(f"Entry {idx} missing required field '{field}'")

        status = item.get("provenance_status", "")
        norm_rel = item.get("normalized_path", "")
        statuses[norm_rel] = status
        if status not in VALID_PROVENANCE_STATUSES:
            errors.append(f"Entry {idx} ({norm_rel}) invalid provenance_status: '{status}'")

        norm_abs = os.path.join(repo_root, norm_rel)
        if not os.path.exists(norm_abs):
            errors.append(f"Normalized file does not exist: {norm_rel}")
            continue

        try:
            with open(norm_abs, "rb") as f:
                content = f.read()
            raw_sha = hashlib.sha256(content).hexdigest()
            norm_content = normalize_lf(content)
            norm_sha = hashlib.sha256(norm_content).hexdigest()

            expected_raw = item.get("source_sha256")
            expected_norm = item.get("normalized_sha256")

            if raw_sha != expected_raw:
                errors.append(
                    f"Entry {idx} ({norm_rel}) raw SHA-256 mismatch: actual {raw_sha} != expected {expected_raw}"
                )
            if norm_sha != expected_norm:
                errors.append(
                    f"Entry {idx} ({norm_rel}) normalized SHA-256 mismatch: actual {norm_sha} != expected {expected_norm}"
                )
        except Exception as e:
            errors.append(f"Error reading {norm_rel}: {e}")

        # Guard against falsely claiming commit verified
        if item.get("commit_verified") is True and status not in ("UPSTREAM_COMMIT_VERIFIED", "SIGNED_UPSTREAM_VERIFIED"):
            errors.append(
                f"Entry {idx} ({norm_rel}) commit_verified cannot be true while status is '{status}'"
            )
        if item.get("commit_verified") is True:
            errors.append(
                f"Entry {idx} ({norm_rel}) cannot claim commit_verified=true without cryptographically verified upstream commit object"
            )

    return len(errors) == 0, errors, statuses


def main() -> int:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    manifest_path = os.path.join(repo_root, "references", "official", "source-manifest.json")
    valid, errors, statuses = validate_manifest(manifest_path, repo_root)
    if valid:
        print("[PASS] Official Source Manifest Validation: Local files verified, upstream status honest:")
        for path, status in statuses.items():
            print(f"  - {path}: {status} (commit_verified=False)")
        return 0
    else:
        print("[FAIL] Official Source Manifest Validation Failed:")
        for err in errors:
            print(f"  - {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
