#!/usr/bin/env python3
"""Video container/codec probe (C-5): ffprobe-first, OpenCV fallback, honest NOT_RUN.

Usage:
  python scripts/video_probe.py <video_file> [--json]
Output is a three-layer document: objective observation / algorithmic inference
(with confidence) / creative suggestion. When neither ffprobe nor OpenCV is
available, emits status NOT_RUN with an explicit reason and exit code 2.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys


def try_ffprobe(path: str) -> dict | None:
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except Exception:
        return None


def try_opencv(path: str) -> dict | None:
    try:
        import cv2  # type: ignore
    except Exception:
        return None
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        w = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0
        h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0
        cap.release()
        return {"fps": fps, "frames": frames, "width": w, "height": h, "backend": "opencv"}
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Video probe with honest NOT_RUN fallback")
    parser.add_argument("video", help="Path to video file")
    parser.add_argument("--json", action="store_true", help="Emit JSON document")
    args = parser.parse_args()

    if not os.path.isfile(args.video):
        print(f"ERROR: file does not exist: {args.video}", file=sys.stderr)
        return 2

    ff = try_ffprobe(args.video)
    if ff is not None:
        doc = {"status": "PASS", "backend": "ffprobe",
               "observation": ff,
               "inference": {"note": "container/streams parsed from ffprobe", "confidence": 0.95},
               "suggestion": {"note": "use stream parameters for H3 reference planning", "confidence": 0.6}}
    else:
        cv = try_opencv(args.video)
        if cv is not None:
            doc = {"status": "PASS", "backend": "opencv",
                   "observation": cv,
                   "inference": {"note": "basic geometry from OpenCV (no codec detail)", "confidence": 0.7},
                   "suggestion": {"note": "prefer ffprobe for full codec data", "confidence": 0.5}}
        else:
            doc = {"status": "NOT_RUN",
                   "reason": "neither ffprobe binary nor OpenCV python package available in this environment"}
            print(json.dumps(doc, ensure_ascii=False, indent=2) if args.json else "NOT_RUN: ffprobe/OpenCV unavailable")
            return 2

    print(json.dumps(doc, ensure_ascii=False, indent=2) if args.json else json.dumps(doc, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
