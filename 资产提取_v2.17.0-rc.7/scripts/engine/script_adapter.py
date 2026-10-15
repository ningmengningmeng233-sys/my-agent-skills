# -*- coding: utf-8 -*-
"""剧本输入适配器（W04）。

将 schemas/script.schema.json 规范的 script.json 结构解析为：
1. 带场次与节拍定位的规范化文本（供拆段与原文检索）；
2. 结构化人物、场景、道具实体与其来源引用 source_refs（集/场/节拍/台词精确坐标）；
3. 资产提取索引数据结构，保证 C##/S##/P## 与 CH_/SC_/PR_ 的双向追踪。
"""
from __future__ import annotations

from typing import Any, Dict, List


def script_to_markdown(script: Dict[str, Any]) -> str:
    """将剧本转换为保留场次标题与定位标记的 Markdown 原文。"""
    lines: List[str] = []
    source = script.get("source", "剧本")
    lines.append(f"# 《{source}》剧本原文\n")

    for ep_info in script.get("episodes", []):
        ep = ep_info.get("ep", 1)
        lines.append(f"\n# 第{ep}集\n")
        scenes = ep_info.get("scenes", [])
        for s_idx, scene in enumerate(scenes, 1):
            sid = scene.get("sceneId", f"S{s_idx:02d}")
            loc = scene.get("location", "未命名场景")
            lighting = scene.get("lighting", "")
            chars = scene.get("characters", [])
            props = scene.get("props", [])

            lines.append(f"\n第{ep}集 第{s_idx}场 {sid} {loc}\n")
            if lighting:
                lines.append(f"【光照】{lighting}")
            if chars:
                lines.append(f"【出场人物】{', '.join(chars)}")
            if props:
                lines.append(f"【道具】{', '.join(props)}")
            lines.append("")

            for b_idx, beat in enumerate(scene.get("flow", []), 1):
                b_type = beat.get("type")
                if b_type == "action" or "action" in beat:
                    lines.append(f"▲ {beat.get('action', '')}")
                elif b_type == "dialogue" or "line" in beat:
                    speaker = beat.get("speaker", "旁白")
                    line = beat.get("line", "")
                    delivery = beat.get("delivery", "")
                    deliv_str = f"（{delivery}）" if delivery else ""
                    lines.append(f"{speaker}{deliv_str}：{line}")
            lines.append("")
    return "\n".join(lines)


