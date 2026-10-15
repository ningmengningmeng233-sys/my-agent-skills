#!/usr/bin/env python3
"""Pure closure-gate judgment layer for MiniMax H3 Craft (A-series).

Judgment only: no subprocesses, no wall clock, no I/O except verify_index's
explicit file reads. Collectors and the release builder do the I/O and
translate results through these functions. Status transitions are closed:

    no evidence                    -> NOT_RUN
    evidence broken/failed/absurd  -> FAIL
    machine ok, human review open  -> REVIEW_REQUIRED
    machine + human both covering  -> PASS

A FAIL is never silently erased by a later successful run: every run_id keeps
its own evidence directory and acceptance binds to one explicit run.
"""

from __future__ import annotations

import hashlib
import os

GATE_ORDER = (
    "STATIC_RULE_GATES",
    "CLI_ENTRYPOINT_E2E",
    "LOCAL_PIPELINE_E2E",
    "REAL_AGENT_E2E",
    "MODEL_RENDER_E2E",
    "VIDEO_REVERSE_E2E",
)
LOCAL_GATES = GATE_ORDER[:3]
EXTERNAL_GATES = GATE_ORDER[3:]
RELEASE_TIERS = ("local-stable", "full-closure")


def _is_sha256(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value.lower()))


def command_gate(exit_code, run_id, run_commit, current_commit, log_sha256) -> str:
    """Status of a gate proven by one command execution (A01/A02).

    exit_code: exit status of the gate command (0 = success).
    run_id: run identifier the execution is bound to (non-empty).
    run_commit: commit the execution ran against.
    current_commit: commit evidence is being collected for.
    log_sha256: SHA-256 of this run's log index; None when no log exists.
    """
    if exit_code != 0:
        return "FAIL"          # positive evidence of a failed run
    if log_sha256 is None:
        return "NOT_RUN"       # a success claim without a log cannot be bound
    if not _is_sha256(log_sha256):
        return "FAIL"          # malformed log hash is broken evidence
    if run_commit != current_commit:
        return "FAIL"          # evidence from another commit never counts
    if not isinstance(run_id, str) or not run_id:
        return "FAIL"
    return "PASS"


def local_gate(metrics, summary, run_id, current_commit, expected_cases=30) -> str:
    """Binding judgment for the 30-case local pipeline batch (A03).

    metrics/summary are the binding dicts for ONE batch (already normalized
    to ints by the collector). The batch passes only when both sides exist,
    share one run_id, record the current commit, cover exactly
    expected_cases, and carry a present-and-perfect audit_binding_rate.
    """
    if not isinstance(metrics, dict) and not isinstance(summary, dict):
        return "NOT_RUN"
    if not isinstance(metrics, dict) or not isinstance(summary, dict):
        return "FAIL"
    if not isinstance(run_id, str) or not run_id:
        return "FAIL"
    if summary.get("run_id") != run_id or metrics.get("run_id") != run_id:
        return "FAIL"
    if metrics.get("source_commit") != current_commit:
        return "FAIL"
    if summary.get("source_commit") != current_commit:
        return "FAIL"
    if metrics.get("cases") != expected_cases or summary.get("cases") != expected_cases:
        return "FAIL"
    rate = metrics.get("audit_binding_rate")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate != 1.0:
        return "FAIL"
    return "PASS"


def agent_gate(case_ids, verified_case_ids, session_ids, review,
               evidence_commit, current_commit, failed_case_ids=None) -> str:
    """Native 30-case provenance judgment for REAL_AGENT_E2E (A04).

    Order matters: an explicitly failed human verdict or a machine-failed
    case is positive failure evidence (FAIL) at any coverage; incomplete
    coverage of the frozen case list is honest incompleteness
    (REVIEW_REQUIRED — a dev smoke batch must stay there, never FAIL); only
    a fully covering batch gets the deep integrity checks (unique non-empty
    native session ids) whose violation is FAIL.
    """
    verified = set(verified_case_ids or ())
    sessions = dict(session_ids or {})
    review = dict(review or {})
    failed = set(failed_case_ids or ())
    frozen = list(case_ids or [])
    if evidence_commit != current_commit:
        return "FAIL"          # sessions from another commit never count
    if not verified and not failed and not review:
        return "NOT_RUN"
    if not verified and review:
        return "FAIL"          # review without verified artifacts is fabricated
    if any(v == "FAIL" for v in review.values()):
        return "FAIL"
    if failed:
        return "FAIL"
    if not frozen:
        return "FAIL"
    if not set(frozen).issubset(verified):
        return "REVIEW_REQUIRED"
    seen = set()
    for cid in frozen:
        sid = sessions.get(cid)
        if not isinstance(sid, str) or not sid:
            return "FAIL"
        if sid in seen:
            return "FAIL"      # one native session cannot serve two cases
        seen.add(sid)
    if any(review.get(cid) != "PASS" for cid in frozen):
        return "REVIEW_REQUIRED"
    return "PASS"


