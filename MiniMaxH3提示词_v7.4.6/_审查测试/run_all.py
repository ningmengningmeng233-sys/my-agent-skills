#!/usr/bin/env python3
"""Task 9: the honest, dynamic, current quality gate for minimax-h3-craft.

Honesty contract:
- The FIRST output line is always ``HISTORICAL_FULL_SUITE=UNAVAILABLE``: the
  historical suite and release builder could not be recovered (Task 0), so
  this gate aggregates only the CURRENT suites and never passes their counts
  off as the historical 316.
- Every count is parsed from the sub-suite's own output (``Ran N tests``);
  a sub-suite that produces no parseable count is fail-closed, never assumed
  green.
- Any non-zero sub-suite or auxiliary check forces a non-zero gate exit.

Composition: every ``test_*.py`` under ``_审查测试`` (auto-discovered, so new
suites join the gate without editing this file) plus ``reference_graph.py``,
``validate_source_manifest.py``, ``verification_protocol.py check`` (D1 凭证
协议闸门：PASS 必须带日期与真实存在的产物路径), ``compileall`` over ``scripts``
and ``_审查测试``, and the skill context auditor.

The anti-shrink wiring obligation lives downstream of this aggregator: every
path that composes ``validate_episode_plan`` for publication must pass the
script-parsed ``authoritative_target_seconds`` (Task 10 fixtures do; see the
review note in the work order Task 9 section).

Run:
    python minimax-h3-craft/_审查测试/run_all.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

SKILL_ROOT = Path(__file__).resolve().parents[1]

HISTORICAL_FULL_SUITE_LINE = "HISTORICAL_FULL_SUITE=UNAVAILABLE"

SUITE_GLOB = "test_*.py"

Runner = Callable[[Sequence[str], Optional[str]], Tuple[int, str]]

RAN_RE = re.compile(r"Ran (\d+) tests? in")


def discover_suites(test_dir: Path) -> List[Path]:
    """All ``test_*.py`` under the audit directory, sorted (run_all excluded by name)."""
    return sorted(p for p in Path(test_dir).glob(SUITE_GLOB) if p.name != "run_all.py")


def run_command(cmd: Sequence[str], cwd: Optional[str] = None) -> Tuple[int, str]:
    proc = subprocess.run(
        list(cmd), cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def parse_count(output: str) -> Optional[int]:
    match = RAN_RE.search(output)
    return int(match.group(1)) if match else None


def build_auxiliary_checks(skill_root: Path) -> List[Tuple[str, Sequence[str]]]:
    scripts = skill_root / "scripts"
    audit_dir = skill_root / "_审查测试"
    return [
        ("reference_graph", [sys.executable, str(scripts / "reference_graph.py")]),
        ("validate_source_manifest", [sys.executable, str(scripts / "validate_source_manifest.py")]),
        ("verification_protocol", [sys.executable, str(scripts / "verification_protocol.py"), "check"]),
        ("skill_context_audit", [sys.executable, str(scripts / "skill_context_audit.py"), str(skill_root)]),
        ("compileall", [sys.executable, "-m", "compileall", "-q", str(scripts), str(audit_dir)]),
    ]


def run_gate(
    suites: Optional[Sequence[Tuple[str, Tuple[int, str]]]] = None,
    auxiliary: Optional[Sequence[Tuple[str, Tuple[int, str]]]] = None,
    test_dir: Optional[Path] = None,
    skill_root: Optional[Path] = None,
    runner: Runner = run_command,
) -> Tuple[int, str]:
    """Run the whole gate; return (exit_code, report_text).

    ``suites``/``auxiliary`` pre-supply results (tests inject fakes); when
    omitted they are discovered and executed for real.
    """
    skill_root = Path(skill_root) if skill_root else SKILL_ROOT
    test_dir = Path(test_dir) if test_dir else skill_root / "_审查测试"
    lines: List[str] = [HISTORICAL_FULL_SUITE_LINE]
    exit_code = 0
    total: Optional[int] = 0

    if suites is None:
        discovered = discover_suites(test_dir)
        suites = []
        for suite in discovered:
            rc, output = runner([sys.executable, str(suite)], None)
            suites.append((suite.name, (rc, output)))
    for name, (rc, output) in suites:
        count = parse_count(output)
        if rc != 0:
            exit_code = 1
            tail = " / ".join(
                line.strip() for line in output.strip().splitlines()[-3:] if line.strip()
            )
            lines.append(f"FAIL suite {name} (rc={rc}, count={count}): {tail}")
        elif count is None:
            exit_code = 1
            lines.append(f"FAIL suite {name}: no parseable 'Ran N tests' count (fail-closed)")
        else:
            total += count
            lines.append(f"ok suite {name} count={count}")

    if auxiliary is None:
        auxiliary = []
        for name, cmd in build_auxiliary_checks(skill_root):
            auxiliary.append((name, run_command(list(cmd), None)))
    for name, (rc, output) in auxiliary:
        if rc != 0:
            exit_code = 1
            tail = " / ".join(
                line.strip() for line in output.strip().splitlines()[-3:] if line.strip()
            )
            lines.append(f"FAIL auxiliary {name} (rc={rc}): {tail}")
        else:
            lines.append(f"ok auxiliary {name}")

    if total is None:
        total = 0
    lines.append(f"suites={len(suites)} total={total}")
    lines.append("QUALITY_GATE: PASS" if exit_code == 0 else "QUALITY_GATE: FAIL")
    return exit_code, "\n".join(lines)


def main() -> int:
    code, report = run_gate()
    print(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
