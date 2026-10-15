#!/usr/bin/env python3
"""Auditable H3 V2 request payload builder (P1-1).

Builds the effective submission payload a case would send to MiniMax H3 V2:
  - prompt_text: the exact UTF-8 bytes of prompt.txt
  - api_parameters: duration/ratio/resolution carried as real interface params
  - asset_payload: role-tagged asset inputs with sha256 when local files exist
  - metadata_only: notes recorded in analysis.json but never submitted

Constraint contract: every expected constraint carries delivery_scope in
{prompt_text, api_parameter, asset_payload, metadata_only}. Only the first
three scopes count toward retention; metadata_only never compensates.
"""

from __future__ import annotations

import hashlib
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VALID_SCOPES = {"prompt_text", "api_parameter", "asset_payload", "metadata_only"}


def sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def normalize_constraint(raw) -> dict:
    """Accept legacy 'key=value' strings and new {id,value,delivery_scope} dicts."""
    if isinstance(raw, dict):
        scope = raw.get("delivery_scope", "metadata_only")
        if scope not in VALID_SCOPES:
            raise ValueError(f"illegal delivery_scope: {scope!r}")
        return {
            "id": str(raw.get("id", "")),
            "value": raw.get("value"),
            "delivery_scope": scope,
            "target_path": str(raw.get("target_path", "")),
        }
    text = str(raw)
    if text.startswith("asset:"):
        _, rest = text.split(":", 1)
        key, _, val = rest.partition("=")
        return {"id": key, "value": val, "delivery_scope": "asset_payload", "target_path": f"assets.{key}"}
    key, _, val = text.partition("=")
    scope = {"duration_s": "api_parameter", "aspect_ratio": "api_parameter",
             "resolution": "api_parameter"}.get(key, "metadata_only")
    target = {"duration_s": "duration", "aspect_ratio": "ratio", "resolution": "resolution"}.get(key, "")
    try:
        value = int(val) if val.isdigit() else val
    except Exception:
        value = val
    return {"id": key, "value": value, "delivery_scope": scope, "target_path": target}


def build_payload(case: dict, prompt_text: str, assets: dict | None = None) -> dict:
    """Build the auditable submission payload for one golden case."""
    assets = dict(assets or case.get("assets") or {})
    constraints = [normalize_constraint(c) for c in case.get("constraints", case.get("hard_constraints", []))]
    duration = case.get("duration_s", 6)
    ratio = case.get("aspect_ratio", "16:9")
    resolution = case.get("resolution", "768P")

    asset_payload: dict = {}
    for role in ("first_frame", "last_frame", "image", "video", "audio",
                 "reference_image", "reference_video", "reference_audio"):
        val = assets.get(role)
        if val is None or val == "":
            continue
        entry: dict = {"value": val}
        p = val if isinstance(val, str) and os.path.isfile(val) else None
        if p is None and isinstance(val, str):
            cand = os.path.join(REPO_ROOT, val)
            p = cand if os.path.isfile(cand) else None
        entry["exists"] = p is not None
        entry["sha256"] = sha256_file(p) if p else None
        asset_payload[role] = entry

    return {
        "case_id": case.get("case_id"),
        "model": case.get("model", "H3"),
        "mode": case.get("expected", {}).get("mode", case.get("forced_mode", "t2va")),
        "prompt_text": prompt_text,
        "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        "api_parameters": {"duration": duration, "ratio": ratio, "resolution": resolution},
        "asset_payload": asset_payload,
        "constraints": constraints,
        "metadata_only": case.get("assumptions", []),
    }


def check_constraint(payload: dict, constraint: dict) -> tuple[bool, str]:
    scope = constraint["delivery_scope"]
    value = constraint["value"]
    if scope == "metadata_only":
        return False, "metadata_only never counts toward retention"
    if scope == "prompt_text":
        needle = "" if value is None else str(value)
        if needle and needle in payload["prompt_text"]:
            return True, "retained in prompt_text"
        return False, f"{constraint['id']} not found in prompt_text"
    if scope == "api_parameter":
        params = payload["api_parameters"]
        target = constraint.get("target_path") or constraint["id"]
        key = {"duration": "duration", "ratio": "ratio", "resolution": "resolution"}.get(target, target)
        if key in params and params[key] == value:
            return True, f"retained in api_parameters.{key}"
        return False, f"{constraint['id']}={value!r} != payload {key}={params.get(key)!r}"
    if scope == "asset_payload":
        role = constraint.get("target_path", "").split(".")[-1] or constraint["id"]
        entry = payload["asset_payload"].get(role)
        if not entry:
            return False, f"asset role {role} missing from payload"
        if not entry.get("exists"):
            return False, f"asset role {role} file/URL unverifiable"
        if constraint["id"] in ("first_frame", "last_frame") and role in ("first_frame", "last_frame"):
            want = str(value)
            if want and want != str(entry.get("value")):
                return False, f"asset role {role} value swapped: payload={entry.get('value')!r} want={want!r}"
        return True, f"asset role {role} verified"
    return False, f"unknown scope {scope}"
