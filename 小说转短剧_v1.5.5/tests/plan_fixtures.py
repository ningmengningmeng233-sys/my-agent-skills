# -*- coding: utf-8 -*-
"""T2 规划件夹具构造器（测试支撑，**不进发布包**）。

手艺：合法规划件由定稿剧本**机械派生**（1:1 映射，台词逐字抄），各类硬伤件由
变异函数从合法件派生——剧本改一处，夹具跟着变，不会悄悄失配（比手抄 JSON 稳）。

- `stats` 一律保持**源集口径**（H3 就是拿它跟源对账），除 `lie_stats` 故意虚报、以及
  合并/拆场变异只改 `stats.scenes`（场数是规划件自身结构，不是源口径）。
- 需要真文件时（T3/T7 用）：`python tests/plan_fixtures.py --out fixtures/`
"""
import copy
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures")


def load_json(path):
    with io.open(path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def sample_script():
    return load_json(os.path.join(FIXTURES, "sample_script.json"))


def get_ep(doc, n=1):
    """从剧本文件或单集件里取第 n 集。"""
    if isinstance(doc, dict) and isinstance(doc.get("episodes"), list):
        for ep in doc["episodes"]:
            if ep.get("ep") == n:
                return ep
        raise KeyError(f"剧本集中没有第 {n} 集")
    return doc


def dialogues(scene):
    """源场台词序列 [(speaker, line)]，与 validate.mjs 同源（flow 里 type == dialogue）。"""
    out = []
    for beat in scene.get("flow") or []:
        if isinstance(beat, dict) and beat.get("type") == "dialogue":
            out.append((str(beat.get("speaker", "")), str(beat.get("line", ""))))
    return out


def source_totals(ep):
    lines = [d for sc in ep.get("scenes") or [] for d in dialogues(sc)]
    return {
        "scenes": len(ep.get("scenes") or []),
        "intLines": len(lines),
        "intChars": sum(len(t) for _, t in lines),
    }


def make_valid_plan(ep, ep_num=None):
    """1:1 合法规划件：每源场 → 每规划场；台词逐字抄；cast/place 源自本集。"""
    ep_num = ep_num if ep_num is not None else ep.get("ep", 1)
    scenes = []
    for idx, sc in enumerate(ep.get("scenes") or [], start=1):
        loc = str(sc.get("location", "")).strip()
        scenes.append({
            "planSceneId": f"Sc{idx}",
            "srcSceneIds": [str(sc.get("sceneId", ""))],
            "place": (loc + "·暴雨压城") if loc else "未标注地点·承接上场",
            "cast": [str(c) for c in (sc.get("characters") or [])],
            "lines": [{"speaker": sp, "text": tx} for sp, tx in dialogues(sc)],
            "emotion": {"level": 3, "tone": "羞辱压顶·隐忍不发", "shift": None},
            "transitions": [],
            "notes": "情感砸点：掌心按碎瓷；一致性锚点：掌心伤口；环境音：瓷片碎裂、雨声；易错提示：台词密集",
        })
    return {"ep": ep_num, "schemaVersion": 1, "stats": source_totals(ep), "scenes": scenes}


# ------------------------------------------------------------------ 变异件

def drop_scene(plan, src_id):
    """漏场：把某个源场的映射整个删掉（覆盖缺口）。"""
    p = copy.deepcopy(plan)
    p["scenes"] = [s for s in p["scenes"] if src_id not in s["srcSceneIds"]]
    return _renumber(p)


def reference_ghost_scene(plan, ghost="S99"):
    """规划场引用不存在的源场（张冠李戴）。"""
    p = copy.deepcopy(plan)
    p["scenes"][0]["srcSceneIds"] = list(p["scenes"][0]["srcSceneIds"]) + [ghost]
    return p


def break_numbering(plan):
    """编号不连续（红线 4：按序连续编号，不漏不重）。"""
    p = copy.deepcopy(plan)
    p["scenes"][1]["planSceneId"] = "Sc7"
    return p


def wrong_speaker(plan, scene_index=0, line_index=0):
    """张冠李戴：台词挂了本场没说话、但**本集登记过**的说话人（H4 放行、H2 抓）。"""
    p = copy.deepcopy(plan)
    scene = p["scenes"][scene_index]
    spoken = {ln["speaker"] for ln in scene["lines"]}
    roster = [c for s in p["scenes"] for c in s["cast"]]
    cand = next((c for c in roster if c not in spoken), None)
    scene["lines"][line_index]["speaker"] = cand or "C09"
    return p


def drop_line(plan, scene_index=0, line_index=0):
    """丢句：剧本句没被规划场引用。"""
    p = copy.deepcopy(plan)
    del p["scenes"][scene_index]["lines"][line_index]
    return p


def invent_line(plan, scene_index=0):
    """编句：规划场里出现剧本没有的台词（创作只许在过渡，禁在场内）。"""
    p = copy.deepcopy(plan)
    p["scenes"][scene_index]["lines"] = list(p["scenes"][scene_index]["lines"]) + [
        {"speaker": "C01", "text": "这句台词剧本里根本没有。"}]
    return p


def lie_stats(plan, delta=7):
    p = copy.deepcopy(plan)
    p["stats"]["intChars"] = p["stats"]["intChars"] + delta
    return p


def unknown_cast(plan, scene_index=0, code="C99"):
    p = copy.deepcopy(plan)
    p["scenes"][scene_index]["cast"] = list(p["scenes"][scene_index]["cast"]) + [code]
    return p


def unknown_place(plan, scene_index=0):
    p = copy.deepcopy(plan)
    p["scenes"][scene_index]["place"] = "龙潭别墅·深夜灭门"
    return p


def anchored_place_with_summary(plan, scene_index=0):
    """合法：地点 + 「·概况」（原件输出结构就是「地点·概况」）。"""
    p = copy.deepcopy(plan)
    loc = p["scenes"][scene_index]["place"].split("·")[0]
    p["scenes"][scene_index]["place"] = loc + "·窗边暴雨初歇（拆场补充视角）"
    return p


def merge_two_scenes(plan):
    """禁止并：两场并一场（场数 < 源场数，H5 抓）。"""
    p = copy.deepcopy(plan)
    a, b = p["scenes"][0], p["scenes"][1]
    a["srcSceneIds"] = list(a["srcSceneIds"]) + list(b["srcSceneIds"])
    a["lines"] = list(a["lines"]) + list(b["lines"])
    a["cast"] = list(dict.fromkeys(list(a["cast"]) + list(b["cast"])))
    p["scenes"] = [s for i, s in enumerate(p["scenes"]) if i != 1]
    p = _renumber(p)
    p["stats"]["scenes"] = len(p["scenes"])
    return p


def split_first_scene(plan):
    """允许拆：一场拆两场（两场都映射同一源场，场数 > 源场数，全绿）。"""
    p = copy.deepcopy(plan)
    first = p["scenes"][0]
    lines = list(first["lines"])
    half = max(1, len(lines) // 2)
    left = dict(first, lines=lines[:half])
    right = dict(first, lines=lines[half:])
    p["scenes"] = [left, right] + p["scenes"][1:]
    p = _renumber(p)
    p["stats"]["scenes"] = len(p["scenes"])
    return p


def with_transition(plan, text="空镜：摇向窗外飘雪 → 淡入下一场", scene_index=0):
    p = copy.deepcopy(plan)
    p["scenes"][scene_index]["transitions"] = [text]
    return p


def with_notes(plan, text, scene_index=0):
    p = copy.deepcopy(plan)
    p["scenes"][scene_index]["notes"] = text
    return p


# ------------------------------------------------------------------ 内部

def _renumber(plan):
    for idx, sc in enumerate(plan["scenes"], start=1):
        sc["planSceneId"] = f"Sc{idx}"
    return plan


def materialize(out_dir=FIXTURES):
    """把四个真文件写到 fixtures/（T3/T7 需要落盘件时用）。"""
    ep = get_ep(sample_script(), 1)
    plan = make_valid_plan(ep)
    files = {
        "plan_ok.json": plan,
        "plan_missing_scene.json": drop_scene(plan, "S02"),
        "plan_wrong_speaker.json": None,   # 说话人变异需要源台词，见下
        "plan_transitions_ok.json": with_transition(plan),
    }
    src_scenes = ep.get("scenes") or []
    if len(src_scenes) >= 2:
        files["plan_missing_scene.json"] = drop_scene(plan, str(src_scenes[1].get("sceneId")))
    files["plan_wrong_speaker.json"] = _wrong_speaker_vs_source(plan, src_scenes)
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for name, payload in files.items():
        path = os.path.join(out_dir, name)
        with io.open(path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
        written.append(path)
    return written


def _wrong_speaker_vs_source(plan, src_scenes):
    """把第一场某句的说话人换成「本集登记过但没在本场说话」的角色码。"""
    p = copy.deepcopy(plan)
    if not p["scenes"] or not p["scenes"][0]["lines"] or not src_scenes:
        return p
    src = src_scenes[0]
    spoken = {sp for sp, _ in dialogues(src)}
    roster = [str(c) for c in (src.get("characters") or [])]
    cand = next((c for c in roster if c not in spoken), None)
    if cand is None:
        for sc in src_scenes[1:]:
            for c in sc.get("characters") or []:
                if str(c) not in {sp for sp, _ in dialogues(src)}:
                    cand = str(c)
                    break
            if cand:
                break
    if cand:
        p["scenes"][0]["lines"][0]["speaker"] = cand
    return p


if __name__ == "__main__":
    out = FIXTURES
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    for p in materialize(out):
        print(p)