def extract_script_entities(script: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """从 script.json 中结构化提取人物、场景、道具实体与定位。"""
    version = str(script.get("version", "1.0.0"))
    chars: Dict[str, Dict[str, Any]] = {}
    scenes: Dict[str, Dict[str, Any]] = {}
    props: Dict[str, Dict[str, Any]] = {}

    for ep_info in script.get("episodes", []):
        ep = int(ep_info.get("ep", 1))
        for scene in ep_info.get("scenes", []):
            sid = str(scene.get("sceneId", "")).strip()
            loc = str(scene.get("location", "")).strip() or sid
            if sid:
                s_ref = {
                    "source_type": "script",
                    "source_id": sid,
                    "script_version": version,
                    "episode": ep,
                    "scene_id": sid,
                    "quote": loc,
                }
                if sid not in scenes:
                    scenes[sid] = {
                        "id": sid,
                        "name": loc,
                        "source_refs": [s_ref],
                    }
                else:
                    scenes[sid]["source_refs"].append(s_ref)

            # 人物
            for cid in scene.get("characters", []):
                cid = str(cid).strip()
                if not cid:
                    continue
                c_ref = {
                    "source_type": "script",
                    "source_id": cid,
                    "script_version": version,
                    "episode": ep,
                    "scene_id": sid,
                    "appearance_note": f"第{ep}集{sid}场出场",
                }
                if cid not in chars:
                    chars[cid] = {
                        "id": cid,
                        "name": cid,  # 初始用 ID，随后从台词/动作中尝试匹配更优名
                        "source_refs": [c_ref],
                        "lines": [],
                    }
                else:
                    chars[cid]["source_refs"].append(c_ref)

            # 道具
            for pid in scene.get("props", []):
                pid = str(pid).strip()
                if not pid:
                    continue
                p_ref = {
                    "source_type": "script",
                    "source_id": pid,
                    "script_version": version,
                    "episode": ep,
                    "scene_id": sid,
                    "appearance_note": f"第{ep}集{sid}场道具",
                }
                if pid not in props:
                    props[pid] = {
                        "id": pid,
                        "name": pid,
                        "source_refs": [p_ref],
                    }
                else:
                    props[pid]["source_refs"].append(p_ref)

            # 遍历 flow：补全台词引用与说话人关联
            for b_idx, beat in enumerate(scene.get("flow", []), 1):
                speaker = str(beat.get("speaker", "")).strip()
                line = str(beat.get("line", "")).strip()
                action = str(beat.get("action", "")).strip()
                if speaker and speaker != "VO":
                    if speaker not in chars:
                        chars[speaker] = {
                            "id": speaker,
                            "name": speaker,
                            "source_refs": [],
                            "lines": [],
                        }
                    d_ref = {
                        "source_type": "script",
                        "source_id": speaker,
                        "script_version": version,
                        "episode": ep,
                        "scene_id": sid,
                        "beat_index": b_idx,
                        "quote": line,
                    }
                    chars[speaker]["source_refs"].append(d_ref)
                    chars[speaker]["lines"].append(line)

                # 从动作中寻找可能的人名别称提示（可选增强）
                if action and chars:
                    for cid, c_data in chars.items():
                        if c_data["name"] == cid:
                            # 尝试从动作首词找中文名
                            import re
                            m = re.match(r"^([A-Za-z0-9\u4e00-\u9fa5]{2,6})(?:推开|走|持|看|见|拿|翻|大怒|笑|拔|拔出)", action)
                            if m and m.group(1) not in ("他们", "众人", "此时", "忽然"):
                                c_data["name"] = m.group(1)

    return {"characters": chars, "scenes": scenes, "props": props}


def adapt_script_to_assets(script: Dict[str, Any]) -> Dict[str, Any]:
    """将 script.json 适配为资产提取输入字典（含 source_refs）。"""
    entities = extract_script_entities(script)
    chars = entities["characters"]
    scenes = entities["scenes"]
    props = entities["props"]

    assets: List[Dict[str, Any]] = []

    # 1. 人物
    for cid, data in chars.items():
        name = data["name"]
        assets.append({
            "asset_id": f"CH_{name}",
            "stable_key": f"CHR:{cid}",
            "name": name,
            "type": "人物",
            "aliases": [cid] if cid != name else [],
            "priority": "P0",
            "fact_level": "剧本事实",
            "status": "CONFIRMED",
            "source_refs": data["source_refs"],
            "positions": f"剧本 {cid}",
            "remark": f"剧本角色（来源编号: {cid}）",
        })

    # 2. 场景
    for sid, data in scenes.items():
        name = data["name"]
        assets.append({
            "asset_id": f"SC_{name}",
            "stable_key": f"SCN:{sid}",
            "name": name,
            "type": "场景",
            "aliases": [sid] if sid != name else [],
            "priority": "P1",
            "fact_level": "剧本事实",
            "status": "CONFIRMED",
            "source_refs": data["source_refs"],
            "positions": f"剧本 {sid}",
            "remark": f"剧本场景（来源编号: {sid}）",
        })

    # 3. 道具
    for pid, data in props.items():
        name = data["name"]
        assets.append({
            "asset_id": f"PR_{name}",
            "stable_key": f"PRP:{pid}",
            "name": name,
            "type": "道具",
            "aliases": [pid] if pid != name else [],
            "priority": "P2",
            "fact_level": "剧本事实",
            "status": "CONFIRMED",
            "source_refs": data["source_refs"],
            "positions": f"剧本 {pid}",
            "remark": f"剧本道具（来源编号: {pid}）",
        })

    return {
        "schema_version": 1,
        "source": script.get("source", "剧本"),
        "assets": assets,
    }
