#!/usr/bin/env python3
"""Local 30-case batch driver (LOCAL_PIPELINE_E2E evidence producer, rebuilt).

The historical e2e batch harness (driver + case corpus) was lost with the
7.3.1 tooling; this driver regenerates the missing execution layer on top of
the unchanged generator (``generate_output.generate_case``) and the unchanged
measurer (``measure_e2e_metrics.py``):

  1. one shared run-id for the whole batch (binding requires per-case parity);
  2. execute every frozen case in ``e2e/cases`` against the real chain
     (route -> draft -> audit CLI -> atomic emit);
  3. write ``_summary.json`` in exactly the shape the measurer and
     ``collect_closure_evidence.evaluate_local_run`` bind against;
  4. delegate all scoring to ``measure_e2e_metrics.py`` (``_metrics.json``).

Exit codes: 0 batch clean and metrics gate PASS; 1 gate FAIL; 2 crashed.

Run:

    python scripts/run_local_batch.py [--run-id <id>] [--out-root e2e/results]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from uuid import uuid4

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from generate_output import generate_case, git_head  # noqa: E402

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def mint_run_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"


def frozen_case_ids(expected_dir: str) -> list[str]:
    """The frozen case list IS the expected directory: one json per case."""
    return sorted(p[:-5] for p in os.listdir(expected_dir) if p.endswith(".json"))


def build_summary(run_id: str, source_commit: str, case_ids: list[str],
                  crashes: int) -> dict:
    return {
        "run_id": run_id,
        "source_commit": source_commit,
        "crashes": crashes,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases": [{"case_file": cid + ".json"} for cid in case_ids],
    }


def run_batch(cases_dir: str, expected_dir: str, out_root: str,
              run_id: str | None = None) -> dict:
    """Execute every frozen case; returns {run_id, run_dir, results, crashes}."""
    run_id = run_id or mint_run_id()
    if not SAFE_ID_RE.match(run_id):
        raise SystemExit(f"unsafe run-id: {run_id!r}")
    case_ids = frozen_case_ids(expected_dir)
    run_dir = os.path.join(out_root, run_id)
    os.makedirs(run_dir, exist_ok=True)
    results = []
    crashes = 0
    for cid in case_ids:
        case_path = os.path.join(cases_dir, cid + ".json")
        try:
            res = generate_case(case_path, out_root, run_id=run_id, replace=True)
            rc = int(res.get("exit_code", 2))
        except Exception as exc:  # generator fail-closed: loud, recorded, counted
            res = {"case_id": cid, "run_id": run_id, "exit_code": 2,
                   "error": f"{type(exc).__name__}: {exc}"}
            rc = 2
        if rc == 2:
            crashes += 1
        results.append(res)
    summary = build_summary(run_id, git_head(), case_ids, crashes)
    with open(os.path.join(run_dir, "_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return {"run_id": run_id, "run_dir": run_dir, "results": results,
            "crashes": crashes, "summary": summary}


def run_measure(run_dir: str) -> int:
    """Delegate scoring to the untouched measurer; return its exit code."""
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "measure_e2e_metrics.py"),
         "--run-dir", run_dir],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8",
                              "PYTHONUTF8": "1"})
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the frozen local 30-case batch")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--out-root", default=os.path.join(REPO_ROOT, "e2e", "results"))
    args = ap.parse_args()
    batch = run_batch(os.path.join(REPO_ROOT, "e2e", "cases"),
                      os.path.join(REPO_ROOT, "e2e", "expected"),
                      args.out_root, args.run_id)
    rc_measure = run_measure(batch["run_dir"])
    failed_cases = [r["case_id"] for r in batch["results"]
                    if int(r.get("exit_code", 2)) != 0]
    print(json.dumps({"run_id": batch["run_id"], "run_dir": batch["run_dir"],
                      "cases": len(batch["results"]), "crashes": batch["crashes"],
                      "failed_cases": failed_cases, "measure_rc": rc_measure},
                     ensure_ascii=False))
    if batch["crashes"] or failed_cases or rc_measure != 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
