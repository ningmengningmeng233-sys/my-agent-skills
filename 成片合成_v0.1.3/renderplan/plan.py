"""分镜契约 → 成片工单（render_plan.json）推导与 storyboard.schema.json 符合性校验。

字段名严格对齐 schemas/storyboard.schema.json：
  episodes[].segments[].{id, sceneIndex, cuts[].{beats, seconds, size, camera, characters, props, frame}}
镜号（工单主键）按 x-shot-table 推导：段号 + f<段内切序>，如 E01-01-f2。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

SOURCE_SKILL = "成片合成"

#: 统一运行台账内核（C8）位置：默认取工作区同级的 schemas/，允许 env 覆盖，
#: 与 资产提取/scripts/engine/perf_ledger.py 同一口径（技能装到别处时靠 env 指路）。
def _resolve_unified_ledger_kernel() -> Path:
    """解析顺序：显式 env → 本包 `vendor/schemas/`（收件机可用）→ 工作区同级 `schemas/`。

    只往同级 `schemas/` 找是开发柜特权：技能装到别人机器上时那里什么都没有，
    自带台账测试会报「缺统一台账内核」。改成优先吃包内副本，env 仍可覆盖。
    """
    here = Path(__file__).resolve().parent.parent
    candidates = [Path(env) if (env := os.environ.get("UNIFIED_LEDGER_PY")) else None,
                  here / "vendor" / "schemas" / "ledger.py",
                  here.parent / "schemas" / "ledger.py"]
    for cand in candidates:
        if cand and cand.is_file():
            return cand
    return candidates[2]


UNIFIED_LEDGER_PY = _resolve_unified_ledger_kernel()

_SIZE_ENUM = {"extreme-wide", "wide", "medium", "close", "extreme-close"}
_SEG_ID = r"E\d{2}-\d{2}"
_CHAR = r"C\d{2,3}"
_PROP = r"P\d{2,3}"

_TOP_ALLOWED = {"source", "lang", "style", "promptLang", "params", "episodes"}
_EP_ALLOWED = {"ep", "segments"}
_SEG_ALLOWED = {"id", "sceneIndex", "cuts", "h3Prompt", "note"}
_CUT_ALLOWED = {"beats", "seconds", "size", "camera", "characters", "props",
                "frame", "recipe", "note"}


class StoryboardError(Exception):
    """分镜不合契约：对外只暴露一条纯中文错误，不外泄 traceback。"""

    def __init__(self, problems):
        if isinstance(problems, str):
            problems = [problems]
        self.problems = list(problems)
        super().__init__("；".join(self.problems))


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _re_matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_keys(obj, required, allowed, missing_fmt, extra_fmt):
    """必填字段缺失 + 白名单外多余字段的通用体检；模板里的 {key} 会被替换为字段名。"""
    problems = []
    for key in required:
        if key not in obj:
            problems.append(missing_fmt.format(key=key))
    for key in obj:
        if key not in allowed:
            problems.append(extra_fmt.format(key=key))
    return problems


def validate_storyboard(data):
    """按 storyboard.schema.json 的真字段名与约束校验，返回中文问题列表（空即通过）。"""
    problems = []

    if not isinstance(data, dict):
        return ["分镜文件顶层必须是 JSON 对象"]

    problems.extend(_check_keys(data, ("source", "episodes"), _TOP_ALLOWED,
                                "分镜缺少必填字段 {key}", "分镜顶层不允许出现字段 {key}"))

    if data.get("style") is not None and data["style"] not in {"realistic", "ghibli"}:
        problems.append("style 只能是 realistic 或 ghibli")
    if data.get("lang") is not None and data["lang"] not in {"zh", "en"}:
        problems.append("lang 只能是 zh 或 en")
    if data.get("promptLang") is not None and data["promptLang"] not in {"en", "zh"}:
        problems.append("promptLang 只能是 en 或 zh")

    episodes = data.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        problems.append("episodes 必须是非空数组")
        return problems

    for ei, ep in enumerate(episodes):
        loc = f"第{ei + 1}集"
        if not isinstance(ep, dict):
            problems.append(f"{loc} 必须是对象")
            continue
        for k in ("ep", "segments"):
            if k not in ep:
                problems.append(f"{loc} 缺少必填字段 {k}")
        for k in ep:
            if k not in _EP_ALLOWED:
                problems.append(f"{loc} 不允许出现字段 {k}")
        if "ep" in ep and (not _is_int(ep["ep"]) or ep["ep"] < 1):
            problems.append(f"{loc} 的 ep 必须是 ≥1 的整数")
        segments = ep.get("segments")
        if not isinstance(segments, list) or not segments:
            problems.append(f"{loc} 的 segments 必须是非空数组")
            continue
        for si, seg in enumerate(segments):
            problems.extend(_validate_segment(seg, f"{loc}第{si + 1}段"))
    return problems


def _validate_segment(seg, loc):
    problems = []
    if not isinstance(seg, dict):
        return [f"{loc} 必须是对象"]
    problems.extend(_check_keys(seg, ("id", "sceneIndex", "cuts"), _SEG_ALLOWED,
                                f"{loc} 缺少必填字段 {{key}}", f"{loc} 不允许出现字段 {{key}}"))
    if "id" in seg and not _re_matches(seg["id"], _SEG_ID):
        problems.append(f"{loc} 段号 id 必须符合 E数字数字-数字数字（如 E01-01），实为 {seg.get('id')!r}")
    if "sceneIndex" in seg and (not _is_int(seg["sceneIndex"]) or seg["sceneIndex"] < 1):
        problems.append(f"{loc} 的 sceneIndex 必须是 ≥1 的整数")
    cuts = seg.get("cuts")
    if not isinstance(cuts, list) or not cuts:
        problems.append(f"{loc} 的 cuts 必须是非空数组")
        return problems
    for ci, cut in enumerate(cuts):
        problems.extend(_validate_cut(cut, f"{loc}第{ci + 1}切"))
    return problems


def _validate_cut(cut, loc):
    problems = []
    if not isinstance(cut, dict):
        return [f"{loc} 必须是对象"]
    problems.extend(_check_keys(cut, ("beats", "seconds", "size", "camera", "characters", "frame"),
                                _CUT_ALLOWED, f"{loc} 缺少必填字段 {{key}}", f"{loc} 不允许出现字段 {{key}}"))
    beats = cut.get("beats")
    if "beats" in cut:
        if not isinstance(beats, list) or len(beats) != 2 or not all(_is_int(b) and b >= 1 for b in beats):
            problems.append(f"{loc} 的 beats 必须是两个 ≥1 的整数")
    if "seconds" in cut and (not _is_num(cut["seconds"]) or cut["seconds"] < 0):
        problems.append(f"{loc} 的 seconds 必须是 ≥0 的数值，实为 {cut.get('seconds')!r}")
    if "size" in cut and cut["size"] not in _SIZE_ENUM:
        problems.append(f"{loc} 的 size 必须是 {sorted(_SIZE_ENUM)} 之一，实为 {cut.get('size')!r}")
    if "camera" in cut and not isinstance(cut["camera"], str):
        problems.append(f"{loc} 的 camera 必须是字符串")
    if "characters" in cut:
        if not isinstance(cut["characters"], list) or any(not _re_matches(c, _CHAR) for c in cut["characters"]):
            problems.append(f"{loc} 的 characters 必须是 C 编号数组（如 C01）")
    if "props" in cut:
        if not isinstance(cut["props"], list) or any(not _re_matches(p, _PROP) for p in cut["props"]):
            problems.append(f"{loc} 的 props 必须是 P 编号数组（如 P01）")
    if "frame" in cut and (not isinstance(cut["frame"], str) or not cut["frame"]):
        problems.append(f"{loc} 的 frame 必须是非空字符串")
    return problems


def _alignments(dubbing):
    if dubbing is None:
        return []
    if isinstance(dubbing, list):
        return dubbing
    if isinstance(dubbing, dict):
        return dubbing.get("alignments") or dubbing.get("dialogue") or []
    return []


def _ledger_formal_ids(asset_ledger):
    if not asset_ledger:
        return []
    entries = asset_ledger.get("entries", []) if isinstance(asset_ledger, dict) else []
    return [e["asset_id"] for e in entries
            if isinstance(e, dict) and e.get("ledger_status") == "formal"]


def build_plan(storyboard, dubbing=None, asset_ledger=None):
    """把合契约的分镜推导成逐镜工单；不合契约直接抛 StoryboardError。"""
    problems = validate_storyboard(storyboard)
    if problems:
        raise StoryboardError(problems)

    align_index = {}
    for a in _alignments(dubbing):
        key = (a.get("segment"), a.get("cut"))
        align_index[key] = a
    ledger_ids = _ledger_formal_ids(asset_ledger)

    shots = []
    for ep in storyboard["episodes"]:
        for seg in ep["segments"]:
            for ci, cut in enumerate(seg["cuts"]):
                cut_no = ci + 1
                shot_id = f"{seg['id']}-f{cut_no}"
                al = align_index.get((seg["id"], cut_no))
                speaker = al.get("speaker") if al else None
                planned = al.get("seconds") if al else None
                dialogue = al is not None
                on_cam = dialogue and speaker != "VO"
                shots.append({
                    "shot_id": shot_id,
                    "segment_id": seg["id"],
                    "ep": ep["ep"],
                    "scene_index": seg["sceneIndex"],
                    "cut_index": cut_no,
                    "prompt_ref": {"segment_id": seg["id"], "field": "h3Prompt"},
                    "asset_refs": {
                        "characters": list(cut.get("characters", [])),
                        "props": list(cut.get("props", [])),
                        "scene_index": seg["sceneIndex"],
                        "ledger_asset_ids": list(ledger_ids),
                    },
                    "size": cut["size"],
                    "camera": cut["camera"],
                    "frame": cut["frame"],
                    "duration": cut["seconds"],
                    "dialogue": dialogue,
                    "on_camera_dialogue": on_cam,
                    "speaker": speaker,
                    "dialogue_planned_seconds": planned,
                })

    return {
        "contract": "storyboard",
        "source": storyboard.get("source"),
        "style": storyboard.get("style", "realistic"),
        "promptLang": storyboard.get("promptLang", "en"),
        "generated_by": SOURCE_SKILL,
        "lip_sync": {"owner": SOURCE_SKILL, "default_mode": "off"},
        "tts": {"drift_threshold_default": 0.35},
        "shots": shots,
    }
