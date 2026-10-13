#!/usr/bin/env python3
"""E2E metrics measurer (C-4): structure-and-constraint scoring over e2e/results.

Metrics (no full-text equality):
  - mode_route_accuracy: analysis.generation_mode == expected_mode
  - profile_route_accuracy: analysis.output_profile == expected_profile
  - asset_check_accuracy: required asset keys from expected hard_constraints present in analysis.assets
  - hard_constraint_retention: every expected hard constraint token retained in prompt.txt/analysis
  - template_residue_rate: prompts containing {{var}}/${VAR}/[待]/TODO must be 0
  - audit_pollution: prompts containing PASS/FAIL/AUDIT_STATUS/```json/免责声明 must be 0
  - cli_crashes: generator returncode==2 count must be 0
  - json_parse_rate: analysis.json + audit-report.json parse cleanly

Targets: routing 100%, assets 100%, retention 100%, residue 0%, pollution 0%, crashes 0, JSON 100%.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from build_h3_request import build_payload, check_constraint, normalize_constraint

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED_DIR = os.path.join(REPO_ROOT, "e2e", "expected")
RESULTS_DIR = os.path.join(REPO_ROOT, "e2e", "results")

TEMPLATE_RESIDUE_RES = [r"\{\{[^}]+\}\}", r"\$\{[^}]+\}", r"\[待[^\]]+\]", r"\bTODO\b", r"\bFIXME\b"]
POLLUTION_TOKENS = ["AUDIT_STATUS", "PASS:", "RESULT:", "```json", "免责声明"]


def main() -> int:
    ap = argparse.ArgumentParser(description="E2E metrics measurer (C-4)")
    ap.add_argument("--run-dir", default=None,
                    help="Explicit per-run results directory (the results root "
                         "now holds one directory per run)")
    args = ap.parse_args()
    if args.run_dir:
        run_dir = os.path.abspath(args.run_dir)
    elif os.path.isfile(os.path.join(RESULTS_DIR, "_summary.json")):
        run_dir = RESULTS_DIR  # legacy flat layout
    else:
        print("ERROR: --run-dir required: the results root holds one directory per run",
              file=sys.stderr)
        return 2
    exp_files = sorted(f for f in os.listdir(EXPECTED_DIR) if f.endswith(".json"))
    n = len(exp_files)
    mode_ok = profile_ok = asset_ok = retention_ok = json_ok = 0
    residue_hits = pollution_hits = 0
    summary_path = os.path.join(run_dir, "_summary.json")
    crashes = 0
    if os.path.isfile(summary_path):
        with open(summary_path, "r", encoding="utf-8") as f:
            crashes = json.load(f).get("crashes", 0)

    details: list[dict] = []
    summary_run_id = None
    summary_source_commit = None
    summary_case_ids: list[str] = []
    if os.path.isfile(summary_path):
        with open(summary_path, "r", encoding="utf-8") as f:
            summary_doc = json.load(f)
        summary_run_id = summary_doc.get("run_id")
        summary_source_commit = summary_doc.get("source_commit")
        summary_case_ids = [c.get("case_file", "")[:-5] for c in summary_doc.get("cases", [])]
    result_dirs = sorted(d for d in os.listdir(run_dir) if os.path.isdir(os.path.join(run_dir, d)))
    stale_dirs = sorted(set(result_dirs) - set(fn[:-5] for fn in exp_files))
    missing_dirs = sorted(set(fn[:-5] for fn in exp_files) - set(result_dirs))
    run_id_mismatch = False
    for fn in exp_files:
        case_id = fn[:-5]
        with open(os.path.join(EXPECTED_DIR, fn), "r", encoding="utf-8") as f:
            exp = json.load(f)
        rec: dict = {"case_id": case_id}
        case_dir = os.path.join(run_dir, case_id)
        try:
            with open(os.path.join(case_dir, "analysis.json"), "r", encoding="utf-8") as f:
                analysis = json.load(f)
            with open(os.path.join(case_dir, "audit-report.json"), "r", encoding="utf-8") as f:
                audit = json.load(f)
            with open(os.path.join(case_dir, "prompt.txt"), "r", encoding="utf-8") as f:
                prompt = f.read()
            # P1-3 audit binding: disk bytes must match the audited sha; run_id/commit/case must line up.
            with open(os.path.join(case_dir, "prompt.txt"), "rb") as f:
                disk_sha = hashlib.sha256(f.read()).hexdigest()
            binding_ok = (
                audit.get("prompt_sha256") == disk_sha
                and (audit.get("total") or 0) > 0
                and (audit.get("failed") or 0) == 0
                and analysis.get("run_id") == audit.get("run_id")
                and (summary_run_id is None or analysis.get("run_id") == summary_run_id)
                and analysis.get("case_id") == case_id
                and audit.get("case_id") == case_id
            )
            rec["audit_binding_ok"] = binding_ok
            if not binding_ok:
                rec["binding_error"] = {
                    "disk_sha": disk_sha, "audit_sha": audit.get("prompt_sha256"),
                    "total": audit.get("total"), "failed": audit.get("failed"),
                    "analysis_run": analysis.get("run_id"), "audit_run": audit.get("run_id"),
                    "summary_run": summary_run_id,
                }
            else:
                json_ok += 1
            rec["json_parse"] = True
            if analysis.get("run_id") != summary_run_id:
                run_id_mismatch = True
        except Exception as exc:
            rec["json_parse"] = False
            rec["audit_binding_ok"] = False
            rec["error"] = str(exc)
            details.append(rec)
            continue

        m_ok = analysis.get("generation_mode") == exp["expected_mode"]
        p_ok = analysis.get("output_profile") == exp["expected_profile"]
        mode_ok += m_ok
        profile_ok += p_ok
        rec["mode_ok"] = m_ok
        rec["profile_ok"] = p_ok

        # P1-1 delivery-scope scoring: constraints count only at their declared
        #落点 (prompt_text / api_parameter / asset_payload). metadata_only never counts.
        with open(os.path.join(REPO_ROOT, "e2e", "cases", fn), "r", encoding="utf-8") as f:
            case_req = json.load(f)
        payload = build_payload(case_req, prompt, analysis.get("assets"))
        audit_failed = (audit.get("failed") or 0) > 0
        rec["audit_failed"] = audit_failed
        per_constraint = []
        keep = True
        for raw in exp.get("constraints", exp.get("hard_constraints", [])):
            c = normalize_constraint(raw)
            if c["delivery_scope"] == "metadata_only":
                per_constraint.append({"id": c["id"], "scope": "metadata_only", "counted": False})
                continue
            ok, why = check_constraint(payload, c)
            per_constraint.append({"id": c["id"], "scope": c["delivery_scope"], "ok": ok, "why": why})
            if not ok:
                keep = False
        if audit_failed:
            keep = False
        rec["constraints"] = per_constraint
        a_ok = all(c.get("ok", True) for c in per_constraint if c.get("scope") == "asset_payload")
        if not [c for c in per_constraint if c.get("scope") == "asset_payload"]:
            # no asset constraints on this case: asset check passes only if audit binding holds
            a_ok = not audit_failed
        asset_ok += bool(a_ok)
        rec["asset_ok"] = bool(a_ok)
        retention_ok += bool(keep)
        rec["retention_ok"] = bool(keep)

        res_hit = any(re.search(p, prompt) for p in TEMPLATE_RESIDUE_RES)
        pol_hit = any(t in prompt for t in POLLUTION_TOKENS)
        residue_hits += res_hit
        pollution_hits += pol_hit
        rec["template_residue"] = res_hit
        rec["audit_pollution"] = pol_hit
        binding_ok = bool(rec.get("audit_binding_ok"))
        if not binding_ok:
            # P1-3: tampered prompt, missing binding fields, or run/commit drift
            # fails retention even when every declared constraint is present.
            keep = False
            rec["retention_ok"] = False
            retention_ok += 0
        details.append(rec)

    binding_rate = round(sum(1 for d in details if d.get("audit_binding_ok")) / n, 4) if n else 0
    metrics = {
        "cases": n,
        "mode_route_accuracy": round(mode_ok / n, 4) if n else 0,
        "profile_route_accuracy": round(profile_ok / n, 4) if n else 0,
        "asset_check_accuracy": round(asset_ok / n, 4) if n else 0,
        "hard_constraint_retention": round(retention_ok / n, 4) if n else 0,
        "audit_binding_rate": binding_rate,
        "stale_dirs": stale_dirs,
        "missing_dirs": missing_dirs,
        "run_id_mismatch": run_id_mismatch,
        "run_id": summary_run_id,
        "source_commit": summary_source_commit,
        "summary_run_id": summary_run_id,
        "expected_case_count": n,
        "summary_case_count": len(summary_case_ids),
        "template_residue_rate": round(residue_hits / n, 4) if n else 0,
        "audit_pollution_rate": round(pollution_hits / n, 4) if n else 0,
        "cli_crashes": crashes,
        "json_parse_rate": round(json_ok / n, 4) if n else 0,
        "details": details,
    }
    out_path = os.path.join(run_dir, "_metrics.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in metrics.items() if k != "details"}, ensure_ascii=False, indent=2))

    gate = (
        metrics["mode_route_accuracy"] == 1.0
        and metrics["profile_route_accuracy"] == 1.0
        and metrics["asset_check_accuracy"] == 1.0
        and metrics["hard_constraint_retention"] == 1.0
        and metrics["audit_binding_rate"] == 1.0
        and not stale_dirs
        and not missing_dirs
        and not run_id_mismatch
        and summary_case_ids == sorted(fn[:-5] for fn in exp_files)
        and metrics["template_residue_rate"] == 0.0
        and metrics["audit_pollution_rate"] == 0.0
        and metrics["cli_crashes"] == 0
        and metrics["json_parse_rate"] == 1.0
    )
    print("E2E_METRICS_GATE:", "PASS" if gate else "FAIL")
    return 0 if gate else 1


if __name__ == "__main__":
    sys.exit(main())
