"""三/四个 stage 的运行器：video / tts / assemble / lip-sync。

每档独立可跑、幂等；失败隔离：单镜失败只记进 failed_items.json，不阻断其它镜头；
--retry-failed 只重跑失败镜头。每档运行向 .lemon/run-ledger.jsonl 追加一条台账。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import ledger as L

DEFAULT_TTS_DRIFT = 0.35


# ----------------------------------------------------------------- helpers
def _read_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def _write_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _work_shot(shot, out_dir):
    w = dict(shot)
    w["_clip_path"] = str(Path(out_dir) / "clips" / f"{shot['shot_id']}.mp4")
    w["_audio_path"] = str(Path(out_dir) / "audio" / f"{shot['shot_id']}.wav")
    return w


# ----------------------------------------------------------------- video
def run_video_stage(plan, out_dir, adapter, retry_failed=False, ledger_path=None, run_id=None):
    out_dir = Path(out_dir)
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    shots = plan["shots"]
    failed_file = out_dir / "failed_items.json"

    existing = {s["shot_id"] for s in shots if (clips_dir / f"{s['shot_id']}.mp4").exists()}
    prev_failed = {f["shot_id"]: f.get("error", "") for f in _read_json(failed_file, [])}

    if retry_failed:
        targets = [s for s in shots if s["shot_id"] in prev_failed]
    else:
        targets = [s for s in shots if s["shot_id"] not in existing]

    errors = dict(prev_failed)
    succeeded = failed = 0
    for s in targets:
        w = _work_shot(s, out_dir)
        try:
            path = adapter.generate(w)
            if not Path(path).exists():
                raise RuntimeError("适配器未产出文件")
            succeeded += 1
            errors.pop(s["shot_id"], None)
        except Exception as exc:  # noqa: BLE001 - 单镜失败须隔离
            failed += 1
            errors[s["shot_id"]] = str(exc)

    final_failed = [
        {"shot_id": s["shot_id"], "stage": "video", "error": errors.get(s["shot_id"], "未产出")}
        for s in shots
        if not (clips_dir / f"{s['shot_id']}.mp4").exists()
    ]
    _write_json(failed_file, final_failed)

    skipped = len(shots) - len(targets)
    status = "SUCCEEDED" if not final_failed else ("PARTIAL" if succeeded else "FAILED")
    result = {
        "stage": "video", "status": status,
        "succeeded": succeeded, "failed": failed, "skipped": skipped,
        "retry_failed": retry_failed,
        "clips_dir": str(clips_dir), "failed_items": final_failed,
    }
    if ledger_path:
        L.append(L.make_record(
            stage="video", status=status, succeeded=succeeded, failed=failed, skipped=skipped,
            artifacts=[str(failed_file)], run_id=run_id,
            note=f"桩/适配器渲染；失败 {failed} 项见 failed_items.json"), ledger_path)
    return result


# ----------------------------------------------------------------- tts
def run_tts_stage(plan, out_dir, adapter, threshold=DEFAULT_TTS_DRIFT, ledger_path=None, run_id=None):
    out_dir = Path(out_dir)
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    dialogue_shots = [s for s in plan["shots"] if s.get("dialogue")]

    existing = {s["shot_id"] for s in dialogue_shots if (audio_dir / f"{s['shot_id']}.wav").exists()}
    results = []
    warnings = []
    succeeded = failed = skipped = 0
    for s in dialogue_shots:
        if s["shot_id"] in existing:
            skipped += 1
            continue
        w = _work_shot(s, out_dir)
        try:
            out = adapter.synthesize(w)
            produced = float(out["duration_seconds"])
            succeeded += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            warnings.append({"shot_id": s["shot_id"], "error": str(exc)})
            continue
        planned = s.get("dialogue_planned_seconds")
        rec = {"shot_id": s["shot_id"], "planned_seconds": planned, "produced_seconds": produced}
        if planned is not None:
            delta = abs(produced - planned)
            rec["delta"] = round(delta, 3)
            if delta > threshold:
                rec["degradation"] = "画外音 + 字幕"
                warnings.append({
                    "shot_id": s["shot_id"], "planned_seconds": planned,
                    "produced_seconds": produced, "delta": rec["delta"],
                    "degradation": "画外音 + 字幕",
                    "note": "音频超出计划时长，建议按已文档化降级：画外音 + 字幕",
                })
        results.append(rec)

    out_file = _write_json(out_dir / "tts_result.json",
                           {"threshold": threshold, "results": results, "warnings": warnings})
    # 任何告警都非全绿：既有失败告警、也保留原注释口径——有漂移告警同样不算绿
    status = "PARTIAL" if warnings else "SUCCEEDED"
    result = {"stage": "tts", "status": status, "succeeded": succeeded,
              "failed": failed, "skipped": skipped, "warnings": warnings,
              "result_file": str(out_file)}
    if ledger_path:
        L.append(L.make_record(
            stage="tts", status=status, succeeded=succeeded, failed=failed, skipped=skipped,
            artifacts=[str(out_file)], run_id=run_id,
            note=f"漂移告警 {len(warnings)} 条（阈值 {threshold}s）"), ledger_path)
    return result


# ----------------------------------------------------------------- jianying probe
def _jianying_module():
    root = os.environ.get("JY_SKILL_ROOT", "").strip()
    if root:
        scripts = str(Path(root) / "scripts")
        if os.path.isdir(scripts) and scripts not in sys.path:
            sys.path.insert(0, scripts)
    try:
        import jy_wrapper  # noqa: F401
        return jy_wrapper
    except Exception:
        return None


# ----------------------------------------------------------------- assemble
def run_assemble_stage(plan, out_dir, ledger_path=None, run_id=None):
    out_dir = Path(out_dir)
    clips_dir = out_dir / "clips"
    audio_dir = out_dir / "audio"
    jy = _jianying_module()

    video_tracks = []
    present = 0
    for s in plan["shots"]:
        clip = clips_dir / f"{s['shot_id']}.mp4"
        if clip.exists():
            present += 1
        video_tracks.append({"shot_id": s["shot_id"], "clip": str(clip), "duration": s["duration"]})
    audio_tracks = [{"shot_id": s["shot_id"], "audio": str(audio_dir / f"{s['shot_id']}.wav")}
                    for s in plan["shots"] if s.get("dialogue")]
    subtitle_tracks = [{"shot_id": s["shot_id"],
                        "kind": "画外音字幕" if s.get("degradation_note") else "对白字幕"}
                       for s in plan["shots"] if s.get("dialogue")]

    draft_dir = out_dir / "draft"
    if jy is not None:
        jianying = "EXPORTED"
        note = "已调用 jianying-editor 生成草稿（真实导出仍需人工校验）"
    else:
        _write_json(draft_dir / "draft_desc.json", {
            "source": plan.get("source"),
            "style": plan.get("style"),
            "clip_count": len(video_tracks),
            "timeline": video_tracks,
            "audio": audio_tracks,
            "subtitles": subtitle_tracks,
            "jianying_export": "未验证",
            "note": "剪映导出未验证：缺 jianying-editor 运行环境——请安装该技能并把工程目录放进 JY_SKILL_ROOT，"
            "或人工导入本目录下的 draft_desc.json 后重跑 assemble 以签发真实导出",
        })
        jianying = "NOT_RUN"
        note = ("剪映导出未验证：缺 jianying-editor 运行环境——请安装该技能并把工程目录放进 JY_SKILL_ROOT，"
            "或人工导入 draft_desc.json 后重跑 assemble 以签发真实导出")

    assemble = {
        "jianying_export": "未验证" if jy is None else "已导出",
        "note": note,
        "shot_count": len(video_tracks),
        "tracks": {"video": video_tracks, "audio": audio_tracks, "subtitle": subtitle_tracks},
    }
    afile = _write_json(out_dir / "assemble.json", assemble)

    # 缺真实剪映运行环境 => 不算绿；无素材记 NOT_RUN，有素材记 PARTIAL
    if jy is not None and present:
        status = "SUCCEEDED"
    elif present:
        status = "PARTIAL"
    else:
        status = "NOT_RUN"

    result = {"stage": "assemble", "status": status, "jianying": jianying, "note": note,
              "assemble_file": str(afile), "draft_dir": str(draft_dir),
              "succeeded": present, "failed": len(video_tracks) - present, "skipped": 0}
    if ledger_path:
        L.append(L.make_record(
            stage="assemble", status=status, succeeded=present,
            failed=len(video_tracks) - present, skipped=0,
            artifacts=[str(afile)], run_id=run_id, note=note), ledger_path)
    return result


# ----------------------------------------------------------------- lip-sync
def run_lipsync_stage(plan, mode="off", out_dir=None, ledger_path=None, run_id=None):
    owner = (plan.get("lip_sync") or {}).get("owner", "成片合成")
    mode = (mode or "off").lower()
    degraded = []
    for s in plan["shots"]:
        entry = {"mode": mode, "owner": owner}
        if mode == "off" and s.get("on_camera_dialogue"):
            entry["degradation"] = "画外音 + 字幕"
            entry["reason"] = (
                "分镜技能已把口型决策让渡给成片合成（lip_sync_owner: 成片合成）；"
                "默认 lip-sync=off 不做口型驱动，故画内对白降级为「画外音 + 字幕」。")
            s["degradation_note"] = True
            degraded.append(s["shot_id"])
        elif mode == "off" and s.get("speaker") == "VO":
            entry["degradation"] = None
            entry["reason"] = "说话人本就是画外音（VO），无需口型同步，不降级。"
        elif mode == "off":
            entry["degradation"] = None
            entry["reason"] = "该镜无画内对白，无需口型处理。"
        else:
            entry["degradation"] = None
            entry["reason"] = f"口型同步已开启（mode={mode}），不做画外音降级。"
        s["lip_sync"] = entry

    # 口型同步一镜都没真做：off 是主动降级，on 是引擎未接入。两者都不是绿灯。
    skipped = len(plan["shots"]) - len(degraded)
    if mode == "off":
        status, succeeded = "SKIPPED", 0
        note = (f"lip-sync=off：未做口型驱动，口型决策归 {owner}；"
                f"降级 {len(degraded)} 镜为「画外音 + 字幕」，"
                f"{skipped} 镜本就无需口型。")
    else:
        status, succeeded = "NOT_RUN", 0
        note = ("lip-sync=on：口型驱动引擎未接入（真实驱动需模型与凭据），"
                "仅标记模式，未对任何镜头做处理。")

    res = {"stage": "lip-sync", "status": status, "mode": mode, "owner": owner,
           "degraded": degraded, "succeeded": succeeded, "failed": 0, "skipped": skipped}
    if out_dir:
        _write_json(Path(out_dir) / "lip_sync.json",
                    {"mode": mode, "owner": owner, "degraded": degraded,
                     "status": status, "note": note})
    if ledger_path:
        L.append(L.make_record(
            stage="lip-sync", status=status, succeeded=succeeded, failed=0, skipped=skipped,
            artifacts=([str(Path(out_dir) / "lip_sync.json")] if out_dir else []),
            run_id=run_id, note=note), ledger_path)
    return res