def render_gate(verified_modes, expected_modes, live, review,
                evidence_commit, current_commit) -> str:
    """Rendered-artifact judgment for MODEL_RENDER_E2E (A05).

    A summary-only batch (all SUCCEEDED strings, no verified artifacts) can
    never PASS; a review without verified artifacts is fabricated (FAIL);
    dry-run/never-live is NOT_RUN even if a review file exists.
    """
    if not live:
        return "NOT_RUN"
    verified = set(verified_modes or ())
    review = dict(review or {})
    expected = list(expected_modes or [])
    if not verified:
        return "FAIL" if review else "NOT_RUN"
    if evidence_commit != current_commit:
        return "FAIL"
    unknown = verified - set(expected)
    if unknown:
        return "FAIL"
    if any(v == "FAIL" for v in review.values()):
        return "FAIL"
    if any(mode not in expected for mode in review):
        return "FAIL"
    if not set(expected).issubset(verified):
        return "REVIEW_REQUIRED"
    if any(review.get(mode) != "PASS" for mode in expected):
        return "REVIEW_REQUIRED"
    return "PASS"


def video_gate(videos, review) -> str:
    """Authorized-real-video judgment for VIDEO_REVERSE_E2E (A06).

    Fixture-origin packages are never evidence: they are filtered before
    judging, so an all-fixture batch can only be NOT_RUN/FAIL, never PASS.
    A real (licensed_video) package that is present but unauthorized, or a
    duplicate id/sha256, is positive failure evidence (FAIL); fewer than
    five fully verified real videos is honest incompleteness
    (REVIEW_REQUIRED).
    """
    review = dict(review or {})
    entries = [v for v in (videos or []) if isinstance(v, dict)]
    real = [v for v in entries if v.get("origin") == "licensed_video"]
    if not real:
        return "FAIL" if review else "NOT_RUN"
    if any(v == "FAIL" for v in review.values()):
        return "FAIL"
    seen_ids: set[str] = set()
    seen_shas: set[str] = set()
    counted: list[str] = []
    for v in real:
        vid = v.get("id")
        sha = v.get("sha256")
        if not isinstance(vid, str) or not vid or vid in seen_ids:
            return "FAIL"
        if not _is_sha256(sha) or sha in seen_shas:
            return "FAIL"
        seen_ids.add(vid)
        seen_shas.add(sha)
        if v.get("authorized") is not True:
            return "FAIL"
        if v.get("analysis_verified") is True:
            counted.append(vid)
    if len(counted) < 5:
        return "REVIEW_REQUIRED"
    if any(review.get(vid) != "PASS" for vid in counted):
        return "REVIEW_REQUIRED"
    return "PASS"


