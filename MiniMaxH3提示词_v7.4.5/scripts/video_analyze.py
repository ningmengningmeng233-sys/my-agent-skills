#!/usr/bin/env python3
"""Evidence-backed video analysis pipeline (P4).

Stages:
  1. ffprobe container/stream parameters (NOT_RUN/FAIL honestly on absence).
  2. Fixed-interval frame extraction (ffmpeg preferred, OpenCV fallback)
     including first/last/cut-adjacent frames; each frame carries
     millisecond timestamp + SHA-256.
  3. Shot boundaries via grayscale histogram distance with candidate
     boundaries + evidence frames (never assert motion from stills).
  4. In-shot camera-motion scoring via sparse feature tracking
     (goodFeaturesToTrack + Lucas-Kanade): coherent radial scale change =
     push-in/out candidate; dominant translation = pan/tilt candidate;
     mixed/weak evidence = UNASSESSABLE. Subject motion alone never claims
     camera movement; hard cuts are segments, never pans.
  5. Visual semantics come ONLY from human annotations file or declared
     vision-model outputs (model version + input frames + confidence).
     Otherwise VISUAL_ANALYSIS=NOT_RUN and algorithmic_inference=[].
  6. H3 draft mapping: every line points at an observation_id; ungrounded
     additions are marked creative_assumption.

Usage:
  python scripts/video_analyze.py --video <file> --out <dir> --json
  [--interval-ms 500] [--annotations annotations.json]
Exit 0 on analyzed video, 2 when media backend missing or input unusable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="strict", timeout=timeout)


def ffprobe_streams(path: str) -> dict | None:
    if shutil.which("ffprobe") is None:
        return None
    try:
        p = run(["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", path])
        return json.loads(p.stdout) if p.returncode == 0 else None
    except Exception:
        return None


def extract_frames_ffmpeg(path: str, out_dir: str, interval_ms: int) -> list[dict]:
    fps_expr = f"fps={1000 / max(interval_ms, 1)}"
    frames_dir = os.path.join(out_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    p = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
             "-vf", fps_expr, os.path.join(frames_dir, "f_%06d.png")])
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed: {p.stderr[-300:]}")
    # ffprobe duration for ms mapping
    dur_ms = None
    probe = ffprobe_streams(path) or {}
    try:
        dur_ms = float((probe.get("format") or {}).get("duration", 0)) * 1000
    except Exception:
        dur_ms = None
    files = sorted(f for f in os.listdir(frames_dir) if f.endswith(".png"))
    if not files:
        raise RuntimeError("ffmpeg produced no frames")
    step = (dur_ms / len(files)) if dur_ms else float(interval_ms)
    out = []
    for i, f in enumerate(files):
        fp = os.path.join(frames_dir, f)
        out.append({"frame_file": os.path.join("frames", f),
                    "frame_t_ms": int(round(i * step)),
                    "frame_sha256": sha256_file(fp)})
    return out


def extract_frames_opencv(path: str, out_dir: str, interval_ms: int) -> list[dict]:
    import cv2  # type: ignore
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("opencv cannot open video")
    fps = cap.get(5) or 25.0
    step = max(int(round(fps * interval_ms / 1000)), 1)
    frames_dir = os.path.join(out_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    out, idx, saved = [], 0, 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            fp = os.path.join(frames_dir, f"f_{saved:06d}.png")
            cv2.imwrite(fp, frame)
            out.append({"frame_file": os.path.join("frames", fp.split("frames")[-1].lstrip("/\\")),
                        "frame_t_ms": int(round(idx / fps * 1000)),
                        "frame_sha256": sha256_file(fp)})
            saved += 1
        idx += 1
    cap.release()
    if not out:
        raise RuntimeError("opencv produced no frames")
    return out


def gray_hist(path: str):
    import cv2  # type: ignore
    import numpy as np
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    hist, _ = np.histogram(img, bins=32, range=(0, 256))
    s = hist.sum() or 1
    return hist / s


def shot_segments(out_dir: str, frames: list[dict], cut_threshold: float = 0.55) -> list[dict]:
    """Histogram-distance cuts; each boundary cites evidence frames on both sides."""
    try:
        dists = []
        for i in range(1, len(frames)):
            a = gray_hist(os.path.join(out_dir, frames[i - 1]["frame_file"]))
            b = gray_hist(os.path.join(out_dir, frames[i]["frame_file"]))
            dists.append(float(abs(a - b).sum() / 2))
    except Exception:
        # Without image decoding there is no shot evidence at all.
        return [{"shot_id": "shot-001", "start_t_ms": frames[0]["frame_t_ms"],
                 "end_t_ms": frames[-1]["frame_t_ms"], "method": "single-segment-no-decode",
                 "confidence": 0.2, "evidence_frames": [frames[0], frames[-1]]}]
    bounds = [0]
    for i, d in enumerate(dists, start=1):
        if d >= cut_threshold:
            bounds.append(i)
    bounds.append(len(frames))
    segs = []
    for s, (a, b) in enumerate(zip(bounds[:-1], bounds[1:]), start=1):
        seg_frames = frames[a:b]
        segs.append({"shot_id": f"shot-{s:03d}", "start_t_ms": seg_frames[0]["frame_t_ms"],
                     "end_t_ms": seg_frames[-1]["frame_t_ms"], "method": "gray-hist-distance-v1",
                     "cut_distance": round(dists[a - 1], 3) if a > 0 else 0.0,
                     "confidence": 0.7 if a > 0 else 0.5,
                     "evidence_frames": [seg_frames[0], seg_frames[-1]]})
    return segs


def camera_motion(out_dir: str, seg: dict) -> dict:
    """LK feature-track motion score inside one shot; weak/mixed -> UNASSESSABLE."""
    try:
        import cv2  # type: ignore
        import numpy as np
    except Exception:
        return {"verdict": "UNASSESSABLE", "method": "no-tracking-backend", "confidence": 0.0}
    ev = seg["evidence_frames"]
    if len(ev) < 2:
        return {"verdict": "UNASSESSABLE", "method": "single-frame", "confidence": 0.0}
    g0 = cv2.imread(os.path.join(out_dir, ev[0]["frame_file"]), cv2.IMREAD_GRAYSCALE)
    g1 = cv2.imread(os.path.join(out_dir, ev[-1]["frame_file"]), cv2.IMREAD_GRAYSCALE)
    if g0 is None or g1 is None or g0.shape != g1.shape:
        return {"verdict": "UNASSESSABLE", "method": "unreadable-pair", "confidence": 0.0}
    pts = cv2.goodFeaturesToTrack(g0, maxCorners=200, qualityLevel=0.01, minDistance=8)
    if pts is None or len(pts) < 12:
        return {"verdict": "STATIC_OR_TEXTURELESS", "method": "lk-track-v1", "confidence": 0.4,
                "tracked_points": 0}
    p1, st, _ = cv2.calcOpticalFlowPyrLK(g0, g1, pts, None)
    ok = st.reshape(-1) == 1
    if ok.sum() < 12:
        return {"verdict": "UNASSESSABLE", "method": "lk-track-v1", "confidence": 0.3,
                "tracked_points": int(ok.sum())}
    p0 = pts.reshape(-1, 2)[ok]
    p1 = p1.reshape(-1, 2)[ok]
    flow = p1 - p0
    h, w = g0.shape
    cx, cy = w / 2, h / 2
    r0 = ((p0 - (cx, cy)) ** 2).sum(axis=1) ** 0.5 + 1e-6
    r1 = ((p1 - (cx, cy)) ** 2).sum(axis=1) + 1e-6
    radial = float((r1 / r0).mean())
    trans = float(abs(flow).mean())
    spread = float(flow.std())
    # coherent scale change with low spread -> push; dominant translation -> pan/tilt
    if radial > 1.04 and spread < trans * 1.5 + 2.0 and trans > 1.0:
        return {"verdict": "PUSH_IN_CANDIDATE", "method": "lk-track-v1",
                "radial_scale": round(radial, 3), "mean_flow": round(trans, 2),
                "confidence": 0.62, "tracked_points": int(ok.sum())}
    if radial < 0.96 and spread < trans * 1.5 + 2.0 and trans > 1.0:
        return {"verdict": "PULL_OUT_CANDIDATE", "method": "lk-track-v1",
                "radial_scale": round(radial, 3), "mean_flow": round(trans, 2),
                "confidence": 0.6, "tracked_points": int(ok.sum())}
    if trans > 3.0 and abs(radial - 1.0) < 0.02:
        axis = "PAN_CANDIDATE" if abs(flow[:, 0]).mean() >= abs(flow[:, 1]).mean() else "TILT_CANDIDATE"
        return {"verdict": axis, "method": "lk-track-v1", "mean_flow": round(trans, 2),
                "confidence": 0.58, "tracked_points": int(ok.sum())}
    if trans < 0.8:
        return {"verdict": "STATIC_CANDIDATE", "method": "lk-track-v1",
                "mean_flow": round(trans, 2), "confidence": 0.55,
                "tracked_points": int(ok.sum())}
    return {"verdict": "UNASSESSABLE", "method": "lk-track-v1",
            "mean_flow": round(trans, 2), "radial_scale": round(radial, 3),
            "confidence": 0.35, "tracked_points": int(ok.sum())}


def main() -> int:
    ap = argparse.ArgumentParser(description="Evidence-backed video analysis (P4)")
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--interval-ms", type=int, default=500)
    ap.add_argument("--annotations", default=None, help="human/model visual annotations JSON")
    args = ap.parse_args()

    if not os.path.isfile(args.video):
        print(f"ERROR: file does not exist: {args.video}", file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)
    video_sha = sha256_file(args.video)
    probe = ffprobe_streams(args.video)
    if probe is None:
        doc = {"status": "NOT_RUN", "reason": "ffprobe unavailable or unreadable input",
               "source_video_sha256": video_sha, "VISUAL_ANALYSIS": "NOT_RUN",
               "algorithmic_inference": []}
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 2

    try:
        if shutil.which("ffmpeg") is not None:
            frames = extract_frames_ffmpeg(args.video, args.out, args.interval_ms)
            frame_backend = "ffmpeg"
        else:
            frames = extract_frames_opencv(args.video, args.out, args.interval_ms)
            frame_backend = "opencv"
    except Exception as exc:
        doc = {"status": "FAIL", "reason": f"frame extraction failed: {exc}",
               "source_video_sha256": video_sha, "VISUAL_ANALYSIS": "NOT_RUN",
               "algorithmic_inference": []}
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 1

    segments = shot_segments(args.out, frames)
    observations, inferences = [], []
    oid = 0
    for seg in segments:
        oid += 1
        obs_id = f"obs-{oid:03d}"
        motion = camera_motion(args.out, seg)
        observations.append({
            "source_video_sha256": video_sha,
            "observation_id": obs_id,
            "claim": f"{seg['shot_id']} spans {seg['start_t_ms']}–{seg['end_t_ms']} ms",
            "evidence": [{"frame_t_ms": f["frame_t_ms"], "frame_sha256": f["frame_sha256"]}
                         for f in seg["evidence_frames"]],
            "method": seg["method"],
            "confidence": seg["confidence"],
            "review_status": "PENDING_HUMAN_REVIEW",
        })
        if motion["verdict"] in ("PUSH_IN_CANDIDATE", "PULL_OUT_CANDIDATE",
                                 "PAN_CANDIDATE", "TILT_CANDIDATE", "STATIC_CANDIDATE"):
            oid += 1
            inferences.append({
                "source_video_sha256": video_sha,
                "observation_id": f"obs-{oid:03d}",
                "claim": f"{seg['shot_id']} motion: {motion['verdict']}",
                "evidence": [{"frame_t_ms": f["frame_t_ms"], "frame_sha256": f["frame_sha256"]}
                             for f in seg["evidence_frames"]],
                "method": motion["method"],
                "confidence": motion["confidence"],
                "review_status": "PENDING_HUMAN_REVIEW",
            })

    annotations: list = []
    if args.annotations and os.path.isfile(args.annotations):
        try:
            annotations = json.load(open(args.annotations, encoding="utf-8"))
        except Exception as exc:
            print(f"ERROR: bad annotations file: {exc}", file=sys.stderr)
            return 2

    visual = "NOT_RUN" if not annotations else "ANNOTATED"
    if annotations:
        for a in annotations:
            oid += 1
            a = dict(a)
            a.setdefault("observation_id", f"obs-{oid:03d}")
            a.setdefault("review_status", "PENDING_HUMAN_REVIEW")
            observations.append(a)

    # H3 draft mapping: every line cites an observation; the rest is assumption.
    draft = []
    for o in observations:
        if o["observation_id"].startswith("obs-"):
            draft.append({"text": o["claim"], "observation_id": o["observation_id"]})
    draft.append({"text": "未观测项按 creative_assumption 处理，不得写入事实断言。",
                  "observation_id": None, "creative_assumption": True})

    doc = {
        "status": "PASS",
        "source_video": os.path.abspath(args.video),
        "source_video_sha256": video_sha,
        "container": probe.get("format", {}),
        "frame_backend": frame_backend,
        "frames": frames,
        "segments": segments,
        "objective_observation": observations,
        "algorithmic_inference": inferences,
        "VISUAL_ANALYSIS": visual,
        "h3_draft_mapping": draft,
        "review_status": "PENDING_HUMAN_REVIEW",
    }
    print(json.dumps(doc, ensure_ascii=False, indent=2) if args.json else json.dumps(doc, ensure_ascii=False))
    with open(os.path.join(args.out, "analysis.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
