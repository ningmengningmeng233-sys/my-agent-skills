#!/usr/bin/env python3
"""Video reverse-engineering planner (P4-1 hardened): no vision without vision.

Usage:
  python scripts/video_reverse_engineer.py --probe <probe_json> [--json]
Reads a video_probe.py JSON document and emits a layered plan for H3 prompt
reconstruction. Container/codec metadata alone proves NOTHING about camera
motion or lighting, so with only technical metadata the planner returns
algorithmic_inference=[] and VISUAL_ANALYSIS=NOT_RUN (exit 0: honest empty
result, not a failure). A NOT_RUN probe propagates as NOT_RUN (exit 2).
Use scripts/video_analyze.py for frame-evidenced motion claims.
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Layered video reverse-engineering planner")
    parser.add_argument("--probe", required=True, help="Path to probe JSON document")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        with open(args.probe, "r", encoding="utf-8") as f:
            probe = json.load(f)
    except Exception as exc:
        print(f"ERROR: cannot read probe JSON: {exc}", file=sys.stderr)
        return 2

    if probe.get("status") == "NOT_RUN":
        doc = {"status": "NOT_RUN", "reason": f"upstream probe NOT_RUN: {probe.get('reason')}"}
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 2

    obs = probe.get("observation", {})
    doc = {
        "status": "PASS",
        "objective_observation": obs,
        "algorithmic_inference": [],
        "VISUAL_ANALYSIS": "NOT_RUN",
        "reason": "container/stream metadata cannot evidence camera motion or lighting; run scripts/video_analyze.py on the video bytes for frame-evidenced claims",
        "creative_suggestion": [
            "run scripts/video_analyze.py --video <file> for timestamped frame/shot evidence first",
            "only map observations with observation_id into H3 draft lines; mark the rest creative_assumption",
        ],
    }
    print(json.dumps(doc, ensure_ascii=False, indent=2) if args.json else json.dumps(doc, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
