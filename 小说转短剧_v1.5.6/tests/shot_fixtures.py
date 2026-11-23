# -*- coding: utf-8 -*-
"""T3 镜头组夹具构造器（测试支撑，**不进发布包**）。

合法镜头组由「剧本集 + 规划件」机械派生：按规划场轮转分配组，组时长取 11-13s（中值 12s），
组数取 max(规划场数, round(目标秒/12))，使 Σ中值 落在目标 ±15% 内——与 Omni 卡 v1.2.0 :50-53
的五字段口径同源。硬伤件由变异函数造。
"""
import copy
import math

import plan_fixtures as fx  # 同目录；pytest 会把 tests/ 加进 sys.path

GROUP_SECS = "11-13"          # 中值 12s，端点均在 4~15s 内
GROUP_MID = 12


def make_valid_shots(ep, plan, target_seconds=90, ep_num=None):
    """机械派生合法镜头组件：规划场全覆盖、组时长合规、五字段齐。"""
    ep_num = ep_num if ep_num is not None else plan.get("ep", ep.get("ep", 1))
    n = max(len(plan.get("scenes") or []), max(1, round(target_seconds / GROUP_MID)))
    groups = []
    for i in range(1, n + 1):
        scene = plan["scenes"][(i - 1) % max(1, len(plan["scenes"]))]
        groups.append({
            "gid": f"EP{ep_num:02d}-SG{i:02d}",
            "secs": GROUP_SECS,
            "scene": scene.get("place", ""),
            "cast": list(scene.get("cast") or []),
            "content": f"第{i}组：按{scene.get('planSceneId')}的台词与动作推进冲突。",
            "planSceneId": scene.get("planSceneId"),
        })
    return {"ep": ep_num, "schemaVersion": 1, "groups": groups}


# ------------------------------------------------------------------ 变异件

def gid_hole(shots):
    """编号有洞：抽掉中间一组（剩下的组号不再连续）。"""
    p = copy.deepcopy(shots)
    if len(p["groups"]) > 2:
        del p["groups"][1]
    return p


def duplicate_gid(shots):
    p = copy.deepcopy(shots)
    p["groups"].append(dict(p["groups"][0]))
    return p


def absolute_secs(shots, secs="00:00-00:25"):
    p = copy.deepcopy(shots)
    p["groups"][0]["secs"] = secs
    return p


def out_of_range_secs(shots, secs="18-22"):
    p = copy.deepcopy(shots)
    p["groups"][0]["secs"] = secs
    return p


def thin(shots, keep=2, secs="6-10"):
    """整体敷衍：只留两组 8 秒（中值）去对 90 秒的目标时长。"""
    p = copy.deepcopy(shots)
    p["groups"] = p["groups"][:keep]
    for i, g in enumerate(p["groups"], start=1):
        g["secs"] = secs
        g["gid"] = f"EP{int(p.get('ep', 1)):02d}-SG{i:02d}"
    return p


def gap_on_plan_scene(shots, scene_index=1):
    """某规划场没有组承载（漏承载）。"""
    p = copy.deepcopy(shots)
    if len(p["scenes"] if False else p["groups"]) == 0:
        return p
    target = (p["groups"][scene_index]["planSceneId"]
              if scene_index < len(p["groups"]) else p["groups"][-1]["planSceneId"])
    p["groups"] = [g for g in p["groups"] if g.get("planSceneId") != target]
    for i, g in enumerate(p["groups"], start=1):
        g["gid"] = f"EP{int(p.get('ep', 1)):02d}-SG{i:02d}"
    return p


def ghost_plan_scene(shots, pid="Sc99"):
    p = copy.deepcopy(shots)
    p["groups"][0]["planSceneId"] = pid
    return p


def unknown_cast(shots, code="C99"):
    p = copy.deepcopy(shots)
    p["groups"][0]["cast"] = list(p["groups"][0]["cast"]) + [code]
    return p


def empty_field(shots, field="content"):
    p = copy.deepcopy(shots)
    p["groups"][0][field] = ""
    return p


def no_character_group(shots):
    """空镜/纯环境组：人物为空是合法的（Omni 卡写「无人物写无」）。"""
    p = copy.deepcopy(shots)
    p["groups"][0]["cast"] = []
    return p


def abstract_psych(shots, text="她内心很崩溃，命运感很强，气氛很好。"):
    p = copy.deepcopy(shots)
    p["groups"][0]["content"] = p["groups"][0]["content"] + text
    return p


def foreshadow_ref(shots, fid="F001"):
    p = copy.deepcopy(shots)
    p["groups"][0]["content"] = p["groups"][0]["content"] + f"（回收伏笔 {fid}）"
    return p


def materialize(out_dir):
    """把枪组真文件写到 out_dir（T7 预演用）。"""
    import io
    import os
    import json
    ep = fx.get_ep(fx.sample_script(), 1)
    plan = fx.make_valid_plan(ep)
    shots = make_valid_shots(ep, plan, 90)
    os.makedirs(out_dir, exist_ok=True)
    files = {"shotgroups_ok.json": shots, "shotgroups_thin.json": thin(shots),
             "shotgroups_gap.json": gap_on_plan_scene(shots, 1)}
    out = []
    for name, payload in files.items():
        path = os.path.join(out_dir, name)
        with io.open(path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
        out.append(path)
    return out
