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
from .adapters import ArtifactValidationError, validate_rendered_artifact

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


def _is_usable_artifact(path, kind):
    """断点续跑用：文件非空且通过基本校验才算 cache hit。

    0 字节、无魔数的截断/垃圾文件一律判不可用（必须重新生成）；
    有意写入的桩占位（STUB-*/stub-* 短字节）视为有效占位，不误伤离线桩链路。
    """
    p = Path(path)
    if not p.is_file():
        return False
    try:
        head = p.read_bytes()[:32]
    except OSError:
        return False
    if not head:
        return False
    if head.lower().startswith(b"stub"):
        return True
    try:
        validate_rendered_artifact(p, kind, is_stub_allowed=True)
    except ArtifactValidationError:
        return False
    return True


# ----------------------------------------------------------------- video
def run_video_stage(plan, out_dir, adapter, retry_failed=False, ledger_path=None, run_id=None):
    out_dir = Path(out_dir)
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    shots = plan["shots"]
    failed_file = out_dir / "failed_items.json"

    existing = {s["shot_id"] for s in shots
                if _is_usable_artifact(clips_dir / f"{s['shot_id']}.mp4", "video")}
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
        if not _is_usable_artifact(clips_dir / f"{s['shot_id']}.mp4", "video")
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

    existing = {s["shot_id"] for s in dialogue_shots
                if _is_usable_artifact(audio_dir / f"{s['shot_id']}.wav", "audio")}
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


# jy 导出入口候选（按常见命名排序）：只认这些可调用集成点，
# 找到第一个就调；一个都找不到 => 视为"未实际调用"，绝不记 EXPORTED。
_JIANYING_EXPORT_CANDIDATES = (
    "export_draft", "export", "create_draft", "generate_draft", "build_draft",
    "make_draft", "save_draft", "write_draft", "render_draft", "compose_draft",
    "create_project", "export_project", "generate_project", "build_project",
    "save_project", "write_project", "export_to_jianying", "export_to_draft",
    "create_timeline", "build_timeline", "export_timeline",
    "assemble", "generate", "create", "save", "run", "main",
)

_RETURN_PATH_KEYS = ("draft", "draft_path", "draft_file", "path", "file",
                      "project", "project_path", "out", "output")


def _extract_return_paths(ret):
    """从导出返回值里抠候选产物路径：str/PathLike、其列表、或含路径键的 dict。"""
    if isinstance(ret, (str, os.PathLike)):
        return [Path(ret)]
    if isinstance(ret, (list, tuple, set)):
        out = []
        for item in ret:
            out.extend(_extract_return_paths(item))
        return out
    if isinstance(ret, dict):
        out = []
        for key in _RETURN_PATH_KEYS:
            if key in ret:
                out.extend(_extract_return_paths(ret[key]))
        return out
    return []


def _snapshot_files(root):
    """记录目录下已有文件的 (大小, mtime)：导出前后比对，新增/改动即视为产物。"""
    snap = {}
    root = Path(root)
    if not root.is_dir():
        return snap
    for p in root.rglob("*"):
        if p.is_file():
            try:
                st = p.stat()
            except OSError:
                continue
            snap[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return snap


def _call_jianying_export(jy, payload, draft_dir):
    """真正调用 jy_wrapper 的实际导出方法，并核验草稿产物文件存在。

    返回 (verified, method_name, artifacts, error)：只有"调成功且产物文件存在"
    才 verified；入口缺失、调用抛错、无产物文件一律 verified=False。
    """
    draft_dir = Path(draft_dir)
    target = None
    name = None
    for cand in _JIANYING_EXPORT_CANDIDATES:
        fn = getattr(jy, cand, None)
        if callable(fn):
            target, name = fn, cand
            break
    if target is None and callable(jy):
        target, name = jy, getattr(jy, "__name__", "jy_callable")
    if target is None:
        return False, None, [], "未找到可调用的导出入口"

    before = _snapshot_files(draft_dir)
    ret = None
    # 签名未知：按常见形参逐一试探；TypeError 视为签名不匹配换下一组，
    # 其它异常视为导出已尝试但失败。
    tried_type_error = None
    for args in ((payload, str(draft_dir)), (str(draft_dir), payload),
                 (payload,), (str(draft_dir),), ()):
        try:
            ret = target(*args)
            tried_type_error = None
            break
        except TypeError as exc:
            tried_type_error = exc
            continue
        except Exception as exc:  # noqa: BLE001 - 导出失败须如实记录
            return False, name, [], f"导出调用抛错：{exc}"
    else:
        return False, name, [], f"导出入口签名不匹配（TypeError：{tried_type_error}）"

    produced = [str(p.relative_to(draft_dir)) if _is_within(p, draft_dir) else str(p)
                for p in _extract_return_paths(ret)
                if isinstance(p, Path) and p.is_file()]
    after = _snapshot_files(draft_dir)
    for rel, sig in after.items():
        if before.get(rel) != sig and rel not in produced:
            produced.append(rel)
    if not produced:
        return False, name, [], "导出调用未产出任何草稿文件"
    return True, name, produced, None


def _is_within(path, root):
    try:
        Path(path).relative_to(Path(root))
        return True
    except ValueError:
        return False


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
    verified, method, artifacts, export_error = False, None, [], None
    if jy is not None:
        payload = {
            "source": plan.get("source"),
            "style": plan.get("style"),
            "clip_count": len(video_tracks),
            "timeline": video_tracks,
            "audio": audio_tracks,
            "subtitles": subtitle_tracks,
        }
        verified, method, artifacts, export_error = _call_jianying_export(
            jy, payload, draft_dir)

    if verified:
        jianying = "EXPORTED"
        note = (f"已调用 jy_wrapper.{method} 生成草稿并核验产物文件存在"
                f"（{', '.join(artifacts)}）")
    else:
        if jy is None:
            reason = "缺 jianying-editor 运行环境"
        else:
            reason = f"jy_wrapper 已导入但{export_error}"
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
        note = (f"剪映导出未验证：{reason}——请检查 jianying-editor 集成点是否可用"
                "（需提供 export_draft 等导出入口并落盘草稿文件），"
                "或人工导入 draft_desc.json 后重跑 assemble 以签发真实导出")

    assemble = {
        "jianying_export": "已导出" if verified else "未验证",
        "note": note,
        "shot_count": len(video_tracks),
        "tracks": {"video": video_tracks, "audio": audio_tracks, "subtitle": subtitle_tracks},
    }
    afile = _write_json(out_dir / "assemble.json", assemble)

    # 只有"真正调了导出且产物文件存在"才算绿；无素材记 NOT_RUN，有素材记 PARTIAL
    if verified and present:
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
