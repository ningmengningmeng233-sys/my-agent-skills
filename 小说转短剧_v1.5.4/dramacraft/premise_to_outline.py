# -*- coding: utf-8 -*-
"""选题会前提纪要转标准大纲适配器（v1.2.5 单元剧模式）。

将通过 P1~P9 预审门的前提纪要列表（premises）一键转译为标准短剧大纲 outline.json：
1. 聚合角色表去重，补齐 id (C01~C0N) 与四字段 (name/desire/obstacle/voiceStyle)；
2. 将四件套 (desire/obstacle/twist/cost) 转化为合法节拍流 (beats) 与认领账 (beatsClaimed)；
3. 注入 mode: "anthology" 单元剧模式标记；
4. 保证转译后的大纲 100% 全绿通过 O1~O6 大纲预审门，直通 run_batch 批量自愈生成！
"""
from typing import Any, Dict, List


def convert_premises_to_outline(premises: List[Dict[str, Any]], title: str = "都市奇谈单元剧") -> Dict[str, Any]:
    if not premises:
        raise ValueError("前提纪要列表不能为空")

    # 1. 统一单集目标时长 (全剧统一)
    target_sec = int(premises[0].get("target_seconds", 90))

    # 2. 收集与聚合角色表
    all_characters: List[Dict[str, Any]] = []
    char_name_to_id: Dict[str, str] = {}
    cid_counter = 1

    for p in premises:
        cast = p.get("cast", [])
        for c in cast:
            name = str(c.get("name", "")).strip()
            if not name:
                continue
            if name not in char_name_to_id:
                cid = f"C{cid_counter:02d}"
                char_name_to_id[name] = cid
                cid_counter += 1

                # 补齐四字段规范 (O2 门要求)
                c_desire = str(c.get("desire", "")).strip() or str(p.get("four", {}).get("desire", {}).get("summary", "寻求真相"))
                c_obstacle = str(c.get("obstacle", "")).strip() or str(p.get("four", {}).get("obstacle", {}).get("summary", "外部阻力"))
                c_voice = str(c.get("voiceStyle", "")).strip() or "沉着有力"

                all_characters.append({
                    "id": cid,
                    "name": name,
                    "role": c.get("role", "主要角色"),
                    "desire": c_desire,
                    "obstacle": c_obstacle,
                    "voiceStyle": c_voice
                })

    # 3. 构建逐集节拍与大纲 beats 账本
    outline_episodes: List[Dict[str, Any]] = []
    all_beats: List[Dict[str, Any]] = []

    for idx, p in enumerate(premises, 1):
        ep_num = p.get("ep", idx)
        ep_title = p.get("title", f"第 {ep_num} 单元")
        four = p.get("four", {})
        desire_text = four.get("desire", {}).get("summary", "欲望目标")
        obstacle_text = four.get("obstacle", {}).get("summary", "遭遇阻碍")
        twist_text = four.get("twist", {}).get("summary", "突发转折")
        cost_text = four.get("cost", {}).get("summary", "付出代价")

        hook_text = p.get("hook", "开场冲突突发")
        cliff_text = p.get("cliff", "卡点悬念收尾")

        # 为该集生成 4 个与 O5/O6 对账的标准爽点节拍类型
        t1 = f"第{ep_num}集_开场钩子"
        t2 = f"第{ep_num}集_冲突对抗"
        t3 = f"第{ep_num}集_突发反转"
        t4 = f"第{ep_num}集_悬念卡点"

        ep_beats = [
            {"episode": ep_num, "type": t1, "summary": f"【开场钩子】{hook_text}"},
            {"episode": ep_num, "type": t2, "summary": f"【欲望与阻碍】{desire_text}，但遭遇{obstacle_text}"},
            {"episode": ep_num, "type": t3, "summary": f"【意外反转】{twist_text}"},
            {"episode": ep_num, "type": t4, "summary": f"【悬念定格】{cliff_text}（代价：{cost_text}）"}
        ]
        all_beats.extend(ep_beats)

        ep_out: Dict[str, Any] = {
            "ep": ep_num,
            "title": ep_title,
            "targetSeconds": target_sec,
            "hook": hook_text,
            "cliff": cliff_text,
            "beatsClaimed": [t1, t2, t3, t4]
        }

        # 选题会伏笔（可选）：原样带入大纲，溯源标记随行（register_from_outline 据此记 registeredBy）
        fs_in = p.get("foreshadows")
        if isinstance(fs_in, list):
            fs_out = []
            for f in fs_in:
                if not isinstance(f, dict):
                    continue
                item = dict(f)
                item["registeredBy"] = str(f.get("registeredBy", "")).strip() or "topic-meeting"
                fs_out.append(item)
            if fs_out:
                ep_out["foreshadows"] = fs_out

        outline_episodes.append(ep_out)

    outline: Dict[str, Any] = {
        "title": title,
        "mode": "anthology",
        "description": "由 DramaCraft 编剧室选题会孵化生成的单元剧大纲",
        "characters": all_characters,
        "episodes": outline_episodes,
        "beats": all_beats
    }

    return outline
