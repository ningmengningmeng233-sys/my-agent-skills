#!/usr/bin/env python3
"""MiniMax H3 V2 API client (P3): dry-run by default, explicit --live only.

Endpoints (V2):
  create: POST {base}/v2/video_generation
  query:  GET  {base}/v2/query/video_generation/{task_id}
Auth: MINIMAX_API_KEY env or system credential store only; never logged,
never written to JSON/Git/desktop manifests.
Error taxonomy: 401/402/422/429/5xx are DISTINCT states, never PASS.
Retries: bounded backoff on 429/5xx for query polling; create is never
blind-resent when success is unknown (returns UNKNOWN_CREATE).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.minimax.io"
TERMINAL_OK = {"succeeded"}
RETRYABLE = {429, 500, 502, 503, 504}


def redacted_summary(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def get_api_key() -> str | None:
    return os.environ.get("MINIMAX_API_KEY") or None


def build_v2_body(prompt_text: str, model_id: str, duration: int, ratio: str,
                  assets: dict | None = None) -> dict:
    """Map internal (mode, assets) onto V2 content[] per provider mapping."""
    from router import normalize_mode  # local import: scripts/ is on sys.path for CLI
    assets = assets or {}
    mode = normalize_mode((assets.pop("_mode", None) or "t2va"))
    content: list[dict] = [{"type": "text", "text": prompt_text}]
    if mode == "i2va":
        content.append({"type": "image_url", "role": "first_frame", "image_url": assets.get("first_frame", "")})
    elif mode == "l2va":
        content.append({"type": "image_url", "role": "last_frame", "image_url": assets.get("last_frame", "")})
    elif mode == "fl2va":
        content.append({"type": "image_url", "role": "first_frame", "image_url": assets.get("first_frame", "")})
        content.append({"type": "image_url", "role": "last_frame", "image_url": assets.get("last_frame", "")})
    elif mode == "ref2va":
        for role in ("reference_image", "reference_video", "reference_audio"):
            if assets.get(role):
                content.append({"type": role, "url": assets[role]})
        for legacy in ("image", "video"):
            if assets.get(legacy):
                content.append({"type": f"reference_{legacy}", "url": assets[legacy]})
    return {"model": model_id, "content": content, "duration": duration, "ratio": ratio}


def _http_json(method: str, url: str, api_key: str, body: dict | None = None,
               timeout: int = 30) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            payload = {"http_error": exc.code}
        return exc.code, payload
    except Exception as exc:
        return -1, {"transport_error": str(exc)}


def create_task(base: str, api_key: str, body: dict, dry_run: bool,
                http_timeout: int = 30) -> dict:
    if dry_run:
        return {"state": "DRY_RUN", "payload_sha16": redacted_summary(body),
                "note": "no network call made"}
    status, payload = _http_json("POST", f"{base}/v2/video_generation", api_key, body,
                                 timeout=http_timeout)
    if status == 200 and payload.get("task_id"):
        return {"state": "CREATED", "task_id": payload["task_id"],
                "payload_sha16": redacted_summary(body)}
    if status == -1 or status in (502, 503, 504):
        # 成功未知：超时或网关 5xx 时服务端是否建单未知，不得当成确定
        # 业务拒绝而盲目重发（会重复计费）；上层应经 query 核实后再决策。
        return {"state": "UNKNOWN_CREATE", "status": status, "response": payload,
                "payload_sha16": redacted_summary(body),
                "note": "not blind-resent: success unknown; verify via query"}
    if status in (401, 402, 422, 429) or status >= 500 or status == -1:
        label = {401: "AUTH_401", 402: "QUOTA_402", 422: "INVALID_422",
                 429: "RATE_429"}.get(status, f"HTTP_{status}")
        return {"state": label, "status": status, "response": payload,
                "payload_sha16": redacted_summary(body)}
    return {"state": "UNKNOWN_CREATE", "status": status, "response": payload,
            "payload_sha16": redacted_summary(body),
            "note": "not blind-resent: success unknown"}


def poll_task(base: str, api_key: str, task_id: str, timeout_s: int = 600,
              interval_s: int = 10, max_retries: int = 5,
              http_timeout: int = 30) -> dict:
    """Poll query endpoint to a terminal state with bounded 429/5xx backoff."""
    deadline = time.time() + timeout_s
    retries = 0
    last: dict = {}
    while time.time() < deadline:
        status, payload = _http_json(
            "GET", f"{base}/v2/query/video_generation/{task_id}", api_key,
            timeout=http_timeout)
        last = {"status": status, "payload": payload}
        state = str(payload.get("status", "")).lower()
        if status == 200 and state in TERMINAL_OK:
            return {"state": "SUCCEEDED", "task_id": task_id, "response": payload}
        if status == 200 and state in ("failed", "cancelled", "expired"):
            return {"state": f"TERMINAL_{state.upper()}", "task_id": task_id, "response": payload}
        if status in (401, 402, 422):
            return {"state": {401: "AUTH_401", 402: "QUOTA_402", 422: "INVALID_422"}[status],
                    "task_id": task_id, "response": payload}
        if status in RETRYABLE or status == -1:
            retries += 1
            if retries > max_retries:
                return {"state": "RETRY_EXHAUSTED", "task_id": task_id, "last": last}
            time.sleep(min(interval_s * (2 ** (retries - 1)), 60))
            continue
        time.sleep(interval_s)
    return {"state": "POLL_TIMEOUT", "task_id": task_id, "last": last}


def main() -> int:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
    parser = argparse.ArgumentParser(description="H3 V2 client (dry-run default)")
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--model-id", default="MiniMax-H3")
    parser.add_argument("--duration", type=int, default=6)
    parser.add_argument("--ratio", default="16:9")
    parser.add_argument("--assets", default="{}")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--live", action="store_true", help="actually call the API (needs MINIMAX_API_KEY)")
    parser.add_argument("--poll-timeout", type=int, default=600)
    parser.add_argument("--http-timeout", type=int, default=30,
                        help="Per-request HTTP timeout in seconds")
    args = parser.parse_args()
    with open(args.prompt_file, "r", encoding="utf-8") as f:
        prompt = f.read()
    try:
        assets = json.loads(args.assets)
    except Exception as exc:
        print(f"ERROR: bad --assets JSON: {exc}", file=sys.stderr)
        return 2
    body = build_v2_body(prompt, args.model_id, args.duration, args.ratio, assets)
    if not args.live:
        print(json.dumps(create_task(args.base, "DRY", body, dry_run=True), ensure_ascii=False, indent=2))
        return 0
    key = get_api_key()
    if not key:
        print(json.dumps({"state": "NOT_RUN", "reason": "MINIMAX_API_KEY absent; refusing live call"},
                         ensure_ascii=False, indent=2))
        return 2
    created = create_task(args.base, key, body, dry_run=False,
                          http_timeout=args.http_timeout)
    print(json.dumps(created, ensure_ascii=False, indent=2))
    if created.get("state") != "CREATED":
        return 1
    final = poll_task(args.base, key, created["task_id"], timeout_s=args.poll_timeout,
                      http_timeout=args.http_timeout)
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0 if final.get("state") == "SUCCEEDED" else 1


if __name__ == "__main__":
    sys.exit(main())
