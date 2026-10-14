#!/usr/bin/env python3
"""Single-run closure evidence collector (P5, A-series hardened).

Every gate status is derived from evidence bound to THIS run and commit:
static/CLI gates are EXECUTED here with per-suite logs; local/agent/render/
video gates are recomputed or verified from explicit per-stage result files.
Emits:
  evidence/<run_id>/closure-report.json
  evidence/<run_id>/closure-report.md
  evidence/<run_id>/evidence-index.json
  evidence/<run_id>/sha256-manifest.json
  evidence/<run_id>/logs/*
Six split gates (PASS/FAIL/NOT_RUN/REVIEW_REQUIRED), each with evidence_paths,
evidence hashes, case counts, commit and timestamp. A PASS missing any required
evidence fails report generation. Never maps static-test success onto
Agent/model/video PASS. Old run_ids never backfill new ones.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
if os.path.join(REPO_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
from closure_gate_contract import (agent_gate, command_gate, local_gate,
                                   render_gate, video_gate)
from versioning import (VersionValidationError, check_single_source_version,
                        get_version)

MISSING_VALIDATOR = "MISSING_VALIDATOR"

EXTERNAL_VERIFIERS = {
    "REAL_AGENT_E2E": os.path.join("e2e", "real_agent", "verify_transcripts.py"),
    "MODEL_RENDER_E2E": os.path.join("e2e", "model_render",
                                     "verify_render_evidence.py"),
    "VIDEO_REVERSE_E2E": os.path.join("e2e", "video_reverse",
                                      "verify_video_evidence.py"),
}


def missing_validator_gate(gate_name: str, validator_rel: str, now: str) -> dict:
    """MM-03：外部校验器文件缺失时的诚实记录。

    工具缺件不是业务状态：状态记 NOT_RUN（可发布档位照旧），但必须带
    ``validator_status=MISSING_VALIDATOR`` 并在原因里点名缺失文件，不许
    复用“等待原生会话 / dry-run / 无视频包”等业务话术。
    """
    rel = validator_rel.replace("\\", "/")
    return {
        "status": "NOT_RUN",
        "validator_status": MISSING_VALIDATOR,
        "validator": rel,
        "reason": (f"外部校验器缺件（{MISSING_VALIDATOR}）：{rel} 不存在，"
                   f"无法执行 {gate_name} 验证；这是工具缺件，不是业务待跑"),
        "at": now,
        "evidence_paths": [],
    }


def sha_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def git_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="strict",
                          check=True, env=UTF8_ENV).stdout.strip()


def load_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None


def gate(name: str, status: str, paths: list[str], extra: dict | None = None,
         base_dir: str | None = None) -> dict:
    hashes, missing = {}, []
    for p in paths:
        candidates = []
        if base_dir:
            candidates.append(os.path.join(base_dir, p))
        candidates.append(os.path.join(REPO_ROOT, p))
        full = next((c for c in candidates if os.path.isfile(c)), None)
        if full:
            hashes[p] = sha_file(full)
        else:
            missing.append(p)
    doc = {"gate": name, "status": status, "evidence_paths": paths,
           "evidence_sha256": hashes, "missing": missing}
    if extra:
        doc.update(extra)
    if status == "PASS" and missing:
        doc["status"] = "FAIL"
        doc["fail_reason"] = f"PASS claimed with missing evidence: {missing}"
    return doc


# Gate registries. The names used to be hardcoded here — and that list named
# nine static suites plus one CLI suite from the historical master suite which
# is gone. Every one of them exited 2 in 50ms ("No such file or directory"), so
# STATIC_RULE_GATES and CLI_ENTRYPOINT_E2E have been *permanently* red since the
# loss: a gate that can never go green is not a gate. Registries are now derived
# from what actually exists in `_审查测试`, and the three segments partition it:
#   LOCAL  — full-chain/pipeline proofs and the gate-composition check (never
#            counted as "static audit"; also the recursion guard),
#   CLI    — suites that really spawn the command-line entrypoints,
#   STATIC — everything else (rule/structure/consistency scans).
AUDIT_DIR = os.path.join(REPO_ROOT, "_审查测试")

LOCAL_SUITES = {
    "test_episode_pipeline.py",
    "test_render_episode_pack.py",
    "test_run_all_gate.py",
}

CLI_SUITES = {
    "test_verification_protocol.py",
    "test_skill_structure_guards.py",
}


def _audit_suites(audit_dir: str | os.PathLike = AUDIT_DIR) -> list[str]:
    return sorted(p.name for p in Path(audit_dir).glob("test_*.py")
                  if p.name != "run_all.py")


def discover_cli_suites(audit_dir: str | os.PathLike = AUDIT_DIR) -> list[str]:
    return [s for s in _audit_suites(audit_dir) if s in CLI_SUITES]


def discover_static_suites(audit_dir: str | os.PathLike = AUDIT_DIR) -> list[str]:
    taken = LOCAL_SUITES | set(discover_cli_suites(audit_dir))
    return [s for s in _audit_suites(audit_dir) if s not in taken]


def parse_suite_counts(output: str) -> tuple[int, int]:
    """Same contract as _审查测试/run_all.py: 'Result: X/Y PASS' or [PASS] lines.

    Also understands unittest's own tail line (`Ran 27 tests in 0.5s`) — without
    it every gate log reported `0/0`, which reads like an empty run rather than
    like 27 checks that passed.
    """
    m = re.search(r"Result:\s*(\d+)/(\d+)\s*PASS", output)
    if m:
        return int(m.group(1)), int(m.group(2))
    pass_cnt = len(re.findall(r"^\s*\[PASS\]", output, re.MULTILINE))
    fail_cnt = len(re.findall(r"^\s*\[FAIL\]", output, re.MULTILINE))
    if not (pass_cnt or fail_cnt):
        ran = re.search(r"Ran (\d+) tests? in", output)
        if ran:
            n = int(ran.group(1))
            return n, n
    return pass_cnt, pass_cnt + fail_cnt


def run_gate_suites(suites: list[str], out_dir: str, gate_name: str,
                    run_id: str, source_commit: str) -> dict:
    """Execute gate suites now and persist per-suite logs plus an index.

    Returns the index dict extended with index_path and log_sha256; the
    caller alone turns (exit_code, run_id, run_commit, current_commit,
    log_sha256) into a gate status via closure_gate_contract.command_gate.
    """
    logs_dir = os.path.join(out_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    entries: list[dict] = []
    overall_rc = 0
    for suite in suites:
        suite_started = datetime.now(timezone.utc).isoformat()
        suite_path = os.path.join(REPO_ROOT, "_审查测试", suite)
        if os.path.isfile(suite_path):
            proc = subprocess.run([sys.executable, suite_path],
                                  cwd=REPO_ROOT, capture_output=True, env=UTF8_ENV)
            stdout = proc.stdout.decode("utf-8", errors="replace")
            stderr = proc.stderr.decode("utf-8", errors="replace")
            rc = proc.returncode
        else:
            # A stale registry entry is a broken lock, not a failed run: say so
            # in plain words instead of letting python's traceback be the note.
            rc = 97
            stdout = ""
            stderr = (f"登记的 {gate_name} 段套件文件不存在：{suite_path}\n"
                      f"这是采集器自己的口径腐坏（门被堵死），不是模型或规则跑挂了。"
                      f"修法：把登记表改为按目录发现，或删除失效条目。")
            overall_rc = overall_rc or rc
        log_rel = f"logs/{gate_name}-{suite}.log"
        log_path = os.path.join(out_dir, log_rel)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(stdout)
            if stderr.strip():
                f.write("\n--- STDERR ---\n" + stderr)
        passed, total = parse_suite_counts(stdout + "\n" + stderr)
        entries.append({
            "suite": suite, "exit_code": rc,
            "passed": passed, "total": total,
            "started_at": suite_started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "log": log_rel, "log_sha256": sha_file(log_path),
        })
        if proc.returncode != 0 and overall_rc == 0:
            overall_rc = proc.returncode
    if not entries:
        # An empty segment is not "nothing left to prove": it means the registry
        # rotted, and command_gate would otherwise read rc=0 as PASS.
        overall_rc = 96
    index: dict = {
        "schema_version": "1.0.0",
        "gate": gate_name,
        "run_id": run_id,
        "source_commit": source_commit,
        "exit_code": overall_rc,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "suites": entries,
    }
    index_rel = f"logs/{gate_name}-index.json"
    with open(os.path.join(out_dir, index_rel), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    index["index_path"] = index_rel
    index["log_sha256"] = sha_file(os.path.join(out_dir, index_rel))
    return index


ORIGINAL_BASES = ("evidence_root", "run_dir")


def external_original_entry(rec: dict, gate_name: str, out_dir: str, run_id: str,
                            commit: str, seen: set) -> dict:
    """P0-4: recompute one declared private original, never trust the record.

    A gate may declare, per case, the original it actually measured: a
    native transcript, a rendered media file, a licensed source video. Those
    originals stay in the restricted evidence root and are not shipped, so
    the only honest way to describe them is the byte count and SHA-256 of
    the file read at index time. Anything unverifiable — an absolute or
    escaping path, a symlink leaving its base, a duplicate case, a missing
    origin label, a declared hash that disagrees with disk — aborts
    collection: an index that cannot be recomputed must not exist.
    """
    if not isinstance(rec, dict):
        raise ValueError(f"{gate_name}: external original record must be an object")
    case_id = rec.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError(f"{gate_name}: external original without a case_id")
    if (gate_name, case_id) in seen:
        raise ValueError(f"{gate_name}: duplicate external original for case {case_id!r}")
    seen.add((gate_name, case_id))
    origin = rec.get("origin")
    if not isinstance(origin, str) or not origin:
        raise ValueError(f"{gate_name}/{case_id}: external original without an origin")
    base_name = rec.get("base", "evidence_root")
    if base_name not in ORIGINAL_BASES:
        raise ValueError(f"{gate_name}/{case_id}: unknown external base {base_name!r}")
    base = (os.path.dirname(os.path.abspath(out_dir)) if base_name == "evidence_root"
            else os.path.abspath(out_dir))
    rel = rec.get("path")
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        raise ValueError(f"{gate_name}/{case_id}: external original path must be relative")
    full = os.path.realpath(os.path.join(base, rel))
    root = os.path.realpath(base)
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        raise ValueError(
            f"{gate_name}/{case_id}: external original missing or escaping {base_name}: {rel}")
    digest = sha_file(full)
    declared = rec.get("sha256")
    if declared is not None and declared != digest:
        raise ValueError(
            f"{gate_name}/{case_id}: declared sha256 disagrees with the bytes on disk")
    entry = {"path": rel, "sha256": digest, "bytes": os.path.getsize(full),
             "run_id": run_id, "source_commit": commit, "type": gate_name,
             "kind": "external_original", "case_id": case_id, "origin": origin,
             "base": base_name}
    if "authorized" in rec:
        entry["authorized"] = rec.get("authorized") is True
    return entry


def build_evidence_index(out_dir: str, gates: dict, run_id: str,
                         commit: str) -> list[dict]:
    """A07/P0-4: one index entry per hashed file plus every declared original.

    Also indexes each gate's ``external_originals`` (private per-case
    originals kept in the restricted evidence root) with bytes recomputed
    here, so the release builder can recheck the same bytes in an authorized
    environment instead of trusting a bare digest.
    """
    entries: list[dict] = []
    seen_originals: set = set()
    for gate_name, g in gates.items():
        for path, sha in (g.get("evidence_sha256") or {}).items():
            size = None
            for base in (out_dir, REPO_ROOT):
                cand = os.path.join(base, path)
                if os.path.isfile(cand):
                    size = os.path.getsize(cand)
                    break
            entries.append({"path": path, "sha256": sha, "bytes": size,
                            "run_id": run_id, "source_commit": commit,
                            "type": gate_name, "kind": "file"})
        for rec in (g.get("external_originals") or ()):
            entries.append(external_original_entry(
                rec, gate_name, out_dir, run_id, commit, seen_originals))
    return entries


def evaluate_local_run(run_dir: str, out_dir: str, head_commit: str,
                       now: str) -> dict:
    """Recompute the 30-case local batch binding from disk bytes (A03).

    Nothing is trusted from the recorded metrics except cross-checking: the
    audit sha vs disk bytes, per-case run_id parity, case coverage and the
    binding rate are all recomputed here before local_gate decides. Copies
    of the batch summary/metrics plus the recomputed binding land in the
    evidence run dir under logs/.
    """
    if not os.path.isdir(run_dir):
        return {"status": "FAIL", "reason": f"--local-run-dir does not exist: {run_dir}",
                "at": now, "evidence_paths": []}
    summary = load_json(os.path.join(run_dir, "_summary.json"))
    recorded = load_json(os.path.join(run_dir, "_metrics.json"))
    if not isinstance(summary, dict) or not isinstance(recorded, dict):
        return {"status": "FAIL",
                "reason": "run dir missing/unparseable _summary.json or _metrics.json",
                "at": now, "evidence_paths": []}
    expected_dir = os.path.join(REPO_ROOT, "e2e", "expected")
    exp_files = sorted(f for f in os.listdir(expected_dir) if f.endswith(".json"))
    if len(exp_files) != 30:
        return {"status": "FAIL",
                "reason": f"expected case count drifted: {len(exp_files)} (freeze broken)",
                "at": now, "evidence_paths": []}
    batch_run_id = summary.get("run_id")
    if not batch_run_id:
        return {"status": "FAIL", "reason": "summary has no run_id", "at": now,
                "evidence_paths": []}
    per_case: dict = {}
    bound = 0
    for fn in exp_files:
        case_id = fn[:-5]
        try:
            with open(os.path.join(run_dir, case_id, "prompt.txt"), "rb") as f:
                disk_sha = hashlib.sha256(f.read()).hexdigest()
            with open(os.path.join(run_dir, case_id, "analysis.json"), "r", encoding="utf-8") as f:
                analysis = json.load(f)
            with open(os.path.join(run_dir, case_id, "audit-report.json"), "r", encoding="utf-8") as f:
                audit = json.load(f)
        except Exception as exc:
            per_case[case_id] = {"ok": False, "why": f"unreadable evidence: {exc}"}
            continue
        ok = (audit.get("prompt_sha256") == disk_sha
              and (audit.get("total") or 0) > 0
              and (audit.get("failed") or 0) == 0
              and analysis.get("run_id") == batch_run_id
              and audit.get("run_id") == batch_run_id
              and analysis.get("case_id") == case_id
              and audit.get("case_id") == case_id)
        per_case[case_id] = {"ok": ok, "disk_sha256": disk_sha,
                             "audit_sha256": audit.get("prompt_sha256")}
        if ok:
            bound += 1
    dirs = sorted(d for d in os.listdir(run_dir) if os.path.isdir(os.path.join(run_dir, d)))
    stale = sorted(set(dirs) - {f[:-5] for f in exp_files})
    missing = sorted({f[:-5] for f in exp_files} - set(dirs))
    rate = round(bound / len(exp_files), 4)
    recomputed = {"run_id": batch_run_id, "source_commit": summary.get("source_commit"),
                  "cases": len(exp_files), "audit_binding_rate": rate}
    status = local_gate(recomputed, {"run_id": batch_run_id,
                                     "source_commit": summary.get("source_commit"),
                                     "cases": len(exp_files)},
                        batch_run_id, head_commit)
    if status == "PASS":
        checks = [
            (recorded.get("run_id") == batch_run_id, "metrics run_id differs from summary"),
            (recorded.get("cases") == len(exp_files), "metrics case count differs"),
            (recorded.get("audit_binding_rate") == rate, "recorded binding rate != recomputed"),
            (recorded.get("mode_route_accuracy") == 1.0, "mode_route_accuracy != 1.0"),
            (recorded.get("profile_route_accuracy") == 1.0, "profile_route_accuracy != 1.0"),
            (recorded.get("asset_check_accuracy") == 1.0, "asset_check_accuracy != 1.0"),
            (recorded.get("hard_constraint_retention") == 1.0, "hard_constraint_retention != 1.0"),
            (recorded.get("template_residue_rate") == 0.0, "template_residue_rate != 0.0"),
            (recorded.get("audit_pollution_rate") == 0.0, "audit_pollution_rate != 0.0"),
            (recorded.get("cli_crashes") == 0, "cli_crashes != 0"),
            (recorded.get("json_parse_rate") == 1.0, "json_parse_rate != 1.0"),
            (not recorded.get("stale_dirs"), "metrics lists stale dirs"),
            (not recorded.get("missing_dirs"), "metrics lists missing dirs"),
            (not recorded.get("run_id_mismatch"), "metrics lists run_id mismatch"),
            (not stale, f"stale case dirs present: {stale}"),
            (not missing, f"missing case dirs: {missing}"),
        ]
        bad = [msg for ok2, msg in checks if not ok2]
        if bad:
            status = "FAIL"
            per_case["_recorded_metrics_conflicts"] = bad
    # Copy the batch's own result files into the evidence run dir so the
    # report's hashes describe bytes that survive the batch directory.
    os.makedirs(os.path.join(out_dir, "logs"), exist_ok=True)
    copied = []
    for name, data in (("local-summary.json", summary), ("local-metrics.json", recorded)):
        rel = f"logs/{name}"
        with open(os.path.join(out_dir, rel), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        copied.append(rel)
    binding_rel = "logs/local-binding.json"
    with open(os.path.join(out_dir, binding_rel), "w", encoding="utf-8") as f:
        json.dump({"batch_run_id": batch_run_id, "head_commit": head_commit,
                   "recomputed_rate": rate, "bound": bound, "cases": len(exp_files),
                   "stale_dirs": stale, "missing_dirs": missing, "per_case": per_case},
                  f, ensure_ascii=False, indent=2)
    copied.append(binding_rel)
    doc = {"status": status, "run_id": batch_run_id,
           "source_commit": summary.get("source_commit"),
           "cases": len(exp_files), "bound": bound, "audit_binding_rate": rate,
           "stale_dirs": stale, "missing_dirs": missing, "run_dir": run_dir,
           "evidence_paths": copied, "at": now}
    if status != "PASS":
        doc["reason"] = "local batch failed recomputation/binding checks"
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect single-run closure evidence (P5)")
    ap.add_argument("--out-root", default=os.path.join(REPO_ROOT, "evidence"))
    ap.add_argument("--local-run-dir", default=None,
                    help="Explicit 30-case local pipeline run directory "
                         "(required evidence for the LOCAL gate)")
    ap.add_argument("--render-run-id", default=None,
                    help="Explicit model-render run id scored by "
                         "verify_render_evidence.py (required for the RENDER gate)")
    args = ap.parse_args()
    try:
        report_version = check_single_source_version(REPO_ROOT)
    except VersionValidationError as exc:
        print(f"单一真源版本号漂移（MM-01），拒绝采集：{exc}", file=sys.stderr)
        return 1
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    commit = git_head()
    now = datetime.now(timezone.utc).isoformat()
    out_dir = os.path.join(args.out_root, run_id)
    os.makedirs(out_dir, exist_ok=False)

    # A01: the static gate is derived from an execution bound to THIS run and
    # commit, never asserted. run_commit is re-read afterwards so a tree that
    # moved mid-collection fails the commit binding inside command_gate.
    static_index = run_gate_suites(discover_static_suites(), out_dir, "static",
                                   run_id, commit)
    run_commit = git_head()
    static_status = command_gate(static_index["exit_code"], run_id,
                                 run_commit, commit, static_index["log_sha256"])
    static = {
        "status": static_status, "at": now,
        "via": "collector-executed static suites (logs/static-index.json)",
        "run_id": run_id, "source_commit": commit, "run_commit": run_commit,
        "exit_code": static_index["exit_code"],
        "suites": [e["suite"] for e in static_index["suites"]],
        "passed": sum(e["passed"] for e in static_index["suites"]),
        "total": sum(e["total"] for e in static_index["suites"]),
        "started_at": static_index["started_at"],
        "finished_at": static_index["finished_at"],
        "log_sha256": static_index["log_sha256"],
    }
    # A02: the CLI entrypoint gate carries the same execution binding as the
    # static gate — run the CLI suites now, keep their logs, bind run/commit.
    cli_index = run_gate_suites(discover_cli_suites(), out_dir, "cli", run_id, commit)
    cli_status = command_gate(cli_index["exit_code"], run_id,
                              run_commit, commit, cli_index["log_sha256"])
    cli = {
        "status": cli_status, "at": now,
        "via": "collector-executed CLI entrypoint suite (logs/cli-index.json)",
        "run_id": run_id, "source_commit": commit, "run_commit": run_commit,
        "exit_code": cli_index["exit_code"],
        "suites": [e["suite"] for e in cli_index["suites"]],
        "passed": sum(e["passed"] for e in cli_index["suites"]),
        "total": sum(e["total"] for e in cli_index["suites"]),
        "started_at": cli_index["started_at"],
        "finished_at": cli_index["finished_at"],
        "log_sha256": cli_index["log_sha256"],
    }

    # A03: the LOCAL gate reads ONE explicit run directory and recomputes the
    # binding itself; the fixed e2e/results default is gone on purpose so a
    # stale batch, a missing field, or a mixed run_id can never PASS.
    if args.local_run_dir:
        local = evaluate_local_run(args.local_run_dir, out_dir, commit, now)
    else:
        local = {"status": "NOT_RUN", "reason": "no --local-run-dir given",
                 "at": now, "evidence_paths": []}

    # A04: the collector only executes the verifier and reads the v2 review
    # file; coverage/provenance judgment lives in the pure agent_gate. The
    # R1-suspended reality (5 dev smokes, no v2 review) stays REVIEW_REQUIRED.
    # MM-03: a missing verifier is a tool gap, never a business "awaiting" state.
    _ra_verifier = os.path.join(REPO_ROOT, EXTERNAL_VERIFIERS["REAL_AGENT_E2E"])
    if not os.path.isfile(_ra_verifier):
        real_agent = missing_validator_gate(
            "REAL_AGENT_E2E", EXTERNAL_VERIFIERS["REAL_AGENT_E2E"], now)
        real_agent["verified_cases"] = 0
        ra_doc = None
        real_agent_out = None
    else:
        real_agent_out = subprocess.run(
            [sys.executable, _ra_verifier],
            capture_output=True, text=True, encoding="utf-8", errors="strict",
            cwd=REPO_ROOT, env=UTF8_ENV)
        try:
            ra_doc = json.loads(real_agent_out.stdout)
        except Exception:
            ra_doc = {}
    if real_agent_out is None:
        pass
    elif real_agent_out.returncode == 2:
        # T7 / P0-3: the dev smoke batch lives isolated in results_smoke/; the main
        # results directory holds the 30 frozen cases. When 0 native sessions exist,
        # the gate honestly reports NOT_RUN.
        real_agent = {"status": "NOT_RUN",
                      "reason": "awaiting native Agent session channel: 0/30 frozen "
                                "cases have native sessions (dev smoke isolated in "
                                "e2e/real_agent/results_smoke)",
                      "at": now, "evidence_paths": [], "verified_cases": 0}
    elif not isinstance(ra_doc, dict) or not ra_doc:
        real_agent = {"status": "FAIL", "reason": "verifier produced no parseable output",
                      "at": now, "evidence_paths": []}
    else:
        manifest = load_json(os.path.join(REPO_ROOT, "e2e", "real_agent", "case-manifest.json")) or {}
        frozen_ids = [c.get("case_id") for c in manifest.get("cases", []) if c.get("case_id")]
        verified = ra_doc.get("verified_case_ids", [])
        sessions = ra_doc.get("session_ids", {})
        failed = ra_doc.get("failed_case_ids", [])
        review_doc = load_json(os.path.join(REPO_ROOT, "e2e", "real_agent", "human-review.json")) or {}
        review = {}
        if review_doc.get("schema_version") == "2.0" and isinstance(review_doc.get("cases"), dict):
            review = {cid: (c.get("verdict") or "")
                      for cid, c in review_doc["cases"].items() if isinstance(c, dict)}
        covered = bool(frozen_ids) and set(frozen_ids).issubset(set(verified))
        skill_ok = bool(ra_doc.get("skill_sha256_all_match"))
        # Skill drift only hard-fails a FULLY covering batch; the suspended
        # smoke batch must stay REVIEW_REQUIRED regardless of drift.
        evidence_commit = commit if (skill_ok or not covered) else "SKILL_DRIFT"
        ra_status = agent_gate(frozen_ids, verified, sessions, review,
                               evidence_commit, commit, failed_case_ids=failed)
        real_agent = {
            "status": ra_status, "at": now,
            "verified_cases": len(verified), "failed_cases": len(failed),
            "manifest_total": len(frozen_ids),
            "missing_manifest_cases": ra_doc.get("missing_manifest_cases", []),
            "skill_sha256_all_match": skill_ok,
            "human_review_schema": review_doc.get("schema_version"),
            "review_entries": len(review),
        }
        if ra_status == "REVIEW_REQUIRED":
            real_agent["reason"] = ("smoke/partial transcripts pass machine checks; "
                                    "30 frozen-case native sessions + v2 human-review.json "
                                    "are still required before PASS")
        elif ra_status == "FAIL":
            real_agent["reason"] = "agent_gate rejected evidence (sessions/verdict/skill binding)"

    # A05: render evidence is scored by the independent verifier for ONE
    # explicitly named run; a summary of SUCCEEDED strings alone can never
    # promote the gate, and machine failures stay FAIL.
    render_run_id = args.render_run_id
    if not render_run_id:
        model_render = {"status": "NOT_RUN", "reason": "no --render-run-id given",
                        "at": now, "evidence_paths": []}
    else:
        _render_verifier = os.path.join(
            REPO_ROOT, EXTERNAL_VERIFIERS["MODEL_RENDER_E2E"])
        if not os.path.isfile(_render_verifier):
            # MM-03: 缺校验器不许复用 dry-run 业务话术。
            model_render = missing_validator_gate(
                "MODEL_RENDER_E2E", EXTERNAL_VERIFIERS["MODEL_RENDER_E2E"], now)
            model_render["run_id"] = render_run_id
            vr = None
            vr_doc = None
        else:
            vr = subprocess.run(
                [sys.executable, _render_verifier, render_run_id],
                capture_output=True, text=True, encoding="utf-8", errors="strict",
                cwd=REPO_ROOT, env=UTF8_ENV)
            try:
                vr_doc = json.loads(vr.stdout)
            except Exception:
                vr_doc = {}
        if vr is None:
            pass
        elif vr.returncode == 2 or vr_doc.get("MODEL_RENDER_E2E") == "NOT_RUN":
            model_render = {"status": "NOT_RUN",
                            "reason": vr_doc.get("reason", "dry-run only; no live tasks"),
                            "run_id": render_run_id, "at": now,
                            "evidence_paths": [
                                f"e2e/model_render/results/{render_run_id}/_summary.json",
                                "e2e/model_render/verify_render_evidence.py",
                                "scripts/h3_v2_client.py"]}
        elif not isinstance(vr_doc, dict) or not vr_doc:
            model_render = {"status": "FAIL", "reason": "render verifier produced no parseable output",
                            "run_id": render_run_id, "at": now, "evidence_paths": []}
        else:
            live = bool(vr_doc.get("live"))
            review = vr_doc.get("review", {})
            verified_modes = vr_doc.get("verified_modes", [])
            machine_errors = vr_doc.get("errors", {})
            summary_commit = vr_doc.get("summary_source_commit")
            if not live:
                mr_status = "NOT_RUN"
            elif machine_errors:
                mr_status = "FAIL"
            else:
                mr_status = render_gate(
                    verified_modes,
                    ["t2va", "i2va", "fl2va", "l2va", "ref2va"],
                    live, review,
                    summary_commit if summary_commit else "UNBOUND", commit)
            model_render = {
                "status": mr_status, "run_id": render_run_id, "at": now,
                "live": live, "verified_modes": verified_modes,
                "machine_error_count": len(machine_errors),
                "review_entries": len(review),
                "review_schema": vr_doc.get("review_schema"),
                "summary_source_commit": summary_commit,
                "evidence_paths": [
                    f"e2e/model_render/results/{render_run_id}/_summary.json",
                    f"e2e/model_render/results/{render_run_id}/content-review.json",
                    "e2e/model_render/verify_render_evidence.py",
                    "scripts/h3_v2_client.py"],
            }
            if mr_status == "REVIEW_REQUIRED":
                model_render["reason"] = ("machine-verified live renders present; "
                                          "per-mode v2 content review still required")
            elif mr_status == "FAIL":
                model_render["reason"] = "machine verification or gate binding failed"

    # A06: package machine-verification via the dedicated verifier; the pure
    # video_gate decides (fixtures never count, <5 real videos stays honest).
    # MM-03: 缺校验器不许复用“无视频包”业务话术。
    _video_verifier = os.path.join(
        REPO_ROOT, EXTERNAL_VERIFIERS["VIDEO_REVERSE_E2E"])
    if not os.path.isfile(_video_verifier):
        video = missing_validator_gate(
            "VIDEO_REVERSE_E2E", EXTERNAL_VERIFIERS["VIDEO_REVERSE_E2E"], now)
        vv = None
        vv_doc = None
    else:
        vv = subprocess.run(
            [sys.executable, _video_verifier],
            capture_output=True, text=True, encoding="utf-8", errors="strict",
            cwd=REPO_ROOT, env=UTF8_ENV)
        try:
            vv_doc = json.loads(vv.stdout)
        except Exception:
            vv_doc = {}
    if vv is None:
        pass
    elif vv.returncode == 2 or not isinstance(vv_doc, dict) or not vv_doc:
        video = {"status": "NOT_RUN",
                 "reason": "no video packages: 5 authorized real videos pending "
                           "(synthetic fixtures never count as evidence)",
                 "at": now, "evidence_paths": []}
    else:
        v_status = "FAIL" if vv_doc.get("errors") else video_gate(
            vv_doc.get("videos", []), vv_doc.get("review", {}))
        video = {
            "status": v_status, "at": now,
            "real_videos": vv_doc.get("real_count", 0),
            "fixture_packages": vv_doc.get("fixture_count", 0),
            "broken_packages": len(vv_doc.get("errors", {})),
            "review_entries": len(vv_doc.get("review", {})),
            "evidence_paths": ["e2e/video_reverse/verify_video_evidence.py",
                               "scripts/video_analyze.py",
                               "_审查测试/test_video_reverse_evidence.py"],
        }
        if v_status == "REVIEW_REQUIRED":
            video["reason"] = ("verified real packages present; five-package coverage "
                               "or per-claim human review still incomplete")
        elif v_status == "FAIL":
            video["reason"] = "broken/unauthorized packages or failed review verdicts"

    gates = {
        "STATIC_RULE_GATES": gate("STATIC_RULE_GATES", static["status"],
                                  [static_index["index_path"]]
                                  + [e["log"] for e in static_index["suites"]],
                                  static, base_dir=out_dir),
        "CLI_ENTRYPOINT_E2E": gate("CLI_ENTRYPOINT_E2E", cli["status"],
                                   [cli_index["index_path"]]
                                   + [e["log"] for e in cli_index["suites"]],
                                   cli, base_dir=out_dir),
        "LOCAL_PIPELINE_E2E": gate("LOCAL_PIPELINE_E2E", local["status"],
                                   local.get("evidence_paths", []),
                                   local, base_dir=out_dir),
        "REAL_AGENT_E2E": gate("REAL_AGENT_E2E", real_agent["status"],
                               ["e2e/real_agent/verify_transcripts.py",
                                "e2e/real_agent/case-manifest.json",
                                "e2e/real_agent/README.md"], real_agent, base_dir=out_dir),
        "MODEL_RENDER_E2E": gate("MODEL_RENDER_E2E", model_render["status"],
                                 model_render.get("evidence_paths", []),
                                 model_render, base_dir=out_dir),
        "VIDEO_REVERSE_E2E": gate("VIDEO_REVERSE_E2E", video["status"],
                                  video.get("evidence_paths", []),
                                  video, base_dir=out_dir),
    }
    # A07: full evidence index + its hash inside the report, so the release
    # builder can recompute every artifact byte before publishing.
    index_entries = build_evidence_index(out_dir, gates, run_id, commit)
    index_path = os.path.join(out_dir, "evidence-index.json")
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index_entries, f, ensure_ascii=False, indent=2)
    try:
        branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="strict",
            check=True, env=UTF8_ENV).stdout.strip()
    except subprocess.SubprocessError:
        branch = "DETACHED_HEAD"
    report = {"run_id": run_id, "commit": commit, "branch": branch,
              "version": report_version, "created_at": now,
              "gates": gates,
              "evidence_index_sha256": sha_file(index_path)}
    with open(os.path.join(out_dir, "closure-report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    lines = [f"# Closure Report {run_id}", "", f"commit: {commit}", ""]
    for name, g in gates.items():
        lines.append(f"- {name}: {g['status']}")
    with open(os.path.join(out_dir, "closure-report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    manifest = {}
    for root, _, files in os.walk(out_dir):
        for fn in sorted(files):
            full = os.path.join(root, fn)
            manifest[os.path.relpath(full, out_dir).replace("\\", "/")] = sha_file(full)
    with open(os.path.join(out_dir, "sha256-manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v["status"] for k, v in gates.items()}, ensure_ascii=False, indent=2))
    print(f"closure-report written: {out_dir}")
    # Collector exit: 0 when report built (even with NOT_RUN gates); 1 only on build failure.
    return 0


if __name__ == "__main__":
    sys.exit(main())
