#!/usr/bin/env python3
"""Agent E2E output generator: request -> route -> build draft -> audit -> emit 3 physical files.

Real chain under test (C-2/C-3):
  e2e/cases/<case_id>.json
    -> scripts/router.route_request (profile/mode/assets/model decision)
    -> scripts/generate_output.build_clean_draft (deterministic draft composer)
    -> scripts/audit_cli.run_audit via subprocess (real CLI entrypoint)
    -> emits e2e/results/<case_id>/prompt.txt + analysis.json + audit-report.json

Isolation contract (C-3):
  - prompt.txt contains ONLY the copyable prompt body (no PASS/FAIL, no JSON, no disclaimer).
  - analysis.json holds profile/mode/model/hard-constraints/assumptions/warnings.
  - audit-report.json holds rule totals, failed rule IDs, prompt_sha256.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import subprocess
import uuid
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from router import normalize_mode, route_request, validate_v2_api_call  # noqa: E402
from build_h3_request import build_payload, check_constraint, normalize_constraint  # noqa: E402

# T1a: request-supplied identifiers are path components. Only plain ASCII
# ids are accepted; anything else (path separators, dots, traversal, unicode
# tricks) is rejected before any filesystem operation happens.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

NL = chr(10)  # line terminator used by draft assembly (LF, byte-exact with audit)


def validate_safe_id(value: str, kind: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.match(value):
        raise RuntimeError(
            f"unsafe {kind} rejected: {value!r} (must match {SAFE_ID_RE.pattern})")
    return value


def extract_hard_constraints(request: dict) -> list[str]:
    out: list[str] = []
    for key in ("duration_s", "aspect_ratio", "resolution", "hard_constraints"):
        val = request.get(key)
        if val is None or val == "":
            continue
        if isinstance(val, list):
            out.extend(str(v) for v in val)
        else:
            out.append(f"{key}={val}")
    for asset_key in ("first_frame", "last_frame", "image", "video", "audio"):
        assets = request.get("assets") or {}
        if assets.get(asset_key):
            out.append(f"asset:{asset_key}={assets.get(asset_key)}")
    return out


class NeedMoreInput(RuntimeError):
    """T2: decisive information is missing; ask instead of fabricating."""

    def __init__(self, missing):
        super().__init__("decisive information missing: " + "; ".join(missing))
        self.missing = list(missing)


def build_clean_draft(request: dict, decision) -> str:
    """Compose the submission-ready prompt body from the case request.

    Never embeds audit verdicts, JSON, disclaimers, or Markdown reports --
    and never INVENTS content. Every value comes verbatim from the request;
    native-general with only a natural-language request passes the user's
    own words through unchanged; drama treats all five content fields as
    decisive and raises NeedMoreInput instead of substituting defaults
    (no persona names, no canned lighting/space/sound/motion lines).
    """
    scene = (request.get("scene") or "").strip()
    action = (request.get("action") or "").strip()
    style = (request.get("style") or "").strip()
    camera = (request.get("camera") or "").strip()
    audio = (request.get("audio") or "").strip()
    user_prompt = (request.get("user_prompt") or "").strip()
    profile = decision.output_profile

    if profile == "ref2va-en":
        return (request.get("prompt_body") or "").strip() + NL

    if profile == "native-drama-series":
        ending = "写实真人摄影质感；真实皮肤质感；自然真实的人类面部特征，带有轻微的生理不完美。"
        missing = []
        if not scene:
            missing.append("scene（空间基准：剧情发生的具体地点）")
        if not action:
            missing.append("action（画面演进：这一镜里实际发生的连续动作）")
        if not style:
            missing.append("style（环境光影：光线与色调基调）")
        if not audio:
            missing.append("audio（声音设计：环境声与是否无配乐）")
        if not (request.get("voice_signature") or "").strip():
            missing.append("voice_signature（角色声音签名：姓名＋音色/音量/语速，不得使用默认人名）")
        if missing:
            raise NeedMoreInput(missing)
        parts = [
            "这是一段连续的全画幅画面，遵守对话轴和 180 度原则。",
            "",
            "【环境光影】" + style,
            "",
            "【空间基准】" + scene,
            "",
            "【角色声音签名】",
            request["voice_signature"].strip(),
            "",
            "【画面演进】",
            scene + "，" + action + "。",
        ]
        if camera:
            parts.append(camera + "。")
        parts += [
            "",
            "【声音设计】" + audio,
            "",
            "【画面风格】" + ending,
            "",
        ]
        return NL.join(p for p in parts if p != "").rstrip() + NL

    # native-general: plain cinematic paragraph built ONLY from given values;
    # with no structured fields the user's own natural language IS the body.
    sentences = [s for s in (scene, action, camera, style) if s]
    if sentences:
        return "，".join(sentences).rstrip("，。") + "。" + NL
    if user_prompt:
        return user_prompt + NL
    raise NeedMoreInput(["scene/action 结构化字段，或一句可直接使用的自然语言剧情描述"])


def run_real_audit_cli(prompt_text: str, mode: str) -> dict:
    """Invoke the real audit_cli.py entrypoint via a file arg (byte-exact).

    Stdin text-mode pipes translate LF->CRLF on win32, which would make the
    audited sha differ from the prompt.txt bytes. Passing a file keeps the
    audited bytes identical to what is committed to prompt.txt.
    """
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8", newline="\n") as f:
        f.write(prompt_text)
        tmp_path = f.name
    try:
        cmd = [sys.executable, os.path.join(SCRIPTS_DIR, "audit_cli.py"),
               tmp_path, "--mode", mode, "--json", "--warn-policy", "report"]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="strict",
            cwd=REPO_ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"audit_cli crashed rc={proc.returncode}: stdout={proc.stdout!r} stderr={proc.stderr!r}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"audit_cli stdout not JSON-parseable: {exc}; stdout={proc.stdout!r}") from exc


def _mint_run_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def ensure_within_root(root: str, target: str) -> str:
    """T1b: refuse any target that resolves outside the output root.

    realpath resolves symlinks and NTFS junctions, so a link planted inside
    the tree that points elsewhere is caught before anything is touched.
    """
    root_real = os.path.normcase(os.path.realpath(root))
    target_real = os.path.normcase(os.path.realpath(target))
    if target_real != root_real and not target_real.startswith(root_real + os.sep):
        raise RuntimeError(
            f"path escape refused: {target!r} resolves outside output root {root!r}")
    return target_real


def create_staging_dir(out_root: str, name: str) -> str:
    """Fresh staging slot under ``out_root/.staging/<name>`` (rmtree-if-exists).

    The slot name is caller-chosen (e.g. run-id + case-id), so re-running the
    same case reuses and cleans the same slot instead of accumulating debris.
    Staging lives OUTSIDE the run directory so a failed attempt leaves
    forensics without polluting the run dir's case coverage.
    """
    import shutil as _shutil

    staging_dir = os.path.join(out_root, ".staging", name)
    ensure_within_root(out_root, staging_dir)
    if os.path.exists(staging_dir):
        _shutil.rmtree(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)
    return staging_dir


def atomic_swap(staging_dir: str, target_dir: str) -> None:
    """T1d: replace target_dir with staging_dir in one rename.

    The old result is restored if the swap itself fails; when the target does
    not exist yet this is a plain rename after creating its parent. Once the
    swap has succeeded, a stuck backup cleanup (AV lock, open handle) must
    not turn a completed swap into a reported crash — the leftover ``.bak``
    is reported loudly and left for manual cleanup instead.
    """
    import shutil as _shutil

    if os.path.exists(target_dir):
        backup = target_dir + ".bak"
        if os.path.exists(backup):
            _shutil.rmtree(backup)
        os.rename(target_dir, backup)
        try:
            os.rename(staging_dir, target_dir)
        except Exception:
            os.rename(backup, target_dir)
            raise
        try:
            _shutil.rmtree(backup)
        except OSError as exc:
            print(f"WARNING: swap completed but stale backup could not be "
                  f"removed: {backup} ({exc})", file=sys.stderr)
    else:
        os.makedirs(os.path.dirname(target_dir), exist_ok=True)
        os.rename(staging_dir, target_dir)


def generate_case(case_path: str, out_root: str, run_id: str | None = None,
                  replace: bool = False) -> dict:
    with open(case_path, "r", encoding="utf-8") as f:
        request = json.load(f)
    case_id = validate_safe_id(request["case_id"], "case_id")
    if run_id is not None:
        validate_safe_id(run_id, "run-id")
    run_id = run_id or _mint_run_id()

    # T1b/T1c: every case lives under its own run directory. Nothing is ever
    # deleted up front; an existing target is refused unless --replace is
    # given, and all validation happens before any swap.
    out_root = os.path.abspath(out_root)
    case_dir = os.path.join(out_root, run_id, case_id)
    ensure_within_root(out_root, case_dir)
    if os.path.exists(case_dir) and not replace:
        raise RuntimeError(
            f"target already exists for {case_id} in run {run_id}: {case_dir} "
            "(pass --replace explicitly to swap it after full re-validation)")

    decision = route_request(
        request.get("user_prompt", ""),
        target_model=request.get("model", "H3"),
        forced_mode=request.get("forced_mode"),
        forced_profile=request.get("forced_profile"),
        aspect_ratio=request.get("aspect_ratio", "16:9"),
        resolution=request.get("resolution", "768P"),
        duration_s=request.get("duration_s", 6),
        assets=request.get("assets"),
    )

    if not decision.is_valid:
        raise RuntimeError(f"route rejected {case_id}: {decision.error_message}")
    v2_ok, v2_err, _ = validate_v2_api_call(decision.model, decision.generation_mode, request.get("assets"))
    if not v2_ok:
        raise RuntimeError(f"public V2 rejected {case_id}: {v2_err}")
    for role, val in (request.get("assets") or {}).items():
        if isinstance(val, str) and val and not val.startswith(("http://", "https://")):
            cand = val if os.path.isfile(val) else os.path.join(REPO_ROOT, val)
            if not os.path.isfile(cand):
                raise RuntimeError(f"asset missing for {case_id}: role={role} value={val!r}")
    effective_request = ((request.get("prompt_body") or "")
                         if decision.output_profile == "ref2va-en"
                         else request.get("user_prompt"))
    if not (effective_request or "").strip():
        raise RuntimeError(f"empty user request for {case_id}: refusing to fabricate a prompt")

    try:
        draft = build_clean_draft(request, decision)
    except NeedMoreInput as need:
        # T2: never fabricate. Deliver an explicit question list instead of a
        # prompt; analysis.json is the ONLY artifact for this case.
        os.makedirs(case_dir, exist_ok=True)
        analysis = {
            "case_id": case_id,
            "run_id": run_id,
            "commit": git_head(),
            "status": "NEEDS_INPUT",
            "missing_info": need.missing,
            "model": decision.model,
            "generation_mode": normalize_mode(decision.generation_mode),
            "output_profile": decision.output_profile,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(os.path.join(case_dir, "analysis.json"), "w", encoding="utf-8") as f:
            json.dump(analysis, f, ensure_ascii=False, indent=2)
        return {"case_id": case_id, "run_id": run_id, "exit_code": 3,
                "status": "NEEDS_INPUT", "missing_info": need.missing}

    audit_mode = (
        "ref2va-en"
        if decision.output_profile == "ref2va-en"
        else ("native-general" if decision.output_profile == "native-general" else "native-zh")
    )
    report = run_real_audit_cli(draft, audit_mode)

    # T3 quality contract: classify the four information classes and prove
    # every non-metadata hard constraint landed in the effective payload.
    payload = build_payload(request, draft, request.get("assets"))
    traceability: list[dict] = []
    unlanded: list[str] = []
    for raw in request.get("constraints", []) or []:
        c = normalize_constraint(raw)
        if c["delivery_scope"] == "metadata_only":
            traceability.append({"id": c["id"], "scope": "metadata_only", "landed": False,
                                 "where": None,
                                 "note": "metadata_only never counts as a delivery landing"})
            continue
        ok, why = check_constraint(payload, c)
        traceability.append({"id": c["id"], "scope": c["delivery_scope"],
                             "landed": bool(ok), "where": c["delivery_scope"] if ok else None,
                             "why": None if ok else why})
        if not ok:
            unlanded.append(c["id"])
    unknown_info: list[str] = []
    if decision.output_profile == "native-general":
        if not (request.get("camera") or "").strip():
            unknown_info.append("camera（运镜未提供：正文未声称任何具体运镜）")
        if not (request.get("audio") or "").strip():
            unknown_info.append("audio（声音设计未提供）")
    elif decision.output_profile == "native-drama-series":
        if not (request.get("camera") or "").strip():
            unknown_info.append("camera（运镜未提供：画面演进未含具体运镜行）")
    quality_contract = {
        "user_hard_constraints": extract_hard_constraints(request),
        "materials_usage": [
            {"role": role, "value": val,
             "usage": "declared asset payload; file/url verified before generation"}
            for role, val in (request.get("assets") or {}).items()],
        "autonomous_preferences": {
            "soft_preferences": request.get("soft_preferences", []),
            "creative_suggestions": [],
        },
        "unknown_info": unknown_info,
        "constraint_traceability": traceability,
    }


    # Staging lives OUTSIDE the run directory (out_root/.staging) so a failed
    # attempt leaves forensics without polluting the run dir's case coverage.
    staging_dir = create_staging_dir(out_root, f"{run_id}-{case_id}")
    with open(os.path.join(staging_dir, "prompt.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(draft)
    # NOTE: on failure the staging dir under out_root/.staging is
    # intentionally left for forensics; run directories stay clean.

    route_info = {
        "model": decision.model,
        "generation_mode": decision.generation_mode,
        "output_profile": decision.output_profile,
        "workflow_profile": decision.workflow_profile,
        "medium": decision.medium,
        "is_valid": decision.is_valid,
        "route_error": decision.error_message,
        "loaded_references": decision.loaded_references,
    }
    analysis = {
        "case_id": case_id,
        "run_id": run_id,
        "commit": git_head(),
        "model": decision.model,
        "generation_mode": normalize_mode(decision.generation_mode),
        "output_profile": decision.output_profile,
        "duration_s": request.get("duration_s", 6),
        "aspect_ratio": request.get("aspect_ratio", "16:9"),
        "resolution": request.get("resolution", "768P"),
        "assets": request.get("assets") or {},
        "hard_constraints": extract_hard_constraints(request),
        "soft_preferences": request.get("soft_preferences", []),
        "assumptions": request.get("assumptions", []),
        "warnings": [decision.error_message] if decision.error_message else [],
        "quality_contract": quality_contract,
        "unlanded_constraints": unlanded,
        "route": route_info,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(os.path.join(staging_dir, "analysis.json"), "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)

    audit_doc = {
        "case_id": case_id,
        "run_id": run_id,
        "profile": report.get("profile"),
        "ruleset_version": report.get("ruleset_version"),
        "total": len(report.get("results", [])),
        "passed": report.get("passed"),
        "warned": report.get("warned"),
        "failed": report.get("failed"),
        "failed_rule_ids": [r.get("rule_id") for r in report.get("results", []) if r.get("status") == "FAIL"],
        "prompt_sha256": report.get("prompt_sha256"),
        "executed_at": report.get("executed_at"),
    }
    with open(os.path.join(staging_dir, "audit-report.json"), "w", encoding="utf-8") as f:
        json.dump(audit_doc, f, ensure_ascii=False, indent=2)

    # Commit the case directory only after all three files verify.
    for name in ("prompt.txt", "analysis.json", "audit-report.json"):
        p = os.path.join(staging_dir, name)
        if not os.path.isfile(p):
            raise RuntimeError(f"staging incomplete for {case_id}: missing {name}")
    with open(os.path.join(staging_dir, "prompt.txt"), "rb") as f:
        disk_sha = hashlib.sha256(f.read()).hexdigest()
    if disk_sha != audit_doc.get("prompt_sha256"):
        raise RuntimeError(f"audit binding failed for {case_id}: prompt sha mismatch")
    atomic_swap(staging_dir, case_dir)

    return {"case_id": case_id, "run_id": run_id, "exit_code": 0 if audit_doc["failed"] == 0 and not unlanded else 1,
            "case_dir": case_dir}


@functools.lru_cache(maxsize=1)
def git_head() -> str:
    """HEAD of REPO_ROOT, cached per process (a CLI run sees one commit)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
            text=True, encoding="utf-8", errors="strict", check=True,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        return proc.stdout.strip()
    except Exception:
        return "UNKNOWN"


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent E2E output generator (prompt.txt + analysis.json + audit-report.json)")
    parser.add_argument("--case", required=True, help="Path to e2e/cases/<case_id>.json")
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "e2e", "results"), help="Results root directory")
    parser.add_argument("--run-id", default=None, help="Run identifier propagated into analysis/audit docs")
    parser.add_argument("--replace", action="store_true",
                        help="Allow replacing an existing case directory AFTER the new "
                             "result fully validates (atomic swap; default refuses)")
    args = parser.parse_args()
    try:
        summary = generate_case(args.case, args.out, run_id=args.run_id, replace=args.replace)
        print(json.dumps(summary, ensure_ascii=False))
        return summary["exit_code"]
    except Exception as exc:  # fail-closed: crash loudly, never fabricate files
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
