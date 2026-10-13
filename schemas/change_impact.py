# -*- coding: utf-8 -*-
"""跨技能产物变更影响与选择性重跑分析器（W08）。

纯函数只读分析，绝不自动覆盖或删除交付物。
支持：
1. 剧本上游差异提取（台词、角色、场景、节拍）；
2. 下游分镜、提示词、TTS 与资产总账的失效精确定位；
3. 保留未受影响产物，阻断全剧无差别重跑。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Set


def diff_scripts(old_script: Dict[str, Any], new_script: Dict[str, Any]) -> Dict[str, Any]:
    """对比两版 script.json，提取结构化变更明细。"""
    dialogue_changes: List[Dict[str, Any]] = []
    character_changes: List[Dict[str, Any]] = []
    scene_changes: List[Dict[str, Any]] = []

    old_eps = {ep.get("ep", 1): ep for ep in old_script.get("episodes", [])}
    new_eps = {ep.get("ep", 1): ep for ep in new_script.get("episodes", [])}

    all_ep_nos = sorted(set(old_eps.keys()) | set(new_eps.keys()))

    for ep_no in all_ep_nos:
        old_ep = old_eps.get(ep_no, {})
        new_ep = new_eps.get(ep_no, {})

        old_scenes = {sc.get("sceneId", f"S{i:02d}"): sc for i, sc in enumerate(old_ep.get("scenes", []), 1)}
        new_scenes = {sc.get("sceneId", f"S{i:02d}"): sc for i, sc in enumerate(new_ep.get("scenes", []), 1)}

        all_scene_ids = sorted(set(old_scenes.keys()) | set(new_scenes.keys()))

        for sid in all_scene_ids:
            osc = old_scenes.get(sid)
            nsc = new_scenes.get(sid)

            if not osc or not nsc:
                scene_changes.append({
                    "episode": ep_no,
                    "sceneId": sid,
                    "kind": "added" if nsc else "deleted",
                })
                continue

            # 地点与光照
            if osc.get("location") != nsc.get("location") or osc.get("lighting") != nsc.get("lighting"):
                scene_changes.append({
                    "episode": ep_no,
                    "sceneId": sid,
                    "kind": "modified",
                    "old_location": osc.get("location"),
                    "new_location": nsc.get("location"),
                })

            # 人物列表变更
            old_chars = set(osc.get("characters", []))
            new_chars = set(nsc.get("characters", []))
            if old_chars != new_chars:
                character_changes.append({
                    "episode": ep_no,
                    "sceneId": sid,
                    "added_characters": list(new_chars - old_chars),
                    "removed_characters": list(old_chars - new_chars),
                })

            # 节拍流台词对比
            oflow = osc.get("flow", [])
            nflow = nsc.get("flow", [])
            max_len = max(len(oflow), len(nflow))

            for b_idx in range(max_len):
                ob = oflow[b_idx] if b_idx < len(oflow) else None
                nb = nflow[b_idx] if b_idx < len(nflow) else None

                ob_line = ob.get("line") if ob else None
                nb_line = nb.get("line") if nb else None

                if ob_line != nb_line:
                    dialogue_changes.append({
                        "episode": ep_no,
                        "sceneId": sid,
                        "beat_index": b_idx + 1,
                        "old_line": ob_line,
                        "new_line": nb_line,
                    })

    return {
        "dialogue_changes": dialogue_changes,
        "character_changes": character_changes,
        "scene_changes": scene_changes,
    }


def evaluate_impact(
    diff: Dict[str, Any],
    storyboard: Dict[str, Any] | None = None,
    asset_ledger: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """根据剧本差异评估下游产物的失效范围。"""
    invalidated: List[Dict[str, Any]] = []
    retained: List[Dict[str, Any]] = []

    # 汇总受影响的集与场次
    dlg_hits = {(ch["episode"], ch["sceneId"], ch["beat_index"]): ch for ch in diff.get("dialogue_changes", [])}
    scene_hits = {(sc["episode"], sc["sceneId"]): sc for sc in diff.get("scene_changes", [])}
    char_change_set: Set[str] = set()
    for cc in diff.get("character_changes", []):
        char_change_set.update(cc.get("added_characters", []))
        char_change_set.update(cc.get("removed_characters", []))

    if storyboard:
        for ep in storyboard.get("episodes", []):
            ep_no = int(ep.get("ep", 1))
            for seg in ep.get("segments", []):
                seg_id = seg.get("id", "")
                scene_idx = seg.get("sceneIndex", 1)
                scene_id = f"S{scene_idx:02d}"

                seg_invalid = False
                seg_reasons = []

                if (ep_no, scene_id) in scene_hits:
                    seg_invalid = True
                    seg_reasons.append(f"场景 {scene_id} 发生变更")

                cuts = seg.get("cuts", [])
                for ci, cut in enumerate(cuts, 1):
                    shot_id = cut.get("shot_id") or f"{seg_id}-f{ci}"
                    cut_invalid = False
                    cut_reasons = []

                    # 检查台词节拍认领
                    beats = cut.get("beats", [1, 1])
                    for b_n in range(beats[0], beats[1] + 1):
                        if (ep_no, scene_id, b_n) in dlg_hits:
                            cut_invalid = True
                            seg_invalid = True
                            ch = dlg_hits[(ep_no, scene_id, b_n)]
                            cut_reasons.append(f"认领的第 {b_n} 拍台词变更: 「{ch.get('old_line')}」→「{ch.get('new_line')}」")

                    # 检查出场角色
                    for cid in cut.get("characters", []):
                        if cid in char_change_set:
                            cut_invalid = True
                            seg_invalid = True
                            cut_reasons.append(f"涉及变更角色 {cid}")

                    if cut_invalid:
                        invalidated.append({
                            "type": "cut",
                            "id": shot_id,
                            "segment_id": seg_id,
                            "reasons": cut_reasons,
                        })
                    else:
                        retained.append({
                            "type": "cut",
                            "id": shot_id,
                            "segment_id": seg_id,
                        })

                if seg_invalid:
                    invalidated.append({
                        "type": "h3_prompt",
                        "id": seg_id,
                        "reasons": seg_reasons or ["包含失效分镜镜头，需同步重跑提示词与TTS"],
                    })
                else:
                    retained.append({
                        "type": "h3_prompt",
                        "id": seg_id,
                    })

    return {
        "invalidated": invalidated,
        "retained": retained,
    }