def verify_index(root, index, fallback_root=None, evidence_root=None) -> bool:
    """Recompute every index entry's SHA-256 and byte count (A07, P0-4).

    A file entry ("path" + "sha256") must exist under root — or, when given,
    under fallback_root (the repo root for repo-relative evidence such as
    verifier sources) — with exactly those bytes. An entry may name its base
    explicitly: run_dir (the default, the report's own directory), repo
    (repo-relative evidence) or evidence_root (the restricted root holding
    private originals such as native transcripts, rendered media and
    licensed source videos; defaults to the run dir's parent). Whichever
    base resolves the path must contain it after symlinks/junctions are
    resolved; absolute paths, traversal and link escapes are rejected, and a
    recorded "bytes" must match the file exactly. An external_digest entry
    (private artifact kept out of the release package) is disclosed as a
    well-formed public hash only: it proves nothing about the original and
    never satisfies a gate.
    """
    if not isinstance(index, list):
        return False
    bases: dict[str, str] = {"run_dir": os.path.realpath(os.path.abspath(root))}
    if fallback_root:
        bases["repo"] = os.path.realpath(os.path.abspath(fallback_root))
    bases["evidence_root"] = (os.path.realpath(os.path.abspath(evidence_root))
                              if evidence_root
                              else os.path.dirname(bases["run_dir"]))
    for entry in index:
        if not isinstance(entry, dict):
            return False
        sha = entry.get("sha256")
        if not _is_sha256(sha):
            return False
        path = entry.get("path")
        if path is None:
            if entry.get("kind") != "external_digest":
                return False
            continue
        if not isinstance(path, str) or not path or os.path.isabs(path):
            return False
        base_name = entry.get("base", "run_dir")
        if base_name not in bases:
            return False
        ordered = [bases[base_name]] + [b for k, b in bases.items() if k != base_name]
        matched = None
        for base in ordered:
            full = os.path.realpath(os.path.join(base, path))
            if full.startswith(base + os.sep) and os.path.isfile(full):
                matched = full
                break
        if matched is None:
            return False
        h = hashlib.sha256()
        size = 0
        with open(matched, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
                size += len(chunk)
        if h.hexdigest() != sha.lower():
            return False
        recorded = entry.get("bytes")
        if recorded is not None:
            if isinstance(recorded, bool) or not isinstance(recorded, int) or recorded != size:
                return False
    return True


def external_originals_ok(entries, run_id, commit, require_authorization=False) -> bool:
    """Per-case provenance of the originals behind one external gate (P0-4).

    Every entry must describe a distinct case and carry the file it was
    measured from: a path, its byte count, its SHA-256, the run and commit
    it belongs to, and a non-empty origin label. A record missing any of
    those, a duplicated case, or a foreign run/commit refuses — a digest
    with no path is disclosure, not evidence — and video originals must
    additionally be authorized.
    """
    if not isinstance(entries, list) or not entries:
        return False
    seen: set = set()
    for e in entries:
        if not isinstance(e, dict) or e.get("kind") != "external_original":
            return False
        case_id = e.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            return False
        seen.add(case_id)
        path = e.get("path")
        if not isinstance(path, str) or not path:
            return False
        if not isinstance(e.get("origin"), str) or not e["origin"]:
            return False
        if e.get("run_id") != run_id or e.get("source_commit") != commit:
            return False
        size = e.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            return False
        if not _is_sha256(e.get("sha256")):
            return False
        if require_authorization and e.get("authorized") is not True:
            return False
    return True


def index_supports_gate(entries, gate_name, run_id, commit) -> bool:
    """Whether verified index entries back one gate's PASS (P0-4).

    A digest-only entry never counts: the gate needs at least one concrete
    indexed file of its own type, and the three external gates additionally
    need their per-case originals with run/commit provenance.
    """
    own = [e for e in (entries or ())
           if isinstance(e, dict) and e.get("type") == gate_name
           and e.get("kind") != "external_digest"]
    if not any(e.get("path") is not None for e in own):
        return False
    if gate_name in EXTERNAL_GATES:
        return external_originals_ok(
            [e for e in own if e.get("kind") == "external_original"],
            run_id, commit,
            require_authorization=(gate_name == "VIDEO_REVERSE_E2E"))
    return True


def release_allowed(tier, gate_statuses, report_commit, current_commit,
                    index=None, run_id=None) -> bool:
    """Tier gate for build_release (A08, P0-4).

    full-closure requires all six gates PASS *and*, per PASS gate, verified
    index entries with per-case originals for the external three: a public
    digest alone never supports closure, and a report without an index
    cannot be judged. local-stable requires only the three locally provable
    gates; when an index is supplied, any gate claiming PASS there must also
    be backed by concrete evidence (the external gates still ship honestly
    as NOT_RUN). Any tier value outside the two known ones, a commit drift,
    or a missing/malformed gate map refuses.
    """
    if tier not in RELEASE_TIERS:
        return False
    if report_commit != current_commit:
        return False
    if not isinstance(gate_statuses, dict) or set(gate_statuses) != set(GATE_ORDER):
        return False
    if tier == "full-closure":
        if not all(gate_statuses[g] == "PASS" for g in GATE_ORDER):
            return False
        if index is None or not isinstance(run_id, str) or not run_id:
            return False
        return all(index_supports_gate(index, g, run_id, current_commit)
                   for g in GATE_ORDER)
    if not all(gate_statuses[g] == "PASS" for g in LOCAL_GATES):
        return False
    if index is None:
        return True
    return all(index_supports_gate(index, g, run_id, current_commit)
               for g in GATE_ORDER if gate_statuses[g] == "PASS")
