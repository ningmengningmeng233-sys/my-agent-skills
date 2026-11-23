"""D 门族：导演规划机读执法（v1.5.0 T2；方案 §3.1 + ToonFlow 原件红线）。

规划件 schema（本票定版，T3/T4/T7 只读不扩）::

    {"ep": 1, "schemaVersion": 1,
     "stats": {"scenes": 5, "intLines": 24, "intChars": 610},
     "scenes": [{"planSceneId": "Sc1", "srcSceneIds": ["S01"],
                 "place": "顾家客厅·暴雨压城", "cast": ["C01", "C02"],
                 "lines": [{"speaker": "C03", "text": "…"}],
                 "emotion": {"level": 3, "tone": "…", "shift": "试探→破防" | null},
                 "transitions": ["…"], "notes": "…"}]}

硬闸（非空 → 该集回炉；CLI 退出码 2）：
- **H1 逐场映射全覆盖**：剧本每场至少被一个规划场映射；规划场引用的源场必须真实存在；
  `planSceneId` 按序连续编号、不漏不重（原件红线 4）
- **H2 台词双向守恒**：剧本每句 (说话人, 台词) 在其映射规划场里逐字出现；规划场每句必须能在
  **对应源场**命中（禁「本集级」宽松——那正是张冠李戴的漏网口）；**多抄也算违规**（同句复读
  会让下游重拍同一句话，而统计件看不出来）；两侧的空说话人/空台词条目一律落硬伤
- **H3 统计自洽**：`stats` 按**源集**重算对账（条数/字数），场数按规划件自身结构对账
- **H4 切场忠实**：`cast` ⊆ 本集登记角色码（拆场两侧重复登记同一批人是允许的）；`place` 的
  **「·」前那段**必须一字不差等于其源场地点（「·」后是导演补的概况，随他写）；源场自己没写
  `location` 时不能静默放行 → 转 H4 告警（核不了要说出来）
- **H5 场数 ≥ 源场数**：允许拆场，禁止并场
- **H6 情绪字段**：逐场必须有 `emotion.level`（0~10 的整数）与 `emotion.tone` 非空
  （原件 :73-75 / 红线 7：每场都要有浓度与基调）

告警（进报告不回炉；词表误伤不否决交付——v1.4.0 教训）：
- **W1 违禁词**：光影/色温/明暗/色调词与音乐/配乐/乐器词，**全字段生效，含 `transitions`**
  （原件红线 8 / :56 / :84 三处同口径）。:84 给的例外是**创作自由度**——过渡字段可增补剧本
  未写的衔接内容，所以没有任何门把过渡文本对回剧本；这与违禁词禁令是两件事，不要混。
  词表只告**导演自己写的话**：剧本原句（H2 强制逐字照抄）与源场地点名里带这些字眼是剧本的事，
  不反过来算规划件违规——否则忠实照抄反而挨告。

CLI（与门族同合同）::

    python -m dramacraft.plan_lint <plan.json> --script <剧本集.json|剧本.json> --json <报告.json>

退出码：0 = 硬闸全绿；2 = 有硬伤；1 = 用法错或输入不可用（**不落报告文件**，让调用方一眼
看出「没有报告 ≠ 通过」）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 违禁词表单源：规范卡附录只引常量名，不复制第二份表（原件红线 8 / :56 / :84）
FORBIDDEN_LEXICON: Tuple[str, ...] = (
    "光影", "光效", "打光", "灯光", "逆光", "侧光", "顶光", "补光",
    "色温", "色调", "调色", "明暗", "冷光", "暖光", "月光", "日光", "光线",
    "配乐", "音乐", "乐器", "旋律", "BGM", "bgm", "音轨",
)


class PlanInputError(Exception):
    """输入件不可用（缺文件 / 不可解析 / 结构不识别 / 找不到对应集）——CLI exit 1。"""


# 报错里给用户看的字段名（终端直吐 transitions/notes 这类英文键名，人不认识）
FIELD_LABELS: Dict[str, str] = {
    "planSceneId": "场次编号",
    "place": "场景地点",
    "notes": "导演备注",
    "lines": "台词",
    "transitions": "场间过渡",
    "emotion.tone": "情绪基调",
    "emotion.shift": "情绪推进",
}


# ------------------------------------------------------------------ 读取与归一

def _dialogues(scene: Any) -> List[Tuple[str, str]]:
    """源场台词序列 [(说话人, 台词)]，口径与 validate.mjs 同源（flow 里 type == dialogue）。"""
    out: List[Tuple[str, str]] = []
    if not isinstance(scene, dict):
        return out
    for beat in scene.get("flow") or []:
        if isinstance(beat, dict) and beat.get("type") == "dialogue":
            out.append((str(beat.get("speaker") or ""), str(beat.get("line") or "")))
    return out


def _source_totals(ep: Dict[str, Any]) -> Dict[str, int]:
    lines = [d for sc in ep.get("scenes") or [] for d in _dialogues(sc)]
    return {
        "scenes": len(ep.get("scenes") or []),
        "intLines": len(lines),
        "intChars": sum(len(t) for _, t in lines),
    }


def resolve_episode(script: Any, ep_num: Any) -> Dict[str, Any]:
    """剧本件可以是「整本剧本（含 episodes）」或「单集件」；按 plan.ep 取集。

    单集件也要核集号：`plan.ep=9` 配一集 `ep=1` 的单集件，属于拿错文件——按输入错拦住，
    不许当成「对不上就全绿」（评审 Block-1 实证的静默放行）。
    """
    if not isinstance(script, dict):
        raise PlanInputError("剧本件结构无法识别（应为 JSON 对象）")
    episodes = script.get("episodes")
    if isinstance(episodes, list):
        for ep in episodes:
            if isinstance(ep, dict) and ep.get("ep") == ep_num:
                return ep
        raise PlanInputError(f"剧本集中找不到第 {ep_num} 集（规划件 ep={ep_num}）")
    if "scenes" in script:
        got = script.get("ep")
        if ep_num is not None and got is not None and str(got) != str(ep_num):
            raise PlanInputError(f"单集剧本件的集号是 {got}，规划件写的是 {ep_num}——"
                                 f"两件不是同一集，请换对文件再跑")
        return script
    raise PlanInputError("剧本件结构无法识别（既无 episodes 也无 scenes）")


# ------------------------------------------------------------------ 门族

def _plan_scene_text(scene: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """规划场里所有会被读的文本字段 (字段名, 文本, 说话人或空) —— W1 扫描用。"""
    fields: List[Tuple[str, str, str]] = [
        ("planSceneId", str(scene.get("planSceneId") or ""), ""),
        ("place", str(scene.get("place") or ""), ""),
        ("notes", str(scene.get("notes") or ""), ""),
    ]
    for ln in scene.get("lines") or []:
        if isinstance(ln, dict):
            fields.append(("lines", str(ln.get("text") or ""), str(ln.get("speaker") or "")))
    for t in scene.get("transitions") or []:
        fields.append(("transitions", str(t), ""))
    emo = scene.get("emotion")
    if isinstance(emo, dict):
        fields.append(("emotion.tone", str(emo.get("tone") or ""), ""))
        fields.append(("emotion.shift", str(emo.get("shift") or ""), ""))
    return fields


def lint_plan(plan: Any, script: Any) -> Dict[str, Any]:
    """跑 D 门族。返回 {"hard": [...], "warn": [...], "recomputed": {...}}。"""
    if not isinstance(plan, dict):
        raise PlanInputError("规划件结构无法识别（应为 JSON 对象）")
    scenes = plan.get("scenes")
    if not isinstance(scenes, list):
        raise PlanInputError("规划件缺 scenes 列表")

    ep_num = plan.get("ep")
    ep = resolve_episode(script, ep_num)
    label = f"ep{ep_num}"
    src_scenes = [sc for sc in ep.get("scenes") or [] if isinstance(sc, dict)]
    src_ids = [str(sc.get("sceneId") or "") for sc in src_scenes]
    src_map = {str(sc.get("sceneId") or ""): sc for sc in src_scenes}
    plan_scenes = [sc for sc in scenes if isinstance(sc, dict)]
    plan_ids = [str(sc.get("planSceneId") or "") for sc in plan_scenes]

    hard: List[str] = []
    warn: List[str] = []

    # ---- H1 逐场映射全覆盖 + 编号连续 ----
    mapped: List[str] = []
    for sc in plan_scenes:
        pid = str(sc.get("planSceneId") or "").strip() or "(未命名规划场)"
        srcs = [str(x) for x in (sc.get("srcSceneIds") or [])]
        if not srcs:
            hard.append(f"H1 {label} 规划场{pid} 没有填「对应原剧本场次」（srcSceneIds），无法核对")
            continue
        for sid in srcs:
            if sid not in src_map:
                hard.append(f"H1 {label} 规划场{pid} 引用了原剧本里没有的场次 {sid}")
            else:
                mapped.append(sid)
    for sid in src_ids:
        if sid not in mapped:
            hard.append(f"H1 {label} 源场{sid}{_place_hint(src_map[sid])}未被任何规划场映射（漏场）")
    if len(set(plan_ids)) != len(plan_ids):
        dupes = sorted({i for i, n in Counter(plan_ids).items() if n > 1})
        hard.append(f"H1 {label} 规划场编号重复：{', '.join(dupes or ['(空)'])}")
    expected_ids = [f"Sc{i}" for i in range(1, len(plan_ids) + 1)]
    if plan_ids != expected_ids:
        hard.append(f"H1 {label} 规划场编号不连续：请按顺序编成 Sc1…Sc{len(expected_ids)}，"
                    f"现在是 {'/'.join(plan_ids) or '(空)'}")

    # ---- H2 台词双向守恒 ----
    src_line_pool: Counter = Counter()
    for sid in src_ids:
        src_line_pool.update(_dialogues(src_map[sid]))
    # 剧本侧的坏条目（缺说话人/缺台词）不能静默变成空串参与守恒
    for sid in src_ids:
        bad = [(sp, tx) for sp, tx in _dialogues(src_map[sid]) if not sp or not tx]
        if bad:
            hard.append(f"H2 {label} 源场{sid} 有 {len(bad)} 条台词缺说话人或缺台词内容"
                        f"（先把剧本补全，再做逐字守恒核对）")
    plans_of_src: Dict[str, List[Dict[str, Any]]] = {}
    for sc in plan_scenes:
        for sid in (sc.get("srcSceneIds") or []):
            plans_of_src.setdefault(str(sid), []).append(sc)
    for sid in src_ids:
        if sid not in plans_of_src:
            continue
        pool: Counter = Counter()
        for sc in plans_of_src[sid]:
            for ln in sc.get("lines") or []:
                if isinstance(ln, dict):
                    pool[(str(ln.get("speaker") or ""), str(ln.get("text") or ""))] += 1
        for (sp, tx), n in Counter(_dialogues(src_map[sid])).items():
            if pool[(sp, tx)] < n:
                hard.append(f"H2 {label} 源场{sid} 的台词没有逐字出现在对应规划场：{sp}「{_clip(tx)}」")
    for sc in plan_scenes:
        pid = str(sc.get("planSceneId") or "").strip() or "(未命名规划场)"
        src_pool: Counter = Counter()
        for sid in (sc.get("srcSceneIds") or []):
            if str(sid) in src_map:
                src_pool.update(_dialogues(src_map[str(sid)]))
        plan_pool: Counter = Counter()
        for ln in sc.get("lines") or []:
            if not isinstance(ln, dict):
                continue
            key = (str(ln.get("speaker") or ""), str(ln.get("text") or ""))
            plan_pool[key] += 1
            if not key[0] or not key[1]:
                hard.append(f"H2 {label} 规划场{pid} 有台词条目缺说话人或缺台词内容")
        for key, n in plan_pool.items():
            if src_pool[key] <= 0:
                hard.append(f"H2 {label} 规划场{pid} 的台词在原剧本对应场次里找不到"
                            f"（不能挪到别的场次充数）：{key[0] or '(缺说话人)'}「{_clip(key[1])}」")
            elif n > src_pool[key]:
                hard.append(f"H2 {label} 规划场{pid} 把源场台词多抄了 {n - src_pool[key]} 遍"
                            f"（只拆不创作，同一句不许复读）：{key[0]}「{_clip(key[1])}」")

    # ---- H3 统计自洽 ----
    recomputed = _source_totals(ep)
    stats = plan.get("stats") if isinstance(plan.get("stats"), dict) else {}
    if stats.get("scenes") != len(plan_scenes):
        hard.append(f"H3 {label} 场数统计与实际不符：统计写 {stats.get('scenes')} 场，"
                    f"实际规划了 {len(plan_scenes)} 场")
    if stats.get("intLines") != recomputed["intLines"]:
        hard.append(f"H3 {label} 台词条数统计与原文不符：统计写 {stats.get('intLines')} 条，"
                    f"按原剧本重算是 {recomputed['intLines']} 条")
    if stats.get("intChars") != recomputed["intChars"]:
        hard.append(f"H3 {label} 台词字数统计与原文不符：统计写 {stats.get('intChars')} 字，"
                    f"按原剧本重算是 {recomputed['intChars']} 字")

    # ---- H4 切场忠实 ----
    roster = {str(c) for sc in src_scenes for c in (sc.get("characters") or [])}
    src_places = {str(sc.get("sceneId") or ""): str(sc.get("location") or "").strip()
                  for sc in src_scenes}
    for sc in plan_scenes:
        pid = str(sc.get("planSceneId") or "").strip() or "(未命名规划场)"
        for c in sc.get("cast") or []:
            if str(c) not in roster:
                hard.append(f"H4 {label} 规划场{pid} 出现原剧本没登记的角色 {c}（本集登记："
                            f"{'/'.join(sorted(roster)) or '无'}）")
        place = str(sc.get("place") or "").strip()
        if not place:
            hard.append(f"H4 {label} 规划场{pid} 没有填场景地点")
            continue
        # 「源场地点·概况」写法：源场地点**原样照抄打头**，导演补的概况接在它后面。
        # 源场 location 自己就带 · 时（如「东吴大学·校门内林荫道·日」）也照样成立——
        # 判据是「以源地点开头」，不是「按 · 切出第一段再比」（旧写法在带 · 的源地点上永远比不中，
        # 是 2026-09-22 装包实验里导演用穷举实证逼出来的门侧缺陷）。
        anchors = [src_places.get(str(sid), "") for sid in (sc.get("srcSceneIds") or [])]
        anchors = [a for a in anchors if a]
        if not anchors:
            warn.append(f"H4 {label} 规划场{pid} 的场景地点「{place}」没法核对："
                        f"源场没写地点（location）——请在剧本里补上，才能对名")
        elif not any(place == a or place.startswith(a + "·") for a in anchors):
            hard.append(f"H4 {label} 规划场{pid} 的场景地点「{place}」的地点名不是原剧本场次的地点"
                        f"（原为 {'/'.join(anchors)}）——必须原样照抄源场地点打头，概况接在它后面（用 · 分隔）")

    # ---- H5 场数 ----
    if len(plan_scenes) < len(src_scenes):
        hard.append(f"H5 {label} 规划场数 {len(plan_scenes)} < 源场数 {len(src_scenes)}"
                    f"（允许拆场、禁止并场）")

    # ---- H6 情绪字段（原件 :73-75 逐场给 0~10 浓度 + 一句话基调；红线 7 每场都要有）----
    for sc in plan_scenes:
        pid = str(sc.get("planSceneId") or "").strip() or "(未命名规划场)"
        emo = sc.get("emotion")
        if not isinstance(emo, dict):
            hard.append(f"H6 {label} 规划场{pid} 缺情绪分析（浓度 0~10 + 一句话基调）")
            continue
        level = emo.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or not (0 <= level <= 10):
            hard.append(f"H6 {label} 规划场{pid} 的情绪浓度 {level!r} 不合规（要 0~10 的整数）")
        if not str(emo.get("tone") or "").strip():
            hard.append(f"H6 {label} 规划场{pid} 缺情绪基调（一句话写这场的情绪是哪种）")

    # ---- W1 违禁词（全字段，含 transitions）----
    # 只告导演自己写的话：剧本原句被 H2 强制逐字照抄、源场地点名也非导演所起，
    # 这类文本里出现「月光」「光线」等字眼是剧本的事，不是本阶段违规。
    for sc in plan_scenes:
        pid = str(sc.get("planSceneId") or "").strip() or "(未命名规划场)"
        hits: Dict[str, List[str]] = {}
        for field, text, speaker in _plan_scene_text(sc):
            if field == "lines" and src_line_pool[(speaker, text)] > 0:
                continue
            scan = text
            if field == "place":
                # 只有 · 前那段（照抄源场地点）继承豁免；导演补的概况照查（评审 W-2）
                scan = text.split("·", 1)[1] if "·" in text else ""
            found = [w for w in FORBIDDEN_LEXICON if w in scan]
            if found:
                hits.setdefault(field, [])
                for w in found:
                    if w not in hits[field]:
                        hits[field].append(w)
        for field, words in hits.items():
            warn.append(f"W1 {label} 规划场{pid} 的{FIELD_LABELS.get(field, field)}里出现"
                        f"「{'、'.join(words)}」（光影/色温/明暗/色调与音乐/配乐/乐器不属本阶段产物）")

    return {"hard": hard, "warn": warn, "recomputed": recomputed}


def _clip(text: str, limit: int = 18) -> str:
    t = str(text or "")
    return t if len(t) <= limit else t[:limit] + "…"


def _place_hint(scene: Any) -> str:
    """给源场报错带上地点名，人不用来回翻剧本找 S0X 是哪个场。"""
    loc = str(scene.get("location") or "").strip() if isinstance(scene, dict) else ""
    return f"（{loc}）" if loc else ""


# ------------------------------------------------------------------ CLI

class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # 用法错一律 exit 1（与门族合同一致）
        self.print_usage(sys.stderr)
        print(f"plan_lint: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _read_json(path: Any, what: str) -> Any:
    p = Path(str(path))
    if not p.exists():
        raise PlanInputError(f"{what}不存在：{path}")
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError) as err:
        raise PlanInputError(f"{what}不可解析：{path}（{err}）")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = _Parser(prog="python -m dramacraft.plan_lint",
                 description="导演规划检查工具：核对着场覆盖、台词逐字、统计数字、角色地点与违禁词")
    ap.add_argument("plan", help="规划件 JSON 路径")
    ap.add_argument("--script", required=True, help="定稿剧本 JSON 路径（整本剧本或单集件都行）")
    ap.add_argument("--json", required=True, dest="json_path",
                    help="质检报告保存路径（JSON；程序按「退出码 + 报告文件」两道一起判，缺一不算通过）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    try:
        plan = _read_json(args.plan, "规划件")
        script = _read_json(args.script, "剧本件")
        result = lint_plan(plan, script)
    except PlanInputError as err:
        print(f"plan_lint: {err}", file=sys.stderr)
        return 1

    # 先落报告再报喜：报告写不下去时，stdout 里绝不能先出现「合格」（评审 Block-2）
    payload = {"ep": plan.get("ep"), "hard": result["hard"], "warn": result["warn"],
               "recomputed": result["recomputed"]}
    try:
        Path(str(args.json_path)).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8")
    except OSError as err:
        print(f"plan_lint: 报告写不进去：{args.json_path}（{err}）", file=sys.stderr)
        return 1

    print(f"===== 导演规划检查（第 {plan.get('ep')} 集） =====")
    for h in result["hard"]:
        print(f"✗ [必须改] {h}")
    for w in result["warn"]:
        print(f"⚠ [提醒] {w}")
    if not result["hard"]:
        print("✓ 规划合格：分场完整覆盖原剧本、台词一字未改、数字对得上、角色与地点都合规、场数达标。")
    print(f"（按原剧本重算：{result['recomputed']['scenes']} 场 / 台词 "
          f"{result['recomputed']['intLines']} 条 / {result['recomputed']['intChars']} 字）")
    return 2 if result["hard"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
